import asyncio
from typing import List, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from ..base import IntentProviderBase
from config.logger import setup_logging
from core.utils.util import get_system_error_response
import re
import json
import hashlib
import time



TAG = __name__
logger = setup_logging()


class IntentProvider(IntentProviderBase):
    def __init__(self, config):
        super().__init__(config)
        self.llm = None
        self.promot = ""
        # Use the shared cache manager.
        from core.utils.cache.manager import cache_manager, CacheType

        self.cache_manager = cache_manager
        self.CacheType = CacheType
        self.history_count = 4

    def get_intent_system_prompt(self, functions_list: str) -> str:
        """Build the intent classifier prompt from the available functions."""
        functions_desc = "Available functions:\n"
        for func in functions_list:
            func_info = func.get("function", {})
            name = func_info.get("name", "")
            desc = func_info.get("description", "")
            params = func_info.get("parameters", {})

            functions_desc += f"\nFunction: {name}\n"
            functions_desc += f"Description: {desc}\n"

            if params:
                functions_desc += "Parameters:\n"
                for param_name, param_info in params.get("properties", {}).items():
                    param_desc = param_info.get("description", "")
                    param_type = param_info.get("type", "")
                    functions_desc += f"- {param_name} ({param_type}): {param_desc}\n"

            functions_desc += "---\n"

        prompt = (
            "STRICT FORMAT: return valid JSON only, never natural-language text.\n\n"
            "Classify the user's final message and select the matching function.\n\n"
            "Return result_for_context directly for questions about the current time, "
            "today's date or weekday, today's lunar date or solar term, or the current "
            "city. The system will answer these from context.\n"
            "A question about how or why a conversation exited is not an exit command; "
            "return continue_chat. Only call handle_exit_intent for an explicit request "
            "to end the conversation.\n\n"
            f"{functions_desc}\n"
            "Process:\n"
            "1. Determine the user's intent.\n"
            "2. Return result_for_context for the context queries listed above.\n"
            "3. Otherwise select the best matching available function.\n"
            "4. If no function matches, return continue_chat.\n\n"
            "Return one of these JSON shapes:\n"
            '{"function_call": {"name": "function_name", "arguments": {}}}\n'
            '{"function_call": {"name": "continue_chat"}}\n'
            '{"function_call": {"name": "result_for_context"}}\n'
            "Examples:\n"
            'User: "What time is it?"\n'
            'Return: {"function_call": {"name": "result_for_context"}}\n'
            'User: "What is the current battery level?"\n'
            'Return: {"function_call": {"name": "self_battery_get_status"}}\n'
            'User: "Set screen brightness to 50 percent"\n'
            'Return: {"function_call": {"name": "self_screen_set_brightness", '
            '"arguments": {"brightness": 50}}}\n'
            'User: "End this conversation"\n'
            'Return: {"function_call": {"name": "handle_exit_intent", "arguments": {}}}\n'
            'User: "Hello"\n'
            'Return: {"function_call": {"name": "continue_chat"}}\n\n'
            "For multiple explicit commands, return a function_calls array. Include "
            "arguments only when the selected function needs them. Return JSON only, "
            "without Markdown, explanation, or emoji."
        )
        return prompt

    async def replyResult(self, text: str, original_text: str):
        """Generate a concise spoken reply without blocking the event loop."""
        try:
            user_prompt = (
                "Reply naturally and concisely using the information above. "
                "Return only the answer to the user's message: "
                + original_text
            )
            # Run the synchronous provider outside the event loop.
            llm_result = await asyncio.to_thread(
                self.llm.response_no_stream,
                system_prompt=text,
                user_prompt=user_prompt,
            )
            return llm_result
        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in generating reply result: {e}")
            return get_system_error_response(self.config)

    async def detect_intent(
        self, conn: "ConnectionHandler", dialogue_history: List[Dict], text: str
    ) -> str:
        if not self.llm:
            raise ValueError("LLM provider not set")
        if conn.func_handler is None:
            return '{"function_call": {"name": "continue_chat"}}'

        # Track intent latency.
        total_start_time = time.time()

        model_info = getattr(self.llm, "model_name", str(self.llm.__class__.__name__))
        logger.bind(tag=TAG).debug(f"Intent model: {model_info}")

        # Cache by device and utterance.
        cache_key = hashlib.md5((conn.device_id + text).encode()).hexdigest()

        cached_intent = self.cache_manager.get(self.CacheType.INTENT, cache_key)
        if cached_intent is not None:
            cache_time = time.time() - total_start_time
            logger.bind(tag=TAG).debug(
                f"Using cached intent: {cache_key} -> {cached_intent}, elapsed={cache_time:.4f}s"
            )
            return cached_intent

        if self.promot == "":
            functions = conn.func_handler.get_functions()
            if hasattr(conn, "mcp_client"):
                mcp_tools = conn.mcp_client.get_available_tools()
                if mcp_tools is not None and len(mcp_tools) > 0:
                    if functions is None:
                        functions = []
                    functions.extend(mcp_tools)

            self.promot = self.get_intent_system_prompt(functions)

        logger.bind(tag=TAG).debug(f"Intent system prompt: {self.promot}")

        # Build the recent dialogue context.
        msgStr = ""

        start_idx = max(0, len(dialogue_history) - self.history_count)
        for i in range(start_idx, len(dialogue_history)):
            msgStr += f"{dialogue_history[i].role}: {dialogue_history[i].content}\n"

        msgStr += f"User: {text}\n"
        user_prompt = f"current dialogue:\n{msgStr}"

        preprocess_time = time.time() - total_start_time
        logger.bind(tag=TAG).debug(f"Intent preprocessing: {preprocess_time:.4f}s")

        llm_start_time = time.time()
        logger.bind(tag=TAG).debug(f"Starting intent LLM call: model={model_info}")

        try:
            # Run the synchronous provider outside the event loop.
            intent = await asyncio.to_thread(
                self.llm.response_no_stream,
                system_prompt=self.promot,
                user_prompt=user_prompt,
            )
        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in intent detection LLM call: {e}")
            return '{"function_call": {"name": "continue_chat"}}'

        llm_time = time.time() - llm_start_time
        logger.bind(tag=TAG).debug(
            f"Intent LLM completed: model={model_info}, elapsed={llm_time:.4f}s"
        )

        postprocess_start_time = time.time()

        # Extract JSON from providers that wrap it in surrounding text.
        intent = intent.strip()
        match = re.search(r"\{.*\}", intent, re.DOTALL)
        if match:
            intent = match.group(0)

        total_time = time.time() - total_start_time
        logger.bind(tag=TAG).debug(
            f"Intent timing: model={model_info}, total={total_time:.4f}s, "
            f"llm={llm_time:.4f}s, query='{text[:20]}...'"
        )

        try:
            intent_data = json.loads(intent)
            if "function_call" in intent_data:
                function_data = intent_data["function_call"]
                function_name = function_data.get("name")
                function_args = function_data.get("arguments", {})

                logger.bind(tag=TAG).info(
                    f"LLM intent: {function_name}, arguments: {function_args}"
                )

                if function_name == "result_for_context":
                    logger.bind(tag=TAG).info(
                        "result_for_context selected; answering from context"
                    )

                elif function_name == "continue_chat":
                    # Keep only conversational history for a regular reply.
                    clean_history = [
                        msg
                        for msg in conn.dialogue.dialogue
                        if msg.role not in ["tool", "function"]
                    ]
                    conn.dialogue.dialogue = clean_history

                else:
                    logger.bind(tag=TAG).info(f"Tool intent selected: {function_name}")

            self.cache_manager.set(self.CacheType.INTENT, cache_key, intent)
            postprocess_time = time.time() - postprocess_start_time
            logger.bind(tag=TAG).debug(f"Intent postprocessing: {postprocess_time:.4f}s")
            return intent
        except json.JSONDecodeError:
            postprocess_time = time.time() - postprocess_start_time
            logger.bind(tag=TAG).error(
                f"Unable to parse intent JSON: {intent}, elapsed={postprocess_time:.4f}s"
            )
            # Fall back to regular chat when classification output is invalid.
            return '{"function_call": {"name": "continue_chat"}}'
