import json
import os
import tempfile
import threading

CONFIG_PATH = os.path.abspath(os.environ.get("CONFIG_PATH", "/data/config.json"))
_CONFIG_DIR = os.path.dirname(CONFIG_PATH)

_DEFAULT = {
    "bridge_ip": None,
    "api_key": None,
    "custom_patterns": {},   # id -> {id, name, sequence}
    "presets": {},           # id -> saved flicker settings for reuse
}

_lock = threading.Lock()


def _ensure_file():
    os.makedirs(_CONFIG_DIR, exist_ok=True)
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
    # Written to a sibling temp file and renamed, so a crash mid-write can't
    # truncate the config — the bridge credentials live in here and losing
    # them means re-pairing with the physical link button.
    with _lock:
        _ensure_file()
        fd, tmp_path = tempfile.mkstemp(dir=_CONFIG_DIR, prefix=".config-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, CONFIG_PATH)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise


def update(**kwargs):
    data = load()
    data.update(kwargs)
    save(data)
    return data
