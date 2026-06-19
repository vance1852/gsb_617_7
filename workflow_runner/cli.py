from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional, Set

from . import __version__
from .config import ConfigError, WorkflowConfig, load_config
from .graph import CyclicDependencyError
from .logger import setup_logger
from .runner import RunOptions, WorkflowRunner
from .state import StateStore


def _parse_var_pairs(pairs: List[str]) -> dict:
    result = {}
    for p in pairs:
        if "=" not in p:
            raise argparse.ArgumentTypeError(f"Variable override must be KEY=VALUE, got: {p}")
        k, v = p.split("=", 1)
        k = k.strip()
        if not k:
            raise argparse.ArgumentTypeError(f"Empty key in variable override: {p}")
        result[k] = v
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="workflow-runner",
        description="A lightweight Make-like workflow runner with task orchestration, parallel execution, "
                    "incremental builds, and resume support.",
    )
    parser.add_argument(
        "config",
        nargs="?",
        default="workflow.json",
        help="Path to workflow config file (.json or .toml). Default: workflow.json",
    )
    parser.add_argument(
        "--version", action="version", version=f"workflow-runner {__version__}"
    )
    parser.add_argument(
        "-n", "--dry-run",
        action="store_true",
        help="Print the execution plan without actually running any commands",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from previous state: skip tasks that already succeeded in the last run",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="TASK",
        help="Run only the specified tasks (and their dependencies)",
    )
    parser.add_argument(
        "--exclude",
        nargs="+",
        metavar="TASK",
        help="Exclude the specified tasks (and their dependents) from execution",
    )
    parser.add_argument(
        "-j", "--jobs",
        type=int,
        default=None,
        help="Maximum number of parallel tasks (overrides config max_concurrency)",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        default=None,
        help="Stop scheduling new tasks as soon as one task fails (default behavior)",
    )
    parser.add_argument(
        "--continue",
        dest="continue_on_error",
        action="store_true",
        default=None,
        help="Continue running independent tasks even if some tasks fail (continue-on-error)",
    )
    parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="Force re-run all tasks, ignoring incremental up-to-date checks",
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help="Override state file path for persistence/resume",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Override log file path",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose/debug logging",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Suppress console output (log file will still be written if configured)",
    )
    parser.add_argument(
        "--var",
        nargs="+",
        metavar="KEY=VALUE",
        default=[],
        help="Override global variables from command line (e.g. --var env=prod version=1.2)",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    config_path = Path(args.config).resolve()
    if not config_path.exists():
        print(f"Error: config file not found: {config_path}", file=sys.stderr)
        return 2

    try:
        config = load_config(config_path)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    if args.jobs is not None:
        config.max_concurrency = max(1, args.jobs)

    if args.continue_on_error:
        config.fail_fast = False
    elif args.fail_fast:
        config.fail_fast = True

    if args.state_file:
        config.state_file = args.state_file
    if args.log_file:
        config.log_file = args.log_file

    if config.state_file is None:
        config.state_file = str(config_path.parent / f".{config.name}.state.json")

    variable_overrides = _parse_var_pairs(args.var) if args.var else None

    only_set = set(args.only) if args.only else None
    exclude_set = set(args.exclude) if args.exclude else None

    if only_set:
        unknown = only_set - set(config.tasks.keys())
        if unknown:
            print(f"Error: unknown tasks in --only: {', '.join(sorted(unknown))}", file=sys.stderr)
            print(f"Available tasks: {', '.join(sorted(config.tasks.keys()))}", file=sys.stderr)
            return 2
    if exclude_set:
        unknown = exclude_set - set(config.tasks.keys())
        if unknown:
            print(f"Error: unknown tasks in --exclude: {', '.join(sorted(unknown))}", file=sys.stderr)
            print(f"Available tasks: {', '.join(sorted(config.tasks.keys()))}", file=sys.stderr)
            return 2

    setup_logger(
        name="workflow",
        log_file=config.log_file,
        verbose=args.verbose,
        quiet=args.quiet,
    )

    state_store = StateStore(config.state_file)
    base_dir = str(config_path.parent)

    opts = RunOptions(
        dry_run=args.dry_run,
        resume=args.resume,
        only_tasks=only_set,
        exclude_tasks=exclude_set,
        fail_fast=config.fail_fast,
        force=args.force,
        variables=variable_overrides,
    )

    runner = WorkflowRunner(
        config=config,
        state_store=state_store,
        base_dir=base_dir,
        options=opts,
    )

    try:
        return runner.run()
    except CyclicDependencyError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
