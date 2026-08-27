"""Checks on the documentation that the suite can actually enforce.

Prose is not testable, but three things about it are: that it does not repeat
itself, that its links go somewhere, and that the numbers it quotes are the
numbers the code uses. All three have gone wrong here before — the README
carried the same paragraph about area rows twice, arrived at by editing the
same section on two different days.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = [ROOT / "README.md", ROOT / "docs" / "streaming.md",
        ROOT / "docs" / "pattern-packs.md"]


def paragraphs(text: str) -> list[str]:
    """Prose blocks, normalised so a rewrap is not mistaken for a difference."""
    out = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(("#", "|", "```", "-", "*", "1.", "2.")):
            continue
        out.append(" ".join(block.split()))
    return out


def test_no_document_says_the_same_thing_twice():
    for doc in DOCS:
        seen: dict[str, int] = {}
        for para in paragraphs(doc.read_text()):
            # Short lines repeat innocently ("```bash" fences, one-line notes).
            if len(para) < 120:
                continue
            seen[para] = seen.get(para, 0) + 1
        repeats = {p[:70]: n for p, n in seen.items() if n > 1}
        assert not repeats, f"{doc.name} repeats a paragraph: {repeats}"


def test_every_internal_link_goes_somewhere():
    for doc in DOCS:
        for target in re.findall(r"\]\(([^)]+)\)", doc.read_text()):
            if target.startswith(("http://", "https://", "#")):
                continue
            resolved = (ROOT / target)
            if not resolved.exists():
                resolved = (doc.parent / target)
            assert resolved.exists(), f"{doc.name} links to a missing {target}"


def test_the_numbers_the_docs_quote_are_the_numbers_the_code_uses():
    from app.config_store import _DEFAULT_SETTINGS
    from app.hue_stream import MAX_AREA_LIGHTS, MAX_STREAM_HZ
    from app.patterns import GAMES

    text = "\n".join(d.read_text() for d in DOCS)
    assert f"**{MAX_AREA_LIGHTS} lights**" in text, "the area ceiling has drifted"
    assert f"{int(MAX_STREAM_HZ)} Hz" in text, "the stream rate has drifted"
    assert f"{_DEFAULT_SETTINGS['stream_settle_ms']} ms" in text, "the settle default has drifted"

    # "twenty classic games" is the picker's count, which includes the games
    # that share another engine's table rather than owning patterns.
    words = {16: "sixteen", 20: "twenty", 21: "twenty-one"}
    assert f"{words.get(len(GAMES), len(GAMES))} classic games" in text, (
        f"the README's game count is not {len(GAMES)}"
    )


def test_the_docs_stay_wrapped():
    """Unwrapped prose makes a one-word change look like a rewritten paragraph."""
    for doc in DOCS:
        for n, line in enumerate(doc.read_text().splitlines(), 1):
            assert len(line) <= 88, f"{doc.name}:{n} is {len(line)} chars"
