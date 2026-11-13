# Contributing

The short version: **explain the failure a line prevents, not the mechanism**;
`make ci` must pass; a fix comes with a test that fails without it.

## Getting set up

```bash
git clone https://github.com/IncepTools/python
cd python
uv sync --all-extras
make ci
```

[uv](https://docs.astral.sh/uv/) manages the environment. If you would rather
not install it, `pip install -e ".[all]"` plus the dev dependencies in
`pyproject.toml` works.

## The checks

```bash
make test       # pytest
make lint       # ruff check
make format     # ruff format
make types      # mypy
make ci         # all of the above, in the order CI runs them
```

Run the narrowest thing while iterating — `uv run pytest tests/web` — then
`make ci` before opening a pull request.

## What a good change looks like

**It names a failure.** This framework is organised around failure modes, not
features. A pull request that says "adds a `retries` option" is harder to
review than one that says "a transient 503 from the pricing service currently
fails the whole checkout; this retries it, and only the kinds that are safe to
repeat." The second one tells a reviewer how to judge it.

**It comes with a test that fails without it.** For a bug fix, write the test
first and watch it fail. For a feature, test the guarantee rather than the
implementation: `test_a_query_with_no_account_is_refused` will survive a
rewrite of the repository; `test_scope_calls_where` will not.

**Its comments explain why.** The code says what it does. A comment earns its
place by saying what goes wrong without it:

```python
# Compare-and-delete: releasing by key alone means a slow holder whose TTL
# expired deletes the *next* holder's lock, and now two replicas run at once.
```

Not:

```python
# Delete the key if the token matches.
```

**It does not widen the public surface by accident.** Anything in `__all__` is
something we have to keep working. If a helper is only for internal use, do not
export it.

## Conventions

- **Formatting** is `ruff format`, line length 100. Do not hand-format.
- **Types.** `mypy` must pass. Annotate public signatures; a private helper may
  be loose where the annotation would be noise.
- **Docstrings** on every public module, class and function. A module docstring
  says what the package is for and which failure it prevents.
- **Tests** mirror the source tree under `tests/`, named after guarantees.
- **No new core dependency** without explaining why it belongs in every
  consumer's dependency graph. The bar is deliberately high — a framework's
  dependencies become every importer's. Optional capabilities go in an extra.
- **No `# type: ignore`** or `# noqa` without a reason on the same line.

## Commits and pull requests

Commit subjects are `<type>: <description>` using `feat:`, `fix:`, `docs:`,
`test:`, `refactor:`, `perf:`, `build:`, `ci:`, `style:`, `chore:` or
`revert:`. The subject starts lower-case and does not end with a full stop —
it becomes a commit subject, not a sentence. A pull request title is checked
against the same list, because it becomes the squashed commit subject.

A pull request should say what failure it addresses, what you changed, and how
you verified it. If it changes behaviour, update the guide under `docs/` and
add a line to `CHANGELOG.md` under **Unreleased**.

## Changing a public capability

Four things move together, and a review will ask for all four:

1. the code,
2. a test naming the guarantee,
3. the relevant page in `docs/`,
4. `CHANGELOG.md` under **Unreleased**.

If it changes a default, say so under **Changed** with what a user should do
about it.

## Adding an example

An example under `examples/` must stay runnable — `tests/app/test_examples.py`
asserts every one of them builds, serves and has no unguarded writes.
Documentation that lies is worse than no documentation.

## Security

Do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).

## Questions

[Discussions → Q&A](https://github.com/IncepTools/python/discussions/categories/q-a).
Asking before building something large is welcome and usually faster.
