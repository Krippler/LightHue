import httpx


class HueError(Exception):
    pass


_shared: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    """One connection pool for the whole process.

    Building a fresh AsyncClient per call meant a new TCP handshake for every
    flicker tick — up to 10/second, sustained, against a bridge that is not a
    web server. Created lazily so it binds to the running event loop.
    """
    global _shared
    if _shared is None or _shared.is_closed:
        _shared = httpx.AsyncClient(timeout=5.0)
    return _shared


async def aclose():
    """Release the shared pool. Called from the app's shutdown hook."""
    global _shared
    if _shared is not None and not _shared.is_closed:
        await _shared.aclose()
    _shared = None


class HueClient:
    def __init__(self, bridge_ip: str, api_key: str):
        self.bridge_ip = bridge_ip
        self.api_key = api_key
        self.base_url = f"http://{bridge_ip}/api/{api_key}"

    async def get_lights(self) -> dict:
        r = await _http().get(f"{self.base_url}/lights", timeout=5)
        r.raise_for_status()
        return r.json()

    async def set_light_state(self, light_id: str, **state) -> dict:
        r = await _http().put(f"{self.base_url}/lights/{light_id}/state", json=state, timeout=3)
        r.raise_for_status()
        return r.json()

    @staticmethod
    async def discover() -> list:
        """Uses Philips' public N-UPnP discovery endpoint to find bridges on the LAN."""
        r = await _http().get("https://discovery.meethue.com", timeout=6)
        r.raise_for_status()
        return r.json()

    @staticmethod
    async def pair(bridge_ip: str, devicetype: str = "quake_hue_flicker#server") -> dict:
        """Call after the user has pressed the physical link button on the bridge."""
        r = await _http().post(f"http://{bridge_ip}/api", json={"devicetype": devicetype}, timeout=6)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and data and "success" in data[0]:
            return {"ok": True, "api_key": data[0]["success"]["username"]}
        if isinstance(data, list) and data and "error" in data[0]:
            return {"ok": False, "error": data[0]["error"].get("description", "unknown error")}
        return {"ok": False, "error": "unexpected response from bridge"}
