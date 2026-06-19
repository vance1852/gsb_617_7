"""Logging utilities: console + file with consistent format."""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional


def setup_logger(
    log_file: Optional[Path] = None,
    level: int = logging.INFO,
    name: str = "mercury",
) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)
    # Remove any existing handlers (so re-setup is idempotent)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    logger.propagate = False

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(log_file), encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    return logger


def get_logger(name: str = "mercury") -> logging.Logger:
    return logging.getLogger(name)
