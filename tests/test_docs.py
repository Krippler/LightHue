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


# ---------- Deployment templates ----------

TEMPLATE = ROOT / "unraid-template.xml"
COMPOSE = ROOT / "docker-compose.yml"


def template():
    import xml.etree.ElementTree as ET
    return ET.parse(TEMPLATE).getroot()


def test_the_unraid_template_has_what_community_applications_needs():
    """A missing field here is rejected at submission, not at install.

    Every one of these is load-bearing: without Icon and Overview the app has
    no card, without TemplateURL it cannot update itself, and without Support
    and Project there is nowhere for a user to go when it breaks.
    """
    root = template()
    for field in ("Name", "Repository", "Registry", "Network", "Shell",
                  "Privileged", "Support", "Project", "Overview", "Category",
                  "WebUI", "TemplateURL", "Icon"):
        value = (root.findtext(field) or "").strip()
        assert value, f"the template has no {field}"

    assert root.findtext("Privileged").strip() == "false"
    for field in ("Support", "Project", "TemplateURL", "Icon"):
        assert root.findtext(field).strip().startswith("https://"), field


def test_the_template_still_calls_the_project_by_its_name():
    """It was shipped as GameHueFlicker for a while after the rename.

    <Name> is what Unraid calls the container it creates, so a stale one is
    what every new install ends up with.
    """
    root = template()
    assert root.findtext("Name").strip() == "LightHue"
    text = TEMPLATE.read_text() + COMPOSE.read_text()
    assert "game-hue-flicker" not in text, "the old project name is still in a deploy file"
    assert "GameHueFlicker" not in text


def test_the_image_the_template_pulls_is_the_one_ci_publishes():
    published = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    assert "ghcr.io/${{ github.repository }}" in published
    assert template().findtext("Repository").strip() == "ghcr.io/krippler/lighthue:latest"


def test_host_networking_users_are_given_the_port_field_the_readme_names():
    """The README tells them to set PORT; the template has to offer it.

    With host networking the port mapping does nothing, so this variable is the
    only way to move the listener.
    """
    ports = [c for c in template().findall("Config")
             if c.get("Type") == "Variable" and c.get("Target") == "PORT"]
    assert ports, "no PORT variable in the template"
    assert ports[0].get("Required") == "false", "it is only needed on host networking"
    # The README sends people to it by name, so the two have to agree on one.
    # Compared with whitespace flattened: the name spans a line break there.
    readme = " ".join((ROOT / "README.md").read_text().split())
    assert " ".join(ports[0].get("Name").split()) in readme, (
        "the README does not name the field the template offers"
    )


def test_the_icon_the_template_points_at_is_in_the_repo():
    """CA fetches it by URL from the default branch, so a moved file is a blank card."""
    icon = template().findtext("Icon").strip()
    assert icon.endswith(".png")
    path = icon.split("/main/", 1)[1]
    assert (ROOT / path).exists(), f"{path} is not in the repo"
