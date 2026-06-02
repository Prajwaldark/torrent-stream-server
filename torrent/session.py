"""
torrent/session.py — libtorrent session wrapper running in a QThread.

The TorrentWorker lives in a background QThread and drives the libtorrent
alert loop. It communicates with the UI exclusively through Qt signals.

Signals emitted (connect in MainWindow):
    metadata_ready(files)         — list[FileInfo], after info_hash resolved
    stats_updated(speed, peers, dl, total)
    piece_finished(index)
    error_occurred(message)
    torrent_finished()
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import List, Optional

try:
    import libtorrent as lt
except ImportError as exc:
    raise ImportError(
        "libtorrent not found. Install with:\n"
        "  pip install libtorrent\n"
        "or on some platforms:\n"
        "  pip install python-libtorrent"
    ) from exc

from PySide6.QtCore import QObject, QThread, Signal, Slot

from torrent.file_selector import FileInfo, detect_video_files
from utils.config import AppConfig

log = logging.getLogger("TORRENT")

# Alert categories we care about
_ALERT_MASK = (
    lt.alert.category_t.status_notification
    | lt.alert.category_t.progress_notification
    | lt.alert.category_t.piece_progress_notification
    | lt.alert.category_t.error_notification
)


class TorrentWorker(QObject):
    """Runs in a QThread. Never create UI objects here."""

    # Emitted when torrent metadata (file list) is available
    metadata_ready = Signal(list)           # list[FileInfo]

    # Periodic stats: download speed (B/s), peer count, downloaded (B), total (B)
    stats_updated = Signal(object, object, object, object)

    # A piece finished downloading
    piece_finished = Signal(int)

    # Human-readable error message
    error_occurred = Signal(str)

    # Torrent has finished downloading completely
    torrent_finished = Signal()

    def __init__(self, config: AppConfig, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._config = config
        self._session: Optional[lt.session] = None
        self._handle: Optional[lt.torrent_handle] = None
        self._torrent_info: Optional[lt.torrent_info] = None
        self._running = False
        import queue
        self._cmd_queue = queue.Queue()
        self._selected_file_index: int = -1
        self._selected_first_piece: int = -1
        self._selected_last_piece: int = -1
        self._startup_last_piece: int = -1
        self._startup_target_bytes: int = 0
        self._selection_started_at: float = 0.0
        self._last_piece_finished_at: float = 0.0
        self._pieces_finished_since_selection: int = 0
        self._last_scheduler_diag_at: float = 0.0
        self._last_stall_dump_at: float = 0.0
        self._download_queue_shape_logged = False
        self._download_queue_block_shape_logged = False

    # ------------------------------------------------------------------ #
    #  Public slots (called from UI thread via signals or direct call)    #
    # ------------------------------------------------------------------ #

    @Slot(str)
    def add_magnet(self, uri: str) -> None:
        """Parse and add a magnet link to the session."""
        self._cmd_queue.put(("add_magnet", uri))

    @Slot(str)
    def add_torrent_file(self, path: str) -> None:
        """Load a .torrent file and add it."""
        self._cmd_queue.put(("add_torrent", path))

    def _get_save_path(self) -> str:
        if self._config.save_path:
            # Ensure path exists
            from pathlib import Path
            Path(self._config.save_path).mkdir(parents=True, exist_ok=True)
            return self._config.save_path
        return str(self._config.cache_dir)

    def _do_add_magnet(self, uri: str) -> None:
        log.info("Adding magnet: %s", uri[:80])
        self._reset_scheduler_diagnostics()
        self._ensure_session()
        params = lt.parse_magnet_uri(uri)
        params.save_path = self._get_save_path()
        params.flags |= lt.torrent_flags.sequential_download
        self._handle = self._session.add_torrent(params)
        self._handle.set_sequential_download(True)
        log.info("Torrent added — waiting for metadata…")

    def _do_add_torrent_file(self, path: str) -> None:
        log.info("Adding torrent file: %s", path)
        self._reset_scheduler_diagnostics()
        self._ensure_session()
        info = lt.torrent_info(path)
        params = lt.add_torrent_params()
        params.ti = info
        params.save_path = self._get_save_path()
        params.flags |= lt.torrent_flags.sequential_download
        self._handle = self._session.add_torrent(params)
        self._handle.set_sequential_download(True)

        # .torrent files already have metadata
        self._torrent_info = info
        files = detect_video_files(info, self._get_save_path())
        self.metadata_ready.emit(files)

    @Slot(int)
    def select_file(self, file_index: int) -> None:
        """Focus the torrent on a single file."""
        self._cmd_queue.put(("select_file", file_index))

    def _do_select_file(self, file_index: int) -> None:
        if self._handle is None or self._torrent_info is None:
            return
        self._selected_file_index = file_index
        self._selection_started_at = time.monotonic()
        self._last_piece_finished_at = 0.0
        self._pieces_finished_since_selection = 0
        self._last_stall_dump_at = 0.0

        num_files = self._torrent_info.files().num_files()
        file_prios = [0] * num_files
        file_prios[file_index] = 7
        self._handle.prioritize_files(file_prios)
        self._handle.set_sequential_download(True)

        files = self._torrent_info.files()
        piece_length = self._torrent_info.piece_length()
        file_offset = files.file_offset(file_index)
        file_size = files.file_size(file_index)
        self._selected_first_piece = file_offset // piece_length
        last_byte = file_offset + file_size - 1
        self._selected_last_piece = min(
            last_byte // piece_length,
            self._torrent_info.num_pieces() - 1,
        )
        self._startup_target_bytes = min(self._config.startup_buffer_bytes, file_size)
        if self._startup_target_bytes > 0:
            startup_last_byte = file_offset + self._startup_target_bytes - 1
            self._startup_last_piece = min(
                startup_last_byte // piece_length,
                self._selected_last_piece,
            )
        else:
            self._startup_last_piece = self._selected_first_piece - 1
        sequential = False
        try:
            sequential = bool(self._handle.status().sequential_download)
        except Exception:
            pass

        log.info(
            "Selected file %d — other files skipped, sequential mode on",
            file_index,
        )
        log.debug(
            "[SCHED] File selected: index=%d offset=%d size=%d first_piece=%d last_piece=%d sequential=%s",
            file_index,
            file_offset,
            file_size,
            self._selected_first_piece,
            self._selected_last_piece,
            sequential,
        )
        log.debug(
            "[SCHED] File priorities applied: selected=%d others_zero=%s",
            file_prios[file_index],
            all(priority == 0 for idx, priority in enumerate(file_prios) if idx != file_index),
        )
        log.info(
            "[SCHED] Startup window: target=%.1fMB pieces=%d-%d piece_size=%.1fMB",
            self._startup_target_bytes / (1024 * 1024),
            self._selected_first_piece,
            self._startup_last_piece,
            piece_length / (1024 * 1024),
        )
        self._log_scheduler_state("after select_file", force_full=True)

    @Slot()
    def pause_download(self) -> None:
        self._cmd_queue.put(("pause",))

    def _do_pause_download(self) -> None:
        if self._handle:
            self._handle.pause()
            log.info("Torrent paused")

    @Slot()
    def resume_download(self) -> None:
        self._cmd_queue.put(("resume",))

    def _do_resume_download(self) -> None:
        if self._handle:
            self._handle.resume()
            log.info("Torrent resumed")

    @Slot()
    def remove_torrent(self) -> None:
        self._cmd_queue.put(("remove",))

    def _do_remove_torrent(self) -> None:
        if self._session and self._handle:
            try:
                self._session.remove_torrent(self._handle)
                log.info("Torrent removed from session")
            except Exception:
                pass
        self._handle = None
        self._torrent_info = None
        self._selected_file_index = -1
        self._selected_first_piece = -1
        self._selected_last_piece = -1
        self._reset_scheduler_diagnostics()

    def get_handle(self):
        return self._handle

    def get_torrent_info(self):
        return self._torrent_info

    def pause_torrent(self) -> None:
        """Pause downloading (keeps peers connected)."""
        if self._handle is not None:
            self._handle.pause()
            log.info("Torrent paused")

    def resume_torrent(self) -> None:
        """Resume downloading after a pause."""
        if self._handle is not None:
            self._handle.resume()
            log.info("Torrent resumed")

    def cancel(self) -> None:
        """Remove the current torrent from the session entirely."""
        if self._session and self._handle:
            try:
                self._session.remove_torrent(self._handle)
                log.info("Torrent removed from session")
            except Exception:
                pass
            self._handle = None
            self._torrent_info = None
            self._selected_file_index = -1
            self._selected_first_piece = -1
            self._selected_last_piece = -1
            self._reset_scheduler_diagnostics()

    @property
    def is_paused(self) -> bool:
        if self._handle is None:
            return False
        try:
            return bool(self._handle.status().paused)
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    #  Main loop — called by QThread.started signal                       #
    # ------------------------------------------------------------------ #

    @Slot()
    def run(self) -> None:
        """Alert processing loop. Runs until stop() is called."""
        self._running = True
        log.info("Torrent worker started")

        last_stat_emit = 0.0

        import queue
        while self._running:
            # Process command queue
            try:
                while not self._cmd_queue.empty():
                    cmd = self._cmd_queue.get_nowait()
                    if cmd[0] == "add_magnet":
                        self._do_add_magnet(cmd[1])
                    elif cmd[0] == "add_torrent":
                        self._do_add_torrent_file(cmd[1])
                    elif cmd[0] == "select_file":
                        self._do_select_file(cmd[1])
                    elif cmd[0] == "pause":
                        self._do_pause_download()
                    elif cmd[0] == "resume":
                        self._do_resume_download()
                    elif cmd[0] == "remove":
                        self._do_remove_torrent()
            except queue.Empty:
                pass

            if self._session:
                self._process_alerts()
                
                now = time.time()
                if now - last_stat_emit >= 0.5:
                    self._emit_stats()
                    last_stat_emit = now
                    
            time.sleep(0.1)  # 100 ms tick

        log.info("Torrent worker stopped")

    def stop(self) -> None:
        """Signal the loop to exit. Call from any thread."""
        self._running = False
        if self._session and self._handle:
            try:
                self._session.remove_torrent(self._handle)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    #  Internals                                                           #
    # ------------------------------------------------------------------ #

    def _ensure_session(self) -> None:
        if self._session is not None:
            return

        settings = {
            "alert_mask": _ALERT_MASK,
            "enable_dht": True,
            "enable_lsd": True,
            "enable_upnp": True,
            "enable_natpmp": True,
        }
        if self._config.connections_limit > 0:
            settings["connections_limit"] = self._config.connections_limit
        if self._config.max_download_rate > 0:
            settings["download_rate_limit"] = self._config.max_download_rate
        if self._config.max_upload_rate > 0:
            settings["upload_rate_limit"] = self._config.max_upload_rate

        self._session = lt.session(settings)
        log.info("libtorrent session created (v%s)", lt.version)

    def _process_alerts(self) -> None:
        alerts = self._session.pop_alerts()
        for alert in alerts:
            atype = type(alert).__name__
            log.debug("Alert: %s", atype)

            if isinstance(alert, lt.metadata_received_alert):
                self._on_metadata_received(alert)

            elif isinstance(alert, lt.piece_finished_alert):
                self._last_piece_finished_at = time.monotonic()
                self._pieces_finished_since_selection += 1
                log.debug("[SCHED] piece_finished_alert: piece=%d", alert.piece_index)
                self.piece_finished.emit(alert.piece_index)

            elif isinstance(alert, lt.torrent_finished_alert):
                log.info("Torrent download complete")
                self.torrent_finished.emit()

            elif isinstance(alert, lt.torrent_error_alert):
                msg = str(alert.error)
                log.error("Torrent error: %s", msg)
                self.error_occurred.emit(msg)

    def _on_metadata_received(self, alert) -> None:
        self._torrent_info = self._handle.get_torrent_info()
        if self._torrent_info is None:
            return
        log.info("Metadata received: %s", self._torrent_info.name())
        files = detect_video_files(
            self._torrent_info, self._get_save_path()
        )
        self.metadata_ready.emit(files)

    def _emit_stats(self) -> None:
        if self._handle is None:
            return
        try:
            s = self._handle.status()
            speed = int(s.download_rate)
            peers = s.num_peers
            downloaded = int(s.total_done)
            total = int(s.total_wanted)
            self.stats_updated.emit(speed, peers, downloaded, total)
            self._maybe_log_scheduler_diagnostics(s)
        except Exception:
            pass

    def _reset_scheduler_diagnostics(self) -> None:
        self._selected_file_index = -1
        self._selected_first_piece = -1
        self._selected_last_piece = -1
        self._startup_last_piece = -1
        self._startup_target_bytes = 0
        self._selection_started_at = 0.0
        self._last_piece_finished_at = 0.0
        self._pieces_finished_since_selection = 0
        self._last_scheduler_diag_at = 0.0
        self._last_stall_dump_at = 0.0
        self._download_queue_shape_logged = False
        self._download_queue_block_shape_logged = False

    def _maybe_log_scheduler_diagnostics(self, status) -> None:
        if self._handle is None:
            return
        now = time.monotonic()
        if self._selection_started_at > 0 and now - self._last_scheduler_diag_at >= 5.0:
            self._last_scheduler_diag_at = now
            self._log_scheduler_state("periodic")

        if (
            self._selection_started_at > 0
            and self._pieces_finished_since_selection == 0
            and now - self._selection_started_at >= 8.0
            and now - self._last_stall_dump_at >= 8.0
        ):
            self._last_stall_dump_at = now
            log.warning("[SCHED] No pieces completed within %.1fs after file selection", now - self._selection_started_at)
            self._log_scheduler_state("stall warning", force_full=True)

    def _log_scheduler_state(self, reason: str, force_full: bool = False) -> None:
        if self._handle is None:
            return
        try:
            status = self._handle.status()
        except Exception as exc:
            log.warning("[SCHED] Failed to read torrent status for %s: %s", reason, exc)
            return

        queue_summary = self._summarize_download_queue()
        piece_zero_state = self._piece_state_summary(0, queue_summary["piece_states"])
        head_piece = self._selected_first_piece if self._selected_first_piece >= 0 else 0
        startup_piece_state = self._piece_state_summary(head_piece, queue_summary["piece_states"])
        elapsed = max(0.001, time.monotonic() - self._selection_started_at) if self._selection_started_at > 0 else 0.0
        pieces_per_sec = self._pieces_finished_since_selection / elapsed if elapsed > 0 else 0.0

        log.debug(
            "[SCHED] %s: rate=%dB/s peers=%d active_requests=%d downloading_pieces=%d queued_pieces=%d sequential=%s total_wanted_done=%d startup_rate=%.2fpcs/s startup_completed=%d",
            reason,
            int(status.download_rate),
            int(status.num_peers),
            queue_summary["active_requests"],
            queue_summary["downloading_pieces"],
            queue_summary["queued_pieces"],
            bool(status.sequential_download),
            int(getattr(status, "total_wanted_done", 0)),
            pieces_per_sec,
            self._pieces_finished_since_selection,
        )
        log.debug("[SCHED] piece 0: %s", piece_zero_state)
        log.debug("[SCHED] startup piece %d: %s", head_piece, startup_piece_state)
        if self._selected_first_piece >= 0 and self._startup_last_piece >= self._selected_first_piece:
            log.debug(
                "[SCHED] startup window status: pieces=%d-%d completed=%d/%d",
                self._selected_first_piece,
                self._startup_last_piece,
                self._count_completed_startup_pieces(),
                self._startup_last_piece - self._selected_first_piece + 1,
            )

        if force_full and self._selected_first_piece >= 0 and self._selected_last_piece >= self._selected_first_piece:
            self._log_priority_readback(head_piece)

    def _log_priority_readback(self, head_piece: int) -> None:
        if self._handle is None:
            return
        try:
            priorities = list(self._handle.get_piece_priorities())
        except Exception as exc:
            log.warning("[SCHED] get_piece_priorities failed during diagnostics: %s", exc)
            return

        sample_end = min(self._selected_last_piece, self._selected_first_piece + 7)
        for piece in range(self._selected_first_piece, sample_end + 1):
            log.debug("[SCHED] readback piece %d -> priority %d", piece, priorities[piece])

        if self._selected_first_piece >= 0:
            zero_count = sum(
                1
                for piece in range(self._selected_first_piece, self._selected_last_piece + 1)
                if priorities[piece] == 0
            )
            log.debug(
                "[SCHED] selected-file zero-priority pieces=%d of %d",
                zero_count,
                self._selected_last_piece - self._selected_first_piece + 1,
            )
            if zero_count == (self._selected_last_piece - self._selected_first_piece + 1):
                log.warning("[SCHED] Selected file currently has priority 0 for every piece")

        if 0 <= head_piece < len(priorities):
            log.debug("[SCHED] readback startup piece %d priority=%d", head_piece, priorities[head_piece])
        if priorities:
            log.debug("[SCHED] readback torrent piece 0 priority=%d", priorities[0])

    def _piece_state_summary(self, piece_index: int, piece_states: dict[int, dict[str, int]]) -> str:
        if self._handle is None or self._torrent_info is None:
            return "unavailable"

        have_piece = False
        availability = -1
        priority = -1
        try:
            have_piece = bool(self._handle.have_piece(piece_index))
        except Exception:
            pass
        try:
            priority = int(self._handle.piece_priority(piece_index))
        except Exception:
            pass
        try:
            availability_map = self._handle.piece_availability()
            if piece_index < len(availability_map):
                availability = int(availability_map[piece_index])
        except Exception:
            pass

        queue_state = piece_states.get(piece_index, {})
        return (
            f"availability={availability} priority={priority} have={have_piece} "
            f"requested={queue_state.get('requested', 0)} "
            f"finished={queue_state.get('finished', 0)} "
            f"writing={queue_state.get('writing', 0)} "
            f"blocks={queue_state.get('blocks', 0)}"
        )

    def _summarize_download_queue(self) -> dict[str, object]:
        if self._handle is None:
            return {
                "active_requests": 0,
                "downloading_pieces": 0,
                "queued_pieces": 0,
                "piece_states": {},
            }

        try:
            queue = list(self._handle.get_download_queue())
        except Exception as exc:
            log.warning("[SCHED] get_download_queue failed: %s", exc)
            return {
                "active_requests": 0,
                "downloading_pieces": 0,
                "queued_pieces": 0,
                "piece_states": {},
            }

        piece_states: dict[int, dict[str, int]] = {}
        active_requests = 0
        if queue and not self._download_queue_shape_logged:
            self._download_queue_shape_logged = True
            log.debug(
                "[SCHED] download queue entry attrs: %s",
                [name for name in dir(queue[0]) if not name.startswith("_")],
            )

        for entry in queue:
            piece_index = self._safe_int_attr(entry, "piece_index", default=-1)
            if piece_index < 0:
                piece_index = self._safe_int_attr(entry, "index", default=-1)
            blocks = self._safe_attr(entry, "blocks", default=[])
            requested = self._safe_int_attr(entry, "requested", default=0)
            finished = self._safe_int_attr(entry, "finished", default=0)
            writing = self._safe_int_attr(entry, "writing", default=0)
            if blocks:
                if requested == 0:
                    requested = sum(1 for block in blocks if self._block_flag(block, "requested"))
                if finished == 0:
                    finished = sum(1 for block in blocks if self._block_flag(block, "finished"))
                if writing == 0:
                    writing = sum(1 for block in blocks if self._block_flag(block, "writing"))
                if (
                    self._download_queue_shape_logged
                    and blocks
                    and not getattr(self, "_download_queue_block_shape_logged", False)
                ):
                    self._download_queue_block_shape_logged = True
                    log.debug(
                        "[SCHED] download queue block attrs: %s",
                        [name for name in dir(blocks[0]) if not name.startswith("_")],
                    )
            active_requests += requested
            piece_states[piece_index] = {
                "requested": requested,
                "finished": finished,
                "writing": writing,
                "blocks": len(blocks) if blocks else 0,
            }

        return {
            "active_requests": active_requests,
            "downloading_pieces": len(piece_states),
            "queued_pieces": len(queue),
            "piece_states": piece_states,
        }

    @staticmethod
    def _safe_attr(obj, name: str, default=None):
        try:
            return getattr(obj, name)
        except Exception:
            return default

    def _safe_int_attr(self, obj, name: str, default: int = 0) -> int:
        value = self._safe_attr(obj, name, default)
        try:
            return int(value)
        except Exception:
            return default

    def _block_flag(self, block, name: str) -> bool:
        value = self._safe_attr(block, name, False)
        if isinstance(value, bool):
            return value
        try:
            return bool(int(value))
        except Exception:
            return False

    def _count_completed_startup_pieces(self) -> int:
        if self._handle is None or self._selected_first_piece < 0 or self._startup_last_piece < self._selected_first_piece:
            return 0
        completed = 0
        for piece in range(self._selected_first_piece, self._startup_last_piece + 1):
            try:
                if self._handle.have_piece(piece):
                    completed += 1
            except Exception:
                continue
        return completed
