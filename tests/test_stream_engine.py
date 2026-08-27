"""The streaming driver, exercised against a stub bridge on a real DTLS socket.

The point of the whole feature is that ten lights change on the same frame
rather than taking turns, so the tests check what actually went on the wire:
one datagram per frame carrying every light, at the stream's own rate, not
divided between them.
"""
import socket
import threading
import time

import pytest

from app.hue_stream import DTLS_CIPHERS, MAGIC, StreamError
from app.stream_engine import FRAME_RATE_HZ, StreamEngine, level_at, rgb_for

IDENTITY = "stream-user"
KEY_HEX = "0123456789abcdef0123456789abcdef"


# ---------- pattern maths, no socket involved ----------

def test_the_pattern_advances_against_the_epoch():
    # "az" at 10 Hz: a for the first 100ms, z for the next.
    assert level_at("az", 10.0, 0.0, 0.00) == pytest.approx(0.0)
    assert level_at("az", 10.0, 0.0, 0.05) == pytest.approx(0.0)
    assert level_at("az", 10.0, 0.0, 0.15) == pytest.approx(1.0)
    assert level_at("az", 10.0, 0.0, 0.25) == pytest.approx(0.0)   # wrapped


def test_speed_is_capped_at_the_frame_rate():
    # Asking for 200 Hz cannot show more than the stream sends.
    fast = level_at("az", 200.0, 0.0, 1.0 / FRAME_RATE_HZ * 1.5)
    capped = level_at("az", FRAME_RATE_HZ, 0.0, 1.0 / FRAME_RATE_HZ * 1.5)
    assert fast == capped


def test_brightness_window_scales_the_frame():
    state = {"sequence": "a", "hz": 10.0, "epoch": 0.0,
             "min_bri": 254, "max_bri": 254, "hue": None, "sat": None}
    assert rgb_for(state, 0.0) == (0xFFFF, 0xFFFF, 0xFFFF)
    state["min_bri"] = state["max_bri"] = 1
    assert max(rgb_for(state, 0.0)) < 0x0400


def test_a_named_colour_rides_through_the_frame():
    state = {"sequence": "z", "hz": 10.0, "epoch": 0.0, "min_bri": 1, "max_bri": 254,
             "hue": 0, "sat": 254}
    r, g, b = rgb_for(state, 0.0)
    assert r == 0xFFFF and g == 0 and b == 0


# ---------- the sender, over a real handshake ----------

def local_ipv4() -> str:
    """An address of this machine that the bridge-address rules accept.

    The stub has to live somewhere DtlsStream will actually dial, and that rules
    out loopback: the same check that stops the console being aimed at the
    host's private services applies to the stream socket too.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))          # TEST-NET-1; no packet is sent
        return probe.getsockname()[0]
    finally:
        probe.close()


def usable_stub_host() -> str:
    from app.hue_client import BridgeAddressError, parse_bridge_address
    host = local_ipv4()
    try:
        parse_bridge_address(host)
    except BridgeAddressError:
        pytest.skip(f"no non-loopback address to host the stub bridge on (got {host})")
    return host


@pytest.fixture
def stub_bridge():
    tls = pytest.importorskip("mbedtls.tls")
    frames, ready = [], threading.Event()
    raw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    config = tls.DTLSConfiguration(
        pre_shared_key_store={IDENTITY: bytes.fromhex(KEY_HEX)},
        ciphers=DTLS_CIPHERS, validate_certificates=False)
    sock = tls.ServerContext(config).wrap_socket(raw)
    host = usable_stub_host()
    sock.bind((host, 0))
    port = sock.getsockname()[1]

    def serve():
        ready.set()
        try:
            conn, addr = sock.accept()
            conn.setcookieparam(addr[0].encode())
            try:
                conn.do_handshake()
            except tls.HelloVerifyRequest:
                conn, addr = conn.accept()
                conn.setcookieparam(addr[0].encode())
                conn.do_handshake()
        except Exception:
            return
        while True:
            try:
                frames.append((time.monotonic(), conn.recv(4096)))
            except Exception:
                return

    threading.Thread(target=serve, daemon=True).start()
    ready.wait(2)
    yield {"host": host, "port": port, "frames": frames}
    sock.close()


def run_area(engine, stub, light_ids, hz=10.0, seconds=1.0, **kw):
    import app.hue_stream as hue_stream
    original = hue_stream.STREAM_PORT
    hue_stream.STREAM_PORT = stub["port"]
    try:
        engine.start(stub["host"], IDENTITY, KEY_HEX, "6", light_ids,
                     kw.pop("sequence", "mz"), "p", hz,
                     kw.pop("min_bri", 1), kw.pop("max_bri", 254),
                     kw.pop("hue", None), kw.pop("sat", None))
        time.sleep(seconds)
    finally:
        hue_stream.STREAM_PORT = original


def test_one_datagram_carries_every_light_in_the_area(stub_bridge):
    engine = StreamEngine()
    run_area(engine, stub_bridge, ["1", "2", "3", "4", "5", "6", "7"], seconds=0.8)
    engine.stop()

    frames = [f for _, f in stub_bridge["frames"]]
    assert frames, "nothing reached the bridge"
    for frame in frames:
        assert frame[:9] == MAGIC
        # 16-byte header plus nine bytes for each of the seven lights, in one
        # datagram — this is the whole point of the feature.
        assert len(frame) == 16 + 9 * 7
    ids = [int.from_bytes(frames[0][16 + 9 * i + 1:16 + 9 * i + 3], "big") for i in range(7)]
    assert ids == [1, 2, 3, 4, 5, 6, 7]


def test_seven_lights_still_run_at_the_asked_for_speed(stub_bridge):
    """The REST path would give seven bulbs about 1.1 Hz each. Here the rate is
    not divided at all."""
    engine = StreamEngine()
    run_area(engine, stub_bridge, [str(i) for i in range(1, 8)], hz=10.0, seconds=1.5)
    status = engine.status()
    engine.stop()

    stamps = [t for t, _ in stub_bridge["frames"]]
    assert len(stamps) >= 2
    elapsed = stamps[-1] - stamps[0]
    rate = (len(stamps) - 1) / elapsed
    assert rate > 15, f"expected roughly {FRAME_RATE_HZ} frames/sec, got {rate:.1f}"
    assert status["effective_hz"] == 10.0      # not 10 * 0.8 / 7


def test_the_pattern_actually_changes_between_frames(stub_bridge):
    engine = StreamEngine()
    run_area(engine, stub_bridge, ["1"], hz=10.0, seconds=1.0, sequence="az")
    engine.stop()
    reds = {frame[19:21] for _, frame in stub_bridge["frames"]}
    assert len(reds) >= 2, "every frame carried the same value; the pattern isn't running"


def test_retuning_mid_stream_changes_what_goes_out(stub_bridge):
    engine = StreamEngine()
    import app.hue_stream as hue_stream
    original = hue_stream.STREAM_PORT
    hue_stream.STREAM_PORT = stub_bridge["port"]
    try:
        engine.start(stub_bridge["host"], IDENTITY, KEY_HEX, "6", ["1"], "z", "p",
                     10.0, 1, 1, None, None)          # pinned dark
        time.sleep(0.4)
        dark = len(stub_bridge["frames"])
        assert engine.update(min_bri=254, max_bri=254) is True
        time.sleep(0.4)
    finally:
        hue_stream.STREAM_PORT = original
        engine.stop()

    before = stub_bridge["frames"][max(0, dark - 3)][1][19:21]
    after = stub_bridge["frames"][-1][1][19:21]
    assert before != after
    assert int.from_bytes(after, "big") > int.from_bytes(before, "big")


def test_stopping_closes_the_stream_and_clears_the_state(stub_bridge):
    engine = StreamEngine()
    run_area(engine, stub_bridge, ["1"], seconds=0.4)
    assert engine.running is True
    engine.stop()
    assert engine.running is False
    assert engine.status()["area_id"] is None
    # Let anything already on the wire land before taking the baseline: a
    # datagram sent a moment before stop can arrive after it, and counting that
    # as "still sending" makes the test fail under load rather than on a bug.
    time.sleep(0.3)
    sent = len(stub_bridge["frames"])
    time.sleep(0.5)
    assert len(stub_bridge["frames"]) == sent, "still sending after stop"


def test_update_before_start_is_refused():
    assert StreamEngine().update(hz=5) is False


def test_stopping_tells_the_caller_which_area_to_hand_back(stub_bridge):
    released = []
    engine = StreamEngine(on_stopped=lambda area, lights: released.append((area, lights)))
    run_area(engine, stub_bridge, ["1"], seconds=0.4)
    engine.stop()
    time.sleep(0.3)
    # The lights come with it: they have to be put back where they were, and
    # once the area is released there is nothing left to ask which they were.
    assert released == [("6", ["1"])]


def test_a_sender_that_dies_on_its_own_still_hands_the_area_back(stub_bridge, monkeypatch):
    """The bridge keeps an area claimed until told otherwise. A sender that
    gives up quietly would leave those lights answering to nothing — not this
    console, not the Hue app — until the bridge was restarted."""
    released = []
    engine = StreamEngine(on_stopped=lambda area, lights: released.append(area))
    import app.hue_stream as hue_stream

    def die(self, frame):
        raise OSError("network went away")

    run_area(engine, stub_bridge, ["1"], seconds=0.2)
    monkeypatch.setattr(hue_stream.DtlsStream, "send", die)
    deadline = time.monotonic() + 6
    while engine.running and time.monotonic() < deadline:
        time.sleep(0.1)

    assert engine.running is False, "the sender should have given up"
    assert released == ["6"]
    engine.stop()


def test_a_failed_handshake_leaves_no_area_behind(stub_bridge):
    """A start that never connected must not report an area as streaming."""
    engine = StreamEngine()
    import app.hue_stream as hue_stream
    original = hue_stream.STREAM_PORT
    hue_stream.STREAM_PORT = 1     # nothing is listening there
    try:
        with pytest.raises(StreamError):
            engine.start(stub_bridge["host"], IDENTITY, KEY_HEX, "6", ["1"], "mz", "p",
                         10.0, 1, 254, None, None)
    finally:
        hue_stream.STREAM_PORT = original
    assert engine.area_id() is None
    assert engine.status()["running"] is False


def test_a_colour_can_be_set_and_then_cleared_on_the_running_state():
    engine = StreamEngine()
    with engine._lock:
        engine._state = {"sequence": "z", "hz": 10.0, "epoch": 0.0,
                         "min_bri": 1, "max_bri": 254, "hue": None, "sat": None,
                         "area_id": "6", "light_ids": ["1"], "pattern_id": "p"}
    assert engine.update(hue=43000, sat=240) is True
    assert engine.status()["settings"]["hue"] == 43000

    # None is a request here, not an omission — the next frame carries it.
    assert engine.update(hue=None, sat=None) is True
    assert engine.status()["settings"]["hue"] is None
    r, g, b = rgb_for(engine.status()["settings"], 0.0)
    assert r == g == b, "with no colour a frame should be plain brightness"

    # Everything else still treats None as "leave it alone".
    assert engine.update(hz=None) is True
    assert engine.status()["settings"]["hz"] == 10.0


def test_a_v2_area_sends_channel_frames_not_light_frames(stub_bridge):
    """v1 addresses light ids in nine-byte blocks; v2 addresses channels in
    seven, behind the area's 36-character UUID. A bridge sent the wrong one
    gets a well-formed frame it has no reason to act on."""
    engine = StreamEngine()
    uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    import app.hue_stream as hue_stream
    original = hue_stream.STREAM_PORT
    hue_stream.STREAM_PORT = stub_bridge["port"]
    try:
        engine.start(stub_bridge["host"], IDENTITY, KEY_HEX, "200",
                     ["1", "2", "3"], "mz", "p", 10.0, 1, 254, None, None,
                     area_uuid=uuid, channels=[0, 1, 2])
        time.sleep(0.8)
        assert engine.status()["protocol"] == 2
    finally:
        hue_stream.STREAM_PORT = original
        engine.stop()

    frames = [f for _, f in stub_bridge["frames"]]
    assert frames, "nothing reached the bridge"
    for frame in frames:
        assert frame[9] == 2                          # protocol version 2
        assert frame[16:52] == uuid.encode("ascii")
        assert len(frame) == 16 + 36 + 7 * 3
    # Channel ids, one byte each, not two-byte light ids.
    assert [frames[0][52 + 7 * i] for i in range(3)] == [0, 1, 2]


def test_an_area_with_no_v2_identity_still_sends_v1_frames(stub_bridge):
    engine = StreamEngine()
    run_area(engine, stub_bridge, ["4", "5"], seconds=0.6)
    assert engine.status()["protocol"] == 1
    engine.stop()
    frames = [f for _, f in stub_bridge["frames"]]
    assert frames and frames[0][9] == 1
    assert len(frames[0]) == 16 + 9 * 2


def test_the_framing_is_still_reported_after_the_stream_stops(monkeypatch):
    """A finished run should say which framing carried it.

    transport and frames_sent already survive a stop; protocol was derived from
    the live state and so went blank with it, leaving a report that named the
    client and the frame count but not the protocol — the one thing about a
    finished run that nothing else records.
    """
    import app.stream_engine as stream_engine

    class Fake:
        transport = "minimal"
        def send(self, _frame): pass
        def close(self): pass

    monkeypatch.setattr(stream_engine, "DtlsStream",
                        lambda *a, **kw: type("S", (Fake,), {
                            "connect": lambda self, **kw: None})())

    engine = stream_engine.StreamEngine()
    engine.start("10.0.0.7", "user", "00" * 16, "6", ["1"], "mn", "p",
                 10, 1, 254, None, None,
                 area_uuid="aaaa-bbbb", channels=[0, 1])
    assert engine.status()["protocol"] == 2
    engine.stop()
    after = engine.status()
    assert after["running"] is False
    assert after["transport"] == "minimal"
    assert after["protocol"] == 2, "the framing went blank when the stream did"

    # And a v1 area reports v1, before and after.
    engine.start("10.0.0.7", "user", "00" * 16, "6", ["1"], "mn", "p",
                 10, 1, 254, None, None)
    engine.stop()
    assert engine.status()["protocol"] == 1


def test_a_transition_eases_between_letters_instead_of_stepping():
    """HueStream carries no transition field — a frame is a colour and nothing
    else — so the ramp the REST path asks the bridge for is done here.

    Finer than the bridge's, too: it interpolates in 100ms steps where this has
    a frame every 40ms.
    """
    from app.stream_engine import level_at

    # 'az' at 10 Hz: dark for 100ms, then full for 100ms.
    hard = [level_at("az", 10, 0, t) for t in (0.10, 0.12, 0.15, 0.19)]
    assert hard == [1.0, 1.0, 1.0, 1.0], "with no transition it should step"

    ramped = [level_at("az", 10, 0, t, 60) for t in (0.10, 0.12, 0.15, 0.19)]
    assert ramped[0] == 0.0                      # starts from the letter before
    assert ramped == sorted(ramped)              # and climbs
    assert ramped[-1] == 1.0                     # arriving before the letter ends
    assert 0.0 < ramped[1] < 1.0


def test_a_transition_longer_than_the_step_still_arrives():
    """Asking for a ramp longer than a letter is on screen would otherwise mean
    never reaching the level the pattern asked for."""
    from app.stream_engine import level_at

    # 100ms per letter, 500ms requested. Halfway through the letter the level
    # should be halfway up, which only holds if the ramp was clamped to the
    # step: over the requested 500ms it would have reached about a fifth.
    assert level_at("az", 10, 0, 0.15, 500) == pytest.approx(0.5, abs=0.01)
    # And it arrives as the letter ends rather than stalling short of the level.
    assert level_at("az", 10, 0, 0.1999, 500) == pytest.approx(1.0, abs=0.01)


def test_the_engine_carries_the_transition_into_its_state(monkeypatch):
    import app.stream_engine as stream_engine

    class Fake:
        transport = "minimal"
        def connect(self, **kw): pass
        def send(self, _frame): pass
        def close(self): pass

    monkeypatch.setattr(stream_engine, "DtlsStream", lambda *a, **kw: Fake())
    engine = stream_engine.StreamEngine()
    engine.start("10.0.0.7", "user", "00" * 16, "6", ["1"], "mn", "p",
                 10, 1, 254, None, None, transition_ms=250)
    try:
        assert engine.status()["settings"]["transition_ms"] == 250
    finally:
        engine.stop()


def test_channels_without_overrides_all_run_the_area_pattern():
    """The ordinary stream: one effect across the room, as it always was."""
    from app.stream_engine import framing_for_channel

    state = {"sequence": "mmno", "pattern_id": "q", "hz": 10.0, "min_bri": 1,
             "max_bri": 254, "hue": None, "sat": None, "transition_ms": 0,
             "epoch": 0.0, "per_channel": {}}
    for channel in (0, 1, 2):
        assert framing_for_channel(state, channel) is state


def test_a_channel_keeps_the_areas_framing_for_what_it_does_not_name():
    """Naming a pattern must not silently reset speed and brightness too."""
    from app.stream_engine import framing_for_channel

    state = {"sequence": "mmno", "pattern_id": "q", "hz": 8.0, "min_bri": 20,
             "max_bri": 200, "hue": 6000, "sat": 180, "transition_ms": 100,
             "epoch": 0.0,
             "per_channel": {1: {"sequence": "za", "pattern_id": "strobe"}}}

    mine = framing_for_channel(state, 1)
    assert mine["sequence"] == "za" and mine["pattern_id"] == "strobe"
    # Everything it stayed quiet about still comes from the area.
    assert (mine["hz"], mine["min_bri"], mine["max_bri"]) == (8.0, 20, 200)
    assert (mine["hue"], mine["sat"], mine["transition_ms"]) == (6000, 180, 100)
    # And the area's own state is untouched by the merge.
    assert state["sequence"] == "mmno"


def test_a_channel_can_ask_to_run_white_under_a_coloured_area():
    """None is a real answer for colour, not an omission."""
    from app.stream_engine import framing_for_channel

    state = {"sequence": "m", "pattern_id": "q", "hz": 10.0, "min_bri": 1,
             "max_bri": 254, "hue": 6000, "sat": 200, "transition_ms": 0,
             "epoch": 0.0, "per_channel": {2: {"hue": None, "sat": None}}}
    mine = framing_for_channel(state, 2)
    assert mine["hue"] is None and mine["sat"] is None
    assert framing_for_channel(state, 3)["hue"] == 6000


def test_every_channel_derives_its_frame_from_the_one_clock():
    """Patterns of different lengths must stay on the same beat.

    A channel with an epoch of its own would drift against the rest of the
    room, which is the whole reason the area's is not overridable.
    """
    from app.stream_engine import framing_for_channel

    state = {"sequence": "m", "pattern_id": "q", "hz": 10.0, "min_bri": 1,
             "max_bri": 254, "hue": None, "sat": None, "transition_ms": 0,
             "epoch": 1234.5,
             "per_channel": {0: {"sequence": "za", "epoch": 99.0}}}
    assert framing_for_channel(state, 0)["epoch"] == 1234.5


@pytest.mark.asyncio
async def test_two_channels_send_different_values_in_the_same_frame():
    """The point of the whole change: one frame, different lights, no extra cost."""
    from app.stream_engine import framing_for_channel, rgb_for

    now = 0.30                      # 3 frames into a 10 Hz pattern
    state = {"sequence": "aaaaaaaazzzzzzzz", "pattern_id": "strobe", "hz": 10.0,
             "min_bri": 1, "max_bri": 254, "hue": None, "sat": None,
             "transition_ms": 0, "epoch": 0.0,
             "per_channel": {1: {"sequence": "z", "pattern_id": "hold"}}}

    dark = rgb_for(framing_for_channel(state, 0), now)   # 'a' -> floor
    lit = rgb_for(framing_for_channel(state, 1), now)    # 'z' -> full
    assert dark != lit, "both channels resolved to the same colour"
    assert max(lit) > max(dark)
