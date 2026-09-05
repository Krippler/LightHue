# LightHue

Drives Philips Hue lights with the flicker patterns of twenty classic games —
Blood, Descent, Deus Ex, DOOM, Doom 3, Duke Nukem 3D, Half-Life, Half-Life 2,
Heretic, Hexen, Marathon, Quake, Quake II, Quake 4, Rise of the Triad, Shadow
Warrior, System Shock 2, Thief, Unreal and Unreal Tournament — through a small
web UI that several people on your network can use at once.

<img src="static/icon.png" width="96" alt="">

- Talks to your Hue Bridge directly over your own network. No Philips account.
- Pick lights, pattern, speed, brightness range and colour — and change any of
  it while the lights are still running.
- Build entertainment areas on the bridge and stream a whole set at once, up to
  25 Hz — far faster than driving lights one at a time.
- Write your own patterns, with a live preview, and share them as a file.
- Puts every bulb back how it was when the flicker stops.
- Optional password to lock the console.

**Everything is configured in the web UI.** There are no environment variables
to set beyond the config path.

## Install on Unraid

1. **Docker → Add Container**, and paste this into the *Template* field:

   ```
   https://raw.githubusercontent.com/Krippler/LightHue/main/templates/lighthue.xml
   ```

2. Check the two settings it fills in:
   - **WebUI Port** — defaults to `26000`. Change it if you already use it.
   - **Config Storage** — holds your bridge pairing, saved patterns and
     password. Keep it mapped or you'll re-pair after every update.

3. **Apply**, then open the WebUI from the Docker tab.

If discovery or pairing fails, set *Network Type* to **Host** and try again —
some Unraid setups don't let the default bridge network reach the Hue Bridge.
Host networking ignores port mappings, so use the **Port (host networking
only)** field under *Show advanced settings* to move the listener instead.

## Run it anywhere else

```bash
docker compose up -d --build
```

Then open `http://<your-server-ip>:26000`. To use the published image instead
of building, swap `build: .` in `docker-compose.yml` for
`image: ghcr.io/krippler/lighthue:latest`.

## First run

The UI walks you through it: auto-discover your bridge or type its IP, then
press the physical link button on the bridge and hit **Pair** within about
30 seconds.

It has to be an IP address rather than a host name — `192.168.1.23`, or
`192.168.1.23:8080` if you run something like diyHue on another port.

## Patterns

Patterns are grouped by game in the picker. There are two kinds and the
difference is marked on hover:

**Straight from the engine.** Quake stored its light effects as literal `a`–`z`
strings, and they're here transcribed from id Software's released source rather
than from memory. Quake II, Half-Life and Source ship the same table unchanged,
so those styles are *shared* into each game's menu rather than duplicated —
picking "Quake II — 4 Fast Strobe" runs the same lightstyle as the Quake one,
because in the engines it is the same lightstyle.

**Written here, in that style.** DOOM, Heretic, Hexen, the Build games, Thief,
System Shock 2, Doom 3, Quake 4, Descent, Marathon, Rise of the Triad and
Unreal don't store light effects as strings at all — they run them in code.
Those presets approximate the documented behaviour at roughly the original
timing. They're a tribute, not a dump of engine data.

*Quake III Arena is deliberately absent*: id Tech 3 ships no default lightstyle
table, so there's nothing verbatim to claim.

**Every pattern carries its own framing** — speed, brightness window,
transition and colour, not just the letters. Picking one brings all of it
along, so the light looks right without tuning anything:

| | |
|---|---|
| Blood — Guttering Torch | 10 Hz, 40–215, 100 ms, warm orange |
| Shadow Warrior — Paper Lantern | 4 Hz, 60–200, 300 ms, soft amber |
| Doom 3 — Corridor Strobe | 12 Hz, full range, hard steps, no colour |
| Hexen — Slow Mana Pulse | 5 Hz, 20–230, 200 ms, violet |
| Quake 4 — Strogg Machinery | 8 Hz, 35–225, 100 ms, sickly green |
| Quake — 3 Candle | 10 Hz, 30–210, 100 ms, warm orange |
| Quake — 4 Fast Strobe | 10 Hz, full range, hard steps, no colour |

Flames keep a brightness floor and a little smoothing, because a real flame
doesn't go out between frames. Failing tubes and strobes do the opposite: they
snap, and they go dark. Every slider still works afterwards.

**Colour is named where the fixture has one.** A candle is orange, a neon sign
is magenta, a Strogg machine is a sickly green — those patterns bring their
colour with them. A strobe, a generic flicker or a failing fluorescent has no
colour of its own, so those leave whatever you set in the Hue app alone and
just flicker it. Unticking **Set colour** overrides a pattern that names one.

Under each swatch is a box for an exact colour. It takes `6000,225` (Hue's own
hue and saturation, the exact form) or `#ff991d` / `#f80` (converted to the
nearest hue/sat). The box always shows the numbers being sent.

**A pattern that never changes is a hold, not a flicker.** Write `z` (or pick
Quake's own *0 Steady*) and the light is set to that brightness and colour and
left there — one command, then nothing. The button says **Hold this colour**,
the badge reads HOLDING, and moving a slider re-sends it once.

**Plugs can't flicker.** A plug is on or off, with no brightness in between,
and a flicker frame is a brightness. Plugs are still listed — greyed, at the
end of Lights & Plugs, saying so — but offered no controls. White-only bulbs do
flicker; they just lose the colour swatch they have no way to use.

## Entertainment areas

The ordinary path sends one command per light and the bridge takes about ten a
second in total, shared — so seven bulbs flicker at barely 1 Hz each.

**An area is the way past that.** The bridge streams to it as one unit, a single
frame carrying every light, so the speed stops being divided: up to 25 Hz across
the whole area.

Build one at the top of **Lights & Plugs**, beside the tickboxes that fill it:
tick the lights, name the set, **Save as area**. Or **Use a room from the
bridge** to tick a room you already made in the Hue app. It then appears in the
**Entertainment** panel, one row each — click a row to stream to it, and use the
✎ and × to rename or delete it.

Areas live on the bridge rather than in this console, so they show up in the Hue
app too and survive the container being replaced.

**Lights in one area can run different patterns at once.** Tick **Different
patterns per light** and each gets its own row. A stream frame already carries a
value per light, so this is free — the same frame, at the same 25 Hz. A row left
at *Same as the area* follows the settings below.

A row is a *channel* rather than a bulb: one channel can drive several lights (a
lightstrip usually spans several), so each row says what it actually moves.

Three limits, all the bridge's. An area holds at most **10 lights**, and only
**colour-capable** ones — saving one containing a plug or a white-only bulb is
refused with that light named. Only one area streams at a time, so an area Hue
Sync or a game has claimed is marked **in use elsewhere**. And you need a
pairing that included a streaming key; a console paired before this feature
existed has to pair again, and the panel says so.

The controls below the list set the pattern, speed, brightness, transition and
colour, and **Start** runs it. If a stream won't start, the error says what
happened.

For more than that, turn on **Streaming diagnostics** in Settings. It adds a
**Diagnostics** button reporting what the bridge is doing —
[docs/streaming.md](docs/streaming.md) covers how to read it. It ships off
because the report describes your bridge, your lights and your network, and it
is meant to be pasted into a bug report. Your bridge key is never in it either
way.

## If something goes wrong

The container checks its own health every 30 seconds and restarts itself if it
stops answering — including the case where it is still running but stuck, which
a restart policy alone cannot see. Lights left mid-flicker are put back.

It is also capped at 256 MB (steady state is about 66 MB), so a fault in here
stays in here rather than becoming a fault on your server.

The check reads `/api/health`, which is reachable without the console password
because Docker cannot log in. It reports uptime and lag, and nothing about your
bridge, lights or network.

## Custom patterns

Write your own `a`–`z` lightstyle strings in the **Custom Lightstyles** panel,
with a live waveform preview and their own speed, brightness range, transition
and colour. `a` is darkest through `z` brightest, one letter per frame.

The pencil on a saved pattern loads it back into the form to edit; **Update
pattern** writes over it, keeping the same pattern so every light already set
to it follows the change. A pattern that is currently running has to be stopped
before it can be edited.

**Export to file** downloads everything you've written; **Import from file**
loads one someone sent you. Exported files hold only names and sequences — no
bridge details, no credentials — so they're safe to pass around. Importing
never overwrites what you already have: duplicates are skipped, name clashes
are added as "Torchlight (2)", and a malformed file is refused whole.

The file format is documented in [docs/pattern-packs.md](docs/pattern-packs.md)
if you want to write one by hand.

## Settings

Open **Settings** in the bar above the panels.

**Bridge send rate.** A ceiling on commands per second across every flickering
light. Philips advises against sustained bursts past ~10/second, which is the
default. Raising it lets more lights run faster; the trade is more load on the
bridge.

**When flicker stops.** Puts each bulb back how it was, even after a container
restart. On by default. Turn it off to leave lights where the flicker ends —
each card still grows a **Revert** button.

**Stream settle.** A pause between claiming an entertainment area and
connecting to it. Some bridges want a moment; most don't. Default 1500 ms, and
dropping it to 0 makes streams start faster.

**Streaming diagnostics.** Off by default. Turning it on adds a **Diagnostics**
button to the Entertainment panel; see [docs/streaming.md](docs/streaming.md).

**Bridge.** Shows which bridge you are paired with, and **Change bridge** pairs
with a different one — or with the same one again, which is how a console
paired before streaming existed gets a streaming key.

**Console password.** Off by default — anyone who can reach the port can drive
your lights. Setting one puts a login prompt on the page and requires it on
every API route. Scripts can send a header instead:

```bash
curl -H 'X-Console-Password: yourpassword' http://server:26000/api/lights
```

Even with a password, this is a LAN tool — don't port-forward it.

Panels can be dragged into any order by the grip at the left of their title
bar, and clicking a title bar folds one away. Both are remembered per browser;
**Reset layout** puts them back.

## If the UI looks stale

The badge beside the header's status light shows which build of the interface
the page is running. If it doesn't change after an update, the container is
still serving the old files — rebuild and pull rather than hunting for a UI
bug.

## Development

```bash
pip install -r requirements-dev.txt
CONFIG_PATH=./data/config.json uvicorn app.main:app --reload --port 26000

pytest        # API, auth, engines, config store, patterns
ruff check .  # lint
```

CI runs the suite on Python 3.11 and 3.12 plus a Docker build-and-boot check.
The image is published to `ghcr.io/krippler/lighthue:latest` on every push to
`main`; work branches publish under `ghcr.io/krippler/lighthue:<branch>`.

- Add built-in patterns in `app/patterns.py`.
- The `/api` routes are plain REST plus one `/ws` WebSocket, so scripting
  against them from Home Assistant or a Stream Deck plugin is straightforward.
- [docs/streaming.md](docs/streaming.md) covers how entertainment streaming
  works and how to diagnose it.
- [docs/pattern-packs.md](docs/pattern-packs.md) is the pattern-pack format.
