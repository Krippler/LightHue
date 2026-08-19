import json
import os
import threading

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/data/config.json")

_DEFAULT = {
    "bridge_ip": None,
    "api_key": None,
    "custom_patterns": {},   # id -> {id, name, sequence}
    "presets": {},           # id -> saved flicker settings for reuse
}

_lock = threading.Lock()


def _ensure_file():
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w") as f:
            json.dump(_DEFAULT, f, indent=2)


def load() -> dict:
    with _lock:
        _ensure_file()
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        merged = {**_DEFAULT, **data}
        return merged


def save(data: dict):
    with _lock:
        _ensure_file()
        with open(CONFIG_PATH, "w") as f:
            json.dump(data, f, indent=2)


def update(**kwargs):
    data = load()
    data.update(kwargs)
    save(data)
    return data
