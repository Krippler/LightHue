from app.patterns import BUILTIN_BY_ID, BUILTIN_PATTERNS, GAMES, level_for_char


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
    for pattern in BUILTIN_PATTERNS:
        assert pattern["sequence"], pattern["id"]
        assert all("a" <= c <= "z" for c in pattern["sequence"]), pattern["id"]
        assert pattern["origin"] in ("engine", "inspired"), pattern["id"]
    assert len(BUILTIN_BY_ID) == len(BUILTIN_PATTERNS)   # ids are unique


def test_quake_still_ships_its_full_twelve_styles():
    quake = [p for p in BUILTIN_PATTERNS if p["game"] == "Quake"]
    assert len(quake) == 12
    assert all(p["origin"] == "engine" for p in quake)


def test_every_pattern_is_named_after_its_game():
    for pattern in BUILTIN_PATTERNS:
        assert pattern["game"] in GAMES, pattern["id"]
        assert pattern["name"].startswith(pattern["game"]), pattern["name"]


def test_engine_sourced_patterns_are_only_the_ones_with_real_tables():
    # Only Quake and GoldSrc shipped literal a-z lightstyle strings; the rest
    # are hand-authored here and must not claim otherwise.
    verbatim = {p["game"] for p in BUILTIN_PATTERNS if p["origin"] == "engine"}
    assert verbatim == {"Quake", "Half-Life"}


def test_every_game_contributes_patterns():
    for game in GAMES:
        assert any(p["game"] == game for p in BUILTIN_PATTERNS), game


def test_level_for_char_spans_zero_to_one():
    assert level_for_char("a") == 0.0
    assert level_for_char("z") == 1.0
    assert 0.4 < level_for_char("m") < 0.6


def test_level_for_char_falls_back_for_junk():
    # Out-of-range input maps to the mid-level rather than raising.
    assert level_for_char("!") == level_for_char("m")
    assert level_for_char("M") == level_for_char("m")


def test_half_life_offers_quakes_table_without_duplicating_it():
    # GoldSrc inherited styles 0-11 verbatim. They belong to Quake in the
    # table and are shared into Half-Life's menu, so the strings exist once.
    from app.patterns import patterns_for

    hl = patterns_for("Half-Life")
    assert len(hl) == 13
    owned = [p for p in hl if p["game"] == "Half-Life"]
    assert [p["id"] for p in owned] == ["hl_underwater"]
    assert all(p["game"] == "Quake" for p in hl if p["id"] != "hl_underwater")


def test_quakes_menu_does_not_pick_up_half_lifes_addition():
    from app.patterns import patterns_for

    assert "hl_underwater" not in [p["id"] for p in patterns_for("Quake")]
    assert len(patterns_for("Quake")) == 12


def test_shared_patterns_are_the_same_objects_not_copies():
    from app.patterns import BUILTIN_BY_ID, patterns_for

    for pattern in patterns_for("Half-Life"):
        assert pattern is BUILTIN_BY_ID[pattern["id"]]


def test_build_engine_games_are_all_represented():
    build = {"Duke Nukem 3D", "Blood", "Shadow Warrior"}
    assert build <= {p["game"] for p in BUILTIN_PATTERNS}
    for game in build:
        # None of the Build games shipped a lightstyle string table.
        assert all(p["origin"] == "inspired"
                   for p in BUILTIN_PATTERNS if p["game"] == game), game


def test_every_game_in_the_menu_order_has_options():
    from app.patterns import patterns_for

    for game in GAMES:
        assert patterns_for(game), game
