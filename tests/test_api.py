def configure(client):
    r = client.post("/api/bridge/set", json={"bridge_ip": "10.0.0.5", "api_key": "k"})
    assert r.status_code == 200


def test_index_and_unconfigured_state(client):
    assert client.get("/").status_code == 200
    assert client.get("/api/bridge").json() == {"bridge_ip": None, "configured": False}
    assert client.get("/api/lights").status_code == 400
    assert client.get("/api/status").json() == {"lights": {}}


def test_lights_are_listed_sorted_by_name(client, bridge):
    configure(client)
    lights = client.get("/api/lights").json()["lights"]
    assert [x["name"] for x in lights] == ["Armory Strip", "Slipgate Sconce"]
    assert lights[0]["reachable"] is False


def test_bridge_failure_surfaces_as_502(client, app_modules, monkeypatch):
    configure(client)

    async def boom(self):
        raise RuntimeError("no route to host")

    monkeypatch.setattr(app_modules.HueClient, "get_lights", boom)
    assert client.get("/api/lights").status_code == 502


def test_pairing_stores_credentials(client, bridge):
    r = client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    assert r.status_code == 200
    assert client.get("/api/bridge").json()["configured"] is True


# ---------- patterns ----------

def test_custom_pattern_crud(client):
    created = client.post("/api/patterns", json={"name": "Torch", "sequence": " MMA zz "}).json()
    assert created["sequence"] == "mmazz"       # trimmed and lowercased
    listed = client.get("/api/patterns").json()
    assert len(listed["builtin"]) == 12
    assert [p["id"] for p in listed["custom"]] == [created["id"]]
    assert client.delete(f"/api/patterns/{created['id']}").status_code == 200
    assert client.get("/api/patterns").json()["custom"] == []


def test_pattern_sequence_must_be_letters(client):
    assert client.post("/api/patterns", json={"name": "x", "sequence": "abc123"}).status_code == 400
    assert client.post("/api/patterns", json={"name": "x", "sequence": ""}).status_code == 400


def test_pattern_name_is_required(client):
    assert client.post("/api/patterns", json={"name": "", "sequence": "abc"}).status_code == 422


def test_builtin_patterns_cannot_be_deleted(client):
    r = client.delete("/api/patterns/steady")
    assert r.status_code == 400
    assert len(client.get("/api/patterns").json()["builtin"]) == 12


def test_deleting_an_unknown_pattern_is_404(client):
    assert client.delete("/api/patterns/custom_nope").status_code == 404


def test_running_pattern_cannot_be_deleted(client, bridge):
    configure(client)
    created = client.post("/api/patterns", json={"name": "Torch", "sequence": "mz"}).json()
    client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": created["id"], "hz": 5})
    r = client.delete(f"/api/patterns/{created['id']}")
    assert r.status_code == 409
    client.post("/api/flicker/stop", json={})
    assert client.delete(f"/api/patterns/{created['id']}").status_code == 200


# ---------- flicker validation ----------

def test_start_requires_a_configured_bridge(client):
    r = client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": "steady"})
    assert r.status_code == 400


def test_start_rejects_unknown_pattern(client, bridge):
    configure(client)
    assert client.post("/api/flicker/start",
                       json={"light_ids": ["1"], "pattern_id": "nope"}).status_code == 404


def test_start_rejects_inverted_brightness_window(client, bridge):
    configure(client)
    r = client.post("/api/flicker/start", json={
        "light_ids": ["1"], "pattern_id": "steady", "min_bri": 200, "max_bri": 50,
    })
    assert r.status_code == 422
    assert "min_bri" in str(r.json()["detail"])


def test_start_rejects_half_specified_colour(client, bridge):
    configure(client)
    r = client.post("/api/flicker/start", json={
        "light_ids": ["1"], "pattern_id": "steady", "hue": 100,
    })
    assert r.status_code == 422


def test_start_rejects_out_of_range_values(client, bridge):
    configure(client)
    for payload in (
        {"hz": 0}, {"hz": 25}, {"min_bri": 0}, {"max_bri": 500},
        {"transition_ms": -1}, {"sat": 900}, {"light_ids": []},
    ):
        body = {"light_ids": ["1"], "pattern_id": "steady", **payload}
        assert client.post("/api/flicker/start", json=body).status_code == 422, payload


def test_start_and_stop_roundtrip(client, bridge):
    configure(client)
    r = client.post("/api/flicker/start",
                    json={"light_ids": ["1", "2"], "pattern_id": "flicker_a", "hz": 5})
    assert r.status_code == 200
    status = client.get("/api/status").json()["lights"]
    assert status["1"]["running"] is True and status["2"]["running"] is True

    client.post("/api/flicker/stop", json={"light_ids": ["1"]})
    status = client.get("/api/status").json()["lights"]
    assert status["1"]["running"] is False and status["2"]["running"] is True

    client.post("/api/flicker/stop", json={})
    status = client.get("/api/status").json()["lights"]
    assert all(s["running"] is False for s in status.values())


# ---------- settings ----------

def test_settings_roundtrip_and_engine_wiring(client, app_modules):
    assert client.get("/api/settings").json()["max_commands_per_second"] == 10.0
    r = client.put("/api/settings", json={"max_commands_per_second": 4.0})
    assert r.status_code == 200
    assert app_modules.engine.limiter.min_interval == 0.25
    assert client.get("/api/settings").json()["max_commands_per_second"] == 4.0


def test_settings_rejects_out_of_range_rate(client):
    assert client.put("/api/settings", json={"max_commands_per_second": 0}).status_code == 422
    assert client.put("/api/settings", json={"max_commands_per_second": 99}).status_code == 422


# ---------- websocket ----------

def test_websocket_pushes_status_on_connect(client, bridge):
    configure(client)
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json() == {"type": "status", "data": {}}


# ---------- live update ----------

def test_update_retunes_running_lights(client, bridge):
    configure(client)
    client.post("/api/flicker/start",
                json={"light_ids": ["1", "2"], "pattern_id": "steady", "hz": 5})
    r = client.post("/api/flicker/update",
                    json={"light_ids": ["1", "2"], "hz": 12, "pattern_id": "fast_strobe"})
    assert r.status_code == 200
    assert sorted(r.json()["updated"]) == ["1", "2"]
    status = client.get("/api/status").json()["lights"]
    assert status["1"]["hz"] == 12
    assert status["1"]["pattern_id"] == "fast_strobe"
    assert status["1"]["sequence"] == "mamamamamama"
    client.post("/api/flicker/stop", json={})


def test_update_only_touches_supplied_fields(client, bridge):
    configure(client)
    client.post("/api/flicker/start", json={
        "light_ids": ["1"], "pattern_id": "steady", "hz": 5, "min_bri": 10, "max_bri": 200,
    })
    client.post("/api/flicker/update", json={"light_ids": ["1"], "hz": 9})
    status = client.get("/api/status").json()["lights"]["1"]
    assert status["hz"] == 9
    assert status["min_bri"] == 10 and status["max_bri"] == 200
    client.post("/api/flicker/stop", json={})


def test_update_can_set_colour_on_a_colourless_run(client, bridge):
    configure(client)
    client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": "steady"})
    assert client.get("/api/status").json()["lights"]["1"]["hue"] is None
    client.post("/api/flicker/update", json={"light_ids": ["1"], "hue": 12000, "sat": 200})
    status = client.get("/api/status").json()["lights"]["1"]
    assert (status["hue"], status["sat"]) == (12000, 200)
    client.post("/api/flicker/stop", json={})


def test_update_on_stopped_lights_is_409(client, bridge):
    configure(client)
    assert client.post("/api/flicker/update",
                       json={"light_ids": ["1"], "hz": 5}).status_code == 409


def test_update_validates_like_start(client, bridge):
    configure(client)
    client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": "steady"})
    for payload in ({"hz": 99}, {"min_bri": 200, "max_bri": 5}, {"hue": 100}, {"sat": 900}):
        body = {"light_ids": ["1"], **payload}
        assert client.post("/api/flicker/update", json=body).status_code == 422, payload
    assert client.post("/api/flicker/update",
                       json={"light_ids": ["1"], "pattern_id": "nope"}).status_code == 404
    client.post("/api/flicker/stop", json={})


# ---------- groups ----------

def test_group_crud(client):
    created = client.post("/api/groups", json={"name": " Hallway ", "light_ids": ["1", "2"]}).json()
    assert created["name"] == "Hallway"
    assert created["light_ids"] == ["1", "2"]
    assert [g["id"] for g in client.get("/api/groups").json()["groups"]] == [created["id"]]

    updated = client.put(f"/api/groups/{created['id']}",
                         json={"name": "Hall", "light_ids": ["3"]}).json()
    assert updated["name"] == "Hall" and updated["light_ids"] == ["3"]
    assert updated["id"] == created["id"]

    assert client.delete(f"/api/groups/{created['id']}").status_code == 200
    assert client.get("/api/groups").json()["groups"] == []


def test_group_requires_a_name_and_members(client):
    assert client.post("/api/groups", json={"name": "", "light_ids": ["1"]}).status_code == 422
    assert client.post("/api/groups", json={"name": "x", "light_ids": []}).status_code == 422


def test_unknown_group_is_404(client):
    assert client.delete("/api/groups/group_nope").status_code == 404
    assert client.put("/api/groups/group_nope",
                      json={"name": "x", "light_ids": ["1"]}).status_code == 404


def test_groups_survive_a_restart(client, app_modules):
    created = client.post("/api/groups", json={"name": "Hallway", "light_ids": ["1", "2"]}).json()
    app_modules.config_store._cache = None      # force a re-read from disk
    assert client.get("/api/groups").json()["groups"][0]["id"] == created["id"]


def test_starting_a_group_starts_every_member(client, bridge):
    configure(client)
    group = client.post("/api/groups", json={"name": "Hallway", "light_ids": ["1", "2"]}).json()
    client.post("/api/flicker/start",
                json={"light_ids": group["light_ids"], "pattern_id": "flicker_a", "hz": 5})
    status = client.get("/api/status").json()["lights"]
    assert status["1"]["running"] and status["2"]["running"]
    assert status["1"]["pattern_id"] == status["2"]["pattern_id"] == "flicker_a"
    client.post("/api/flicker/stop", json={})
