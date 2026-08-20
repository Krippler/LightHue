import asyncio
import logging
import time
from collections.abc import Callable

from .hue_client import HueClient
from .patterns import level_for_char

logger = logging.getLogger("flicker_engine")

# How many sends in a row may fail before a light is written off. A bulb that
# has been unplugged or removed from the bridge otherwise keeps its loop
# forever, taking rate-limiter slots from lights that can still answer.
GIVE_UP_AFTER_FAILURES = 40

# Settings a running loop will pick up without being restarted.
LIVE_FIELDS = ("sequence", "pattern_id", "hz", "min_bri", "max_bri",
               "hue", "sat", "transition_ms")


def restorable(state: dict) -> dict:
    """Pull the parts of a Hue light state worth putting back afterwards.

    colormode says which of hue/sat, xy or ct the bulb is actually using;
    the bridge reports all three regardless, and sending the wrong one would
    change the colour rather than restore it.
    """
    snapshot = {"on": bool(state.get("on", False))}
    if not snapshot["on"]:
        return snapshot          # an off bulb has no colour worth keeping
    if "bri" in state:
        snapshot["bri"] = int(state["bri"])
    mode = state.get("colormode")
    if mode == "hs" and "hue" in state and "sat" in state:
        snapshot["hue"], snapshot["sat"] = int(state["hue"]), int(state["sat"])
    elif mode == "xy" and isinstance(state.get("xy"), list) and len(state["xy"]) == 2:
        snapshot["xy"] = [float(state["xy"][0]), float(state["xy"][1])]
    elif mode == "ct" and "ct" in state:
        snapshot["ct"] = int(state["ct"])
    return snapshot


# Never let a burst exceed a second's worth of commands, however many lights
# are running.
MAX_BURST = 10

# Fraction of the budget a group is allowed to spend. Running flat out against
# the ceiling means every send has to wait for the one before, which forces a
# group's lights apart by the gap between commands — 200ms across three lights
# at 10/sec, plainly visible on a strobe. Leaving a little unspent lets a whole
# round go out in one burst and land together.
GROUP_HEADROOM = 0.8


class RateLimiter:
    """A sustained ceiling on commands to the bridge, with room for a burst.

    Philips' guidance is a rate — about ten commands a second to /lights — not
    an exact spacing. Insisting on a fixed gap between every single command
    also spread a group's lights across hundreds of milliseconds, which is
    plainly visible on a strobe. Letting a whole round take its tokens at once
    and then waiting for them to refill keeps the average identical and the
    group in step.

    (The /groups endpoint would be genuinely simultaneous, but the bridge
    broadcasts those and Philips caps them at one a second, which is far too
    slow to flicker with.)
    """

    def __init__(self, max_per_second: float = 10.0, burst: int = 1):
        self.min_interval = 1.0 / max_per_second
        self._burst = max(1, burst)
        self._tokens = float(self._burst)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def max_per_second(self) -> float:
        return 1.0 / self.min_interval

    @property
    def burst(self) -> int:
        return self._burst

    def set_rate(self, max_per_second: float):
        self.min_interval = 1.0 / max(0.5, max_per_second)

    def _refill(self):
        now = time.monotonic()
        earned = (now - self._updated) / self.min_interval
        self._tokens = min(float(self._burst), self._tokens + earned)
        self._updated = now

    def set_burst(self, burst: int):
        """Size the bucket to the number of lights running together.

        Idle time counts toward the new size, so a group starting after a
        quiet spell can send its first round together instead of taking three
        rounds to fill up. Nothing is granted outright: a caller cycling
        start/stop earns only what the elapsed time is worth.
        """
        self._burst = max(1, min(int(burst), MAX_BURST))
        self._refill()

    async def wait(self):
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                shortfall = (1.0 - self._tokens) * self.min_interval
            # Slept outside the lock on purpose: waiters that still have a
            # token to claim must not queue behind this one.
            await asyncio.sleep(shortfall)


class FlickerEngine:
    def __init__(self, get_client: Callable[[], HueClient | None], on_change: Callable = None,
                 on_snapshots: Callable = None):
        self._get_client = get_client
        self._tasks: dict[str, asyncio.Task] = {}
        # Each loop reads its own entry every tick, so writing here is what
        # makes a change take effect live instead of on the next restart.
        self._states: dict[str, dict] = {}
        # light_id -> what the bulb looked like before we touched it.
        self._snapshots: dict[str, dict] = {}
        self.limiter = RateLimiter(max_per_second=10.0)
        self.restore_on_stop = True
        self._on_change = on_change or (lambda: None)
        self._on_snapshots = on_snapshots or (lambda snapshots: None)

    @property
    def snapshots(self) -> dict:
        return {lid: dict(s) for lid, s in self._snapshots.items()}

    def load_snapshots(self, snapshots: dict):
        """Adopt snapshots persisted by a previous run."""
        self._snapshots = {lid: dict(s) for lid, s in (snapshots or {}).items()}

    async def capture(self, light_ids) -> dict:
        """Read the bulbs' current state so it can be put back later.

        One bulk GET covers the whole group. Lights already flickering keep the
        snapshot taken before *that* run started — otherwise restarting a light
        would overwrite the original with our own flicker output.
        """
        wanted = [lid for lid in light_ids if lid not in self._snapshots]
        if not wanted:
            return self.snapshots
        client = self._get_client()
        if client is None:
            return self.snapshots
        try:
            await self.limiter.wait()
            lights = await client.get_lights()
        except Exception as e:
            logger.warning("Could not snapshot light state before flickering: %s", e)
            return self.snapshots
        for lid in wanted:
            state = (lights.get(lid) or {}).get("state")
            if state:
                self._snapshots[lid] = restorable(state)
        self._on_snapshots(self.snapshots)
        return self.snapshots

    async def restore(self, light_ids=None, transition_ms: int = 400) -> list:
        """Put lights back the way capture() found them."""
        ids = list(self._snapshots.keys()) if light_ids is None else list(light_ids)
        client = self._get_client()
        if client is None:
            return []
        restored = []
        for lid in ids:
            snapshot = self._snapshots.get(lid)
            if snapshot is None:
                continue
            payload = dict(snapshot)
            payload["transitiontime"] = max(0, int(transition_ms / 100))
            try:
                await self.limiter.wait()
                await client.set_light_state(lid, **payload)
                restored.append(lid)
            except Exception as e:
                logger.warning("Could not restore light %s: %s", lid, e)
                continue
            self._snapshots.pop(lid, None)
        if restored:
            self._on_snapshots(self.snapshots)
        return restored

    def running_light_ids(self):
        return list(self._tasks.keys())

    def _share(self) -> float:
        """Frames per second each running light can actually be sent.

        One light on its own gets the whole budget; there is nothing to stay in
        step with. Lights running together give a little of it back so they
        can be sent as one burst rather than strung out across the second.
        """
        running = max(1, len(self._tasks))
        if running == 1:
            return self.limiter.max_per_second
        return (self.limiter.max_per_second * GROUP_HEADROOM) / running

    def status(self) -> dict:
        # effective_hz is derived here rather than written by the loops: a
        # status push can happen between ticks, and the number depends on how
        # many lights are running right now anyway.
        share = self._share()
        out = {}
        for lid, st in self._states.items():
            entry = dict(st)
            if st.get("running"):
                entry["effective_hz"] = round(min(st["hz"], share), 2)
            out[lid] = entry
        return out

    def expect_batch(self, count: int):
        """Tell the limiter a batch of this many lights is about to start.

        Sizing the bucket up front is what lets the whole group's first send go
        out together; discovering the size one start at a time means the first
        light spends the only token there is.
        """
        self.limiter.set_burst(max(count, len(self._tasks)))

    async def start(self, light_id: str, sequence: str, pattern_id: str, hz: float,
                    min_bri: int, max_bri: int, hue: int | None, sat: int | None,
                    transition_ms: int, epoch: float | None = None):
        # Clearing any previous loop must not restore: we are about to flicker
        # this bulb again, and the snapshot has to survive until it really stops.
        await self.stop(light_id, notify=False, restore=False)

        client = self._get_client()
        if client is None:
            raise RuntimeError("Hue bridge is not configured yet")

        self._states[light_id] = {
            "pattern_id": pattern_id,
            "sequence": sequence,
            "hz": hz,
            "min_bri": min_bri,
            "max_bri": max_bri,
            "hue": hue,
            "sat": sat,
            "transition_ms": transition_ms,
            # Frame position is derived from this instant rather than counted
            # per tick, so lights handed the same epoch play the same frame at
            # the same moment however unevenly the rate limiter serves them.
            "epoch": time.monotonic() if epoch is None else epoch,
            "running": True,
        }
        self._tasks[light_id] = asyncio.create_task(self._run_light(light_id, client))
        # Never shrink while starting: a caller bringing several lights up at
        # once sizes the bucket for the whole batch first, and the first
        # light's send would otherwise collapse it back to one.
        self.limiter.set_burst(max(self.limiter.burst, len(self._tasks)))
        self._on_change()

    def update(self, light_id: str, **changes) -> bool:
        """Retune a running loop in place. Returns False if it isn't running."""
        state = self._states.get(light_id)
        if state is None or not state.get("running"):
            return False
        for key, value in changes.items():
            if value is not None and key in LIVE_FIELDS:
                state[key] = value
        return True

    async def stop(self, light_id: str, notify: bool = True, restore: bool = True):
        task = self._tasks.pop(light_id, None)
        if task:
            # Only resize when something actually stopped. start() calls this
            # to clear any previous loop, and resizing on a no-op would undo
            # the sizing a batch of starts just set up.
            self.limiter.set_burst(len(self._tasks))
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Flicker loop for light %s failed on the way down", light_id)
        if light_id in self._states:
            self._states[light_id]["running"] = False
        if restore and self.restore_on_stop:
            await self.restore([light_id])
        if notify:
            self._on_change()

    def _abandon(self, light_id: str):
        """Drop a light that has stopped answering, without cancelling itself.

        The snapshot is kept: the bulb may come back, and a manual revert
        should still have something to put back.
        """
        self._tasks.pop(light_id, None)
        self.limiter.set_burst(len(self._tasks))
        if light_id in self._states:
            self._states[light_id]["running"] = False
        self._on_change()

    async def stop_all(self, restore: bool = True):
        for lid in list(self._tasks.keys()):
            await self.stop(lid, notify=False, restore=False)
        if restore and self.restore_on_stop:
            await self.restore()
        self._on_change()

    async def _run_light(self, light_id, client: HueClient):
        state = self._states[light_id]
        applied_color = None
        failures = 0
        last_frame = None
        last_interval = None

        try:
            while True:
                # You cannot show frames you cannot send: running the pattern
                # faster than this light's share of the bridge budget would
                # just sample it, and a stride that divides the pattern evenly
                # (a two-frame strobe at half rate) freezes it outright.
                hz = min(state["hz"], self._share())
                interval = 1.0 / max(0.5, hz)
                epoch = state["epoch"]

                # A frame number only means anything alongside the interval it
                # was counted in. Both change under us — the speed slider moves,
                # or another light starts and the share is recut — and after
                # that the previous number is in the wrong units: at 10 Hz for
                # 30s it is 300, and the same instant at 2 Hz is frame 60. Left
                # alone, the guard below would read that as "not due yet" and
                # wait for frame 301 at the new interval, which is two minutes
                # away. So forget it and let the new interval start counting.
                if interval != last_interval:
                    last_frame = None
                    last_interval = interval

                # Which frame the pattern is on right now. Counting ticks
                # instead would let a throttled light fall behind, which is
                # what made lights in a group drift apart.
                frame = int((time.monotonic() - epoch) / interval)
                if last_frame is not None and frame <= last_frame:
                    # Woke a hair before the boundary. Sending now would put
                    # the same frame on the wire twice — a wasted command out
                    # of a budget the whole group is sharing — so wait for the
                    # frame that is actually due. Checked before taking a
                    # token so the wasted send doesn't cost one either.
                    due = epoch + (last_frame + 1) * interval
                    # Never wait longer than a frame: whatever the arithmetic
                    # says, the next one is at most one interval away.
                    await asyncio.sleep(min(interval, max(0.001, due - time.monotonic())))
                    continue

                # Take a slot, then re-read the clock: the value that goes out
                # should be the one due at the moment it is actually sent.
                await self.limiter.wait()
                frame = int((time.monotonic() - epoch) / interval)
                last_frame = frame
                sequence = state["sequence"] or "m"
                level = level_for_char(sequence[frame % len(sequence)])
                min_bri, max_bri = state["min_bri"], state["max_bri"]
                bri = int(round(min_bri + level * (max_bri - min_bri)))
                bri = max(1, min(254, bri))

                payload = {
                    "on": True,
                    "bri": bri,
                    "transitiontime": max(0, int(state["transition_ms"] / 100)),
                }

                # Colour rides along with the brightness PUT rather than costing
                # its own slot in the rate limiter, and is only re-sent when it
                # actually changes — including when changed mid-flicker.
                hue, sat = state["hue"], state["sat"]
                wanted = (int(hue), int(sat)) if hue is not None and sat is not None else None
                if wanted is not None and wanted != applied_color:
                    payload["hue"], payload["sat"] = wanted

                try:
                    await client.set_light_state(light_id, **payload)
                    if "hue" in payload:
                        applied_color = wanted
                    failures = 0
                except Exception as e:
                    failures += 1
                    if failures == 1:
                        # Only the first of a run: at 10Hz a dead light would
                        # otherwise write a line to the log ten times a second.
                        logger.warning("Hue PUT failed for light %s: %s", light_id, e)
                    if failures >= GIVE_UP_AFTER_FAILURES:
                        logger.error(
                            "Giving up on light %s after %d failed sends in a row; "
                            "freeing its share of the bridge budget",
                            light_id, failures,
                        )
                        self._abandon(light_id)
                        return

                # Sleep to the next frame boundary rather than a fixed
                # interval, so slow sends skip frames instead of dragging the
                # whole pattern out of time.
                next_frame_at = epoch + (frame + 1) * interval
                await asyncio.sleep(max(0.0, next_frame_at - time.monotonic()))
        except asyncio.CancelledError:
            raise
