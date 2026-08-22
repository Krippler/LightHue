"""The hand-rolled DTLS 1.2 PSK client, against a real DTLS-PSK server.

Written because python-mbedtls cannot be told to send a small ClientHello: it
offers an SCSV pseudo-suite beside the real one plus signature_algorithms,
encrypt_then_mac, extended_master_secret and session_ticket, with no way to
turn any of it off. This one offers a single suite and no extensions.

A handshake is not something to take on trust, so these run it end to end
against mbedtls acting as the server — if the derivation, the Finished hash or
the record layer were wrong, the server would reject it and nothing would
decrypt.
"""
import socket
import struct
import threading
import time

import pytest

from app.dtls_psk import (
    CLIENT_HELLO,
    DTLS_1_2,
    HANDSHAKE,
    PSK_AES_128_GCM_SHA256,
    DtlsError,
    DtlsPskClient,
    prf,
    psk_premaster,
)

IDENTITY = "stream-user"
KEY = bytes.fromhex("0123456789abcdef0123456789abcdef")


def local_ipv4() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        return probe.getsockname()[0]
    finally:
        probe.close()


@pytest.fixture
def psk_server():
    tls = pytest.importorskip("mbedtls.tls")
    received, ready, errors = [], threading.Event(), []
    raw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    config = tls.DTLSConfiguration(
        pre_shared_key_store={IDENTITY: KEY},
        ciphers=("TLS-PSK-WITH-AES-128-GCM-SHA256",), validate_certificates=False)
    sock = tls.ServerContext(config).wrap_socket(raw)
    host = local_ipv4()
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
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")
            return
        while True:
            try:
                received.append(conn.recv(4096))
            except Exception:
                return

    threading.Thread(target=serve, daemon=True).start()
    ready.wait(2)
    time.sleep(0.2)
    yield {"host": host, "port": port, "received": received, "errors": errors}
    sock.close()


# ---------- the crypto, checked on its own ----------

def test_the_psk_premaster_secret_is_shaped_as_the_rfc_says():
    """RFC 4279 2: N zero bytes then the key, each behind a 16-bit length."""
    out = psk_premaster(b"\xaa\xbb\xcc")
    assert out == struct.pack(">H", 3) + b"\x00\x00\x00" + struct.pack(">H", 3) + b"\xaa\xbb\xcc"


def test_the_prf_is_deterministic_and_the_length_asked_for():
    assert len(prf(b"secret", b"master secret", b"seed", 48)) == 48
    assert len(prf(b"secret", b"key expansion", b"seed", 40)) == 40
    assert prf(b"s", b"l", b"x", 16) == prf(b"s", b"l", b"x", 16)
    assert prf(b"s", b"l", b"x", 16) != prf(b"s", b"l", b"y", 16)
    assert prf(b"a", b"l", b"x", 16) != prf(b"b", b"l", b"x", 16)
    # RFC 5246 5 defines PRF(secret, label, seed) as P_hash(secret, label +
    # seed), so the split between label and seed genuinely does not matter.
    # Asserting otherwise would be asserting a bug.
    assert prf(b"s", b"aa", b"b", 16) == prf(b"s", b"a", b"ab", 16)


# ---------- the ClientHello, which is the whole point ----------

def test_the_client_hello_offers_one_suite_and_no_extensions():
    client = DtlsPskClient("10.0.0.5", 2100, IDENTITY, KEY)
    client.client_random = bytes(32)
    body = client._client_hello_body(b"")
    assert body[:2] == DTLS_1_2
    offset = 2 + 32 + 1 + 1                       # version, random, session id, cookie
    assert body[offset:offset + 2] == struct.pack(">H", 2)
    assert body[offset + 2:offset + 4] == PSK_AES_128_GCM_SHA256
    assert body[offset + 4:] == b"\x01\x00" + b"\x00\x00"   # null compression, no extensions


def test_the_cookie_comes_back_in_the_second_hello():
    client = DtlsPskClient("10.0.0.5", 2100, IDENTITY, KEY)
    client.client_random = bytes(32)
    body = client._client_hello_body(b"\xde\xad\xbe\xef")
    assert body[2 + 32] == 0                       # still no session id
    assert body[2 + 32 + 1] == 4
    assert body[2 + 32 + 2:2 + 32 + 6] == b"\xde\xad\xbe\xef"


def test_records_and_handshakes_are_split_the_way_a_flight_arrives():
    """A datagram carries several records and a record several handshake
    messages; a server flight routinely does both."""
    one = struct.pack(">B", HANDSHAKE) + DTLS_1_2 + bytes(8) + struct.pack(">H", 3) + b"abc"
    two = struct.pack(">B", HANDSHAKE) + DTLS_1_2 + bytes(8) + struct.pack(">H", 2) + b"de"
    assert DtlsPskClient._split_records(one + two) == [(HANDSHAKE, b"abc"), (HANDSHAKE, b"de")]

    body = b"\x01\x02"
    msg = struct.pack(">B", CLIENT_HELLO) + len(body).to_bytes(3, "big") + bytes(8) + body
    parsed = DtlsPskClient._split_handshakes(msg + msg)
    assert [p[0] for p in parsed] == [CLIENT_HELLO, CLIENT_HELLO]
    assert [p[2] for p in parsed] == [body, body]


# ---------- the handshake, end to end ----------

def test_it_handshakes_and_its_frames_decrypt(psk_server):
    client = DtlsPskClient(psk_server["host"], psk_server["port"], IDENTITY, KEY, timeout=5)
    client.connect()
    for i in range(3):
        client.send(b"HueStream-frame-%d" % i)
        time.sleep(0.05)
    time.sleep(0.5)
    client.close()

    assert psk_server["errors"] == []
    # The server decrypting these is the real assertion: it could not, unless
    # the master secret, the key block, the Finished hash and the GCM nonce and
    # AAD were all right.
    assert psk_server["received"] == [b"HueStream-frame-0", b"HueStream-frame-1",
                                      b"HueStream-frame-2"]


def test_a_key_the_server_rejects_is_reported_rather_than_looking_fine(psk_server):
    """Nothing is read back once streaming starts, so without waiting for the
    server's confirmation a rejected key looks exactly like a good one and the
    first symptom is lights that never change."""
    client = DtlsPskClient(psk_server["host"], psk_server["port"], IDENTITY,
                           bytes.fromhex("ff" * 16), timeout=3)
    with pytest.raises(DtlsError, match="does not recognise|never confirmed|rejected"):
        client.connect()


def test_sending_before_connecting_is_an_error():
    with pytest.raises(DtlsError, match="not connected"):
        DtlsPskClient("10.0.0.5", 2100, IDENTITY, KEY).send(b"x")


def test_closing_an_unopened_client_is_harmless():
    DtlsPskClient("10.0.0.5", 2100, IDENTITY, KEY).close()


def test_the_sample_first_flight_is_the_real_opening_datagram():
    """It exists to be compared against a packet capture, so it has to be the
    same bytes connect() sends — a plausible-looking sample would send whoever
    reads the capture after a difference that was never on the wire."""
    from app.dtls_psk import (
        CLIENT_HELLO,
        HANDSHAKE,
        PSK_AES_128_GCM_SHA256,
        first_flight,
    )

    flight = first_flight("some-application-key")
    assert flight[0] == HANDSHAKE
    # DTLS 1.0 at the record layer, 1.2 in the hello body: the split RFC 6347
    # asks for before a version is agreed, and what OpenSSL puts on the wire.
    assert flight[1:3] == b"\xfe\xff"
    assert flight[3:5] == b"\x00\x00"                  # epoch 0
    assert flight[5:11] == b"\x00" * 6                 # first record of the flight
    body = flight[13:]
    assert int.from_bytes(flight[11:13], "big") == len(body)
    assert body[0] == CLIENT_HELLO
    # One cipher suite and no extensions is the whole point of this client.
    assert PSK_AES_128_GCM_SHA256 in body
    assert body.endswith(b"\x01\x00\x00\x00")
    # No cookie yet: the bridge answers the first ClientHello with one.
    assert body[12 + 2 + 32] == 0                      # empty session id
    assert body[12 + 2 + 32 + 1] == 0                  # empty cookie


def _hello_verify(cookie: bytes) -> bytes:
    """A HelloVerifyRequest, framed the way the bridge frames one."""
    from app.dtls_psk import DTLS_1_2, HANDSHAKE, HELLO_VERIFY_REQUEST
    body = DTLS_1_2 + bytes([len(cookie)]) + cookie
    handshake = (bytes([HELLO_VERIFY_REQUEST]) + len(body).to_bytes(3, "big")
                 + (0).to_bytes(2, "big") + (0).to_bytes(3, "big")
                 + len(body).to_bytes(3, "big") + body)
    return (bytes([HANDSHAKE]) + DTLS_1_2 + (0).to_bytes(2, "big")
            + (0).to_bytes(6, "big") + len(handshake).to_bytes(2, "big") + handshake)


def _record_seq(datagram: bytes) -> int:
    return int.from_bytes(datagram[5:11], "big")


def test_the_cookie_reply_does_not_reuse_a_record_sequence_number():
    """A repeated record number in one epoch is a replay, and gets dropped.

    The server keeps an anti-replay window over the record sequence number, so
    resending at a number already used in epoch 0 means the second ClientHello
    is discarded with no alert and no reply — indistinguishable, from the
    client, from a server that answered the first hello and then died.

    Checked on the bytes rather than against a library server: an mbedtls stub
    re-accepts for the cookie exchange and so never sees the reuse at all,
    which is exactly how this survived a suite that already handshakes for real.
    """
    import socket
    import threading

    from app.dtls_psk import DtlsError, DtlsPskClient

    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.settimeout(5)
    seen = []

    def serve():
        datagram, peer = server.recvfrom(4096)
        seen.append(datagram)
        server.sendto(_hello_verify(b"\xa5" * 32), peer)
        datagram, _ = server.recvfrom(4096)
        seen.append(datagram)          # and then say nothing, as the bridge did

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    client = DtlsPskClient(*server.getsockname(), "user", b"\x00" * 16, timeout=1.5)
    with pytest.raises(DtlsError, match="never sent a ServerHello"):
        client.connect()               # no ServerHello ever comes; that is fine
    thread.join(5)
    server.close()

    assert len(seen) == 2, "the client never sent the cookie back"
    assert _record_seq(seen[0]) == 0
    assert _record_seq(seen[1]) == 1, "flight 2 reused flight 1's record number"
    # message_seq does restart at 1 for the second ClientHello (RFC 6347 4.2.2);
    # the two counters are separate and only one of them carries over.
    assert int.from_bytes(seen[1][17:19], "big") == 1


def test_the_server_hello_suite_is_read_past_the_session_id():
    """ServerHello puts a variable-length session id before the cipher suite, so
    a fixed offset reads the wrong two bytes whenever the server resumes."""
    from app.dtls_psk import server_hello_suite

    def server_hello(session_id: bytes) -> bytes:
        return (b"\xfe\xfd" + bytes(32) + bytes([len(session_id)]) + session_id
                + b"\x00\xa8" + b"\x00")

    assert server_hello_suite(server_hello(b"")) == b"\x00\xa8"
    assert server_hello_suite(server_hello(b"\x01" * 32)) == b"\x00\xa8"
    assert server_hello_suite(b"\xfe\xfd") is None


def test_a_suite_this_client_cannot_speak_is_named_not_swallowed():
    """One record layer is implemented here, so another suite has to fail — but
    it fails saying which, because that is the whole content of the report."""
    import socket
    import threading

    from app.dtls_psk import (
        DTLS_1_2,
        HANDSHAKE,
        SERVER_HELLO,
        SERVER_HELLO_DONE,
        DtlsError,
        DtlsPskClient,
    )

    def handshake_record(msg_type: int, body: bytes, seq: int) -> bytes:
        message = (bytes([msg_type]) + len(body).to_bytes(3, "big")
                   + seq.to_bytes(2, "big") + (0).to_bytes(3, "big")
                   + len(body).to_bytes(3, "big") + body)
        return (bytes([HANDSHAKE]) + DTLS_1_2 + (0).to_bytes(2, "big")
                + seq.to_bytes(6, "big") + len(message).to_bytes(2, "big") + message)

    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.settimeout(5)

    def serve():
        _, peer = server.recvfrom(4096)
        server.sendto(_hello_verify(b"\xa5" * 32), peer)
        server.recvfrom(4096)
        # AES-256-GCM, which this client has no record layer for.
        hello = b"\xfe\xfd" + bytes(32) + b"\x00" + b"\x00\xa9" + b"\x00"
        server.sendto(handshake_record(SERVER_HELLO, hello, 0), peer)
        server.sendto(handshake_record(SERVER_HELLO_DONE, b"", 1), peer)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    client = DtlsPskClient(*server.getsockname(), "user", b"\x00" * 16, timeout=2)
    with pytest.raises(DtlsError, match="0x00a9"):
        client.connect()
    thread.join(5)
    server.close()


def test_a_dropped_flight_is_sent_again():
    """DTLS runs over a protocol that loses things, so resending is the client's
    job — and this bridge drops the first ClientHello carrying a cookie and
    answers the second. A client that sends each flight once stops exactly
    there, which is how this failed while OpenSSL walked through it.
    """
    import socket
    import threading

    from app.dtls_psk import DtlsError, DtlsPskClient

    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.settimeout(10)
    seen = []

    def serve():
        datagram, peer = server.recvfrom(4096)
        seen.append(datagram)
        server.sendto(_hello_verify(b"\xa5" * 32), peer)
        # Ignore the first cookie'd hello, exactly as the bridge does.
        seen.append(server.recvfrom(4096)[0])
        seen.append(server.recvfrom(4096)[0])

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    client = DtlsPskClient(*server.getsockname(), "user", b"\x00" * 16, timeout=6)
    with pytest.raises(DtlsError):
        client.connect()          # no ServerHello ever comes; the resend is the point
    thread.join(10)
    server.close()

    assert len(seen) == 3, "the cookie'd ClientHello was never resent"
    # Same handshake message both times — it is the same message.
    assert seen[1][13:] == seen[2][13:]
    # New record sequence number, or the server drops the resend as a replay.
    assert _record_seq(seen[2]) > _record_seq(seen[1])
