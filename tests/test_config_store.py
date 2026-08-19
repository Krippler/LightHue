import json
import os

import pytest


@pytest.fixture
def store(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "nested" / "config.json"))
    import app.config_store as config_store
    return importlib.reload(config_store)


def test_creates_nested_directory_and_defaults(store):
    cfg = store.load()
    assert cfg["bridge_ip"] is None
    assert cfg["custom_patterns"] == {}
    assert cfg["auth"] is None
    assert cfg["settings"]["max_commands_per_second"] == 10.0


def test_roundtrip(store):
    store.update(bridge_ip="10.0.0.5", api_key="k")
    assert store.load()["bridge_ip"] == "10.0.0.5"


def test_load_returns_a_copy_so_callers_cannot_poison_the_cache(store):
    cfg = store.load()
    cfg["custom_patterns"]["ghost"] = {"id": "ghost"}
    assert store.load()["custom_patterns"] == {}


def test_save_leaves_no_temp_files_behind(store):
    store.update(bridge_ip="10.0.0.5")
    leftovers = [f for f in os.listdir(os.path.dirname(store.CONFIG_PATH)) if f.startswith(".config-")]
    assert leftovers == []


def test_failed_save_does_not_truncate_the_existing_config(store, monkeypatch):
    store.update(bridge_ip="10.0.0.5", api_key="precious")

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store.json, "dump", boom)
    with pytest.raises(RuntimeError):
        store.update(bridge_ip="10.0.0.9")

    monkeypatch.undo()
    with open(store.CONFIG_PATH) as f:
        on_disk = json.load(f)
    assert on_disk["api_key"] == "precious"
    leftovers = [f for f in os.listdir(os.path.dirname(store.CONFIG_PATH)) if f.startswith(".config-")]
    assert leftovers == []


def test_partial_settings_block_keeps_defaults(store):
    store.load()   # create the file and its directory first
    with open(store.CONFIG_PATH, "w") as f:
        json.dump({"bridge_ip": "1.2.3.4", "settings": {}}, f)
    assert store.load()["settings"]["max_commands_per_second"] == 10.0


def test_external_edit_is_picked_up(store):
    store.update(bridge_ip="10.0.0.5")
    data = store.load()
    data["bridge_ip"] = "10.0.0.99"
    # Simulate another process replacing the file.
    with open(store.CONFIG_PATH, "w") as f:
        json.dump(data, f)
    os.utime(store.CONFIG_PATH, ns=(0, 0))
    assert store.load()["bridge_ip"] == "10.0.0.99"


def test_update_settings_merges(store):
    store.update_settings(max_commands_per_second=4.0)
    assert store.get_settings()["max_commands_per_second"] == 4.0
    assert store.load()["bridge_ip"] is None
