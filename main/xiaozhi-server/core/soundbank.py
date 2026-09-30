"""Static soundbank schema, path safety, authoring, and preview helpers."""

import asyncio
import copy
import hashlib
import inspect
import json
import math
import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
import re
import subprocess
import tempfile
import unicodedata
import wave

from config.config_loader import get_project_dir
from core.utils import text_utils
from core.utils.config_secrets import is_credential_name, normalize_config_name


SOUNDBANK_EXTENSIONS = frozenset({".p3", ".wav", ".mp3"})
SOUNDBANK_MIME_TYPES = {
    ".mp3": "audio/mpeg",
    ".p3": "application/octet-stream",
    ".wav": "audio/wav",
}
MAX_GENERATION_TEXT_LENGTH = 512
_SAFE_SOURCE_EXTENSION = re.compile(r"^[a-z0-9]{1,8}$")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[a-zA-Z]:[\\/]")
_NON_GENERATION_SETTINGS = {"max_retries", "output_dir", "tts_timeout"}
_STRUCTURED_STRING_SETTINGS = {
    "body",
    "extra_body",
    "headers",
    "options",
    "parameters",
    "params",
    "payload",
    "query",
    "query_params",
    "request",
    "request_body",
    "request_headers",
}
_TRANSPORT_SETTINGS = {
    "api_endpoint",
    "api_url",
    "base_endpoint",
    "base_url",
    "endpoint",
    "endpoint_url",
    "host",
    "hostname",
    "port",
    "proxy",
    "proxy_url",
    "server_url",
    "service_url",
    "url",
    "uri",
    "websocket_url",
    "ws_url",
    "wss_url",
}
_COMPACT_TRANSPORT_SETTINGS = {
    "apiurl",
    "baseurl",
    "endpointurl",
    "proxyurl",
    "serverurl",
    "serviceurl",
    "websocketurl",
    "wsurl",
    "wssurl",
}
_DROP_SETTING = object()


class SoundbankError(Exception):
    """A user-facing soundbank failure with an HTTP-compatible status."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def normalize_soundbank_text(text):
    """Normalize stable segment-edge and whitespace differences."""
    if not isinstance(text, str):
        return ""
    normalized = unicodedata.normalize("NFC", text)
    normalized = text_utils.strip_edge_separators(normalized)
    normalized = " ".join(normalized.split()).strip()
    return normalized.casefold()


def soundbank_entry_filename(entry):
    """Return the asset filename from a legacy string or metadata entry."""
    if isinstance(entry, str) and entry.strip():
        return entry.strip()
    if isinstance(entry, Mapping):
        filename = entry.get("file")
        if isinstance(filename, str) and filename.strip():
            return filename.strip()
        raise SoundbankError("soundbank entry object must contain a non-empty file")
    raise SoundbankError("soundbank entry must be a filename string or object")


def resolve_soundbank_root(config, create=False):
    """Resolve the configured soundbank directory against the server root."""
    if not isinstance(config, Mapping):
        raise SoundbankError("static_soundbank must be an object")
    directory = config.get("directory", "data/soundbank")
    if not isinstance(directory, str) or not directory.strip():
        raise SoundbankError("static soundbank directory must be a non-empty string")

    try:
        root = Path(directory.strip()).expanduser()
        if not root.is_absolute():
            root = Path(get_project_dir()) / root
        root = root.resolve()
        if create:
            root.mkdir(parents=True, exist_ok=True)
            root = root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise SoundbankError(f"static soundbank directory is invalid: {error}") from error
    if root.exists() and not root.is_dir():
        raise SoundbankError("static soundbank directory is not a directory")
    return root


def resolve_soundbank_asset(root, filename, require_file=False):
    """Resolve one supported relative asset without permitting root escape."""
    if not isinstance(filename, str) or not filename.strip():
        raise SoundbankError("soundbank asset path must be a non-empty string")
    asset_name = filename.strip()
    if (
        asset_name.startswith(("/", "\\"))
        or _WINDOWS_ABSOLUTE_PATH.match(asset_name)
    ):
        raise SoundbankError("soundbank asset path must be relative")

    parts = [part for part in re.split(r"[\\/]+", asset_name) if part not in {"", "."}]
    if not parts or ".." in parts:
        raise SoundbankError("soundbank asset path cannot contain '..'")
    relative_path = Path(*parts)
    if relative_path.suffix.lower() not in SOUNDBANK_EXTENSIONS:
        raise SoundbankError("soundbank asset extension is not supported")

    soundbank_root = Path(root).resolve()
    try:
        asset_path = (soundbank_root / relative_path).resolve(strict=require_file)
    except FileNotFoundError as error:
        raise SoundbankError("soundbank asset was not found", status=404) from error
    except (OSError, RuntimeError, ValueError) as error:
        raise SoundbankError(f"soundbank asset path is invalid: {error}") from error
    if not asset_path.is_relative_to(soundbank_root):
        raise SoundbankError("soundbank asset resolves outside its directory")
    if require_file:
        try:
            if not asset_path.is_file() or asset_path.stat().st_size <= 0:
                raise SoundbankError(
                    "soundbank asset is missing or empty", status=404
                )
        except OSError as error:
            raise SoundbankError(
                f"soundbank asset is unavailable: {error}", status=404
            ) from error
    return asset_path


def validate_soundbank_file(root, asset_path):
    """Revalidate a previously resolved runtime asset before playback."""
    soundbank_root = Path(root).resolve()
    try:
        resolved_path = Path(asset_path).resolve(strict=True)
    except FileNotFoundError as error:
        raise SoundbankError("soundbank asset was not found", status=404) from error
    except (OSError, RuntimeError, ValueError) as error:
        raise SoundbankError(f"soundbank asset path is invalid: {error}") from error
    if not resolved_path.is_relative_to(soundbank_root):
        raise SoundbankError("soundbank asset resolves outside its directory")
    if resolved_path.suffix.lower() not in SOUNDBANK_EXTENSIONS:
        raise SoundbankError("soundbank asset extension is not supported")
    try:
        if not resolved_path.is_file() or resolved_path.stat().st_size <= 0:
            raise SoundbankError("soundbank asset is missing or empty", status=404)
    except OSError as error:
        raise SoundbankError(
            f"soundbank asset is unavailable: {error}", status=404
        ) from error
    return resolved_path


def _normalized_setting_name(key):
    return normalize_config_name(key)


def _is_transport_setting(key):
    normalized = _normalized_setting_name(key)
    compact = normalized.replace("_", "")
    return (
        normalized in _TRANSPORT_SETTINGS
        or compact in _COMPACT_TRANSPORT_SETTINGS
        or normalized.endswith(
            ("_endpoint", "_host", "_hostname", "_port", "_uri", "_url")
        )
        or normalized.startswith(
            (
                "api_url_",
                "base_url_",
                "endpoint_",
                "proxy_",
                "server_url_",
                "service_url_",
                "uri_",
                "url_",
                "websocket_url_",
                "ws_url_",
                "wss_url_",
            )
        )
    )


def _sanitize_value(value, field_name=None):
    if isinstance(value, Mapping):
        sanitized = {}
        for key, child in value.items():
            if is_credential_name(key) or _is_transport_setting(key):
                continue
            sanitized_child = _sanitize_value(child, key)
            if sanitized_child is not _DROP_SETTING:
                sanitized[str(key)] = sanitized_child
        return sanitized
    if isinstance(value, (list, tuple)):
        sanitized = []
        for child in value:
            sanitized_child = _sanitize_value(child)
            if sanitized_child is not _DROP_SETTING:
                sanitized.append(sanitized_child)
        return sanitized
    if isinstance(value, str):
        normalized_name = _normalized_setting_name(field_name)
        looks_structured = value.lstrip().startswith(("{", "["))
        if normalized_name in _STRUCTURED_STRING_SETTINGS or looks_structured:
            try:
                structured_value = json.loads(value)
            except (TypeError, ValueError):
                return _DROP_SETTING
            if not isinstance(structured_value, (Mapping, list)):
                return _DROP_SETTING
            return _sanitize_value(structured_value)
        return value
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _DROP_SETTING
    return _DROP_SETTING


def sanitize_generation_settings(config):
    """Return a JSON-safe provider snapshot without secrets or runtime controls."""
    if not isinstance(config, Mapping):
        raise SoundbankError("TTS provider configuration must be an object")
    sanitized = {}
    for key, value in config.items():
        if (
            _normalized_setting_name(key) in _NON_GENERATION_SETTINGS
            or is_credential_name(key)
            or _is_transport_setting(key)
        ):
            continue
        sanitized_value = _sanitize_value(value, key)
        if sanitized_value is not _DROP_SETTING:
            sanitized[str(key)] = sanitized_value
    return sanitized


def _merge_safe_settings(current, overlay):
    if isinstance(current, Mapping) and isinstance(overlay, Mapping):
        merged = copy.deepcopy(dict(current))
        for key, value in overlay.items():
            if is_credential_name(key) or _is_transport_setting(key):
                continue
            if key in merged:
                merged[key] = _merge_safe_settings(merged[key], value)
            else:
                merged[key] = copy.deepcopy(value)
        return merged
    if isinstance(current, list) and isinstance(overlay, list):
        merged = copy.deepcopy(current)
        for index, value in enumerate(overlay):
            if index < len(merged):
                merged[index] = _merge_safe_settings(merged[index], value)
            else:
                merged.append(copy.deepcopy(value))
        return merged
    return copy.deepcopy(overlay)


def _overlay_generation_settings(current_config, stored_settings):
    """Overlay safe provenance while retaining current secrets and transports."""
    safe_settings = sanitize_generation_settings(stored_settings)
    merged = copy.deepcopy(dict(current_config))
    for key, value in safe_settings.items():
        current_value = merged.get(key, _DROP_SETTING)
        if isinstance(current_value, str) and isinstance(value, (Mapping, list)):
            try:
                parsed_current = json.loads(current_value)
            except (TypeError, ValueError):
                continue
            if not isinstance(parsed_current, type(value)):
                continue
            merged_value = _merge_safe_settings(parsed_current, value)
            merged[key] = json.dumps(
                merged_value,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        elif current_value is _DROP_SETTING:
            merged[key] = copy.deepcopy(value)
        else:
            merged[key] = _merge_safe_settings(current_value, value)
    return merged


def generation_fingerprint(provider, settings):
    """Create a stable, non-secret fingerprint for one generation identity."""
    payload = json.dumps(
        {"provider": provider, "settings": settings},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:10]


class SoundbankAuthoringService:
    """Generate canonical soundbank WAV assets with isolated TTS providers."""

    def __init__(self, config):
        # This is the running server's startup configuration. Settings writes
        # remain restart-required and are deliberately not hot-reloaded here.
        self.config = config

    def generate(self, text, mode="current", generated_by=None):
        text = self._validate_text(text)
        provider_name, provider_config = self._generation_config(
            mode, generated_by
        )
        settings = sanitize_generation_settings(provider_config)
        fingerprint = generation_fingerprint(provider_name, settings)
        sample_rate = self._sample_rate()
        soundbank_config = self.config.get("static_soundbank", {})
        soundbank_root = resolve_soundbank_root(soundbank_config, create=True)
        filename = self._generated_filename(
            text, provider_name, fingerprint
        )
        final_path = resolve_soundbank_asset(soundbank_root, filename)

        isolated_config = copy.deepcopy(self.config)
        selected_modules = isolated_config.setdefault("selected_module", {})
        selected_modules["TTS"] = provider_name
        isolated_config.setdefault("TTS", {})[provider_name] = provider_config

        try:
            self._generate_wav(
                isolated_config,
                text,
                sample_rate,
                soundbank_root,
                final_path,
            )
        except SoundbankError:
            raise
        except Exception as error:
            raise SoundbankError(
                f"TTS generation failed: {error}", status=502
            ) from error

        provenance = {
            "provider": provider_name,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "config_fingerprint": fingerprint,
            "settings": settings,
            "sample_rate": sample_rate,
            "format": "wav",
        }
        model = self._first_setting(provider_config, "model", "model_name")
        voice = self._first_setting(provider_config, "private_voice", "voice")
        if model is not None:
            provenance["model"] = model
        if voice is not None:
            provenance["voice"] = voice
        return {"file": filename, "generated_by": provenance}

    def preview_path(self, filename):
        soundbank_root = resolve_soundbank_root(
            self.config.get("static_soundbank", {})
        )
        path = resolve_soundbank_asset(
            soundbank_root, filename, require_file=True
        )
        return path, SOUNDBANK_MIME_TYPES[path.suffix.lower()]

    @staticmethod
    def _validate_text(text):
        if not isinstance(text, str) or not text.strip():
            raise SoundbankError("text must be a non-empty string")
        normalized = text.strip()
        if len(normalized) > MAX_GENERATION_TEXT_LENGTH:
            raise SoundbankError(
                f"text must not exceed {MAX_GENERATION_TEXT_LENGTH} characters"
            )
        if not normalize_soundbank_text(normalized):
            raise SoundbankError("text must contain a matchable phrase")
        return normalized

    def _generation_config(self, mode, generated_by):
        if mode not in {"current", "same"}:
            raise SoundbankError("mode must be 'current' or 'same'")
        selected_modules = self.config.get("selected_module", {})
        providers = self.config.get("TTS", {})
        if not isinstance(selected_modules, Mapping) or not isinstance(
            providers, Mapping
        ):
            raise SoundbankError("TTS configuration is unavailable", status=422)

        if mode == "current":
            provider_name = selected_modules.get("TTS")
            stored_settings = None
        else:
            if not isinstance(generated_by, Mapping):
                raise SoundbankError(
                    "generated_by is required for mode 'same'"
                )
            provider_name = generated_by.get("provider")
            stored_settings = generated_by.get("settings")
            if not isinstance(stored_settings, Mapping):
                raise SoundbankError(
                    "generated_by.settings must be an object"
                )

        if not isinstance(provider_name, str) or not provider_name.strip():
            raise SoundbankError("TTS provider is not configured", status=422)
        provider_name = provider_name.strip()
        current_provider_config = providers.get(provider_name)
        if not isinstance(current_provider_config, Mapping):
            raise SoundbankError(
                f"TTS provider '{provider_name}' is unavailable", status=422
            )

        provider_config = copy.deepcopy(dict(current_provider_config))
        if stored_settings is not None:
            provider_config = _overlay_generation_settings(
                provider_config, stored_settings
            )
        return provider_name, provider_config

    def _sample_rate(self):
        audio_params = self.config.get("xiaozhi", {}).get("audio_params", {})
        sample_rate = audio_params.get("sample_rate")
        if isinstance(sample_rate, bool):
            sample_rate = None
        try:
            sample_rate = int(sample_rate)
        except (TypeError, ValueError):
            sample_rate = None
        if sample_rate is None or sample_rate <= 0:
            raise SoundbankError(
                "xiaozhi.audio_params.sample_rate is invalid", status=500
            )
        return sample_rate

    @staticmethod
    def _generated_filename(text, provider, fingerprint):
        normalized_text = normalize_soundbank_text(text)
        slug_source = unicodedata.normalize("NFKD", normalized_text)
        slug_source = slug_source.encode("ascii", "ignore").decode("ascii")
        slug = re.sub(r"[^a-z0-9]+", "-", slug_source.lower()).strip("-")
        slug = (slug[:48].rstrip("-") or "speech")
        identity = json.dumps(
            {
                "text": normalized_text,
                "provider": provider,
                "config_fingerprint": fingerprint,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        short_hash = hashlib.sha256(identity).hexdigest()[:8]
        return f"{slug}-{short_hash}.wav"

    @staticmethod
    def _first_setting(config, *keys):
        for key in keys:
            value = config.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                return value
        return None

    def _generate_wav(
        self,
        isolated_config,
        text,
        sample_rate,
        soundbank_root,
        final_path,
    ):
        from core.utils.modules_initialize import initialize_tts

        provider = None
        with tempfile.TemporaryDirectory(
            prefix=".generate-", dir=soundbank_root
        ) as temporary_directory:
            temp_root = Path(temporary_directory)
            try:
                provider = initialize_tts(isolated_config)
            except Exception as error:
                raise SoundbankError(
                    f"TTS provider could not be initialized: {error}",
                    status=422,
                ) from error

            extension = str(
                getattr(provider, "audio_file_type", "bin")
            ).lower().lstrip(".")
            if not _SAFE_SOURCE_EXTENSION.fullmatch(extension):
                extension = "bin"
            source_path = temp_root / f"source.{extension}"
            normalized_path = temp_root / "normalized.wav"

            try:
                result = asyncio.run(
                    self._run_provider(provider, text, source_path)
                )
                if isinstance(result, (bytes, bytearray, memoryview)) and (
                    not source_path.exists() or source_path.stat().st_size == 0
                ):
                    source_path.write_bytes(bytes(result))
                if not source_path.is_file() or source_path.stat().st_size == 0:
                    raise SoundbankError(
                        "TTS provider returned no audio", status=502
                    )
                self._normalize_wav(source_path, normalized_path, sample_rate)
                self._validate_wav(normalized_path, sample_rate)
                os.replace(normalized_path, final_path)
            except SoundbankError:
                raise
            except Exception as error:
                raise SoundbankError(
                    f"TTS generation failed: {error}", status=502
                ) from error

    @staticmethod
    async def _run_provider(provider, text, source_path):
        try:
            return await provider.text_to_speak(text, os.fspath(source_path))
        finally:
            close = getattr(provider, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result

    @staticmethod
    def _normalize_wav(source_path, output_path, sample_rate):
        try:
            process = subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    os.fspath(source_path),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    str(sample_rate),
                    "-c:a",
                    "pcm_s16le",
                    "-f",
                    "wav",
                    os.fspath(output_path),
                ],
                capture_output=True,
                check=False,
                timeout=60,
            )
        except FileNotFoundError as error:
            raise SoundbankError(
                "ffmpeg is not installed or executable", status=500
            ) from error
        except subprocess.TimeoutExpired as error:
            raise SoundbankError(
                "ffmpeg timed out while normalizing audio", status=502
            ) from error
        if process.returncode != 0:
            detail = process.stderr.decode("utf-8", errors="replace").strip()
            raise SoundbankError(
                f"ffmpeg could not normalize generated audio: {detail[:300]}",
                status=502,
            )

    @staticmethod
    def _validate_wav(path, sample_rate):
        try:
            with wave.open(os.fspath(path), "rb") as wav_file:
                channels = wav_file.getnchannels()
                sample_width = wav_file.getsampwidth()
                actual_rate = wav_file.getframerate()
                compression = wav_file.getcomptype()
                frame_count = wav_file.getnframes()
                audio_data = wav_file.readframes(frame_count)
        except (OSError, EOFError, wave.Error) as error:
            raise SoundbankError(
                f"generated WAV is invalid: {error}", status=502
            ) from error
        if (
            channels != 1
            or sample_width != 2
            or actual_rate != sample_rate
            or compression != "NONE"
        ):
            raise SoundbankError(
                "generated WAV does not match mono PCM16 output settings",
                status=502,
            )
        if frame_count <= 0 or len(audio_data) < frame_count * sample_width:
            raise SoundbankError("generated WAV contains no audio", status=502)
