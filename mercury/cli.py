"""CLI entry point."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from .config import ConfigError, load_workflow
from .graph import GraphError
from .runner import WorkflowRunner


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mercury",
        description="Mercury — a lightweight workflow runner (Make-like).",
    )
    p.add_argument(
        "-f", "--file",
        default="workflow.json",
        help="Path to workflow config (.json or .toml). Default: workflow.json",
    )
    p.add_argument(
        "--only", "--include",
        dest="include",
        action="append",
        default=[],
        help="Only run these task(s) (and their dependencies). May repeat.",
    )
    p.add_argument(
        "--exclude",
        dest="exclude",
        action="append",
        default=[],
        help="Exclude these task(s) (and their descendants). May repeat.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan & rendered commands but execute nothing.",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="Resume from previous run, skipping tasks that already succeeded.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Ignore incremental cache; re-run even if up-to-date.",
    )
    p.add_argument(
        "--max-parallel",
        type=int,
        default=None,
        help="Override max_parallel from the config file.",
    )
    p.add_argument(
        "--fail-strategy",
        choices=("fail-fast", "continue-on-error"),
        default=None,
        help="Override fail strategy from the config file.",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="List the planned tasks in topological order and exit.",
    )
    p.add_argument(
        "-V", "--version",
        action="store_true",
        help="Print version and exit.",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        from . import __version__
        print(f"mercury {__version__}")
        return 0

    cfg_path = Path(args.file).resolve()
    try:
        wf = load_workflow(cfg_path)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    if args.max_parallel is not None:
        wf.max_parallel = max(1, args.max_parallel)
    if args.fail_strategy is not None:
        wf.fail_strategy = args.fail_strategy

    base_dir = Path(wf.workdir).resolve() if wf.workdir else cfg_path.parent

    try:
        runner = WorkflowRunner(
            workflow=wf,
            base_dir=base_dir,
            include=args.include,
            exclude=args.exclude,
            dry_run=args.dry_run,
            resume=args.resume,
            force=args.force,
        )
    except GraphError as e:
        print(f"Graph error: {e}", file=sys.stderr)
        return 3

    if args.list:
        try:
            plan = runner.plan()
        except GraphError as e:
            print(f"Graph error: {e}", file=sys.stderr)
            return 3
        for name in plan:
            t = runner.graph.tasks[name]
            deps = ", ".join(t.depends_on) if t.depends_on else "-"
            print(f"{name}\t(deps: {deps})")
        return 0

    try:
        summary = runner.run()
    except GraphError as e:
        print(f"Graph error: {e}", file=sys.stderr)
        return 3

    return 0 if not summary.failed and not summary.cancelled else 1


if __name__ == "__main__":
    sys.exit(main())
