# Entertainment streaming

How it works, and what to do when it does not. The README covers turning it on;
this is the part underneath.

## Why it exists

The ordinary path sends one HTTP command per light, and the bridge takes about
ten a second in total for everything. That budget is shared, so the more lights
flicker at once the slower each one gets:

| lights | at 10/sec | at 20/sec | at 30/sec |
|-------:|----------:|----------:|----------:|
| 2      | 4.00 Hz   | 8.00 Hz   | 12.00 Hz  |
| 4      | 2.00 Hz   | 4.00 Hz   | 6.00 Hz   |
| 7      | 1.14 Hz   | 2.29 Hz   | 3.43 Hz   |

Seven bulbs at a real 10 Hz would need about 88 commands a second, which the
REST API will not do at any setting.

Streaming sends one UDP frame holding every light in the area, so a frame costs
the same whether it holds one bulb or ten and the speed stops being divided —
up to 25 Hz across the whole area, every light changing on the same frame.

The bridge does **not** need to be on the same subnet as the console. Streaming
is UDP where the rest of the API is HTTP, and it routes across a boundary like
anything else.

## Making an area

Areas are created on the bridge, not in this console's config, so one made here
appears in the Hue app and survives the container being replaced.

`POST /clip/v2/resource/entertainment_configuration` takes a name, a
configuration type, and a `service_locations` entry per light. Each entry
references the light's **entertainment service** rather than the light itself —
`GET /clip/v2/resource/entertainment` lists them, carrying `id_v1` to map back
to a v1 light id and a `renderer` flag saying whether the light can be driven by
a stream at all. That flag is the real difference between an area and a group:
a plug or a white-only bulb has no service to contribute, and the bridge will
refuse an area containing one.

Positions are required even though nothing here uses them — this app drives
every light in an area from the same pattern, so where a bulb sits in the room
does not change what it is sent. They are spread along a line rather than
stacked at the origin, because a stack draws as a single dot in the Hue app and
makes an area created here look broken next to one made there.

Deleting addresses the configuration by its v2 id, not the v1 group number; the
group number is a compatibility view of the same object and disappears with it.

## Handing the area back

While an area is streaming the bridge ignores everything else for those lights,
including the Hue app. So the console hands the area back when you press Stop,
when the sender stops for any reason at all, and on the way out if the container
is stopped mid-stream. The release is checked rather than assumed: a bridge that
accepts the call and keeps holding the area would otherwise leave those lights
answering to nothing, with nothing on screen saying why.

Streaming leaves each bulb on whatever the last frame held, so the console reads
the area's lights before it claims the area and puts them back after it releases
it — in that order, because a bridge that is streaming to an area ignores
anything else sent to those lights.

Stopping sends a DTLS close_notify before dropping the socket. The bridge allows
one entertainment session and keeps it on its books until told the last one
ended, so a stream that closes quietly can leave a ghost session behind.

On startup the console hands back any area the bridge still records as claimed
by its own API key. That covers what nothing else does: a container killed,
redeployed or crashed mid-stream leaves the claim behind, and until something
clears it those lights answer to nothing at all. **Release area** clears one by
hand, including a claim left by something else, which beats restarting the
bridge.

## The DTLS client

Streaming opens a DTLS 1.2 connection secured with a pre-shared key. The console
carries two clients and tries them one per attempt, each against a freshly armed
area:

* **`minimal`** — hand-rolled, and the one that leads. It retransmits a flight
  that goes unanswered.
* **`mbedtls`** — python-mbedtls, as a fallback.

That order is the whole thing, and it was expensive to learn. DTLS runs over a
protocol that loses datagrams, so resending an unanswered flight is the client's
job. At least one bridge in the wild drops the *first* ClientHello carrying a
cookie and answers the second, which makes retransmission not politeness but the
handshake itself. python-mbedtls drives a blocking socket, so its own
retransmission timer never gets to run — it sends a flight once and sits in
`recv` until the socket gives up. A client that does not resend stops dead at
exactly that point, and the symptom is a stream that works sometimes.

Three details in the framing matter, and all three are easy to get wrong:

* A retransmit keeps its handshake `message_seq`, because it is the same
  message, but needs a **new record sequence number** — the server's anti-replay
  window silently drops a record number it has already seen. The two counters
  pull in opposite directions.
* The record layer carries **DTLS 1.0** until a version is agreed, with 1.2
  requested inside the ClientHello. That split is what RFC 6347 asks for and
  what OpenSSL and mbedtls both put on the wire.
* The cookie exchange is excluded from the Finished hash: both hellos are
  replaced by the second one alone.

Diagnostics reports which client got through, as `transport`.

## Reading Diagnostics

**Diagnostics** in the streaming panel shows what the bridge says about each
area, what the last start attempt did step by step, the bridge's own model and
firmware, and — when a start fails — a deeper look taken while the area is still
claimed.

That deeper look sends a bare ClientHello and reports how far it gets. The PSK
identity is not sent until the fifth message of the handshake, so everything
before that is identical whether the key is right or hopeless: a probe that
stops at the first reply cannot tell a rejected key from a rejected offer.

| how far it gets | what it means |
|---|---|
| **server-hello** | The bridge accepted the offer and would have gone on to check a key. Path fine, port open — so the streaming key is the problem. Pair again for a matching key and API key. |
| **hello-verify-only** | It answered the first ClientHello with a cookie and ignored the one carrying it back. The path works in both directions or the cookie could not have arrived, and the key is not offered this early. Usually the second flight needing a resend. |
| **alert** | The bridge named its objection outright. That is the offer, not the key. |
| **refused** (ICMP port unreachable) | The path is fine — the refusal itself had to reach you — but the port is shut. The bridge took the claim without arming the stream behind it. |
| **silent** | Nothing came back at all. The only one that is a network problem. |

An ICMP refusal is *proof of reachability*, not evidence of blockage. It also
means the probe has to run while the area is claimed — the bridge only binds the
port while it holds one, so probing after the release measures a closed port and
calls a healthy network broken.

Alongside it the console runs **OpenSSL**, which shares no code with either
client here and reaches Hue bridges with one command. Two clients agreeing may
only mean they share a mistake; a third implementation disagreeing settles which
side the fault is on. If OpenSSL connects where the console does not, the error
says so outright and stops blaming the bridge.

## Diagnosing from a shell

`scripts/probe_stream.py` does the same standalone. It claims an area, tries the
library handshake, then the hand-rolled one, then OpenSSL, and says which got
how far. One file with no dependencies beyond the standard library, so it can be
copied anywhere that can see the bridge:

```bash
scp scripts/probe_stream.py someone@192.168.1.50:/tmp/
python3 /tmp/probe_stream.py 192.168.1.23 <api-key> <client-key>
```

It also ships in the image, where `--config` reads the bridge address and both
keys from the console's own config rather than putting a 40-character key into
shell history:

```bash
docker exec -it <container> python3 /srv/scripts/probe_stream.py --config /data/config.json
```

For a failure that only happens sometimes, `--repeat` runs the same attempt on a
loop and reports the shape of it rather than one sample. Evenly spaced successes
mean state carried from one attempt to the next; scattered ones mean loss, and
those want completely different fixes.

```bash
docker exec -it <container> python3 /srv/scripts/probe_stream.py \
    --config /data/config.json --repeat 20 --interval 20
```

When the probe says nothing came back, the next question is whether anything
left. `capture_stream.sh` answers it in one command — it starts a packet
capture, runs the probe inside it, and prints what was on the wire:

```bash
docker exec -it <container> sh /srv/scripts/capture_stream.sh
```

Driving both from one place matters more than it sounds: a capture started by
hand in another window can miss the attempt entirely, and a capture that missed
it looks exactly like an attempt that sent nothing. Four outcomes, pointing four
different ways — no packets at all (something local dropped it), an ICMP refusal
(a firewall, and the message names the hop), ours out with nothing back (the
bridge is receiving and staying silent), or a reply that arrives while the
handshake still fails (the reply is being dropped on the way in).

`tcpdump` and `openssl` are in the image for exactly this. A NAS very likely has
neither, and asking someone to install packet-capture tools on their server to
debug a light is a poor trade.

## Where the code lives

```
app/hue_stream.py     HueStream framing, the DTLS transport, and the probes
app/dtls_psk.py       A DTLS 1.2 PSK client, hand-rolled, that retransmits
app/stream_engine.py  One entertainment area over one socket, at a fixed rate
app/hue_v2.py         The Hue v2 API, only as far as entertainment configurations
scripts/probe_stream.py    Standalone handshake probe — stdlib only
scripts/capture_stream.sh  Packet capture wrapped around one attempt
```

`app/dtls_psk.py` and the probe carry deliberately duplicated copies of the
handshake framing — the probe has to run on a machine with no checkout and no
dependencies, so it cannot import the app. A test asserts the two produce
byte-identical ClientHellos, which is what keeps the duplication honest.

Streaming has no transition setting: every frame is sent, so there is nothing to
interpolate between.
