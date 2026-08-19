import copy
import json
import os
import tempfile
import threading

CONFIG_PATH = os.path.abspath(os.environ.get("CONFIG_PATH", "/data/config.json"))
_CONFIG_DIR = os.path.dirname(CONFIG_PATH)

# Everything here is set from the web UI — the only thing the container needs
# to know is where to put this file.
_DEFAULT_SETTINGS = {
    "max_commands_per_second": 10.0,
}

_DEFAULT = {
    "bridge_ip": None,
    "api_key": None,
    "custom_patterns": {},   # id -> {id, name, sequence}
    "groups": {},            # id -> {id, name, light_ids}
    "settings": dict(_DEFAULT_SETTINGS),
    "auth": None,            # None => console is open; else a password record
}

_lock = threading.Lock()

# The config is read on nearly every request (get_client, pattern lookups) and
# written rarely, so it's held in memory and only re-read if the file changes
# underneath us.
_cache = None
_cache_mtime = None


def _merged(data: dict) -> dict:
    merged = {**_DEFAULT, **data}
    # Shallow merge would drop defaults for any key a partial settings block
    # omits, so fill that one nested dict explicitly.
    merged["settings"] = {**_DEFAULT_SETTINGS, **(data.get("settings") or {})}
    return merged


def _ensure_file():
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w") as f:
            json.dump(_DEFAULT, f, indent=2)


def _read_locked() -> dict:
    global _cache, _cache_mtime
    _ensure_file()
    mtime = os.stat(CONFIG_PATH).st_mtime_ns
    if _cache is None or mtime != _cache_mtime:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        _cache = _merged(data)
        _cache_mtime = mtime
    return _cache


def load() -> dict:
    # Callers mutate what they get back and hand it to save(), so hand out a
    # copy rather than the cached object itself.
    with _lock:
        return copy.deepcopy(_read_locked())


def save(data: dict):
    # Written to a sibling temp file and renamed, so a crash mid-write can't
    # truncate the config — the bridge credentials live in here and losing
    # them means re-pairing with the physical link button.
    global _cache, _cache_mtime
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
        _cache = copy.deepcopy(_merged(data))
        _cache_mtime = os.stat(CONFIG_PATH).st_mtime_ns


def update(**kwargs):
    with _lock:
        data = copy.deepcopy(_read_locked())
    data.update(kwargs)
    save(data)
    return data


def get_settings() -> dict:
    return load()["settings"]


def update_settings(**kwargs) -> dict:
    data = load()
    data["settings"] = {**data["settings"], **kwargs}
    save(data)
    return data["settings"]
