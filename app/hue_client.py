import httpx


class HueError(Exception):
    pass


class HueClient:
    def __init__(self, bridge_ip: str, api_key: str):
        self.bridge_ip = bridge_ip
        self.api_key = api_key
        self.base_url = f"http://{bridge_ip}/api/{api_key}"

    async def get_lights(self) -> dict:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{self.base_url}/lights")
            r.raise_for_status()
            return r.json()

    async def set_light_state(self, light_id: str, **state) -> dict:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.put(f"{self.base_url}/lights/{light_id}/state", json=state)
            r.raise_for_status()
            return r.json()

    @staticmethod
    async def discover() -> list:
        """Uses Philips' public N-UPnP discovery endpoint to find bridges on the LAN."""
        async with httpx.AsyncClient(timeout=6) as client:
            r = await client.get("https://discovery.meethue.com")
            r.raise_for_status()
            return r.json()

    @staticmethod
    async def pair(bridge_ip: str, devicetype: str = "quake_hue_flicker#server") -> dict:
        """Call after the user has pressed the physical link button on the bridge."""
        async with httpx.AsyncClient(timeout=6) as client:
            r = await client.post(f"http://{bridge_ip}/api", json={"devicetype": devicetype})
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list) and data and "success" in data[0]:
                return {"ok": True, "api_key": data[0]["success"]["username"]}
            if isinstance(data, list) and data and "error" in data[0]:
                return {"ok": False, "error": data[0]["error"].get("description", "unknown error")}
            return {"ok": False, "error": "unexpected response from bridge"}
