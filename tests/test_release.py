"""The release machinery, run for real.

What publishes a version is shell inside .github/workflows/publish.yml, and
nothing exercises it short of merging to main and watching. So these lift the
workflow's own run blocks out of the file and execute them — against a scratch
git repository, with a stand-in for `gh` — rather than restating the logic in
Python, where it could agree with itself and still disagree with CI.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "publish.yml"
CHANGELOG = ROOT / "CHANGELOG.md"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None,
                                reason="needs bash and git, as the runner has")


def step(job: str, name: str) -> str:
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"][job]["steps"]
    found = [s for s in steps if s.get("name") == name or s.get("id") == name]
    assert found, f"no step {name!r} in {job}"
    return found[0]["run"]


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


def repo_with(tmp_path: Path, changelog: str, tags=()) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "CHANGELOG.md").write_text(changelog)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")
    for tag in tags:
        git(repo, "tag", "-a", tag, "-m", tag)
    return repo


def run(script: str, repo: Path, tmp_path: Path, **env) -> dict:
    """Run a step's shell the way the runner does, and read back its outputs."""
    out = tmp_path / "github_output"
    out.write_text("")
    result = subprocess.run(["bash", "-c", script], cwd=repo, capture_output=True, text=True,
                            env={**os.environ, "GITHUB_OUTPUT": str(out), **env})
    assert result.returncode == 0, result.stdout + result.stderr
    return dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)


def released(changelog: str, tmp_path: Path, tags=()) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = repo_with(tmp_path, changelog, tags)
    return run(step("publish", "detect"), repo, tmp_path)


SKELETON = "# Changelog\n\n{top}\n\n### Added\n\n- A thing.\n\n## [0.5.0] — 2026-09-05\n\n- Old.\n"


# ---------- the changelog decides ----------

def test_a_merge_still_unreleased_publishes_edge_only(tmp_path):
    assert released(SKELETON.format(top="## [Unreleased]"), tmp_path) == {
        "is_release_merge": "false"}


def test_a_merge_with_a_version_on_top_is_a_release(tmp_path):
    out = released(SKELETON.format(top="## [0.6.0] — 2026-10-01"), tmp_path)
    assert out == {"is_release_merge": "true", "version": "0.6.0",
                   "major": "0", "major_minor": "0.6"}


def test_a_version_already_tagged_is_not_released_twice(tmp_path):
    """A later merge with the same heading on top must not re-cut it."""
    out = released(SKELETON.format(top="## [0.6.0] — 2026-10-01"), tmp_path, tags=["v0.6.0"])
    assert out == {"is_release_merge": "false"}


def real_versions() -> list[str]:
    return re.findall(r"^## \[(\d+\.\d+\.\d+)\]", CHANGELOG.read_text(), re.M)


def test_the_real_changelog_releases_exactly_what_its_top_heading_says(tmp_path):
    """Whatever state the file is in: work in progress publishes edge only, and
    a version on top is cut once — never again after its tag exists."""
    top = re.search(r"^## \[([^\]]+)\]", CHANGELOG.read_text(), re.M).group(1)
    older = [f"v{v}" for v in real_versions() if v != top]
    fresh = released(CHANGELOG.read_text(), tmp_path / "fresh", tags=older)
    if top == "Unreleased":
        assert fresh == {"is_release_merge": "false"}
        return
    assert fresh["is_release_merge"] == "true" and fresh["version"] == top
    again = released(CHANGELOG.read_text(), tmp_path / "again", tags=[*older, f"v{top}"])
    assert again == {"is_release_merge": "false"}, "a tagged version must not be re-cut"


def test_a_new_version_on_top_of_the_real_changelog_is_cut(tmp_path):
    """What PUBLISHING.md tells you to do, done to the real file — its preamble
    and all, which quotes the heading syntax before any heading appears."""
    text = CHANGELOG.read_text()
    first = re.search(r"^## \[", text, re.M).start()
    cut = text[:first] + "## [99.0.0] — 2099-01-01\n\n- Next.\n\n" + text[first:]
    out = released(cut, tmp_path, tags=[f"v{v}" for v in real_versions()])
    assert out["is_release_merge"] == "true" and out["version"] == "99.0.0"


# ---------- the changelog's own shape ----------

def headings() -> list[str]:
    return re.findall(r"^## .*$", CHANGELOG.read_text(), re.M)


def test_every_heading_is_one_the_workflow_can_read():
    for heading in headings():
        assert heading == "## [Unreleased]" or re.fullmatch(
            r"## \[\d+\.\d+\.\d+\] — \d{4}-\d{2}-\d{2}", heading), heading


def test_unreleased_only_ever_sits_on_top():
    marks = [i for i, h in enumerate(headings()) if h == "## [Unreleased]"]
    assert marks in ([], [0]), "an [Unreleased] below a version would never publish"


def test_versions_run_newest_first():
    found = [tuple(int(n) for n in m.split("."))
             for m in re.findall(r"^## \[(\d+\.\d+\.\d+)\]", CHANGELOG.read_text(), re.M)]
    assert found == sorted(found, reverse=True) and len(set(found)) == len(found), found


# ---------- the stamp baked into the image ----------

def stamp(repo: Path, tmp_path: Path, version: str = "", **env) -> str:
    script = step("publish", "Work out the build stamp").replace(
        "${{ steps.detect.outputs.version }}", version)
    return run(script, repo, tmp_path, **env)["version"]


def test_a_release_build_is_stamped_with_its_version(tmp_path):
    repo = repo_with(tmp_path, "# Changelog\n")
    assert stamp(repo, tmp_path, "0.6.0") == "0.6.0"


def test_a_hand_pushed_tag_is_stamped_with_its_version(tmp_path):
    repo = repo_with(tmp_path, "# Changelog\n", tags=["v0.6.0"])
    assert stamp(repo, tmp_path, GITHUB_REF="refs/tags/v0.6.0",
                 GITHUB_REF_NAME="v0.6.0") == "0.6.0"


def test_anything_else_is_stamped_with_the_commit_it_came_from(tmp_path):
    repo = repo_with(tmp_path, "# Changelog\n", tags=["v0.5.0"])
    for n in range(3):
        (repo / "f").write_text(str(n))
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", str(n))
    sha = git(repo, "rev-parse", "--short", "HEAD")
    assert stamp(repo, tmp_path, GITHUB_REF="refs/heads/main") == f"v0.5.0-3-g{sha}"


# ---------- the tag and the release notes ----------

def test_a_release_merge_pushes_its_tag(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo = repo_with(tmp_path, "# Changelog\n")
    git(repo, "remote", "add", "origin", str(origin))
    run(step("publish", "Auto-create vX.Y.Z git tag"), repo, tmp_path, VERSION="0.6.0")
    assert "refs/tags/v0.6.0" in git(repo, "ls-remote", "--tags", "origin")


def release_notes(tmp_path: Path, changelog: str | None = None, **env) -> tuple[str, str]:
    """Run the release step against a changelog with `gh` stood in for."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "gh-calls"
    (bin_dir / "gh").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{calls}"\n'
        # "release view" is how the step asks whether it exists: say no.
        '[ "$2" = view ] && exit 1\n'
        "exit 0\n")
    (bin_dir / "gh").chmod(0o755)
    repo = repo_with(tmp_path, changelog or CHANGELOG.read_text())
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["github-release"]["steps"]
    script = next(s["run"] for s in steps if s.get("name") == "Create / update GitHub Release")
    run(script, repo, tmp_path, PATH=f"{bin_dir}:{os.environ['PATH']}",
        GITHUB_REPOSITORY="Krippler/LightHue", GITHUB_SHA="abc1234", GH_TOKEN="x", **env)
    return (repo / "notes.md").read_text(), calls.read_text()


def test_the_release_is_written_from_that_versions_changelog_section(tmp_path):
    notes, calls = release_notes(tmp_path, GITHUB_REF="refs/heads/main",
                                 RELEASE_VERSION="0.5.0")
    assert "First tagged release" in notes
    # The section ends where the next heading starts: nothing from Unreleased
    # above it, nothing from an older release below.
    assert "[Unreleased]" not in notes and "## [" not in notes
    assert "docker pull ghcr.io/krippler/lighthue:0.5.0" in notes
    assert "release create v0.5.0 --title v0.5.0" in calls


def test_a_hand_pushed_tag_gets_the_same_release(tmp_path):
    notes, calls = release_notes(tmp_path, GITHUB_REF="refs/tags/v0.5.0",
                                 GITHUB_REF_NAME="v0.5.0")
    assert "First tagged release" in notes
    assert "release create v0.5.0" in calls


def test_a_release_in_the_middle_takes_only_its_own_section(tmp_path):
    """0.5.0 is the last section in the real file, so it cannot show this."""
    changelog = ("# Changelog\n\n## [Unreleased]\n\n- Not yet.\n\n"
                 "## [0.6.0] — 2026-10-01\n\n- The new one.\n\n"
                 "## [0.5.0] — 2026-09-05\n\n- The old one.\n")
    notes, _ = release_notes(tmp_path, changelog, GITHUB_REF="refs/heads/main",
                             RELEASE_VERSION="0.6.0")
    assert "The new one." in notes
    assert "Not yet." not in notes and "The old one." not in notes
