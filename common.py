import json
from pathlib import Path

try:
    import requests
except ModuleNotFoundError:
    requests = None


CONFIG_PATH = "config.json"


def load_config(path: str = CONFIG_PATH) -> dict:
    config_file = Path(path)

    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(config_file, "r", encoding="utf-8") as f:
        config = json.load(f)

    required = ["host", "email", "password"]
    missing = [key for key in required if not config.get(key)]

    if missing:
        raise ValueError(f"Missing required config fields: {missing}")

    return config


def debug(message: str) -> None:
    print(f"[debug] {message}")


def config_bool(config: dict, key: str, default: bool = False) -> bool:
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def require_requests():
    if requests is None:
        raise RuntimeError("Missing dependency: run `python3 -m pip install -r requirements.txt`")
    return requests
