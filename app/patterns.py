"""Flicker patterns from (and inspired by) classic game engines.

Each character is one frame of brightness, 'a' darkest through 'z' brightest,
sampled at whatever rate the light is running at. That encoding is Quake's,
and it turns out to describe most engines' light effects perfectly well.

Two kinds of pattern live here, and the difference is worth keeping honest:

  origin="engine"
      Copied verbatim from the game's own lightstyle table. Quake shipped
      styles 0-11 as literal a-z strings in the engine, and GoldSrc (Half-Life)
      inherited that table and added one of its own.

  origin="inspired"
      Hand-authored here. DOOM, Build (Duke Nukem 3D) and Unreal don't store
      light effects as strings at all — they run procedural sector/actor
      effects in code. These sequences approximate the documented behaviour of
      those effects at roughly their original timing; they are a tribute, not
      a dump of engine data.
"""

BUILTIN_PATTERNS = [
    # ---- Quake: id Software's lightstyle table, styles 0-11, verbatim ----
    {"id": "steady", "game": "Quake", "shared_with": ["Half-Life"], "name": "Quake — 0 Steady",
     "sequence": "m", "origin": "engine"},
    {"id": "flicker_a", "game": "Quake", "shared_with": ["Half-Life"], "name": "Quake — 1 Flicker",
     "sequence": "mmnmmommommnonmmonqnmmo", "origin": "engine"},
    {"id": "slow_strong_pulse", "game": "Quake", "shared_with": ["Half-Life"], "name": "Quake — 2 Slow Strong Pulse",
     "sequence": "abcdefghijklmnopqrstuvwxyzyxwvutsrqponmlkjihgfedcba", "origin": "engine"},
    {"id": "candle_a", "game": "Quake", "shared_with": ["Half-Life"], "name": "Quake — 3 Candle",
     "sequence": "mmmmmaaaaammmmmaaaaaabcdefgabcdefg", "origin": "engine"},
    {"id": "fast_strobe", "game": "Quake", "shared_with": ["Half-Life"], "name": "Quake — 4 Fast Strobe",
     "sequence": "mamamamamama", "origin": "engine"},
    {"id": "gentle_pulse", "game": "Quake", "shared_with": ["Half-Life"], "name": "Quake — 5 Gentle Pulse",
     "sequence": "jklmnopqrstuvwxyzyxwvutsrqponmlkj", "origin": "engine"},
    {"id": "flicker_b", "game": "Quake", "shared_with": ["Half-Life"], "name": "Quake — 6 Flicker (alt)",
     "sequence": "nmonqnmomnmomomno", "origin": "engine"},
    {"id": "candle_b", "game": "Quake", "shared_with": ["Half-Life"], "name": "Quake — 7 Candle (alt)",
     "sequence": "mmmaaaabcdefgmmmmaaaammmaamm", "origin": "engine"},
    {"id": "candle_c", "game": "Quake", "shared_with": ["Half-Life"], "name": "Quake — 8 Candle (long)",
     "sequence": "mmmaaammmaaammmabcdefaaaaammmmmabcdefmmmmaaaammmaamm", "origin": "engine"},
    {"id": "hard_strobe", "game": "Quake", "shared_with": ["Half-Life"], "name": "Quake — 9 Slow Strobe",
     "sequence": "aaaaaaaazzzzzzzz", "origin": "engine"},
    {"id": "fluorescent", "game": "Quake", "shared_with": ["Half-Life"], "name": "Quake — 10 Fluorescent Flicker",
     "sequence": "mmamammmmammamamaaamammma", "origin": "engine"},
    {"id": "slow_pulse_nb", "game": "Quake", "shared_with": ["Half-Life"], "name": "Quake — 11 Slow Pulse (no black)",
     "sequence": "abcdefghijklmnopqrrqponmlkjihgfedcba", "origin": "engine"},

    # ---- Half-Life: GoldSrc inherited Quake's styles 0-11 verbatim (marked
    # shared_with above, so they appear under Half-Life too rather than being
    # copied) and added style 12 of its own ----
    {"id": "hl_underwater", "game": "Half-Life", "name": "Half-Life — 12 Underwater Mutation",
     "sequence": "mmnnmmnnnmmnn", "origin": "engine"},

    # ---- DOOM: sector light effects from p_lights.c, timed at 35 tics/sec ----
    {"id": "doom_strobe_fast", "game": "DOOM", "name": "DOOM — Strobe (fast)",
     "sequence": "zaaaa", "origin": "inspired"},
    {"id": "doom_strobe_slow", "game": "DOOM", "name": "DOOM — Strobe (slow)",
     "sequence": "zaaaaaaaaaa", "origin": "inspired"},
    {"id": "doom_glow", "game": "DOOM", "name": "DOOM — Glow",
     "sequence": "acegikmoqsuwywusqomkigeca", "origin": "inspired"},
    {"id": "doom_fire_flicker", "game": "DOOM", "name": "DOOM — Fire Flicker",
     "sequence": "wxwyxwvxywxwvwyxwvxwyxvw", "origin": "inspired"},
    {"id": "doom_light_flash", "game": "DOOM", "name": "DOOM — Light Flash (random)",
     "sequence": "zzaaaaaaaaaaaaazzaaaaaaazzzaaaaaaaaaaaaaa", "origin": "inspired"},

    # ---- Duke Nukem 3D: Build engine sector lighting effects ----
    {"id": "duke_flicker", "game": "Duke Nukem 3D", "name": "Duke Nukem 3D — Flickering Sector",
     "sequence": "ttusttrtusstturtsuttrsut", "origin": "inspired"},
    {"id": "duke_blink", "game": "Duke Nukem 3D", "name": "Duke Nukem 3D — Blinking Sign",
     "sequence": "zzzaaazzzaaaaaa", "origin": "inspired"},
    {"id": "duke_pulse", "game": "Duke Nukem 3D", "name": "Duke Nukem 3D — Pulsating Sector",
     "sequence": "hjlnprtvxzxvtrpnljh", "origin": "inspired"},
    {"id": "duke_broken_neon", "game": "Duke Nukem 3D", "name": "Duke Nukem 3D — Broken Neon",
     "sequence": "zazzaaazzazaaaaaazzzazaaa", "origin": "inspired"},

    # ---- Blood: Build engine again, by way of Monolith's fork ----
    {"id": "blood_torch", "game": "Blood", "name": "Blood — Guttering Torch",
     "sequence": "rstusrtsuvtsrqstuvutsrst", "origin": "inspired"},
    {"id": "blood_candle", "game": "Blood", "name": "Blood — Gothic Candle",
     "sequence": "nmlmnonmlkjklmnonmlmnmlk", "origin": "inspired"},
    {"id": "blood_lightning", "game": "Blood", "name": "Blood — Lightning Flash",
     "sequence": "aaaaaaaaaazzaazaaaaaaaaaaaaaaaazzzaaaaaa", "origin": "inspired"},
    {"id": "blood_throb", "game": "Blood", "name": "Blood — Slow Throb",
     "sequence": "fhjlnprtvxvtrpnljhf", "origin": "inspired"},
    {"id": "blood_dying_flame", "game": "Blood", "name": "Blood — Dying Flame",
     "sequence": "utsrqponmlkjihgfedcba", "origin": "inspired"},

    # ---- Shadow Warrior: Build engine, neon and paper lanterns ----
    {"id": "sw_neon", "game": "Shadow Warrior", "name": "Shadow Warrior — Neon Sign",
     "sequence": "zzzzzzazzzzzzzazzzzaz", "origin": "inspired"},
    {"id": "sw_fluorescent", "game": "Shadow Warrior", "name": "Shadow Warrior — Failing Fluorescent",
     "sequence": "zzazzzazazzzzzzazazzzzazzz", "origin": "inspired"},
    {"id": "sw_lantern", "game": "Shadow Warrior", "name": "Shadow Warrior — Paper Lantern",
     "sequence": "qrstutsrqpopqrstutsrq", "origin": "inspired"},
    {"id": "sw_sputter", "game": "Shadow Warrior", "name": "Shadow Warrior — Sputtering Bulb",
     "sequence": "wawwawwwawawwaawwwaw", "origin": "inspired"},

    # ---- Unreal: LT_* light types from the actor's LightEffect ----
    {"id": "unreal_pulse", "game": "Unreal", "name": "Unreal — LT_Pulse",
     "sequence": "moqsuwyzywusqomkigecaacegik", "origin": "inspired"},
    {"id": "unreal_subtle_pulse", "game": "Unreal", "name": "Unreal — LT_SubtlePulse",
     "sequence": "pqrstuvwxyxwvutsrqp", "origin": "inspired"},
    {"id": "unreal_blink", "game": "Unreal", "name": "Unreal — LT_Blink",
     "sequence": "zzzzaazzaaaazzzzzzaazzzaa", "origin": "inspired"},
    {"id": "unreal_flicker", "game": "Unreal", "name": "Unreal — LT_Flicker",
     "sequence": "vzsxtywuzvxsytwvzuxs", "origin": "inspired"},
    {"id": "unreal_strobe", "game": "Unreal", "name": "Unreal — LT_Strobe",
     "sequence": "zzaazzaa", "origin": "inspired"},
]

BUILTIN_BY_ID = {p["id"]: p for p in BUILTIN_PATTERNS}

# Menu order for the pattern picker, roughly by release date.
GAMES = ["DOOM", "Duke Nukem 3D", "Quake", "Blood", "Shadow Warrior",
         "Half-Life", "Unreal"]


def patterns_for(game: str) -> list:
    """Every pattern that game's picker should offer.

    Includes patterns another game owns but shares with it — Half-Life runs
    Quake's styles 0-11, so they are listed under both without the strings
    being duplicated in the table.
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
