# Pattern pack format

A pack is a small JSON file holding custom lightstyles. **Export to file** in
the Custom Lightstyles panel writes one; **Import from file** reads one. The
format is deliberately simple so a pack can be written by hand.

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

## Fields

| field | meaning |
|---|---|
| `sequence` | The lightstyle, one letter per frame: `a` darkest through `z` brightest, mapped evenly across the brightness window below. |
| `hz` | Frames per second. Defaults to 10, the rate the original engines stepped their lightstyle table at. |
| `min_bri` / `max_bri` | The brightness window the sequence is mapped into, 1–254. Defaults to the bulb's full range. |
| `transition_ms` | Smoothing between frames, in 100 ms steps — that is the resolution the bridge accepts. Defaults to none. |
| `hue` / `sat` | Hue's own colour numbers. Both or neither. Omit them and the pattern leaves whatever colour is already set alone. |

Sequences are normalised on the way in, so spacing and capitalisation don't
matter. Unknown keys are ignored, which leaves room for packs to carry extra
metadata without older consoles choking on it.

## What importing does

Importing never overwrites anything you already have:

- A pattern whose sequence you already have — under any name, custom or
  built-in — is skipped and reported. Two names for one identical effect is
  just clutter in the picker.
- A pattern whose *name* collides but whose sequence differs is added as
  "Torchlight (2)" rather than replacing yours.
- If any entry in the file is malformed the whole import is refused and nothing
  changes, with a message naming the entry at fault.

Exported files contain only names and sequences — no bridge details, no
credentials, nothing console-specific — so they are safe to pass around.

## A note on the format id

`game-hue-flicker/patterns` is the project's original name. It stays as it is:
it is written into every pack anyone has already exported, and changing it
would make those files fail to import.
