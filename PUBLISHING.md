# Releases and images

## Tags

| | |
| --- | --- |
| `ghcr.io/krippler/lighthue:latest` | the newest release |
| `ghcr.io/krippler/lighthue:edge` | tracks `main` between releases |
| `ghcr.io/krippler/lighthue:1.2.3` | one release; `1.2` and `1` follow its line |

`linux/amd64` only. Work branches also publish under their own name, for
trying a change on real hardware before it is merged.

0.5.0 predates this scheme and is published as `:v0.5.0`.

## Cutting a release

1. Rename the top of [CHANGELOG.md](CHANGELOG.md) from `## [Unreleased]` to
   `## [1.2.3] — YYYY-MM-DD`.
2. Bring the template's `<Changes>` and `<Date>` in
   [templates/lighthue.xml](templates/lighthue.xml) up to match. The test suite
   fails until they do: Community Applications shows them as the release notes.
3. Merge to `main`. `.github/workflows/publish.yml` sees the version heading,
   publishes `1.2.3`, `1.2`, `1` and `latest`, creates the `v1.2.3` tag, and
   makes the GitHub Release from that section of the changelog.

Pushing a `v1.2.3` tag by hand does the same, for a release cut outside a
merge. A merge whose top heading is still `[Unreleased]` publishes only
`edge`.

Then start the next section with `## [Unreleased]`.

The version is baked into the image as `LIGHTHUE_VERSION`, shown in the
console's header and printed first in the container log. A build that is not a
release shows the commit it came from instead — `v0.5.0-3-ga569c66` is three
commits past 0.5.0 — so a report can be tied to a build without guessing.
