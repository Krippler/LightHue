"""Flicker patterns from (and inspired by) classic game engines.

Each character is one frame of brightness, 'a' darkest through 'z' brightest.
That encoding is Quake's, and it turns out to describe most engines' light
effects perfectly well. (In the engines 'm' is normal and 'z' is overbright;
a bulb has no headroom above full, so a-z is mapped straight onto the
brightness range you pick per light.)

Every pattern also carries the rate its sequence was written for, because the
speed is as much part of an effect as its shape: a sputtering bulb and a slow
gothic throb are not the same thing played faster or slower. Quake's engine
steps lightstyles at 10 frames per second and everything descended from it
inherited that, so the styles taken verbatim from those tables say 10 — that
is the rate the strings mean, not a preference. DOOM's effects are timed in
35ths of a second, and those sequences were written to land on the real
durations at the rate given. For the rest the rate is authored alongside the
letters.

Two kinds of pattern live here, and the difference is worth keeping honest:

  origin="engine"
      Copied verbatim from the game's own lightstyle table, checked against
      the released source rather than memory. Quake shipped styles 0-11 and 63
      as literal a-z strings; Quake II, Half-Life and Source all ship that same
      table byte for byte, and GoldSrc/Source add style 12. Because they are
      the same strings, they are listed once and shared into each game's menu
      via shared_with rather than copied.

  origin="inspired"
      Hand-authored here. DOOM and its descendants, the Build games, the Dark
      engine games, id Tech 4 and the rest don't store light effects as strings
      at all — they run procedural sector/actor/material effects in code. These
      sequences approximate the documented behaviour at roughly the original
      timing; they are a tribute, not a dump of engine data.

Verified against:
  Quake      id-Software/Quake        QW/progs/world.qc
  Quake II   id-Software/Quake-2      game/g_spawn.c
  Half-Life  ValveSoftware/halflife   dlls/world.cpp
  Source     ValveSoftware/source-sdk-2013  mp/src/game/server/world.cpp

Quake III Arena ships no default lightstyle table in its game code, so it is
deliberately absent.
"""

# Every game whose engine ships Quake's lightstyle table verbatim.
QUAKE_LINEAGE = ["Quake II", "Half-Life", "Half-Life 2 / Source"]
# Unreal Engine 1 games share Unreal's LT_* light types.
UE1_LINEAGE = ["Unreal Tournament", "Deus Ex"]


BUILTIN_PATTERNS = [
    # ---- Quake: id Software's table, verbatim, and shared with every
    # engine that inherited it unchanged ----
    {"id": "steady", "game": "Quake", "shared_with": QUAKE_LINEAGE,
     "name": "Quake — 0 Steady", "hz": 10,
     "sequence": "m",
     "origin": "engine"},
    {"id": "flicker_a", "game": "Quake", "shared_with": QUAKE_LINEAGE,
     "name": "Quake — 1 Flicker", "hz": 10,
     "sequence": "mmnmmommommnonmmonqnmmo",
     "origin": "engine"},
    {"id": "slow_strong_pulse", "game": "Quake", "shared_with": QUAKE_LINEAGE,
     "name": "Quake — 2 Slow Strong Pulse", "hz": 10,
     "sequence": "abcdefghijklmnopqrstuvwxyzyxwvutsrqponmlkjihgfedcba",
     "origin": "engine"},
    {"id": "candle_a", "game": "Quake", "shared_with": QUAKE_LINEAGE,
     "name": "Quake — 3 Candle", "hz": 10,
     "sequence": "mmmmmaaaaammmmmaaaaaabcdefgabcdefg",
     "origin": "engine"},
    {"id": "fast_strobe", "game": "Quake", "shared_with": QUAKE_LINEAGE,
     "name": "Quake — 4 Fast Strobe", "hz": 10,
     "sequence": "mamamamamama",
     "origin": "engine"},
    {"id": "gentle_pulse", "game": "Quake", "shared_with": QUAKE_LINEAGE,
     "name": "Quake — 5 Gentle Pulse", "hz": 10,
     "sequence": "jklmnopqrstuvwxyzyxwvutsrqponmlkj",
     "origin": "engine"},
    {"id": "flicker_b", "game": "Quake", "shared_with": QUAKE_LINEAGE,
     "name": "Quake — 6 Flicker (alt)", "hz": 10,
     "sequence": "nmonqnmomnmomomno",
     "origin": "engine"},
    {"id": "candle_b", "game": "Quake", "shared_with": QUAKE_LINEAGE,
     "name": "Quake — 7 Candle (alt)", "hz": 10,
     "sequence": "mmmaaaabcdefgmmmmaaaammmaamm",
     "origin": "engine"},
    {"id": "candle_c", "game": "Quake", "shared_with": QUAKE_LINEAGE,
     "name": "Quake — 8 Candle (long)", "hz": 10,
     "sequence": "mmmaaammmaaammmabcdefaaaammmmabcdefmmmaaaa",
     "origin": "engine"},
    {"id": "hard_strobe", "game": "Quake", "shared_with": QUAKE_LINEAGE,
     "name": "Quake — 9 Slow Strobe", "hz": 10,
     "sequence": "aaaaaaaazzzzzzzz",
     "origin": "engine"},
    {"id": "fluorescent", "game": "Quake", "shared_with": QUAKE_LINEAGE,
     "name": "Quake — 10 Fluorescent Flicker", "hz": 10,
     "sequence": "mmamammmmammamamaaamammma",
     "origin": "engine"},
    {"id": "slow_pulse_nb", "game": "Quake", "shared_with": QUAKE_LINEAGE,
     "name": "Quake — 11 Slow Pulse (no black)", "hz": 10,
     "sequence": "abcdefghijklmnopqrrqponmlkjihgfedcba",
     "origin": "engine"},
    {"id": "quake_testing", "game": "Quake", "shared_with": QUAKE_LINEAGE,
     "name": "Quake — 63 Testing (held dark)", "hz": 10,
     "sequence": "a",
     "origin": "engine"},

    # ---- Half-Life: GoldSrc added style 12, which Source carries too ----
    {"id": "hl_underwater", "game": "Half-Life", "shared_with": ["Half-Life 2 / Source"],
     "name": "Half-Life — 12 Underwater Mutation", "hz": 10,
     "sequence": "mmnnmmnnnmmnn",
     "origin": "engine"},

    # ---- DOOM ----
    {"id": "doom_strobe_fast", "game": "DOOM",
     "name": "DOOM — Strobe (fast)", "hz": 10,
     "sequence": "zaaaa",
     "origin": "inspired"},
    {"id": "doom_strobe_slow", "game": "DOOM",
     "name": "DOOM — Strobe (slow)", "hz": 10,
     "sequence": "zaaaaaaaaaa",
     "origin": "inspired"},
    {"id": "doom_glow", "game": "DOOM",
     "name": "DOOM — Glow", "min_bri": 1, "max_bri": 254, "transition_ms": 100, "hz": 12,
     "sequence": "acegikmoqsuwywusqomkigeca",
     "origin": "inspired"},
    {"id": "doom_fire_flicker", "game": "DOOM",
     "name": "DOOM — Fire Flicker", "hz": 9,
     "sequence": "wxwyxwvxywxwvwyxwvxwyxvw",
     "origin": "inspired"},
    {"id": "doom_light_flash", "game": "DOOM",
     "name": "DOOM — Light Flash (random)", "hz": 10,
     "sequence": "zzaaaaaaaaaaaaazzaaaaaaazzzaaaaaaaaaaaaaa",
     "origin": "inspired"},

    # ---- Marathon ----
    {"id": "marathon_primary", "game": "Marathon",
     "name": "Marathon — Primary/Secondary Phase", "hz": 7,
     "sequence": "zzzzzzzaaaaaaa",
     "origin": "inspired"},
    {"id": "marathon_flicker", "game": "Marathon",
     "name": "Marathon — Flicker State", "hz": 12,
     "sequence": "yzxwyzvwyxzwyv",
     "origin": "inspired"},
    {"id": "marathon_pulse", "game": "Marathon",
     "name": "Marathon — Smooth Phase", "min_bri": 1, "max_bri": 254, "transition_ms": 200, "hz": 6,
     "sequence": "nprtvxzvtrpnlj",
     "origin": "inspired"},

    # ---- Heretic ----
    {"id": "heretic_wall_torch", "game": "Heretic",
     "name": "Heretic — Wall Torch", "min_bri": 40, "max_bri": 210, "transition_ms": 100, "hz": 10,
     "sequence": "stutsrstutsvutsrstutvuts",
     "origin": "inspired"},
    {"id": "heretic_brazier", "game": "Heretic",
     "name": "Heretic — Sputtering Brazier", "hz": 9,
     "sequence": "rqstrqpqrstusrqpqrstsrq",
     "origin": "inspired"},
    {"id": "heretic_enchanted", "game": "Heretic",
     "name": "Heretic — Enchanted Glow", "min_bri": 50, "max_bri": 230, "transition_ms": 200, "hz": 6,
     "sequence": "klmnopqrstuvwxyvutsrqponmlk",
     "origin": "inspired"},

    # ---- Descent ----
    {"id": "descent_marker", "game": "Descent",
     "name": "Descent — Blinking Mine Marker", "hz": 6,
     "sequence": "zzzzaaaazzzzaaaa",
     "origin": "inspired"},
    {"id": "descent_reactor", "game": "Descent",
     "name": "Descent — Reactor Warning", "hz": 10,
     "sequence": "zaazaazaazzzaaa",
     "origin": "inspired"},
    {"id": "descent_panel", "game": "Descent",
     "name": "Descent — Damaged Panel", "hz": 14,
     "sequence": "wvzwuzvwxzuwvzxw",
     "origin": "inspired"},

    # ---- Hexen ----
    {"id": "hexen_sconce", "game": "Hexen",
     "name": "Hexen — Guttering Sconce", "min_bri": 40, "max_bri": 200, "transition_ms": 100, "hz": 9,
     "sequence": "pqrsrqpnopqrqpoprqponmop",
     "origin": "inspired"},
    {"id": "hexen_mana_pulse", "game": "Hexen",
     "name": "Hexen — Slow Mana Pulse", "min_bri": 20, "max_bri": 230, "transition_ms": 200, "hz": 5,
     "sequence": "ikmoqsuwyzyxwusqomkigeca",
     "origin": "inspired"},
    {"id": "hexen_storm", "game": "Hexen",
     "name": "Hexen — Storm Flash", "hz": 12,
     "sequence": "aaaaaazzaaaaaaaaaazzzaaaaaaaa",
     "origin": "inspired"},

    # ---- Rise of the Triad ----
    {"id": "rott_torch", "game": "Rise of the Triad",
     "name": "Rise of the Triad — Flickering Torch", "min_bri": 45, "max_bri": 205, "transition_ms": 100, "hz": 10,
     "sequence": "tuvutsrtuvwvutstuv",
     "origin": "inspired"},
    {"id": "rott_ambush", "game": "Rise of the Triad",
     "name": "Rise of the Triad — Ambush Pulse", "min_bri": 1, "max_bri": 254, "transition_ms": 100, "hz": 7,
     "sequence": "hjlnprtvxvtrpnljh",
     "origin": "inspired"},
    {"id": "rott_trap", "game": "Rise of the Triad",
     "name": "Rise of the Triad — Strobe Trap", "hz": 11,
     "sequence": "zzzaaaaazzzaaaaaaaa",
     "origin": "inspired"},

    # ---- Duke Nukem 3D ----
    {"id": "duke_flicker", "game": "Duke Nukem 3D",
     "name": "Duke Nukem 3D — Flickering Sector", "hz": 12,
     "sequence": "ttusttrtusstturtsuttrsut",
     "origin": "inspired"},
    {"id": "duke_blink", "game": "Duke Nukem 3D",
     "name": "Duke Nukem 3D — Blinking Sign", "hz": 6,
     "sequence": "zzzaaazzzaaaaaa",
     "origin": "inspired"},
    {"id": "duke_pulse", "game": "Duke Nukem 3D",
     "name": "Duke Nukem 3D — Pulsating Sector", "min_bri": 20, "max_bri": 240, "transition_ms": 100, "hz": 8,
     "sequence": "hjlnprtvxzxvtrpnljh",
     "origin": "inspired"},
    {"id": "duke_broken_neon", "game": "Duke Nukem 3D",
     "name": "Duke Nukem 3D — Broken Neon", "hz": 14,
     "sequence": "zazzaaazzazaaaaaazzzazaaa",
     "origin": "inspired"},

    # ---- Blood ----
    {"id": "blood_torch", "game": "Blood",
     "name": "Blood — Guttering Torch", "min_bri": 40, "max_bri": 215, "transition_ms": 100, "hz": 10,
     "sequence": "rstusrtsuvtsrqstuvutsrst",
     "origin": "inspired"},
    {"id": "blood_candle", "game": "Blood",
     "name": "Blood — Gothic Candle", "min_bri": 40, "max_bri": 180, "transition_ms": 100, "hz": 8,
     "sequence": "nmlmnonmlkjklmnonmlmnmlk",
     "origin": "inspired"},
    {"id": "blood_lightning", "game": "Blood",
     "name": "Blood — Lightning Flash", "hz": 12,
     "sequence": "aaaaaaaaaazzaazaaaaaaaaaaaaaaaazzzaaaaaa",
     "origin": "inspired"},
    {"id": "blood_throb", "game": "Blood",
     "name": "Blood — Slow Throb", "min_bri": 30, "max_bri": 220, "transition_ms": 200, "hz": 5,
     "sequence": "fhjlnprtvxvtrpnljhf",
     "origin": "inspired"},
    {"id": "blood_dying_flame", "game": "Blood",
     "name": "Blood — Dying Flame", "min_bri": 1, "max_bri": 230, "transition_ms": 200, "hz": 6,
     "sequence": "utsrqponmlkjihgfedcba",
     "origin": "inspired"},

    # ---- Shadow Warrior ----
    {"id": "sw_neon", "game": "Shadow Warrior",
     "name": "Shadow Warrior — Neon Sign", "hz": 8,
     "sequence": "zzzzzzazzzzzzzazzzzaz",
     "origin": "inspired"},
    {"id": "sw_fluorescent", "game": "Shadow Warrior",
     "name": "Shadow Warrior — Failing Fluorescent", "hz": 14,
     "sequence": "zzazzzazazzzzzzazazzzzazzz",
     "origin": "inspired"},
    {"id": "sw_lantern", "game": "Shadow Warrior",
     "name": "Shadow Warrior — Paper Lantern", "min_bri": 60, "max_bri": 200, "transition_ms": 300, "hz": 4,
     "sequence": "qrstutsrqpopqrstutsrq",
     "origin": "inspired"},
    {"id": "sw_sputter", "game": "Shadow Warrior",
     "name": "Shadow Warrior — Sputtering Bulb", "hz": 15,
     "sequence": "wawwawwwawawwaawwwaw",
     "origin": "inspired"},

    # ---- Unreal ----
    {"id": "unreal_pulse", "game": "Unreal", "shared_with": UE1_LINEAGE,
     "name": "Unreal — LT_Pulse", "min_bri": 1, "max_bri": 254, "transition_ms": 100, "hz": 8,
     "sequence": "moqsuwyzywusqomkigecaacegik",
     "origin": "inspired"},
    {"id": "unreal_subtle_pulse", "game": "Unreal", "shared_with": UE1_LINEAGE,
     "name": "Unreal — LT_SubtlePulse", "min_bri": 120, "max_bri": 220, "transition_ms": 200, "hz": 6,
     "sequence": "pqrstuvwxyxwvutsrqp",
     "origin": "inspired"},
    {"id": "unreal_blink", "game": "Unreal", "shared_with": UE1_LINEAGE,
     "name": "Unreal — LT_Blink", "hz": 10,
     "sequence": "zzzzaazzaaaazzzzzzaazzzaa",
     "origin": "inspired"},
    {"id": "unreal_flicker", "game": "Unreal", "shared_with": UE1_LINEAGE,
     "name": "Unreal — LT_Flicker", "hz": 16,
     "sequence": "vzsxtywuzvxsytwvzuxs",
     "origin": "inspired"},
    {"id": "unreal_strobe", "game": "Unreal", "shared_with": UE1_LINEAGE,
     "name": "Unreal — LT_Strobe", "hz": 12,
     "sequence": "zzaazzaa",
     "origin": "inspired"},

    # ---- Thief ----
    {"id": "thief_torch", "game": "Thief",
     "name": "Thief — Wall Torch", "min_bri": 45, "max_bri": 205, "transition_ms": 100, "hz": 9,
     "sequence": "rstusrtusrqstursqtus",
     "origin": "inspired"},
    {"id": "thief_gaslight", "game": "Thief",
     "name": "Thief — Gaslight", "min_bri": 70, "max_bri": 190, "transition_ms": 100, "hz": 7,
     "sequence": "opqrqponoqrsrqpoqrs",
     "origin": "inspired"},
    {"id": "thief_electric", "game": "Thief",
     "name": "Thief — Failing Electric Light", "hz": 13,
     "sequence": "zazzzaazzzzazaazzzza",
     "origin": "inspired"},

    # ---- System Shock 2 ----
    {"id": "ss2_deck", "game": "System Shock 2",
     "name": "System Shock 2 — Failing Deck Light", "hz": 11,
     "sequence": "yzyxzyxwzyxyzwyxz",
     "origin": "inspired"},
    {"id": "ss2_emergency", "game": "System Shock 2",
     "name": "System Shock 2 — Emergency Strobe", "hz": 9,
     "sequence": "zzzaaaaaaazzz",
     "origin": "inspired"},
    {"id": "ss2_medsci", "game": "System Shock 2",
     "name": "System Shock 2 — Med-Sci Flicker", "hz": 15,
     "sequence": "wyxzwvyxwzyvxw",
     "origin": "inspired"},

    # ---- Doom 3 ----
    {"id": "doom3_ceiling", "game": "Doom 3",
     "name": "Doom 3 — Failing Ceiling Light", "hz": 14,
     "sequence": "zzzzaazzzaaaaaazzaazzzzzzaaazz",
     "origin": "inspired"},
    {"id": "doom3_strobe", "game": "Doom 3",
     "name": "Doom 3 — Corridor Strobe", "hz": 12,
     "sequence": "zzaaaaaazzaaaaaa",
     "origin": "inspired"},
    {"id": "doom3_panel", "game": "Doom 3",
     "name": "Doom 3 — Dying Panel", "hz": 8,
     "sequence": "zyxwvutsrqponmlkjihgfedcba",
     "origin": "inspired"},

    # ---- Quake 4 ----
    {"id": "quake4_strogg", "game": "Quake 4",
     "name": "Quake 4 — Strogg Machinery Pulse", "hz": 8,
     "sequence": "nprtvxzxvtrpnlkjklmn",
     "origin": "inspired"},
    {"id": "quake4_conduit", "game": "Quake 4",
     "name": "Quake 4 — Damaged Conduit", "hz": 12,
     "sequence": "vxzxvutvxzyxvuvxzxv",
     "origin": "inspired"},
    {"id": "quake4_medlab", "game": "Quake 4",
     "name": "Quake 4 — Med-Lab Throb", "min_bri": 60, "max_bri": 200, "transition_ms": 200, "hz": 6,
     "sequence": "lnprtvxwvtrpnlkjlnp",
     "origin": "inspired"},
]

BUILTIN_BY_ID = {p["id"]: p for p in BUILTIN_PATTERNS}

def _menu_games() -> list:
    """Every game with something to offer, sorted by name.

    Derived from the table rather than kept as a second list beside it, so a
    game added below turns up in the menu without anything else to remember.
    casefold keeps DOOM next to Doom 3 rather than sorting all-caps names first.
    """
    games = set()
    for pattern in BUILTIN_PATTERNS:
        games.add(pattern["game"])
        games.update(pattern.get("shared_with", ()))
    return sorted(games, key=str.casefold)


GAMES = _menu_games()


# A pattern is more than its letters: the speed it runs at, how far the bulb
# swings, and how hard the steps are all belong to the effect. Anything a
# pattern doesn't state falls back to these.
DEFAULT_HZ = 10.0
DEFAULT_MIN_BRI = 1
DEFAULT_MAX_BRI = 254
DEFAULT_TRANSITION_MS = 0
# The bridge takes transitions in 100ms units, so that is the real resolution;
# anything finer is truncated on the way through and only misleads the UI.
TRANSITION_STEP_MS = 100

FRAMING_FIELDS = ("hz", "min_bri", "max_bri", "transition_ms")
_FRAMING_DEFAULTS = {
    "hz": DEFAULT_HZ,
    "min_bri": DEFAULT_MIN_BRI,
    "max_bri": DEFAULT_MAX_BRI,
    "transition_ms": DEFAULT_TRANSITION_MS,
}

# Filled in here rather than repeated on every entry above, so the table stays
# about the patterns and every consumer can read the fields unconditionally.
for _pattern in BUILTIN_PATTERNS:
    for _field, _default in _FRAMING_DEFAULTS.items():
        _pattern.setdefault(_field, _default)


def framing_of(pattern: dict) -> dict:
    """The speed and brightness framing a pattern was written for."""
    return {f: pattern.get(f, _FRAMING_DEFAULTS[f]) for f in FRAMING_FIELDS}


def patterns_for(game: str) -> list:
    """Every pattern that game's picker should offer.

    Includes patterns another game owns but shares with it: Quake II, Half-Life
    and Source all run Quake's table byte for byte, and the Unreal Engine 1
    games run Unreal's light types, so those are listed under each without the
    strings being duplicated in the table.
    """
    return [p for p in BUILTIN_PATTERNS
            if p["game"] == game or game in p.get("shared_with", ())]


def level_for_char(c: str) -> float:
    """Map a lightstyle character a-z to a 0.0-1.0 brightness level."""
    c = c.lower()
    if not ("a" <= c <= "z"):
        c = "m"
    val = (ord(c) - ord("a")) / 25.0
    return max(0.0, min(1.0, val))
