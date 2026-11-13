# Claude instructions

Read [AGENTS.md](AGENTS.md) before changing code in this repository. It has
the repository map, the required checks, the invariants that must hold, and
the traps specific to this codebase. Do not duplicate its contents here.

In short: `make ci` must pass, a fix comes with a test that fails without it,
comments explain the failure a line prevents, and a public change moves code,
test, `docs/` and `CHANGELOG.md` together.

Ask before releasing, tagging, publishing, or anything that contacts an
external service.
