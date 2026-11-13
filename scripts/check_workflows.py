"""Check the CI configuration before CI runs it.

A workflow with a typo fails on the push that introduces it, which is a slow
way to find out. This parses every workflow and asserts the few things that go
wrong in practice:

* the YAML parses at all;
* every job declares a runner and at least one step;
* no step is both ``uses`` and ``run``;
* every ``uses`` is pinned to a major version or a SHA, never a branch;
* the workflow declares ``permissions``, so it does not inherit write-all.

If ``actionlint`` is on the PATH it runs that too, which catches a great deal
more.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    print("pyyaml is not installed; skipping workflow checks", file=sys.stderr)
    raise SystemExit(0) from None

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


#: Actions whose maintainers publish a moving ref on purpose, and where
#: following it is the documented and safer choice.
#:
#: ``gh-action-pypi-publish@release/v1`` is PyPA's own recommendation: they
#: move that branch to patch the publishing flow itself, including the trusted
#: publishing handshake. Pinning it would mean shipping a known-stale
#: credential exchange until somebody noticed.
PINNING_EXCEPTIONS = {
    "pypa/gh-action-pypi-publish": "release/v1",
}


def _uses_is_pinned(uses: str) -> bool:
    """``actions/checkout@v5`` or a SHA, never ``@main``."""
    if "@" not in uses:
        return False
    action, ref = uses.rsplit("@", 1)
    if PINNING_EXCEPTIONS.get(action) == ref:
        return True
    ref = uses.rsplit("@", 1)[1]
    if ref.startswith("v") and ref[1:2].isdigit():
        return True
    if len(ref) == 40 and all(c in "0123456789abcdef" for c in ref):
        return True
    # A local or Docker action has no version to pin.
    return uses.startswith(("./", "docker://"))


def check(path: Path) -> list[str]:
    problems: list[str] = []
    try:
        document: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [f"{path.name}: does not parse: {exc}"]

    if not isinstance(document, dict):
        return [f"{path.name}: the top level is not a mapping"]

    if "permissions" not in document:
        problems.append(
            f"{path.name}: no top-level `permissions` — it would inherit the "
            "repository default, which may be write-all"
        )

    jobs = document.get("jobs") or {}
    if not jobs:
        problems.append(f"{path.name}: no jobs")

    for name, job in jobs.items():
        if not isinstance(job, dict):
            problems.append(f"{path.name}:{name}: job is not a mapping")
            continue
        if "uses" in job:
            continue  # a reusable workflow call has no steps of its own
        if "runs-on" not in job:
            problems.append(f"{path.name}:{name}: no `runs-on`")
        steps = job.get("steps") or []
        if not steps:
            problems.append(f"{path.name}:{name}: no steps")
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                problems.append(f"{path.name}:{name}: step {index} is not a mapping")
                continue
            if "uses" in step and "run" in step:
                problems.append(
                    f"{path.name}:{name}: step {index} has both `uses` and `run`"
                )
            if "uses" in step and not _uses_is_pinned(str(step["uses"])):
                problems.append(
                    f"{path.name}:{name}: step {index} uses {step['uses']!r}, "
                    "which is not pinned to a version or SHA"
                )
    return problems


def main() -> int:
    if not WORKFLOWS.is_dir():
        print("no workflows to check")
        return 0

    files = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    problems = [problem for path in files for problem in check(path)]

    for path in sorted(ROOT.glob(".github/*.yml")):
        try:
            yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            problems.append(f"{path.name}: does not parse: {exc}")

    for problem in problems:
        print(f"workflow: {problem}", file=sys.stderr)

    if shutil.which("actionlint"):
        print("running actionlint")
        result = subprocess.run(["actionlint"], cwd=ROOT, check=False)  # noqa: S603, S607
        if result.returncode != 0:
            return result.returncode
    else:
        print("actionlint is not installed; ran the built-in checks only")

    if problems:
        print(f"\n{len(problems)} workflow problem(s)", file=sys.stderr)
        return 1
    print(f"{len(files)} workflow(s) look sound")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
