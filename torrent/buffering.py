"""
torrent/buffering.py — Buffer readiness monitor.

Given a torrent handle and the byte-range of the selected file,
calculates how much of the initial buffer window has been downloaded
from the START of the file. Pure sequential strategy: download front
to back, start playing once the head buffer is full.

Uses the torrent's ``total_wanted_done`` (bytes downloaded for files with
non-zero priority) for sub-piece accuracy.  After ``select_file()`` only
the chosen video file has priority > 0, so ``total_wanted_done`` == bytes
downloaded of that file.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("TORRENT")


class BufferMonitor:
    """
    Tracks whether enough data is available at the start of a file to
    begin playback.

    Parameters
    ----------
    buffer_bytes:
        Number of bytes from the beginning of the file that must be
        present before ``is_ready()`` returns True.
    """

    def __init__(self, buffer_bytes: int = 64 * 1024 * 1024) -> None:
        self._buffer_bytes = buffer_bytes
        self._handle = None          # libtorrent torrent_handle
        self._file_index: int = 0
        self._file_offset: int = 0   # byte offset of file inside torrent
        self._file_size: int = 0
        self._target: int = 0        # target bytes to buffer before play

    # ------------------------------------------------------------------ #
    #  Setup                                                               #
    # ------------------------------------------------------------------ #

    def attach(self, handle, torrent_info, file_index: int) -> None:
        """Bind this monitor to a specific file in the torrent."""
        self._handle = handle
        self._file_index = file_index

        storage = torrent_info.files()
        self._file_offset = storage.file_offset(file_index)
        self._file_size = storage.file_size(file_index)

        self._target = min(self._buffer_bytes, self._file_size)

        log.info(
            "Buffer monitor: file=%d  target=%.1f MB  file_size=%.1f MB",
            file_index,
            self._target / (1024 * 1024),
            self._file_size / (1024 * 1024),
        )

    def detach(self) -> None:
        self._handle = None

    # ------------------------------------------------------------------ #
    #  Status queries                                                      #
    # ------------------------------------------------------------------ #

    def is_ready(self) -> bool:
        """True once the head buffer of the file has been downloaded."""
        if self._handle is None or self._target == 0:
            return False
        return self._file_bytes_downloaded() >= self._target

    def buffer_percent(self) -> float:
        """Download progress of the buffer window, 0.0–100.0."""
        if self._handle is None or self._target == 0:
            return 0.0
        pct = (self._file_bytes_downloaded() / self._target) * 100.0
        return min(pct, 100.0)

    def buffer_bytes_downloaded(self) -> int:
        """Approximate number of bytes downloaded in the buffer window."""
        if self._handle is None:
            return 0
        return int(self._file_bytes_downloaded())

    # ------------------------------------------------------------------ #
    #  Internals                                                           #
    # ------------------------------------------------------------------ #

    def _file_bytes_downloaded(self) -> int:
        """
        Return how many bytes of the selected file have been downloaded.

        After :meth:`~torrent.session.TorrentWorker.select_file` all other
        files have priority 0, so ``total_wanted_done`` reflects only the
        chosen file's progress — even for partially-downloaded pieces.
        """
        if self._handle is None:
            return 0
        try:
            s = self._handle.status()
            return max(0, int(s.total_wanted_done))
        except Exception as exc:
            log.debug("Buffer progress error: %s", exc)
            return 0
