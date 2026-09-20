import os, json, uuid
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List

import requests
from google import genai
from google.genai import types

from core.providers.llm.base import LLMProviderBase
from core.utils.util import check_model_key
from config.logger import setup_logging
from requests import RequestException

log = setup_logging()
TAG = __name__

_MISSING_THOUGHT_SIGNATURE = b"skip_thought_signature_validator"
_MAX_CACHED_THOUGHT_SIGNATURES = 256


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

        # Create one provider client and reuse it across turns.
        self.client = genai.Client(api_key=self.api_key)

        self.gen_cfg = {
            "temperature": 0.7,
            "top_p": 0.9,
            "top_k": 40,
            "max_output_tokens": 2048,
        }
        self._thought_signatures: Dict[str, bytes] = {}

    @staticmethod
    def _build_tools(funcs: List[Dict[str, Any]] | None):
        if not funcs:
            return None
        return [
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name=f["function"]["name"],
                        description=f["function"]["description"],
                        parameters_json_schema=f["function"]["parameters"],
                    )
                    for f in funcs
                ]
            )
        ]

    # Gemini receives the complete dialogue, so no provider session ID is needed.
    def response(self, session_id, dialogue, **kwargs):
        yield from self._generate(dialogue, None)

    def response_with_functions(self, session_id, dialogue, functions=None):
        yield from self._generate(dialogue, self._build_tools(functions))

    def _generate(self, dialogue, tools):
        role_map = {"assistant": "model", "user": "user"}
        contents: list = []
        # Convert the shared dialogue format to Gemini content parts.
        for m in dialogue:
            r = m["role"]

            if r == "assistant" and "tool_calls" in m:
                tc = m["tool_calls"][0]
                thought_signature = self._thought_signatures.get(
                    tc.get("id"), _MISSING_THOUGHT_SIGNATURE
                )
                contents.append(
                    {
                        "role": "model",
                        "parts": [
                            {
                                "function_call": {
                                    "name": tc["function"]["name"],
                                    "args": json.loads(tc["function"]["arguments"]),
                                },
                                "thought_signature": thought_signature,
                            }
                        ],
                    }
                )
                continue

            if r == "tool":
                contents.append(
                    {
                        "role": "model",
                        "parts": [{"text": str(m.get("content", ""))}],
                    }
                )
                continue

            contents.append(
                {
                    "role": role_map.get(r, "user"),
                    "parts": [{"text": str(m.get("content", ""))}],
                }
            )

        config = types.GenerateContentConfig(
            **self.gen_cfg,
            tools=tools,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
            http_options=types.HttpOptions(timeout=int(self.timeout * 1000)),
        )
        stream = self.client.models.generate_content_stream(
            model=self.model_name,
            contents=contents,
            config=config,
        )

        try:
            for chunk in stream:
                cand = chunk.candidates[0]
                for part in cand.content.parts:
                    # Function call.
                    if getattr(part, "function_call", None):
                        fc = part.function_call
                        tool_call_id = uuid.uuid4().hex
                        if part.thought_signature:
                            self._thought_signatures[tool_call_id] = (
                                part.thought_signature
                            )
                            if (
                                len(self._thought_signatures)
                                > _MAX_CACHED_THOUGHT_SIGNATURES
                            ):
                                oldest_id = next(iter(self._thought_signatures))
                                self._thought_signatures.pop(oldest_id, None)
                        yield None, [
                            SimpleNamespace(
                                id=tool_call_id,
                                type="function",
                                function=SimpleNamespace(
                                    name=fc.name,
                                    arguments=json.dumps(
                                        dict(fc.args), ensure_ascii=False
                                    ),
                                ),
                            )
                        ]
                        return
                    # Regular text output.
                    if getattr(part, "text", None):
                        yield part.text if tools is None else (part.text, None)

        finally:
            if tools is not None:
                yield None, None  # Mark the end of function-call mode.

    # Close a stream on abort to stop quota and resource consumption promptly.
    @staticmethod
    def _safe_finish_stream(stream: Iterator[types.GenerateContentResponse]):
        if hasattr(stream, "close"):
            stream.close()
        else:
            for _ in stream:  # Exhaust streams that do not expose close().
                pass
