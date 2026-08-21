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
