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
from contextlib import suppress
from pathlib import Path

STREAM_PORT = 2100
CIPHERS = ("TLS-PSK-WITH-AES-128-GCM-SHA256",)


VERDICT = {
    "works": """
  The handshake works. Streaming is fine from here.
""",
    "library": """
  The library's handshake failed where a hand-rolled one, offering a single
  cipher suite and no extensions, reached ServerHello against the same claim.
  So the bridge is listening and willing — what it will not answer is the
  library's ClientHello. Not the path, not the area, not the key.
""",
    "nothing": """
  Neither handshake got anywhere ({how}). HTTP works and the claim was accepted,
  so either the bridge never arms the stream behind a v1 claim, or UDP {port} is
  not making the round trip from {local}.

  If the line above says this machine is not on the bridge's own network, start
  there. Streaming is the one part of the Hue API that wants the client on the
  same network as the bridge; REST routes anywhere, which is why everything
  except the stream has worked.
""",
}


EXPLAIN = {
    "server-hello": """
  The bridge answered ServerHello, so it accepts our offer and would have gone
  on to check a key. The path is fine and the port is open: what it will not
  accept is this client key. A client key is only issued alongside the api key
  it belongs to, so pair again and use both new values together.
""",
    "refused": """
  The port is shut even though the bridge says it is holding the area, and the
  refusal itself proves the path works. So the bridge took the v1 claim without
  arming the stream behind it.
""",
    "hello-verify-only": """
  The bridge sent its cookie and then went quiet, so it is rejecting our
  ClientHello rather than our key — the key is never offered this early. That
  usually means firmware that wants the v2 entertainment API to start a stream.
""",
    "alert": """
  The bridge rejected our ClientHello outright ({how}). That is the offer, not
  the key: the key is not sent until several messages later.
""",
    "silent": """
  Nothing comes back on UDP {port} ({how}), while HTTP to the same bridge works.
  That is the network path: UDP is not getting there, or the reply is not
  finding its way back to {local}.

  Run this on the host as well. Working there and failing here means container
  networking; switch the container to Host networking.
""",
}


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
    # All three live in the console's config, so reading them from there beats
    # copying credentials onto a command line where they end up in shell
    # history — and beats mistyping a 40-character key.
    ap.add_argument("bridge", nargs="?", help="bridge IP, e.g. 192.168.1.23")
    ap.add_argument("api_key", nargs="?")
    ap.add_argument("client_key", nargs="?", help="32 hex characters, from pairing")
    ap.add_argument("--config", default=None,
                    help="read all three from a config.json instead (e.g. /data/config.json)")
    ap.add_argument("--area", help="entertainment area id; default is the first found")
    ap.add_argument("--timeout", type=float, default=8.0)
    args = ap.parse_args()

    if args.config:
        with open(args.config) as f:
            cfg = json.load(f)
        args.bridge = args.bridge or cfg.get("bridge_ip")
        args.api_key = args.api_key or cfg.get("api_key")
        args.client_key = args.client_key or cfg.get("client_key")
    missing = [n for n in ("bridge", "api_key", "client_key") if not getattr(args, n)]
    if missing:
        ap.error(f"missing {', '.join(missing)} — pass them, or use --config /data/config.json")

    print(f"\nProbing {args.bridge} as {args.api_key[:6]}...\n")

    try:
        from mbedtls import tls
    except ImportError:
        tls = None
        say(False, "python-mbedtls is not installed here — the library handshake "
                   "will be skipped, the hand-rolled one still runs")

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
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.connect((host, STREAM_PORT))
    local = probe.getsockname()
    probe.close()
    say(True, f"UDP would leave from {local[0]} — the bridge replies to this")
    from app.hue_stream import same_subnet_as_bridge
    on_net = same_subnet_as_bridge(local[0], host)
    say(on_net, f"on the bridge's own network: {on_net}"
                + ("" if on_net else f"  ({local[0]} vs {host})"))

    # 4. The real handshake first, then a hand-rolled one only if it failed.
    #
    # Order matters. The library's handshake is the one that has to work, so it
    # gets the first and cleanest shot at the claim. The hand-rolled one is a
    # follow-up question — "would this bridge have talked to anyone?" — and
    # asking it first would spend the claim on a connection we then abandon.
    ok = False
    from app.hue_stream import probe_handshake_stage

    try:
        config = None if tls is None else tls.DTLSConfiguration(
            pre_shared_key=(args.api_key, bytes.fromhex(args.client_key)),
            ciphers=CIPHERS, validate_certificates=False)
        try:
            if config is None:
                raise RuntimeError("python-mbedtls is not installed here")
            raw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            raw.settimeout(args.timeout)
            sock = tls.ClientContext(config).wrap_socket(raw, server_hostname=None)
            started = time.monotonic()
            sock.connect((host, STREAM_PORT))
            sock.do_handshake()
            ok = say(True, f"mbedtls handshake in {time.monotonic() - started:.1f}s "
                           f"({sock.negotiated_tls_version()}, {sock.cipher()})")
            sock.send(b"HueStream" + bytes([0x01, 0x00, 0x00, 0, 0, 0x00, 0x00]))
            say(True, "a frame was accepted")
            try:
                sock.shutdown(socket.SHUT_RDWR)   # the goodbye the bridge needs
            except OSError:
                pass
            sock.close()
        except Exception as e:
            say(False, f"mbedtls handshake failed after {args.timeout:.0f}s: "
                       f"{type(e).__name__}: {e}")

        if ok:
            print(VERDICT["works"].format(port=STREAM_PORT, local=local[0], how=""))
        else:
            # Re-claim: the attempt above may have used up the bridge's short
            # listening window, and probing a lapsed claim proves nothing.
            with suppress(Exception):
                rest(args.bridge, args.api_key, f"/groups/{area_id}", "PUT",
                     {"stream": {"active": True}})
            stage, how = probe_handshake_stage(host, STREAM_PORT, timeout=args.timeout)
            reached = stage == "server-hello"
            say(reached, f"hand-rolled handshake: {stage} — {how}")
            print(VERDICT["library" if reached else "nothing"].format(
                port=STREAM_PORT, local=local[0], how=how))
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
