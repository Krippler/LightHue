import pytest

from app import packs


def test_round_trip_through_build_and_parse():
    original = [{"name": "Torchlight", "sequence": "mmnmmo"},
                {"name": "Sputter", "sequence": "azaz"}]
    pack = packs.build(original, name="My pack", author="someone")
    assert pack["format"] == packs.FORMAT
    assert pack["name"] == "My pack" and pack["author"] == "someone"
    assert packs.parse(pack) == original


def test_build_omits_optional_fields_when_absent():
    pack = packs.build([{"name": "x", "sequence": "az"}])
    assert "name" not in pack and "author" not in pack
    assert pack["exported_at"].endswith("+00:00")


def test_parse_normalises_names_and_sequences():
    got = packs.parse({"patterns": [{"name": "  Torch   light ", "sequence": " MM Az\n"}]})
    assert got == [{"name": "Torch light", "sequence": "mmaz"}]


def test_parse_accepts_a_hand_written_pack_without_boilerplate():
    # Someone should be able to write one in a text editor.
    assert packs.parse({"patterns": [{"name": "Hand made", "sequence": "abc"}]}) == [
        {"name": "Hand made", "sequence": "abc"}
    ]


def test_parse_ignores_unknown_keys():
    got = packs.parse({
        "format": packs.FORMAT, "version": 1, "notes": "hello",
        "patterns": [{"name": "x", "sequence": "az", "colour": "red"}],
    })
    assert got == [{"name": "x", "sequence": "az"}]


@pytest.mark.parametrize("payload, message", [
    ([], "JSON object"),
    ("nope", "JSON object"),
    ({}, "no 'patterns' list"),
    ({"patterns": {}}, "no 'patterns' list"),
    ({"patterns": []}, "no patterns"),
    ({"format": "something/else", "patterns": [{"name": "x", "sequence": "a"}]}, "unknown pack format"),
    ({"version": 99, "patterns": [{"name": "x", "sequence": "a"}]}, "only understands up to"),
    ({"version": "1", "patterns": [{"name": "x", "sequence": "a"}]}, "whole number"),
    ({"patterns": ["nope"]}, "not an object"),
    ({"patterns": [{"name": "x", "sequence": "ab1"}]}, "only contain letters a-z"),
    ({"patterns": [{"name": "x", "sequence": ""}]}, "sequence is empty"),
    ({"patterns": [{"name": "x", "sequence": 5}]}, "must be text"),
    ({"patterns": [{"name": "", "sequence": "ab"}]}, "name is empty"),
    ({"patterns": [{"sequence": "ab"}]}, "name must be text"),
])
def test_parse_rejects_bad_packs(payload, message):
    with pytest.raises(packs.PackError) as e:
        packs.parse(payload)
    assert message in str(e.value)


def test_bad_entries_are_reported_by_name():
    with pytest.raises(packs.PackError) as e:
        packs.parse({"patterns": [{"name": "Good", "sequence": "az"},
                                  {"name": "Broken One", "sequence": "zz9"}]})
    assert "Broken One" in str(e.value)


def test_unnamed_bad_entries_are_reported_by_position():
    with pytest.raises(packs.PackError) as e:
        packs.parse({"patterns": [{"name": "Good", "sequence": "az"},
                                  {"name": "   ", "sequence": "az"}]})
    assert "pattern 2" in str(e.value)


def test_caps_are_enforced():
    with pytest.raises(packs.PackError, match="more than"):
        packs.parse({"patterns": [{"name": f"p{i}", "sequence": "az"}
                                  for i in range(packs.MAX_PATTERNS + 1)]})
    with pytest.raises(packs.PackError, match="longer than"):
        packs.parse({"patterns": [{"name": "x", "sequence": "a" * (packs.MAX_SEQUENCE + 1)}]})


def test_long_names_are_trimmed_not_rejected():
    got = packs.parse({"patterns": [{"name": "x" * 200, "sequence": "az"}]})
    assert len(got[0]["name"]) == packs.MAX_NAME


def test_unique_name_suffixes_until_free():
    assert packs.unique_name("Torch", set()) == "Torch"
    assert packs.unique_name("Torch", {"Torch"}) == "Torch (2)"
    assert packs.unique_name("Torch", {"Torch", "Torch (2)", "Torch (3)"}) == "Torch (4)"
