## What failure does this address?

<!-- Not the feature — the thing that currently goes wrong. A reviewer judges
     the change against this. -->

## What changed

<!-- The shape of the change. Mention anything a reader would find surprising. -->

## How it was verified

<!-- The command you ran, and the test that fails without this change. -->

```
make ci
```

## Checklist

- [ ] A test that fails without this change
- [ ] `make ci` passes
- [ ] The relevant page under `docs/` is updated, if behaviour changed
- [ ] A line in `CHANGELOG.md` under **Unreleased**
- [ ] No new core dependency (or the pull request explains why it belongs in
      every consumer's dependency graph)
- [ ] Nothing new in `__all__` that is not meant to be public
