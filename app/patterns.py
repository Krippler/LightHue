"""
Quake lightstyle sequences. Each character represents a brightness level
from 'a' (darkest) to 'z' (brightest), sampled at the given rate (10Hz in
the original engine). This is the classic default lightstyle table used by
id Software's Quake engine (styles 0-11) plus a couple of hand-tuned extras.
"""

BUILTIN_PATTERNS = [
    {"id": "steady", "name": "0 - Steady", "sequence": "m"},
    {"id": "flicker_a", "name": "1 - Flicker (classic)", "sequence": "mmnmmommommnonmmonqnmmo"},
    {"id": "slow_strong_pulse", "name": "2 - Slow Strong Pulse", "sequence": "abcdefghijklmnopqrrqponmlkjihgfedcba"},
    {"id": "candle_a", "name": "3 - Candle (soft)", "sequence": "mmmmmaaaaammmmmaaaaaabcdefgabcdefg"},
    {"id": "fast_strobe", "name": "4 - Fast Strobe", "sequence": "mamamamamama"},
    {"id": "gentle_pulse", "name": "5 - Gentle Pulse", "sequence": "jklmnopqrstuvwxyzyxwvutsrqponmlkj"},
    {"id": "flicker_b", "name": "6 - Flicker (alt)", "sequence": "nmonqnmomnmomomno"},
    {"id": "candle_b", "name": "7 - Candle (flicker)", "sequence": "mmmaaaabcdefgmmmmaaaammmaamm"},
    {"id": "candle_c", "name": "8 - Candle (long)", "sequence": "mmmaaammmaaammmabcdefaaaaammmmmabcdefmmmmaaaammmaamm"},
    {"id": "hard_strobe", "name": "9 - Hard Strobe (on/off)", "sequence": "aaaaaaaazzzzzzzz"},
    {"id": "fluorescent", "name": "10 - Fluorescent Flicker", "sequence": "mmamammmmammamamaaamammma"},
    {"id": "slow_pulse_nb", "name": "11 - Slow Pulse (no fade to black)", "sequence": "abcdefghijklmnopqrrqponmlkjihgfedcba"},
]

BUILTIN_BY_ID = {p["id"]: p for p in BUILTIN_PATTERNS}


def level_for_char(c: str) -> float:
    """Map a lightstyle character a-z to a 0.0-1.0 brightness level."""
    c = c.lower()
    if not ("a" <= c <= "z"):
        c = "m"
    val = (ord(c) - ord("a")) / 25.0
    return max(0.0, min(1.0, val))
