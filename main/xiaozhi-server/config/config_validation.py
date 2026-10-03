"""Shared validation for Settings commits, cloud snapshots and LKG."""

from collections.abc import Mapping

PROVIDER_GROUPS = ("VAD", "ASR", "LLM", "VLLM", "TTS", "Memory", "Intent")
DIAGNOSTIC_THRESHOLD_KEYS = {
    "first_audio",
    "llm_first",
    "resumed_llm_first",
    "tool",
    "total",
    "tts_first",
}


def validate_config(config):
    from core.soundbank import normalize_soundbank_text

    selected = config.get("selected_module")
    if not isinstance(selected, Mapping):
        raise ValueError("selected_module must be an object")

    for group in PROVIDER_GROUPS:
        available = config.get(group)
        if available is not None and (
            not isinstance(available, Mapping)
            or any(not isinstance(value, Mapping) for value in available.values())
        ):
            raise ValueError(f"{group} providers must be objects")
        provider = selected.get(group)
        if not provider:
            continue
        available = config.get(group, {})
        if not isinstance(available, Mapping) or provider not in available:
            raise ValueError(f"Unknown {group} provider: {provider}")

    server = config.get("server", {})
    if not isinstance(server, Mapping):
        raise ValueError("server must be an object")
    if not isinstance(config.get("log", {}), Mapping):
        raise ValueError("log must be an object")
    for key in ("port", "http_port"):
        value = int(server.get(key, 0))
        if not 1 <= value <= 65535:
            raise ValueError(f"server.{key} must be between 1 and 65535")

    settings_config = server.get("settings", {})
    if not isinstance(settings_config, Mapping):
        raise ValueError("server.settings must be an object")
    diagnostics_config = settings_config.get("diagnostics", {})
    if not isinstance(diagnostics_config, Mapping):
        raise ValueError("server.settings.diagnostics must be an object")
    diagnostic_thresholds = diagnostics_config.get("thresholds_ms", {})
    if not isinstance(diagnostic_thresholds, Mapping):
        raise ValueError(
            "server.settings.diagnostics.thresholds_ms must be an object"
        )
    unsupported_thresholds = sorted(
        set(diagnostic_thresholds) - DIAGNOSTIC_THRESHOLD_KEYS
    )
    if unsupported_thresholds:
        raise ValueError(
            "Unsupported diagnostic threshold: "
            f"{unsupported_thresholds[0]}"
        )
    for key, value in diagnostic_thresholds.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"Diagnostic threshold {key} must be an integer"
            )
        if value < 1:
            raise ValueError(
                f"Diagnostic threshold {key} must be at least 1 ms"
            )

    log_level = str(config.get("log", {}).get("log_level", "INFO")).upper()
    if log_level not in {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR"}:
        raise ValueError("log.log_level is not supported")

    if int(config.get("asr_min_audio_ms", 300)) < 0:
        raise ValueError("asr_min_audio_ms must not be negative")
    if int(config.get("asr_audio_queue_max_frames", 200)) < 1:
        raise ValueError("asr_audio_queue_max_frames must be at least 1")

    soundbank_config = config.get("static_soundbank", {})
    if not isinstance(soundbank_config, Mapping):
        raise ValueError("static_soundbank must be an object")
    soundbank_entries = soundbank_config.get("entries", {})
    if not isinstance(soundbank_entries, Mapping):
        raise ValueError("static_soundbank.entries must be an object")

    normalized_phrases = {}
    for phrase in soundbank_entries:
        normalized = normalize_soundbank_text(phrase)
        if not normalized:
            raise ValueError("Soundbank phrase must contain matchable text")
        previous = normalized_phrases.get(normalized)
        if previous is not None:
            raise ValueError(
                "Soundbank phrases normalize to the same key: "
                f"{previous!r} and {phrase!r}"
            )
        normalized_phrases[normalized] = phrase
