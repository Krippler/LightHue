import json

import pytest

from app.patterns import BUILTIN_PATTERNS


def configure(client):
    r = client.post("/api/bridge/set", json={"bridge_ip": "10.0.0.5", "api_key": "k"})
    assert r.status_code == 200


def diagnostics(client) -> dict:
    """Read the streaming diagnostics, turning them on first.

    They ship off, so every caller has to ask. Going through one helper keeps
    that fact visible at each call site instead of hiding it in a fixture.
    """
    r = client.put("/api/settings", json={"diagnostics_enabled": True})
    assert r.status_code == 200
    r = client.get("/api/stream/diagnostics")
    assert r.status_code == 200, r.text
    return r.json()


def test_index_and_unconfigured_state(client):
    assert client.get("/").status_code == 200
    assert client.get("/api/bridge").json() == {
        "bridge_ip": None, "configured": False, "can_stream": False}
    assert client.get("/api/lights").status_code == 400
    status = client.get("/api/status").json()
    assert status["lights"] == {} and status["snapshots"] == {}
    assert isinstance(status["now"], float)


def test_lights_are_listed_sorted_by_name(client, bridge):
    configure(client)
    lights = client.get("/api/lights").json()["lights"]
    assert [x["name"] for x in lights] == [
        "Armory Strip", "Nailgun Nook", "Quad Socket", "Rocket Alcove",
        "Slipgate Sconce",
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
def test_lights_expose_their_current_colour(client, bridge):
    configure(client)
    lights = {x["name"]: x for x in client.get("/api/lights").json()["lights"]}
    sconce = lights["Slipgate Sconce"]
    assert (sconce["hue"], sconce["sat"], sconce["bri"]) == (8000, 140, 180)
    assert sconce["colormode"] == "hs"
    assert sconce["has_color"] is True
    assert lights["Nailgun Nook"]["has_color"] is False   # white-only bulb


def test_lights_report_whether_they_can_be_dimmed(client, bridge):
    """A plug has no brightness, and a flicker frame is a brightness.

    The UI needs this to decide whether to offer the controls at all: the
    bridge accepts a PUT carrying a bri the device lacks and declines just
    that key, so nothing downstream would notice the setting being dropped.
    """
    configure(client)
    lights = {x["name"]: x for x in client.get("/api/lights").json()["lights"]}
    assert lights["Slipgate Sconce"]["dimmable"] is True
    assert lights["Nailgun Nook"]["dimmable"] is True     # white, but dimmable
    assert lights["Quad Socket"]["dimmable"] is False     # a plug
    assert lights["Quad Socket"]["has_color"] is False


def test_a_plug_is_refused_a_flicker_by_name(client, bridge):
    """Refused at the API rather than left to run against a device that ignores it.

    Before this, starting a flicker on a plug turned it on and then sent it a
    brightness ten times a second forever. Each send was answered 200, so the
    loop never gave up — and every one of those frames came out of the same
    rate budget the real bulbs flicker from.
    """
    configure(client)
    r = client.post("/api/flicker/start", json={"light_ids": ["5"], "pattern_id": "flicker_a"})
    assert r.status_code == 422
    assert "Quad Socket" in r.json()["detail"]
    assert client.get("/api/status").json()["lights"] == {}
    # And nothing was sent to it.
    assert bridge["puts"] == []


def test_a_plug_in_a_group_stops_the_whole_start(client, bridge):
    """All-or-nothing, and the message names the offender.

    Starting the bulbs and quietly dropping the plug would leave the caller
    believing the plug is flickering.
    """
    configure(client)
    r = client.post("/api/flicker/start",
                    json={"light_ids": ["1", "5"], "pattern_id": "flicker_a"})
    assert r.status_code == 422
    assert "Quad Socket" in r.json()["detail"]
    assert client.get("/api/status").json()["lights"] == {}


def test_dimmable_lights_still_start(client, bridge):
    """The guard must not cost a working light its flicker."""
    configure(client)
    r = client.post("/api/flicker/start",
                    json={"light_ids": ["1", "3"], "pattern_id": "flicker_a"})
    assert r.status_code == 200
    assert set(client.get("/api/status").json()["lights"]) == {"1", "3"}
    client.post("/api/flicker/stop", json={})


def test_a_start_is_not_blocked_when_the_bridge_cannot_be_read(client, bridge, monkeypatch,
                                                              app_modules):
    """A failed read skips the check rather than turning into a refusal.

    The check exists to stop a pointless flicker, not to add a new way for a
    momentary bridge hiccup to refuse a good one.
    """
    configure(client)

    async def unreadable():
        return None            # what the helper returns when the GET failed

    monkeypatch.setattr(app_modules, "_read_lights_for_start", unreadable)
    r = client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": "flicker_a"})
    assert r.status_code == 200
    client.post("/api/flicker/stop", json={})


def test_starting_a_flicker_still_costs_one_read(client, bridge):
    """The capability check reuses the snapshot's GET rather than adding one."""
    configure(client)
    before = bridge["light_reads"]
    client.post("/api/flicker/start", json={"light_ids": ["1"], "pattern_id": "flicker_a"})
    client.post("/api/flicker/stop", json={})
    assert bridge["light_reads"] - before == 1


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
    assert r.json() == {"max_commands_per_second": 7.0, "restore_on_stop": False,
                        "stream_settle_ms": 1500, "diagnostics_enabled": False}


def test_the_settle_delay_is_adjustable_and_can_be_turned_off(client):
    """The right pause between arming an area and handshaking is a property of
    one bridge on one network, so it is a setting rather than a constant."""
    assert client.get("/api/settings").json()["stream_settle_ms"] == 1500
    assert client.put("/api/settings", json={"stream_settle_ms": 0}).json()[
        "stream_settle_ms"] == 0
    assert client.put("/api/settings", json={"stream_settle_ms": 3000}).json()[
        "stream_settle_ms"] == 3000
    assert client.put("/api/settings", json={"stream_settle_ms": -1}).status_code == 422
    assert client.put("/api/settings", json={"stream_settle_ms": 60000}).status_code == 422
    # And it survives a change to something else.
    client.put("/api/settings", json={"restore_on_stop": False})
    assert client.get("/api/settings").json()["stream_settle_ms"] == 3000


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
def test_importing_is_gated_by_the_console_password(client):
    client.put("/api/auth/password", json={"new_password": "quaddamage"})
    client.cookies.clear()
    assert client.get("/api/bridge/groups").status_code == 401


def test_bridge_groups_report_what_was_filtered_out(client, bridge):
    # A bridge with nothing set up and one whose groups are all luminaires are
    # different problems, and the UI has to be able to tell them apart.
    configure(client)
    body = client.get("/api/bridge/groups").json()
    assert body["total"] == 6
    assert body["seen"] == {"Room": 1, "Zone": 1, "Luminaire": 1,
                            "Entertainment": 2, "LightGroup": 1}
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


# ---------- keeping the browser off a stale build ----------

def test_asset_urls_carry_a_version(client, app_modules):
    # An unversioned /static/app.js can sit in a browser or proxy cache for
    # ever, so an updated console keeps running the previous build and nothing
    # you change appears to take effect.
    html = client.get("/").text
    version = app_modules.asset_version()
    assert f'/static/app.js?v={version}' in html
    assert f'/static/style.css?v={version}' in html
    assert "__BUILD__" not in html


def test_the_index_itself_is_never_cached(client):
    # If the HTML is cached the versioned URLs inside it never arrive.
    cache = client.get("/").headers["cache-control"]
    assert "no-store" in cache


def test_the_running_build_is_visible_and_reported(client, app_modules):
    version = app_modules.asset_version()
    assert client.get("/api/version").json() == {"assets": version}
    assert f"build {version}" in client.get("/").text


def test_the_version_changes_when_the_ui_does(client, app_modules, tmp_path, monkeypatch):
    before = app_modules.asset_version()
    original = app_modules.STATIC_DIR / "app.js"
    text = original.read_text()
    try:
        original.write_text(text + "\n// a change\n")
        assert app_modules.asset_version() != before
    finally:
        original.write_text(text)
    assert app_modules.asset_version() == before


def test_the_versioned_asset_is_actually_served(client, app_modules):
    version = app_modules.asset_version()
    r = client.get(f"/static/app.js?v={version}")
    assert r.status_code == 200
    assert "bridgeGroupsBtn" in r.text


def test_raising_the_send_rate_reaches_the_running_lights(client, bridge, app_modules, monkeypatch):
    """A new ceiling has to be pushed, not just stored.

    Each card reports the share its light is actually getting, and nothing
    pushes a status between ticks — so a rate change that isn't broadcast looks
    like it did nothing until something else starts or stops. Asserted through
    a recorded call rather than a socket read: waiting on a push that never
    comes hangs the suite instead of failing it.
    """
    configure(client)
    client.post("/api/flicker/start", json={
        "light_ids": ["1", "2", "3", "4"], "pattern_id": "flicker_a", "hz": 10})

    def shares():
        lights = client.get("/api/status").json()["lights"]
        return {v["effective_hz"] for v in lights.values() if v.get("running")}

    assert shares() == {2.0}                      # 10 * 0.8 / 4

    pushes = []
    monkeypatch.setattr(app_modules, "_broadcast_status_soon", lambda: pushes.append(1))
    client.put("/api/settings", json={"max_commands_per_second": 20})

    assert shares() == {4.0}                      # 20 * 0.8 / 4
    assert pushes, "raising the rate has to push a status, or the cards stay stale"


def test_pairing_asks_for_a_streaming_key_and_reports_getting_one(client, bridge, app_modules):
    r = client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    assert r.status_code == 200
    assert r.json()["can_stream"] is True
    assert client.get("/api/bridge").json()["can_stream"] is True
    # The key itself is never handed back out.
    assert "client_key" not in r.json()
    assert app_modules.config_store.load()["client_key"] == "stub-client-key"


def test_a_bridge_too_old_to_issue_one_still_pairs(client, bridge, app_modules):
    bridge["omit_client_key"] = True
    r = client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    assert r.status_code == 200
    assert r.json()["configured"] is True and r.json()["can_stream"] is False


def test_resaving_the_same_credentials_leaves_the_client_key_alone(client, app_modules):
    """Moving a bridge to a new address must not cost it the streaming key."""
    client.post("/api/bridge/set", json={
        "bridge_ip": "10.0.0.5", "api_key": "k", "client_key": "00" * 16})
    assert client.get("/api/bridge").json()["can_stream"] is True
    client.post("/api/bridge/set", json={"bridge_ip": "10.0.0.9", "api_key": "k"})
    assert client.get("/api/bridge").json()["can_stream"] is True
    client.post("/api/bridge/set", json={"bridge_ip": "10.0.0.9"})
    assert client.get("/api/bridge").json()["can_stream"] is True


def test_a_new_api_key_drops_the_client_key_that_did_not_come_with_it(client):
    """The two are one credential, not two settings.

    Streaming offers the api key as its DTLS identity and the client key as the
    pre-shared key. Keeping the old client key beside a new api key builds an
    offer out of halves that never belonged together, and a bridge that simply
    ignores it is indistinguishable from a dead network.
    """
    client.post("/api/bridge/set", json={
        "bridge_ip": "10.0.0.5", "api_key": "k", "client_key": "00" * 16})
    r = client.post("/api/bridge/set", json={"bridge_ip": "10.0.0.5", "api_key": "k2"})
    assert r.json()["can_stream"] is False
    assert client.get("/api/bridge").json()["can_stream"] is False
    # Supplying both together is how you replace them, and that still works.
    client.post("/api/bridge/set", json={
        "bridge_ip": "10.0.0.5", "api_key": "k3", "client_key": "11" * 16})
    assert client.get("/api/bridge").json()["can_stream"] is True


def provenance(client):
    return diagnostics(client)["keys_from_same_pairing"]


def test_key_provenance_separates_not_knowing_from_knowing_it_is_wrong(client, bridge):
    """Three answers, because there are three situations.

    Reporting hand-entered keys as "no" would make every console configured
    before this was tracked look like it had a fault to chase — which is the
    opposite of what a diagnostic is for.
    """
    # Nothing configured, and nothing was ever watched being issued.
    assert provenance(client) == "unknown"

    # Typed in by hand: possibly a matched set from elsewhere, possibly not.
    client.post("/api/bridge/set", json={
        "bridge_ip": "10.0.0.5", "api_key": "k", "client_key": "00" * 16})
    assert provenance(client) == "unknown"

    # A pairing issues both at once, which is the only way to actually know.
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    assert provenance(client) == "yes"

    # Replacing one alone splits them, and that much we can state outright.
    client.post("/api/bridge/set", json={"bridge_ip": "10.0.0.7", "api_key": "other"})
    assert provenance(client) == "no"


def test_pairing_on_firmware_that_issues_no_streaming_key_is_not_a_match(client, bridge):
    """Nothing to pair the api key with means nothing to vouch for."""
    bridge["omit_client_key"] = True
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    assert diagnostics(client)["can_stream"] is False
    assert provenance(client) == "no"


# ---------- entertainment areas ----------

def test_entertainment_areas_are_offered_separately_from_rooms(client, bridge):
    configure(client)
    rooms = client.get("/api/bridge/groups").json()["groups"]
    assert "Game room" not in [g["name"] for g in rooms]   # not a flicker group

    body = client.get("/api/stream/areas").json()
    assert [a["name"] for a in body["areas"]] == ["Game room", "TV area"]
    game = next(a for a in body["areas"] if a["name"] == "Game room")
    assert game["light_ids"] == ["1", "2", "3", "4"]
    assert game["too_many_lights"] is False
    assert game["in_use_by_someone_else"] is False


def test_an_area_someone_else_is_streaming_to_is_flagged(client, bridge):
    configure(client)
    areas = client.get("/api/stream/areas").json()["areas"]
    busy = next(a for a in areas if a["name"] == "TV area")
    assert busy["in_use_by_someone_else"] is True
    assert busy["claimed_by_us"] is False


def test_a_claim_this_console_left_reads_as_ours_not_a_conflict(client, bridge):
    configure(client)                                   # stores api_key "k"
    bridge["groups"]["6"]["stream"] = {"active": True, "owner": "k"}
    area = next(a for a in client.get("/api/stream/areas").json()["areas"]
                if a["name"] == "Game room")
    assert area["claimed_by_us"] is True
    assert area["in_use_by_someone_else"] is False


def test_the_areas_listing_never_returns_the_api_key(client, bridge):
    """Ownership is worked out server-side precisely so the key never has to
    be handed to the browser to compare against."""
    secret = "Xk7Qv2zPmNb4Ls9Wd1Tr"
    client.post("/api/bridge/set", json={"bridge_ip": "10.0.0.5", "api_key": secret})
    bridge["groups"]["6"]["stream"] = {"active": True, "owner": secret}
    body = client.get("/api/stream/areas").json()
    assert secret not in str(body)
    area = next(a for a in body["areas"] if a["name"] == "Game room")
    assert area["claimed_by_us"] is True


def test_areas_report_whether_this_console_could_stream_at_all(client, bridge):
    configure(client)                               # no client key stored
    assert client.get("/api/stream/areas").json()["can_stream"] is False
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    assert client.get("/api/stream/areas").json()["can_stream"] is True


def test_handing_an_area_to_the_stream_and_back(client, bridge, app_modules):
    configure(client)
    hue_client = app_modules.get_client()
    import asyncio
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        hue_client.set_stream("6", True))
    assert bridge["stream_calls"][-1] == ("6", True)
    assert bridge["groups"]["6"]["stream"]["active"] is True


# ---------- streaming ----------

def stream_body(**kw):
    return {"area_id": "6", "pattern_id": "flicker_a", **kw}


def test_arming_clears_a_hold_the_bridge_is_already_reporting(client, bridge,
                                                              app_modules, monkeypatch):
    """A configuration the bridge already calls active takes a "start" as a no-op.

    That is what a session killed mid-stream leaves behind, and it is invisible
    from the REST side: the call succeeds, and the port stays bound to something
    that will never answer a handshake. So arming stops it first, every time.
    """
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    bridge["v2"]["configurations"][0]["status"] = "active"

    monkeypatch.setattr(app_modules.stream_engine, "start",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            app_modules.StreamError("no route to the bridge")))
    client.post("/api/stream/start", json=stream_body())
    assert bridge["v2"]["armed"][:2] == ["stop", "start"]


def test_the_arm_step_records_what_the_bridge_said_not_what_we_asked(
        client, bridge, app_modules, monkeypatch):
    """A 200 on the start call is not evidence the stream came up.

    Reading the configuration back is the only thing that distinguishes an area
    that armed from one where the bridge accepted the word and did nothing —
    and those two look identical from the socket.
    """
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    monkeypatch.setattr(app_modules.stream_engine, "start",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            app_modules.StreamError("no route to the bridge")))
    client.post("/api/stream/start", json=stream_body())

    steps = diagnostics(client)["last_attempt"]["steps"]
    armed = next(s for s in steps if s["step"] == "armed")
    assert armed["over"] == "v2"
    assert armed["bridge_says"]["status"] == "active"


def test_each_client_gets_a_freshly_armed_area(client, bridge, app_modules,
                                               monkeypatch):
    """The bridge stops listening ~10s after arming an area nobody connects to.

    Two clients behind one claim means the second speaks into a window the first
    already spent timing out — so each attempt names one client and re-arms
    first. This is the regression that broke streaming: the clients used to fall
    through to each other inside a single six-second-each connect.
    """
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    tried = []

    def refuse(*a, transport=None, **kw):
        tried.append(transport)
        raise app_modules.StreamError("timed out")

    monkeypatch.setattr(app_modules.stream_engine, "start", refuse)
    client.post("/api/stream/start", json=stream_body())

    assert tried == list(app_modules.STREAM_TRANSPORTS)
    assert None not in tried, "an unnamed transport would fall through internally"
    # One arm before the first client, one before each of the rest.
    steps = diagnostics(client)["last_attempt"]["steps"]
    arms = [s for s in steps if s["step"] in ("armed", "re-armed")]
    assert len(arms) == len(tried)


def test_a_release_that_did_not_take_is_said_out_loud(client, bridge, app_modules,
                                                     monkeypatch):
    """An area the bridge keeps holding blocks the Hue app too, so silence here
    costs the user their lights with nothing on screen explaining why."""
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    monkeypatch.setattr(app_modules.stream_engine, "start",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            app_modules.StreamError("no route to the bridge")))

    # A bridge that takes the release call and goes on streaming anyway.
    original = app_modules.HueClient.set_stream

    async def ignore_release(self, group_id, active):
        if not active:
            return [{"success": {}}]
        return await original(self, group_id, active)

    monkeypatch.setattr(app_modules.HueClient, "set_stream", ignore_release)
    monkeypatch.setattr(app_modules, "_arm_v2", lambda *a, **kw: _none())

    r = client.post("/api/stream/start", json=stream_body())
    assert r.status_code == 502
    assert "still holding area 6" in r.json()["detail"]
    steps = diagnostics(client)["last_attempt"]["steps"]
    assert any(s["step"] == "release-failed" for s in steps)


async def _none():
    return None


def test_the_port_probe_note_reads_the_bridge_not_our_engine(client, bridge):
    """An area stranded by a failed attempt is the case worth naming, and the
    engine knows nothing about it — it is not running, which is the point."""
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})

    # Area 4 is streaming, but to Hue Sync — working as intended, not a fault.
    note = diagnostics(client)["udp_to_stream_port"]["note"]
    assert "something else is streaming to area 4" in note
    assert "stranded" not in note

    # An area this console claimed and never released is the one worth naming.
    bridge["groups"]["6"]["stream"] = {"active": True, "owner": "stub-key"}
    note = diagnostics(client)["udp_to_stream_port"]["note"]
    assert "this console is holding area 6" in note and "stranded" in note

    bridge["groups"]["4"]["stream"] = {"active": False, "owner": None}
    bridge["groups"]["6"]["stream"] = {"active": False, "owner": None}
    assert "no area claimed" in diagnostics(client)["udp_to_stream_port"]["note"]


def test_health_answers_before_login(client, app_modules):
    """Docker's HEALTHCHECK has no session and cannot be given one.

    Gated, it would report every password-protected console as unhealthy and
    restart it forever.
    """
    assert client.put("/api/auth/password",
                      json={"new_password": "hunter2"}).status_code == 200
    client.cookies.clear()                       # what the healthcheck looks like
    assert client.get("/api/lights").status_code == 401     # everything else is shut
    assert client.get("/api/status").status_code == 401
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_health_describes_the_process_and_nothing_else(client, bridge):
    """It answers before login, so it must not describe the home it sits in."""
    configure(client)
    body = client.get("/api/health").json()
    assert set(body) == {"ok", "uptime_s", "loop_lag_ms"}
    assert body["ok"] is True
    assert body["uptime_s"] >= 0
    assert body["loop_lag_ms"] >= 0
    # Nothing that would tell a stranger about the bridge or the lights.
    blob = json.dumps(body)
    for leak in ("10.0.0.5", "stub-key", "k", "Slipgate", "bridge"):
        assert leak not in blob or leak == "k"     # "k" would only match a key name


def test_health_turns_503_when_the_loop_falls_behind(client, app_modules,
                                                      monkeypatch):
    """The whole point: a wedged loop is alive, so no restart policy sees it.

    The heartbeat is what turns that into something Docker can act on, so a
    stale beat has to become a non-200.
    """
    import time as _time

    # A heartbeat that last ticked well over the threshold ago.
    stale = _time.monotonic() - (app_modules._UNHEALTHY_AFTER_SECONDS
                                 + app_modules._HEARTBEAT_SECONDS + 5)
    monkeypatch.setattr(app_modules, "_last_beat", stale)
    r = client.get("/api/health")
    assert r.status_code == 503
    assert r.json()["ok"] is False
    assert r.json()["loop_lag_ms"] >= app_modules._UNHEALTHY_AFTER_SECONDS * 1000


def test_health_tolerates_one_missed_beat(client, app_modules, monkeypatch):
    """A busy moment is not a wedge, or the container restarts on a hiccup."""
    import time as _time

    late = _time.monotonic() - (app_modules._HEARTBEAT_SECONDS + 1.0)
    monkeypatch.setattr(app_modules, "_last_beat", late)
    assert client.get("/api/health").status_code == 200


def test_diagnostics_are_off_until_asked_for(client, bridge):
    """The most revealing thing this console can say, so it ships off.

    Not a secret in itself — the API key is redacted either way — but it
    describes a bridge, its areas and lights, and the local network, and the
    console ships with no password.
    """
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    assert client.get("/api/settings").json()["diagnostics_enabled"] is False

    r = client.get("/api/stream/diagnostics")
    assert r.status_code == 403
    assert "Settings" in r.json()["detail"]

    client.put("/api/settings", json={"diagnostics_enabled": True})
    assert client.get("/api/stream/diagnostics").status_code == 200

    # And back off again — a switch that only goes one way is not a switch.
    client.put("/api/settings", json={"diagnostics_enabled": False})
    assert client.get("/api/stream/diagnostics").status_code == 403


def test_turning_diagnostics_on_does_not_disturb_the_other_settings(client):
    """One setting at a time: the request model leaves out what it is not sent."""
    client.put("/api/settings", json={"max_commands_per_second": 7.0,
                                      "stream_settle_ms": 0,
                                      "restore_on_stop": False})
    settings = client.put("/api/settings",
                          json={"diagnostics_enabled": True}).json()
    assert settings["diagnostics_enabled"] is True
    assert settings["max_commands_per_second"] == 7.0
    assert settings["stream_settle_ms"] == 0
    assert settings["restore_on_stop"] is False


def test_a_failed_start_still_says_why_with_diagnostics_off(client, bridge,
                                                            app_modules, monkeypatch):
    """The gate is on the report, not on the error.

    Someone who pressed Start and got nothing still has to be told what
    happened, or the switch makes the app unfixable for the person using it.
    """
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    monkeypatch.setattr(app_modules.stream_engine, "start",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            app_modules.StreamError("no route to the bridge")))
    r = client.post("/api/stream/start", json=stream_body())
    assert r.status_code == 502
    assert r.json()["detail"], "a failed start has to explain itself"
    assert client.get("/api/settings").json()["diagnostics_enabled"] is False


def test_diagnostics_never_returns_the_bridge_api_key(client, bridge):
    """The whole response, not one field: this output exists to be pasted.

    On the v1 API `stream.owner` is the whitelist username — this console's
    own API key. The endpoint's own docstring invites the user to paste its
    output into a bug report, and the console ships with no password, so the
    key must not appear anywhere in it. The comparison every caller actually
    wanted travels in its place.
    """
    import json

    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    api_key = client.get("/api/bridge").json()
    assert "api_key" not in api_key          # and not from /api/bridge either
    key = "stub-key"                         # what the stub bridge issues

    # Claimed by us, claimed by someone else, and claimed then released: the
    # owner is populated in every state a user would run diagnostics in.
    bridge["groups"]["6"]["stream"] = {"active": True, "owner": key}
    body = diagnostics(client)
    assert key not in json.dumps(body), "the API key reached the diagnostics output"
    assert body["areas"]["6"]["stream"]["owned_by_us"] is True
    assert body["areas"]["6"]["stream"]["owned_by_other"] is False
    # The fields that are safe still travel.
    assert body["areas"]["6"]["stream"]["active"] is True

    bridge["groups"]["6"]["stream"] = {"active": True, "owner": "hue-sync"}
    body = diagnostics(client)
    assert "hue-sync" not in json.dumps(body)
    assert body["areas"]["6"]["stream"]["owned_by_other"] is True
    assert body["areas"]["6"]["stream"]["owned_by_us"] is False


def test_a_start_attempt_does_not_record_the_api_key(client, bridge, app_modules,
                                                     monkeypatch):
    """last_attempt is returned wholesale, and the v1 arm reads the group back.

    That read happens *after* the bridge records us as the owner, so it is the
    path that captures the key even once the area is released and the live
    areas block reads null again. Firmware with no v2 record is what puts the
    arm down that path.
    """
    import json

    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    bridge["v2"]["configurations"] = []          # older firmware: v1 is the real thing
    monkeypatch.setattr(app_modules.stream_engine, "start",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            app_modules.StreamError("no route to the bridge")))
    client.post("/api/stream/start", json=stream_body())

    body = diagnostics(client)
    steps = body["last_attempt"]["steps"]
    armed = next(s for s in steps if s["step"] == "armed")
    assert armed["over"] == "v1", "this test only means anything on the v1 path"
    assert armed["bridge_says"]["owned_by_us"] is True, "the claim should be ours"
    assert "stub-key" not in json.dumps(body)


def test_streaming_needs_a_console_paired_for_it(client, bridge):
    configure(client)                               # api key only, no client key
    r = client.post("/api/stream/start", json=stream_body())
    assert r.status_code == 409
    assert "pair" in r.json()["detail"].lower()


def test_streaming_rejects_an_area_the_bridge_does_not_have(client, bridge):
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    assert client.post("/api/stream/start", json=stream_body(area_id="999")).status_code == 404
    # A room is not an entertainment area, even though both are groups.
    assert client.post("/api/stream/start", json=stream_body(area_id="1")).status_code == 404


def test_streaming_refuses_an_area_someone_else_holds(client, bridge):
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    r = client.post("/api/stream/start", json=stream_body(area_id="4"))   # TV area
    assert r.status_code == 409
    assert "already streaming" in r.json()["detail"]


def test_a_failed_stream_hands_the_area_back(client, bridge, app_modules, monkeypatch):
    """The bridge ignores everything else while it holds an area for streaming,
    so an area claimed for a stream that never opened would strand the lights."""
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})

    def refuse(*a, **kw):
        raise app_modules.StreamError("no route to the bridge")

    monkeypatch.setattr(app_modules.stream_engine, "start", refuse)
    r = client.post("/api/stream/start", json=stream_body())
    assert r.status_code == 502
    # This bridge knows the area in v2, so arming goes there and the v1 flag is
    # only touched on the way out. Arming clears any hold before taking one:
    # a bridge that already thinks the area is streaming treats a bare "start"
    # as nothing to do, and leaves the port bound to the dead session.
    assert bridge["v2"]["armed"][:2] == ["stop", "start"]
    assert bridge["v2"]["armed"][-1] == "stop"
    assert bridge["groups"]["6"]["stream"]["active"] is False
    assert bridge["groups"]["6"]["stream"]["active"] is False


def test_starting_a_stream_stops_rest_flicker_on_those_lights(client, bridge, app_modules,
                                                              monkeypatch):
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    client.post("/api/flicker/start",
                json={"light_ids": ["1", "2"], "pattern_id": "flicker_a", "hz": 5})
    assert client.get("/api/status").json()["lights"]["1"]["running"] is True

    monkeypatch.setattr(app_modules.stream_engine, "start", lambda *a, **kw: None)
    assert client.post("/api/stream/start", json=stream_body()).status_code == 200
    # Area 6 holds lights 1-4, so both REST loops must have been stood down.
    lights = client.get("/api/status").json()["lights"]
    assert lights["1"]["running"] is False and lights["2"]["running"] is False


def test_stopping_a_stream_releases_the_area(client, bridge, app_modules, monkeypatch):
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    monkeypatch.setattr(app_modules.stream_engine, "start", lambda *a, **kw: None)
    monkeypatch.setattr(app_modules.stream_engine, "area_id", lambda: "6")
    monkeypatch.setattr(app_modules.stream_engine, "light_ids", lambda: ["1", "2"])
    client.post("/api/stream/start", json=stream_body())
    assert client.post("/api/stream/stop").status_code == 200
    assert bridge["stream_calls"][-1] == ("6", False)


def test_updating_when_nothing_is_streaming_is_409(client, bridge):
    configure(client)
    assert client.post("/api/stream/update", json={"hz": 5}).status_code == 409


def test_status_reports_the_stream(client, bridge):
    configure(client)
    body = client.get("/api/status").json()
    assert body["stream"]["running"] is False
    assert body["stream"]["max_hz"] == app_stream_max()


def app_stream_max():
    from app.hue_stream import MAX_STREAM_HZ
    return MAX_STREAM_HZ


def test_an_area_this_console_left_claimed_is_taken_back(client, bridge, app_modules,
                                                         monkeypatch):
    """A session that died without letting go leaves the claim behind. It is
    taken back rather than reported as a conflict, since it is ours."""
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    # As if a previous run of this console had claimed it and never returned.
    bridge["groups"]["6"]["stream"] = {"active": True, "owner": "stub-key"}
    monkeypatch.setattr(app_modules.stream_engine, "start", lambda *a, **kw: None)

    r = client.post("/api/stream/start", json=stream_body())
    assert r.status_code == 200
    # Armed over v2, which is the route this bridge listens to; the stale v1
    # flag is not what was blocking it.
    assert "start" in bridge["v2"]["armed"]


def test_an_area_someone_else_claimed_is_still_a_conflict(client, bridge):
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    bridge["groups"]["6"]["stream"] = {"active": True, "owner": "hue-sync"}
    r = client.post("/api/stream/start", json=stream_body())
    assert r.status_code == 409
    assert "already streaming" in r.json()["detail"]


def test_an_area_can_be_handed_back_by_hand(client, bridge):
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    bridge["groups"]["6"]["stream"] = {"active": True, "owner": "whoever"}
    assert client.post("/api/stream/release", json={"area_id": "6"}).status_code == 200
    assert bridge["groups"]["6"]["stream"]["active"] is False


def test_a_timeout_says_what_to_try(client, bridge, app_modules, monkeypatch):
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})

    def slow(*a, **kw):
        raise app_modules.StreamError(
            "Could not open the entertainment stream: timed out")

    monkeypatch.setattr(app_modules.stream_engine, "start", slow)
    r = client.post("/api/stream/start", json=stream_body())
    assert r.status_code == 502
    detail = r.json()["detail"]
    # A timeout means "no answer", which is equally what a blocked UDP path and
    # a key the bridge won't accept look like. The message has to name whichever
    # one the probe actually found rather than listing both.
    # Whichever one the probe found — never a list of maybes.
    assert ("streaming key is the problem" in detail
            or "never opening the port" in detail
            or "not on the bridge's own network" in detail
            or "that is the network path" in detail.lower()), detail
    # And the area is not left held by a stream that never opened.
    assert bridge["groups"]["6"]["stream"]["active"] is False


def test_streaming_snapshots_the_lights_and_puts_them_back(client, bridge, app_modules,
                                                           monkeypatch):
    """A stream leaves each bulb on whatever the last frame held. Without a
    snapshot taken before the area is claimed there is nothing to put back —
    and it has to be taken first, because a claimed area stops answering REST."""
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    monkeypatch.setattr(app_modules.stream_engine, "start", lambda *a, **kw: None)
    monkeypatch.setattr(app_modules.stream_engine, "area_id", lambda: "6")
    monkeypatch.setattr(app_modules.stream_engine, "light_ids", lambda: ["1", "2", "3", "4"])

    assert client.post("/api/stream/start", json=stream_body()).status_code == 200
    snapshots = client.get("/api/status").json()["snapshots"]
    assert set(snapshots) >= {"1", "2", "3", "4"}
    # Light 1 was on, bright and warm before we touched it.
    assert snapshots["1"]["bri"] == 180

    bridge["puts"].clear()
    assert client.post("/api/stream/stop").status_code == 200
    assert bridge["puts"], "nothing was sent to put the bulbs back"
    assert any(p.get("bri") == 180 for p in bridge["puts"])


def test_the_area_goes_back_before_the_lights_are_restored(client, bridge, app_modules,
                                                           monkeypatch):
    """A restore sent while the bridge is still streaming to the area goes
    nowhere, so the order is not incidental."""
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    monkeypatch.setattr(app_modules.stream_engine, "start", lambda *a, **kw: None)
    monkeypatch.setattr(app_modules.stream_engine, "area_id", lambda: "6")
    monkeypatch.setattr(app_modules.stream_engine, "light_ids", lambda: ["1"])
    client.post("/api/stream/start", json=stream_body())

    order = []
    original_set_stream = app_modules.HueClient.set_stream
    original_set_state = app_modules.HueClient.set_light_state

    async def note_stream(self, gid, active):
        order.append(("stream", active))
        return await original_set_stream(self, gid, active)

    async def note_state(self, lid, **st):
        order.append(("light", lid))
        return await original_set_state(self, lid, **st)

    monkeypatch.setattr(app_modules.HueClient, "set_stream", note_stream)
    monkeypatch.setattr(app_modules.HueClient, "set_light_state", note_state)
    client.post("/api/stream/stop")

    assert ("stream", False) in order
    assert ("light", "1") in order
    assert order.index(("stream", False)) < order.index(("light", "1"))


def test_a_colour_can_be_cleared_mid_stream(client, bridge, app_modules, monkeypatch):
    """Unlike a bulb over REST, a stream frame carries the colour every time, so
    a stream really can go back to having none. Null therefore means "clear it"
    rather than "not mentioned", and dropping it with the other empties made
    unticking the colour box do nothing at all."""
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    monkeypatch.setattr(app_modules.stream_engine, "start", lambda *a, **kw: None)
    state = {"area_id": "6", "light_ids": ["1"], "hue": 3295, "sat": 236,
             "min_bri": 1, "max_bri": 254}
    monkeypatch.setattr(app_modules.stream_engine, "status",
                        lambda: {"running": True, "settings": state, "area_id": "6"})
    applied = {}
    monkeypatch.setattr(app_modules.stream_engine, "update",
                        lambda **kw: (applied.update(kw), True)[1])

    client.post("/api/stream/update", json={"hue": None, "sat": None})
    assert "hue" in applied and applied["hue"] is None
    assert "sat" in applied and applied["sat"] is None

    applied.clear()
    client.post("/api/stream/update", json={"hz": 8})
    assert "hue" not in applied, "a colour nobody mentioned must be left alone"


def test_an_area_stranded_by_a_previous_run_is_released_on_startup(app_modules, bridge):
    """A container killed mid-stream leaves the area claimed on the bridge, and
    nothing else ever clears it — those lights then answer to nothing at all,
    not this console and not the Hue app."""
    from fastapi.testclient import TestClient
    app_modules.config_store.update(bridge_ip="10.0.0.5", api_key="k")
    bridge["groups"]["6"]["stream"] = {"active": True, "owner": "k"}

    with TestClient(app_modules.app):
        pass                       # startup and shutdown are the whole test

    assert bridge["groups"]["6"]["stream"]["active"] is False
    assert ("6", False) in bridge["stream_calls"]


def test_an_area_someone_else_holds_is_left_alone_on_startup(app_modules, bridge):
    from fastapi.testclient import TestClient
    app_modules.config_store.update(bridge_ip="10.0.0.5", api_key="k")
    bridge["groups"]["6"]["stream"] = {"active": True, "owner": "hue-sync"}

    with TestClient(app_modules.app):
        pass

    assert bridge["groups"]["6"]["stream"]["active"] is True, \
        "released an area belonging to something else"


def test_a_bridge_that_moved_network_keeps_its_credentials(client, bridge):
    """The key belongs to the bridge, not to its address. Requiring it back on
    every address change would mean digging a forty-character string out of the
    config file to retype whenever a DHCP lease moved."""
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    before = client.get("/api/bridge").json()
    assert before["can_stream"] is True

    r = client.post("/api/bridge/set", json={"bridge_ip": "10.0.0.9"})
    assert r.status_code == 200
    after = client.get("/api/bridge").json()
    assert after["bridge_ip"] == "10.0.0.9"
    assert after["configured"] is True
    assert after["can_stream"] is True, "moving the bridge cost it the streaming key"


def test_an_address_with_no_key_at_all_is_still_refused(client):
    r = client.post("/api/bridge/set", json={"bridge_ip": "10.0.0.9"})
    assert r.status_code == 400
    assert "API key" in r.json()["detail"]


# ---------- the v2 entertainment path ----------

def test_an_area_that_exists_in_v2_is_armed_over_v2(client, bridge, app_modules,
                                                    monkeypatch):
    """An area made by the current Hue app is a v2 entertainment configuration,
    and the v1 group is a compatibility view. Setting stream.active through that
    view binds UDP 2100 without arming the service behind it, so the bridge then
    answers no handshake at all — which looks exactly like a dead network."""
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    monkeypatch.setattr(app_modules.stream_engine, "start", lambda *a, **kw: None)

    r = client.post("/api/stream/start", json=stream_body())
    assert r.status_code == 200
    assert "start" in bridge["v2"]["armed"], "never armed over v2"


def test_the_v2_area_identity_reaches_the_engine(client, bridge, app_modules,
                                                 monkeypatch):
    """A v2 frame addresses channels within the area and carries the area's
    UUID; a v1 frame addresses light ids. Getting that wrong sends a
    well-formed frame the bridge has no reason to act on."""
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    captured = {}
    monkeypatch.setattr(app_modules.stream_engine, "start",
                        lambda *a, **kw: captured.update(kw))

    client.post("/api/stream/start", json=stream_body())
    assert captured["area_uuid"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert captured["channels"] == [0, 1, 2]


def test_stopping_disarms_over_v2_as_well(client, bridge, app_modules, monkeypatch):
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    monkeypatch.setattr(app_modules.stream_engine, "start", lambda *a, **kw: None)
    monkeypatch.setattr(app_modules.stream_engine, "area_id", lambda: "6")
    monkeypatch.setattr(app_modules.stream_engine, "light_ids", lambda: ["1"])
    client.post("/api/stream/start", json=stream_body())
    bridge["v2"]["armed"].clear()

    client.post("/api/stream/stop")
    assert "stop" in bridge["v2"]["armed"], "left the v2 configuration armed"


def test_a_bridge_with_no_v2_view_still_uses_the_v1_flag(client, bridge, app_modules,
                                                        monkeypatch):
    """Older firmware has no v2 record, and there the v1 flag is the real thing
    rather than a shim over one."""
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    bridge["v2"]["configurations"] = []
    monkeypatch.setattr(app_modules.stream_engine, "start", lambda *a, **kw: None)

    r = client.post("/api/stream/start", json=stream_body())
    assert r.status_code == 200
    assert bridge["v2"]["armed"] == []
    assert ("6", True) in bridge["stream_calls"]


def test_diagnostics_names_the_bridge_and_its_firmware(client, bridge):
    """Streaming faults get reported to people who cannot see the hardware, and
    "which bridge, running what" is the first thing any of them will ask."""
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    reported = diagnostics(client)["bridge"]
    assert reported["modelid"] == "BSB002"
    assert reported["swversion"] == "1970010101"
    assert reported["apiversion"] == "1.68.0"


def test_diagnostics_survives_a_bridge_that_will_not_describe_itself(client):
    """An unconfigured console still has to render its diagnostics page."""
    assert diagnostics(client)["bridge"] == {}


# ---------- Creating entertainment areas ----------

def test_candidates_separate_lights_that_can_stream_from_those_that_cannot(client, bridge):
    """The difference between an area and a group is which lights may join it.

    An area is built from each light's entertainment service, so a plug or a
    white-only bulb has nothing to contribute. Both lists are returned: a light
    that silently vanished from the picker would look like a bug in the picker.
    """
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    body = client.get("/api/stream/candidates").json()

    assert [c["light_id"] for c in body["candidates"]] == ["2", "1"]  # by name
    assert {c["light_id"] for c in body["excluded"]} == {"3", "4", "5"}
    assert body["max_lights"] == 10
    # The service id is what the create call needs, so it has to come back too.
    assert {c["service_rid"] for c in body["candidates"]} == {"ent-1", "ent-2"}


def test_an_area_can_be_created_on_the_bridge(client, bridge):
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    r = client.post("/api/stream/areas",
                    json={"name": "Front room", "light_ids": ["1", "2"]})
    assert r.status_code == 200
    assert r.json()["name"] == "Front room"

    created = bridge["v2"]["configurations"][-1]["created"]
    assert created["metadata"]["name"] == "Front room"
    assert [sl["service"]["rid"]
            for sl in created["locations"]["service_locations"]] == ["ent-1", "ent-2"]
    # The bridge insists on positions, and two lights stacked at the origin draw
    # as one dot in the Hue app.
    xs = [sl["positions"][0]["x"] for sl in created["locations"]["service_locations"]]
    assert xs == [-0.8, 0.8]


def test_creating_an_area_refuses_a_light_that_cannot_render(client, bridge):
    """Named outright rather than dropped, so the user is not left wondering
    why the area they built came back a light short."""
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    r = client.post("/api/stream/areas",
                    json={"name": "Nope", "light_ids": ["1", "3"]})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "3" in detail and "colour-capable" in detail
    assert not any(c.get("created") for c in bridge["v2"]["configurations"])


def test_creating_an_area_refuses_a_repeated_light(client, bridge):
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    r = client.post("/api/stream/areas",
                    json={"name": "Twice", "light_ids": ["1", "1"]})
    assert r.status_code == 422
    assert "more than once" in r.json()["detail"]


def test_an_area_cannot_hold_more_than_the_bridge_allows(client, bridge):
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    r = client.post("/api/stream/areas",
                    json={"name": "Too many", "light_ids": [str(i) for i in range(11)]})
    assert r.status_code == 422


def test_an_area_can_be_deleted_and_is_addressed_by_its_v2_id(client, bridge):
    """The v1 group number is a compatibility view; the configuration itself is
    what gets deleted, so the listing has to hand the UI that id."""
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    listed = client.get("/api/stream/areas").json()["areas"]
    area = next(a for a in listed if a["id"] == "6")
    assert area["uuid"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    assert client.delete(f"/api/stream/areas/{area['uuid']}").status_code == 200
    assert not [c for c in bridge["v2"]["configurations"] if c["id"] == area["uuid"]]


def test_an_area_is_not_deleted_out_from_under_a_running_stream(client, bridge,
                                                               app_modules, monkeypatch):
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    monkeypatch.setattr(type(app_modules.stream_engine), "running",
                        property(lambda self: True))
    r = client.delete("/api/stream/areas/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert r.status_code == 409
    assert "Stop the stream" in r.json()["detail"]


def test_an_area_can_be_renamed(client, bridge):
    """Renaming addresses the configuration, so it survives in the Hue app too —
    which is the point of areas living on the bridge rather than in here."""
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    r = client.put(f"/api/stream/areas/{uuid}", json={"name": "Snug"})
    assert r.status_code == 200
    assert next(c for c in bridge["v2"]["configurations"]
                if c["id"] == uuid)["name"] == "Snug"


def test_renaming_an_area_still_wants_a_name(client, bridge):
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert client.put(f"/api/stream/areas/{uuid}", json={"name": ""}).status_code == 422


def test_local_groups_are_gone(client, bridge):
    """Areas on the bridge replaced them. The routes should be absent rather
    than lingering as something a stale page could still call."""
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    assert client.get("/api/groups").status_code == 404
    assert client.post("/api/groups", json={"name": "x", "light_ids": ["1"]}).status_code == 404
    # And nothing writes a groups key back into the config.
    assert "groups" not in diagnostics(client)


# ---------- Editing a custom pattern ----------

def make_pattern(client, **kw):
    body = {"name": "Torchlight", "sequence": "mmnnaamm", "hz": 10,
            "min_bri": 1, "max_bri": 254, "transition_ms": 0, **kw}
    return client.post("/api/patterns", json=body).json()


def test_a_custom_pattern_can_be_edited_in_place(client):
    """The id has to survive an edit.

    Light cards, the stream panel and any saved selection all refer to a
    pattern by its id, so a save that minted a new one would leave every one of
    them pointing at something that no longer exists.
    """
    made = make_pattern(client, name="Before", hue=6000, sat=225)
    r = client.put(f"/api/patterns/{made['id']}", json={
        "name": "After", "sequence": "zzaa", "hz": 12,
        "min_bri": 20, "max_bri": 200, "transition_ms": 300,
    })
    assert r.status_code == 200
    updated = r.json()
    assert updated["id"] == made["id"]
    assert (updated["name"], updated["sequence"], updated["hz"]) == ("After", "zzaa", 12)
    assert updated["transition_ms"] == 300
    # Colour omitted on the edit means the pattern no longer names one.
    assert updated["hue"] is None and updated["sat"] is None

    listed = client.get("/api/patterns").json()["custom"]
    assert [p["id"] for p in listed] == [made["id"]], "the edit should not add a second"


def test_editing_a_pattern_normalises_the_sequence_like_creating_one(client):
    made = make_pattern(client)
    r = client.put(f"/api/patterns/{made['id']}",
                   json={"name": "Spaced", "sequence": " M M a A "})
    assert r.status_code == 200
    assert r.json()["sequence"] == "mmaa"


def test_editing_rejects_what_creating_rejects(client):
    made = make_pattern(client)
    bad = client.put(f"/api/patterns/{made['id']}",
                     json={"name": "Bad", "sequence": "mm!!"})
    assert bad.status_code == 400
    assert client.put("/api/patterns/custom_nope",
                      json={"name": "x", "sequence": "mm"}).status_code == 404
    builtin = client.get("/api/patterns").json()["builtin"][0]["id"]
    assert client.put(f"/api/patterns/{builtin}",
                      json={"name": "x", "sequence": "mm"}).status_code == 400


def test_a_pattern_running_on_a_light_cannot_be_edited_under_it(client, bridge):
    """A running loop holds its own copy of the sequence, so an edit would leave
    lights flickering something the UI no longer describes."""
    client.post("/api/bridge/pair", json={"bridge_ip": "10.0.0.7"})
    made = make_pattern(client)
    client.post("/api/flicker/start",
                json={"light_ids": ["1"], "pattern_id": made["id"]})
    r = client.put(f"/api/patterns/{made['id']}",
                   json={"name": "Nope", "sequence": "aabb"})
    assert r.status_code == 409
    assert "stop them first" in r.json()["detail"]
    assert client.get("/api/patterns").json()["custom"][0]["sequence"] == "mmnnaamm"
