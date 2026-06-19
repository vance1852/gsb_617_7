from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional


class ColoredFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[0m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[35m",
    }
    RESET = "\033[0m"

    def __init__(self, use_color: bool = True):
        super().__init__()
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if self.use_color and record.levelname in self.COLORS:
            color = self.COLORS[record.levelname]
            return f"{color}{msg}{self.RESET}"
        return msg


def setup_logging(
    log_file: Optional[str] = None,
    verbose: bool = False,
    quiet: bool = False,
    use_color: bool = True,
) -> logging.Logger:
    logger = logging.getLogger("saturn")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.handlers.clear()

    file_handler = None
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)

    console_level = logging.WARNING if quiet else (logging.DEBUG if verbose else logging.INFO)
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(console_level)
    if not use_color:
        console_handler.setFormatter(logging.Formatter("%(message)s"))
    else:
        console_handler.setFormatter(ColoredFormatter(use_color=sys.stderr.isatty()))
    logger.addHandler(console_handler)

    logger._event_file_handler = file_handler
    return logger


class EventLogger:
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.messages: list = []

    def log(self, task_name: str, message: str) -> None:
        ts = self._timestamp()
        if task_name:
            line = f"[{ts}] [{task_name}] {message}"
        else:
            line = f"[{ts}] {message}"
        print(line, file=sys.stderr, flush=True)
        file_handler = getattr(self.logger, "_event_file_handler", None)
        if file_handler:
            record = logging.LogRecord(
                name="saturn.event", level=logging.INFO, pathname="", lineno=0,
                msg=line, args=(), exc_info=None,
            )
            file_handler.emit(record)
        self.messages.append(line)

    def _timestamp(self) -> str:
        from datetime import datetime
        return datetime.now().strftime("%H:%M:%S")
