"""The bridge address decides which machine the server talks to, so it gets
its own suite: everything here is about refusing to be aimed somewhere else.
"""
import httpx
import pytest

from app import hue_client


# Resolved through the module, not bound at import: conftest reloads
# app.hue_client per test, which rebinds these to fresh objects.
def parse_bridge_address(value):
    return hue_client.parse_bridge_address(value)


def refused():
    return hue_client.BridgeAddressError


def HueClient(*a, **kw):
    return hue_client.HueClient(*a, **kw)

ACCEPTED = [
    ("192.168.1.23", ("192.168.1.23", 80)),
    (" 10.0.0.5 ", ("10.0.0.5", 80)),          # trimmed
    ("10.0.0.5:9950", ("10.0.0.5", 9950)),     # a diyHue-style emulator on another port
    ("172.17.0.4", ("172.17.0.4", 80)),
    ("8.8.8.8", ("8.8.8.8", 80)),              # routed subnets and VPNs still work
    ("fd00::1", ("fd00::1", 80)),
    ("[fd00::1]:8080", ("fd00::1", 8080)),
]

REFUSED = [
    "",                             # nothing typed
    "   ",
    "127.0.0.1",                    # the host's own services
    "127.0.0.1:9091",
    "[::1]:80",
    "169.254.169.254",              # link-local, incl. cloud metadata
    "0.0.0.0",
    "224.0.0.1",                    # multicast
    "evil.example.com",             # host names: no DNS, so no rebinding
    "evil.example.com:8080",
    "1.2.3.4@169.254.169.254",      # userinfo hiding the real host
    "user:pw@internal.host",
    "10.0.0.5/../x",
    "10.0.0.5:0",
    "10.0.0.5:70000",
    "10.0.0.5:abc",
    "[fd00::1",                     # unbalanced bracket
]


@pytest.mark.parametrize("value,expected", ACCEPTED)
def test_usable_addresses_are_accepted(value, expected):
    assert parse_bridge_address(value) == expected


@pytest.mark.parametrize("value", REFUSED)
def test_addresses_we_will_not_call_are_refused(value):
    with pytest.raises(refused()):
        parse_bridge_address(value)


def test_client_refuses_a_bad_address_from_stored_config():
    # The API edge isn't the only way into the config, so the client checks too.
    with pytest.raises(refused()):
        HueClient("127.0.0.1:9091", "k")


def test_urls_keep_the_host_the_address_named():
    client = HueClient("10.0.0.5:9950", "abc")
    assert str(client._url("lights")) == "http://10.0.0.5:9950/api/abc/lights"
    assert str(client._url("lights", "3", "state")) == "http://10.0.0.5:9950/api/abc/lights/3/state"


def test_a_key_or_light_id_cannot_rewrite_the_path():
    # ".." in a path segment would otherwise be normalised away, silently
    # pointing the request somewhere other than the caller asked for.
    client = HueClient("10.0.0.5", "abc/../def")
    assert str(client.base_url) == "http://10.0.0.5/api/abc%2F..%2Fdef"
    url = client._url("lights", "3/../../evil", "state")
    assert str(url) == "http://10.0.0.5/api/abc%2F..%2Fdef/lights/3%2F..%2F..%2Fevil/state"


def test_the_shared_client_does_not_follow_redirects(app_modules):
    # A redirect would hand the host choice straight back to the response.
    assert hue_client._http().follow_redirects is False


# ---------- the API edge ----------

@pytest.mark.parametrize("value", ["127.0.0.1:9091", "169.254.169.254",
                                   "evil.example.com", "1.2.3.4@169.254.169.254"])
def test_set_bridge_rejects_addresses_we_will_not_call(client, value):
    r = client.post("/api/bridge/set", json={"bridge_ip": value, "api_key": "k"})
    assert r.status_code == 422
    assert client.get("/api/bridge").json()["configured"] is False


@pytest.mark.parametrize("value", ["127.0.0.1:9091", "evil.example.com"])
def test_pair_rejects_them_too_without_calling_out(client, bridge, value):
    r = client.post("/api/bridge/pair", json={"bridge_ip": value})
    assert r.status_code == 422
    assert client.get("/api/bridge").json()["configured"] is False


def test_a_usable_address_still_configures(client):
    assert client.post("/api/bridge/set",
                       json={"bridge_ip": "10.0.0.5:9950", "api_key": "k"}).status_code == 200
    assert client.get("/api/bridge").json()["bridge_ip"] == "10.0.0.5:9950"


def test_bridge_errors_do_not_echo_the_response(client, app_modules, monkeypatch):
    """A 502 shouldn't tell the caller a live port from a dead one."""
    client.post("/api/bridge/set", json={"bridge_ip": "10.0.0.5", "api_key": "k"})

    async def refused(self):
        request = httpx.Request("GET", "http://10.0.0.5/api/k/lights")
        raise httpx.HTTPStatusError(
            "Client error '401 Unauthorized' for url 'http://10.0.0.5/api/k/lights'",
            request=request, response=httpx.Response(401, request=request))

    monkeypatch.setattr(app_modules.HueClient, "get_lights", refused)
    body = client.get("/api/lights")
    assert body.status_code == 502
    detail = body.json()["detail"]
    for leak in ("401", "Unauthorized", "http://", "10.0.0.5"):
        assert leak not in detail
