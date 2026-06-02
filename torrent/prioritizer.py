"""
torrent/prioritizer.py — Strict streaming-queue piece priorities.

A torrent client normally tries to spread bandwidth across whatever
pieces are rarest/easiest. That is the opposite of what a video
streamer wants. This module forces libtorrent to behave like a
streaming queue: only the pieces immediately ahead of the playhead
are downloaded; everything else is parked at lowest priority until
the window slides past it.

The window is **byte-based**, not time-based — bitrate estimates
(especially before MPV reports the duration) are unreliable, but
"the next 64 MB of file bytes" is always a meaningful unit.

    [ skipped/low ] [ CRITICAL 64 MB ] [ PREFETCH 192 MB ] [ low ]
                   ^
                   playhead (byte offset within the file)
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Optional

log = logging.getLogger("TORRENT")

# libtorrent piece priority constants
PRIO_SKIP = 0       # do not download (used for pieces in OTHER files)
PRIO_LOW = 1        # download only if nothing better to do
PRIO_MID = 3        # mid-range — keeps the request pool large enough for
                    # libtorrent to maintain throughput on fast peers
PRIO_PREFETCH = 6   # next in line after the critical window
PRIO_CRITICAL = 7   # must arrive ASAP — the demuxer is about to read this

PHASE_STARTUP = "startup"
PHASE_PLAYBACK = "playback"

# Startup phase: aggressively focus the first few pieces so playback can
# begin quickly, even on torrents with very large pieces.
STARTUP_CRITICAL_WINDOW_BYTES = 24 * 1024 * 1024
STARTUP_PREFETCH_WINDOW_BYTES = 16 * 1024 * 1024
STARTUP_MID_WINDOW_BYTES = 32 * 1024 * 1024
STARTUP_TAIL_PIECES = 2
STARTUP_TAIL_PRIORITY = PRIO_PREFETCH
STARTUP_DEADLINE_STEP_MS = 150

# Playback phase: broaden the queue once streaming has started so the
# reader has more runway and the swarm can maintain throughput.
PLAYBACK_CRITICAL_WINDOW_BYTES = 32 * 1024 * 1024
PLAYBACK_PREFETCH_WINDOW_BYTES = 96 * 1024 * 1024
PLAYBACK_MID_WINDOW_BYTES = 384 * 1024 * 1024
PLAYBACK_TAIL_PIECES = 8
PLAYBACK_TAIL_PRIORITY = PRIO_CRITICAL
PLAYBACK_DEADLINE_STEP_MS = 350


class SeekPrioritizer:
    """
    Maintains a strict moving playback window of piece priorities.

    Call :meth:`attach` once after a file is selected, then call
    :meth:`on_seek` whenever the playhead moves (initial buffer,
    periodic tick during playback, user seek).
    """

    def __init__(self) -> None:
        self._handle = None
        self._torrent_info = None
        self._file_index: int = 0
        self._file_offset: int = 0
        self._file_size: int = 0
        self._piece_size: int = 0
        self._total_pieces: int = 0
        self._first_piece: int = 0
        self._last_piece: int = 0
        self._bitrate: float = 0.0  # bytes per second; only used to map secs→bytes
        # Previous window boundaries — used for delta updates so we don't
        # rebuild the full priority map every tick (which churns the
        # picker and resets peer request queues).
        self._prev_now: Optional[int] = None
        self._prev_crit_end: Optional[int] = None
        self._prev_pre_end: Optional[int] = None
        self._prev_mid_end: Optional[int] = None
        self._deadline_usage_logged = False
        self._phase = PHASE_STARTUP

    # ------------------------------------------------------------------ #
    #  Setup                                                               #
    # ------------------------------------------------------------------ #

    def attach(
        self,
        handle,
        torrent_info,
        file_index: int,
        duration_secs: float = 0.0,
    ) -> None:
        """Bind prioritizer to a specific file in the torrent."""
        self._handle = handle
        self._torrent_info = torrent_info
        self._file_index = file_index

        storage = torrent_info.files()
        self._file_offset = storage.file_offset(file_index)
        self._file_size = storage.file_size(file_index)
        self._piece_size = torrent_info.piece_length()
        self._total_pieces = torrent_info.num_pieces()

        self._first_piece = self._file_offset // self._piece_size
        last_byte = self._file_offset + self._file_size - 1
        self._last_piece = min(
            last_byte // self._piece_size, self._total_pieces - 1
        )

        # Reset delta state — next _apply_window call will publish a
        # full baseline before delta updates take over.
        self._prev_now = None
        self._prev_crit_end = None
        self._prev_pre_end = None
        self._prev_mid_end = None
        self._phase = PHASE_STARTUP

        if duration_secs > 0:
            self._bitrate = self._file_size / duration_secs
        else:
            # Used only to convert seek-time to byte offset before MPV
            # has reported a real duration. Wrong by ~2× is fine.
            self._bitrate = 4 * 1024 * 1024

        log.debug(
            "Prioritizer attached: file=%d  pieces %d–%d  piece_size=%d KB  "
            "startup_crit=%d pcs  startup_prefetch=%d pcs  startup_mid=%d pcs",
            file_index, self._first_piece, self._last_piece,
            self._piece_size // 1024,
            max(1, STARTUP_CRITICAL_WINDOW_BYTES // self._piece_size),
            max(1, STARTUP_PREFETCH_WINDOW_BYTES // self._piece_size),
            max(1, STARTUP_MID_WINDOW_BYTES // self._piece_size),
        )
        if not self._deadline_usage_logged:
            log.debug(
                "[SCHED] set_piece_deadline is active for startup-critical pieces when supported"
            )
            self._deadline_usage_logged = True

        self.enter_startup_phase(0)

    def update_duration(self, duration_secs: float) -> None:
        """Update the bitrate estimate once MPV reports the video duration."""
        if duration_secs > 0 and self._file_size > 0:
            self._bitrate = self._file_size / duration_secs

    def detach(self) -> None:
        self._handle = None
        self._torrent_info = None
        self._prev_now = None
        self._prev_crit_end = None
        self._prev_pre_end = None
        self._prev_mid_end = None
        self._phase = PHASE_STARTUP

    def enter_startup_phase(self, byte_in_file: int = 0) -> None:
        """Collapse priorities around the startup window for fast first frame."""
        self._set_phase(PHASE_STARTUP, byte_in_file)

    def enter_playback_phase(self, byte_in_file: int = 0) -> None:
        """Expand the queue after startup so playback has more runway."""
        self._set_phase(PHASE_PLAYBACK, byte_in_file)

    # ------------------------------------------------------------------ #
    #  Window application                                                  #
    # ------------------------------------------------------------------ #

    def on_seek(self, position_secs: float) -> None:
        """
        Rebuild piece priorities around *position_secs*.

        Called for the initial buffer (position 0), periodically during
        playback, and on every user seek.
        """
        if self._handle is None or self._bitrate <= 0:
            return
        byte_in_file = max(0, int(position_secs * self._bitrate))
        try:
            self._apply_window(byte_in_file)
        except Exception as exc:
            log.warning("Prioritizer error: %s", exc)

    def on_seek_bytes(self, byte_in_file: int) -> None:
        """Same as :meth:`on_seek` but takes a byte offset directly."""
        if self._handle is None:
            return
        try:
            self._apply_window(max(0, byte_in_file))
        except Exception as exc:
            log.warning("Prioritizer error: %s", exc)

    def _set_phase(self, phase: str, byte_in_file: int) -> None:
        if self._handle is None:
            return
        if phase not in (PHASE_STARTUP, PHASE_PLAYBACK):
            raise ValueError(f"Unknown scheduler phase: {phase}")
        self._phase = phase
        # Phase changes alter the baseline priority map, so publish a full
        # reset on the next update instead of a narrow delta.
        self._prev_now = None
        self._prev_crit_end = None
        self._prev_pre_end = None
        self._prev_mid_end = None
        self.on_seek_bytes(byte_in_file)

    # ------------------------------------------------------------------ #
    #  Internals                                                           #
    # ------------------------------------------------------------------ #

    def _apply_window(self, byte_in_file: int) -> None:
        """
        Slide the priority window forward using DELTA updates.

        First call after :meth:`attach`: publish a full baseline via
        ``prioritize_pieces`` — all non-selected files at SKIP, the
        selected file at LOW with the playback window boosted on top.

        Subsequent calls: only the pieces whose tier actually changed
        between the previous and new window boundaries get a per-piece
        ``piece_priority`` update. Everything else is left untouched,
        so libtorrent's picker and peer request queues stay stable.
        """
        # Clamp byte offset to within the file
        byte_in_file = min(byte_in_file, max(0, self._file_size - 1))
        abs_pos = self._file_offset + byte_in_file

        piece_now = max(
            self._first_piece,
            min(abs_pos // self._piece_size, self._last_piece),
        )
        critical_bytes, prefetch_bytes, mid_bytes = self._window_bytes()
        piece_crit_end = min(
            (abs_pos + max(0, critical_bytes - 1)) // self._piece_size,
            self._last_piece,
        )
        piece_pre_end = min(
            (abs_pos + max(0, critical_bytes + prefetch_bytes - 1))
                // self._piece_size,
            self._last_piece,
        )
        piece_mid_end = min(
            (abs_pos + max(0, critical_bytes + prefetch_bytes + mid_bytes - 1))
                // self._piece_size,
            self._last_piece,
        )

        if self._prev_now is None:
            # First call — publish the full baseline once.
            self._publish_baseline(
                piece_now, piece_crit_end, piece_pre_end, piece_mid_end,
            )
        else:
            # Subsequent calls — delta only.
            changed = self._publish_delta(
                piece_now, piece_crit_end, piece_pre_end, piece_mid_end,
            )
            log.debug(
                "Delta reprio: phase=%s head=piece %d changed=%d pcs "
                "crit=%d-%d prefetch=%d-%d mid=%d-%d",
                self._phase,
                piece_now, changed,
                piece_now, piece_crit_end,
                piece_crit_end + 1, piece_pre_end,
                piece_pre_end + 1, piece_mid_end,
            )

        # Remember new boundaries for next delta
        self._prev_now = piece_now
        self._prev_crit_end = piece_crit_end
        self._prev_pre_end = piece_pre_end
        self._prev_mid_end = piece_mid_end

    def _publish_baseline(
        self,
        piece_now: int,
        piece_crit_end: int,
        piece_pre_end: int,
        piece_mid_end: int,
    ) -> None:
        """Publish the full priority map once (first call after attach)."""
        priorities = [PRIO_SKIP] * self._total_pieces
        for p in range(self._first_piece, self._last_piece + 1):
            priorities[p] = self._base_priority()
        for p in range(piece_pre_end + 1, piece_mid_end + 1):
            priorities[p] = PRIO_MID
        for p in range(piece_crit_end + 1, piece_pre_end + 1):
            priorities[p] = PRIO_PREFETCH
        for p in range(piece_now, piece_crit_end + 1):
            priorities[p] = PRIO_CRITICAL
        log.debug(
            "[SCHED] Calling prioritize_pieces for baseline: total=%d selected_range=%d-%d",
            len(priorities),
            self._first_piece,
            self._last_piece,
        )
        self._handle.prioritize_pieces(priorities)
        self._arm_deadline_window(piece_now, piece_crit_end)

        # Always boost the tail of the file — MKV/MP4 containers store
        # their seek index (Cues / moov atom) at the end. MPV probes
        # these immediately on open, before any playhead-driven window
        # can reach them.
        tail_pieces, tail_priority = self._tail_policy()
        tail_start = max(self._first_piece, self._last_piece - tail_pieces + 1)
        for p in range(tail_start, self._last_piece + 1):
            try:
                log.debug(
                    "[SCHED] Calling piece_priority(piece=%d, priority=%d) for tail boost",
                    p,
                    tail_priority,
                )
                self._handle.piece_priority(p, tail_priority)
                if tail_priority == PRIO_CRITICAL:
                    self._arm_deadline(p)
            except Exception:
                pass

        self._log_initial_priority_diagnostics(
            piece_now,
            piece_crit_end,
            piece_pre_end,
            piece_mid_end,
        )

        log.debug(
            "Baseline published: phase=%s head=piece %d crit=%d-%d "
            "prefetch=%d-%d mid=%d-%d",
            self._phase,
            piece_now,
            piece_now, piece_crit_end,
            piece_crit_end + 1, piece_pre_end,
            piece_pre_end + 1, piece_mid_end,
        )

    def _publish_delta(
        self,
        piece_now: int,
        piece_crit_end: int,
        piece_pre_end: int,
        piece_mid_end: int,
    ) -> int:
        """
        Apply per-piece priority changes only where the tier actually
        changed since the previous window. Returns the number of pieces
        whose priority was updated.
        """
        # Only pieces near a moving boundary can change tier — gather
        # them into a candidate set so we don't scan the whole torrent.
        candidates = set()
        for old, new in (
            (self._prev_now,      piece_now),
            (self._prev_crit_end, piece_crit_end),
            (self._prev_pre_end,  piece_pre_end),
            (self._prev_mid_end,  piece_mid_end),
        ):
            lo = max(self._first_piece, min(old, new))
            hi = min(self._last_piece, max(old, new))
            # Include the boundary piece on either side
            for p in range(lo, hi + 1):
                candidates.add(p)

        changed = 0
        for p in candidates:
            old_prio = self._tier_of(
                p,
                self._prev_now, self._prev_crit_end,
                self._prev_pre_end, self._prev_mid_end,
            )
            new_prio = self._tier_of(
                p,
                piece_now, piece_crit_end,
                piece_pre_end, piece_mid_end,
            )
            if old_prio != new_prio:
                try:
                    log.debug(
                        "[SCHED] Calling piece_priority(piece=%d, priority=%d)",
                        p,
                        new_prio,
                    )
                    self._handle.piece_priority(p, new_prio)
                    if new_prio == PRIO_CRITICAL:
                        self._arm_deadline(p)
                    changed += 1
                except Exception as exc:
                    log.debug("piece_priority(%d, %d) failed: %s",
                              p, new_prio, exc)
        return changed

    def _log_initial_priority_diagnostics(
        self,
        piece_now: int,
        piece_crit_end: int,
        piece_pre_end: int,
        piece_mid_end: int,
    ) -> None:
        if self._handle is None:
            return
        try:
            readback = list(self._handle.get_piece_priorities())
        except Exception as exc:
            log.warning("[SCHED] get_piece_priorities failed after baseline: %s", exc)
            return

        selected = readback[self._first_piece:self._last_piece + 1]
        counts = Counter(selected)
        log.debug(
            "[SCHED] Readback counts for selected file: skip=%d low=%d mid=%d prefetch=%d critical=%d",
            counts.get(PRIO_SKIP, 0),
            counts.get(PRIO_LOW, 0),
            counts.get(PRIO_MID, 0),
            counts.get(PRIO_PREFETCH, 0),
            counts.get(PRIO_CRITICAL, 0),
        )
        if selected and counts.get(PRIO_SKIP, 0) == len(selected):
            log.warning("[SCHED] All selected-file pieces read back as priority 0")

        sample_end = min(self._last_piece, self._first_piece + 7)
        for p in range(self._first_piece, sample_end + 1):
            log.debug("[SCHED] piece %d -> priority %d", p, readback[p])

        if readback:
            log.debug("[SCHED] torrent piece 0 -> priority %d", readback[0])

        log.debug(
            "[SCHED] Initial window applied: now=%d crit_end=%d pre_end=%d mid_end=%d",
            piece_now,
            piece_crit_end,
            piece_pre_end,
            piece_mid_end,
        )

    def _arm_deadline_window(self, start_piece: int, end_piece: int) -> None:
        deadline_step_ms = self._deadline_step_ms()
        for offset, piece in enumerate(range(start_piece, end_piece + 1)):
            self._arm_deadline(piece, offset * deadline_step_ms)

    def _arm_deadline(self, piece_index: int, deadline_ms: int = 0) -> None:
        if self._handle is None:
            return
        deadline_fn = getattr(self._handle, "set_piece_deadline", None)
        if deadline_fn is None:
            return
        try:
            deadline_fn(piece_index, max(0, int(deadline_ms)))
        except TypeError:
            try:
                deadline_fn(piece_index, max(0, int(deadline_ms)), 0)
            except Exception as exc:
                log.debug("set_piece_deadline(%d) failed: %s", piece_index, exc)
        except Exception as exc:
            log.debug("set_piece_deadline(%d) failed: %s", piece_index, exc)

    def _window_bytes(self) -> tuple[int, int, int]:
        if self._phase == PHASE_STARTUP:
            return (
                STARTUP_CRITICAL_WINDOW_BYTES,
                STARTUP_PREFETCH_WINDOW_BYTES,
                STARTUP_MID_WINDOW_BYTES,
            )
        return (
            PLAYBACK_CRITICAL_WINDOW_BYTES,
            PLAYBACK_PREFETCH_WINDOW_BYTES,
            PLAYBACK_MID_WINDOW_BYTES,
        )

    def _base_priority(self) -> int:
        if self._phase == PHASE_STARTUP:
            return PRIO_SKIP
        return PRIO_LOW

    def _tail_policy(self) -> tuple[int, int]:
        if self._phase == PHASE_STARTUP:
            return STARTUP_TAIL_PIECES, STARTUP_TAIL_PRIORITY
        return PLAYBACK_TAIL_PIECES, PLAYBACK_TAIL_PRIORITY

    def _deadline_step_ms(self) -> int:
        if self._phase == PHASE_STARTUP:
            return STARTUP_DEADLINE_STEP_MS
        return PLAYBACK_DEADLINE_STEP_MS

    def _tier_of(
        self,
        p: int,
        piece_now: int,
        piece_crit_end: int,
        piece_pre_end: int,
        piece_mid_end: int,
    ) -> int:
        """Resolve the priority a given piece should have under the
        supplied window boundaries. Pieces outside the selected file
        are always SKIP; pieces inside the file but outside every
        window tier are LOW (the stable sequential baseline)."""
        if p < self._first_piece or p > self._last_piece:
            return PRIO_SKIP
        if piece_now <= p <= piece_crit_end:
            return PRIO_CRITICAL
        if piece_crit_end < p <= piece_pre_end:
            return PRIO_PREFETCH
        if piece_pre_end < p <= piece_mid_end:
            return PRIO_MID
        return PRIO_LOW
