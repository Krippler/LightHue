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
   - **WebUI Port** — defaults to `8080`. Change it if something else on the
     server already uses that port.
   - **Config Storage** — defaults to `/mnt/user/appdata/quake-hue-flicker`.
     This holds your bridge pairing, saved patterns and console password.
     Keep it mapped or you'll re-pair after every update.

3. **Apply**, then open the WebUI from the Docker tab.

The image is published to `ghcr.io/krippler/lighthue:latest` and rebuilt on
every push to `main`, so Unraid's update check works normally.

**If bridge discovery or pairing fails**, set *Network Type* to **Host** on
the container and try again. Some Unraid setups don't let the default bridge
network reach the Hue Bridge or the discovery endpoint.

## Run it anywhere else

```bash
docker compose up -d --build
```

Then open `http://<your-server-ip>:8080`. To use the published image instead
of building, swap the `build: .` line in `docker-compose.yml` for
`image: ghcr.io/krippler/lighthue:latest`.

**First run:** the UI will prompt you to either auto-discover your bridge
or enter its IP manually, then walk you through pairing (press the
physical link button on the bridge, then hit Pair within ~30 seconds).

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
curl -H 'X-Console-Password: yourpassword' http://server:8080/api/lights
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
CONFIG_PATH=./data/config.json uvicorn app.main:app --reload --port 8080

pytest        # API, auth, flicker engine, config store, patterns
ruff check .  # lint
```

CI runs the suite on Python 3.11 and 3.12, plus a Docker build-and-boot
check, on every push and pull request.

## Extending it

- Add more built-in patterns in `app/patterns.py`.
- The `/api` routes are plain REST + one `/ws` WebSocket — easy to script
  against from Home Assistant, a Stream Deck plugin, etc.
