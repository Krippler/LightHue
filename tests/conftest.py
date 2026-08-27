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
        # A smart plug, as the bridge really reports one: it switches a relay,
        # so there is no bri and no colour anywhere in its state.
        "5": {"name": "Quad Socket", "state": {
            "on": False, "reachable": True, "mode": "homeautomation",
        }},
    }, "puts": [], "light_reads": 0}

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
        "status": "inactive",
        # A real configuration always names what each channel drives. Channel 2
        # carries two services on purpose: a channel is a position in the room,
        # not a bulb, and code that assumes one-to-one breaks there.
        "channels": [
            {"channel_id": 0,
             "members": [{"service": {"rid": "ent-1", "rtype": "entertainment"}, "index": 0}]},
            {"channel_id": 1,
             "members": [{"service": {"rid": "ent-2", "rtype": "entertainment"}, "index": 0}]},
            {"channel_id": 2,
             "members": [{"service": {"rid": "ent-1", "rtype": "entertainment"}, "index": 1},
                         {"service": {"rid": "ent-2", "rtype": "entertainment"}, "index": 1}]},
        ],
    }]}

    # Per-light entertainment services. Light 3 is white-only and light 4 is a
    # plug in spirit: neither can render, which is what keeps them out of an
    # area and is the whole difference between an area and a group.
    state["v2"]["entertainment"] = [
        {"id": "ent-1", "id_v1": "/lights/1", "renderer": True},
        {"id": "ent-2", "id_v1": "/lights/2", "renderer": True},
        {"id": "ent-3", "id_v1": "/lights/3", "renderer": False},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/clip/v2/resource/entertainment"):
            if request.headers.get("hue-application-key") is None:
                return httpx.Response(401, json={"errors": [{"description": "no key"}]})
            return httpx.Response(200, json={"data": state["v2"]["entertainment"],
                                             "errors": []})
        if "/clip/v2/resource/entertainment_configuration" in path:
            if request.headers.get("hue-application-key") is None:
                return httpx.Response(401, json={"errors": [{"description": "no key"}]})
            wanted = path.rsplit("/", 1)[-1]
            configurations = state["v2"]["configurations"]
            if wanted != "entertainment_configuration":
                configurations = [c for c in configurations if c["id"] == wanted]
            if request.method == "POST":
                import json
                body = json.loads(request.content)
                created = {
                    "id": f"new-area-{len(state['v2']['configurations'])}",
                    "id_v1": f"/groups/{90 + len(state['v2']['configurations'])}",
                    "name": body["metadata"]["name"],
                    "status": "inactive",
                    "channels": [],
                    "created": body,
                }
                state["v2"]["configurations"].append(created)
                return httpx.Response(200, json={
                    "data": [{"rid": created["id"], "rtype": "entertainment_configuration"}],
                    "errors": []})
            if request.method == "DELETE":
                state["v2"]["configurations"] = [
                    c for c in state["v2"]["configurations"] if c["id"] != wanted]
                return httpx.Response(200, json={
                    "data": [{"rid": wanted, "rtype": "entertainment_configuration"}],
                    "errors": []})
            if request.method == "PUT":
                import json
                body = json.loads(request.content)
                if "metadata" in body:
                    for configuration in configurations:
                        configuration["name"] = body["metadata"]["name"]
                    return httpx.Response(200, json={"data": [], "errors": []})
                action = body.get("action")
                state["v2"]["armed"].append(action)
                # The bridge reports the stream as up only once it is; the app
                # reads this back rather than trusting the 200.
                for configuration in configurations:
                    configuration["status"] = ("active" if action == "start"
                                               else "inactive")
                return httpx.Response(200, json={"data": [], "errors": []})
            return httpx.Response(200, json={"data": configurations, "errors": []})
        if path.endswith("/groups"):
            return httpx.Response(200, json=state["groups"])
        if path.endswith("/lights"):
            # Counted so a test can hold the line on how many reads a start costs.
            state["light_reads"] += 1
            return httpx.Response(200, json=state["lights"])
        if request.method == "PUT" and "/groups/" in request.url.path:
            import json
            body = json.loads(request.content)
            gid = request.url.path.rsplit("/", 1)[-1]
            active = bool(body.get("stream", {}).get("active"))
            state["groups"][gid]["stream"]["active"] = active
            # A real bridge records whoever made the call, which is what makes
            # owner the caller's own API key. Hardcoding one username hid that:
            # a console paired for a different key read its own claim as
            # somebody else's.
            caller = request.url.path.split("/api/", 1)[-1].split("/", 1)[0]
            state["groups"][gid]["stream"]["owner"] = caller if active else None
            state.setdefault("stream_calls", []).append((gid, active))
            return httpx.Response(200, json=[{"success": {}}])
        if request.url.path.endswith("/state"):
            import json
            lid = request.url.path.split("/lights/")[1].split("/")[0]
            body = json.loads(request.content)
            state["puts"].append(body)
            # A real bridge answers per parameter, and declines the ones the
            # device does not have — at HTTP 200, so a caller that only checks
            # the status code cannot tell. Modelled here so that sending a
            # brightness to the plug looks exactly as harmless as it really is.
            known = (state["lights"].get(lid) or {}).get("state", {})
            answer = []
            for key, value in body.items():
                if key == "on" or key in known or key == "transitiontime" and "bri" in known:
                    answer.append({"success": {f"/lights/{lid}/state/{key}": value}})
                else:
                    answer.append({"error": {
                        "type": 6, "address": f"/lights/{lid}/state/{key}",
                        "description": f"parameter, {key}, not available"}})
            return httpx.Response(200, json=answer)
        if path.endswith("/config"):
            return httpx.Response(200, json={
                "name": "Stub Bridge", "modelid": "BSB002",
                "swversion": "1970010101", "apiversion": "1.68.0",
                "bridgeid": "0000000000000000", "factorynew": False,
            })
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
