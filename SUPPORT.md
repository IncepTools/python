# Support

| What | Where |
|---|---|
| How do I …? | [Discussions → Q&A](https://github.com/IncepTools/python/discussions/categories/q-a) |
| This is broken | [Open an issue](https://github.com/IncepTools/python/issues/new/choose) |
| I have an idea | [Discussions → Ideas](https://github.com/IncepTools/python/discussions/categories/ideas) |
| A security problem | **Not a public issue** — [SECURITY.md](SECURITY.md) |

## Before opening an issue

Most reports are resolved faster with these three things:

1. **The version** — `python -c "import inceptools; print(inceptools.VERSION)"`,
   plus your Python and FastAPI versions.
2. **What you expected and what happened.** The envelope's `code` and
   `traceId` from the failing response are usually enough to find it.
3. **The smallest reproduction.** A twenty-line module with one controller
   beats a repository.

## Where the answer probably is

- [docs/](docs/) — one page per capability, each explaining the failure it
  prevents.
- [docs/first-steps.md](docs/first-steps.md) if you are starting out.
- [docs/migrating.md](docs/migrating.md) if you are adopting it in a service
  that already exists.
- [examples/](examples/) — three runnable services, all covered by tests.
- The docstring on the package you are using. They are written to be read.

## Response times

This is a small project maintained alongside other work. Questions and issues
are usually answered within a week; security reports within three working
days. If something is urgent for you, say so and why — it helps with
prioritising.

## Paid support

None. If you need a guarantee, vendor the parts you depend on; it is Apache-2.0
and the code is written to be read.
