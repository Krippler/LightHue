"""Driving an entertainment area over the DTLS stream.

Deliberately separate from FlickerEngine rather than folded into it. The REST
path is one asyncio task per light, each taking a token from a shared budget;
this is one socket carrying every light in the area, at a fixed frame rate, with
no budget to share because a frame costs the same whether it holds one light or
ten. The two have almost nothing in common but the pattern maths, and the REST
path works — there was no reason to put it at risk.

Frames go out at a steady rate regardless of the pattern's own speed. The
bridge drops a stream that goes quiet, and a constant flow is what lets every
light in the area change on the same frame instead of whenever its turn came
round. The pattern is sampled against that clock, so `hz` up to the stream rate
is honoured exactly, for all ten lights at once.
"""
import logging
import threading
import time

from .hue_stream import (
    MAX_STREAM_HZ,
    DtlsStream,
    StreamError,
    build_frame_v1,
    build_frame_v2,
    hue_sat_bri_to_rgb16,
)
from .patterns import level_for_char

logger = logging.getLogger("game_hue_flicker.stream")

LIVE_FIELDS = ("sequence", "pattern_id", "hz", "min_bri", "max_bri", "hue", "sat")

# How often a frame goes on the wire. Also the ceiling on a pattern's speed:
# you cannot show frames you do not send.
FRAME_RATE_HZ = MAX_STREAM_HZ
FRAME_INTERVAL = 1.0 / FRAME_RATE_HZ

# The bridge gives up on a stream that stops sending. Frames go out steadily
# anyway, so this only matters if the sender thread stalls.
GIVE_UP_AFTER_FAILURES = 40


def level_at(sequence: str, hz: float, epoch: float, now: float) -> float:
    """Where the pattern is at `now`, as 0..1.

    Derived from the epoch rather than counted per frame, for the same reason
    the REST path does it: every light is answering the same clock, so they
    land on the same letter at the same moment.
    """
    seq = sequence or "m"
    interval = 1.0 / max(0.1, min(hz, FRAME_RATE_HZ))
    index = int(max(0.0, now - epoch) / interval)
    return level_for_char(seq[index % len(seq)])


def rgb_for(state: dict, now: float) -> tuple[int, int, int]:
    level = level_at(state["sequence"], state["hz"], state["epoch"], now)
    lo, hi = state["min_bri"], state["max_bri"]
    bri = int(round(lo + level * (hi - lo)))
    return hue_sat_bri_to_rgb16(state.get("hue"), state.get("sat"), max(1, min(254, bri)))


class StreamEngine:
    """Runs one entertainment area. The bridge only streams to one at a time."""

    def __init__(self, on_change=None, on_stopped=None):
        self._on_change = on_change or (lambda: None)
        # Fired with the area id and its lights whenever the sender stops for
        # any reason,
        # including one nobody asked for. The bridge keeps an area claimed
        # until it is told otherwise, so a sender that dies quietly would
        # leave those lights answering to nothing — not this console, not the
        # Hue app — until the bridge was restarted.
        self._on_stopped = on_stopped or (lambda area_id, light_ids: None)
        self._lock = threading.Lock()
        self._state: dict | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._error: str | None = None
        self._frames_sent = 0

    # ---------- state ----------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        with self._lock:
            state = dict(self._state) if self._state else None
        return {
            "running": self.running,
            "area_id": state["area_id"] if state else None,
            "light_ids": list(state["light_ids"]) if state else [],
            "settings": state,
            "frames_sent": self._frames_sent,
            # Which DTLS client got through: "minimal" is ours, "mbedtls" the
            # library's fuller ClientHello.
            "transport": getattr(self, "_transport", None),
            # Which HueStream framing is going out: v2 addresses channels
            # within an entertainment configuration, v1 addresses light ids.
            "protocol": getattr(self, "_protocol", None),
            "error": self._error,
            # Unlike the REST path there is nothing to divide: every light in
            # the area is in the same frame.
            "effective_hz": min(state["hz"], FRAME_RATE_HZ) if state else None,
            "max_hz": FRAME_RATE_HZ,
        }

    def update(self, **changes) -> bool:
        """Retune the running area in place."""
        with self._lock:
            if self._state is None:
                return False
            for key, value in changes.items():
                if key not in LIVE_FIELDS:
                    continue
                # Colour is the one field where None is a request rather than an
                # omission. A frame carries the colour every time, so a stream
                # can genuinely go back to having none — unlike a bulb over
                # REST, where there is no call that undoes a colour.
                if value is None and key not in ("hue", "sat"):
                    continue
                self._state[key] = value
        return True

    # ---------- lifecycle ----------

    def start(self, bridge_ip: str, username: str, client_key: str,
              area_id: str, light_ids: list[str], sequence: str, pattern_id: str,
              hz: float, min_bri: int, max_bri: int,
              hue: int | None, sat: int | None, connect_timeout: float = 6.0,
              area_uuid: str | None = None, channels: list[int] | None = None,
              transport: str | None = None):
        """Open the stream and start sending. The caller activates the area
        over REST first — the bridge ignores port 2100 until it has."""
        self.stop()
        self._error = None
        self._frames_sent = 0
        # Connect before recording any state: a failed handshake would
        # otherwise leave an area id behind with nothing running under it, and
        # the next status push would claim a stream that does not exist.
        stream = DtlsStream(bridge_ip, username, client_key)
        stream.connect(timeout=connect_timeout, transport=transport)
        with self._lock:
            self._state = {
                "area_id": area_id,
                "light_ids": [str(x) for x in light_ids],
                "sequence": sequence,
                "pattern_id": pattern_id,
                "hz": float(hz),
                "min_bri": int(min_bri),
                "max_bri": int(max_bri),
                "hue": hue,
                "sat": sat,
                "epoch": time.monotonic(),
                # Set when the area is a v2 entertainment configuration. Its
                # frames address channels within the area rather than light ids,
                # and carry the area's UUID in the header.
                "area_uuid": area_uuid,
                "channels": list(channels) if channels else None,
            }
        self._transport = stream.transport
        # Kept beside the transport rather than derived from the live state, so
        # the two survive a stop together. Reading one from state meant that
        # after a run the report showed which client had connected and how many
        # frames went out, but not which framing carried them — the one part of
        # a finished run that nothing else records.
        self._protocol = 2 if area_uuid else 1
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, args=(stream,), name="hue-stream", daemon=True)
        self._thread.start()
        self._on_change()

    def stop(self):
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=3.0)
            self._thread = None
        with self._lock:
            self._state = None
        self._on_change()

    def area_id(self) -> str | None:
        with self._lock:
            return self._state["area_id"] if self._state else None

    def light_ids(self) -> list[str]:
        with self._lock:
            return list(self._state["light_ids"]) if self._state else []

    # ---------- the sender ----------

    def _run(self, stream: DtlsStream):
        sequence_id = 0
        failures = 0
        next_frame = time.monotonic()
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                with self._lock:
                    state = dict(self._state) if self._state else None
                if state is None:
                    return

                rgb = rgb_for(state, now)
                # Every light in the area carries the same value: this is one
                # effect across a room, not ten independent ones.
                if state.get("area_uuid") and state.get("channels"):
                    frame = build_frame_v2(
                        sequence_id, state["area_uuid"],
                        [(channel, rgb) for channel in state["channels"]])
                else:
                    frame = build_frame_v1(
                        sequence_id, [(int(lid), rgb) for lid in state["light_ids"]])
                sequence_id = (sequence_id + 1) & 0xFF

                try:
                    stream.send(frame)
                    self._frames_sent += 1
                    failures = 0
                except Exception as e:
                    failures += 1
                    if failures == 1:
                        logger.warning("Entertainment frame failed: %s", e)
                    if failures >= GIVE_UP_AFTER_FAILURES:
                        self._error = f"Stream stopped: {e}"
                        logger.error("Giving up on the entertainment stream: %s", e)
                        return

                # Pace against a running deadline rather than sleeping a fixed
                # interval, so a slow send costs one frame instead of dragging
                # the whole pattern out of time.
                next_frame += FRAME_INTERVAL
                delay = next_frame - time.monotonic()
                if delay < -FRAME_INTERVAL:
                    next_frame = time.monotonic()      # fell behind; re-anchor
                    delay = 0.0
                if delay > 0:
                    self._stop.wait(delay)
        finally:
            stream.close()
            area, lights = self.area_id(), self.light_ids()
            # Told even when the sender is exiting because it was asked to:
            # releasing an area twice is harmless, never releasing one is not.
            if area:
                try:
                    self._on_stopped(area, lights)
                except Exception:
                    logger.exception("Could not hand entertainment area %s back", area)
            self._on_change()


__all__ = ["StreamEngine", "StreamError", "FRAME_RATE_HZ", "level_at", "rgb_for"]
