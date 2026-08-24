"""Guards on the front-end source that the Python suite can still enforce.

There is no JS test runner here, and standing one up for a handful of
invariants would cost more than it returns. These read the shipped files and
assert the properties whose absence has actually broken something.
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"
APP_JS = (STATIC / "app.js").read_text()
INDEX = (STATIC / "index.html").read_text()


def colour_swatch_ids() -> set[str]:
    """Every <input type="color"> in the page."""
    return {
        m.group(1)
        for tag in re.findall(r"<input\b[^>]*>", INDEX)
        if 'type="color"' in tag
        for m in [re.search(r'id="([^"]+)"', tag)]
        if m
    }


def test_every_colour_swatch_answers_both_events():
    """A colour picker has to listen to `input` and `change`.

    Which one an <input type="color"> fires is the browser's choice: some send
    `input` while the picker moves and `change` when it closes, others send
    only `change`. Binding `input` alone meant that on those browsers picking a
    colour moved the swatch and did nothing else — including never ticking "Set
    colour", which is what decides whether the colour is sent at all. The
    colour was then dropped on save with no error, which reads as the picker
    being broken rather than as an event that never arrived.
    """
    swatches = colour_swatch_ids()
    # The card template's swatch has a class rather than an id, so it is not in
    # that set; count the bindings instead, which covers all three.
    assert len(swatches) >= 2, f"expected the custom and stream swatches, got {swatches}"
    assert APP_JS.count("onColourPicked(") >= 4, (
        "every colour swatch should be bound through onColourPicked — one "
        "definition plus one call per swatch"
    )

    # And none of them may go back to a bare `input` subscription.
    for variable in ("colorInput", "customColor", "streamColor"):
        assert f"{variable}.addEventListener('input'" not in APP_JS, (
            f"{variable} is bound to `input` alone again; on a browser that "
            "only fires `change` the colour is silently dropped"
        )


def test_the_helper_binds_both_events():
    body = re.search(r"function onColourPicked\([^)]*\)\s*\{(.*?)\n\}", APP_JS, re.S)
    assert body, "onColourPicked is missing"
    assert "'input'" in body.group(1) and "'change'" in body.group(1)
