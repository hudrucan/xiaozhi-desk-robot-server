import os
from config.config_loader import get_project_dir, load_config


default_config_file = "config.yaml"
config_file_valid = False


def check_config_file():
    """Verify that the local override file exists."""
    global config_file_valid
    if config_file_valid:
        return
    custom_config_file = get_project_dir() + "data/." + default_config_file
    if not os.path.exists(custom_config_file):
        raise FileNotFoundError(
            "data/.config.yaml was not found; create it before starting the server"
        )

    config_file_valid = True
