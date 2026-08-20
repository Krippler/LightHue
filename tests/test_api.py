import pytest

from app.patterns import BUILTIN_PATTERNS


def configure(client):
    r = client.post("/api/bridge/set", json={"bridge_ip": "10.0.0.5", "api_key": "k"})
    assert r.status_code == 200


def test_index_and_unconfigured_state(client):
    assert client.get("/").status_code == 200
    assert client.get("/api/bridge").json() == {"bridge_ip": None, "configured": False}
    assert client.get("/api/lights").status_code == 400
    status = client.get("/api/status").json()
    assert status["lights"] == {} and status["snapshots"] == {}
    assert isinstance(status["now"], float)


def test_lights_are_listed_sorted_by_name(client, bridge):
    configure(client)
    lights = client.get("/api/lights").json()["lights"]
    assert [x["name"] for x in lights] == [
        "Armory Strip", "Nailgun Nook", "Rocket Alcove", "Slipgate Sconce",
    ]
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
    assert len(listed["builtin"]) == len(BUILTIN_PATTERNS)
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
    assert len(client.get("/api/patterns").json()["builtin"]) == len(BUILTIN_PATTERNS)


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
        msg = ws.receive_json()
    assert msg["type"] == "status"
    assert msg["lights"] == {} and msg["snapshots"] == {}
    # the browser needs this to line its playhead up with the running loops
    assert isinstance(msg["now"], float)


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


# ---------- colour snapshot / restore ----------

def test_lights_expose_their_current_colour(client, bridge):
    configure(client)
    lights = {x["name"]: x for x in client.get("/api/lights").json()["lights"]}
    sconce = lights["Slipgate Sconce"]
    assert (sconce["hue"], sconce["sat"], sconce["bri"]) == (8000, 140, 180)
    assert sconce["colormode"] == "hs"
    assert sconce["has_color"] is True
    assert lights["Nailgun Nook"]["has_color"] is False   # white-only bulb


def test_starting_snapshots_the_bulb_state(client, bridge):
    configure(client)
    client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": "steady"})
    snaps = client.get("/api/lights").json()["snapshots"]
    assert snaps["1"] == {"on": True, "bri": 180, "hue": 8000, "sat": 140}
    client.post("/api/flicker/stop", json={})


def test_stop_puts_the_bulb_back(client, bridge):
    configure(client)
    client.post("/api/flicker/start",
                json={"light_ids": ["1"], "pattern_id": "steady", "hue": 40000, "sat": 250})
    bridge["puts"].clear()
    client.post("/api/flicker/stop", json={})
    restore = bridge["puts"][-1]
    assert restore["on"] is True
    assert (restore["hue"], restore["sat"], restore["bri"]) == (8000, 140, 180)
    # snapshot is consumed once it has been put back
    assert client.get("/api/lights").json()["snapshots"] == {}


def test_restore_respects_colormode(client, bridge):
    # Sending hue/sat to a bulb the bridge reports in xy or ct mode would
    # change its colour rather than put it back.
    configure(client)
    client.post("/api/flicker/start", json={"light_ids": ["3", "4"], "pattern_id": "steady"})
    bridge["puts"].clear()
    client.post("/api/flicker/stop", json={})
    ct_put = next(p for p in bridge["puts"] if "ct" in p)
    xy_put = next(p for p in bridge["puts"] if "xy" in p)
    assert ct_put["ct"] == 366 and "hue" not in ct_put and "xy" not in ct_put
    assert xy_put["xy"] == [0.31, 0.33] and "hue" not in xy_put and "ct" not in xy_put


def test_a_bulb_that_was_off_is_left_off(client, bridge):
    configure(client)
    client.post("/api/flicker/start", json={"light_ids": ["2"], "pattern_id": "steady"})
    snaps = client.get("/api/lights").json()["snapshots"]
    assert snaps["2"]["on"] is False
    bridge["puts"].clear()
    client.post("/api/flicker/stop", json={})
    assert bridge["puts"][-1]["on"] is False


def test_restarting_keeps_the_original_snapshot(client, bridge):
    configure(client)
    client.post("/api/flicker/start",
                json={"light_ids": ["1"], "pattern_id": "steady", "hue": 40000, "sat": 250})
    first = client.get("/api/lights").json()["snapshots"]["1"]
    # Restart while running — the snapshot must not become our own flicker output.
    client.post("/api/flicker/start",
                json={"light_ids": ["1"], "pattern_id": "fast_strobe", "hue": 100, "sat": 10})
    assert client.get("/api/lights").json()["snapshots"]["1"] == first
    client.post("/api/flicker/stop", json={})


def test_restore_can_be_switched_off(client, bridge):
    configure(client)
    client.put("/api/settings", json={"max_commands_per_second": 10, "restore_on_stop": False})
    client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": "steady"})
    bridge["puts"].clear()
    client.post("/api/flicker/stop", json={})
    assert not any("hue" in p and p.get("bri") == 180 for p in bridge["puts"])
    # the snapshot is kept, so it can still be reverted by hand
    assert "1" in client.get("/api/lights").json()["snapshots"]


def test_manual_restore_endpoint(client, bridge):
    configure(client)
    client.put("/api/settings", json={"max_commands_per_second": 10, "restore_on_stop": False})
    client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": "steady"})
    client.post("/api/flicker/stop", json={})
    bridge["puts"].clear()
    r = client.post("/api/flicker/restore", json={"light_ids": ["1"]})
    assert r.json()["restored"] == ["1"]
    assert bridge["puts"][-1]["bri"] == 180
    assert client.get("/api/lights").json()["snapshots"] == {}


def test_snapshots_persist_for_a_later_run(client, app_modules, bridge):
    configure(client)
    client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": "steady"})
    # Written to the config, so a container that dies mid-flicker can still
    # put the bulb back on its next boot.
    assert app_modules.config_store.load()["snapshots"]["1"]["bri"] == 180
    client.post("/api/flicker/stop", json={})
    assert app_modules.config_store.load()["snapshots"] == {}


def test_leftover_snapshots_are_restored_on_boot(app_modules, bridge):
    # A container killed mid-flicker leaves bulbs wherever the flicker stopped;
    # the snapshot on disk is what lets the next boot put them back.
    from fastapi.testclient import TestClient

    app_modules.config_store.update(
        bridge_ip="10.0.0.5", api_key="k",
        snapshots={"1": {"on": True, "bri": 180, "hue": 8000, "sat": 140}},
    )
    bridge["puts"].clear()
    with TestClient(app_modules.app):
        pass
    assert bridge["puts"][-1]["bri"] == 180
    assert bridge["puts"][-1]["hue"] == 8000
    assert app_modules.config_store.load()["snapshots"] == {}


def test_leftover_snapshots_are_kept_when_restore_is_off(app_modules, bridge):
    from fastapi.testclient import TestClient

    app_modules.config_store.update(
        bridge_ip="10.0.0.5", api_key="k",
        snapshots={"1": {"on": True, "bri": 180}},
    )
    app_modules.config_store.update_settings(restore_on_stop=False)
    bridge["puts"].clear()
    with TestClient(app_modules.app):
        pass
    assert bridge["puts"] == []
    assert app_modules.config_store.load()["snapshots"] == {"1": {"on": True, "bri": 180}}


def test_boot_restore_is_skipped_without_a_bridge(app_modules, bridge):
    from fastapi.testclient import TestClient

    app_modules.config_store.update(snapshots={"1": {"on": True, "bri": 180}})
    with TestClient(app_modules.app) as c:
        assert c.get("/api/bridge").json()["configured"] is False
    assert app_modules.config_store.load()["snapshots"] == {"1": {"on": True, "bri": 180}}


def test_status_channel_carries_snapshots(client, bridge):
    # The browser needs to know a revert is available without re-fetching
    # everything, so snapshots ride along with the status push.
    configure(client)
    client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": "steady"})
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
    assert msg["snapshots"]["1"]["bri"] == 180
    assert msg["lights"]["1"]["running"] is True
    assert client.post("/api/flicker/stop", json={}).json()["snapshots"] == {}


def test_status_reports_the_rate_a_light_actually_gets(client, bridge, app_modules):
    # Three lights at 10Hz want 30 commands/sec from a 10/sec budget, so each
    # one is really running at a third of what was asked for. The UI needs
    # that number to tell the truth about what the bridge can carry.
    configure(client)
    client.put("/api/settings", json={"max_commands_per_second": 10, "restore_on_stop": True})
    client.post("/api/flicker/start",
                json={"light_ids": ["1", "2", "3"], "pattern_id": "flicker_a", "hz": 10})
    from app.flicker_engine import GROUP_HEADROOM

    status = client.get("/api/status").json()["lights"]
    assert status["1"]["hz"] == 10
    # Lights sharing the bridge leave a little of the budget unspent so their
    # sends can go out as one burst instead of strung out across the second.
    assert status["1"]["effective_hz"] == pytest.approx(10 * GROUP_HEADROOM / 3, abs=0.01)
    client.post("/api/flicker/stop", json={"light_ids": ["3"]})
    assert client.get("/api/status").json()["lights"]["1"]["effective_hz"] == pytest.approx(
        10 * GROUP_HEADROOM / 2, abs=0.01)
    client.post("/api/flicker/stop", json={"light_ids": ["2"]})
    # the last light left has nothing to stay in step with, so it gets the lot
    assert client.get("/api/status").json()["lights"]["1"]["effective_hz"] == 10.0
    client.post("/api/flicker/stop", json={})


def test_effective_rate_is_absent_once_stopped(client, bridge):
    configure(client)
    client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": "steady"})
    client.post("/api/flicker/stop", json={})
    assert "effective_hz" not in client.get("/api/status").json()["lights"]["1"]


def test_patterns_endpoint_lists_games_in_order(client):
    body = client.get("/api/patterns").json()
    assert body["games"] == sorted(body["games"], key=str.casefold)
    assert body["games"][0] == "Blood" and body["games"][-1] == "Unreal Tournament"
    # Every game in the menu order can be filled from the pattern list, either
    # by owning patterns or by having them shared into it.
    for game in body["games"]:
        assert any(p["game"] == game or game in p.get("shared_with", [])
                   for p in body["builtin"]), game


def test_patterns_endpoint_exposes_sharing(client):
    body = client.get("/api/patterns").json()
    shared = [p for p in body["builtin"] if p.get("shared_with")]
    # Quake's whole table, Half-Life's style 12, and Unreal's light types.
    by_game = {}
    for p in shared:
        by_game.setdefault(p["game"], 0)
        by_game[p["game"]] += 1
    assert by_game == {"Quake": 13, "Half-Life": 1, "Unreal": 5}


# ---------- sharing patterns as a file ----------

def make(client, name, sequence):
    r = client.post("/api/patterns", json={"name": name, "sequence": sequence})
    assert r.status_code == 200, r.text
    return r.json()


def test_export_returns_a_downloadable_pack(client):
    make(client, "Torchlight", "mmnmmo")
    make(client, "Sputter", "azaz")
    r = client.get("/api/patterns/export")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert "game-hue-flicker-patterns-" in r.headers["content-disposition"]
    pack = r.json()
    assert pack["format"] == "game-hue-flicker/patterns"
    assert sorted(p["name"] for p in pack["patterns"]) == ["Sputter", "Torchlight"]
    # ids are console-local bookkeeping and have no business in a shared file
    assert all(set(p) == {"name", "sequence", "hz", "min_bri", "max_bri",
                          "transition_ms", "hue", "sat"}
               for p in pack["patterns"])


def test_export_with_nothing_to_share_is_404(client):
    assert client.get("/api/patterns/export").status_code == 404


def test_export_then_import_round_trips(client, app_modules):
    make(client, "Torchlight", "mmnmmo")
    pack = client.get("/api/patterns/export").json()

    # a second console, starting empty
    app_modules.config_store.update(custom_patterns={})
    r = client.post("/api/patterns/import", json=pack)
    assert r.status_code == 200
    assert [p["name"] for p in r.json()["added"]] == ["Torchlight"]
    assert r.json()["skipped"] == []
    listed = client.get("/api/patterns").json()["custom"]
    assert [(p["name"], p["sequence"]) for p in listed] == [("Torchlight", "mmnmmo")]


def test_imported_patterns_are_usable_immediately(client, bridge):
    configure(client)
    r = client.post("/api/patterns/import", json={
        "patterns": [{"name": "Imported", "sequence": "azaz"}],
    })
    pid = r.json()["added"][0]["id"]
    start = client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": pid})
    assert start.status_code == 200
    assert client.get("/api/status").json()["lights"]["1"]["sequence"] == "azaz"
    client.post("/api/flicker/stop", json={})


def test_import_accepts_a_hand_written_file(client):
    r = client.post("/api/patterns/import", json={
        "name": "Handmade pack", "author": "someone",
        "patterns": [{"name": " Sputtering  Lamp ", "sequence": " MM Az "}],
    })
    body = r.json()
    assert body["pack_name"] == "Handmade pack" and body["author"] == "someone"
    assert body["added"][0]["name"] == "Sputtering Lamp"
    assert body["added"][0]["sequence"] == "mmaz"


def test_import_skips_patterns_it_already_has(client):
    make(client, "Torchlight", "mmnmmo")
    r = client.post("/api/patterns/import", json={
        "patterns": [{"name": "Someone Elses Name", "sequence": "mmnmmo"},
                     {"name": "Genuinely New", "sequence": "azazaz"}],
    })
    body = r.json()
    assert [p["name"] for p in body["added"]] == ["Genuinely New"]
    assert body["skipped"] == [
        {"name": "Someone Elses Name", "reason": 'same sequence as "Torchlight"'},
    ]


def test_import_skips_patterns_matching_a_builtin(client):
    r = client.post("/api/patterns/import", json={
        "patterns": [{"name": "Definitely Mine", "sequence": "mmnmmommommnonmmonqnmmo"}],
    })
    body = r.json()
    assert body["added"] == []
    assert "Quake" in body["skipped"][0]["reason"]


def test_import_renames_rather_than_clobbering_on_a_name_clash(client):
    make(client, "Torchlight", "mmnmmo")
    r = client.post("/api/patterns/import", json={
        "patterns": [{"name": "Torchlight", "sequence": "zzzaaa"}],
    })
    assert r.json()["added"][0]["name"] == "Torchlight (2)"
    names = sorted(p["name"] for p in client.get("/api/patterns").json()["custom"])
    assert names == ["Torchlight", "Torchlight (2)"]


def test_a_pack_of_only_duplicates_changes_nothing(client, app_modules):
    make(client, "Torchlight", "mmnmmo")
    before = app_modules.config_store.load()["custom_patterns"]
    r = client.post("/api/patterns/import", json={
        "patterns": [{"name": "Copy", "sequence": "mmnmmo"}],
    })
    assert r.json()["added"] == []
    assert app_modules.config_store.load()["custom_patterns"] == before


def test_import_rejects_a_bad_file_with_a_useful_message(client):
    r = client.post("/api/patterns/import", json={
        "patterns": [{"name": "Broken", "sequence": "abc123"}],
    })
    assert r.status_code == 400
    assert "Broken" in r.json()["detail"]
    assert "a-z" in r.json()["detail"]


def test_import_rejects_a_file_that_is_not_a_pack(client):
    r = client.post("/api/patterns/import", json={"hello": "world"})
    assert r.status_code == 400
    assert "patterns" in r.json()["detail"]
    assert client.get("/api/patterns").json()["custom"] == []


def test_sharing_endpoints_are_behind_the_console_password(client):
    client.put("/api/auth/password", json={"new_password": "quaddamage"})
    client.cookies.clear()
    assert client.get("/api/patterns/export").status_code == 401
    assert client.post("/api/patterns/import", json={"patterns": []}).status_code == 401


# ---------- a pattern's own speed ----------

def test_starting_without_a_speed_uses_the_patterns_own(client, bridge):
    configure(client)
    # Shadow Warrior's paper lantern is written for 4Hz, not the old blanket 10.
    client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": "sw_lantern"})
    assert client.get("/api/status").json()["lights"]["1"]["hz"] == 4
    client.post("/api/flicker/stop", json={})


def test_an_explicit_speed_still_wins(client, bridge):
    configure(client)
    client.post("/api/flicker/start",
                json={"light_ids": ["1"], "pattern_id": "sw_lantern", "hz": 18})
    assert client.get("/api/status").json()["lights"]["1"]["hz"] == 18
    client.post("/api/flicker/stop", json={})


def test_quake_styles_keep_their_engine_rate(client, bridge):
    configure(client)
    client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": "fast_strobe"})
    assert client.get("/api/status").json()["lights"]["1"]["hz"] == 10
    client.post("/api/flicker/stop", json={})


def test_switching_pattern_live_adopts_the_new_rate(client, bridge):
    configure(client)
    client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": "sw_lantern"})
    assert client.get("/api/status").json()["lights"]["1"]["hz"] == 4
    client.post("/api/flicker/update",
                json={"light_ids": ["1"], "pattern_id": "unreal_flicker"})
    status = client.get("/api/status").json()["lights"]["1"]
    assert (status["pattern_id"], status["hz"]) == ("unreal_flicker", 16)
    client.post("/api/flicker/stop", json={})


def test_switching_pattern_live_with_an_explicit_speed_keeps_it(client, bridge):
    configure(client)
    client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": "steady"})
    client.post("/api/flicker/update",
                json={"light_ids": ["1"], "pattern_id": "unreal_flicker", "hz": 7})
    assert client.get("/api/status").json()["lights"]["1"]["hz"] == 7
    client.post("/api/flicker/stop", json={})


def test_custom_patterns_carry_a_speed(client, bridge):
    configure(client)
    made = client.post("/api/patterns",
                       json={"name": "Fast One", "sequence": "azaz", "hz": 17}).json()
    assert made["hz"] == 17
    client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": made["id"]})
    assert client.get("/api/status").json()["lights"]["1"]["hz"] == 17
    client.post("/api/flicker/stop", json={})


def test_custom_patterns_default_to_ten(client):
    made = client.post("/api/patterns", json={"name": "Plain", "sequence": "azaz"}).json()
    assert made["hz"] == 10.0


def test_a_shared_pattern_keeps_its_speed(client):
    client.post("/api/patterns", json={"name": "Fast One", "sequence": "azaz", "hz": 17})
    pack = client.get("/api/patterns/export").json()
    assert pack["patterns"][0]["hz"] == 17
    r = client.post("/api/patterns/import", json={
        "patterns": [{"name": "From A Friend", "sequence": "zzaa", "hz": 6}],
    })
    assert r.json()["added"][0]["hz"] == 6


def test_patterns_endpoint_reports_every_rate(client):
    body = client.get("/api/patterns").json()
    assert all("hz" in p for p in body["builtin"])
    rates = {p["id"]: p["hz"] for p in body["builtin"]}
    assert rates["sw_lantern"] == 4 and rates["unreal_flicker"] == 16


def test_status_carries_the_clock_the_loops_run_on(client, bridge):
    configure(client)
    client.put("/api/settings", json={"max_commands_per_second": 10, "restore_on_stop": True})
    client.post("/api/flicker/start",
                json={"light_ids": ["1", "2", "3"], "pattern_id": "flicker_a", "hz": 10})
    status = client.get("/api/status").json()
    light = status["lights"]["1"]
    # epoch and now share a clock, so a browser can work out which frame is due
    assert light["epoch"] <= status["now"]
    assert status["now"] - light["epoch"] < 60
    # and the rate to advance it at is the one the light really gets
    assert light["effective_hz"] < light["hz"]
    client.post("/api/flicker/stop", json={})


def test_starting_adopts_the_patterns_whole_framing(client, bridge):
    configure(client)
    # The paper lantern is written mellow: 4Hz, a narrow window, soft steps.
    client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": "sw_lantern"})
    st = client.get("/api/status").json()["lights"]["1"]
    assert (st["hz"], st["min_bri"], st["max_bri"], st["transition_ms"]) == (4, 60, 200, 300)
    client.post("/api/flicker/stop", json={})


def test_engine_styles_keep_the_full_range_and_hard_steps(client, bridge):
    configure(client)
    client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": "flicker_a"})
    st = client.get("/api/status").json()["lights"]["1"]
    assert (st["min_bri"], st["max_bri"], st["transition_ms"]) == (1, 254, 0)
    client.post("/api/flicker/stop", json={})


def test_explicit_framing_still_overrides_the_pattern(client, bridge):
    configure(client)
    client.post("/api/flicker/start", json={
        "light_ids": ["1"], "pattern_id": "sw_lantern",
        "min_bri": 10, "max_bri": 100, "transition_ms": 0,
    })
    st = client.get("/api/status").json()["lights"]["1"]
    assert (st["min_bri"], st["max_bri"], st["transition_ms"]) == (10, 100, 0)
    assert st["hz"] == 4          # unspecified, so still the pattern's
    client.post("/api/flicker/stop", json={})


def test_a_supplied_bound_that_crosses_the_patterns_own_is_refused(client, bridge):
    configure(client)
    # sw_lantern tops out at 200; asking for a floor above that is incoherent
    r = client.post("/api/flicker/start", json={
        "light_ids": ["1"], "pattern_id": "sw_lantern", "min_bri": 240,
    })
    assert r.status_code == 422
    assert "min_bri" in r.json()["detail"]


def test_switching_pattern_live_adopts_the_new_framing(client, bridge):
    configure(client)
    client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": "flicker_a"})
    client.post("/api/flicker/update", json={"light_ids": ["1"], "pattern_id": "sw_lantern"})
    st = client.get("/api/status").json()["lights"]["1"]
    assert (st["hz"], st["min_bri"], st["max_bri"], st["transition_ms"]) == (4, 60, 200, 300)
    client.post("/api/flicker/stop", json={})


def test_custom_patterns_carry_their_whole_framing(client, bridge):
    configure(client)
    made = client.post("/api/patterns", json={
        "name": "Soft One", "sequence": "azaz",
        "hz": 6, "min_bri": 30, "max_bri": 180, "transition_ms": 250,
    }).json()
    assert (made["hz"], made["min_bri"], made["max_bri"], made["transition_ms"]) == (6, 30, 180, 250)
    client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": made["id"]})
    st = client.get("/api/status").json()["lights"]["1"]
    assert (st["min_bri"], st["max_bri"], st["transition_ms"]) == (30, 180, 250)
    client.post("/api/flicker/stop", json={})


def test_a_custom_pattern_cannot_be_saved_with_an_inverted_window(client):
    r = client.post("/api/patterns", json={
        "name": "Backwards", "sequence": "azaz", "min_bri": 200, "max_bri": 50,
    })
    assert r.status_code == 422


def test_framing_survives_being_shared(client):
    client.post("/api/patterns", json={
        "name": "Soft One", "sequence": "azaz",
        "hz": 6, "min_bri": 30, "max_bri": 180, "transition_ms": 250,
    })
    pack = client.get("/api/patterns/export").json()
    entry = pack["patterns"][0]
    assert (entry["hz"], entry["min_bri"], entry["max_bri"], entry["transition_ms"]) \
        == (6, 30, 180, 250)
    r = client.post("/api/patterns/import", json={"patterns": [
        {"name": "From Afar", "sequence": "zzaa", "min_bri": 90, "transition_ms": 400},
    ]})
    added = r.json()["added"][0]
    assert (added["min_bri"], added["max_bri"], added["transition_ms"]) == (90, 254, 400)


# ---------- a pattern's suggested colour ----------

def test_starting_takes_the_patterns_colour_when_none_is_given(client, bridge):
    configure(client)
    client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": "blood_torch"})
    st = client.get("/api/status").json()["lights"]["1"]
    assert (st["hue"], st["sat"]) == (6000, 225)
    client.post("/api/flicker/stop", json={})


def test_an_explicit_colour_beats_the_patterns(client, bridge):
    configure(client)
    client.post("/api/flicker/start", json={
        "light_ids": ["1"], "pattern_id": "blood_torch", "hue": 40000, "sat": 100,
    })
    st = client.get("/api/status").json()["lights"]["1"]
    assert (st["hue"], st["sat"]) == (40000, 100)
    client.post("/api/flicker/stop", json={})


def test_an_explicit_null_colour_means_leave_the_bulb_alone(client, bridge):
    # This is how the UI says "Set color is unticked" — it must not be read as
    # "no preference", or the pattern's colour would override the user.
    configure(client)
    client.post("/api/flicker/start", json={
        "light_ids": ["1"], "pattern_id": "blood_torch", "hue": None, "sat": None,
    })
    st = client.get("/api/status").json()["lights"]["1"]
    assert st["hue"] is None and st["sat"] is None
    client.post("/api/flicker/stop", json={})


def test_a_colourless_pattern_leaves_the_bulb_alone(client, bridge):
    configure(client)
    client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": "doom3_strobe"})
    st = client.get("/api/status").json()["lights"]["1"]
    assert st["hue"] is None and st["sat"] is None
    client.post("/api/flicker/stop", json={})


def test_switching_pattern_live_never_recolours_on_its_own(client, bridge):
    # Brightness and timing re-frame on a pattern switch, but reaching over and
    # recolouring a running bulb without being asked would be a surprise.
    configure(client)
    client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": "doom3_strobe"})
    client.post("/api/flicker/update", json={"light_ids": ["1"], "pattern_id": "blood_torch"})
    st = client.get("/api/status").json()["lights"]["1"]
    assert st["pattern_id"] == "blood_torch"
    assert st["min_bri"] == 40                 # framing did follow
    assert st["hue"] is None                   # colour did not
    client.post("/api/flicker/stop", json={})


def test_custom_patterns_can_save_a_colour(client, bridge):
    configure(client)
    made = client.post("/api/patterns", json={
        "name": "Ember", "sequence": "azaz", "hue": 5000, "sat": 240,
    }).json()
    assert (made["hue"], made["sat"]) == (5000, 240)
    client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": made["id"]})
    st = client.get("/api/status").json()["lights"]["1"]
    assert (st["hue"], st["sat"]) == (5000, 240)
    client.post("/api/flicker/stop", json={})


def test_half_a_colour_is_refused_on_a_custom_pattern(client):
    r = client.post("/api/patterns", json={"name": "x", "sequence": "az", "hue": 100})
    assert r.status_code == 422


def test_colour_survives_being_shared(client):
    client.post("/api/patterns", json={
        "name": "Ember", "sequence": "azaz", "hue": 5000, "sat": 240,
    })
    entry = client.get("/api/patterns/export").json()["patterns"][0]
    assert (entry["hue"], entry["sat"]) == (5000, 240)
    added = client.post("/api/patterns/import", json={"patterns": [
        {"name": "Cold", "sequence": "zzaa", "hue": 44000, "sat": 200},
    ]}).json()["added"][0]
    assert (added["hue"], added["sat"]) == (44000, 200)


def test_update_cannot_invert_the_window_with_one_bound(client, bridge):
    # Regression: supplying only max_bri was validated against nothing, so it
    # could land below the min already running and play the waveform upside
    # down with a 200 OK.
    configure(client)
    client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": "sw_lantern"})
    running = client.get("/api/status").json()["lights"]["1"]
    assert (running["min_bri"], running["max_bri"]) == (60, 200)

    r = client.post("/api/flicker/update", json={"light_ids": ["1"], "max_bri": 20})
    assert r.status_code == 422
    assert "60-200" in r.json()["detail"]

    unchanged = client.get("/api/status").json()["lights"]["1"]
    assert (unchanged["min_bri"], unchanged["max_bri"]) == (60, 200)
    client.post("/api/flicker/stop", json={})


def test_update_accepts_one_bound_that_still_fits(client, bridge):
    configure(client)
    client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": "sw_lantern"})
    assert client.post("/api/flicker/update",
                       json={"light_ids": ["1"], "max_bri": 120}).status_code == 200
    st = client.get("/api/status").json()["lights"]["1"]
    assert (st["min_bri"], st["max_bri"]) == (60, 120)
    client.post("/api/flicker/stop", json={})


def test_update_rejects_the_whole_request_not_just_one_light(client, bridge):
    configure(client)
    client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": "flicker_a"})
    client.post("/api/flicker/start", json={"light_ids": ["2"], "pattern_id": "sw_lantern"})
    # light 2 is running 60-200, so this is fine for light 1 and not for 2
    r = client.post("/api/flicker/update", json={"light_ids": ["1", "2"], "max_bri": 30})
    assert r.status_code == 422
    st = client.get("/api/status").json()["lights"]
    assert st["1"]["max_bri"] == 254 and st["2"]["max_bri"] == 200
    client.post("/api/flicker/stop", json={})


def test_changing_one_setting_leaves_the_other_alone(client, app_modules):
    # Regression: the model defaulted restore_on_stop to True, so a caller
    # sending only the rate silently switched restoring back on.
    client.put("/api/settings", json={"max_commands_per_second": 10, "restore_on_stop": False})
    assert client.get("/api/settings").json()["restore_on_stop"] is False

    client.put("/api/settings", json={"max_commands_per_second": 5})
    settings = client.get("/api/settings").json()
    assert settings["max_commands_per_second"] == 5.0
    assert settings["restore_on_stop"] is False
    assert app_modules.engine.restore_on_stop is False

    client.put("/api/settings", json={"restore_on_stop": True})
    settings = client.get("/api/settings").json()
    assert settings["restore_on_stop"] is True
    assert settings["max_commands_per_second"] == 5.0     # rate survived


def test_an_empty_settings_body_changes_nothing(client):
    client.put("/api/settings", json={"max_commands_per_second": 7, "restore_on_stop": False})
    r = client.put("/api/settings", json={})
    assert r.status_code == 200
    assert r.json() == {"max_commands_per_second": 7.0, "restore_on_stop": False}


def test_settings_still_rejects_out_of_range_values(client):
    assert client.put("/api/settings", json={"max_commands_per_second": 0}).status_code == 422
    assert client.put("/api/settings", json={"max_commands_per_second": 99}).status_code == 422


def test_starting_a_group_sizes_the_send_budget_for_it(client, bridge, app_modules):
    # The limiter has to know the batch size before anything is sent, or the
    # snapshot read spends the only token and the group's first round is
    # strung out behind it. The read counts too, hence one more than the
    # number of lights.
    configure(client)
    client.post("/api/flicker/start",
                json={"light_ids": ["1", "2", "3"], "pattern_id": "flicker_a"})
    assert app_modules.engine.limiter.burst == 4

    # and it shrinks back as lights stop, so a single light doesn't sit on a
    # bucket sized for a group
    client.post("/api/flicker/stop", json={"light_ids": ["3"]})
    assert app_modules.engine.limiter.burst == 2
    client.post("/api/flicker/stop", json={})
    assert app_modules.engine.limiter.burst == 1


# ---------- importing the bridge's own rooms ----------

def test_bridge_rooms_and_zones_are_offered(client, bridge):
    configure(client)
    groups = client.get("/api/bridge/groups").json()["groups"]
    by_name = {g["name"]: g for g in groups}
    assert set(by_name) == {"Living room", "Upstairs", "Odds and ends"}
    assert by_name["Living room"]["type"] == "Room"
    assert by_name["Living room"]["class"] == "Living room"
    assert by_name["Living room"]["light_ids"] == ["1", "3"]
    assert by_name["Upstairs"]["type"] == "Zone"


def test_luminaires_and_entertainment_areas_are_not_offered(client, bridge):
    # A Luminaire describes the innards of one fitting and an Entertainment
    # area belongs to the streaming API; neither is a group you'd flicker.
    configure(client)
    names = [g["name"] for g in client.get("/api/bridge/groups").json()["groups"]]
    assert "Ceiling fitting" not in names
    assert "TV area" not in names


def test_rooms_are_listed_before_zones(client, bridge):
    configure(client)
    types = [g["type"] for g in client.get("/api/bridge/groups").json()["groups"]]
    assert types.index("Room") < types.index("Zone")


def test_bridge_groups_need_a_bridge(client):
    assert client.get("/api/bridge/groups").status_code == 400


def test_bridge_groups_surface_a_failure(client, app_modules, monkeypatch):
    configure(client)

    async def boom(self):
        raise RuntimeError("no route to host")

    monkeypatch.setattr(app_modules.HueClient, "get_groups", boom)
    assert client.get("/api/bridge/groups").status_code == 502


def test_a_bridge_room_can_be_saved_as_a_group(client, bridge):
    configure(client)
    room = next(g for g in client.get("/api/bridge/groups").json()["groups"]
                if g["name"] == "Living room")
    made = client.post("/api/groups",
                       json={"name": room["name"], "light_ids": room["light_ids"]}).json()
    assert made["light_ids"] == ["1", "3"]
    # and it drives its members like any other group
    client.post("/api/flicker/start",
                json={"light_ids": made["light_ids"], "pattern_id": "flicker_a"})
    status = client.get("/api/status").json()["lights"]
    assert status["1"]["running"] and status["3"]["running"]
    client.post("/api/flicker/stop", json={})


def test_importing_is_gated_by_the_console_password(client):
    client.put("/api/auth/password", json={"new_password": "quaddamage"})
    client.cookies.clear()
    assert client.get("/api/bridge/groups").status_code == 401


def test_bridge_groups_report_what_was_filtered_out(client, bridge):
    # A bridge with nothing set up and one whose groups are all luminaires are
    # different problems, and the UI has to be able to tell them apart.
    configure(client)
    body = client.get("/api/bridge/groups").json()
    assert body["total"] == 5
    assert body["seen"] == {"Room": 1, "Zone": 1, "Luminaire": 1,
                            "Entertainment": 1, "LightGroup": 1}
    assert len(body["groups"]) == 3


def test_a_bridge_with_no_groups_says_so(client, bridge, app_modules, monkeypatch):
    configure(client)

    async def none(self):
        return {}

    monkeypatch.setattr(app_modules.HueClient, "get_groups", none)
    body = client.get("/api/bridge/groups").json()
    assert body == {"groups": [], "seen": {}, "total": 0}


def test_a_group_of_an_unknown_type_is_counted_not_dropped(client, bridge, app_modules,
                                                           monkeypatch):
    configure(client)

    async def odd(self):
        return {"9": {"name": "Mystery", "lights": ["1"]}}

    monkeypatch.setattr(app_modules.HueClient, "get_groups", odd)
    body = client.get("/api/bridge/groups").json()
    assert body["total"] == 1
    assert body["seen"] == {"unknown": 1}
    assert body["groups"] == []
