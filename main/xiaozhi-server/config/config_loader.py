import os
import yaml
from collections.abc import Mapping


def get_project_dir():
    """Return the server project directory."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/"


def read_config(config_path):
    with open(config_path, "r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def load_default_config(config_path=None):
    """Load reference fragments, then inline defaults from the root YAML file."""
    config_path = config_path or os.path.join(get_project_dir(), "config.yaml")
    manifest = read_config(config_path)
    if not isinstance(manifest, Mapping):
        raise ValueError(f"Default configuration must be an object: {config_path}")

    includes = manifest.get("includes", [])
    if not isinstance(includes, list):
        raise ValueError("Default configuration includes must be a list")

    base_dir = os.path.realpath(os.path.dirname(os.path.abspath(config_path)))
    defaults = {}
    included_paths = set()
    for filename in includes:
        if not isinstance(filename, str) or not filename.strip():
            raise ValueError("Default configuration includes must contain file paths")
        fragment_path = os.path.realpath(os.path.join(base_dir, filename))
        if os.path.isabs(filename) or os.path.commonpath(
            [base_dir, fragment_path]
        ) != base_dir:
            raise ValueError(f"Default configuration include must stay local: {filename}")
        if fragment_path in included_paths:
            raise ValueError(f"Duplicate default configuration include: {filename}")
        included_paths.add(fragment_path)

        fragment = read_config(fragment_path)
        if not isinstance(fragment, Mapping) or "includes" in fragment:
            raise ValueError(
                f"Default configuration fragment must be an object without includes: {filename}"
            )
        defaults = merge_configs(defaults, fragment)

    inline_defaults = {key: value for key, value in manifest.items() if key != "includes"}
    return merge_configs(defaults, inline_defaults)


async def load_config():
    """Load and merge the local YAML configuration."""
    from core.utils.cache.manager import cache_manager, CacheType

    # Reuse the process-local config cache.
    cached_config = cache_manager.get(CacheType.CONFIG, "main_config")
    if cached_config is not None:
        return cached_config

    from config.local_config import load_local_config

    # Load defaults and local overrides.
    default_config = load_default_config()
    custom_config = load_local_config()

    # Private local overrides take precedence over all reference defaults.
    config = merge_configs(default_config, custom_config)
    # Create configured output directories.
    ensure_directories(config)

    # Cache the merged config.
    cache_manager.set(CacheType.CONFIG, "main_config", config)
    return config


def ensure_directories(config):
    """Create directories referenced by the configuration."""
    dirs_to_create = set()
    project_dir = get_project_dir()
    # Log directory.
    log_dir = config.get("log", {}).get("log_dir", "tmp")
    dirs_to_create.add(os.path.join(project_dir, log_dir))

    # ASR/TTS output directories.
    for module in ["ASR", "TTS"]:
        if config.get(module) is None:
            continue
        for provider in config.get(module, {}).values():
            output_dir = provider.get("output_dir", "")
            if output_dir:
                dirs_to_create.add(output_dir)

    # Output directories for selected providers.
    selected_modules = config.get("selected_module", {})
    for module_type in ["ASR", "LLM", "TTS"]:
        selected_provider = selected_modules.get(module_type)
        if not selected_provider:
            continue
        if config.get(module_type) is None:
            continue
        if config.get(selected_provider) is None:
            continue
        provider_config = config.get(module_type, {}).get(selected_provider, {})
        output_dir = provider_config.get("output_dir")
        if output_dir:
            full_model_dir = os.path.join(project_dir, output_dir)
            dirs_to_create.add(full_model_dir)

    # Create all resolved directories.
    for dir_path in dirs_to_create:
        try:
            os.makedirs(dir_path, exist_ok=True)
        except PermissionError:
            print(f"Warning: cannot create {dir_path}; check write permissions")


def merge_configs(default_config, custom_config):
    """
    Recursively merge configs, with custom_config taking precedence.

    Args:
        default_config: Reference defaults.
        custom_config: Local overrides.

    Returns:
        The merged configuration.
    """
    if not isinstance(default_config, Mapping) or not isinstance(
        custom_config, Mapping
    ):
        return custom_config

    merged = dict(default_config)

    for key, value in custom_config.items():
        if (
            key in merged
            and isinstance(merged[key], Mapping)
            and isinstance(value, Mapping)
        ):
            merged[key] = merge_configs(merged[key], value)
        else:
            merged[key] = value

    return merged
