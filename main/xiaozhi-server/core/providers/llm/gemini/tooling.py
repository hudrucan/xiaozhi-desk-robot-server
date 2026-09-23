import json
from typing import Any, Dict, List

from google.genai import types


_MISSING_THOUGHT_SIGNATURE = b"skip_thought_signature_validator"
_MAX_CACHED_TOOL_CONTEXTS = 256


class GeminiTooling:
    """Build Gemini tools and preserve provider-specific tool context."""

    def __init__(self, native_google_search: bool, model_name: str):
        self.native_google_search = native_google_search
        if native_google_search and not model_name.startswith("gemini-3"):
            raise ValueError(
                "Gemini native Google Search with custom tools requires a "
                "Gemini 3 model"
            )

        self._thought_signatures: Dict[str, bytes] = {}
        self._tool_contexts: Dict[str, types.Content] = {}

    def build_tools(self, funcs: List[Dict[str, Any]] | None):
        custom_funcs = funcs or []
        if self.native_google_search:
            # Other providers still receive the configured web_search plugin.
            # Gemini uses its managed search tool instead.
            custom_funcs = [
                func
                for func in custom_funcs
                if func.get("function", {}).get("name") != "web_search"
            ]

        function_declarations = [
            types.FunctionDeclaration(
                name=func["function"]["name"],
                description=func["function"]["description"],
                parameters_json_schema=func["function"]["parameters"],
            )
            for func in custom_funcs
        ]

        if not function_declarations and not self.native_google_search:
            return None, False

        tool = types.Tool(
            function_declarations=function_declarations or None,
            google_search=(
                types.GoogleSearch() if self.native_google_search else None
            ),
        )
        return [tool], bool(function_declarations)

    def append_model_tool_calls(self, contents, tool_calls):
        """Append exact Gemini context when available, or rebuild a fallback."""
        fallback_parts = []
        cached_content = None
        tool_call_names = {}

        for tool_call in tool_calls:
            tool_call_id = tool_call.get("id")
            tool_name = tool_call["function"]["name"]
            if tool_call_id:
                tool_call_names[tool_call_id] = tool_name
                cached_content = cached_content or self._tool_contexts.get(
                    tool_call_id
                )

            function_call = {
                "name": tool_name,
                "args": json.loads(tool_call["function"]["arguments"]),
            }
            if tool_call_id:
                function_call["id"] = tool_call_id

            fallback_parts.append(
                {
                    "function_call": function_call,
                    "thought_signature": self._thought_signatures.get(
                        tool_call_id, _MISSING_THOUGHT_SIGNATURE
                    ),
                }
            )

        if cached_content is None:
            contents.append({"role": "model", "parts": fallback_parts})
            return tool_call_names

        cached_text = "".join(
            part.text or "" for part in (cached_content.parts or [])
        )
        if (
            cached_text
            and contents
            and contents[-1]["role"] == "model"
            and contents[-1]["parts"] == [{"text": cached_text}]
        ):
            # ConnectionHandler stores streamed pre-tool text separately. The
            # original Gemini content already contains it and its signature.
            contents.pop()

        contents.append(cached_content.model_dump(exclude_none=True))
        return tool_call_names

    def remember_thought_signature(self, tool_call_id: str, signature: bytes):
        self._thought_signatures[tool_call_id] = signature
        self._trim_cache(self._thought_signatures)

    def remember_combined_context(self, tool_calls, response_parts):
        original_content = types.Content(role="model", parts=response_parts)
        for tool_call in tool_calls:
            self._tool_contexts[tool_call.id] = original_content
        self._trim_cache(self._tool_contexts)

    @staticmethod
    def _trim_cache(cache):
        while len(cache) > _MAX_CACHED_TOOL_CONTEXTS:
            oldest_id = next(iter(cache))
            cache.pop(oldest_id, None)
