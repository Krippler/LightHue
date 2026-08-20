"""Shareable pattern pack files.

A pack is plain JSON, small enough to read and edit by hand, so a pattern you
write here can be mailed to someone else and loaded straight into their
console — and so a pack can be written in a text editor without this app
being involved at all.

    {
      "format": "game-hue-flicker/patterns",
      "version": 1,
      "name": "Doom 3 style flicker",
      "author": "someone",
      "patterns": [
        {"name": "Sputtering Lamp", "sequence": "mmnaammnnaamm"}
      ]
    }

Everything except `patterns` is optional, including each pattern's `hz` —
the speed it was written for, which defaults to 10 the way Quake's own
lightstyles run. Parsing is deliberately forgiving
about what it accepts and strict about what it produces: unknown keys are
ignored, whitespace and case in sequences are normalised, and anything that
would not be a valid pattern is rejected by name so the caller can say which
entry was bad.
"""

from datetime import UTC, datetime

FORMAT = "game-hue-flicker/patterns"
VERSION = 1

MAX_PATTERNS = 200
MIN_HZ = 0.5
MAX_HZ = 20.0
DEFAULT_HZ = 10.0
MAX_SEQUENCE = 1000
MAX_NAME = 60
ALPHABET = set("abcdefghijklmnopqrstuvwxyz")


class PackError(ValueError):
    """A pack file that cannot be read, with a message worth showing a user."""


def normalise_sequence(raw) -> str:
    if not isinstance(raw, str):
        raise PackError("sequence must be text")
    seq = "".join(raw.split()).lower()
    if not seq:
        raise PackError("sequence is empty")
    if len(seq) > MAX_SEQUENCE:
        raise PackError(f"sequence is longer than {MAX_SEQUENCE} characters")
    bad = sorted({c for c in seq if c not in ALPHABET})
    if bad:
        raise PackError(f"sequence may only contain letters a-z (found {''.join(bad)!r})")
    return seq


def normalise_name(raw) -> str:
    if not isinstance(raw, str):
        raise PackError("name must be text")
    name = " ".join(raw.split())
    if not name:
        raise PackError("name is empty")
    return name[:MAX_NAME]


def normalise_hz(raw, default: float = DEFAULT_HZ) -> float:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise PackError("hz must be a number")
    hz = float(raw)
    if not (MIN_HZ <= hz <= MAX_HZ):
        raise PackError(f"hz must be between {MIN_HZ} and {MAX_HZ}")
    return round(hz, 2)


def build(patterns, name: str | None = None, author: str | None = None) -> dict:
    """Assemble a pack for export."""
    pack = {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "patterns": [
            {"name": p["name"], "sequence": p["sequence"],
             "hz": p.get("hz", DEFAULT_HZ)}
            for p in patterns
        ],
    }
    if name:
        pack["name"] = name
    if author:
        pack["author"] = author
    return pack


def parse(payload) -> list[dict]:
    """Read a pack into a list of {name, sequence}, or raise PackError."""
    if not isinstance(payload, dict):
        raise PackError("this doesn't look like a pattern pack — expected a JSON object")

    declared = payload.get("format")
    if declared is not None and declared != FORMAT:
        raise PackError(f"unknown pack format {declared!r}; expected {FORMAT!r}")

    version = payload.get("version", VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise PackError("version must be a whole number")
    if version > VERSION:
        raise PackError(
            f"pack is version {version}, but this console only understands up to {VERSION}"
        )

    entries = payload.get("patterns")
    if not isinstance(entries, list):
        raise PackError("pack has no 'patterns' list")
    if not entries:
        raise PackError("pack contains no patterns")
    if len(entries) > MAX_PATTERNS:
        raise PackError(f"pack contains more than {MAX_PATTERNS} patterns")

    out = []
    for i, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise PackError(f"pattern {i} is not an object")
        try:
            name = normalise_name(entry.get("name"))
            sequence = normalise_sequence(entry.get("sequence"))
            hz = normalise_hz(entry.get("hz"))
        except PackError as e:
            raw_name = entry.get("name")
            label = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else f"pattern {i}"
            raise PackError(f"{label}: {e}") from e
        out.append({"name": name, "sequence": sequence, "hz": hz})
    return out


def unique_name(name: str, taken) -> str:
    """Suffix a name until it stops colliding, the way a file manager would."""
    if name not in taken:
        return name
    n = 2
    while f"{name} ({n})"[:MAX_NAME] in taken:
        n += 1
    return f"{name} ({n})"[:MAX_NAME]
