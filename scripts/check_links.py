"""Fail on a broken relative link in the documentation.

Documentation that lies is worse than no documentation, and a renamed page is
the easiest way to produce some.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
ROOT = Path(__file__).resolve().parent.parent


def broken() -> list[str]:
    problems: list[str] = []
    sources = [
        *ROOT.glob("*.md"),
        *ROOT.glob("docs/*.md"),
        *ROOT.glob("examples/*.md"),
        *ROOT.glob(".github/*.md"),
    ]
    for source in sources:
        for target in LINK.findall(source.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            path = (source.parent / target.split("#")[0]).resolve()
            if not path.exists():
                problems.append(f"{source.relative_to(ROOT)} -> {target}")
    return problems


def main() -> int:
    problems = broken()
    for problem in problems:
        print(f"broken link: {problem}", file=sys.stderr)
    if problems:
        print(f"\n{len(problems)} broken link(s)", file=sys.stderr)
        return 1
    print("every relative documentation link resolves")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
