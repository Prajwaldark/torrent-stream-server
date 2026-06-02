"""
cache/cleanup.py — Temporary download directory management.

The CacheManager creates the streaming cache folder, registers an
atexit hook so the folder is always cleaned up, and optionally lets
the user keep a completed download.
"""
from __future__ import annotations

import atexit
import logging
import shutil
from pathlib import Path

log = logging.getLogger("TORRENT")


class CacheManager:
    def __init__(self, cache_dir: str) -> None:
        self._root = Path(cache_dir)
        self._keep = False
        self._ensure_dir()
        atexit.register(self._atexit_cleanup)

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    @property
    def download_path(self) -> Path:
        """Directory where torrent files are saved."""
        return self._root

    def set_keep(self, keep: bool) -> None:
        """If *keep* is True the cache will NOT be deleted on exit."""
        self._keep = keep

    def cleanup(self, *, force: bool = False) -> None:
        """
        Delete the cache directory.

        Respects ``self._keep`` unless *force* is True.
        """
        if self._keep and not force:
            log.info("Cache kept at %s", self._root)
            return
        if self._root.exists():
            try:
                shutil.rmtree(self._root)
                log.info("Cache deleted: %s", self._root)
            except OSError as exc:
                log.warning("Could not delete cache %s: %s", self._root, exc)

    def ensure_fresh(self) -> None:
        """
        Wipe the cache directory and recreate it.

        Useful when starting a new streaming session to avoid stale data.
        """
        self.cleanup(force=True)
        self._ensure_dir()

    # ------------------------------------------------------------------ #
    #  Internals                                                           #
    # ------------------------------------------------------------------ #

    def _ensure_dir(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        log.debug("Cache directory: %s", self._root)

    def _atexit_cleanup(self) -> None:
        """Called automatically when the Python interpreter exits."""
        try:
            self.cleanup()
        except Exception:
            pass  # best-effort during shutdown
