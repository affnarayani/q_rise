import json
import os
from pathlib import Path
from typing import Any, Dict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "json" / "config.json"


def load_config() -> Dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def get_path(config: Dict[str, Any], key: str) -> Path:
    value = config["paths"][key]
    return PROJECT_ROOT / value


def get_env(name: str, required: bool = True, default: str = "") -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"{name} missing in environment variables")
    return value
