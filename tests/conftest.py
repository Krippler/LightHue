import importlib
import os

import httpx
import pytest

# Point the config at a throwaway file before anything imports config_store,
# so tests never touch a real /data/config.json.
os.environ.setdefault("CONFIG_PATH", "/tmp/hue-flicker-tests/config.json")


@pytest.fixture
def app_modules(tmp_path, monkeypatch):
    """A freshly imported app stack with its own config file."""
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.json"))
    import app.auth as auth
    import app.config_store as config_store
    import app.hue_client as hue_client
    import app.main as main
    for mod in (config_store, auth, hue_client, main):
        importlib.reload(mod)
    return main


@pytest.fixture
def client(app_modules):
    from fastapi.testclient import TestClient
    with TestClient(app_modules.app) as c:
        yield c


@pytest.fixture
def bridge(app_modules, monkeypatch):
    """Stub Hue bridge wired in through the shared httpx transport."""
    state = {"lights": {
        "1": {"name": "Slipgate Sconce", "state": {"on": True, "reachable": True}},
        "2": {"name": "Armory Strip", "state": {"on": False, "reachable": False}},
    }, "puts": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/lights"):
            return httpx.Response(200, json=state["lights"])
        if request.url.path.endswith("/state"):
            import json
            state["puts"].append(json.loads(request.content))
            return httpx.Response(200, json=[{"success": {}}])
        if request.url.path == "/api":
            return httpx.Response(200, json=[{"success": {"username": "stub-key"}}])
        return httpx.Response(404, json={})

    import app.hue_client as hue_client
    stub = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(hue_client, "_http", lambda: stub)
    return state
