from typing import Dict, Any
from config.logger import setup_logging
from core.utils import tts, llm, intent, memory, vad, asr

TAG = __name__
logger = setup_logging()


def initialize_modules(
    logger,
    config: Dict[str, Any],
    init_vad=False,
    init_asr=False,
    init_llm=False,
    init_tts=False,
    init_memory=False,
    init_intent=False,
) -> Dict[str, Any]:
    """Initialize the requested provider modules.

    Args:
        config: Server configuration dictionary.

    Returns:
        A dictionary containing the initialized modules.
    """
    modules = {}

    # Initialize TTS.
    if init_tts:
        select_tts_module = config["selected_module"]["TTS"]
        modules["tts"] = initialize_tts(config)
        logger.bind(tag=TAG).info(
            f"Component initialized: TTS {select_tts_module}"
        )

    # Initialize LLM.
    if init_llm:
        select_llm_module = config["selected_module"]["LLM"]
        llm_type = (
            select_llm_module
            if "type" not in config["LLM"][select_llm_module]
            else config["LLM"][select_llm_module]["type"]
        )
        modules["llm"] = llm.create_instance(
            llm_type,
            config["LLM"][select_llm_module],
        )
        logger.bind(tag=TAG).info(
            f"Component initialized: LLM {select_llm_module}"
        )

    # Initialize intent detection.
    if init_intent:
        select_intent_module = config["selected_module"]["Intent"]
        intent_type = (
            select_intent_module
            if "type" not in config["Intent"][select_intent_module]
            else config["Intent"][select_intent_module]["type"]
        )
        modules["intent"] = intent.create_instance(
            intent_type,
            config["Intent"][select_intent_module],
        )
        logger.bind(tag=TAG).info(
            f"Component initialized: intent {select_intent_module}"
        )

    # Initialize memory.
    if init_memory:
        select_memory_module = config["selected_module"]["Memory"]
        memory_type = (
            select_memory_module
            if "type" not in config["Memory"][select_memory_module]
            else config["Memory"][select_memory_module]["type"]
        )
        modules["memory"] = memory.create_instance(
            memory_type,
            config["Memory"][select_memory_module],
            config.get("summaryMemory", None),
        )
        logger.bind(tag=TAG).info(
            f"Component initialized: memory {select_memory_module}"
        )

    # Initialize VAD.
    if init_vad:
        select_vad_module = config["selected_module"]["VAD"]
        vad_type = (
            select_vad_module
            if "type" not in config["VAD"][select_vad_module]
            else config["VAD"][select_vad_module]["type"]
        )
        modules["vad"] = vad.create_instance(
            vad_type,
            config["VAD"][select_vad_module],
        )
        logger.bind(tag=TAG).info(
            f"Component initialized: VAD {select_vad_module}"
        )

    # Initialize ASR.
    if init_asr:
        select_asr_module = config["selected_module"]["ASR"]
        modules["asr"] = initialize_asr(config)
        logger.bind(tag=TAG).info(
            f"Component initialized: ASR {select_asr_module}"
        )
    return modules


def initialize_tts(config):
    select_tts_module = config["selected_module"]["TTS"]
    tts_config = config["TTS"][select_tts_module].copy()
    tts_config.setdefault("tts_timeout", config.get("tts_timeout", 15))
    tts_type = (
        select_tts_module
        if "type" not in tts_config
        else tts_config["type"]
    )
    new_tts = tts.create_instance(
        tts_type,
        tts_config,
        str(config.get("delete_audio", True)).lower() in ("true", "1", "yes"),
    )
    return new_tts


def initialize_asr(config):
    select_asr_module = config["selected_module"]["ASR"]
    asr_type = (
        select_asr_module
        if "type" not in config["ASR"][select_asr_module]
        else config["ASR"][select_asr_module]["type"]
    )
    new_asr = asr.create_instance(
        asr_type,
        config["ASR"][select_asr_module],
        str(config.get("delete_audio", True)).lower() in ("true", "1", "yes"),
    )
    logger.bind(tag=TAG).info("ASR module initialization completed")
    return new_asr


def initialize_voiceprint(asr_instance, config):
    """Initialize optional voiceprint recognition."""
    voiceprint_config = config.get("voiceprint")
    if not voiceprint_config:
        return False

    # Voiceprint requires both an endpoint and registered speakers.
    if not voiceprint_config.get("url") or not voiceprint_config.get("speakers"):
        logger.bind(tag=TAG).warning("Voiceprint configuration is incomplete")
        return False

    try:
        asr_instance.init_voiceprint(voiceprint_config)
        logger.bind(tag=TAG).info("Voiceprint recognition enabled for the ASR module")
        logger.bind(tag=TAG).info(
            f"Configured speakers: {len(voiceprint_config['speakers'])}"
        )
        return True
    except Exception as e:
        logger.bind(tag=TAG).error(
            f"Failed to initialize voiceprint recognition: {str(e)}"
        )
        return False
