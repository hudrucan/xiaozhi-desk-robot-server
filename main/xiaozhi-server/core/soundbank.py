"""Static soundbank schema, path safety, authoring, and preview helpers."""

import asyncio
import copy
import hashlib
import io
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

from core.utils.soundbank_text import normalize_soundbank_text
from config.config_loader import get_project_dir
from core.utils import p3, text_utils
from core.utils.config_secrets import is_credential_name, normalize_config_name


SOUNDBANK_EXTENSIONS = frozenset({".p3", ".wav", ".mp3"})
SOUNDBANK_MIME_TYPES = {
    ".mp3": "audio/mpeg",
    ".p3": "application/octet-stream",
    ".wav": "audio/wav",
}
SOUNDBANK_OPTIMIZED_CODEC = "opus"
SOUNDBANK_OPTIMIZED_CHANNELS = 1
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


def soundbank_entry_optimized(entry):
    """Return optional optimized metadata without changing legacy entry rules."""
    if not isinstance(entry, Mapping):
        return None
    optimized = entry.get("optimized")
    return optimized if isinstance(optimized, Mapping) else None


def soundbank_entry_text(entry):
    """Return an explicit spoken transcript from a metadata entry, if valid."""
    if not isinstance(entry, Mapping):
        return None
    text = entry.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return text.strip()


def validate_soundbank_cloud_pointer(pointer):
    """Validate additive metadata without changing pointerless Local entries."""
    if (not isinstance(pointer, Mapping) or set(pointer) != {"file_id", "sha256", "size"}
            or not isinstance(pointer["file_id"], str)
            or not re.fullmatch(r"[a-zA-Z0-9_-]+", pointer["file_id"])
            or not isinstance(pointer["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", pointer["sha256"])
            or type(pointer["size"]) is not int or pointer["size"] <= 0):
        raise ValueError("Invalid soundbank cloud pointer")


def soundbank_p3_sample_rate(config, optimized=None):
    """Reuse the mono/60ms Opus contract; incompatible negotiated rates fall back in TTS."""
    if optimized is None:
        sample_rate = SoundbankAuthoringService(config)._sample_rate()
    else:
        sample_rate = optimized.get("sample_rate")
        if (optimized.get("codec") != SOUNDBANK_OPTIMIZED_CODEC
                or type(optimized.get("channels")) is not int
                or optimized["channels"] != SOUNDBANK_OPTIMIZED_CHANNELS
                or type(optimized.get("frame_duration_ms")) is not int
                or optimized["frame_duration_ms"] != p3.P3_FRAME_DURATION_MS):
            raise ValueError("Invalid cloud soundbank P3 audio contract")
    if type(sample_rate) is not int or sample_rate not in {8000, 12000, 16000, 24000, 48000}:
        raise ValueError("Invalid cloud soundbank P3 sample rate")
    return sample_rate


def validate_soundbank_cloud_metadata(config):
    """Only pointer-bearing entries gain stricter checks; legacy semantics stay intact."""
    soundbank = config.get("static_soundbank", {})
    for entry in soundbank.get("entries", {}).values():
        if not isinstance(entry, Mapping):
            continue
        optimized = soundbank_entry_optimized(entry)
        for metadata in (entry, optimized):
            if metadata is None or "cloud" not in metadata:
                continue
            validate_soundbank_cloud_pointer(metadata["cloud"])
            try:
                root = resolve_soundbank_root(soundbank)
                path = resolve_soundbank_asset(root, soundbank_entry_filename(metadata))
                if metadata is optimized and path.suffix.lower() != ".p3":
                    raise ValueError("Cloud optimized soundbank asset must use P3")
                if path.suffix.lower() == ".p3":
                    soundbank_p3_sample_rate(config, metadata if metadata is optimized else None)
            except SoundbankError:
                raise ValueError("Invalid cloud soundbank asset path or audio contract") from None


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
    """Return a safe provider snapshot used only for artifact identity."""
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

    def generate(self, text, title=None):
        text = self._validate_text(text)
        if title is None or (isinstance(title, str) and not title.strip()):
            title = text
        elif not isinstance(title, str):
            raise SoundbankError("title must be a string")
        title = self._validate_title(title)
        provider_name, provider_config = self._generation_config()
        settings = sanitize_generation_settings(provider_config)
        fingerprint = generation_fingerprint(provider_name, settings)
        sample_rate = self._sample_rate()
        audio_contract = self._audio_contract(sample_rate)
        soundbank_config = self.config.get("static_soundbank", {})
        soundbank_root = resolve_soundbank_root(soundbank_config, create=True)
        filename = self._generated_filename(
            title, text, provider_name, fingerprint, audio_contract
        )
        final_path = resolve_soundbank_asset(soundbank_root, filename)
        optimized_filename = Path(filename).with_suffix(".p3").as_posix()
        optimized_path = resolve_soundbank_asset(
            soundbank_root, optimized_filename
        )

        isolated_config = copy.deepcopy(self.config)
        selected_modules = isolated_config.setdefault("selected_module", {})
        selected_modules["TTS"] = provider_name
        isolated_config.setdefault("TTS", {})[provider_name] = provider_config

        try:
            self._generate_artifacts(
                isolated_config,
                text,
                sample_rate,
                soundbank_root,
                final_path,
                optimized_path,
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
        }
        model = self._first_setting(
            provider_config, "model", "model_name", "model_repo"
        )
        voice = self._first_setting(provider_config, "private_voice", "voice")
        if model is not None:
            provenance["model"] = model
        if voice is not None:
            provenance["voice"] = voice
        return {
            "file": filename,
            "text": text,
            "optimized": {
                "file": optimized_filename,
                **audio_contract,
            },
            "generated_by": provenance,
        }

    def optimize(self, filename):
        """Compile an existing canonical WAV/MP3 asset into validated p3."""
        soundbank_root = resolve_soundbank_root(
            self.config.get("static_soundbank", {}), create=True
        )
        canonical_path = resolve_soundbank_asset(
            soundbank_root, filename, require_file=True
        )
        if canonical_path.suffix.lower() == ".p3":
            raise SoundbankError(
                "canonical p3 assets are already direct-playable", status=422
            )

        sample_rate = self._sample_rate()
        audio_contract = self._audio_contract(sample_rate)
        optimized_filename = self._optimized_filename(
            filename,
            canonical_path,
            audio_contract,
        )
        optimized_path = resolve_soundbank_asset(
            soundbank_root, optimized_filename
        )

        try:
            with tempfile.TemporaryDirectory(
                prefix=".optimize-", dir=soundbank_root
            ) as temporary_directory:
                temp_root = Path(temporary_directory)
                normalized_path = temp_root / "normalized.wav"
                temporary_p3_path = temp_root / "optimized.p3"
                self._normalize_wav(
                    canonical_path, normalized_path, sample_rate
                )
                self._compile_p3(
                    normalized_path,
                    temporary_p3_path,
                    sample_rate,
                )
                os.replace(temporary_p3_path, optimized_path)
        except SoundbankError:
            raise
        except Exception as error:
            raise SoundbankError(
                f"soundbank optimization failed: {error}", status=502
            ) from error

        return {"file": optimized_filename, **audio_contract}

    def preview_path(self, filename):
        soundbank_root = resolve_soundbank_root(
            self.config.get("static_soundbank", {})
        )
        path = resolve_soundbank_asset(
            soundbank_root, filename, require_file=True
        )
        return path, SOUNDBANK_MIME_TYPES[path.suffix.lower()]

    def preview_audio(self, filename):
        """Return a browser-playable asset, transcoding packetized Opus to WAV."""
        path, content_type = self.preview_path(filename)
        if path.suffix.lower() != ".p3":
            return path, content_type
        return self._p3_preview_wav(path), "audio/wav"

    def runtime_directory(self):
        """Return the soundbank directory loaded by the running server."""
        soundbank_config = self.config.get("static_soundbank", {})
        if not isinstance(soundbank_config, Mapping):
            return "data/soundbank"
        directory = soundbank_config.get("directory", "data/soundbank")
        return directory if isinstance(directory, str) else "data/soundbank"

    def runtime_audio_contract(self):
        """Return the fixed optimized-audio contract of the running server."""
        return self._audio_contract(self._sample_rate())

    def _p3_preview_wav(self, path):
        sample_rate = self._sample_rate()
        try:
            import opuslib_next

            packets = p3.load_validated_opus_file(
                path,
                sample_rate=sample_rate,
            )
            frame_samples = sample_rate * p3.P3_FRAME_DURATION_MS // 1000
            decoder = opuslib_next.Decoder(sample_rate, 1)
            expected_frame_bytes = frame_samples * 2
            pcm_frames = []
            for index, packet in enumerate(packets):
                pcm_frame = decoder.decode(packet, frame_samples)
                if len(pcm_frame) != expected_frame_bytes:
                    raise SoundbankError(
                        "p3 preview decoded an unexpected frame size "
                        f"at packet {index}",
                        status=422,
                    )
                pcm_frames.append(pcm_frame)
            output = io.BytesIO()
            with wave.open(output, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)
                wav_file.writeframes(b"".join(pcm_frames))
            return output.getvalue()
        except SoundbankError:
            raise
        except Exception as error:
            raise SoundbankError(
                f"p3 preview could not be decoded: {error}", status=422
            ) from error

    @staticmethod
    def _validate_text(text):
        if not isinstance(text, str) or not text.strip():
            raise SoundbankError("text must be a non-empty string")
        normalized = text.strip()
        if len(normalized) > MAX_GENERATION_TEXT_LENGTH:
            raise SoundbankError(
                f"text must not exceed {MAX_GENERATION_TEXT_LENGTH} characters"
            )
        return normalized

    @staticmethod
    def _validate_title(title):
        if not isinstance(title, str) or not title.strip():
            raise SoundbankError("title must be a non-empty string")
        normalized = title.strip()
        if len(normalized) > MAX_GENERATION_TEXT_LENGTH:
            raise SoundbankError(
                f"title must not exceed {MAX_GENERATION_TEXT_LENGTH} characters"
            )
        if not normalize_soundbank_text(normalized):
            raise SoundbankError("title must contain a matchable phrase")
        return normalized

    def _generation_config(self):
        selected_modules = self.config.get("selected_module", {})
        providers = self.config.get("TTS", {})
        if not isinstance(selected_modules, Mapping) or not isinstance(
            providers, Mapping
        ):
            raise SoundbankError("TTS configuration is unavailable", status=422)

        provider_name = selected_modules.get("TTS")
        if not isinstance(provider_name, str) or not provider_name.strip():
            raise SoundbankError("TTS provider is not configured", status=422)
        provider_name = provider_name.strip()
        current_provider_config = providers.get(provider_name)
        if not isinstance(current_provider_config, Mapping):
            raise SoundbankError(
                f"TTS provider '{provider_name}' is unavailable", status=422
            )

        return provider_name, copy.deepcopy(dict(current_provider_config))

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
    def _audio_contract(sample_rate):
        return {
            "codec": SOUNDBANK_OPTIMIZED_CODEC,
            "sample_rate": sample_rate,
            "channels": SOUNDBANK_OPTIMIZED_CHANNELS,
            "frame_duration_ms": p3.P3_FRAME_DURATION_MS,
        }

    @staticmethod
    def _generated_filename(
        title, text, provider, fingerprint, audio_contract
    ):
        normalized_title = normalize_soundbank_text(title)
        slug_source = unicodedata.normalize("NFKD", normalized_title)
        slug_source = slug_source.encode("ascii", "ignore").decode("ascii")
        slug = re.sub(r"[^a-z0-9]+", "-", slug_source.lower()).strip("-")
        slug = (slug[:48].rstrip("-") or "speech")
        identity = json.dumps(
            {
                "title": normalized_title,
                "text": text,
                "provider": provider,
                "config_fingerprint": fingerprint,
                "audio_contract": audio_contract,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        short_hash = hashlib.sha256(identity).hexdigest()[:8]
        return f"{slug}-{short_hash}.wav"

    @staticmethod
    def _optimized_filename(filename, canonical_path, audio_contract):
        identity = hashlib.sha256()
        identity.update(
            json.dumps(
                {
                    "file": filename,
                    "audio_contract": audio_contract,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        with open(canonical_path, "rb") as canonical_file:
            while True:
                chunk = canonical_file.read(1024 * 1024)
                if not chunk:
                    break
                identity.update(chunk)
        short_hash = identity.hexdigest()[:8]
        relative_path = Path(*re.split(r"[\\/]+", filename.strip()))
        return relative_path.with_name(
            f"{relative_path.stem}-{short_hash}.p3"
        ).as_posix()

    @staticmethod
    def _first_setting(config, *keys):
        for key in keys:
            value = config.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                return value
        return None

    def _generate_artifacts(
        self,
        isolated_config,
        text,
        sample_rate,
        soundbank_root,
        final_path,
        optimized_path,
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
            temporary_p3_path = temp_root / "optimized.p3"

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
                self._compile_p3(
                    normalized_path,
                    temporary_p3_path,
                    sample_rate,
                )
                self._publish_artifact_pair(
                    normalized_path,
                    temporary_p3_path,
                    final_path,
                    optimized_path,
                )
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
    def _compile_p3(normalized_path, output_path, sample_rate):
        # Config validation/Settings reads need no encoder or NumPy runtime.
        from core.utils import opus_encoder_utils

        frame_count, pcm_data = SoundbankAuthoringService._validate_wav(
            normalized_path, sample_rate
        )
        samples_per_frame = (
            sample_rate * p3.P3_FRAME_DURATION_MS // 1000
        )
        expected_packet_count = (
            frame_count + samples_per_frame - 1
        ) // samples_per_frame
        try:
            packets = opus_encoder_utils.encode_pcm16_to_opus_packets(
                pcm_data,
                sample_rate=sample_rate,
                channels=SOUNDBANK_OPTIMIZED_CHANNELS,
                frame_size_ms=p3.P3_FRAME_DURATION_MS,
            )
            if len(packets) != expected_packet_count:
                raise ValueError(
                    "optimized p3 packet count mismatch: "
                    f"expected {expected_packet_count}, got {len(packets)}"
                )
            p3.validate_opus_packets(
                packets,
                sample_rate=sample_rate,
                frame_duration_ms=p3.P3_FRAME_DURATION_MS,
            )
            p3.write_opus_file(output_path, packets)
            loaded_packets = p3.load_validated_opus_file(
                output_path,
                sample_rate=sample_rate,
                frame_duration_ms=p3.P3_FRAME_DURATION_MS,
            )
        except Exception as error:
            raise SoundbankError(
                f"optimized p3 compilation failed: {error}", status=502
            ) from error
        if len(loaded_packets) != expected_packet_count:
            raise SoundbankError(
                "optimized p3 validation found an incomplete packet sequence",
                status=502,
            )
        if loaded_packets != packets:
            raise SoundbankError(
                "optimized p3 did not round-trip through its reader",
                status=502,
            )

    @staticmethod
    def _publish_artifact_pair(
        temporary_wav,
        temporary_p3,
        final_wav,
        final_p3,
    ):
        """Publish a validated pair while retaining prior files for rollback."""
        backups = {}
        published = []
        try:
            for index, final_path in enumerate((final_wav, final_p3)):
                if final_path.exists():
                    backup_path = temporary_wav.parent / (
                        f"previous-{index}{final_path.suffix}"
                    )
                    os.replace(final_path, backup_path)
                    backups[final_path] = backup_path

            for temporary_path, final_path in (
                (temporary_wav, final_wav),
                (temporary_p3, final_p3),
            ):
                os.replace(temporary_path, final_path)
                published.append(final_path)
        except Exception:
            for final_path in published:
                try:
                    final_path.unlink(missing_ok=True)
                except OSError:
                    pass
            for final_path, backup_path in backups.items():
                if backup_path.exists():
                    os.replace(backup_path, final_path)
            raise

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
        return frame_count, audio_data
