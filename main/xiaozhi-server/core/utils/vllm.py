import os
import sys
from pathlib import Path

# 添加项目根目录到Python路径
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "..", ".."))
sys.path.insert(0, project_root)

from config.logger import setup_logging
import importlib

logger = setup_logging()


def _model_source(process_config):
    hf_model = process_config.get("hf_model")
    if hf_model:
        return "hf", str(hf_model).strip().casefold()

    model_path = process_config.get("model_path")
    if model_path:
        return "path", str(Path(model_path).expanduser().resolve())
    return None


def _projector_source(process_config):
    mmproj_path = process_config.get("mmproj_path")
    if not mmproj_path:
        return None
    return str(Path(mmproj_path).expanduser().resolve())


def resolve_provider_config(config, provider_name):
    """Resolve VLLM config and safely advertise a reusable local LLM server."""
    provider_config = dict(config["VLLM"][provider_name])
    provider_config["process"] = dict(provider_config.get("process") or {})
    provider_type = provider_config.get("type", provider_name)
    if provider_type != "llama_cpp":
        return provider_type, provider_config

    selected_llm = config.get("selected_module", {}).get("LLM")
    llm_config = config.get("LLM", {}).get(selected_llm, {})
    llm_type = llm_config.get("type", selected_llm)
    if llm_type != "llama_cpp":
        return provider_type, provider_config

    vllm_process = provider_config["process"]
    llm_process = dict(llm_config.get("process") or {})
    if _model_source(vllm_process) != _model_source(llm_process):
        return provider_type, provider_config
    if _model_source(vllm_process) is None:
        return provider_type, provider_config
    if _projector_source(vllm_process) != _projector_source(llm_process):
        return provider_type, provider_config

    vllm_context = int(vllm_process.get("context_size", 4096))
    llm_context = int(llm_process.get("context_size", 4096))
    if llm_context < vllm_context:
        return provider_type, provider_config

    host = str(llm_process.get("host", "127.0.0.1"))
    port = int(llm_process.get("port", 6000))
    endpoint_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    provider_config["reuse_server"] = {
        "base_url": f"http://{endpoint_host}:{port}/v1",
        "health_url": f"http://{endpoint_host}:{port}/health",
        "model_name": llm_config.get("model_name"),
    }
    return provider_type, provider_config


def create_instance(class_name, *args, **kwargs):
    # 创建LLM实例
    if os.path.exists(os.path.join("core", "providers", "vllm", f"{class_name}.py")):
        lib_name = f"core.providers.vllm.{class_name}"
        if lib_name not in sys.modules:
            sys.modules[lib_name] = importlib.import_module(f"{lib_name}")
        return sys.modules[lib_name].VLLMProvider(*args, **kwargs)

    raise ValueError(f"不支持的VLLM类型: {class_name}，请检查该配置的type是否设置正确")
