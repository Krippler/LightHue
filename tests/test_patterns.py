from app.patterns import BUILTIN_BY_ID, BUILTIN_PATTERNS, level_for_char


def test_every_builtin_sequence_is_distinct():
    # Style 2 once carried style 11's sequence, making two menu entries
    # produce byte-identical flicker.
    by_sequence = {}
    for pattern in BUILTIN_PATTERNS:
        by_sequence.setdefault(pattern["sequence"], []).append(pattern["name"])
    duplicates = {seq: names for seq, names in by_sequence.items() if len(names) > 1}
    assert duplicates == {}


def test_style_2_sweeps_the_full_range_and_11_does_not():
    assert BUILTIN_BY_ID["slow_strong_pulse"]["sequence"].startswith("abcdefghijklmnopqrstuvwxyz")
    assert "z" not in BUILTIN_BY_ID["slow_pulse_nb"]["sequence"]


def test_builtins_are_well_formed():
    assert len(BUILTIN_PATTERNS) == 12
    for pattern in BUILTIN_PATTERNS:
        assert pattern["sequence"], pattern["id"]
        assert all("a" <= c <= "z" for c in pattern["sequence"]), pattern["id"]
    assert len(BUILTIN_BY_ID) == len(BUILTIN_PATTERNS)


def test_level_for_char_spans_zero_to_one():
    assert level_for_char("a") == 0.0
    assert level_for_char("z") == 1.0
    assert 0.4 < level_for_char("m") < 0.6


def test_level_for_char_falls_back_for_junk():
    # Out-of-range input maps to the mid-level rather than raising.
    assert level_for_char("!") == level_for_char("m")
    assert level_for_char("M") == level_for_char("m")
