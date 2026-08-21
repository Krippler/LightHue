"""Hue Entertainment streaming: DTLS-PSK transport and HueStream framing.

The REST API this app otherwise speaks takes roughly ten commands a second for
the whole bridge, which is why a seven-bulb group flickers at about 1 Hz each.
The Entertainment API is the way out: one DTLS socket carries a frame for every
light in an area at once, so the cost of a frame stops growing with the number
of bulbs and the rate stops being divided between them.

Two things it asks for in return. The bridge only streams to an *entertainment
area*, which is set up in the Hue app and holds at most ten colour-capable
lights. And the credential is not the ordinary API key: pairing has to ask for
a client key as well, which is the DTLS pre-shared key.

Wire format, confirmed against two independent implementations
(music-assistant/hue-entertainment for v2, JakeBednard/node-phea for v1):

    16-byte header, big-endian throughout
      0   9  b"HueStream"
      9   1  version major (1 or 2)
     10   1  version minor (0)
     11   1  sequence number, wraps at 256
     12   2  reserved
     14   1  colour space: 0 RGB, 1 xy + brightness
     15   1  reserved

    v1 body: 9 bytes per light  -- 0x00, light id (2), R (2), G (2), B (2)
    v2 body: the area's 36-char ASCII UUID, then 7 bytes per channel
             -- channel id (1), R (2), G (2), B (2)
"""
import ipaddress
import logging
import socket

from .hue_client import parse_bridge_address

logger = logging.getLogger("game_hue_flicker.stream")

MAGIC = b"HueStream"
COLOUR_SPACE_RGB = 0x00
COLOUR_SPACE_XY = 0x01

STREAM_PORT = 2100
# The bridge accepts more, but Philips' own guidance for an entertainment area
# is 25 frames a second; past that you are paying radio time for frames the
# bulbs cannot act on.
MAX_STREAM_HZ = 25.0
# An area holds at most ten lights, so this is the bridge's ceiling, not ours.
MAX_AREA_LIGHTS = 10

# DTLS 1.2 with a pre-shared key is the only thing the bridge will talk.
DTLS_CIPHERS = ("TLS-PSK-WITH-AES-128-GCM-SHA256",)


def _header(version: int, sequence: int, colour_space: int) -> bytearray:
    out = bytearray(MAGIC)
    out.append(version & 0xFF)
    out.append(0x00)                      # minor
    out.append(sequence & 0xFF)
    out.extend(b"\x00\x00")               # reserved
    out.append(colour_space & 0xFF)
    out.append(0x00)                      # reserved
    return out


def _clamp16(value: int) -> int:
    return max(0, min(0xFFFF, int(value)))


def build_frame_v1(sequence: int, lights, colour_space: int = COLOUR_SPACE_RGB) -> bytes:
    """`lights` is (light_id, (r, g, b)) with 16-bit components."""
    out = _header(1, sequence, colour_space)
    for light_id, (r, g, b) in lights:
        out.append(0x00)                  # 0 = light, the only type there is
        out.extend(int(light_id).to_bytes(2, "big"))
        for component in (r, g, b):
            out.extend(_clamp16(component).to_bytes(2, "big"))
    return bytes(out)


def build_frame_v2(sequence: int, area_id: str, channels,
                   colour_space: int = COLOUR_SPACE_RGB) -> bytes:
    """`channels` is (channel_id, (r, g, b)) with 16-bit components."""
    out = _header(2, sequence, colour_space)
    out.extend(area_id.encode("ascii"))   # 36 chars, dashes and all
    for channel_id, (r, g, b) in channels:
        out.append(int(channel_id) & 0xFF)
        for component in (r, g, b):
            out.extend(_clamp16(component).to_bytes(2, "big"))
    return bytes(out)


def hue_sat_bri_to_rgb16(hue: int | None, sat: int | None, bri: int) -> tuple[int, int, int]:
    """Hue's own colour numbers to the 16-bit RGB the stream carries.

    Brightness is the only thing a lightstyle actually animates, so it scales
    the result rather than being sent separately: the stream has no brightness
    channel of its own in RGB mode. With no colour named, the light runs white
    and the pattern reads as pure brightness — which is what an unframed engine
    lightstyle is.
    """
    value = max(0, min(254, int(bri))) / 254.0
    if hue is None or sat is None:
        level = _clamp16(round(value * 0xFFFF))
        return (level, level, level)

    h = (max(0, min(65535, int(hue))) / 65536.0) * 6.0
    s = max(0, min(254, int(sat))) / 254.0
    sector = int(h) % 6
    offset = h - int(h)
    p = value * (1.0 - s)
    q = value * (1.0 - s * offset)
    t = value * (1.0 - s * (1.0 - offset))
    r, g, b = (
        (value, t, p), (q, value, p), (p, value, t),
        (p, q, value), (t, p, value), (value, p, q),
    )[sector]
    return (_clamp16(round(r * 0xFFFF)),
            _clamp16(round(g * 0xFFFF)),
            _clamp16(round(b * 0xFFFF)))


class StreamError(Exception):
    pass


# TLS_PSK_WITH_AES_128_GCM_SHA256, the only suite the bridge accepts.
_PSK_SUITE = b"\x00\xa8"


def _client_hello(cookie: bytes = b"", message_seq: int = 0) -> bytes:
    """A bare DTLS 1.2 ClientHello, hand-rolled.

    Used to answer one question that the library cannot: did *anything* come
    back from the bridge. A DTLS server replies to a first ClientHello with a
    HelloVerifyRequest carrying a cookie, and it does that before it looks at
    any credential — so a reply proves the UDP path works even when the key is
    wrong, and silence proves it does not even when the key is right.
    """
    body = bytearray()
    body += b"\xfe\xfd"                    # client_version: DTLS 1.2
    body += bytes(32)                       # random; content is irrelevant here
    body += b"\x00"                         # session id: empty
    body += bytes([len(cookie)]) + cookie   # empty first time; the server sends one
    body += len(_PSK_SUITE).to_bytes(2, "big") + _PSK_SUITE
    body += b"\x01\x00"                     # one compression method: null
    body += b"\x00\x00"                     # no extensions

    handshake = bytearray()
    handshake += b"\x01"                     # msg_type: client_hello
    handshake += len(body).to_bytes(3, "big")
    handshake += message_seq.to_bytes(2, "big")
    handshake += b"\x00\x00\x00"             # fragment_offset
    handshake += len(body).to_bytes(3, "big")  # fragment_length
    handshake += body

    record = bytearray()
    record += b"\x16"                        # ContentType: handshake
    record += b"\xfe\xfd"                    # DTLS 1.2
    record += b"\x00\x00"                    # epoch
    record += message_seq.to_bytes(6, "big")   # record sequence number
    record += len(handshake).to_bytes(2, "big")
    record += handshake
    return bytes(record)


def _parse_hello_verify(datagram: bytes) -> bytes | None:
    """Pull the cookie out of a HelloVerifyRequest, if that is what this is."""
    if len(datagram) < 25 or datagram[0] != 0x16:      # not a handshake record
        return None
    body = datagram[13:]                                # past the record header
    if not body or body[0] != 0x03:                     # 3 = hello_verify_request
        return None
    payload = body[12:]                                 # past the handshake header
    if len(payload) < 3:
        return None
    length = payload[2]                                 # server_version, then cookie
    return payload[3:3 + length]


def probe_handshake_stage(host: str, port: int = STREAM_PORT,
                          timeout: float = 4.0) -> tuple[str, str]:
    """Carry a handshake as far as the credentials, without offering any.

    The PSK identity is not sent until the ClientKeyExchange, which is the fifth
    message. Everything before it is the same whether our key is right or
    hopeless — so a probe that stops at the first reply cannot tell a bridge
    that dislikes our key from one that dislikes our ClientHello.

    Going one flight further splits them. A ServerHello means the bridge
    accepted the offer and would have gone on to check a key, so a real
    handshake failing after that is about the credentials. No ServerHello means
    it rejected the ClientHello itself, and the credentials never came into it.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        sock.send(_client_hello())
        first = sock.recv(4096)
        cookie = _parse_hello_verify(first)
        if cookie is None:
            kind = f"0x{first[0]:02x}" if first else "nothing"
            return "no-hello-verify", f"first reply was {kind}, not a HelloVerifyRequest"

        sock.send(_client_hello(cookie=cookie, message_seq=1))
        second = sock.recv(4096)
        if not second:
            return "hello-verify-only", "cookie accepted, then nothing came back"
        if second[0] == 0x15:
            # An alert here names the objection outright.
            level, desc = (second[13], second[14]) if len(second) > 14 else (0, 0)
            return "alert", f"the bridge sent alert {desc} (level {level}) after our ClientHello"
        if second[0] != 0x16:
            return "unexpected", f"reply was 0x{second[0]:02x}, not a handshake"
        if len(second) > 13 and second[13] == 0x02:
            return "server-hello", (
                "the bridge accepted our ClientHello and answered ServerHello, so it "
                "would have gone on to check a key"
            )
        return "handshake-other", f"handshake message 0x{second[13]:02x}"
    except TimeoutError:
        return "silent", "nothing came back"
    except ConnectionRefusedError:
        return "refused", "the port is shut (ICMP port unreachable), so the path is fine"
    except OSError as e:
        return "error", f"could not send: {e}"
    finally:
        sock.close()


def same_subnet_as_bridge(local_ip: str, bridge_ip: str, prefix: int = 24) -> bool:
    """Are we on the bridge's own network?

    Philips documents entertainment streaming as needing the client on the same
    network as the bridge, and it is the only part of the API with that
    constraint — REST routes anywhere, which is why a split like this can look
    like a working setup right up until the stream. A /24 is an assumption, but
    a routed hop between two /24s is exactly the case worth naming.
    """
    try:
        local = ipaddress.ip_network(f"{local_ip}/{prefix}", strict=False)
        bridge = ipaddress.ip_network(f"{bridge_ip}/{prefix}", strict=False)
    except ValueError:
        return True         # unparseable: say nothing rather than mislead
    return local == bridge


def local_address_for(host: str, port: int = STREAM_PORT) -> str | None:
    """Which local address a datagram to the bridge would leave from."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((host, port))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def probe_stream_port(host: str, port: int = STREAM_PORT,
                      timeout: float = 3.0) -> tuple[str, str]:
    """Send a bare ClientHello and report, precisely, what came back.

    Three outcomes that a handshake timeout cannot tell apart:

    * ``answered``  something spoke DTLS. The path carries UDP both ways and
      the bridge is listening, so a handshake that still fails is about the
      credentials, not the network.
    * ``refused``   an ICMP port-unreachable came back. That is *also* proof
      the path works — the datagram arrived and the reply routed home — but
      nothing is bound to the port right now. The bridge only opens it while it
      holds the area, so this is the expected answer when nothing is claimed
      and a real finding when something is.
    * ``silent``    nothing at all. Either the datagram never arrived or the
      reply never came back: the only one of the three that is a network
      problem.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        sock.send(_client_hello())
        data = sock.recv(2048)
        if not data:
            return "silent", "the bridge closed without answering"
        kind = {0x16: "handshake", 0x15: "alert"}.get(data[0], f"0x{data[0]:02x}")
        return "answered", f"answered with {len(data)} bytes ({kind})"
    except TimeoutError:
        return "silent", "nothing came back"
    except ConnectionRefusedError:
        # ICMP port-unreachable: the packet got there and the reply got home.
        return "refused", "the port is shut (ICMP port unreachable), so the path is fine"
    except OSError as e:
        return "error", f"could not send: {e}"
    finally:
        sock.close()


class DtlsStream:
    """A DTLS-PSK datagram socket to the bridge's entertainment endpoint.

    Blocking, and driven from a worker thread: mbedtls has no asyncio binding,
    and a frame is a single datagram with no reply to wait for, so there is
    nothing here worth an event loop.
    """

    def __init__(self, bridge_ip: str, username: str, client_key: str,
                 port: int | None = None):
        # The stored address can carry the REST port ("192.168.1.23:8080"), and
        # streaming does not use it — the entertainment endpoint is its own
        # port. Take the host and leave the rest behind, or the port ends up
        # inside the hostname and the connect fails a name lookup.
        self.bridge_ip, _rest_port = parse_bridge_address(bridge_ip)
        self.username = username
        try:
            self.psk = bytes.fromhex(client_key)
        except ValueError as e:
            raise StreamError("The bridge's client key isn't valid hex") from e
        # Resolved now rather than baked into the signature at import, so the
        # module-level default stays the one source of truth.
        self.port = STREAM_PORT if port is None else port
        # Which local address the socket actually went out from. The bridge
        # replies to whatever it saw, so on a multi-homed host this is the first
        # thing worth knowing when nothing comes back.
        self.local_address = None
        self._sock = None

    def connect(self, timeout: float = 6.0):
        """One handshake attempt against the bridge.

        Deliberately a single attempt. Retrying in here was worse than useless:
        the bridge drops its claim on the area after about ten seconds without a
        handshake, so a second and third try land on an area that is no longer
        listening and can only fail. Retrying is the caller's job, because only
        the caller can claim the area again first.
        """
        try:
            from mbedtls import tls
        except ImportError as e:      # pragma: no cover - dependency is declared
            raise StreamError(
                "python-mbedtls isn't installed, so streaming can't start"
            ) from e

        config = tls.DTLSConfiguration(
            pre_shared_key=(self.username, self.psk),
            ciphers=DTLS_CIPHERS,
            validate_certificates=False,
        )
        raw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        raw.settimeout(timeout)
        sock = tls.ClientContext(config).wrap_socket(raw, server_hostname=None)
        try:
            sock.connect((self.bridge_ip, self.port))
            self.local_address = sock.getsockname()
            sock.do_handshake()
        except Exception as e:
            sock.close()
            raise StreamError(f"Could not open the entertainment stream: {e}") from e
        self._sock = sock

    def send(self, frame: bytes):
        if self._sock is None:
            raise StreamError("Stream isn't open")
        self._sock.send(frame)

    def close(self):
        """End the session and tell the bridge so.

        The telling is the part that matters. mbedtls's close() builds a
        close_notify into its outgoing buffer and then shuts the socket without
        ever sending it; shutdown() is the call that puts it on the wire. A
        bridge that never hears one keeps the session on its books, and since it
        allows only one at a time it then ignores the next handshake — which is
        exactly the shape of streaming working once and never again until the
        bridge times the ghost session out by itself.
        """
        if self._sock is None:
            return
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            # Already gone, or the bridge stopped listening first. Closing is
            # still worth doing, and a failure to say goodbye is not an error
            # worth surfacing.
            logger.debug("Could not send close_notify to the bridge", exc_info=True)
        try:
            self._sock.close()
        except Exception:
            logger.debug("Could not close the stream socket", exc_info=True)
        finally:
            self._sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()
