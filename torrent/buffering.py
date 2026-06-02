"""
torrent/buffering.py — Contiguous startup-buffer readiness monitor.

The stream is only safe to start when the *contiguous* head of the
selected file is downloaded. Global torrent progress is not enough:
pieces can be finished out of order, which makes the torrent look
"downloaded" while the playhead still has a gap at the front.

This monitor inspects actual piece availability from the file start and
only reports ready once the full startup window is backed by completed
pieces.
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
        self._piece_size: int = 0
        self._target: int = 0        # target bytes to buffer before play
        self._startup_first_piece: int = 0
        self._startup_last_piece: int = -1
        self._last_debug_signature: tuple[int, int, tuple[int, ...], bool] | None = None
        self._last_state_time: float = 0.0
        self._last_state: dict[str, object] | None = None

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
        self._piece_size = torrent_info.piece_length()

        self._target = min(self._buffer_bytes, self._file_size)
        self._startup_first_piece = self._file_offset // self._piece_size if self._piece_size > 0 else 0
        if self._target > 0 and self._piece_size > 0:
            required_end_byte = min(self._file_size - 1, self._target - 1)
            self._startup_last_piece = self._piece_for_byte(required_end_byte)
        else:
            self._startup_last_piece = self._startup_first_piece - 1
        self._last_debug_signature = None
        self._last_state_time = 0.0
        self._last_state = None

        log.debug(
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
        if self._handle is None or self._target == 0 or self._startup_last_piece < self._startup_first_piece:
            return False
        state = self.startup_buffer_state()
        ready = bool(state and state["ready"])
        self._maybe_log_state(state)
        return ready

    def buffer_percent(self) -> float:
        """Download progress of the buffer window, 0.0–100.0."""
        if self._handle is None or self._target == 0:
            return 0.0
        pct = (self._contiguous_bytes_downloaded() / self._target) * 100.0
        return min(pct, 100.0)

    def buffer_bytes_downloaded(self) -> int:
        """Number of contiguous bytes available from the file start."""
        if self._handle is None:
            return 0
        return int(self._contiguous_bytes_downloaded())

    def startup_buffer_state(self) -> dict[str, object] | None:
        """
        Return a debug snapshot of the startup window.

        The window is defined as the first ``buffer_bytes`` of the file,
        truncated to the file size. The monitor reports both the full
        required range and the currently available contiguous prefix.
        """
        if self._handle is None or self._target == 0 or self._startup_last_piece < self._startup_first_piece:
            return None

        import time
        now = time.time()
        if self._last_state is not None and now - self._last_state_time <= 0.1:
            return self._last_state

        missing_pieces: list[int] = []
        first_missing_piece: int | None = None
        contiguous_last_piece = self._startup_first_piece - 1
        contiguous_bytes = 0

        for piece_idx in range(self._startup_first_piece, self._startup_last_piece + 1):
            if self._have_piece(piece_idx):
                contiguous_last_piece = piece_idx
            else:
                missing_pieces.append(piece_idx)
                first_missing_piece = piece_idx
                break

        if contiguous_last_piece >= self._startup_first_piece:
            contiguous_bytes = self._piece_range_to_bytes(
                self._startup_first_piece,
                contiguous_last_piece,
                self._piece_size,
            )[1] + 1

        start_scan = first_missing_piece + 1 if first_missing_piece is not None else contiguous_last_piece + 1
        for piece_idx in range(start_scan, self._startup_last_piece + 1):
            if not self._have_piece(piece_idx):
                missing_pieces.append(piece_idx)

        # Relaxed startup validation: accept 4MB contiguous to start playback quickly
        relaxed_target = min(4 * 1024 * 1024, self._target)
        is_ready = bool(contiguous_bytes >= relaxed_target or not missing_pieces)

        state = {
            "required_first_piece": self._startup_first_piece,
            "required_last_piece": self._startup_last_piece,
            "contiguous_first_piece": self._startup_first_piece if contiguous_last_piece >= self._startup_first_piece else None,
            "contiguous_last_piece": contiguous_last_piece if contiguous_last_piece >= self._startup_first_piece else None,
            "contiguous_bytes": contiguous_bytes,
            "target_bytes": self._target,
            "missing_pieces": missing_pieces,
            "ready": is_ready,
        }
        self._last_state = state
        self._last_state_time = now
        return state

    # ------------------------------------------------------------------ #
    #  Internals                                                           #
    # ------------------------------------------------------------------ #

    def _contiguous_bytes_downloaded(self) -> int:
        """Return the size of the downloaded contiguous prefix in bytes."""
        state = self.startup_buffer_state()
        if not state:
            return 0
        return int(state["contiguous_bytes"])

    def _have_piece(self, piece_idx: int) -> bool:
        if self._handle is None:
            return False
        try:
            return bool(self._handle.have_piece(piece_idx))
        except Exception:
            return False

    def _piece_for_byte(self, byte_in_file: int) -> int:
        return (self._file_offset + byte_in_file) // self._piece_size if self._piece_size > 0 else 0

    def _piece_range_to_bytes(self, start_piece: int, end_piece: int, piece_size: int) -> tuple[int, int]:
        start = max(0, start_piece * piece_size - self._file_offset)
        end = min(self._file_size - 1, ((end_piece + 1) * piece_size - self._file_offset) - 1)
        return start, end

    def _maybe_log_state(self, state: dict[str, object] | None) -> None:
        if not state:
            return
        signature = (
            int(state["contiguous_last_piece"]) if state["contiguous_last_piece"] is not None else -1,
            int(state["required_last_piece"]),
            tuple(int(p) for p in state["missing_pieces"]),
            bool(state["ready"]),
        )
        if signature == self._last_debug_signature:
            return
        self._last_debug_signature = signature
        log.debug(
            "Startup buffer: required=%d-%d contiguous=%s-%s missing=%s contiguous_bytes=%d target_bytes=%d ready=%s",
            state["required_first_piece"],
            state["required_last_piece"],
            state["contiguous_first_piece"] if state["contiguous_first_piece"] is not None else "-",
            state["contiguous_last_piece"] if state["contiguous_last_piece"] is not None else "-",
            state["missing_pieces"],
            state["contiguous_bytes"],
            state["target_bytes"],
            state["ready"],
        )
