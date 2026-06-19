from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from .config import load_config
from .executor import TaskExecutor
from .graph import CyclicDependencyError, TaskGraph
from .logger import Logger
from .models import FailureStrategy, TaskStatus
from .state import StateStore
from .webhook import send_webhook


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jupiter",
        description="Lightweight workflow runner with dependency graph, parallel execution, and incremental builds.",
    )
    p.add_argument(
        "-c", "--config",
        default="jupiter.json",
        help="Path to workflow config file (JSON or TOML, default: jupiter.json)",
    )
    p.add_argument(
        "tasks",
        nargs="*",
        help="Specific tasks to run (includes their dependencies)",
    )
    p.add_argument(
        "--only",
        nargs="*",
        default=[],
        help="Run only these tasks without dependencies",
    )
    p.add_argument(
        "--exclude", "-x",
        nargs="*",
        default=[],
        help="Tasks to exclude from execution",
    )
    p.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Print execution plan without running anything",
    )
    p.add_argument(
        "--resume", "-r",
        action="store_true",
        help="Resume from last state, skipping successful tasks",
    )
    p.add_argument(
        "--reset",
        action="store_true",
        help="Clear state before running (ignore previous results)",
    )
    p.add_argument(
        "--continue", "-k",
        dest="continue_on_error",
        action="store_true",
        help="Continue running even if some tasks fail (overrides config)",
    )
    p.add_argument(
        "--jobs", "-j",
        type=int,
        default=None,
        help="Maximum parallel jobs (overrides config)",
    )
    p.add_argument(
        "--log-file",
        default=None,
        help="Path to log file (overrides config)",
    )
    p.add_argument(
        "--no-webhook",
        action="store_true",
        help="Disable webhook notification",
    )
    p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        workflow = load_config(args.config)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 1

    if args.jobs is not None:
        workflow.max_concurrency = args.jobs
    if args.continue_on_error:
        workflow.failure_strategy = FailureStrategy.CONTINUE
    log_file = args.log_file or workflow.log_file

    try:
        graph = TaskGraph(workflow.tasks)
    except KeyError as e:
        print(f"Dependency error: {e}", file=sys.stderr)
        return 1

    cycle = graph.detect_cycle()
    if cycle:
        print(f"Cyclic dependency: {' -> '.join(cycle)}", file=sys.stderr)
        return 1

    try:
        include_tasks = args.tasks + args.only if args.only else args.tasks
        selected = graph.resolve_selected_tasks(
            include=include_tasks if include_tasks else None,
            exclude=args.exclude,
        )
    except KeyError as e:
        print(f"Task error: {e}", file=sys.stderr)
        return 1

    logger = Logger(log_file=log_file, verbose=True)

    layers = graph.get_execution_layers(selected)

    if args.dry_run:
        logger.info(f"Config: {args.config}")
        logger.info(f"Selected tasks: {', '.join(sorted(selected))}")
        logger.info(f"Max concurrency: {workflow.max_concurrency}")
        logger.print_plan(layers)
        return 0

    state = StateStore(workflow.state_file)
    if args.reset:
        state.reset()

    t_start = time.time()

    executor = TaskExecutor(
        workflow=workflow,
        graph=graph,
        state=state,
        logger=logger,
        selected=selected,
        resume=args.resume,
    )

    try:
        results = asyncio.run(executor.run())
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        results = executor.results
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        return 1

    total_duration = time.time() - t_start
    logger.print_summary(results, total_duration)

    if workflow.webhook_url and not args.no_webhook:
        send_webhook(workflow.webhook_url, results, total_duration, logger)

    failed = any(r.status == TaskStatus.FAILED for r in results.values())
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
