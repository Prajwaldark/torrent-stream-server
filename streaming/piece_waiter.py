"""
streaming/piece_waiter.py — Condition-variable wakeup for HTTP threads.

The HTTP request threads need to block until a specific torrent piece has
been downloaded. Polling ``handle.have_piece(idx)`` in a tight loop wastes
CPU and adds latency; instead, we connect ``TorrentWorker.piece_finished``
to :meth:`PieceWaiter.piece_done`, and request threads wait on a single
``threading.Condition`` until they're woken by a relevant piece arrival.

We still call libtorrent's ``have_piece`` on every wake-up — it is the
source of truth. The condition is just a hint that *something* finished;
the predicate decides whether THIS waiter can proceed.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

log = logging.getLogger("STREAM")


class PieceWaiter:
    """Wake-up primitive shared by all HTTP request threads."""

    def __init__(self) -> None:
        self._cond = threading.Condition()

    def piece_done(self, _index: int) -> None:
        """
        Called from the torrent thread whenever a piece finishes.

        We wake ALL waiters and let each re-evaluate its own predicate —
        cheap, because the number of in-flight HTTP threads is tiny
        (MPV opens 1-2 sockets) and ``have_piece`` is O(1).
        """
        with self._cond:
            self._cond.notify_all()

    def wait_for(
        self,
        predicate: Callable[[], bool],
        timeout: float = 60.0,
    ) -> bool:
        """
        Block until *predicate* returns True or *timeout* elapses.

        Returns True if the predicate became satisfied, False on timeout.
        Re-checks the predicate after every wake to handle spurious
        wakeups and to verify against libtorrent's authoritative state.
        """
        if predicate():
            return True
        with self._cond:
            remaining = timeout
            while not predicate():
                if remaining <= 0:
                    return False
                # Wait in 1-second slices so we can also re-poll the
                # predicate periodically — guards against missed
                # notifications between predicate eval and wait().
                self._cond.wait(timeout=min(remaining, 1.0))
                remaining -= 1.0
        return True

    def notify_all(self) -> None:
        """Force-wake every waiter — used on shutdown to unblock threads."""
        with self._cond:
            self._cond.notify_all()
