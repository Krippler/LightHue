# Quake Hue Flicker Console

Drives Philips Hue lights using Quake's classic "lightstyle" flicker patterns
(styles 0–11, straight from id Software's engine), through a small web UI
that multiple people on your network can use at once.

## What it does

- Talks directly to your Hue Bridge's local API (no cloud account needed).
- Runs each light's flicker as its own async loop, all going through one
  shared rate limiter so you can't accidentally flood the bridge.
- Serves a web UI for picking lights, patterns, speed (Hz), brightness
  range, transition time, and an optional color.
- Broadcasts live state over a WebSocket, so if two people have the page
  open, they see the same running/stopped state and controls in real time.
- Lets you save custom a–z lightstyle strings as reusable patterns.
- Persists your bridge pairing + custom patterns in `/data/config.json`
  (mount that as a volume so it survives container restarts).

## Run it

```bash
docker compose up -d --build
```

Then open `http://<your-unraid-ip>:8080`.

**First run:** the UI will prompt you to either auto-discover your bridge
or enter its IP manually, then walk you through pairing (press the
physical link button on the bridge, then hit Pair within ~30 seconds).

If bridge discovery/pairing doesn't work from the default Docker bridge
network on your setup, uncomment `network_mode: host` in
`docker-compose.yml` and rebuild.

## Notes on Hue rate limits

Philips recommends against sustained bursts faster than ~10 commands/second
across the whole bridge. The server enforces a global minimum interval
between any two light commands (configurable in `flicker_engine.py`,
`RateLimiter`), so running several lights at a high Hz will serialize their
updates rather than overwhelm the bridge — just know that with enough
lights × Hz, the *effective* per-light update rate will drop below what
you asked for.

## Project layout

```
app/
  main.py            FastAPI routes + WebSocket
  hue_client.py       Hue Bridge HTTP client (discover/pair/lights/state)
  flicker_engine.py   Per-light async flicker loops + rate limiter
  patterns.py          Built-in Quake lightstyle table
  config_store.py     Persisted JSON config (bridge creds, custom patterns)
static/
  index.html, style.css, app.js   The control UI
```

## Extending it

- Add more built-in patterns in `app/patterns.py`.
- The `/api` routes are plain REST + one `/ws` WebSocket — easy to script
  against from Home Assistant, a Stream Deck plugin, etc.
