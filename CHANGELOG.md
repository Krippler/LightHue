# Changelog

Notable changes to LightHue. Versions are the image tags published to
`ghcr.io/krippler/lighthue`, so `v0.5.0` here is `:v0.5.0` there. `:latest`
always tracks the newest release.

## 0.5.0 — 2026-09-05

First tagged release, and the first packaged for Unraid Community
Applications. Everything below arrived between the initial commit and this
tag.

### Lights and patterns

- 64 presets across twenty games. Quake's lightstyle table is verbatim from
  id Software's released source; the rest are hand-authored.
- Every preset carries the speed, brightness range, transition and colour it
  was written for, so picking one brings its timing with it.
- Write your own a-z lightstyle strings with a live waveform preview, and
  export them to a JSON file to share.
- A pattern with one brightness level is held steady rather than looped.
- Controls a device cannot honour are not offered: plugs switch on and off,
  and white bulbs get no colour picker.
- Every bulb's colour and brightness is read before it starts and put back
  when the flicker stops, even if the container restarted mid-run.

### Entertainment streaming

- Stream a whole area at up to 25 Hz over the bridge's entertainment API,
  instead of spending one command per light out of the roughly ten a second
  the bridge allows.
- Create and delete entertainment areas from the console, built from the
  lights you tick.
- Each light in one area can run a different pattern at the same time.
- Streaming diagnostics ship switched off, with a toggle in Settings.

### Running it

- Optional password on the console.
- A health endpoint and a Docker HEALTHCHECK, so a wedged event loop is
  restarted instead of sitting there looking alive.
- A 256 MB memory ceiling, so a fault in the container stays in the
  container.
- Unraid template, maintainer profile and screenshot for a Community
  Applications listing.

### Fixed

- The bridge API key was returned in the diagnostics output. It no longer is.
