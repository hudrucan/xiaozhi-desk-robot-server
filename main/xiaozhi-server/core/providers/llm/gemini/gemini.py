import asyncio
import os, json, uuid
import queue
import threading
from concurrent.futures import Future
from types import SimpleNamespace
from typing import Any, Dict, Iterator

import requests
from google import genai
from google.genai import types

from core.providers.llm.base import LLMProviderBase
from core.utils.util import check_model_key
from config.logger import setup_logging
from requests import RequestException
from .tooling import GeminiTooling

log = setup_logging()
TAG = __name__


def test_proxy(proxy_url: str, test_url: str) -> bool:
    try:
        resp = requests.get(test_url, proxies={"http": proxy_url, "https": proxy_url})
        return 200 <= resp.status_code < 400
    except RequestException:
        return False


def setup_proxy_env(http_proxy: str | None, https_proxy: str | None):
    """Validate configured proxies and export the usable proxy variables."""
    test_http_url = "http://www.google.com"
    test_https_url = "https://www.google.com"

    ok_http = ok_https = False

    if http_proxy:
        ok_http = test_proxy(http_proxy, test_http_url)
        if ok_http:
            os.environ["HTTP_PROXY"] = http_proxy
            log.bind(tag=TAG).info(f"Configured Gemini HTTP proxy is reachable: {http_proxy}")
        else:
            log.bind(tag=TAG).warning(f"Configured Gemini HTTP proxy is unavailable: {http_proxy}")

    if https_proxy:
        ok_https = test_proxy(https_proxy, test_https_url)
        if ok_https:
            os.environ["HTTPS_PROXY"] = https_proxy
            log.bind(tag=TAG).info(f"Configured Gemini HTTPS proxy is reachable: {https_proxy}")
        else:
            log.bind(tag=TAG).warning(
                f"Configured Gemini HTTPS proxy is unavailable: {https_proxy}"
            )

    # Reuse the HTTP proxy for HTTPS when it can reach the HTTPS endpoint.
    if ok_http and not ok_https:
        if test_proxy(http_proxy, test_https_url):
            os.environ["HTTPS_PROXY"] = http_proxy
            ok_https = True
            log.bind(tag=TAG).info(f"Reusing HTTP proxy for HTTPS: {http_proxy}")

    if not ok_http and not ok_https:
        log.bind(tag=TAG).error("Gemini proxy setup failed: no configured proxy is reachable")
        raise RuntimeError("No configured Gemini proxy is reachable")


class LLMProvider(LLMProviderBase):
    supports_request_cancellation = True

    def __init__(self, cfg: Dict[str, Any]):
        self.model_name = cfg.get("model_name", "gemini-2.0-flash")
        self.api_key = cfg["api_key"]
        http_proxy = cfg.get("http_proxy")
        https_proxy = cfg.get("https_proxy")

        model_key_msg = check_model_key("LLM", self.api_key)
        if model_key_msg:
            log.bind(tag=TAG).error(model_key_msg)

        if http_proxy or https_proxy:
            log.bind(tag=TAG).info(
                "Gemini proxy configuration detected; validating connectivity"
            )
            setup_proxy_env(http_proxy, https_proxy)
            log.bind(tag=TAG).info(
                f"Gemini proxy setup completed - HTTP: {http_proxy}, HTTPS: {https_proxy}"
            )
        self.timeout = cfg.get("timeout", 120)
        self.native_google_search = bool(cfg.get("native_google_search", False))
        self.tooling = GeminiTooling(
            self.native_google_search, self.model_name
        )
        self.last_native_google_search_used = False

        # Create one provider client and reuse it across turns.
        self.client = genai.Client(api_key=self.api_key)
        self._active_requests: Dict[str, Future] = {}
        self._active_requests_lock = threading.Lock()

        self.gen_cfg = {
            "temperature": 0.7,
            "top_p": 0.9,
            "top_k": 40,
            "max_output_tokens": 2048,
        }

    # Gemini receives the complete dialogue, so no provider session ID is needed.
    def response(self, session_id, dialogue, **kwargs):
        # Native search follows the existing server-tool behavior and is only
        # exposed in function-call mode. The same Gemini config may also be
        # reused by memory or intent providers, where search is not appropriate.
        yield from self._generate(
            session_id,
            dialogue,
            None,
            function_mode=False,
            event_loop=kwargs.get("event_loop"),
        )

    def response_with_functions(
        self,
        session_id,
        dialogue,
        functions=None,
        **kwargs,
    ):
        tools, has_custom_tools = self.tooling.build_tools(functions)
        yield from self._generate(
            session_id,
            dialogue,
            tools,
            function_mode=True,
            combined_tool_mode=self.native_google_search and has_custom_tools,
            validated_tool_mode=has_custom_tools,
            event_loop=kwargs.get("event_loop"),
        )

    def _generate(
        self,
        session_id,
        dialogue,
        tools,
        function_mode,
        combined_tool_mode=False,
        validated_tool_mode=False,
        event_loop=None,
    ):
        self.last_native_google_search_used = False
        role_map = {"assistant": "model", "user": "user"}
        contents: list = []
        tool_call_names = {}
        # Convert the shared dialogue format to Gemini content parts.
        for m in dialogue:
            r = m["role"]

            if r == "assistant" and "tool_calls" in m:
                tool_call_names.update(
                    self.tooling.append_model_tool_calls(
                        contents, m["tool_calls"]
                    )
                )
                continue

            if r == "tool":
                tool_call_id = m.get("tool_call_id")
                tool_name = tool_call_names.get(tool_call_id)
                if not tool_name:
                    log.bind(tag=TAG).warning(
                        f"Missing Gemini function name for tool call {tool_call_id}"
                    )
                    contents.append(
                        {
                            "role": "user",
                            "parts": [{"text": str(m.get("content", ""))}],
                        }
                    )
                    continue

                function_response = {
                    "name": tool_name,
                    "response": {"result": str(m.get("content", ""))},
                }
                if tool_call_id:
                    function_response["id"] = tool_call_id
                response_part = {"function_response": function_response}
                if (
                    contents
                    and contents[-1]["role"] == "user"
                    and all(
                        "function_response" in part
                        for part in contents[-1]["parts"]
                    )
                ):
                    contents[-1]["parts"].append(response_part)
                else:
                    contents.append(
                        {"role": "user", "parts": [response_part]}
                    )
                continue

            contents.append(
                {
                    "role": role_map.get(r, "user"),
                    "parts": [{"text": str(m.get("content", ""))}],
                }
            )

        tool_config = None
        if validated_tool_mode:
            tool_config = types.ToolConfig(
                include_server_side_tool_invocations=(
                    True if combined_tool_mode else None
                ),
                function_calling_config=types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.VALIDATED
                ),
            )

        config = types.GenerateContentConfig(
            **self.gen_cfg,
            tools=tools,
            tool_config=tool_config,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
            http_options=types.HttpOptions(timeout=int(self.timeout * 1000)),
        )
        response_parts = []
        pending_tool_calls = []
        for chunk in self._iter_stream(
            session_id,
            contents,
            config,
            event_loop,
        ):
            if not chunk.candidates:
                continue
            cand = chunk.candidates[0]
            if getattr(cand, "grounding_metadata", None):
                self.last_native_google_search_used = True
            if not cand.content or not cand.content.parts:
                continue
            for part in cand.content.parts:
                response_parts.append(part.model_copy(deep=True))
                server_tool_call = getattr(part, "tool_call", None)
                server_tool_response = getattr(part, "tool_response", None)
                server_tool = server_tool_call or server_tool_response
                if server_tool and "GOOGLE_SEARCH" in str(
                    getattr(server_tool, "tool_type", "")
                ):
                    self.last_native_google_search_used = True
                if getattr(part, "function_call", None):
                    fc = part.function_call
                    tool_call_id = getattr(fc, "id", None) or uuid.uuid4().hex
                    if part.thought_signature:
                        self.tooling.remember_thought_signature(
                            tool_call_id, part.thought_signature
                        )
                    pending_tool_calls.append(
                        SimpleNamespace(
                            index=len(pending_tool_calls),
                            id=tool_call_id,
                            type="function",
                            function=SimpleNamespace(
                                name=fc.name,
                                arguments=json.dumps(
                                    dict(fc.args), ensure_ascii=False
                                ),
                            ),
                        )
                    )
                    continue
                if getattr(part, "text", None):
                    yield (
                        (part.text, None)
                        if function_mode
                        else part.text
                    )

        if pending_tool_calls:
            if combined_tool_mode:
                self.tooling.remember_combined_context(
                    pending_tool_calls, response_parts
                )
            yield None, pending_tool_calls

    def _iter_stream(self, session_id, contents, config, event_loop):
        if event_loop is None:
            stream = self.client.models.generate_content_stream(
                model=self.model_name,
                contents=contents,
                config=config,
            )
            try:
                yield from stream
            finally:
                self._safe_finish_stream(stream)
            return

        output = queue.Queue()
        request = asyncio.run_coroutine_threadsafe(
            self._produce_stream(contents, config, output),
            event_loop,
        )
        request.add_done_callback(lambda _: output.put(("done", None)))
        self._register_request(session_id, request)

        try:
            while True:
                kind, payload = output.get()
                if kind == "chunk":
                    yield payload
                elif kind == "error":
                    raise payload
                elif kind in {"cancelled", "done"}:
                    return
        finally:
            self._unregister_request(session_id, request)
            if not request.done():
                request.cancel()

    async def _produce_stream(self, contents, config, output):
        stream = None
        outcome = None
        try:
            stream = await self.client.aio.models.generate_content_stream(
                model=self.model_name,
                contents=contents,
                config=config,
            )
            async for chunk in stream:
                output.put(("chunk", chunk))
        except asyncio.CancelledError:
            outcome = ("cancelled", None)
        except Exception as error:
            outcome = ("error", error)
        finally:
            if stream is not None and hasattr(stream, "aclose"):
                await stream.aclose()
            if outcome is not None:
                output.put(outcome)

    def _register_request(self, session_id, request):
        if not session_id:
            return
        with self._active_requests_lock:
            previous = self._active_requests.get(session_id)
            self._active_requests[session_id] = request
        if previous is not None and not previous.done():
            previous.cancel()

    def _unregister_request(self, session_id, request):
        if not session_id:
            return
        with self._active_requests_lock:
            if self._active_requests.get(session_id) is request:
                self._active_requests.pop(session_id, None)

    def cancel(self, session_id):
        """Cancel the in-flight request owned by one connection."""
        with self._active_requests_lock:
            request = self._active_requests.get(session_id)
        if request is None or request.done():
            return False
        request.cancel()
        return True

    # Close a stream on abort to stop quota and resource consumption promptly.
    @staticmethod
    def _safe_finish_stream(stream: Iterator[types.GenerateContentResponse]):
        if hasattr(stream, "close"):
            stream.close()
