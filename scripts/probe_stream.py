#!/usr/bin/env python3
"""Test just the Hue entertainment handshake, and nothing else.

Streaming failing looks the same whichever part is broken: the console reports
a timeout. This separates the parts. It claims an entertainment area over the
REST API, opens the DTLS socket, and says which step failed — so a network path
that cannot carry UDP is told apart from a bridge that refuses the credentials.

Run it in both places when a stream times out from the container:

    # on the host
    python3 scripts/probe_stream.py 192.168.1.23 <api-key> <client-key>

    # inside the container
    docker exec -it lighthue python3 /srv/scripts/probe_stream.py ...

Working on the host and failing in the container means container networking is
eating the UDP, and Host networking is the fix. Failing in both means the
bridge, the credentials, or the area.

The api key and client key are the pair stored in /data/config.json.
"""
import argparse
import json
import socket
import sys
import time
import urllib.request
from pathlib import Path

STREAM_PORT = 2100
CIPHERS = ("TLS-PSK-WITH-AES-128-GCM-SHA256",)


def say(ok, text):
    print(f"  {'ok  ' if ok else 'FAIL'}  {text}")
    return ok


def split_host(address):
    """Host on its own. The bridge address may carry the REST port, and the
    streaming port is its own — passing "host:port" to a socket makes it a
    hostname, which then fails to resolve."""
    text = address.strip()
    if text.startswith("[") and "]" in text:            # [::1]:80
        host, _, rest_of = text[1:].partition("]")
        return host
    if text.count(":") == 1:
        return text.split(":", 1)[0]
    return text


def rest(bridge, key, path, method="GET", body=None):
    url = f"http://{bridge}/api/{key}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=6) as r:
        return json.loads(r.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bridge", help="bridge IP, e.g. 192.168.1.23")
    ap.add_argument("api_key")
    ap.add_argument("client_key", help="32 hex characters, from pairing")
    ap.add_argument("--area", help="entertainment area id; default is the first found")
    ap.add_argument("--timeout", type=float, default=8.0)
    args = ap.parse_args()

    print(f"\nProbing {args.bridge} as {args.api_key[:6]}...\n")

    try:
        from mbedtls import tls
    except ImportError:
        say(False, "python-mbedtls is not installed here (pip install python-mbedtls)")
        return 2

    # 1. REST reachable at all?
    try:
        groups = rest(args.bridge, args.api_key, "/groups")
    except Exception as e:
        say(False, f"REST /groups failed: {e}")
        print("\n  The bridge is not answering HTTP. Wrong IP, or the key is not valid.\n")
        return 1
    say(True, f"REST reachable, {len(groups)} groups")

    areas = {gid: g for gid, g in groups.items()
             if (g.get("type") or "") == "Entertainment"}
    if not areas:
        say(False, "no entertainment areas — make one in the Hue app first")
        return 1
    # Prefer one nothing is holding: probing an area Hue Sync owns tells us
    # about Hue Sync, not about this machine's path to the bridge.
    free = [gid for gid, g in areas.items() if not (g.get("stream") or {}).get("active")]
    area_id = args.area or (free[0] if free else next(iter(areas)))
    area = areas.get(area_id)
    if area is None:
        say(False, f"area {area_id} is not an entertainment area on this bridge")
        return 1
    say(True, f"area {area_id} \"{area.get('name')}\" with {len(area.get('lights', []))} lights"
              f", positioned={bool(area.get('locations'))}")
    held = (area.get("stream") or {}).get("active")
    say(not held, f"area currently claimed: {held} (owner {(area.get('stream') or {}).get('owner')})")

    # 2. Claim it.
    try:
        rest(args.bridge, args.api_key, f"/groups/{area_id}", "PUT",
             {"stream": {"active": True}})
    except Exception as e:
        say(False, f"could not claim the area: {e}")
        return 1
    say(True, "area claimed for streaming")

    # 3. Where would a reply come back to?
    host = split_host(args.bridge)
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.connect((host, STREAM_PORT))
    local = probe.getsockname()
    probe.close()
    say(True, f"UDP would leave from {local[0]} — the bridge replies to this")

    # 4. The handshake, which is the whole question.
    ok = False
    try:
        config = tls.DTLSConfiguration(
            pre_shared_key=(args.api_key, bytes.fromhex(args.client_key)),
            ciphers=CIPHERS, validate_certificates=False)
        raw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        raw.settimeout(args.timeout)
        sock = tls.ClientContext(config).wrap_socket(raw, server_hostname=None)
        started = time.monotonic()
        sock.connect((host, STREAM_PORT))
        sock.do_handshake()
        ok = say(True, f"DTLS handshake in {time.monotonic() - started:.1f}s "
                       f"({sock.negotiated_tls_version()}, {sock.cipher()})")
        sock.send(b"HueStream" + bytes([0x01, 0x00, 0x00, 0, 0, 0x00, 0x00]))
        say(True, "a frame was accepted")
        sock.close()
    except Exception as e:
        kind = type(e).__name__
        say(False, f"DTLS handshake failed after {args.timeout:.0f}s: {kind}: {e}")
        if "timed out" in str(e) or kind in ("timeout", "TimeoutError"):
            # A wrong key and a blocked path both end here. A bare ClientHello
            # separates them: a DTLS server answers one before it looks at any
            # credential, so a reply means the path is fine and the key is not.
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from app.hue_stream import probe_stream_port
            state, how = probe_stream_port(host, STREAM_PORT)
            say(state == "answered", f"bare ClientHello to UDP {STREAM_PORT}: {how}")
            if state == "refused":
                print("""
  The port is shut even though the bridge says it is holding the area, and the
  refusal itself proves the path works. So the bridge took the v1 claim without
  arming the stream behind it — newer firmware does that when the area was made
  by the current Hue app, which wants the v2 API to start it.
""")
            elif state == "answered":
                print("""
  The path is fine — the bridge answers on the streaming port. So the client key
  is not one this bridge will accept. It is only issued alongside the api key it
  belongs to, at pairing time, so pair again and use both new values together.
""")
            else:
                print(f"""
  Nothing comes back on UDP {STREAM_PORT}, while HTTP to the same bridge works.
  That is the network path: UDP is not getting there, or the reply is not
  finding its way back to {local[0]}.

  Run this on the host as well. Working there and failing here means container
  networking; switch the container to Host networking.
""")
        else:
            print("""
  The bridge answered and refused. That is the credentials: the client key has
  to be the one issued alongside this exact api key. Pair again.
""")
    finally:
        try:
            rest(args.bridge, args.api_key, f"/groups/{area_id}", "PUT",
                 {"stream": {"active": False}})
            say(True, "area handed back")
        except Exception as e:
            say(False, f"could not hand the area back: {e} — the Hue app may be "
                       f"unable to drive those lights until the bridge is restarted")
    print()
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:                    # a traceback helps nobody here
        print(f"\n  FAIL  {type(exc).__name__}: {exc}\n")
        sys.exit(2)
