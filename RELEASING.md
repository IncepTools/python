# Releasing

A release is a tag. Everything else is automated, and the automation refuses to
publish anything it cannot verify.

## What happens

1. You bump the version in **three places** and tag.
2. `release.yml` checks the tag, `pyproject.toml` and `inceptools.VERSION`
   agree, that `CHANGELOG.md` has a section for it, and that `make ci` passes.
3. It builds the wheel and sdist, imports the wheel on a clean interpreter,
   checks the metadata, generates an SBOM, and attests the build provenance.
4. It publishes to PyPI through **trusted publishing** — no API token exists
   anywhere to leak.
5. It creates a GitHub release whose notes are the changelog section, with the
   artefacts and the SBOM attached.

If any step fails, nothing is published. A half-released version is worse than
a late one.

## The checklist

```bash
# 1. The changelog. Written by hand — a list of merged pull requests tells a
#    reader nothing about what to do differently.
$EDITOR CHANGELOG.md            # move Unreleased into [X.Y.Z] — YYYY-MM-DD

# 2. The version, in both places that record it.
$EDITOR pyproject.toml          # [project] version
$EDITOR src/inceptools/application.py   # VERSION

# 3. Prove it locally first. CI will run the same thing.
make ci
make build
uv run --isolated --no-project --with dist/*.whl \
  python -c "import inceptools; print(inceptools.VERSION)"

# 4. Commit, tag, push.
git commit -am "chore: release v0.2.0"
git tag -a v0.2.0 -m "v0.2.0"
git push origin main --follow-tags
```

Then watch the `release` workflow. A failure before the `publish` job is safe
to fix and re-tag; a failure after it means the version exists on PyPI and the
next one must be a new patch, because PyPI does not allow a re-upload.

## A dry run

```bash
gh workflow run release.yml -f dry-run=true
```

Builds, verifies and attests without publishing. Worth doing before the first
release from a new machine or after changing the workflow.

## Versioning

[Semantic Versioning](https://semver.org).

Pre-1.0 the exported API may change between minor versions. Every such change
goes under **Changed** in the changelog with what to do about it — a breaking
change with no migration note is not finished.

After 1.0, no exported identifier changes incompatibly within a major version.
"Exported" means anything in an `__all__`, including the subpackages.

### What counts as breaking

- Removing or renaming anything in an `__all__`.
- Changing a parameter's name, order or meaning.
- Changing a default that alters behaviour — a stricter guard, a different
  status code, a narrower validation rule.
- Changing the response envelope's shape or the meaning of a `code`.
- Raising the minimum Python version.

### What does not

- Adding a keyword argument with a backwards-compatible default.
- Adding a new kind, code, hook or module.
- Anything under a `_private` name.
- Tightening a type annotation that was wider than the implementation.

## Required repository settings

These are the parts an automation cannot create for itself:

- **PyPI trusted publisher** for `inceptools`, pointing at this repository,
  workflow `release.yml`, environment `pypi`.
- A **`pypi` environment** with required reviewers, so a publish is a
  deliberate act rather than a consequence of a pushed tag.
- **`github-pages` environment** for the docs deploy.
- **Branch protection on `main`**: require the `ci` checks, require a review,
  no force pushes.
- **Actions** permitted to create releases and write to Pages.

## After a release

- Check the docs site rebuilt.
- Confirm the PyPI page renders the README.
- Open the next **Unreleased** section in the changelog.
