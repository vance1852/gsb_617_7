from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .config import ConfigError, load_config
from .executor import (
    CyclicDependencyError,
    ExecutionStats,
    ExecutorError,
    WorkflowExecutor,
    build_execution_plan,
)
from .logger import EventLogger, setup_logging
from .notifier import send_webhook_notification
from .state import StateStore
from .task import FailureStrategy, TaskStatus


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="saturn",
        description="A lightweight DAG-based workflow runner - like Make on steroids",
    )
    parser.add_argument(
        "-f", "--file",
        default="saturn.toml",
        help="Path to workflow config file (JSON or TOML), default: saturn.toml",
    )
    parser.add_argument(
        "-j", "--jobs", "--concurrency",
        type=int,
        dest="max_concurrency",
        default=None,
        help="Maximum number of parallel tasks (overrides config)",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        dest="dry_run",
        help="Print execution plan without running anything",
    )
    parser.add_argument(
        "--resume", "-r",
        action="store_true",
        help="Resume from last failure, skip completed tasks",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore state file, start fresh",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="TASK",
        help="Run only specified tasks (and their dependencies)",
    )
    parser.add_argument(
        "--exclude", "--skip",
        nargs="+",
        metavar="TASK",
        help="Exclude specified tasks (and their dependents)",
    )
    parser.add_argument(
        "--keep-going", "-k",
        action="store_true",
        dest="continue_on_error",
        help="Continue execution on error (continue-on-error)",
    )
    parser.add_argument(
        "--stop", "-S",
        action="store_true",
        dest="fail_fast",
        help="Stop immediately on first failure (fail-fast, default)",
    )
    parser.add_argument(
        "--state-file",
        help="Path to state file for resumability (overrides config)",
    )
    parser.add_argument(
        "--log-file",
        help="Path to log file (overrides config)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="count",
        default=0,
        help="Verbose output (-v or -vv)",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress non-error output",
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        dest="list_tasks",
        help="List all available tasks and exit",
    )
    parser.add_argument(
        "--version", "-V",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--var",
        action="append",
        nargs=2,
        metavar=("KEY", "VALUE"),
        dest="cli_vars",
        help="Override a variable (--var KEY VALUE, can repeat)",
    )
    parser.add_argument(
        "--no-webhook",
        action="store_true",
        help="Disable webhook notification even if configured",
    )
    parser.add_argument(
        "-C", "--directory",
        default=None,
        help="Change to directory before running",
    )
    return parser


def print_summary(stats: ExecutionStats, task_results: dict) -> None:
    print("", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print(f"Workflow Summary ({stats.total_duration:.2f}s total)", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print(f"  Total:     {stats.total}", file=sys.stderr)
    print(f"  Success:   {stats.success} (up-to-date: {stats.up_to_date})", file=sys.stderr)
    print(f"  Failed:    {stats.failed}", file=sys.stderr)
    print(f"  Skipped:   {stats.skipped}", file=sys.stderr)

    failed = [n for n, r in task_results.items() if r.status == TaskStatus.FAILED]
    skipped = [n for n, r in task_results.items() if r.status == TaskStatus.SKIPPED]

    if failed:
        print("", file=sys.stderr)
        print("Failed tasks:", file=sys.stderr)
        for name in failed:
            r = task_results[name]
            exit_info = f" (exit code {r.exit_code})" if r.exit_code is not None else ""
            print(f"  - {name}{exit_info} after {r.attempts} attempt(s)", file=sys.stderr)
            if r.stderr and r.stderr.strip():
                last_lines = r.stderr.strip().splitlines()[-3:]
                for line in last_lines:
                    print(f"      {line}", file=sys.stderr)

    if skipped:
        print("", file=sys.stderr)
        print("Skipped tasks:", file=sys.stderr)
        for name in skipped:
            r = task_results[name]
            reason = f": {r.skipped_reason}" if r.skipped_reason else ""
            print(f"  - {name}{reason}", file=sys.stderr)

    print("", file=sys.stderr)
    if stats.ok:
        print("Workflow completed successfully", file=sys.stderr)
    else:
        print(f"Workflow failed with {stats.failed} error(s)", file=sys.stderr)


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.directory:
        os.chdir(args.directory)

    base_dir = str(Path.cwd())

    config_path = args.file
    if not Path(config_path).exists():
        alt_candidates = ["saturn.json", "workflow.toml", "workflow.json", ".saturn.toml"]
        found = False
        for alt in alt_candidates:
            if Path(alt).exists():
                config_path = alt
                found = True
                break
        if not found:
            print(f"Error: Config file not found: {args.file}", file=sys.stderr)
            return 2

    try:
        config = load_config(config_path)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    if args.cli_vars:
        for key, value in args.cli_vars:
            config.variables[key] = value

    if args.max_concurrency is not None:
        config.max_concurrency = max(1, args.max_concurrency)

    if args.continue_on_error:
        config.failure_strategy = FailureStrategy.CONTINUE
    elif args.fail_fast:
        config.failure_strategy = FailureStrategy.FAIL_FAST

    if args.state_file:
        config.state_file = str(Path(args.state_file).resolve())
    elif config.state_file is None:
        config.state_file = str(Path(base_dir) / ".saturn" / "state.json")

    if args.log_file:
        config.log_file = str(Path(args.log_file).resolve())

    if args.list_tasks:
        print(f"Workflow: {config.name}")
        print(f"Tasks ({len(config.tasks)}):")
        for name, task in config.tasks.items():
            deps = f" (depends on: {', '.join(task.depends_on)})" if task.depends_on else ""
            desc = f" - {task.description}" if task.description else ""
            print(f"  {name}{deps}{desc}")
            for cmd in task.commands:
                print(f"    $ {cmd}")
        return 0

    verbose = args.verbose >= 1
    quiet = args.quiet and not args.dry_run
    use_color = not args.no_color
    setup_logging(log_file=config.log_file, verbose=verbose, quiet=quiet, use_color=use_color)

    event_logger = EventLogger(logging := __import__("logging").getLogger("saturn"))

    state_store = StateStore(config.state_file)

    resume = args.resume and not args.no_resume

    try:
        executor = WorkflowExecutor(
            config=config,
            base_dir=base_dir,
            state_store=state_store,
            dry_run=args.dry_run,
            resume=resume,
            only_tasks=args.only,
            exclude_tasks=args.exclude,
            log_callback=event_logger.log,
        )
    except CyclicDependencyError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    if not args.dry_run:
        event_logger.log("", f"Saturn workflow '{config.name}' starting")
        event_logger.log("", f"Config: {Path(config_path).resolve()}")
        event_logger.log("", f"State file: {config.state_file}")
        if resume:
            event_logger.log("", "Resuming from previous run")

    try:
        stats = executor.run()
    except CyclicDependencyError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except ExecutorError as e:
        print(f"Execution error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        print("Interrupted by user", file=sys.stderr)
        return 130

    if not args.dry_run:
        print_summary(stats, executor.task_results)

    if config.webhook_url and not args.dry_run and not args.no_webhook:
        event_logger.log("", f"Sending webhook notification to {config.webhook_url}")
        send_webhook_notification(
            url=config.webhook_url,
            workflow_name=config.name,
            stats=stats,
            task_results=executor.task_results,
        )

    return 0 if stats.ok else 1


if __name__ == "__main__":
    sys.exit(main())
