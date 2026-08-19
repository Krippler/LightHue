# Quake Hue Flicker Console

Drives Philips Hue lights using Quake's classic "lightstyle" flicker patterns
(styles 0–11, straight from id Software's engine), through a small web UI
that multiple people on your network can use at once.

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
  patterns, with a live waveform preview.
- Retunes running lights on the fly — pattern, speed, brightness and color
  all take effect mid-flicker, no stop-and-restart.
- Groups any set of bulbs behind one set of controls, so they stay identical.
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
   - **Config Storage** — defaults to `/mnt/user/appdata/quake-hue-flicker`.
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

## Groups

Tick the lights you want to run together in the **Groups** panel, give them a
name, and they get one card whose controls drive all of them at once. Members
keep their own cards too, so you can still tweak one light without leaving the
group.

A group card shows `2/3 FLICKERING` when only some members are running, and
offers **Start the rest** alongside **Stop** so you can bring stragglers into
line without interrupting the ones already going.

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
  patterns.py        Built-in Quake lightstyle table
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
