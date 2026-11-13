"""Introspection for external tooling.

This is **not** a CLI for building projects — ``icpt`` is one tool, written in
Go, and it covers scaffolding, linting and migrations for both languages. But
only Python can import a Python application and ask it what routes it has, so
this module exists as the seam ``icpt`` shells out to:

    python -m inceptools routes myservice.main:app --json
    python -m inceptools routes myservice.main:app --unguarded

``--json`` prints the same dump as :meth:`Application.routes_json`, which is
the contract the Go side reads.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from typing import Any

from inceptools.application import VERSION, Application


def _load(target: str) -> Application:
    """``package.module:attribute`` -> the Application it names."""
    module_name, _, attribute = target.partition(":")
    if not module_name or not attribute:
        raise SystemExit(f"expected 'module:attribute', got {target!r}")

    sys.path.insert(0, "")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise SystemExit(f"cannot import {module_name!r}: {exc}") from exc

    found: Any = getattr(module, attribute, None)
    if found is None:
        raise SystemExit(f"{module_name!r} has no attribute {attribute!r}")
    if not isinstance(found, Application):
        raise SystemExit(f"{target} is a {type(found).__name__}, not an inceptools Application")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m inceptools",
        description="Introspect an inceptools application. Project tooling lives in icpt.",
    )
    parser.add_argument("--version", action="version", version=f"inceptools {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    routes = sub.add_parser("routes", help="print the route table")
    routes.add_argument("target", help="module:attribute naming the Application")
    routes.add_argument("--json", action="store_true", help="machine-readable dump")
    routes.add_argument(
        "--unguarded",
        action="store_true",
        help="list only mutating routes with no guard; exit 1 if any exist",
    )

    args = parser.parse_args(argv)
    app = _load(args.target)

    if args.unguarded:
        offenders = app.routes.unguarded()
        for item in offenders:
            print(f"{item.route.method} {item.full_path}  ({item.module})")
        if offenders:
            print(f"\n{len(offenders)} mutating route(s) with no guard", file=sys.stderr)
            return 1
        print("every mutating route is guarded or explicitly public")
        return 0

    print(app.routes_json() if args.json else app.route_table())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
