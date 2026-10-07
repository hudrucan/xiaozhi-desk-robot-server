"""Three-segment lookahead inside one turn; playback follows segment order."""
import asyncio

from . import tts_protocol as wire
from .tts_segments import SegmentBuffer
from .tts_playback import TTSPlayback
from .worker_rpc import WorkerRpcError

MAX_LOOKAHEAD = 3
MAX_TURN_SECONDS = 120


class TTSTurn:
    def __init__(self, pool, emit, send_audio, *, playback_factory=TTSPlayback):
        soundbank = getattr(pool, 'soundbank', None)
        self.pool, self.buffer = pool, SegmentBuffer(pool.bundle['options'],
                                                    getattr(soundbank, 'entries', None))
        self.playback = playback_factory(emit, send_audio)
        self.changed = asyncio.Event()
        self.slots = asyncio.Semaphore(MAX_LOOKAHEAD)
        self.queue = asyncio.Queue(MAX_LOOKAHEAD)
        self.jobs = set()
        self.failure = asyncio.get_running_loop().create_future()
        self.rpc = getattr(pool, 'rpc', None)
        if self.rpc is not None:
            self.rpc.streams.add(self)
        self.closed = False
        self.task = asyncio.create_task(self.run())
        self.task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

    def fail(self, code='tts_unavailable'):
        # The whole turn is invalidated even when every segment is queued or
        # already buffered locally. A later reconnect only serves new turns.
        if not self.failure.done():
            self.failure.set_result('tts_unavailable')

    def feed(self, text):
        if self.closed:
            raise asyncio.CancelledError
        if self.failure.done():
            raise WorkerRpcError(self.failure.result())
        self.buffer.append(text)
        self.changed.set()

    async def dispatch(self):
        index = 0
        while True:
            await self.slots.acquire()
            self.changed.clear()
            text = self.buffer.pop()
            if text is None:
                self.slots.release()
                if self.buffer.finished:
                    await self.queue.put(None)
                    return
                await self.changed.wait()
                continue
            if index >= wire.MAX_SEGMENTS:
                raise WorkerRpcError('tts_segment_too_large')
            task = asyncio.create_task(self.pool.generate(index, text))
            self.jobs.add(task)
            task.add_done_callback(self.job_finished)
            await self.queue.put(task)
            index += 1

    def job_finished(self, job):
        if not job.cancelled():
            error = job.exception()
            if error and not self.failure.done():
                self.failure.set_result(error.code if isinstance(error, WorkerRpcError) else 'tts_unavailable')

    async def play(self):
        while True:
            job = await self.queue.get()
            if job is None:
                await self.playback.finish()
                return
            result = await job
            await self.playback.segment(result)
            self.jobs.discard(job)
            self.slots.release()

    async def run(self):
        tasks = [asyncio.create_task(self.dispatch()), asyncio.create_task(self.play())]
        joined = asyncio.gather(*tasks)
        try:
            async with asyncio.timeout(MAX_TURN_SECONDS):
                done, _ = await asyncio.wait((joined, self.failure), return_when=asyncio.FIRST_COMPLETED)
                if self.failure in done:
                    raise WorkerRpcError(self.failure.result())
                await joined
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code = error.code if isinstance(error, WorkerRpcError) else 'tts_unavailable'
            if not self.failure.done():
                self.failure.set_result(code)
            raise WorkerRpcError(code) from None
        finally:
            for task in (*tasks, *tuple(self.jobs)):
                if not task.done():
                    task.cancel()
            cleanup = asyncio.gather(*tasks, *tuple(self.jobs), joined, return_exceptions=True)
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            self.jobs.clear()

    async def wait_llm(self, call):
        # A TTS failure invalidates the text stream immediately, even if its
        # provider is waiting silently for another chunk.
        llm = asyncio.create_task(call)
        try:
            done, _ = await asyncio.wait((llm, self.failure), return_when=asyncio.FIRST_COMPLETED)
            if self.failure in done:
                raise WorkerRpcError(self.failure.result())
            return llm.result()
        finally:
            if not llm.done():
                llm.cancel()
            await asyncio.gather(llm, return_exceptions=True)

    async def finish(self):
        self.buffer.finished = True
        self.changed.set()
        await self.task

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        await self.playback.close()
        if self.rpc is not None:
            self.rpc.streams.discard(self)
        self.failure.cancel()
