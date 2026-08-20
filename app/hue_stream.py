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
import socket
import time

from .hue_client import parse_bridge_address

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
        self._sock = None

    def connect(self, timeout: float = 4.0, attempts: int = 3):
        """Handshake with the bridge, retrying a few times.

        The bridge opens port 2100 only once the area has been handed to the
        stream over REST, and it does not open it the instant the REST call
        returns — a first handshake landing a moment early gets no answer at
        all, which surfaces as a timeout rather than a refusal. Retrying costs
        a few seconds and turns a race into a non-event.
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
        last = None
        for attempt in range(max(1, attempts)):
            raw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            raw.settimeout(timeout)
            sock = tls.ClientContext(config).wrap_socket(raw, server_hostname=None)
            try:
                sock.connect((self.bridge_ip, self.port))
                sock.do_handshake()
            except Exception as e:
                last = e
                sock.close()
                if attempt + 1 < max(1, attempts):
                    time.sleep(0.5)
                continue
            self._sock = sock
            return
        raise StreamError(f"Could not open the entertainment stream: {last}") from last

    def send(self, frame: bytes):
        if self._sock is None:
            raise StreamError("Stream isn't open")
        self._sock.send(frame)

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()
