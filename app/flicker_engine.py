import asyncio
import logging
import time
from collections.abc import Callable

from .hue_client import HueClient
from .patterns import level_for_char

logger = logging.getLogger("flicker_engine")

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


class RateLimiter:
    """Global token-bucket-ish limiter so we never flood the Hue bridge,
    regardless of how many lights are flickering at once."""

    def __init__(self, max_per_second: float = 10.0):
        self.min_interval = 1.0 / max_per_second
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self):
        async with self._lock:
            now = time.monotonic()
            delay = self._last + self.min_interval - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()

    def set_rate(self, max_per_second: float):
        self.min_interval = 1.0 / max(0.5, max_per_second)


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

    def status(self) -> dict:
        return {lid: dict(st) for lid, st in self._states.items()}

    async def start(self, light_id: str, sequence: str, pattern_id: str, hz: float,
                    min_bri: int, max_bri: int, hue: int | None, sat: int | None,
                    transition_ms: int):
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
            "running": True,
        }
        self._tasks[light_id] = asyncio.create_task(self._run_light(light_id, client))
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

    async def stop_all(self, restore: bool = True):
        for lid in list(self._tasks.keys()):
            await self.stop(lid, notify=False, restore=False)
        if restore and self.restore_on_stop:
            await self.restore()
        self._on_change()

    async def _run_light(self, light_id, client: HueClient):
        state = self._states[light_id]
        idx = 0
        applied_color = None

        try:
            while True:
                sequence = state["sequence"] or "m"
                level = level_for_char(sequence[idx % len(sequence)])
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

                await self.limiter.wait()
                try:
                    await client.set_light_state(light_id, **payload)
                    if "hue" in payload:
                        applied_color = wanted
                except Exception as e:
                    logger.warning("Hue PUT failed for light %s: %s", light_id, e)

                idx += 1
                await asyncio.sleep(1.0 / max(0.5, state["hz"]))
        except asyncio.CancelledError:
            raise
