import base64
import binascii
import math

from google import genai
from google.genai import types

from config.logger import setup_logging
from core.providers.vllm.base import VLLMProviderBase
from core.utils.util import check_model_key


TAG = __name__
logger = setup_logging()

_THINKING_LEVELS = ("minimal", "low", "medium", "high")
_MEDIA_RESOLUTIONS = {
    "low": types.MediaResolution.MEDIA_RESOLUTION_LOW,
    "medium": types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
    "high": types.MediaResolution.MEDIA_RESOLUTION_HIGH,
}
_API_KEY_PLACEHOLDERS = {"your_api_key", "你的api_key"}


def _optional_number(config, name, minimum, maximum):
    value = config.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Gemini VLLM {name} must be a number")
    if not math.isfinite(value) or value < minimum or value > maximum:
        raise ValueError(
            f"Gemini VLLM {name} must be between {minimum} and {maximum}"
        )
    return value


def _optional_positive_integer(config, name):
    value = config.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Gemini VLLM {name} must be a positive integer")
    return value


class VLLMProvider(VLLMProviderBase):
    def __init__(self, config):
        api_key = config.get("api_key")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("Gemini VLLM requires a valid API key")
        self.api_key = api_key.strip()
        if (
            self.api_key.casefold() in _API_KEY_PLACEHOLDERS
            or check_model_key("VLLM", self.api_key)
        ):
            raise ValueError("Gemini VLLM requires a valid API key")

        model_name = config.get("model_name")
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("Gemini VLLM requires a model_name")
        self.model_name = model_name.strip()
        self.response_language = str(
            config.get("response_language", "English")
        ).strip() or "English"

        self.max_output_tokens = config.get("max_output_tokens", 256)
        if (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or self.max_output_tokens <= 0
        ):
            raise ValueError(
                "Gemini VLLM max_output_tokens must be a positive integer"
            )

        thinking_level = config.get("thinking_level")
        if thinking_level is None:
            self.thinking_level = None
        elif not isinstance(thinking_level, str):
            raise ValueError("Gemini VLLM thinking_level must be a string")
        else:
            self.thinking_level = thinking_level.strip().lower() or None
            if (
                self.thinking_level is not None
                and self.thinking_level not in _THINKING_LEVELS
            ):
                allowed = ", ".join(_THINKING_LEVELS)
                raise ValueError(
                    "Gemini VLLM thinking_level must be one of: " + allowed
                )

        media_resolution = config.get("media_resolution", "medium")
        if media_resolution is None:
            media_resolution = "medium"
        if not isinstance(media_resolution, str):
            raise ValueError("Gemini VLLM media_resolution must be a string")
        self.media_resolution = media_resolution.strip().lower() or "medium"
        if self.media_resolution not in _MEDIA_RESOLUTIONS:
            allowed = ", ".join(_MEDIA_RESOLUTIONS)
            raise ValueError(
                "Gemini VLLM media_resolution must be one of: " + allowed
            )

        self.temperature = _optional_number(config, "temperature", 0, 2)
        self.top_p = _optional_number(config, "top_p", 0, 1)
        self.top_k = _optional_positive_integer(config, "top_k")

        self.generation_kwargs = {
            "max_output_tokens": self.max_output_tokens,
            "media_resolution": _MEDIA_RESOLUTIONS[self.media_resolution],
        }
        if self.temperature is not None:
            self.generation_kwargs["temperature"] = self.temperature
        if self.top_p is not None:
            self.generation_kwargs["top_p"] = self.top_p
        if self.top_k is not None:
            self.generation_kwargs["top_k"] = self.top_k
        if self.thinking_level is not None:
            self.generation_kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_level=self.thinking_level
            )

        self.client = genai.Client(api_key=self.api_key)

    def response(self, question, base64_image):
        prompt = question + f"\nReply in {self.response_language}."
        try:
            try:
                image_bytes = base64.b64decode(base64_image, validate=True)
            except (binascii.Error, TypeError, ValueError) as error:
                raise ValueError(
                    "Gemini VLLM image must be valid base64-encoded JPEG data"
                ) from error
            if not image_bytes:
                raise ValueError("Gemini VLLM image must not be empty")

            image_part = types.Part.from_bytes(
                data=image_bytes,
                mime_type="image/jpeg",
            )
            generation_config = types.GenerateContentConfig(
                **self.generation_kwargs
            )
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=[prompt, image_part],
                config=generation_config,
            )

            text = response.text if response is not None else None
            if not isinstance(text, str) or not text.strip():
                raise RuntimeError("Gemini VLLM returned no usable text")
            return text
        except Exception as error:
            logger.bind(tag=TAG).error(f"Gemini VLLM request failed: {error}")
            raise
