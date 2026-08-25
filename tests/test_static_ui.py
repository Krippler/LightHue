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


def info_markers() -> list[str]:
    """Every inline-help marker in the page, as its whole tag."""
    return [tag for tag in re.findall(r"<span\b[^>]*>", INDEX) if 'class="info"' in tag]


def test_every_hover_tip_is_also_readable_without_hovering():
    """A tip must carry the same words in data-tip and aria-label.

    data-tip is what the CSS paints on hover; aria-label is what a screen
    reader announces and what a keyboard user gets on focus. Setting one
    without the other makes the help hover-only, which is exactly what the
    paragraphs it replaced were not.
    """
    markers = info_markers()
    assert len(markers) >= 5, f"expected the inline-help markers, found {len(markers)}"
    for tag in markers:
        tip = re.search(r'data-tip="([^"]*)"', tag)
        label = re.search(r'aria-label="([^"]*)"', tag)
        if tip is None and label is None:
            # Tips whose wording quotes a bridge limit are filled in by JS.
            assert re.search(r'id="([^"]+)"', tag), (
                f"a marker with no tip needs an id for the JS to find: {tag}"
            )
            continue
        assert tip and label, f"data-tip and aria-label have to travel together: {tag}"
        assert tip.group(1) == label.group(1), f"tip and label have drifted apart: {tag}"


def test_the_bridge_dependent_tips_are_filled_in():
    """The two tips that quote a bridge limit are set through setTip.

    They ship without text so the numbers cannot go stale, which means nothing
    at all appears if the wiring is dropped.
    """
    assert "function setTip(" in APP_JS
    body = re.search(r"function setTip\([^)]*\)\s*\{(.*?)\n\}", APP_JS, re.S)
    assert body, "setTip is missing"
    assert "dataset.tip" in body.group(1) and "aria-label" in body.group(1), (
        "setTip has to write both halves, or a tip goes hover-only"
    )
    for marker_id in ("#area-tip", "#stream-hz-tip"):
        assert f"setTip($('{marker_id}')" in APP_JS, f"{marker_id} is never filled in"


def test_a_cards_controls_are_gated_on_what_the_device_can_do():
    """A plug loses the flicker controls; a white-only bulb loses the colour row.

    The bridge accepts a PUT carrying a key the device lacks and declines just
    that key, at HTTP 200 — so a control offered here that the device cannot
    honour is set, sent, and silently dropped with nothing reporting it.
    """
    assert "function applyCapabilities(" in APP_JS
    body = re.search(r"function applyCapabilities\([^)]*\)\s*\{(.*?)\n\}", APP_JS, re.S)
    assert body, "applyCapabilities is missing"
    body = body.group(1)
    assert "has_color === false" in body, "the colour row is not gated on has_color"
    assert "dimmable === false" in body, "the flicker controls are not gated on dimmable"
    for hidden in (".color-row", ".controls", ".card-actions", ".card-select"):
        assert hidden in body, f"{hidden} is never hidden for a device that cannot use it"
    assert "applyCapabilities(node, entity.light)" in APP_JS, (
        "buildCard never calls applyCapabilities, so every card gets every control"
    )


def test_the_gated_controls_are_hidden_rather_than_removed():
    """cardSettings() reads the colour box on every send, gated or not."""
    assert "node.querySelector('.color-row').remove()" not in APP_JS
    assert ".color-enable" in APP_JS, "cardSettings still has to find the colour box"


def test_the_card_template_carries_the_slots_the_gating_needs():
    for cls in ("color-row", "controls", "card-actions", "card-select", "cannot-note"):
        assert f'class="{cls}"' in INDEX or f'"{cls} ' in INDEX or f' {cls}"' in INDEX, (
            f"the card template has no .{cls} for applyCapabilities to act on"
        )


def test_the_console_mirrors_the_servers_steady_rule():
    """The button label depends on it, and it is decided client-side.

    A wrong answer here would promise a flicker and deliver a hold, or the
    other way round.
    """
    assert "function isSteadySequence(" in APP_JS
    assert "labelStartButton(card)" in APP_JS, (
        "the label has to be refreshed when the pattern dropdown changes, not "
        "only on a status push"
    )
    assert "settings.holding" in APP_JS, "the badge never reads the held state"


def test_the_diagnostics_button_ships_hidden():
    """It must not be offered before the setting is read back from the server.

    Rendering it and hiding it a moment later would flash a button that the
    server would answer 403 to.
    """
    button = re.search(r'<button id="btn-stream-diagnostics"[^>]*>', INDEX)
    assert button, "the diagnostics button is missing"
    assert "hidden" in button.group(0), "it should start hidden and be revealed"
    assert "applyDiagnosticsSetting" in APP_JS
    assert "diagnostics_enabled" in APP_JS, "the setting is never sent or read"
