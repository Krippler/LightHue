# Game Hue Flicker Console

Drives Philips Hue lights with the flicker patterns of twenty classic games —
DOOM, Marathon, Heretic, Descent, Hexen, Rise of the Triad, Duke Nukem 3D,
Quake, Blood, Shadow Warrior, Quake II, Unreal, Half-Life, Thief, System
Shock 2, Unreal Tournament, Deus Ex, Doom 3, Half-Life 2 and Quake 4 —
through a small web UI that multiple people on your network can use at once.

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
  patterns, with a live waveform preview and their own speed.
- Exports your patterns to a small JSON file you can share, and imports
  files other people send you.
- Retunes running lights on the fly — pattern, speed, brightness and color
  all take effect mid-flicker, no stop-and-restart.
- Groups any set of bulbs behind one set of controls, flickering in step.
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

## Patterns

Every built-in pattern is named after the game it comes from, and the picker
groups them by game. There are two kinds, and the difference is worth knowing:

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

**Every pattern carries its own speed.** A sputtering bulb and a slow gothic
throb aren't the same shape played faster or slower, so the rate is stored with
the sequence and picking a pattern brings its timing along — Shadow Warrior's
paper lantern comes up at 4 Hz, Unreal's `LT_Flicker` at 16. The engine-sourced
styles are all 10 Hz because that is the rate those engines step the lightstyle
table at; that one isn't a taste call. You can still drag
the speed slider afterwards and it will stick.

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
    { "name": "Sputtering Lamp", "sequence": "mmnnaamm", "hz": 12 }
  ]
}
```

Only `patterns` is required — `{"patterns": [...]}` on its own imports fine,
and a pattern's `hz` defaults to 10 the way Quake's own lightstyles run.
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

Turn it off with **Put lights back how they were** in Settings if you'd rather
lights stay where the flicker ends. The snapshot is still kept either way, and
each card grows a **Revert** button you can hit whenever you want it back.

## Settings

Open **Settings** in the toolbar.

**Bridge send rate.** A global ceiling on commands per second across every
flickering light. Philips advises against sustained bursts past ~10/second,
which is the default. The limiter serializes updates rather than dropping
them, so with enough lights × Hz the *effective* per-light update rate falls
below what you asked for — several lights at 10 Hz will each visibly slow
down. That's the trade for not overwhelming the bridge.

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

## Project layout

```
app/
  main.py            FastAPI routes + WebSocket
  auth.py            Optional console password + gating middleware
  hue_client.py      Hue Bridge HTTP client (discover/pair/lights/state)
  flicker_engine.py  Per-light async flicker loops + rate limiter
  patterns.py        Built-in flicker patterns, by game
  packs.py           Shareable pattern-pack file format
  config_store.py    Persisted JSON config (bridge creds, patterns, settings)
static/
  index.html, style.css, app.js   The control UI
tests/                            pytest suite
unraid-template.xml               Unraid Community Applications template
```

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
