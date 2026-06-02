"""
streaming/source.py — Torrent-backed byte source for the HTTP server.

``StreamSource`` is the bridge between a single selected video file in the
torrent and the HTTP layer. It is attached to a libtorrent handle, the
shared :class:`SeekPrioritizer` and a :class:`PieceWaiter`, and exposes
the byte-level reads the HTTP handler needs.

Invariant: every byte returned from :meth:`read_range` lies inside a piece
for which ``handle.have_piece(idx)`` is True at read time. We never read
past the verified frontier — instead, we block.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Iterator, Optional

log = logging.getLogger("STREAM")

# How long to block waiting for any single piece before giving up on the
# request. Past this point the swarm is probably dead.
DEFAULT_WAIT_TIMEOUT = 60.0

# Longer timeout for pieces near the end of the file. MKV/MP4 containers
# store seek index (Cues, moov atom) in the tail; MPV probes these before
# playback can start and they may not be in the priority window yet.
TAIL_WAIT_TIMEOUT = 120.0
TAIL_PIECE_COUNT = 8

# Largest chunk yielded back to the HTTP handler. We additionally cut at
# piece boundaries inside read_range, but this caps the upper size so we
# don't allocate a 4 MB read in one go.
MAX_CHUNK_BYTES = 256 * 1024


class StreamSource:
    """Per-file byte source backed by a libtorrent handle."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handle = None
        self._torrent_info = None
        self._prioritizer = None
        self._waiter = None
        self._file_index: int = 0
        self._file_path: str = ""
        self._file_offset: int = 0
        self._file_size: int = 0
        self._piece_size: int = 0
        self._first_piece: int = 0
        self._last_piece: int = 0
        self._closed = False

    # ------------------------------------------------------------------ #
    #  Setup / teardown                                                    #
    # ------------------------------------------------------------------ #

    def attach(
        self,
        handle,
        torrent_info,
        file_index: int,
        file_path: str,
        prioritizer,
        waiter,
    ) -> None:
        """Bind this source to a specific file in the torrent."""
        with self._lock:
            self._handle = handle
            self._torrent_info = torrent_info
            self._file_index = file_index
            self._file_path = file_path
            self._prioritizer = prioritizer
            self._waiter = waiter
            self._closed = False

            storage = torrent_info.files()
            self._file_offset = storage.file_offset(file_index)
            self._file_size = storage.file_size(file_index)
            self._piece_size = torrent_info.piece_length()

            self._first_piece = self._file_offset // self._piece_size
            last_byte = self._file_offset + self._file_size - 1
            self._last_piece = min(
                last_byte // self._piece_size,
                torrent_info.num_pieces() - 1,
            )

        log.info(
            "StreamSource attached: file=%d path=%s size=%.1f MB  "
            "pieces %d–%d  piece_size=%d KB",
            file_index, file_path,
            self._file_size / (1024 * 1024),
            self._first_piece, self._last_piece,
            self._piece_size // 1024,
        )

    def detach(self) -> None:
        with self._lock:
            self._closed = True
            self._handle = None
            self._torrent_info = None
            self._prioritizer = None
        # Wake any blocked HTTP threads so they can notice and exit
        if self._waiter is not None:
            self._waiter.notify_all()
            self._waiter = None

    # ------------------------------------------------------------------ #
    #  Geometry / queries                                                  #
    # ------------------------------------------------------------------ #

    @property
    def file_size(self) -> int:
        return self._file_size

    @property
    def file_path(self) -> str:
        return self._file_path

    def is_attached(self) -> bool:
        return self._handle is not None and not self._closed

    def piece_for_offset(self, byte_in_file: int) -> int:
        return (self._file_offset + byte_in_file) // self._piece_size

    def buffered_ranges(self) -> list[tuple[int, int]]:
        """
        Return coalesced byte ranges already backed by downloaded pieces.

        The ranges are inclusive and expressed relative to the selected
        file, not the torrent as a whole.
        """
        if not self.is_attached() or self._piece_size <= 0 or self._file_size <= 0:
            return []

        import time
        now = time.time()
        # Cache the heavy piece scan for 1 second to prevent UI freezes
        if getattr(self, "_last_ranges_time", 0) + 1.0 > now:
            return getattr(self, "_last_ranges", [])

        handle = self._handle
        first_piece = self._first_piece
        last_piece = self._last_piece
        piece_size = self._piece_size
        file_offset = self._file_offset
        file_size = self._file_size

        ranges: list[tuple[int, int]] = []
        start_piece: Optional[int] = None
        
        # libtorrent has a status().pieces attribute which is a bitfield
        # we can use that instead of calling have_piece thousands of times
        try:
            status = handle.status()
            pieces = status.pieces
        except Exception:
            pieces = None
            
        for piece_idx in range(first_piece, last_piece + 1):
            try:
                if pieces is not None:
                    have_piece = pieces[piece_idx]
                else:
                    have_piece = bool(handle.have_piece(piece_idx))
            except Exception:
                have_piece = False

            if have_piece:
                if start_piece is None:
                    start_piece = piece_idx
            else:
                if start_piece is not None:
                    ranges.append(
                        self._piece_range_to_bytes(
                            start_piece,
                            piece_idx - 1,
                            piece_size,
                            file_offset,
                            file_size,
                        )
                    )
                    start_piece = None

        if start_piece is not None:
            ranges.append(
                self._piece_range_to_bytes(
                    start_piece,
                    last_piece,
                    piece_size,
                    file_offset,
                    file_size,
                )
            )

        self._last_ranges = ranges
        self._last_ranges_time = now
        return ranges

    def have_byte(self, byte_in_file: int) -> bool:
        if not self.is_attached():
            return False
        if byte_in_file < 0 or byte_in_file >= self._file_size:
            return False
        try:
            return bool(self._handle.have_piece(
                self.piece_for_offset(byte_in_file)))
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    #  Priority nudge                                                      #
    # ------------------------------------------------------------------ #

    def notify_seek(self, byte_in_file: int) -> None:
        """
        Called by the HTTP server when MPV makes a non-contiguous Range
        request. Slides the priority window to cover the new offset so
        the critical pieces land in front of the demuxer.
        """
        if self._prioritizer is None:
            return
        try:
            self._prioritizer.on_seek_bytes(max(0, int(byte_in_file)))
        except Exception as exc:
            log.debug("notify_seek failed: %s", exc)

    # ------------------------------------------------------------------ #
    #  Blocking reads                                                      #
    # ------------------------------------------------------------------ #

    def _wait_for_piece(
        self,
        piece_idx: int,
        timeout: float = DEFAULT_WAIT_TIMEOUT,
    ) -> bool:
        """Block until the given piece is downloaded. Returns False on timeout."""
        if not self.is_attached():
            return False
        # Fast path
        try:
            if self._handle.have_piece(piece_idx):
                return True
        except Exception:
            return False

        # Boost priority for the offset that maps to this piece so it
        # actually gets fetched ahead of the rest of the window. Without
        # this, a forward Range request can sit in MID priority and the
        # critical window never reaches it.
        try:
            byte_in_file = max(
                0,
                piece_idx * self._piece_size - self._file_offset,
            )
            if self._prioritizer is not None:
                self._prioritizer.on_seek_bytes(byte_in_file)
        except Exception:
            pass

        def predicate() -> bool:
            if not self.is_attached():
                return True  # bail out, read_range will check again
            try:
                return bool(self._handle.have_piece(piece_idx))
            except Exception:
                return True

        if self._waiter is None:
            return False
        return self._waiter.wait_for(predicate, timeout=timeout)

    @staticmethod
    def _piece_range_to_bytes(
        start_piece: int,
        end_piece: int,
        piece_size: int,
        file_offset: int,
        file_size: int,
    ) -> tuple[int, int]:
        start = max(0, start_piece * piece_size - file_offset)
        end = min(file_size - 1, ((end_piece + 1) * piece_size - file_offset) - 1)
        return start, end

    def read_range(
        self,
        start: int,
        end: int,
        timeout: float = DEFAULT_WAIT_TIMEOUT,
    ) -> Iterator[bytes]:
        """
        Yield bytes ``[start, end]`` of the selected file, blocking
        whenever the underlying piece has not yet been downloaded.

        Chunks are cut at piece boundaries so we never read across a
        piece we don't fully have. Empty if the source is detached.
        """
        if not self.is_attached():
            return
        if start < 0 or end < start or start >= self._file_size:
            return
        end = min(end, self._file_size - 1)

        pos = start
        # One file handle per request — cheap, avoids cross-thread sharing
        try:
            fh = open(self._file_path, "rb")
        except OSError as exc:
            log.warning("Cannot open stream file %s: %s", self._file_path, exc)
            return

        try:
            while pos <= end and self.is_attached():
                piece_idx = self.piece_for_offset(pos)

                # Ensure the piece is downloaded — block if not
                # Use a longer timeout for tail pieces (container metadata)
                is_tail = (piece_idx >= self._last_piece - TAIL_PIECE_COUNT + 1)
                wait_timeout = TAIL_WAIT_TIMEOUT if is_tail else timeout
                if not self._wait_for_piece(piece_idx, timeout=wait_timeout):
                    if not self.is_attached():
                        return
                    log.warning(
                        "Piece %d not available within %.0fs — stalling stream and retrying",
                        piece_idx, wait_timeout,
                    )
                    time.sleep(0.25)
                    continue
                if not self.is_attached():
                    return

                # Bytes-in-file range covered by THIS piece
                piece_start_in_file = max(
                    0,
                    piece_idx * self._piece_size - self._file_offset,
                )
                piece_end_in_file = min(
                    self._file_size - 1,
                    piece_start_in_file + self._piece_size - 1,
                )
                # Clamp to the requested range
                chunk_end = min(end, piece_end_in_file)
                chunk_len = chunk_end - pos + 1

                # Read in MAX_CHUNK_BYTES slices for backpressure
                read_offset = pos
                remaining = chunk_len
                try:
                    fh.seek(read_offset)
                except OSError:
                    return
                while remaining > 0 and self.is_attached():
                    to_read = min(remaining, MAX_CHUNK_BYTES)
                    try:
                        data = fh.read(to_read)
                    except OSError as exc:
                        log.debug("read() failed at %d: %s", read_offset, exc)
                        return
                    if not data:
                        # libtorrent has the piece but the file backing it
                        # has not flushed yet. Keep the connection open and
                        # retry instead of terminating the stream.
                        time.sleep(0.1)
                        if not self.is_attached():
                            return
                        try:
                            fh.seek(read_offset)
                        except OSError:
                            return
                        continue
                    yield data
                    read_offset += len(data)
                    remaining -= len(data)

                pos = chunk_end + 1
        finally:
            try:
                fh.close()
            except OSError:
                pass
