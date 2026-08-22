# LightHue

Drives Philips Hue lights with the flicker patterns of twenty classic games —
Blood, Descent, Deus Ex, DOOM, Doom 3, Duke Nukem 3D, Half-Life, Half-Life 2,
Heretic, Hexen, Marathon, Quake, Quake II, Quake 4, Rise of the Triad, Shadow
Warrior, System Shock 2, Thief, Unreal and Unreal Tournament — through a small
web UI that multiple people on your network can use at once.

<img src="static/icon.png" width="96" alt="">

## What it does

- Talks directly to your Hue Bridge's local API (no cloud account needed).
- Runs each light's flicker as its own async loop, all going through one
  shared rate limiter so you can't accidentally flood the bridge.
- Serves a web UI for picking lights, patterns, speed (Hz), brightness
  range, transition time, and an optional color.
- Broadcasts live state over a WebSocket, so if two people have the page
  open, they see the same running/stopped state and controls in real time.
- Lets you write and save custom a–z lightstyle strings as reusable
  patterns, with a live waveform preview and their own speed, brightness
  range, transition and colour.
- Exports your patterns to a small JSON file you can share, and imports
  files other people send you.
- Retunes running lights on the fly — pattern, speed, brightness and color
  all take effect mid-flicker, no stop-and-restart.
- Groups any set of bulbs behind one set of controls, flickering in step,
  and can copy the rooms and zones already set up in the Hue app.
- Reads each bulb's colour and brightness before it starts, and puts it back
  when the flicker stops — even if the container was killed mid-run.
- Optionally locks the console behind a password you set in the UI.
- Persists everything in `/data/config.json` (mount that as a volume so it
  survives container restarts).

**Everything is configured from the web UI.** The container takes no
environment variables beyond the config path — bridge pairing, custom
patterns, the send-rate cap and the console password all live in the
Settings panel and in `config.json`.

## Install on Unraid

The easiest route is the template in this repo:

1. In Unraid, go to **Docker → Add Container**, and paste this into the
   *Template* field at the top:

   ```
   https://raw.githubusercontent.com/Krippler/LightHue/main/unraid-template.xml
   ```

2. Check the two settings it fills in:
   - **WebUI Port** — defaults to `26000`, Quake's own registered port, chosen
     to stay clear of the 8080 crowd on a typical Unraid box. Change it if you
     already use it for something.
   - **Config Storage** — defaults to `/mnt/user/appdata/game-hue-flicker`.
     This holds your bridge pairing, saved patterns and console password.
     Keep it mapped or you'll re-pair after every update.

3. **Apply**, then open the WebUI from the Docker tab.

The image is published to `ghcr.io/krippler/lighthue:latest` and rebuilt on
every push to `main`, so Unraid's update check works normally.

Work branches publish under their own tag — `ghcr.io/krippler/lighthue:<branch>`,
with `/` written as `-`. Point a container at one to test a fix on real hardware
without putting it on `main` first, which matters most for the parts that can
only be proven against a real bridge.

**If bridge discovery or pairing fails**, set *Network Type* to **Host** on
the container and try again. Some Unraid setups don't let the default bridge
network reach the Hue Bridge or the discovery endpoint.

Host networking ignores port mappings — the container binds the server's port
directly. If 26000 is taken there, add a `PORT` variable to the container
instead of changing the port mapping.

## Run it anywhere else

```bash
docker compose up -d --build
```

Then open `http://<your-server-ip>:26000`. To use the published image instead
of building, swap the `build: .` line in `docker-compose.yml` for
`image: ghcr.io/krippler/lighthue:latest`.

**First run:** the UI will prompt you to either auto-discover your bridge
or enter its IP manually, then walk you through pairing (press the
physical link button on the bridge, then hit Pair within ~30 seconds).

It has to be an IP address, not a host name — `192.168.1.23`, or
`192.168.1.23:8080` if you run something like diyHue on another port. That
string decides which machine the console makes requests to, so it is checked
before anything is sent: host names are refused (no name lookup means nothing
can point one somewhere else later), as are loopback, link-local, multicast
and reserved addresses, none of which a bridge is ever on. Any other address
works, so a bridge across a VPN or a routed subnet is fine.

## Patterns

Every built-in pattern is named after the game it comes from, and the picker
groups them by game, listed alphabetically. There are two kinds, and the
difference is worth knowing:

**Straight from the engine.** Quake stored its light effects as literal `a`–`z`
strings — styles 0–11 plus 63 — and they're here transcribed from id's released
source, not from memory. Quake II ships that table byte for byte. GoldSrc
(Half-Life) and Source (Half-Life 2, Portal, TF2, Left 4 Dead) ship it too and
add style 12, the underwater mutation.

Because those are literally the same strings, they exist once and are *shared*
into each game's menu rather than copied — `shared_with` in the API. Picking
"Quake II — 4 Fast Strobe" and "Quake — 4 Fast Strobe" runs the same lightstyle,
because in the engines it is the same lightstyle. The same goes for the Unreal
Engine 1 games: Unreal Tournament and Deus Ex list Unreal's `LT_*` light types.
Copying the strings would have filled the picker with dozens of identical
effects under different names.

The exact tables are pinned in `tests/test_patterns.py` against the four
released sources, because a wrong style 8 sat here unnoticed until someone
checked.

**Written here, in that style.** Everything else — DOOM and its descendants
(Heretic, Hexen), the Build games (Duke Nukem 3D, Blood, Shadow Warrior), the
Dark engine games (Thief, System Shock 2), id Tech 4 (Doom 3, Quake 4),
Descent, Marathon, Rise of the Triad and Unreal — doesn't store light effects
as strings at all. They run procedural sector, actor and material effects in
code. Those presets are hand-authored sequences approximating the documented
behaviour at roughly the original timing. They're a tribute, not a dump of
engine data, and the API marks them `origin: "inspired"` so you can tell them
apart. The picker says which is which on hover.

**Quake III Arena is deliberately absent**: id Tech 3 ships no default
lightstyle table in its game code, so there's nothing verbatim to claim.

**Every pattern carries its own framing** — speed, brightness window,
transition and colour, not just the letters. A sputtering bulb and a slow
gothic throb aren't the same shape played faster or slower, so picking a
pattern brings all of it along and the light looks right without you tuning
anything:

| | |
|---|---|
| Blood — Guttering Torch | 10 Hz, 40–215, 100 ms, warm orange |
| Shadow Warrior — Paper Lantern | 4 Hz, 60–200, 300 ms, soft amber |
| Doom 3 — Corridor Strobe | 12 Hz, full range, hard steps, no colour |
| Hexen — Slow Mana Pulse | 5 Hz, 20–230, 200 ms, violet |
| Quake 4 — Strogg Machinery | 8 Hz, 35–225, 100 ms, sickly green |

Flame effects keep a brightness floor and a little smoothing, because a real
flame doesn't go out between frames. Failing tubes and strobes do the opposite:
they snap, and they go dark. You can still drag any slider afterwards and it
sticks.

**Colour is opt-in per pattern.** Half of them name one — torches are orange,
neon is magenta, emergency lighting is red — and half deliberately don't, so
they leave whatever colour you set in the Hue app alone. Unticking **Set
color** overrides a pattern that names one; the brightness framing still
applies.

Under each swatch is a box you can type a colour into, which is the way to get
an exact one. It takes either form:

| You type | You get |
|---|---|
| `6000,225` | exactly that — Hue's own hue and saturation |
| `#ff991d` | the nearest hue/sat, `5993,225` |
| `#f80` | shorthand hex, expanded |

`hue,sat` is the exact form because it's what the bridge actually takes and
what the pattern table stores; a trip through RGB has to round. The box shows
the numbers being sent, so picking a preset tells you its exact colour. Typing
something unparseable marks the box red and changes nothing.

The engine-sourced styles are deliberately left unframed: full range, no
smoothing, no colour, and all 10 Hz because that is the rate those engines step
the lightstyle table at. Their `a`–`z` curve *is* the whole brightness story,
and colour came from the map's light entity rather than the style, so framing
them would misrepresent them.

Transitions move in 100 ms steps, because that is the resolution the bridge
accepts — anything finer is truncated on the way through.

Add your own in `app/patterns.py`, or write them in the UI (see below).

## Sharing patterns

Custom patterns can be moved between consoles as a file. **Export to file** in
the Custom Lightstyles panel downloads everything you've written; **Import from
file** loads one someone sent you.

The format is deliberately small, so a pack can be written by hand in a text
editor and sent to someone who then just imports it:

```json
{
  "format": "game-hue-flicker/patterns",
  "version": 1,
  "name": "Doom 3 style flicker",
  "author": "someone",
  "patterns": [
    {
      "name": "Sputtering Lamp",
      "sequence": "mmnnaamm",
      "hz": 12,
      "min_bri": 40,
      "max_bri": 220,
      "transition_ms": 100,
      "hue": 6000,
      "sat": 220
    }
  ]
}
```

Only `patterns` is required — `{"patterns": [...]}` on its own imports fine.
Each pattern may state the framing it was written for; anything it leaves out
falls back to Quake's own defaults, 10 frames a second across the bulb's full
range with no smoothing and no colour of its own. `hue` and `sat` go together
or not at all.
Sequences are normalised on the way in, so spacing and capitalisation don't
matter. Unknown keys are ignored, which leaves room for packs to carry extra
metadata without older consoles choking on it.

Importing never overwrites anything you already have:

- A pattern whose sequence you already have — under any name, custom or
  built-in — is skipped and reported, because two names for one identical
  effect is just clutter in the picker.
- A pattern whose *name* collides but whose sequence differs is added as
  "Torchlight (2)" rather than replacing yours.
- If any entry in the file is malformed the whole import is refused and
  nothing changes, with a message naming the entry at fault.

Exported files contain only names and sequences — no bridge details, no
credentials, nothing console-specific — so they're safe to pass around.

## Groups

Tick the lights you want to run together in the **Groups** panel, give them a
name, and they get one card whose controls drive all of them at once. Members
keep their own cards too, so you can still tweak one light without leaving the
group.

**Or copy one from the bridge.** The Hue app already knows your lighting as
*Rooms* (a light belongs to exactly one) and *Zones* (any set, overlapping
allowed). **Use a room from the bridge** lists both and copies one over in a
click, rather than making you pick the same bulbs again. LightHue's groups
behave like zones, so either kind copies across fine.

The button is a toggle — it reads *Hide bridge rooms* and highlights while the
list is open, and the list appears directly beneath it.

Luminaires and Entertainment areas aren't offered: the first describes the
innards of a single fitting, the second belongs to Hue's streaming API. A room
containing a bulb this console can't see is listed with that noted and no Add
button, because driving a light id that isn't there just burns bridge budget.

A group card shows `2/3 FLICKERING` when only some members are running, and
offers **Start the rest** alongside **Stop** so you can bring stragglers into
line without interrupting the ones already going.

**Staying in step.** Lights in a group are given a shared start instant, and
each one works out which frame of the pattern is due *now* from that instant
rather than counting its own ticks. That matters because the bridge budget is
shared: a light that gets served less often would otherwise fall progressively
further behind, and a group would drift apart the longer it ran. The rate
limiter still hands out slots one at a time — bulbs physically cannot be sent
to simultaneously — so the residual offset is one limiter slot, not a growing
gap.

If you ask for more frames per second than a light's share of the budget can
carry, the pattern is run at the rate that share allows rather than sampled at
the higher one. Sampling would alias: a two-frame strobe served every second
frame sits on one value and stops flickering altogether. The card shows the
rate it's actually running at whenever that's below what you asked for.

## Changing things mid-flicker

Every control retunes a running light in place — pattern, speed, brightness
window, transition and color all apply without restarting the loop, and the
change reaches everyone else's browser over the WebSocket.

The color swatch is always live, and starts from the color the bulb is actually
showing rather than a fixed default. Picking a color ticks **Set color** for you
rather than making you find the box first. Leaving that box unticked means the
console won't touch the bulb's color at all — useful when you've already set a
color in the Hue app and just want the flicker.

Unticking **Set color** mid-run doesn't revert anything on its own; it just
stops sending further color changes. Stopping the flicker is what puts the bulb
back — see below.

## Putting lights back

Before a light starts flickering, the console reads its current state off the
bridge — on/off, brightness, and whichever of hue/sat, xy or ct the bulb is
actually using — and keeps it. Stopping the flicker restores exactly that.
Restarting a light that's already running keeps the *original* snapshot, so the
thing you get back is always the state from before any of this started.

That snapshot is written to `config.json`, so a container that dies mid-flicker
still puts the bulbs back on its next start rather than leaving them stuck at
whatever brightness the last tick happened to land on.

Turn it off with **Restore lights to how they were** in Settings if you'd rather
lights stay where the flicker ends. The snapshot is still kept either way, and
each card grows a **Revert** button you can hit whenever you want it back.

## Entertainment streaming

The ordinary path sends one HTTP command per light, and the bridge takes about
ten a second in total for everything. That budget is shared, so the more lights
flicker at once the slower each one gets:

| lights | at 10/sec | at 20/sec | at 30/sec |
|-------:|----------:|----------:|----------:|
| 2      | 4.00 Hz   | 8.00 Hz   | 12.00 Hz  |
| 4      | 2.00 Hz   | 4.00 Hz   | 6.00 Hz   |
| 7      | 1.14 Hz   | 2.29 Hz   | 3.43 Hz   |

Seven bulbs at a real 10 Hz would need about 88 commands a second, which is not
something the REST API will do at any setting.

**Entertainment streaming is the way past that.** One UDP socket carries a
frame holding every light in the area, so a frame costs the same whether it
holds one bulb or ten, and the speed stops being divided — up to 25 Hz across
the whole area at once, every light changing on the same frame.

### What it needs

1. **An entertainment area**, set up in the Hue app under *Entertainment
   areas*. The bridge will only stream to one it already knows about, it holds
   at most ten lights, and they have to be colour-capable.
2. **A pairing that includes a streaming key.** The DTLS pre-shared key is only
   issued when the pairing request asks for it, and there is no way to fetch
   one for an existing user — so a console set up before this feature has to
   pair again. The panel says so when that applies.
3. **Nothing else streaming to the same area.** Hue Sync and games claim the
   area exclusively; areas already taken are listed but greyed out.

The bridge does *not* need to be on the same subnet as the console. Streaming
is UDP where the rest of the API is HTTP, and it routes across a boundary like
anything else.

### Handing the area back

While an area is streaming the bridge ignores everything else for those lights,
including the Hue app, so the console hands the area back when you press Stop,
when the sender stops for any reason at all, and on the way out if the container
is stopped mid-stream. The release is checked rather than assumed: a bridge that
accepts the call and keeps holding the area would otherwise leave those lights
answering to nothing, with nothing on screen saying why.

Streaming leaves each bulb on whatever the last frame held, so the console
reads the area's lights before it claims the area and puts them back after it
releases it — the same snapshot the normal path uses, and in that order,
because a bridge that is streaming to an area ignores anything else sent to
those lights.

Stopping sends the bridge a DTLS close_notify before dropping the socket. The
bridge allows one entertainment session and keeps it on its books until it is
told the last one ended, so a stream that closes quietly can leave a ghost
session behind.

On startup the console hands back any entertainment area the bridge still
records as claimed by its own API key. That covers the case nothing else does:
a container killed, redeployed or crashed mid-stream leaves the claim behind,
and until something clears it those lights answer to nothing at all.
**Release area** clears one by hand, including a claim left by something else,
which beats restarting the bridge.

### The DTLS client

Streaming opens a DTLS 1.2 connection secured with a pre-shared key. The
console carries two clients and tries them one per attempt, each against a
freshly armed area:

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

### When a stream will not start

**Diagnostics** shows what the bridge says about each area, what the last start
attempt did step by step, the bridge's own model and firmware, and — when a
start fails — a deeper look taken while the area is still claimed.

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

### Diagnosing from a shell

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

For a failure that only happens sometimes, `--repeat` runs the same attempt on
a loop and reports the shape of it rather than one sample. Evenly spaced
successes mean state carried from one attempt to the next; scattered ones mean
loss, and those want completely different fixes.

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

Streaming has no transition setting: every frame is sent, so there is nothing
to interpolate between.
## Settings

Open **Settings** in the bar above the panels.

**Bridge send rate.** A global ceiling on commands per second across every
flickering light. Philips advises against sustained bursts past ~10/second,
which is the default. The limiter serializes updates rather than dropping
them, so with enough lights × Hz the *effective* per-light update rate falls
below what you asked for — several lights at 10 Hz will each visibly slow
down. That's the trade for not overwhelming the bridge.

**When flicker stops.** Reads each bulb's colour and brightness before it
starts and puts it back on stop, even after a container restart. On by default.

**Stream settle.** How long to wait between handing an entertainment area to
the stream and opening the connection. The bridge answers on the streaming port
before the session behind it is ready, and some bridges want a moment. Default
1500 ms; drop it to 0 if your bridge does not need it, since it is dead time on
every start.

**Console password.** Off by default: anyone who can reach the port can drive
your lights. Set a password and every API route and the WebSocket start
requiring it, with a login prompt on the page. Passwords are stored as a
PBKDF2-SHA256 hash, never in the clear, and changing one signs out every
other open console. Scripts can authenticate with a header instead of the
session cookie:

```bash
curl -H 'X-Console-Password: yourpassword' http://server:26000/api/lights
# or: -H 'Authorization: Bearer yourpassword'
```

Even with a password set, this is a LAN tool — don't port-forward it.

Panels can be dragged into whatever order you like — grab the grip at the left
of a panel's title bar — and clicking a title bar folds that panel away. Both
are remembered per-browser; **Reset layout** puts the order back.

## Project layout

```
app/
  main.py            FastAPI routes + WebSocket
  auth.py            Optional console password + gating middleware
  hue_client.py      Hue Bridge HTTP client, v1 (discover/pair/lights/state)
  hue_v2.py          Hue v2 API, only as far as entertainment configurations
  flicker_engine.py  Per-light async flicker loops + rate limiter
  stream_engine.py   One entertainment area over one socket, at a fixed rate
  hue_stream.py      HueStream framing, the DTLS transport, and the probes
  dtls_psk.py        A DTLS 1.2 PSK client, hand-rolled, that retransmits
  patterns.py        Built-in flicker patterns, by game
  packs.py           Shareable pattern-pack file format
  config_store.py    Persisted JSON config (bridge creds, patterns, settings)
static/
  index.html, style.css, app.js   The control UI
scripts/
  probe_stream.py    Standalone handshake probe — stdlib only, runs anywhere
  capture_stream.sh  Packet capture wrapped around one handshake attempt
tests/                            pytest suite
unraid-template.xml               Unraid Community Applications template
```

`app/dtls_psk.py` and the probe carry deliberately duplicated copies of the
handshake framing — the probe has to run on a machine with no checkout and no
dependencies, so it cannot import the app. A test asserts the two produce
byte-identical ClientHellos, which is what keeps the duplication honest.

## If the UI looks stale

The badge beside the header's status light shows the build of the interface
the page is running, and
`GET /api/version` reports the same string. Asset URLs carry that version and
the page itself is served `no-store`, so a rebuild always reaches the browser
rather than sitting behind a cached `app.js`. If that badge doesn't change
after an update, the container is still serving the old files — rebuild and
pull rather than hunting for a UI bug.

## Development

```bash
pip install -r requirements-dev.txt
CONFIG_PATH=./data/config.json uvicorn app.main:app --reload --port 26000

pytest        # API, auth, flicker engine, config store, patterns
ruff check .  # lint
```

CI runs the suite on Python 3.11 and 3.12, plus a Docker build-and-boot
check, on every push and pull request.

## Extending it

- Add more built-in patterns in `app/patterns.py`.
- The `/api` routes are plain REST + one `/ws` WebSocket — easy to script
  against from Home Assistant, a Stream Deck plugin, etc.
