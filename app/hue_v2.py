"""The Hue v2 API, only as far as entertainment configurations.

The rest of this app speaks v1, which is enough for lights, groups and pairing.
Entertainment is the exception: an area created by the current Hue app exists as
a v2 *entertainment configuration*, and the v1 view of it is a compatibility
shim. Setting `stream.active` through that shim flips a flag and binds UDP 2100,
but does not arm the service behind it — the bridge then answers no handshake at
all, which is indistinguishable from a dead network until everything else has
been ruled out.

Starting it properly means `PUT .../entertainment_configuration/<id>` with
`{"action": "start"}` over v2.

Two things differ from v1 and both matter:

* v2 is HTTPS only, on a self-signed certificate the bridge generates for
  itself. There is no CA to check it against, so verification is off for this
  one host. That is not a step down from v1, which is plain HTTP — and the
  address it connects to has already been through the same validation as
  everything else here.
* The application key travels in a `hue-application-key` header rather than in
  the path.
"""
import httpx

from .hue_client import parse_bridge_address

V2_BASE = "/clip/v2/resource"

_shared: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    """A pool of its own, because this one does not verify certificates.

    Kept apart from the v1 client deliberately: turning verification off is a
    decision that should apply to exactly the connections that need it, not to
    everything the app happens to send.
    """
    global _shared
    if _shared is None or _shared.is_closed:
        _shared = httpx.AsyncClient(timeout=6.0, verify=False, follow_redirects=False)
    return _shared


async def aclose():
    global _shared
    if _shared is not None and not _shared.is_closed:
        await _shared.aclose()
    _shared = None


class HueV2Client:
    def __init__(self, bridge_ip: str, api_key: str):
        host, _rest_port = parse_bridge_address(bridge_ip)
        self.host = host
        self.api_key = api_key

    def _url(self, path: str) -> httpx.URL:
        return httpx.URL(scheme="https", host=self.host, path=f"{V2_BASE}{path}")

    @property
    def _headers(self) -> dict:
        return {"hue-application-key": self.api_key}

    async def entertainment_configurations(self) -> list:
        r = await _http().get(self._url("/entertainment_configuration"),
                              headers=self._headers)
        r.raise_for_status()
        return r.json().get("data", [])

    async def set_streaming(self, area_id: str, active: bool) -> dict:
        """Arm or disarm the stream. This is the call v1 cannot make."""
        r = await _http().put(
            self._url(f"/entertainment_configuration/{area_id}"),
            headers=self._headers,
            json={"action": "start" if active else "stop"},
        )
        r.raise_for_status()
        return r.json()


def v1_group_id(configuration: dict) -> str | None:
    """The v1 group this configuration shows up as, e.g. "/groups/200"."""
    id_v1 = configuration.get("id_v1") or ""
    return id_v1.rsplit("/", 1)[-1] or None


def channel_ids(configuration: dict) -> list[int]:
    """A v2 frame addresses channels, not lights.

    A channel is a position in the room that one or more lights answer to, so
    the mapping is not one per bulb — which is exactly why the v2 frame format
    carries channel ids and the v1 one carries light ids.
    """
    return [int(c["channel_id"]) for c in configuration.get("channels", [])
            if "channel_id" in c]
