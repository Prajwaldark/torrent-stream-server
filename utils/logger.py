"""
utils/logger.py — Centralised logging setup.

Call `setup_logging()` once at startup. All modules then use
`logging.getLogger(__name__)` as usual.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


def _log_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        base = Path.home() / ".local" / "share"
    log_dir = base / "torrent-player"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "app.log"


class _ColorFormatter(logging.Formatter):
    """ANSI-coloured console formatter (Windows 10+ / all POSIX)."""

    COLORS = {
        logging.DEBUG:    "\033[36m",   # cyan
        logging.INFO:     "\033[32m",   # green
        logging.WARNING:  "\033[33m",   # yellow
        logging.ERROR:    "\033[31m",   # red
        logging.CRITICAL: "\033[35m",   # magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelno, "")
        record.levelname = f"{color}{record.levelname:<8}{self.RESET}"
        return super().format(record)


DEBUG_MODE = False


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logger with file + coloured console handlers."""
    root = logging.getLogger()
    if root.handlers:
        return  # already configured

    root.setLevel(level)

    fmt = "%(asctime)s  %(levelname)s  %(name)s — %(message)s"
    datefmt = "%H:%M:%S"

    # File handler — plain text, DEBUG+
    file_handler = logging.FileHandler(_log_path(), encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(fmt, datefmt))

    # Console handler — coloured, level depends on DEBUG_MODE
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG if DEBUG_MODE else logging.INFO)
    console_handler.setFormatter(_ColorFormatter(fmt, datefmt))

    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Quieten noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("pychromecast").setLevel(logging.WARNING)
    logging.getLogger("zeroconf").setLevel(logging.WARNING)
