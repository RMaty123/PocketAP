"""Centrální logování PocketAP: soubor, konzole a fronta pro GUI.

Hesla se do logu nikdy nezapisují. Volající moduly je do zpráv nevkládají.
"""
from __future__ import annotations

import logging
import queue
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_FILE = LOG_DIR / "pocketap.log"
ROOT_LOGGER_NAME = "pocketap"

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class QueueLogHandler(logging.Handler):
    """Předává záznamy do fronty, kterou bezpečně čte vlákno Tkinteru."""

    def __init__(self, target: "queue.Queue[logging.LogRecord]") -> None:
        super().__init__(level=logging.INFO)
        self._target = target

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._target.put_nowait(record)
        except queue.Full:  # GUI nestíhá, log v souboru zůstává úplný.
            pass


def setup_logging(verbose: bool = False) -> "queue.Queue[logging.LogRecord]":
    """Nastaví logování a vrátí frontu záznamů pro GUI."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger(ROOT_LOGGER_NAME)
    root.setLevel(logging.DEBUG)
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_FORMAT)
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if sys.stderr is not None:
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(logging.DEBUG if verbose else logging.INFO)
        console.setFormatter(formatter)
        root.addHandler(console)

    records: "queue.Queue[logging.LogRecord]" = queue.Queue(maxsize=5000)
    root.addHandler(QueueLogHandler(records))
    return records


def get_logger(name: str) -> logging.Logger:
    """Vrátí podřízený logger aplikace."""
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")
