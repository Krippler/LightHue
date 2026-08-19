import asyncio
import logging
import time
from collections.abc import Callable

from .hue_client import HueClient
from .patterns import level_for_char

logger = logging.getLogger("flicker_engine")


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
    def __init__(self, get_client: Callable[[], HueClient | None], on_change: Callable = None):
        self._get_client = get_client
        self._tasks: dict[str, asyncio.Task] = {}
        self._states: dict[str, dict] = {}  # light_id -> current settings, for UI sync
        self.limiter = RateLimiter(max_per_second=10.0)
        self._on_change = on_change or (lambda: None)

    def running_light_ids(self):
        return list(self._tasks.keys())

    def status(self) -> dict:
        return {lid: {k: v for k, v in st.items() if k != "_task"} for lid, st in self._states.items()}

    async def start(self, light_id: str, sequence: str, pattern_id: str, hz: float,
                     min_bri: int, max_bri: int, hue: int | None, sat: int | None,
                     transition_ms: int):
        await self.stop(light_id, notify=False)

        client = self._get_client()
        if client is None:
            raise RuntimeError("Hue bridge is not configured yet")

        self._states[light_id] = {
            "pattern_id": pattern_id,
            "hz": hz,
            "min_bri": min_bri,
            "max_bri": max_bri,
            "hue": hue,
            "sat": sat,
            "transition_ms": transition_ms,
            "running": True,
        }
        task = asyncio.create_task(self._run_light(light_id, sequence, hz, min_bri, max_bri,
                                                     hue, sat, transition_ms, client))
        self._tasks[light_id] = task
        self._on_change()

    async def stop(self, light_id: str, notify: bool = True):
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
        if notify:
            self._on_change()

    async def stop_all(self):
        for lid in list(self._tasks.keys()):
            await self.stop(lid, notify=False)
        self._on_change()

    async def _run_light(self, light_id, sequence, hz, min_bri, max_bri, hue, sat,
                          transition_ms, client: HueClient):
        interval = 1.0 / max(0.5, hz)
        idx = 0

        # Set color once up front, if requested.
        if hue is not None and sat is not None:
            try:
                await self.limiter.wait()
                await client.set_light_state(light_id, on=True, hue=int(hue), sat=int(sat), transitiontime=0)
            except Exception as e:
                logger.warning("Failed to set initial color for light %s: %s", light_id, e)

        try:
            while True:
                char = sequence[idx % len(sequence)]
                level = level_for_char(char)
                bri = int(round(min_bri + level * (max_bri - min_bri)))
                bri = max(1, min(254, bri))

                await self.limiter.wait()
                try:
                    await client.set_light_state(
                        light_id, on=True, bri=bri,
                        transitiontime=max(0, int(transition_ms / 100)),
                    )
                except Exception as e:
                    logger.warning("Hue PUT failed for light %s: %s", light_id, e)

                idx += 1
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
