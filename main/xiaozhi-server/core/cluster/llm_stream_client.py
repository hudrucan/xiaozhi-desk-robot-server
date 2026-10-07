"""One owned, bounded text stream; ACK only after the consumer accepts text."""
import asyncio
import hashlib
import time
import uuid

from . import llm_protocol as rpc
from . import llm_stream_protocol as wire
from .protocol import valid_reply_subject
from .worker_rpc import WorkerRpcError


class LLMStreamClient:
    def __init__(self, client, core_id, revision, messages, seconds, on_chunk, *, tools=None, on_tools=None, memory_context=None):
        self.client, self.on_chunk = client, on_chunk
        self.wire = wire
        self.on_tools = on_tools
        if tools is not None:
            from . import tool_stream_protocol
            self.wire = tool_stream_protocol
        self.value = {'protocol': self.wire.PROTOCOL, 'request_id': uuid.uuid4().hex,
            'cancel_token': uuid.uuid4().hex, 'core_id': core_id, 'revision': revision,
            'deadline_ms': int(time.time() * 1000) + seconds * 1000, 'dialogue': messages}
        if tools is not None:
            self.value['tools'] = tools
        if memory_context is not None:
            if tools is None:
                raise ValueError('Memory context requires the tool-aware stream')
            self.value['memory_context'] = memory_context
        self.payload = rpc.encode(self.value, getattr(self.wire, 'MAX_REQUEST_BYTES', rpc.MAX_REQUEST_BYTES))
        self.wire.request(self.payload)
        self.seconds = seconds
        self.queue = asyncio.Queue(2)
        self.failure = asyncio.get_running_loop().create_future()
        self.subscription = None
        self.closed = False

    def fail(self, code='llm_stream_unavailable'):
        if not self.failure.done() and not self.closed:
            self.failure.set_result(code)

    def require_live(self):
        if self.failure.done():
            raise WorkerRpcError(self.failure.result())
        if self.closed or not self.client.is_connected:
            raise WorkerRpcError('llm_stream_unavailable')

    async def receive(self, message):
        if self.closed:
            return
        try:
            if (getattr(message, 'headers', None) or {}).get('Status') == '503':
                self.fail()
                return
            value = self.wire.parse_event(message.data, self.value['request_id'], self.value['revision'])
            if (value['kind'] != 'error' or message.reply) and (
                    not valid_reply_subject(message.reply) or not message.reply.startswith('_INBOX.')):
                raise ValueError('Invalid stream ACK subject')
            self.queue.put_nowait((message, value))
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, asyncio.QueueFull):
            self.fail('worker_rpc_invalid_reply')

    async def next_event(self):
        pending = asyncio.create_task(self.queue.get())
        try:
            done, _ = await asyncio.wait((pending, self.failure), return_when=asyncio.FIRST_COMPLETED)
            if self.failure in done:
                raise WorkerRpcError(self.failure.result())
            return pending.result()
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def run(self):
        worker_id, seq, total, parts = None, 0, 0, []
        digest = hashlib.sha256()
        inbox = '_INBOX.' + uuid.uuid4().hex
        try:
            async with asyncio.timeout(self.seconds):
                self.subscription = await self.client.subscribe(inbox, cb=self.receive,
                    pending_msgs_limit=4, pending_bytes_limit=4 * self.wire.MAX_EVENT_BYTES)
                await self.client.flush()
                self.require_live()
                await self.client.publish(self.wire.SUBJECT, self.payload, reply=inbox)
                while True:
                    message, value = await self.next_event()
                    self.require_live()
                    if value['seq'] != seq or worker_id is not None and value['worker_id'] != worker_id:
                        raise WorkerRpcError('worker_rpc_invalid_reply')
                    kind = value['kind']
                    if kind == 'error':
                        # Admission errors may precede started; execution errors
                        # use the next sequence number of the admitted worker.
                        if message.reply:
                            await self.client.publish(message.reply, self.wire.ack(value))
                        raise WorkerRpcError(value['error'])
                    if seq == 0:
                        if kind != 'started':
                            raise WorkerRpcError('worker_rpc_invalid_reply')
                        worker_id = value['worker_id']
                    elif kind == 'chunk':
                        encoded = value['text'].encode('utf-8')
                        total += len(encoded)
                        if total > rpc.MAX_TEXT_BYTES:
                            raise WorkerRpcError('llm_output_too_large')
                        # Consumer owns partial text; it must propagate abort or
                        # downstream backpressure rather than enqueue forever.
                        await asyncio.wait_for(self.on_chunk(value['text'], seq - 1), self.wire.ACK_SECONDS)
                        digest.update(encoded)
                        parts.append(value['text'])
                    elif kind == 'tools':
                        results = await self.on_tools(value['calls'])
                        self.require_live()
                        await self.client.publish(message.reply, self.wire.tool_reply(value, results))
                        seq += 1
                        continue
                    elif kind == 'complete':
                        if total != value['text_bytes'] or digest.hexdigest() != value['sha256']:
                            raise WorkerRpcError('worker_rpc_invalid_reply')
                    else:
                        raise WorkerRpcError('worker_rpc_invalid_reply')
                    self.require_live()
                    await self.client.publish(message.reply, self.wire.ack(value))
                    self.require_live()
                    if kind == 'complete':
                        return {'protocol': self.wire.PROTOCOL, 'request_id': self.value['request_id'],
                            'worker_id': worker_id, 'revision': self.value['revision'],
                            'status': 'ok', 'text': ''.join(parts)}
                    seq += 1
        except asyncio.TimeoutError:
            raise WorkerRpcError('llm_expired') from None
        except asyncio.CancelledError:
            raise
        except WorkerRpcError:
            raise
        except Exception:
            raise WorkerRpcError('llm_stream_unavailable') from None
        finally:
            self.closed = True
            cancellation = rpc.encode({'protocol': rpc.PROTOCOL,
                'request_id': self.value['request_id'], 'cancel_token': self.value['cancel_token']}, 512)
            try:
                if self.client.is_connected:
                    await asyncio.wait_for(self.client.publish(rpc.CANCEL_SUBJECT, cancellation), 1)
            except Exception:
                pass
            try:
                if self.subscription:
                    await asyncio.wait_for(self.subscription.unsubscribe(), 1)
            except Exception:
                pass
            self.failure.cancel()
