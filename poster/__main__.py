"""CLI: python -m poster slot|prefetch|health."""

from __future__ import annotations

import argparse
from pathlib import Path

from poster.runner import run_health, run_prefetch, run_slot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish queued clips to Buffer on a fixed slot clock.")
    parser.add_argument("command", choices=("slot", "prefetch", "health"))
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Repository root (default: working directory)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate one clip and skip the Buffer publish call. Queue files are not moved.",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.command == "health":
        return run_health(root)
    if args.command == "prefetch":
        return run_prefetch(root, dry_run=args.dry_run)
    return run_slot(root, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
