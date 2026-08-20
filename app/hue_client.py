import ipaddress
from urllib.parse import quote

import httpx


class HueError(Exception):
    pass


class BridgeAddressError(ValueError):
    """The configured bridge address isn't one we're willing to call."""


def parse_bridge_address(value: str) -> tuple[str, int]:
    """Turn a user-supplied "bridge address" into a host and port we'll call.

    Everything the console sends to a bridge is built from this string, and it
    lands in the *authority* of the URL — so an unchecked value doesn't just
    pick a path, it picks which machine the server talks to. Left open, a caller
    on the LAN can aim the console at whatever the container can reach and read
    the answers back out of /api/lights: sibling containers, or (under
    network_mode: host) services on the host that are deliberately bound to
    loopback only.

    So: an IP literal, nothing else. That single rule does most of the work —
    it rejects hostnames along with DNS rebinding, and it rejects the
    "1.2.3.4@10.0.0.1" userinfo trick that hides the real host behind something
    that looks like an address. On top of it we refuse the ranges a Hue bridge
    is never on but an attacker would want: loopback, link-local (which is also
    where cloud metadata services live), multicast and the reserved blocks.
    """
    raw = (value or "").strip()
    if not raw:
        raise BridgeAddressError("Enter the bridge's IP address")

    host, port = raw, 80
    if raw.startswith("["):                      # [::1]:80 — bracketed IPv6
        closing = raw.find("]")
        if closing == -1:
            raise BridgeAddressError("Enter the bridge's IP address, e.g. 192.168.1.23")
        host, rest = raw[1:closing], raw[closing + 1:]
        if rest.startswith(":"):
            port = _parse_port(rest[1:])
        elif rest:
            raise BridgeAddressError("Enter the bridge's IP address, e.g. 192.168.1.23")
    elif raw.count(":") == 1:                    # 192.168.1.23:80 — a bare IPv6 has more
        host, _, tail = raw.partition(":")
        port = _parse_port(tail)

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise BridgeAddressError(
            "Enter the bridge's IP address, e.g. 192.168.1.23 — host names aren't accepted"
        ) from None

    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        raise BridgeAddressError(f"{ip} isn't an address a Hue bridge can be on")

    return str(ip), port


def _parse_port(text: str) -> int:
    if not text.isdigit():
        raise BridgeAddressError("The port after ':' has to be a number")
    port = int(text)
    if not 1 <= port <= 65535:
        raise BridgeAddressError("The port has to be between 1 and 65535")
    return port


_shared: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    """One connection pool for the whole process.

    Building a fresh AsyncClient per call meant a new TCP handshake for every
    flicker tick — up to 10/second, sustained, against a bridge that is not a
    web server. Created lazily so it binds to the running event loop.
    """
    global _shared
    if _shared is None or _shared.is_closed:
        # No redirects: a bridge never issues one, and following one would
        # hand back the host choice this module just took away.
        _shared = httpx.AsyncClient(timeout=5.0, follow_redirects=False)
    return _shared


async def aclose():
    """Release the shared pool. Called from the app's shutdown hook."""
    global _shared
    if _shared is not None and not _shared.is_closed:
        await _shared.aclose()
    _shared = None


class HueClient:
    def __init__(self, bridge_ip: str, api_key: str):
        # Validated here rather than only at the API edge, so a value that
        # reached the config some other way still can't aim the client.
        host, port = parse_bridge_address(bridge_ip)
        self.bridge_ip = bridge_ip
        self.api_key = api_key
        # Percent-encoded: a key or light id carrying "/" or ".." would
        # otherwise be normalised away and quietly rewrite the path.
        self._prefix = f"/api/{quote(api_key, safe='')}"
        self.base_url = httpx.URL(scheme="http", host=host, port=port, path=self._prefix)

    def _url(self, *segments: str) -> httpx.URL:
        path = self._prefix + "".join(f"/{quote(s, safe='')}" for s in segments)
        return self.base_url.copy_with(raw_path=path.encode())

    async def get_lights(self) -> dict:
        r = await _http().get(self._url("lights"), timeout=5)
        r.raise_for_status()
        return r.json()

    async def get_groups(self) -> dict:
        """The bridge's own groups: the Rooms and Zones set up in the Hue app."""
        r = await _http().get(self._url("groups"), timeout=5)
        r.raise_for_status()
        return r.json()

    async def set_light_state(self, light_id: str, **state) -> dict:
        r = await _http().put(self._url("lights", light_id, "state"), json=state, timeout=3)
        r.raise_for_status()
        return r.json()

    @staticmethod
    async def discover() -> list:
        """Uses Philips' public N-UPnP discovery endpoint to find bridges on the LAN."""
        r = await _http().get("https://discovery.meethue.com", timeout=6)
        r.raise_for_status()
        return r.json()

    @staticmethod
    async def pair(bridge_ip: str, devicetype: str = "game_hue_flicker#server") -> dict:
        """Call after the user has pressed the physical link button on the bridge."""
        host, port = parse_bridge_address(bridge_ip)
        url = httpx.URL(scheme="http", host=host, port=port, path="/api")
        r = await _http().post(url, json={"devicetype": devicetype}, timeout=6)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and data and "success" in data[0]:
            return {"ok": True, "api_key": data[0]["success"]["username"]}
        if isinstance(data, list) and data and "error" in data[0]:
            return {"ok": False, "error": data[0]["error"].get("description", "unknown error")}
        return {"ok": False, "error": "unexpected response from bridge"}
