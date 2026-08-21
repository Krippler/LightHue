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
    # Mirrors what a real bridge returns: full colour state on every light,
    # with colormode saying which of hue/sat, xy or ct is authoritative.
    state = {"lights": {
        "1": {"name": "Slipgate Sconce", "state": {
            "on": True, "reachable": True, "bri": 180,
            "hue": 8000, "sat": 140, "xy": [0.5, 0.4], "ct": 400, "colormode": "hs",
        }},
        "2": {"name": "Armory Strip", "state": {
            "on": False, "reachable": False, "bri": 60,
            "hue": 44000, "sat": 250, "xy": [0.2, 0.1], "ct": 250, "colormode": "xy",
        }},
        "3": {"name": "Nailgun Nook", "state": {
            "on": True, "reachable": True, "bri": 90, "ct": 366, "colormode": "ct",
        }},
        "4": {"name": "Rocket Alcove", "state": {
            "on": True, "reachable": True, "bri": 220,
            "hue": 12000, "sat": 90, "xy": [0.31, 0.33], "ct": 300, "colormode": "xy",
        }},
    }, "puts": []}

    # The bridge's own rooms and zones, as the Hue app would have set them up.
    state["groups"] = {
        "1": {"name": "Living room", "type": "Room", "class": "Living room",
              "lights": ["1", "3"]},
        "2": {"name": "Upstairs", "type": "Zone", "lights": ["2", "4"]},
        "3": {"name": "Ceiling fitting", "type": "Luminaire", "lights": ["1"]},
        # Already being streamed to by something else — Hue Sync, a game.
        "4": {"name": "TV area", "type": "Entertainment", "lights": ["1", "2"],
              "stream": {"active": True, "owner": "someone-else"}},
        "5": {"name": "Odds and ends", "type": "LightGroup", "lights": ["4"]},
        "6": {"name": "Game room", "type": "Entertainment", "class": "Other",
              "lights": ["1", "2", "3", "4"],
              "stream": {"active": False, "owner": None}},
    }

    # The v2 view of the same entertainment area, which is what the current Hue
    # app creates and what the bridge actually arms.
    state["v2"] = {"armed": [], "configurations": [{
        "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "id_v1": "/groups/6",
        "name": "Game room",
        "channels": [{"channel_id": 0}, {"channel_id": 1}, {"channel_id": 2}],
    }]}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/clip/v2/resource/entertainment_configuration" in request.url.path:
            if request.headers.get("hue-application-key") is None:
                return httpx.Response(401, json={"errors": [{"description": "no key"}]})
            if request.method == "PUT":
                import json
                body = json.loads(request.content)
                state["v2"]["armed"].append(body.get("action"))
                return httpx.Response(200, json={"data": [], "errors": []})
            return httpx.Response(200, json={"data": state["v2"]["configurations"],
                                             "errors": []})
        if request.url.path.endswith("/groups"):
            return httpx.Response(200, json=state["groups"])
        if request.url.path.endswith("/lights"):
            return httpx.Response(200, json=state["lights"])
        if request.method == "PUT" and "/groups/" in request.url.path:
            import json
            body = json.loads(request.content)
            gid = request.url.path.rsplit("/", 1)[-1]
            active = bool(body.get("stream", {}).get("active"))
            state["groups"][gid]["stream"]["active"] = active
            state["groups"][gid]["stream"]["owner"] = "k" if active else None
            state.setdefault("stream_calls", []).append((gid, active))
            return httpx.Response(200, json=[{"success": {}}])
        if request.url.path.endswith("/state"):
            import json
            state["puts"].append(json.loads(request.content))
            return httpx.Response(200, json=[{"success": {}}])
        if request.url.path == "/api":
            success = {"username": "stub-key"}
            # Firmware old enough not to issue a streaming key just omits it.
            if not state.get("omit_client_key"):
                success["clientkey"] = "stub-client-key"
            return httpx.Response(200, json=[{"success": success}])
        return httpx.Response(404, json={})

    import app.hue_client as hue_client
    import app.hue_v2 as hue_v2
    stub = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(hue_client, "_http", lambda: stub)
    monkeypatch.setattr(hue_v2, "_http", lambda: stub)
    return state
