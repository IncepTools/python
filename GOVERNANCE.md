# Governance

Small project, short document.

## Decisions

Maintainers decide, in the open, in issues and pull requests. Disagreement
is resolved by argument about the failure a change prevents or causes — not
by seniority and not by vote. If that does not converge, the maintainers
listed in [MAINTAINERS.md](MAINTAINERS.md) make the call and write down why.

## What needs more than one maintainer's agreement

- Removing or changing an exported identifier.
- Adding a dependency to the core package (not an optional extra).
- Changing a default that alters behaviour for existing users.
- A v2.

Everything else — bug fixes, documentation, tests, new optional surface —
is a normal review.

## Adding a dependency

The bar is high and deliberately so: a framework's dependencies become every
importer's dependencies. A pull request adding one to the root module must
say what it is for, what it would take to do without it, and what its own
dependency tree looks like.

Dependencies for the CLI go under `internal/`, where nothing outside the
module can reach them. Dependencies for the examples go in the examples
module. Neither reaches a library consumer.

## Becoming a maintainer

Sustained, good-quality contribution, and judgement visible in reviews — in
particular, the habit of asking what failure a change prevents. An existing
maintainer proposes it; the others agree.

Stepping back is normal and needs no justification. Move yourself to
*Emeritus* in `MAINTAINERS.md` and open a pull request.

## The thing to preserve

This framework is opinionated on purpose, and every feature is something
every user has to read past. The strongest argument for a change is a
concrete failure it prevents; the strongest argument against one is that it
adds surface without removing a failure mode.

A maintainer's main job is saying no to the second kind, kindly.
