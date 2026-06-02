import os
from pathlib import Path

import yaml

_config = None


def load_config(path: str | None = None) -> dict:
    global _config
    if _config is not None and path is None:
        return _config
    if path is None:
        path = os.environ.get("MEDIACLEANER_CONFIG", "config.yaml")
    with open(Path(path)) as f:
        _config = yaml.safe_load(f)
    return _config


def get_config() -> dict:
    if _config is None:
        return load_config()
    return _config
