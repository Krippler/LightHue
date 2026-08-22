#!/bin/sh
# Capture the entertainment handshake as it happens.
#
# Running tcpdump in one window and pressing Start in another means guessing
# when to stop, and a capture that missed the attempt looks the same as an
# attempt that sent nothing. This drives the handshake itself, so the capture
# always brackets exactly one attempt.
#
#   docker exec -it "$(docker ps --filter ancestor=lighthue -q)" \
#       sh /srv/scripts/capture_stream.sh
#
# Or just name your own container — this image gets deployed under whatever
# name suits, so do not go looking for one called "lighthue":
#
#   docker ps --format '{{.Names}}\t{{.Image}}'
#   docker exec -it <that-name> sh /srv/scripts/capture_stream.sh
#
# Everything it needs is in /data/config.json. Pass extra arguments and they go
# to probe_stream.py — --area 201 to try a different area, for instance.
#
# Needs an image built after this script was added. Against an older container,
# run the capture on the host and drive the probe with docker exec instead:
#
#   tcpdump -ni any -s0 -U -w /tmp/hue.pcap 'udp port 2100 or icmp' &
#   sleep 2
#   docker exec <name> python3 /srv/scripts/probe_stream.py --config /data/config.json
#   sleep 1; kill %1; tcpdump -nr /tmp/hue.pcap -vv
set -eu

PORT="${STREAM_PORT:-2100}"
OUT="${OUT:-/tmp/hue-stream.pcap}"

command -v tcpdump >/dev/null 2>&1 || {
    echo "tcpdump is not installed in this image — rebuild to get it." >&2
    exit 1
}

echo "== capturing udp/$PORT and icmp =="
# -U so records hit the file as they arrive: the probe below may end the process
# before a buffered capture would have flushed anything.
tcpdump -ni any -s0 -U -w "$OUT" "udp port $PORT or icmp" 2>/tmp/hue-tcpdump.log &
CAPTURE=$!
# tcpdump needs a moment to attach before the first datagram, or the opening
# ClientHello — the one packet this whole exercise is about — is missed.
sleep 2

echo
echo "== attempting the handshake =="
set +e
python3 /srv/scripts/probe_stream.py --config /data/config.json "$@"
VERDICT=$?
set -e

sleep 1
kill "$CAPTURE" 2>/dev/null || true
wait "$CAPTURE" 2>/dev/null || true

echo
echo "== what was on the wire =="
tcpdump -nr "$OUT" -vv 2>/dev/null || {
    echo "(nothing captured — see /tmp/hue-tcpdump.log)"
    cat /tmp/hue-tcpdump.log >&2
}

echo
echo "== how to read it =="
cat <<'EOF'
  no packets at all         -> the datagram never reached the wire; something
                               local dropped it (firewall, routing rule)
  ours out, ICMP back       -> refused on purpose; the ICMP names the hop
  ours out, nothing back    -> the bridge is receiving and staying silent
  theirs back, still failed -> the reply is being dropped on the way in
EOF
exit "$VERDICT"
