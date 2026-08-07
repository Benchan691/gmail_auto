import json
import os
from pathlib import Path
from typing import Dict

try:
    import requests
except ModuleNotFoundError:
    requests = None


CONFIG_PATH = "config.json"
ENV_PATH = ".env"

# config.json key → environment variable name
_SENSITIVE_ENV_KEYS = {
    "email": "ZIMBRA_EMAIL",
    "password": "ZIMBRA_PASSWORD",
    "splunk_username": "SPLUNK_USERNAME",
    "splunk_password": "SPLUNK_PASSWORD",
}


def _parse_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_dotenv(path: str = ENV_PATH) -> None:
    """Load .env into os.environ without overriding existing variables."""
    for key, value in _parse_env_file(Path(path)).items():
        os.environ.setdefault(key, value)


def load_config(path: str = CONFIG_PATH, env_path: str = ENV_PATH) -> dict:
    config_file = Path(path)

    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(config_file, "r", encoding="utf-8") as f:
        config = json.load(f)

    load_dotenv(env_path)

    for config_key, env_key in _SENSITIVE_ENV_KEYS.items():
        env_value = os.environ.get(env_key)
        if env_value:
            config[config_key] = env_value

    required = ["host", "email", "password"]
    missing = [key for key in required if not config.get(key)]

    if missing:
        raise ValueError(
            f"Missing required config fields: {missing}. "
            f"Set them in {env_path} (see .env.example) or {path}."
        )

    return config


def debug(message: str) -> None:
    """No-op reserved for optional verbose tracing."""
    return


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
