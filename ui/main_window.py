"""
ui/main_window.py — Root application window for torrent streaming server.

Provides the UI to start streaming torrents, monitor progress, and access
the stream via external players or QR code.
"""
from __future__ import annotations

import functools
import logging
import traceback
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, List, Optional

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QImage, QKeySequence, QPalette, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
    QGroupBox,
)

import qrcode

from cache.cleanup import CacheManager
from streaming.http_server import StreamServer
from streaming.piece_waiter import PieceWaiter
from streaming.source import StreamSource
from torrent.buffering import BufferMonitor
from torrent.file_selector import FileInfo
from torrent.prioritizer import SeekPrioritizer
from torrent.session import TorrentWorker
from utils.config import AppConfig
from utils.external_player import launch_mpv, launch_vlc
from utils.network import get_lan_ip
from utils.cast import CastManager

log = logging.getLogger("UI")
_NO_CAST_DEVICE_TEXT = "Select a device"


def _ui_debug_handler(func: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except Exception:
            self._report_exception(f"[UI] {func.__name__} failed")
            return None

    return wrapper


class _DebugEmitter(QObject):
    message = Signal(str)


class _DebugLogHandler(logging.Handler):
    def __init__(self, emitter: _DebugEmitter) -> None:
        super().__init__(level=logging.DEBUG)
        self._emitter = emitter

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._emitter.message.emit(self.format(record))
        except Exception:
            print(traceback.format_exc())


def _fmt_speed(bps: float | int) -> str:
    bps = int(bps)
    if bps >= 1024 * 1024:
        return f"{bps / (1024*1024):.1f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f} KB/s"
    return f"{bps} B/s"


def _fmt_size(b: float | int) -> str:
    b = int(b)
    if b >= 1024 ** 3:
        return f"{b / (1024**3):.2f} GB"
    if b >= 1024 ** 2:
        return f"{b / (1024**2):.1f} MB"
    if b >= 1024:
        return f"{b / 1024:.0f} KB"
    return f"{b} B"


# ---------------------------------------------------------------------------
# File selection dialog
# ---------------------------------------------------------------------------

class _FileSelectorDialog(QDialog):
    def __init__(self, files: List[FileInfo], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select Video File")
        self.setModal(True)
        self.setMinimumWidth(480)
        self._files = files

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)

        title = QLabel("Multiple video files found — choose one to stream:")
        title.setStyleSheet("color: #ccc; font-size: 13px; margin-bottom: 8px;")
        layout.addWidget(title)

        self._list = QListWidget()
        self._list.setStyleSheet("""
            QListWidget {
                background: #1e1e1e;
                color: #ddd;
                border: 1px solid #333;
                border-radius: 4px;
                font-size: 13px;
            }
            QListWidget::item:selected {
                background: #1e4d8c;
            }
            QListWidget::item:hover {
                background: #2a2a2a;
            }
        """)
        for f in files:
            item = QListWidgetItem(f"  {f.name}  ({f.human_size()})")
            self._list.addItem(item)
        if files:
            self._list.setCurrentRow(0)
        self._list.itemDoubleClicked.connect(self.accept)
        layout.addWidget(self._list)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.setStyleSheet("QPushButton { color: #ddd; background: #2a2a2a; "
                           "border: 1px solid #444; border-radius: 4px; padding: 4px 16px; }")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def selected_file(self) -> Optional[FileInfo]:
        row = self._list.currentRow()
        if 0 <= row < len(self._files):
            return self._files[row]
        return None


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    cast_devices_changed = Signal(list)

    def __init__(self, config: AppConfig, cache: CacheManager) -> None:
        super().__init__()
        self._config = config
        self._cache = cache
        self._current_file: Optional[FileInfo] = None
        self._files: List[FileInfo] = []
        self._is_active = False  # True when a torrent session is running
        self._stream_ready = False
        self._debug_backlog: List[str] = []
        self._debug_emitter = _DebugEmitter()
        self._debug_emitter.message.connect(self._append_debug_message)
        self._ui_log_handler: Optional[_DebugLogHandler] = None
        self._lan_ip = get_lan_ip()
        self._cast_devices: dict[str, object] = {}
        self._selected_cast_device_name: Optional[str] = None
        self._selected_cast_device: Optional[object] = None

        # Sub-components
        self._buffer_monitor = BufferMonitor(config.buffer_bytes)
        self._prioritizer = SeekPrioritizer()

        # HTTP streaming layer
        self._piece_waiter = PieceWaiter()
        self._stream_source = StreamSource()
        self._stream_server: Optional[StreamServer] = None

        # Torrent worker in its own thread
        self._torrent_thread = QThread(self)
        self._torrent_worker = TorrentWorker(config)
        self._torrent_worker.moveToThread(self._torrent_thread)
        self._torrent_thread.started.connect(self._torrent_worker.run)

        # Buffer polling timer
        self._buffer_timer = QTimer(self)
        self._buffer_timer.setInterval(500)
        self._buffer_timer.timeout.connect(self._poll_buffer)

        # Cast Manager
        self._cast_manager = CastManager()
        self.cast_devices_changed.connect(self._update_cast_devices)

        self._build_ui()
        self._attach_debug_log_handler()
        self._flush_debug_backlog()
        self._connect_signals()
        self._apply_global_styles()
        self._sync_stream_debug_state()

        self.setWindowTitle("Torrent LAN Streaming Server")
        self.resize(700, 650)
        
        # Start device discovery
        self._cast_manager.start_discovery(self._on_cast_devices_changed)
        self._cast_manager.set_status_listener(self._on_cast_media_status)
        self._cast_manager.set_connection_listener(self._on_cast_connection_changed)

        self._cast_is_playing = False
        self._cast_duration = 0.0
        self._cast_connect_pending = False
        self._expected_disconnect = False
        log.info("LAN IP: %s", self._lan_ip)
        log.info("HTTP bind address: %s", self._stream_bind_address())
        self._sync_stream_debug_state()

    def _attach_debug_log_handler(self) -> None:
        if self._ui_log_handler is not None:
            return
        handler = _DebugLogHandler(self._debug_emitter)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s",
                "%H:%M:%S",
            )
        )
        logging.getLogger().addHandler(handler)
        self._ui_log_handler = handler

    @Slot(str)
    def _append_debug_message(self, message: str) -> None:
        if not hasattr(self, "_debug_console"):
            self._debug_backlog.append(message)
            return
        self._debug_console.appendPlainText(message)

    def _flush_debug_backlog(self) -> None:
        backlog = list(self._debug_backlog)
        self._debug_backlog.clear()
        for message in backlog:
            self._append_debug_message(message)

    def _debug_print(self, message: str, level: int = logging.INFO) -> None:
        if self._ui_log_handler is None:
            self._append_debug_message(message)
        log.log(level, message)

    def _report_exception(self, context: str) -> None:
        tb = traceback.format_exc().rstrip()
        if self._ui_log_handler is None:
            self._append_debug_message(context)
            self._append_debug_message(tb)
        logging.getLogger("ERROR").error("%s\n%s", context, tb)

    def _connect_button(
        self,
        button: QPushButton,
        button_name: str,
        handler: Callable[[], None],
        handler_name: Optional[str] = None,
    ) -> None:
        target_name = handler_name or getattr(handler, "__name__", repr(handler))
        self._debug_print(f"[UI] Connected button '{button_name}' -> {target_name}")

        def wrapped(*_args) -> None:
            self._debug_print(f"[UI] {button_name} clicked")
            try:
                handler()
            except Exception:
                self._report_exception(
                    f"[UI] Button '{button_name}' failed in {target_name}"
                )

        button.clicked.connect(wrapped)

    def _stream_bind_address(self) -> str:
        return "0.0.0.0" if self._config.bind_all_interfaces else "127.0.0.1"

    def _reset_cast_combo_rendering(self) -> None:
        pass



    def _has_valid_lan_ip(self) -> bool:
        return bool(self._lan_ip) and self._lan_ip != "127.0.0.1"

    def _stream_url_ready(self) -> bool:
        lan_url = getattr(self, "_lan_url_label", None)
        if lan_url is not None:
            url_text = lan_url.text()
            if url_text.startswith("http") and not (
                "127.0.0.1" in url_text or (self._lan_ip and self._lan_ip in url_text)
            ):
                return self._stream_ready
        return (
            self._stream_server is not None
            and self._stream_ready
            and self._has_valid_lan_ip()
            and lan_url is not None
            and lan_url.text().startswith("http://")
        )

    def _has_selected_cast_device(self) -> bool:
        return (
            bool(self._selected_cast_device_name)
            and self._selected_cast_device_name in self._cast_devices
            and self._selected_cast_device is not None
        )

    def _selected_cast_device_ip(self) -> str:
        host = getattr(self._selected_cast_device, "host", "")
        return host or "(none)"

    def _sync_stream_debug_state(self) -> None:
        ready = self._stream_url_ready()
        can_cast = ready and self._has_selected_cast_device()
        for button in (
            self._copy_loc_btn,
            self._copy_lan_btn,
            self._vlc_btn,
            self._mpv_btn,
        ):
            button.setEnabled(ready)
        self._cast_btn.setEnabled(can_cast)

        if not ready:
            self._stop_cast_btn.setEnabled(False)

        server_state = "RUNNING" if self._stream_server is not None else "STOPPED"
        port = self._stream_server.port if self._stream_server is not None else 0
        stream_url = self._lan_url_label.text()
        localhost_url = self._localhost_url_label.text()
        selected_name = self._selected_cast_device_name or "(none)"
        selected_ip = self._selected_cast_device_ip()
        self._startup_diag_text.setPlainText(
            "\n".join(
                [
                    f"LAN IP: {self._lan_ip}",
                    f"Bind address: {self._stream_bind_address()}",
                    f"Port: {port or '(pending)'}",
                    f"HTTP server state: {server_state}",
                    f"Localhost URL: {localhost_url}",
                    f"Stream URL: {stream_url}",
                    f"Selected Cast Device: {selected_name}",
                    f"Selected Cast IP: {selected_ip}",
                ]
            )
        )
        self._debug_print(
            f"[DIAG] state={server_state} bind={self._stream_bind_address()} "
            f"lan_ip={self._lan_ip} port={port} ready={ready} "
            f"selected_device={selected_name} cast_enabled={can_cast}",
            logging.DEBUG,
        )

    # ------------------------------------------------------------------ #
    #  UI construction                                                     #
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Top bar ──────────────────────────────────────────────────
        layout.addWidget(self._build_top_bar())

        # ── Main Content Area ────────────────────────────────────────
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(20, 20, 20, 20)
        content_layout.setSpacing(20)

        # ── Stats panel ──────────────────────────────────────────────
        content_layout.addWidget(self._build_stats_panel())

        # ── Cast Panel ───────────────────────────────────────────────
        self._cast_panel = QGroupBox("Google Cast / Android TV")
        self._cast_panel.setStyleSheet("QGroupBox { color: #aaa; border: 1px solid #333; border-radius: 8px; margin-top: 1ex; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px; font-weight: bold; }")
        cast_layout = QVBoxLayout(self._cast_panel)
        cast_layout.setContentsMargins(15, 20, 15, 15)
        cast_layout.setSpacing(10)
        
        # Device Selection Row
        cast_device_row = QHBoxLayout()
        cast_device_row.setSpacing(10)
        self._device_combo = QComboBox()
        self._device_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._reset_cast_combo_rendering()
        cast_device_row.addWidget(self._device_combo)
        
        self._refresh_cast_btn = QPushButton("↻")
        self._refresh_cast_btn.setToolTip("Refresh Devices")
        self._connect_button(self._refresh_cast_btn, "Refresh Cast", self._on_refresh_cast)
        self._refresh_cast_btn.setStyleSheet("background: #252525; color: #ddd; border: 1px solid #444; border-radius: 6px; padding: 6px 12px;")
        cast_device_row.addWidget(self._refresh_cast_btn)

        self._cast_btn = QPushButton("Connect")
        self._connect_button(self._cast_btn, "Cast", self._on_cast)
        self._cast_btn.setEnabled(False)
        self._cast_btn.setStyleSheet("background: #1e4d8c; color: #fff; font-weight: bold; border-radius: 6px; padding: 6px 16px;")
        cast_device_row.addWidget(self._cast_btn)

        self._stop_cast_btn = QPushButton("Disconnect")
        self._connect_button(self._stop_cast_btn, "Stop Cast", self._on_stop_cast)
        self._stop_cast_btn.setStyleSheet("background: #3c1e1e; color: #e84040; border: 1px solid #5a2020; border-radius: 6px; padding: 6px 16px;")
        self._stop_cast_btn.setEnabled(False)
        cast_device_row.addWidget(self._stop_cast_btn)


        
        cast_layout.addLayout(cast_device_row)
        
        # Cast Status Label
        self._cast_status_label = QLabel("Ready to connect")
        self._cast_status_label.setStyleSheet("color: #4caf50; font-size: 12px; font-style: italic;")
        cast_layout.addWidget(self._cast_status_label)
        
        # Playback Controls Row
        self._cast_controls_widget = QWidget()
        cast_controls_row = QHBoxLayout(self._cast_controls_widget)
        cast_controls_row.setContentsMargins(0, 5, 0, 0)
        cast_controls_row.setSpacing(10)
        
        self._cast_play_btn = QPushButton("▶")
        self._cast_play_btn.setFixedSize(36, 36)
        self._cast_play_btn.setStyleSheet("background: #1e90ff; color: #fff; font-size: 16px; border-radius: 18px;")
        self._connect_button(self._cast_play_btn, "Cast Play/Pause", self._on_cast_play_pause)
        cast_controls_row.addWidget(self._cast_play_btn)
        
        self._cast_slider = QSlider(Qt.Orientation.Horizontal)
        self._cast_slider.setRange(0, 1000)
        self._cast_slider.sliderReleased.connect(self._on_cast_seek)
        self._cast_slider.setStyleSheet("""
            QSlider::groove:horizontal { height: 6px; background: #333; border-radius: 3px; }
            QSlider::sub-page:horizontal { background: #1e90ff; border-radius: 3px; }
            QSlider::handle:horizontal { background: #fff; width: 14px; margin: -4px 0; border-radius: 7px; }
        """)
        cast_controls_row.addWidget(self._cast_slider)
        
        self._cast_time_label = QLabel("00:00 / 00:00")
        self._cast_time_label.setStyleSheet("color: #aaa; font-family: monospace;")
        cast_controls_row.addWidget(self._cast_time_label)
        
        self._cast_volume_slider = QSlider(Qt.Orientation.Horizontal)
        self._cast_volume_slider.setRange(0, 100)
        self._cast_volume_slider.setValue(100)
        self._cast_volume_slider.setFixedWidth(60)
        self._cast_volume_slider.sliderReleased.connect(self._on_cast_volume)
        self._cast_volume_slider.setToolTip("Volume")
        self._cast_volume_slider.setStyleSheet("""
            QSlider::groove:horizontal { height: 4px; background: #333; border-radius: 2px; }
            QSlider::sub-page:horizontal { background: #4caf50; border-radius: 2px; }
            QSlider::handle:horizontal { background: #fff; width: 10px; margin: -3px 0; border-radius: 5px; }
        """)
        cast_controls_row.addWidget(self._cast_volume_slider)

        cast_layout.addWidget(self._cast_controls_widget)
        self._cast_controls_widget.setVisible(False)

        # Cast Diagnostics Group
        self._cast_diag_group = QGroupBox("Cast Playback Diagnostics")
        self._cast_diag_group.setStyleSheet("""
            QGroupBox {
                color: #aaa;
                border: 1px solid #333;
                border-radius: 8px;
                margin-top: 1ex;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 3px;
                font-weight: bold;
            }
        """)
        diag_layout = QVBoxLayout(self._cast_diag_group)
        diag_layout.setContentsMargins(15, 15, 15, 15)
        self._cast_diag_text = QPlainTextEdit()
        self._cast_diag_text.setReadOnly(True)
        self._cast_diag_text.setMaximumHeight(140)
        self._cast_diag_text.setStyleSheet("""
            background-color: #151515;
            color: #4caf50;
            font-family: monospace;
            border: 1px solid #222;
            border-radius: 4px;
        """)
        diag_layout.addWidget(self._cast_diag_text)
        cast_layout.addWidget(self._cast_diag_group)
        self._cast_diag_group.setVisible(False)

        content_layout.addWidget(self._cast_panel)

        # ── Stream Info Panel ────────────────────────────────────────

        self._stream_panel = QWidget()
        stream_layout = QVBoxLayout(self._stream_panel)

        urls_layout = QHBoxLayout()
        urls_layout.setSpacing(15)

        loc_group = QGroupBox("Localhost URL (This Computer)")
        loc_layout = QHBoxLayout(loc_group)
        self._localhost_url_label = QLineEdit("Waiting for stream...")
        self._localhost_url_label.setReadOnly(True)
        loc_layout.addWidget(self._localhost_url_label)
        self._copy_loc_btn = QPushButton("Copy")
        self._connect_button(
            self._copy_loc_btn,
            "Copy Local URL",
            lambda: self._copy_url(self._localhost_url_label.text(), self._copy_loc_btn),
            "_copy_url(localhost)",
        )
        self._copy_loc_btn.setEnabled(False)
        loc_layout.addWidget(self._copy_loc_btn)
        urls_layout.addWidget(loc_group)

        lan_group = QGroupBox("LAN URL (Network Devices)")
        lan_layout = QHBoxLayout(lan_group)
        self._lan_url_label = QLineEdit("Waiting for stream...")
        self._lan_url_label.setReadOnly(True)
        lan_layout.addWidget(self._lan_url_label)
        self._copy_lan_btn = QPushButton("Copy")
        self._connect_button(
            self._copy_lan_btn,
            "Copy LAN URL",
            lambda: self._copy_url(self._lan_url_label.text(), self._copy_lan_btn),
            "_copy_url(lan)",
        )
        self._copy_lan_btn.setEnabled(False)
        lan_layout.addWidget(self._copy_lan_btn)
        urls_layout.addWidget(lan_group)

        stream_layout.addLayout(urls_layout)

        # QR Code and Actions
        qr_action_layout = QHBoxLayout()
        qr_action_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self._qr_label = QLabel()
        self._qr_label.setFixedSize(200, 200)
        self._qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_label.setStyleSheet("background: #fff; border-radius: 8px;")
        self._qr_label.setScaledContents(True)
        self._qr_label.setText("QR Code\n(Pending)")
        qr_action_layout.addWidget(self._qr_label)

        action_vbox = QVBoxLayout()
        action_vbox.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        action_vbox.setSpacing(10)

        self._vlc_btn = QPushButton("Open in VLC")
        self._vlc_btn.setMinimumHeight(40)
        self._connect_button(self._vlc_btn, "Open VLC", self._launch_vlc)
        self._vlc_btn.setEnabled(False)
        self._vlc_btn.setStyleSheet("background: #ff8800; color: #fff; font-weight: bold; border-radius: 6px; padding: 0 20px;")
        action_vbox.addWidget(self._vlc_btn)

        self._mpv_btn = QPushButton("Open in MPV")
        self._mpv_btn.setMinimumHeight(40)
        self._connect_button(self._mpv_btn, "Open MPV", self._launch_mpv)
        self._mpv_btn.setEnabled(False)
        self._mpv_btn.setStyleSheet("background: #6a1b9a; color: #fff; font-weight: bold; border-radius: 6px; padding: 0 20px;")
        action_vbox.addWidget(self._mpv_btn)

        qr_action_layout.addSpacing(30)
        qr_action_layout.addLayout(action_vbox)

        stream_layout.addLayout(qr_action_layout)

        self._stream_panel.setVisible(False)
        content_layout.addWidget(self._stream_panel)

        diag_group = QGroupBox("Startup Diagnostics")
        diag_layout = QVBoxLayout(diag_group)
        self._startup_diag_text = QPlainTextEdit()
        self._startup_diag_text.setReadOnly(True)
        self._startup_diag_text.setMaximumHeight(120)
        diag_layout.addWidget(self._startup_diag_text)
        content_layout.addWidget(diag_group)

        debug_group = QGroupBox("Debug Console")
        debug_layout = QVBoxLayout(debug_group)
        self._debug_console = QPlainTextEdit()
        self._debug_console.setReadOnly(True)
        self._debug_console.setMinimumHeight(180)
        debug_layout.addWidget(self._debug_console)
        content_layout.addWidget(debug_group)

        content_layout.addStretch(1)

        layout.addWidget(content)

    def _build_top_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("topBar")
        bar.setFixedHeight(56)
        h = QHBoxLayout(bar)
        h.setContentsMargins(12, 0, 12, 0)
        h.setSpacing(8)

        # App logo / label
        logo = QLabel("⚡ TorrentStream Server")
        logo.setObjectName("logoLabel")
        h.addWidget(logo)

        h.addSpacing(12)

        # Magnet / path input
        self._magnet_input = QLineEdit()
        self._magnet_input.setObjectName("magnetInput")
        self._magnet_input.setPlaceholderText(
            "Paste magnet link or .torrent path here…"
        )
        self._magnet_input.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        h.addWidget(self._magnet_input)

        # Open file button
        self._open_btn = QPushButton("📂 Open")
        self._open_btn.setObjectName("openBtn")
        self._open_btn.setFixedHeight(36)
        self._connect_button(self._open_btn, "Open Torrent", self._on_open_file)
        h.addWidget(self._open_btn)

        # Stream button
        self._stream_btn = QPushButton("▶ Start Server")
        self._stream_btn.setObjectName("streamBtn")
        self._stream_btn.setFixedHeight(36)
        self._connect_button(self._stream_btn, "Start Server", self._on_stream)
        h.addWidget(self._stream_btn)

        # Pause download button
        self._pause_dl_btn = QPushButton("⏸ Pause")
        self._pause_dl_btn.setObjectName("pauseDlBtn")
        self._pause_dl_btn.setFixedHeight(36)
        self._pause_dl_btn.setEnabled(False)
        self._connect_button(self._pause_dl_btn, "Pause Download", self._on_pause_download)
        h.addWidget(self._pause_dl_btn)

        # Cancel button
        self._cancel_btn = QPushButton("✕ Cancel")
        self._cancel_btn.setObjectName("cancelBtn")
        self._cancel_btn.setFixedHeight(36)
        self._cancel_btn.setEnabled(False)
        self._connect_button(self._cancel_btn, "Cancel Torrent", self._on_cancel)
        h.addWidget(self._cancel_btn)

        return bar

    def _build_stats_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("statsPanel")
        v = QVBoxLayout(panel)
        v.setContentsMargins(15, 15, 15, 15)
        v.setSpacing(10)

        # Top row: name + speed + peers
        top_row = QHBoxLayout()
        self._name_label = QLabel("No torrent loaded")
        self._name_label.setObjectName("nameLabel")
        self._name_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        top_row.addWidget(self._name_label, stretch=1)

        self._speed_label = QLabel("↓ 0 B/s")
        self._speed_label.setObjectName("speedLabel")
        top_row.addWidget(self._speed_label)

        self._peers_label = QLabel("0 peers")
        self._peers_label.setObjectName("peersLabel")
        top_row.addWidget(self._peers_label)

        self._size_label = QLabel("")
        self._size_label.setObjectName("sizeLabel")
        top_row.addWidget(self._size_label)

        v.addLayout(top_row)

        # Middle row: Viewers + Buffered Ahead
        mid_row = QHBoxLayout()
        self._viewers_label = QLabel("Current Viewers: 0")
        self._viewers_label.setStyleSheet("color: #aaa; font-size: 12px;")
        mid_row.addWidget(self._viewers_label)
        mid_row.addStretch(1)
        self._buffered_ahead_label = QLabel("Buffered Ahead: 0 sec")
        self._buffered_ahead_label.setStyleSheet("color: #aaa; font-size: 12px;")
        mid_row.addWidget(self._buffered_ahead_label)
        v.addLayout(mid_row)

        # Buffer progress bar
        self._buffer_bar = QProgressBar()
        self._buffer_bar.setObjectName("bufferBar")
        self._buffer_bar.setRange(0, 100)
        self._buffer_bar.setValue(0)
        self._buffer_bar.setFixedHeight(12)
        self._buffer_bar.setTextVisible(False)
        v.addWidget(self._buffer_bar)

        # Status label below bar
        self._status_label = QLabel("Startup Buffer: 0%")
        self._status_label.setObjectName("statusLabel")
        v.addWidget(self._status_label)

        # Throttle setting
        throttle_layout = QHBoxLayout()
        throttle_label = QLabel("Network Bitrate Limit:")
        throttle_label.setStyleSheet("color: #aaa; font-size: 12px;")
        throttle_layout.addWidget(throttle_label)
        
        self._throttle_combo = QComboBox()
        self._throttle_combo.addItems(["Unlimited", "20 Mbps", "10 Mbps", "5 Mbps", "2 Mbps"])
        self._throttle_combo.setStyleSheet("background: #1e1e1e; border: 1px solid #444; border-radius: 4px; padding: 4px; font-size: 12px; color: #ddd;")
        self._throttle_combo.currentTextChanged.connect(self._on_throttle_changed)
        throttle_layout.addWidget(self._throttle_combo)
        throttle_layout.addStretch(1)
        v.addLayout(throttle_layout)

        return panel

    @Slot(str)
    @_ui_debug_handler
    def _on_throttle_changed(self, text: str) -> None:
        rate = 0
        if text == "20 Mbps": rate = 20 * 1024 * 1024 // 8
        elif text == "10 Mbps": rate = 10 * 1024 * 1024 // 8
        elif text == "5 Mbps": rate = 5 * 1024 * 1024 // 8
        elif text == "2 Mbps": rate = 2 * 1024 * 1024 // 8
        if self._stream_server:
            self._debug_print(f"[STREAM] Throttle changed to {text} ({rate} B/s)")
            self._stream_server.set_throttle_rate(rate)

    # ------------------------------------------------------------------ #
    #  Signal wiring                                                       #
    # ------------------------------------------------------------------ #

    def _connect_signals(self) -> None:
        # Torrent worker
        self._debug_print("[UI] Connecting signal 'metadata_ready' -> _on_metadata_ready")
        self._torrent_worker.metadata_ready.connect(self._on_metadata_ready)
        self._debug_print("[UI] Connecting signal 'stats_updated' -> _on_stats_updated")
        self._torrent_worker.stats_updated.connect(self._on_stats_updated)
        self._debug_print("[UI] Connecting signal 'piece_finished' -> _on_piece_finished")
        self._torrent_worker.piece_finished.connect(self._on_piece_finished)
        self._torrent_worker.piece_finished.connect(self._piece_waiter.piece_done)
        self._debug_print("[UI] Connecting signal 'error_occurred' -> _on_torrent_error")
        self._torrent_worker.error_occurred.connect(self._on_torrent_error)
        self._debug_print("[UI] Connecting signal 'device_combo.currentTextChanged' -> _on_cast_device_selected")
        self._device_combo.currentTextChanged.connect(self._on_cast_device_selected)

        # Enter key in input field
        self._debug_print("[UI] Connecting signal 'magnet_input.returnPressed' -> _on_stream")
        self._magnet_input.returnPressed.connect(self._on_stream)

    # ------------------------------------------------------------------ #
    #  Button state management                                             #
    # ------------------------------------------------------------------ #

    def _set_active_state(self, active: bool) -> None:
        """Toggle button enabled/disabled based on whether a torrent is active."""
        self._is_active = active
        self._stream_btn.setEnabled(not active)
        self._magnet_input.setEnabled(not active)
        self._open_btn.setEnabled(not active)
        self._pause_dl_btn.setEnabled(active)
        self._cancel_btn.setEnabled(active)

    def _update_external_urls(self, port: int) -> None:
        localhost_url = f"http://127.0.0.1:{port}/video"
        self._lan_ip = get_lan_ip()
        lan_url = f"http://{self._lan_ip}:{port}/video"
        self._debug_print(f"[STREAM] Generated localhost URL: {localhost_url}")
        self._debug_print(f"[STREAM] Generated LAN URL: {lan_url}")
        
        self._localhost_url_label.setText(localhost_url)
        self._lan_url_label.setText(lan_url)
        self._sync_stream_debug_state()

        # Generate QR code for LAN URL
        try:
            if not self._has_valid_lan_ip():
                self._debug_print(f"[QR] Invalid LAN URL for QR generation: {lan_url}", logging.WARNING)
                self._qr_label.clear()
                self._qr_label.setText("QR Code\n(Invalid LAN URL)")
                return
            self._debug_print(f"[QR] Generating QR for URL: {lan_url}")
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=8,
                border=2,
            )
            qr.add_data(lan_url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            
            # Convert PIL image to QPixmap
            buf = BytesIO()
            img.save(buf, format="PNG")
            qimg = QImage.fromData(buf.getvalue())
            pixmap = QPixmap.fromImage(qimg)
            self._qr_label.setPixmap(pixmap)
            self._debug_print(f"[QR] QR pixmap updated for URL: {lan_url}")
        except Exception as e:
            self._report_exception(f"[QR] Failed to generate QR code for URL: {lan_url}")
            self._debug_print(f"[QR] QR generation exception detail: {e}", logging.ERROR)
            self._qr_label.setText("Failed to generate QR")
        finally:
            self._sync_stream_debug_state()

    # ------------------------------------------------------------------ #
    #  Slots — user actions                                                #
    # ------------------------------------------------------------------ #

    @Slot(str, QPushButton)
    @_ui_debug_handler
    def _copy_url(self, url: str, btn: QPushButton) -> None:
        self._debug_print(f"[UI] Copy URL requested: {url}")
        if not url or url.startswith("Waiting"):
            self._debug_print(f"[UI] Copy URL blocked because URL is not ready: {url}", logging.WARNING)
            return
        clipboard = QApplication.clipboard()
        clipboard.setText(url)
        copied_value = clipboard.text()
        self._debug_print(f"[UI] Clipboard now contains: {copied_value}")
        old_text = btn.text()
        btn.setText("Copied!")
        QTimer.singleShot(1500, lambda: btn.setText(old_text))

    @Slot(list)
    @_ui_debug_handler
    def _on_cast_devices_changed(self, devices: list[str]) -> None:
        self._debug_print(f"[CAST] Devices changed: {devices}")
        self.cast_devices_changed.emit(devices)

    @Slot(list)
    @_ui_debug_handler
    def _update_cast_devices(self, devices: list[str]) -> None:
        self._cast_devices = self._cast_manager.get_devices()
        self._device_combo.blockSignals(True)
        self._device_combo.clear()
        
        # Insert real Chromecast device names only
        for device in devices:
            log.info("[CAST UI] Adding device: %s", device)
            self._device_combo.addItem(device)
            
        count = self._device_combo.count()
        log.info("[CAST UI] Combo count after update: %d", count)
        
        if count > 0:
            # Auto-select the first discovered device
            self._device_combo.setCurrentIndex(0)
            selected = self._device_combo.currentText()
            log.info("[CAST UI] Auto-selected: %s", selected)
        else:
            self._selected_cast_device_name = None
            self._selected_cast_device = None
            
        self._device_combo.blockSignals(False)
        self._on_cast_device_selected(self._device_combo.currentText())

    @Slot()
    @_ui_debug_handler
    def _on_refresh_cast(self) -> None:
        self._debug_print("[CAST] Refresh requested")
        self._cast_manager.start_discovery(self._on_cast_devices_changed)

    @Slot(str)
    @_ui_debug_handler
    def _on_cast_device_selected(self, device_name: str) -> None:
        device_name = (device_name or "").strip()
        log.info("[CAST UI] Selected device changed: %s", device_name)
        if (
            not device_name
            or device_name == _NO_CAST_DEVICE_TEXT
            or device_name == "No devices found"
            or device_name not in self._cast_devices
        ):
            self._selected_cast_device_name = None
            self._selected_cast_device = None
            self._debug_print(f"[CAST] Selected device cleared: {device_name or '(empty)'}")
            self._sync_stream_debug_state()
            return

        self._selected_cast_device_name = device_name
        self._selected_cast_device = self._cast_devices[device_name]
        selected_ip = self._selected_cast_device_ip()
        self._debug_print(f"[CAST] Selected device: {device_name}")
        self._debug_print(f"[CAST] Selected device IP: {selected_ip}")
        self._sync_stream_debug_state()

    @Slot(bool, str)
    @_ui_debug_handler
    def _on_cast_connection_changed(self, connected: bool, reason: str) -> None:
        self._debug_print(f"[CAST UI] Connection status changed: connected={connected} reason={reason}")
        if not connected:
            if getattr(self, "_expected_disconnect", False):
                self._expected_disconnect = False
            else:
                QTimer.singleShot(0, lambda: self._handle_cast_disconnect(unexpected=True))

    def _handle_cast_disconnect(self, unexpected: bool = False) -> None:
        self._cast_is_playing = False
        self._cast_connect_pending = False
        self._cast_duration = 0.0
        self._cast_slider.setValue(0)
        self._cast_time_label.setText("00:00 / 00:00")
        
        # Reset buttons and UI controls
        self._cast_btn.setEnabled(True)
        self._cast_btn.setText("Connect")
        self._stop_cast_btn.setEnabled(False)
        self._cast_controls_widget.setVisible(False)
        
        # Reset diagnostics
        self._update_cast_diagnostics()
        self._cast_diag_group.setVisible(False)
        
        status_text = "❌ Disconnected" if unexpected else "Ready to connect"
        color = "#e84040" if unexpected else "#4caf50"
        self._cast_status_label.setText(status_text)
        self._cast_status_label.setStyleSheet(f"color: {color}; font-size: 12px; font-style: italic;")
        
        self._sync_stream_debug_state()

    def _update_cast_diagnostics(self, status=None) -> None:
        if not hasattr(self, "_cast_diag_text"):
            return
            
        with self._cast_manager._session_lock:
            device = self._cast_manager.cast_device_name or "(none)"
            session_active = "ACTIVE" if self._cast_manager.active_cast is not None else "INACTIVE"
            session = self._cast_manager._session
            stream_url = session.url if session else "(none)"
            
        if status:
            player_state = getattr(status, "player_state", "UNKNOWN")
            current_time = getattr(status, "current_time", 0.0) or 0.0
            duration = getattr(status, "duration", 0.0) or 0.0
            media_title = getattr(status, "title", None) or (session.title if session else None) or "(none)"
        else:
            player_state = self._cast_manager._last_player_state or "UNKNOWN"
            current_time = self._cast_manager._last_known_time or 0.0
            duration = self._cast_duration
            media_title = (session.title if session else None) or "(none)"
            
        def fmt(secs: float) -> str:
            secs = max(0.0, secs)
            m, s = divmod(int(secs), 60)
            h, m = divmod(m, 60)
            if h: return f"{h}:{m:02d}:{s:02d}"
            return f"{m:02d}:{s:02d}"
            
        diag_lines = [
            f"Connected Device: {device}",
            f"Playback State: {player_state}",
            f"Media Title: {media_title}",
            f"Current Time: {fmt(current_time)} ({current_time:.1f}s)",
            f"Duration: {fmt(duration)} ({duration:.1f}s)",
            f"Stream URL: {stream_url}",
            f"Cast Session: {session_active}"
        ]
        
        self._cast_diag_text.setPlainText("\n".join(diag_lines))

    @Slot()
    @_ui_debug_handler
    def _on_cast(self) -> None:
        device_name = self._selected_cast_device_name or ""
        selected_ip = self._selected_cast_device_ip()
        url = self._lan_url_label.text()
        self._debug_print(
            f"[CAST] Attempt with device={device_name} "
            f"stream_server={self._stream_server is not None} "
            f"stream_ready={self._stream_ready}"
        )
        self._debug_print(f"[CAST] Using selected device: {device_name or '(empty)'}")
        self._debug_print(
            f"[CAST] Diagnostics before cast: "
            f"device={device_name or '(empty)'} ip={selected_ip} "
            f"stream_url={url} stream_ready={self._stream_ready} "
            f"http_server_ready={self._stream_server is not None}"
        )
        
        is_direct = url.startswith("http") and not (
            "127.0.0.1" in url or (self._lan_ip and self._lan_ip in url)
        )
        if not self._has_selected_cast_device() or (not is_direct and not self._stream_server):
            self._debug_print(
                f"[CAST] Preconditions not met for cast: "
                f"device={device_name or '(empty)'} "
                f"device_selected={self._has_selected_cast_device()} "
                f"stream_server={self._stream_server is not None}",
                logging.WARNING,
            )
            self._cast_status_label.setText("⚠️ No device selected or stream not ready")
            self._cast_status_label.setStyleSheet("color: #ffb74d; font-size: 12px; font-style: italic;")
            return

        self._debug_print(f"[CAST] Stream URL for cast: {url}")
        
        content_type = "video/mp4"
        if self._current_file:
            from streaming.http_server import guess_content_type
            content_type = guess_content_type(self._current_file.name)
        elif url.startswith("http"):
            from streaming.http_server import guess_content_type
            content_type = guess_content_type(url)
            
        log.info("Selected MIME type: %s", content_type)
            
        self._cast_btn.setEnabled(False)
        self._cast_btn.setText("Casting...")
        self._stop_cast_btn.setEnabled(False)
        self._cast_status_label.setText(f"🔄 Connecting to {device_name}...")
        self._cast_status_label.setStyleSheet("color: #64b5f6; font-size: 12px; font-style: italic;")
        
        self._cast_connect_pending = True
        
        def cast_done(success: bool) -> None:
            self._debug_print(f"[CAST] Finished with success={success}")
            self._cast_btn.setText("Connect")
            if success:
                self._debug_print("[CAST] Cast worker succeeded. Waiting for active receiver status.")
                self._stop_cast_btn.setEnabled(True)
            else:
                self._debug_print("[CAST] Cast failed", logging.ERROR)
                self._cast_connect_pending = False
                self._cast_btn.setEnabled(True)
                self._stop_cast_btn.setEnabled(False)
                self._cast_controls_widget.setVisible(False)
                self._cast_diag_group.setVisible(False)
                self._cast_status_label.setText(f"❌ Failed to connect to {device_name}. Check network and device availability.")
                self._cast_status_label.setStyleSheet("color: #e84040; font-size: 12px; font-style: italic;")
                self._set_status("❌ Failed to cast to device.")
                
        self._debug_print(
            f"[CAST] Calling cast_manager.cast_url device={device_name} "
            f"url={url} content_type={content_type}"
        )
        self._cast_manager.cast_url(
            device_name,
            url,
            content_type,
            on_finished=lambda success: QTimer.singleShot(0, lambda: cast_done(success))
        )

    @Slot()
    @_ui_debug_handler
    def _on_stop_cast(self) -> None:
        self._debug_print("[CAST] Disconnect requested")
        self._expected_disconnect = True
        self._cast_manager.stop_cast()
        self._handle_cast_disconnect(unexpected=False)
        self._debug_print("[CAST] Cast stopped")

    @Slot()
    @_ui_debug_handler
    def _on_cast_play_pause(self) -> None:
        if not self._cast_manager.is_controller_valid():
            self._debug_print("[CAST] Play/pause failed: Cast controller not valid")
            return
            
        player_state = getattr(self._cast_manager.active_media_controller.status, "player_state", None)
        self._debug_print(f"[CAST] Play/pause requested; current player_state={player_state}")
        if player_state == "PLAYING":
            self._cast_manager.pause()
            self._debug_print("[CAST] Pause command sent")
        else:
            self._cast_manager.play()
            self._debug_print("[CAST] Play command sent")

    @Slot()
    @_ui_debug_handler
    def _on_cast_seek(self) -> None:
        val = self._cast_slider.value()
        pos = (val / 1000.0) * self._cast_duration
        self._debug_print(f"[CAST] Seek slider={val} position={pos}")
        self._cast_manager.seek(pos)

    @Slot()
    @_ui_debug_handler
    def _on_cast_volume(self) -> None:
        val = self._cast_volume_slider.value()
        volume = val / 100.0
        self._debug_print(f"[CAST] Volume slider={val} volume={volume}")
        self._cast_manager.set_volume(volume)

    @Slot(object)
    @_ui_debug_handler
    def _on_cast_media_status(self, status) -> None:
        self._debug_print(f"[CAST] Media status callback received: {status}", logging.DEBUG)
        QTimer.singleShot(0, lambda: self._update_cast_status(status))

    @_ui_debug_handler
    def _update_cast_status(self, status) -> None:
        if not status:
            self._debug_print("[CAST] Ignoring empty media status", logging.WARNING)
            return
            
        player_state = getattr(status, "player_state", "UNKNOWN")
        has_session = bool(getattr(status, "media_session_id", None))
        
        # 2. Fix “Connected Successfully” behavior: Show success ONLY after media session active or player_state exists
        if self._cast_connect_pending:
            if player_state in {"PLAYING", "BUFFERING", "PAUSED"} or has_session:
                self._cast_connect_pending = False
                device_name = self._selected_cast_device_name or "Chromecast"
                self._cast_btn.setEnabled(False)
                self._stop_cast_btn.setEnabled(True)
                self._cast_controls_widget.setVisible(True)
                self._cast_diag_group.setVisible(True)
                self._cast_status_label.setText(f"✅ Connected to {device_name}")
                self._cast_status_label.setStyleSheet("color: #4caf50; font-size: 12px; font-style: italic;")
                
        # 4. Add real playback-state synchronization (Track: PLAYING, PAUSED, BUFFERING, IDLE, UNKNOWN)
        self._cast_is_playing = (player_state == "PLAYING")
        self._cast_play_btn.setText("⏸" if self._cast_is_playing else "▶")
        
        self._cast_play_btn.setEnabled(has_session)
        self._cast_slider.setEnabled(has_session)
        
        self._cast_duration = status.duration or 0.0
        current_time = status.current_time or 0.0
        
        if self._cast_duration > 0 and not self._cast_slider.isSliderDown():
            self._cast_slider.blockSignals(True)
            self._cast_slider.setValue(int((current_time / self._cast_duration) * 1000))
            self._cast_slider.blockSignals(False)
            
        def fmt(secs: float) -> str:
            secs = max(0.0, secs)
            m, s = divmod(int(secs), 60)
            h, m = divmod(m, 60)
            if h: return f"{h}:{m:02d}:{s:02d}"
            return f"{m:02d}:{s:02d}"
            
        self._cast_time_label.setText(f"{fmt(current_time)} / {fmt(self._cast_duration)}")
        
        self._update_cast_diagnostics(status)

    @Slot()
    @_ui_debug_handler
    def _launch_vlc(self) -> None:
        url = self._localhost_url_label.text()
        self._debug_print(f"[UI] Open VLC requested with URL: {url}")
        if not self._stream_server:
            self._debug_print("[UI] Open VLC blocked because stream server is not ready", logging.WARNING)
            return
        launch_vlc(url)

    @Slot()
    @_ui_debug_handler
    def _launch_mpv(self) -> None:
        url = self._localhost_url_label.text()
        self._debug_print(f"[UI] Open MPV requested with URL: {url}")
        if not self._stream_server:
            self._debug_print("[UI] Open MPV blocked because stream server is not ready", logging.WARNING)
            return
        launch_mpv(url)

    @Slot()
    @_ui_debug_handler
    def _on_open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Torrent File", "", "Torrent Files (*.torrent)"
        )
        self._debug_print(f"[UI] Open file dialog returned path: {path}")
        if path:
            self._magnet_input.setText(path)

    @Slot()
    @_ui_debug_handler
    def _on_stream(self) -> None:
        text = self._magnet_input.text().strip()
        self._debug_print(f"[STREAM] Start requested with input: {text}")
        if not text:
            self._set_status("⚠️  Please enter a magnet link or torrent file path.")
            return

        if text.startswith("http://") or text.startswith("https://"):
            self._debug_print(f"[STREAM] Direct HTTP stream URL detected: {text}")
            self._stream_ready = True
            self._name_label.setText("Direct Stream Test (No Torrent)")
            self._localhost_url_label.setText(text)
            self._lan_url_label.setText(text)
            self._sync_stream_debug_state()
            self._stream_panel.setVisible(True)
            self._set_status("▶  Server Ready — Stream URL is active")
            
            # Generate QR code for direct link
            try:
                qr = qrcode.QRCode(version=1, box_size=3, border=2)
                qr.add_data(text)
                qr.make(fit=True)
                img = qr.make_image(fill_color="black", back_color="white")
                
                # Convert PIL image to QPixmap
                from io import BytesIO
                from PySide6.QtGui import QPixmap
                buffer = BytesIO()
                img.save(buffer, format="PNG")
                pixmap = QPixmap()
                pixmap.loadFromData(buffer.getvalue())
                self._qr_label.setPixmap(pixmap)
            except Exception as exc:
                self._debug_print(f"[QR] Failed to generate QR code: {exc}", logging.WARNING)
            return

        # Reset state
        self._current_file = None
        self._stream_ready = False
        self._buffer_bar.setValue(0)
        self._buffer_timer.stop()
        self._stream_panel.setVisible(False)
        self._localhost_url_label.setText("Waiting for stream...")
        self._lan_url_label.setText("Waiting for stream...")
        self._qr_label.clear()
        self._qr_label.setText("QR Code\n(Pending)")
        self._sync_stream_debug_state()

        # Update buffer monitor
        self._buffer_monitor = BufferMonitor(self._config.buffer_bytes)

        # Make sure cache is clean for new session
        self._cache.ensure_fresh()

        self._set_status("🔍  Fetching metadata…")
        self._name_label.setText("Resolving torrent…")
        self._set_active_state(True)

        if not self._torrent_thread.isRunning():
            self._torrent_thread.start()

        if text.startswith("magnet:"):
            self._torrent_worker.add_magnet(text)
        elif Path(text).suffix.lower() == ".torrent" and Path(text).exists():
            self._torrent_worker.add_torrent_file(text)
        else:
            # Try as magnet anyway
            self._torrent_worker.add_magnet(text)

    @Slot()
    @_ui_debug_handler
    def _on_pause_download(self) -> None:
        """Toggle pause / resume of the torrent download."""
        if self._torrent_worker.is_paused:
            self._torrent_worker.resume_torrent()
            self._pause_dl_btn.setText("⏸ Pause")
            self._set_status("▶  Resumed downloading")
        else:
            self._torrent_worker.pause_torrent()
            self._pause_dl_btn.setText("▶ Resume")
            self._set_status("⏸  Download paused")

    @Slot()
    @_ui_debug_handler
    def _on_cancel(self) -> None:
        """Cancel the current torrent session entirely."""
        self._debug_print("[STREAM] Cancel requested")

        # Tear down HTTP streaming layer FIRST
        if self._stream_server is not None:
            self._stream_server.stop()
            self._stream_server = None
        self._stream_source.detach()

        # Stop buffer monitoring
        self._buffer_timer.stop()
        self._buffer_monitor.detach()
        self._prioritizer.detach()

        # Remove torrent from libtorrent session
        self._torrent_worker.cancel()

        # Clean cache
        self._cache.cleanup(force=True)
        self._cache.ensure_fresh()

        # Reset UI
        self._current_file = None
        self._files = []
        self._stream_ready = False
        self._buffer_bar.setValue(0)
        self._name_label.setText("No torrent loaded")
        self._speed_label.setText("↓ 0 B/s")
        self._peers_label.setText("0 peers")
        self._size_label.setText("")
        self._set_status("Ready — paste a magnet link to begin")
        self._pause_dl_btn.setText("⏸ Pause")
        self._stream_panel.setVisible(False)
        self._set_active_state(False)
        self._localhost_url_label.setText("Waiting for stream...")
        self._lan_url_label.setText("Waiting for stream...")
        self._qr_label.clear()
        self._qr_label.setText("QR Code\n(Pending)")
        self._sync_stream_debug_state()


    # ------------------------------------------------------------------ #
    #  Slots — torrent worker                                              #
    # ------------------------------------------------------------------ #

    @Slot(list)
    @_ui_debug_handler
    def _on_metadata_ready(self, files: List[FileInfo]) -> None:
        self._debug_print(f"[STREAM] Metadata ready with {len(files)} video file(s)")
        self._files = files

        if not files:
            self._set_status("⚠️  No video files found in this torrent.")
            return

        if len(files) == 1:
            self._select_file(files[0])
        else:
            self._show_file_selector(files)

    @Slot(object, object, object, object)
    @_ui_debug_handler
    def _on_stats_updated(self, speed, peers, downloaded, total) -> None:
        try:
            self._speed_label.setText(f"↓ {_fmt_speed(speed)}")
            self._peers_label.setText(f"{int(peers)} peer{'s' if int(peers) != 1 else ''}")
            if int(total) > 0:
                self._size_label.setText(
                    f"{_fmt_size(downloaded)} / {_fmt_size(total)}"
                )
        except Exception:
            self._report_exception("[STREAM] Stats update failed")

    @Slot(int)
    @_ui_debug_handler
    def _on_piece_finished(self, index: int) -> None:
        # Nothing to do here — buffer_monitor uses the handle directly
        self._debug_print(f"[STREAM] Piece finished: {index}", logging.DEBUG)

    @Slot(str)
    @_ui_debug_handler
    def _on_torrent_error(self, message: str) -> None:
        self._debug_print(f"[STREAM] Torrent error: {message}", logging.ERROR)
        self._set_status(f"❌  Error: {message}")

    # ------------------------------------------------------------------ #
    #  File selection                                                      #
    # ------------------------------------------------------------------ #

    @_ui_debug_handler
    def _show_file_selector(self, files: List[FileInfo]) -> None:
        dlg = _FileSelectorDialog(files, self)
        dlg.setStyleSheet(self.styleSheet())
        if dlg.exec() == QDialog.DialogCode.Accepted:
            selected = dlg.selected_file()
            if selected:
                self._select_file(selected)

    @_ui_debug_handler
    def _select_file(self, file: FileInfo) -> None:
        self._current_file = file
        self._name_label.setText(f"📄 {file.name}  ({file.human_size()})")
        self._set_status(f"⏳  Buffering — waiting for {self._config.buffer_mb} MB…")

        handle = self._torrent_worker.get_handle()
        info = self._torrent_worker.get_torrent_info()

        if handle and info:
            files = info.files()
            piece_size = info.piece_length()
            file_offset = files.file_offset(file.index)
            file_size = files.file_size(file.index)
            first_piece = file_offset // piece_size
            last_piece = min(
                (file_offset + file_size - 1) // piece_size,
                info.num_pieces() - 1,
            )
            self._debug_print(
                f"[SCHED] UI selected file index={file.index} first_piece={first_piece} "
                f"last_piece={last_piece} piece_size={piece_size}"
            )
            self._torrent_worker.select_file(file.index)
            self._buffer_monitor.attach(handle, info, file.index)
            self._prioritizer.attach(handle, info, file.index)
            self._stream_source.attach(
                handle, info, file.index,
                file_path=file.abs_path,
                prioritizer=self._prioritizer,
                waiter=self._piece_waiter,
            )
            # Apply the streaming window immediately so the initial
            # buffer fills with pieces from the FRONT of the file,
            # not whatever libtorrent finds easiest.
            self._prioritizer.on_seek_bytes(0)
            self._buffer_timer.start()
            self._debug_print(f"[STREAM] Started buffering file: {file.abs_path}")
        else:
            self._set_status("⚠️  Torrent handle not ready yet — retrying…")
            # Retry after a short delay
            QTimer.singleShot(1000, lambda: self._select_file(file))

    # ------------------------------------------------------------------ #
    #  Buffer polling                                                      #
    # ------------------------------------------------------------------ #

    @Slot()
    @_ui_debug_handler
    def _poll_buffer(self) -> None:
        if self._current_file is None:
            return

        try:
            pct = self._buffer_monitor.buffer_percent()
            self._buffer_bar.setValue(int(pct))
            buf_downloaded = self._buffer_monitor.buffer_bytes_downloaded()

            log.debug(
                "Buffer poll: %.1f%%  (%s / %s)  ready=%s",
                pct,
                _fmt_size(buf_downloaded),
                _fmt_size(self._config.buffer_bytes),
                self._buffer_monitor.is_ready(),
            )

            if self._buffer_monitor.is_ready() and not self._stream_ready:
                self._start_streaming()
            elif not self._stream_ready:
                self._set_status(f"Startup Buffer: {pct:.0f}%")
            else:
                self._set_status("▶  Server Ready — Stream URL is active")

            # Update extra stats
            if self._stream_server:
                self._viewers_label.setText(f"Current Viewers: {self._stream_server.active_viewers}")
            else:
                self._viewers_label.setText("Current Viewers: 0")

            if self._stream_source and self._stream_source.is_attached() and self._current_file:
                ranges = self._stream_source.buffered_ranges()
                if ranges:
                    # Calculate total megabytes buffered
                    buffered_bytes = sum(end - start for start, end in ranges)
                    buffered_mb = buffered_bytes / (1024 * 1024)
                    self._buffered_ahead_label.setText(f"Buffered Ahead: {buffered_mb:.1f} MB")
                else:
                    self._buffered_ahead_label.setText("Buffered Ahead: 0.0 MB")

        except Exception:
            self._report_exception("[STREAM] Buffer poll failed")

    @_ui_debug_handler
    def _start_streaming(self) -> None:
        if self._current_file is None or self._stream_ready:
            return
        self._stream_ready = True
        self._buffer_bar.setValue(100)
        self._set_status("▶  Server Ready — Stream URL is active")

        # Keep the buffer timer running to continue showing stats

        # Spin up the localhost HTTP server
        if self._stream_server is None:
            self._stream_server = StreamServer(self._stream_source, bind_all=self._config.bind_all_interfaces)
            _, port, url = self._stream_server.start()
            self._debug_print(f"[STREAM] HTTP server started at {url}", logging.DEBUG)
        else:
            port = self._stream_server.port
            self._debug_print(f"[STREAM] Reusing existing HTTP server on port {port}", logging.DEBUG)

        self._update_external_urls(port)
        self._stream_panel.setVisible(True)
        self._debug_print(f"[STREAM] Streaming ready via {self._stream_server.url}", logging.DEBUG)
        self._sync_stream_debug_state()


    @_ui_debug_handler
    def _set_status(self, text: str) -> None:
        self._status_label.setText(text)
        if "Startup Buffer:" in text:
            self._debug_print(f"[STATUS] {text}", logging.DEBUG)
        else:
            if getattr(self, "_last_status_text", None) != text:
                self._last_status_text = text
                self._debug_print(f"[STATUS] {text}", logging.INFO)

    # ------------------------------------------------------------------ #
    #  Global styles                                                       #
    # ------------------------------------------------------------------ #

    def _apply_global_styles(self) -> None:
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #121212;
                color: #e0e0e0;
                font-family: 'Segoe UI', 'Inter', 'Helvetica Neue', sans-serif;
                font-size: 13px;
            }

            /* ── Top Bar ── */
            QWidget#topBar {
                background: #1a1a1a;
                border-bottom: 1px solid #252525;
            }
            QLabel#logoLabel {
                color: #1e90ff;
                font-size: 16px;
                font-weight: bold;
                letter-spacing: 0.5px;
            }
            QLineEdit#magnetInput {
                background: #1e1e1e;
                color: #ddd;
                border: 1px solid #333;
                border-radius: 6px;
                padding: 0 10px;
                height: 34px;
                font-size: 13px;
            }
            QLineEdit#magnetInput:focus {
                border: 1px solid #1e90ff;
            }
            QLineEdit#magnetInput:disabled {
                background: #161616;
                color: #555;
            }
            QPushButton#openBtn {
                background: #252525;
                color: #bbb;
                border: 1px solid #383838;
                border-radius: 6px;
                padding: 0 14px;
                font-size: 13px;
            }
            QPushButton#openBtn:hover {
                background: #2e2e2e;
                color: #ddd;
            }
            QPushButton#streamBtn {
                background: #1e4d8c;
                color: #fff;
                border: none;
                border-radius: 6px;
                padding: 0 18px;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton#streamBtn:hover {
                background: #2563b0;
            }
            QPushButton#streamBtn:pressed {
                background: #1a3f74;
            }
            QPushButton#streamBtn:disabled {
                background: #1a2a3c;
                color: #556;
            }
            QPushButton#pauseDlBtn {
                background: #3a3520;
                color: #e8c840;
                border: 1px solid #554a1a;
                border-radius: 6px;
                padding: 0 14px;
                font-size: 13px;
            }
            QPushButton#pauseDlBtn:hover {
                background: #4a4530;
            }
            QPushButton#pauseDlBtn:disabled {
                background: #1e1e1e;
                color: #555;
                border-color: #333;
            }
            QPushButton#cancelBtn {
                background: #3c1e1e;
                color: #e84040;
                border: 1px solid #5a2020;
                border-radius: 6px;
                padding: 0 14px;
                font-size: 13px;
            }
            QPushButton#cancelBtn:hover {
                background: #4c2828;
            }
            QPushButton#cancelBtn:disabled {
                background: #1e1e1e;
                color: #555;
                border-color: #333;
            }
            
            /* ── Stats Panel ── */
            QWidget#statsPanel {
                background: #1e1e1e;
                border: 1px solid #333;
                border-radius: 8px;
            }
            QLabel#speedLabel {
                color: #4caf50;
                font-size: 12px;
                font-family: 'Consolas', monospace;
            }
            QLabel#peersLabel {
                color: #888;
                font-size: 12px;
            }
            QLabel#sizeLabel {
                color: #888;
                font-size: 12px;
                font-family: 'Consolas', monospace;
            }
            QLabel#statusLabel {
                color: #aaa;
                font-size: 12px;
            }
            QProgressBar#bufferBar {
                background: #111;
                border: 1px solid #222;
                border-radius: 6px;
            }
            QProgressBar#bufferBar::chunk {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #1e4d8c, stop:1 #1e90ff
                );
                border-radius: 6px;
            }

            /* ── Group Boxes ── */
            QGroupBox { 
                color: #aaa; 
                border: 1px solid #333; 
                border-radius: 8px; 
                margin-top: 1ex; 
            } 
            QGroupBox::title { 
                subcontrol-origin: margin; 
                left: 10px; 
                padding: 0 3px; 
            }
            QLineEdit[readOnly="true"] {
                background: #1e1e1e; 
                border: 1px solid #444; 
                border-radius: 4px; 
                padding: 6px; 
                font-size: 13px;
                color: #ddd;
            }

            /* ── Dialog ── */
            QDialog {
                background: #161616;
            }
        """)

    # ------------------------------------------------------------------ #
    #  Window close                                                        #
    # ------------------------------------------------------------------ #

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._debug_print("[APP] Closing application…")
        self._cast_manager.stop_discovery()
        if self._stream_server is not None:
            self._stream_server.stop()
            self._stream_server = None
            self._sync_stream_debug_state()
        self._stream_source.detach()
        self._buffer_timer.stop()
        self._torrent_worker.stop()
        self._torrent_thread.quit()
        self._torrent_thread.wait(3000)
        self._config.save()
        if self._ui_log_handler is not None:
            logging.getLogger().removeHandler(self._ui_log_handler)
            self._ui_log_handler = None
        # CacheManager atexit hook handles actual deletion
        super().closeEvent(event)
