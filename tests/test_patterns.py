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


def test_quake_ships_its_whole_table():
    quake = [p for p in BUILTIN_PATTERNS if p["game"] == "Quake"]
    assert len(quake) == 13          # styles 0-11 plus 63
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


def test_every_game_owns_or_inherits_patterns():
    from app.patterns import patterns_for

    for game in GAMES:
        assert patterns_for(game), game


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
    assert len(hl) == 14          # Quake's 0-11 and 63, plus style 12
    owned = [p for p in hl if p["game"] == "Half-Life"]
    assert [p["id"] for p in owned] == ["hl_underwater"]
    assert all(p["game"] == "Quake" for p in hl if p["id"] != "hl_underwater")


def test_quakes_menu_does_not_pick_up_half_lifes_addition():
    from app.patterns import patterns_for

    assert "hl_underwater" not in [p["id"] for p in patterns_for("Quake")]
    assert len(patterns_for("Quake")) == 13


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


def test_every_pattern_declares_the_rate_it_was_written_for():
    from app.patterns import DEFAULT_HZ

    assert DEFAULT_HZ == 10.0
    for pattern in BUILTIN_PATTERNS:
        assert "hz" in pattern, pattern["id"]
        assert 0.5 <= pattern["hz"] <= 20, pattern["id"]


def test_engine_lightstyles_run_at_quakes_ten_frames_per_second():
    # Quake steps its lightstyle table at 10fps and GoldSrc inherited that, so
    # the verbatim styles are not free to be re-timed.
    for pattern in BUILTIN_PATTERNS:
        if pattern["origin"] == "engine":
            assert pattern["hz"] == 10, pattern["id"]


def test_authored_patterns_actually_use_the_freedom():
    # If every authored pattern were also 10Hz the field would be decoration.
    authored = {p["hz"] for p in BUILTIN_PATTERNS if p["origin"] == "inspired"}
    assert len(authored) > 3


def test_no_pattern_takes_absurdly_long_to_cycle():
    for pattern in BUILTIN_PATTERNS:
        seconds = len(pattern["sequence"]) / pattern["hz"]
        assert seconds <= 8, (pattern["id"], seconds)


# The table below is transcribed from the released game sources, not from
# memory. Style 8 shipped wrong here for a while precisely because nothing
# checked; this is the check.
#
#   Quake      id-Software/Quake             QW/progs/world.qc
#   Quake II   id-Software/Quake-2           game/g_spawn.c
#   Half-Life  ValveSoftware/halflife        dlls/world.cpp
#   Source     ValveSoftware/source-sdk-2013 mp/src/game/server/world.cpp
#
# All four define styles 0-11 and 63 identically; GoldSrc and Source add 12.
ID_SOFTWARE_LIGHTSTYLES = {
    0: "m",
    1: "mmnmmommommnonmmonqnmmo",
    2: "abcdefghijklmnopqrstuvwxyzyxwvutsrqponmlkjihgfedcba",
    3: "mmmmmaaaaammmmmaaaaaabcdefgabcdefg",
    4: "mamamamamama",
    5: "jklmnopqrstuvwxyzyxwvutsrqponmlkj",
    6: "nmonqnmomnmomomno",
    7: "mmmaaaabcdefgmmmmaaaammmaamm",
    8: "mmmaaammmaaammmabcdefaaaammmmabcdefmmmaaaa",
    9: "aaaaaaaazzzzzzzz",
    10: "mmamammmmammamamaaamammma",
    11: "abcdefghijklmnopqrrqponmlkjihgfedcba",
    63: "a",
}
GOLDSRC_ADDITION = {12: "mmnnmmnnnmmnn"}

QUAKE_STYLE_IDS = {
    0: "steady", 1: "flicker_a", 2: "slow_strong_pulse", 3: "candle_a",
    4: "fast_strobe", 5: "gentle_pulse", 6: "flicker_b", 7: "candle_b",
    8: "candle_c", 9: "hard_strobe", 10: "fluorescent", 11: "slow_pulse_nb",
    63: "quake_testing",
}


def test_quake_lightstyles_match_the_released_source_exactly():
    for style, sequence in ID_SOFTWARE_LIGHTSTYLES.items():
        pattern = BUILTIN_BY_ID[QUAKE_STYLE_IDS[style]]
        assert pattern["sequence"] == sequence, f"style {style}"
        assert pattern["origin"] == "engine"
        assert pattern["name"].startswith(f"Quake — {style} ")


def test_goldsrc_addition_matches_the_released_source_exactly():
    assert BUILTIN_BY_ID["hl_underwater"]["sequence"] == GOLDSRC_ADDITION[12]
    assert BUILTIN_BY_ID["hl_underwater"]["origin"] == "engine"


def test_the_quake_lineage_shares_one_table_rather_than_copying_it():
    from app.patterns import patterns_for

    lineage = ["Quake", "Quake II", "Half-Life", "Half-Life 2 / Source"]
    tables = {g: {p["sequence"] for p in patterns_for(g)} for g in lineage}
    quake = tables["Quake"]
    # Quake II runs the same table; GoldSrc and Source add exactly style 12.
    assert tables["Quake II"] == quake
    assert tables["Half-Life"] == quake | {GOLDSRC_ADDITION[12]}
    assert tables["Half-Life 2 / Source"] == tables["Half-Life"]
    # and none of the inheritors own a pattern of their own beyond that
    for game in ["Quake II", "Half-Life 2 / Source"]:
        assert not [p for p in BUILTIN_PATTERNS if p["game"] == game], game


def test_unreal_engine_one_games_share_unreals_light_types():
    from app.patterns import patterns_for

    unreal = {p["id"] for p in patterns_for("Unreal")}
    assert {p["id"] for p in patterns_for("Unreal Tournament")} == unreal
    assert {p["id"] for p in patterns_for("Deus Ex")} == unreal


def test_only_verified_tables_claim_engine_origin():
    # Everything else is authored here, however faithful it aims to be.
    owners = {p["game"] for p in BUILTIN_PATTERNS if p["origin"] == "engine"}
    assert owners == {"Quake", "Half-Life"}


def test_quake_three_is_deliberately_absent():
    # id Tech 3 ships no default lightstyle table in its game code, so there
    # is nothing verbatim to claim and nothing distinctive to author.
    assert "Quake III" not in GAMES
    assert not any("Quake III" in p["game"] for p in BUILTIN_PATTERNS)
