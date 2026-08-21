"""The wire format is pinned here.

Getting a byte wrong in a frame the bridge silently drops is very hard to debug
from the outside, so the layout is asserted against literal bytes rather than
against the code that produces them. The layout itself was taken from two
independent implementations that agree: music-assistant/hue-entertainment for
protocol 2, JakeBednard/node-phea for protocol 1.
"""
import socket
import threading

import pytest

from app.hue_stream import (
    COLOUR_SPACE_RGB,
    COLOUR_SPACE_XY,
    DTLS_CIPHERS,
    MAGIC,
    DtlsStream,
    StreamError,
    build_frame_v1,
    build_frame_v2,
    hue_sat_bri_to_rgb16,
)

AREA = "12345678-1234-1234-1234-123456789abc"


def test_the_v1_header_is_the_bytes_the_bridge_expects():
    frame = build_frame_v1(7, [(3, (0xFFFF, 0x0000, 0x0000))])
    assert frame[:9] == MAGIC == b"HueStream"
    assert frame[9] == 1              # version major
    assert frame[10] == 0             # version minor
    assert frame[11] == 7             # sequence
    assert frame[12:14] == b"\x00\x00"
    assert frame[14] == COLOUR_SPACE_RGB
    assert frame[15] == 0
    # 0x00 = "this is a light", then a 16-bit id, then 16-bit R G B.
    assert frame[16:25] == bytes.fromhex("00" "0003" "ffff" "0000" "0000")


def test_v1_carries_one_nine_byte_block_per_light():
    frame = build_frame_v1(0, [(1, (0, 0, 0)), (2, (0, 0, 0)), (300, (0, 0, 0))])
    assert len(frame) == 16 + 9 * 3
    assert frame[16 + 9 * 2 + 1:16 + 9 * 2 + 3] == (300).to_bytes(2, "big")


def test_the_v2_header_carries_the_area_uuid_as_ascii():
    frame = build_frame_v2(1, AREA, [(0, (0xFFFF, 0, 0))])
    assert frame[9] == 2
    assert frame[16:52] == AREA.encode("ascii")
    assert len(AREA) == 36
    # A v2 channel is one byte, not two: id, then 16-bit R G B.
    assert frame[52:59] == bytes.fromhex("00" "ffff" "0000" "0000")
    assert len(frame) == 16 + 36 + 7


def test_the_sequence_number_wraps_into_one_byte():
    assert build_frame_v1(255, [])[11] == 255
    assert build_frame_v1(256, [])[11] == 0
    assert build_frame_v2(257, AREA, [])[11] == 1


def test_the_colour_space_byte_is_selectable():
    assert build_frame_v1(0, [], colour_space=COLOUR_SPACE_XY)[14] == COLOUR_SPACE_XY


def test_components_are_clamped_into_sixteen_bits():
    frame = build_frame_v1(0, [(1, (-5, 70000, 0xFFFF))])
    assert frame[19:25] == bytes.fromhex("0000" "ffff" "ffff")


# ---------- colour ----------

def test_no_colour_means_white_scaled_by_brightness():
    assert hue_sat_bri_to_rgb16(None, None, 254) == (0xFFFF, 0xFFFF, 0xFFFF)
    r, g, b = hue_sat_bri_to_rgb16(None, None, 127)
    assert r == g == b
    assert 0x7E00 < r < 0x8200          # about half

def test_brightness_scales_the_named_colour_rather_than_greying_it():
    full = hue_sat_bri_to_rgb16(0, 254, 254)
    dim = hue_sat_bri_to_rgb16(0, 254, 25)
    assert full == (0xFFFF, 0, 0)
    assert dim[0] < full[0] and dim[1] == 0 and dim[2] == 0

@pytest.mark.parametrize("hue,expected_max", [(0, "r"), (21845, "g"), (43690, "b")])
def test_the_primaries_land_where_hue_says(hue, expected_max):
    rgb = hue_sat_bri_to_rgb16(hue, 254, 254)
    assert "rgb"[rgb.index(max(rgb))] == expected_max

def test_zero_saturation_is_white_whatever_the_hue():
    assert hue_sat_bri_to_rgb16(30000, 0, 254) == (0xFFFF, 0xFFFF, 0xFFFF)


# ---------- transport ----------

def test_a_client_key_that_is_not_hex_is_refused():
    with pytest.raises(StreamError, match="valid hex"):
        DtlsStream("10.0.0.5", "user", "not-hex!")


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
    """A DTLS-PSK listener standing in for the bridge's port 2100."""
    tls = pytest.importorskip("mbedtls.tls")
    identity, key = "stub-user", bytes.fromhex("0123456789abcdef0123456789abcdef")
    received, ready = [], threading.Event()

    raw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # accept() dups the fd and rebinds the same port; without REUSEPORT that
    # rebind loses to the dup still holding it.
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    config = tls.DTLSConfiguration(pre_shared_key_store={identity: key},
                                   ciphers=DTLS_CIPHERS, validate_certificates=False)
    sock = tls.ServerContext(config).wrap_socket(raw)
    host = usable_stub_host()
    sock.bind((host, 0))
    port = sock.getsockname()[1]

    def serve():
        ready.set()
        # A client offering the wrong key fails the handshake here. That is the
        # point of one of the tests, so it must not surface as a thread that
        # died — pytest reports those as warnings and the suite stops being
        # quiet enough to read.
        try:
            conn, addr = sock.accept()
            conn.setcookieparam(addr[0].encode())
            try:
                conn.do_handshake()
            except tls.HelloVerifyRequest:
                conn, addr = conn.accept()  # DTLS cookie exchange, second pass
                conn.setcookieparam(addr[0].encode())
                conn.do_handshake()
        except Exception:
            return
        while True:
            try:
                received.append(conn.recv(4096))
            except Exception:
                return

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    ready.wait(2)
    yield {"host": host, "port": port, "identity": identity,
           "key": key.hex(), "received": received}
    sock.close()


def test_frames_reach_the_bridge_over_a_real_handshake(stub_bridge):
    stream = DtlsStream(stub_bridge["host"], stub_bridge["identity"], stub_bridge["key"],
                        port=stub_bridge["port"])
    with stream:
        for seq in range(3):
            stream.send(build_frame_v1(seq, [(1, (0xFFFF, 0, 0)), (2, (0, 0, 0xFFFF))]))
        deadline = threading.Event()
        deadline.wait(1.0)

    assert len(stub_bridge["received"]) == 3
    for seq, frame in enumerate(stub_bridge["received"]):
        assert frame[:9] == MAGIC
        assert frame[11] == seq
        assert len(frame) == 16 + 9 * 2


def test_the_wrong_key_does_not_get_a_stream(stub_bridge):
    """Both clients have to fail this. The minimal one especially: it does not
    read anything back once streaming starts, so without waiting for the
    server's confirmation a rejected key would look exactly like success and
    the first sign of trouble would be lights that never change."""
    stream = DtlsStream(stub_bridge["host"], stub_bridge["identity"],
                        "ffffffffffffffffffffffffffffffff", port=stub_bridge["port"])
    with pytest.raises(StreamError, match="Could not open"):
        stream.connect(timeout=2.0)


def test_sending_before_connecting_is_an_error():
    with pytest.raises(StreamError, match="isn't open"):
        DtlsStream("10.0.0.5", "u", "00" * 16).send(b"x")


def test_a_rest_port_in_the_address_does_not_follow_into_the_stream():
    """The console stores "host" or "host:port", and that port is the REST
    one. Streaming has its own endpoint, so only the host carries over —
    passed through whole it lands inside the hostname and fails a lookup."""
    from app.hue_stream import STREAM_PORT
    stream = DtlsStream("192.0.2.2:9950", "user", "00" * 16)
    assert stream.bridge_ip == "192.0.2.2"
    assert stream.port == STREAM_PORT


def test_a_bad_bridge_address_is_refused_here_too():
    from app.hue_client import BridgeAddressError
    with pytest.raises(BridgeAddressError):
        DtlsStream("127.0.0.1", "user", "00" * 16)


# ---------- telling a blocked path from a rejected key ----------

def test_a_listening_bridge_answers_a_bare_client_hello(stub_bridge):
    """A DTLS server replies to a first ClientHello with a HelloVerifyRequest,
    and it does that before looking at any credential. That is what makes this
    usable as a path test: it answers even when our key is wrong."""
    from app.hue_stream import probe_stream_port
    state, how = probe_stream_port(stub_bridge["host"], stub_bridge["port"], timeout=3.0)
    assert state == "answered"
    assert "bytes" in how


def test_a_shut_port_reads_as_refused_not_as_a_broken_path():
    """ICMP port-unreachable is proof the path works: the datagram arrived and
    the reply routed home. Calling that a network failure is what sent the last
    round of debugging in the wrong direction."""
    from app.hue_stream import probe_stream_port
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((usable_stub_host(), 0))
    port = sock.getsockname()[1]
    sock.close()                      # free the port so nothing is behind it
    state, how = probe_stream_port(usable_stub_host(), port, timeout=1.0)
    assert state == "refused"
    assert "path is fine" in how


def test_the_client_hello_is_a_well_formed_dtls_record():
    from app.hue_stream import _client_hello
    hello = _client_hello()
    assert hello[0] == 0x16                       # handshake record
    assert hello[1:3] == b"\xfe\xfd"              # DTLS 1.2
    assert hello[3:5] == b"\x00\x00"              # epoch 0
    body_len = int.from_bytes(hello[11:13], "big")
    assert len(hello) == 13 + body_len            # length field agrees
    assert hello[13] == 0x01                      # client_hello
    # The suite the bridge insists on has to be offered, or it has no reason
    # to carry on past the first exchange.
    assert b"\x00\xa8" in hello


def test_the_staged_probe_reaches_server_hello_against_a_live_psk_server(stub_bridge):
    """A ServerHello proves the bridge accepted the offer and would have gone on
    to check a key — which is what separates "wrong key" from "wrong offer".
    Stopping at the first reply cannot: the identity is not sent until the fifth
    message of the flight."""
    from app.hue_stream import probe_handshake_stage
    stage, how = probe_handshake_stage(stub_bridge["host"], stub_bridge["port"], timeout=4.0)
    assert stage == "server-hello", f"{stage}: {how}"


def test_the_staged_probe_reports_a_shut_port_as_refused():
    from app.hue_stream import probe_handshake_stage
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((usable_stub_host(), 0))
    port = sock.getsockname()[1]
    sock.close()
    stage, _ = probe_handshake_stage(usable_stub_host(), port, timeout=1.0)
    assert stage == "refused"


def test_the_cookie_is_carried_into_the_second_client_hello():
    from app.hue_stream import _client_hello
    first = _client_hello()
    assert first[13 + 12 + 2 + 32 + 1] == 0            # cookie length: empty
    second = _client_hello(cookie=b"\xaa\xbb\xcc", message_seq=1)
    offset = 13 + 12 + 2 + 32 + 1                       # past version, random, session id
    assert second[offset] == 3
    assert second[offset + 1:offset + 4] == b"\xaa\xbb\xcc"
    # A second ClientHello has to advance the sequence, or the server treats it
    # as a retransmission of the first and answers with the cookie again.
    assert second[13 + 4:13 + 6] == (1).to_bytes(2, "big")


def test_a_hello_verify_request_that_is_not_one_is_rejected():
    from app.hue_stream import _parse_hello_verify
    assert _parse_hello_verify(b"") is None
    assert _parse_hello_verify(b"\x17" + bytes(40)) is None       # not a handshake
    assert _parse_hello_verify(b"\x16" + bytes(12) + b"\x02" + bytes(30)) is None  # ServerHello


def test_closing_says_goodbye_whichever_client_got_through(stub_bridge):
    """The bridge allows one session and holds it until told otherwise. Both
    transports have to say so — the minimal client sends its own close_notify,
    mbedtls needs shutdown() because its close() builds one and drops it."""
    stream = DtlsStream(stub_bridge["host"], stub_bridge["identity"], stub_bridge["key"],
                        port=stub_bridge["port"])
    stream.connect()
    assert stream.transport in ("minimal", "mbedtls")
    stream.close()
    assert stream._sock is None


def test_the_minimal_client_is_preferred(stub_bridge):
    """It offers one cipher suite and no extensions, which is the whole reason
    it exists: mbedtls cannot be told to send anything that small."""
    stream = DtlsStream(stub_bridge["host"], stub_bridge["identity"], stub_bridge["key"],
                        port=stub_bridge["port"])
    stream.connect()
    try:
        assert stream.transport == "minimal"
    finally:
        stream.close()


def test_closing_still_closes_when_the_goodbye_cannot_be_sent(stub_bridge, monkeypatch):
    """A bridge that stopped listening first must not turn tidying up into an
    error — the socket still has to go."""
    stream = DtlsStream(stub_bridge["host"], stub_bridge["identity"], stub_bridge["key"],
                        port=stub_bridge["port"])
    stream.connect()
    monkeypatch.setattr(type(stream._sock), "close",
                        lambda self: (_ for _ in ()).throw(OSError("gone")))
    stream.close()
    assert stream._sock is None


def test_closing_twice_is_harmless():
    stream = DtlsStream("10.0.0.5", "u", "00" * 16)
    stream.close()
    stream.close()


def test_a_routed_hop_to_the_bridge_is_noticed():
    """Streaming is the one part of the Hue API that wants the client on the
    bridge's own network. REST routes anywhere, so a split like this looks like
    a working setup right up until the stream — worth naming rather than
    leaving to be deduced."""
    from app.hue_stream import same_subnet_as_bridge
    assert same_subnet_as_bridge("192.168.50.5", "192.168.50.31") is True
    assert same_subnet_as_bridge("192.168.10.37", "192.168.50.31") is False
    assert same_subnet_as_bridge("172.17.0.44", "192.168.50.31") is False


def test_an_unparseable_address_says_nothing_rather_than_crying_wolf():
    from app.hue_stream import same_subnet_as_bridge
    assert same_subnet_as_bridge("", "192.168.50.31") is True
    assert same_subnet_as_bridge("not-an-ip", "192.168.50.31") is True


def test_the_local_address_is_reported_without_sending_anything(stub_bridge):
    from app.hue_stream import local_address_for
    assert local_address_for(stub_bridge["host"]) is not None


def test_the_standalone_probe_sends_the_same_bytes_as_the_app():
    """scripts/probe_stream.py carries its own copy of the protocol so it can be
    run on any machine that can see the bridge, with no checkout and no
    dependencies — the "does it work from the bridge's own network" test is
    worthless if the tool cannot get there. Duplication earns a test: if the two
    ever diverge, the probe stops describing what the app actually does."""
    import importlib.util
    from pathlib import Path

    from app.hue_stream import _client_hello

    path = Path(__file__).resolve().parent.parent / "scripts" / "probe_stream.py"
    spec = importlib.util.spec_from_file_location("probe_stream", path)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)

    assert probe.client_hello() == _client_hello()
    assert probe.client_hello(b"\xde\xad", 1) == _client_hello(b"\xde\xad", 1)
    assert probe.PSK_SUITE == b"\x00\xa8"
    assert probe.same_subnet("192.168.10.37", "192.168.50.31") is False
    assert probe.same_subnet("192.168.50.5", "192.168.50.31") is True


def test_a_container_address_is_not_mistaken_for_a_subnet_mismatch():
    """Behind Docker's NAT the bridge sees the host's address, not the
    container's, so comparing the container's proves nothing — and saying
    "different network" on that basis sends people rearranging a network that
    was never the problem."""
    from app.hue_stream import looks_translated
    assert looks_translated("172.17.0.44") is True
    assert looks_translated("172.31.255.1") is True
    # Real LANs live in the other private ranges; calling those translated
    # would be wrong far more often than right.
    assert looks_translated("192.168.10.37") is False
    assert looks_translated("10.0.0.5") is False
    assert looks_translated("not-an-ip") is False
