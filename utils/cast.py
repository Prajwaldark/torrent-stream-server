"""
Utilities for discovering and controlling Chromecast-compatible receivers.
"""
import logging
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import pychromecast
import zeroconf
from pychromecast.config import APP_MEDIA_RECEIVER
from pychromecast.controllers.media import MediaStatus, MediaStatusListener
from pychromecast.controllers.receiver import CastStatus, CastStatusListener
from pychromecast.socket_client import (
    CONNECTION_STATUS_CONNECTED,
    CONNECTION_STATUS_DISCONNECTED,
    CONNECTION_STATUS_FAILED,
    CONNECTION_STATUS_LOST,
    ConnectionStatus,
    ConnectionStatusListener,
)

# Setup SSLContext monkeypatch for compatibility with legacy devices (e.g. AirReceiver)
_orig_new = ssl.SSLContext.__new__


def new_SSLContext(cls, *args, **kwargs):
    context = _orig_new(cls, *args, **kwargs)
    try:
        context.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
    except Exception:
        pass
    try:
        context.set_ciphers("DEFAULT@SECLEVEL=1")
    except Exception:
        pass
    return context


ssl.SSLContext.__new__ = new_SSLContext

log = logging.getLogger("CAST")

_CONNECT_TIMEOUT_SECS = 10.0
_RECEIVER_TIMEOUT_SECS = 10.0
_MEDIA_SESSION_TIMEOUT_SECS = 15.0
_STATUS_POLL_INTERVAL_SECS = 1.0
_STATUS_WAIT_STEP_SECS = 0.25


@dataclass
class _CastSession:
    device_name: str
    url: str
    content_type: str
    title: str


class CastManager(MediaStatusListener, CastStatusListener, ConnectionStatusListener):
    def __init__(self) -> None:
        self._devices: dict[str, pychromecast.Chromecast] = {}
        self._browser: Optional[pychromecast.discovery.CastBrowser] = None
        self._zconf: Optional[zeroconf.Zeroconf] = None
        self._on_devices_changed: Optional[Callable[[list[str]], None]] = None
        self._on_media_status: Optional[Callable[[MediaStatus], None]] = None
        self._on_connection_changed: Optional[Callable[[bool, str], None]] = None
        self._current_cast: Optional[pychromecast.Chromecast] = None
        self._session: Optional[_CastSession] = None
        self._session_lock = threading.RLock()
        self._status_poll_thread: Optional[threading.Thread] = None
        self._status_poll_stop = threading.Event()
        self._last_known_time = 0.0
        self._last_player_state: Optional[str] = None
        self._last_media_session_id: Optional[int] = None
        self._last_receiver_signature: Optional[tuple[str | None, str | None, str | None]] = None
        
        # Real Cast session state tracking (updated ONLY from actual status/events)
        self.cast_connected = False
        self.cast_playing = False
        self.cast_device_name = None
        self.active_cast = None
        self.active_media_controller = None
        
        log.info("CastManager initialized")

    def start_discovery(self, on_devices_changed: Callable[[list[str]], None]) -> None:
        self._on_devices_changed = on_devices_changed
        if self._browser is not None:
            log.debug("Discovery already started, refreshing device cache")
            self._update_devices()
            return

        try:
            log.info("Starting Chromecast discovery")
            self._zconf = zeroconf.Zeroconf()

            def add_callback(uuid, service):
                log.debug("Chromecast service added: uuid=%s service=%s", uuid, service)
                self._update_devices()

            def remove_callback(uuid, service, cast_info):
                log.debug("Chromecast service removed: uuid=%s cast=%s", uuid, cast_info)
                self._update_devices()

            def update_callback(uuid, service):
                log.debug("Chromecast service updated: uuid=%s service=%s", uuid, service)
                self._update_devices()

            listener = pychromecast.SimpleCastListener(add_callback, remove_callback, update_callback)
            self._browser = pychromecast.CastBrowser(listener, self._zconf)
            self._browser.start_discovery()
            log.info("Chromecast discovery started")
        except Exception as exc:
            log.error("Failed to start Chromecast discovery: %s", exc)

    def _update_devices(self) -> None:
        if not self._browser:
            return

        devices = self._browser.devices
        new_device_names = {
            service.friendly_name
            for service in devices.values()
            if service.friendly_name
        }
        changed = new_device_names != set(self._devices.keys())

        for name in list(self._devices.keys()):
            if name in new_device_names:
                continue
            cc = self._devices.pop(name)
            try:
                log.info("[CAST] Removing stale device entry: %s", name)
                cc.disconnect(timeout=2)
            except Exception:
                pass

        for service in devices.values():
            friendly_name = service.friendly_name
            if not friendly_name:
                continue

            cc = self._devices.get(friendly_name)
            if cc is not None and cc.socket_client.is_alive():
                continue

            try:
                log.info("[CAST] Preparing Chromecast handle for %s", friendly_name)
                self._devices[friendly_name] = pychromecast.get_chromecast_from_cast_info(
                    service,
                    self._browser.zc,
                    tries=None,
                    retry_wait=5.0,
                    timeout=_CONNECT_TIMEOUT_SECS,
                )
            except Exception as exc:
                log.error("[CAST] Failed to prepare Chromecast handle for %s: %s", friendly_name, exc)

        with self._session_lock:
            active_name = self.cast_device_name
        if active_name and active_name not in new_device_names:
            log.warning("[CAST] Active receiver disappeared from discovery: %s", active_name)
            self.stop_cast()
            if self._on_connection_changed:
                self._on_connection_changed(False, "RECEIVER_DISAPPEARED")

        if changed and self._on_devices_changed:
            device_list = list(self._devices.keys())
            log.info("Chromecast devices found: %s", device_list)
            self._on_devices_changed(device_list)

    def get_devices(self) -> dict[str, object]:
        return dict(self._devices)

    def get_device(self, device_name: str) -> Optional[object]:
        return self._devices.get(device_name)

    def stop_discovery(self) -> None:
        self._stop_status_poller()
        if self._browser:
            try:
                self._browser.stop_discovery()
            except Exception as exc:
                log.debug("Failed to stop CastBrowser discovery: %s", exc)
            self._browser = None

        if self._zconf:
            try:
                self._zconf.close()
            except Exception as exc:
                log.debug("Failed to close Zeroconf: %s", exc)
            self._zconf = None

    def set_status_listener(self, on_media_status: Callable[[MediaStatus], None]) -> None:
        self._on_media_status = on_media_status

    def set_connection_listener(self, callback: Callable[[bool, str], None]) -> None:
        self._on_connection_changed = callback

    def is_controller_valid(self) -> bool:
        """
        Verify:
        - media controller exists (active_media_controller is not None)
        - media session active (active_media_controller.status.media_session_id is not None)
        - cast connected (cast_connected is True)
        """
        with self._session_lock:
            if not self.cast_connected:
                log.warning("[CAST] Validation failed: cast is not connected")
                return False
            if not self.active_media_controller:
                log.warning("[CAST] Validation failed: active_media_controller is None")
                return False
            session_id = getattr(self.active_media_controller.status, "media_session_id", None)
            if not session_id:
                log.warning("[CAST] Validation failed: media session is not active")
                return False
            return True

    def new_media_status(self, status: MediaStatus) -> None:
        player_state = getattr(status, "player_state", None)
        media_session_id = getattr(status, "media_session_id", None)
        current_time = getattr(status, "current_time", 0.0) or 0.0
        duration = getattr(status, "duration", 0.0) or 0.0

        with self._session_lock:
            self._last_known_time = current_time
            should_log = (
                player_state != self._last_player_state
                or media_session_id != self._last_media_session_id
            )
            self._last_player_state = player_state
            self._last_media_session_id = media_session_id
            session = self._session
            
            # Update persistent playing state based on actual events
            self.cast_playing = (player_state == "PLAYING")
            
            if self._current_cast:
                self.cast_connected = self._current_cast.socket_client.is_connected
                if self.cast_connected:
                    self.active_cast = self._current_cast
                    self.active_media_controller = self._current_cast.media_controller
                    self.cast_device_name = self._current_cast.device.friendly_name

        if should_log:
            device_name = session.device_name if session else "(unknown)"
            log.info(
                "[CAST] Player state device=%s state=%s session=%s time=%.1f/%.1f",
                device_name,
                player_state,
                media_session_id,
                current_time,
                duration,
            )

        if self._on_media_status:
            self._on_media_status(status)

    def new_cast_status(self, status: CastStatus) -> None:
        signature = (status.app_id, status.display_name, status.transport_id)
        with self._session_lock:
            should_log = signature != self._last_receiver_signature
            self._last_receiver_signature = signature
            session = self._session

        if not should_log:
            return

        device_name = session.device_name if session else "(unknown)"
        log.info(
            "[CAST] Receiver status device=%s app_id=%s app=%s transport_id=%s",
            device_name,
            status.app_id,
            status.display_name,
            status.transport_id,
        )

    def new_connection_status(self, status: ConnectionStatus) -> None:
        address = status.address.address if status.address else None
        port = status.address.port if status.address else None
        log.info(
            "[CAST] Connection status=%s address=%s port=%s service=%s",
            status.status,
            address,
            port,
            status.service,
        )

        if status.status in (
            CONNECTION_STATUS_DISCONNECTED,
            CONNECTION_STATUS_FAILED,
            CONNECTION_STATUS_LOST,
        ):
            was_active = False
            with self._session_lock:
                if self.active_cast or self._current_cast or self._session:
                    was_active = True
                self.cast_connected = False
                self.cast_playing = False
                self.cast_device_name = None
                self.active_cast = None
                self.active_media_controller = None
                self._current_cast = None
                self._session = None
                self._last_known_time = 0.0
                self._last_player_state = None
                self._last_media_session_id = None
                self._last_receiver_signature = None

            self._stop_status_poller()
            if was_active and self._on_connection_changed:
                self._on_connection_changed(False, status.status)

        if status.status == CONNECTION_STATUS_CONNECTED:
            with self._session_lock:
                self.cast_connected = True
                if self._current_cast:
                    self.active_cast = self._current_cast
                    self.active_media_controller = self._current_cast.media_controller
                    self.cast_device_name = self._current_cast.device.friendly_name
            self._refresh_status_async()
            if self._on_connection_changed:
                self._on_connection_changed(True, "CONNECTED")

    def load_media_failed(self, queue_item_id: int, error_code: int) -> None:
        log.error(
            "[CAST] Media load failed: queue_item_id=%s error_code=%s",
            queue_item_id,
            error_code,
        )

    def cast_url(
        self,
        device_name: str,
        url: str,
        content_type: str = "video/mp4",
        title: str = "TorrentStream",
        on_finished: Optional[Callable[[bool], None]] = None,
    ) -> None:
        log.info(
            "[CAST] Cast request device=%s url=%s content_type=%s",
            device_name,
            url,
            content_type,
        )

        def worker() -> None:
            success = False
            try:
                cc = self._ensure_device(device_name)
                self._register_listeners(cc)
                try:
                    self._ensure_connected(cc, device_name)
                except Exception:
                    cc = self._ensure_device(device_name, force_rebuild=True)
                    self._register_listeners(cc)
                    self._ensure_connected(cc, device_name)
                self._ensure_default_receiver(cc, device_name)
                self._launch_media(
                    cc,
                    device_name,
                    url,
                    content_type,
                    title or "TorrentStream",
                    current_time=None,
                )
                with self._session_lock:
                    self._current_cast = cc
                    self._session = _CastSession(
                        device_name=device_name,
                        url=url,
                        content_type=content_type,
                        title=title or "TorrentStream",
                    )
                self._start_status_poller()
                success = True
            except Exception as exc:
                log.error("[CAST] Failed to launch media on %s: %s", device_name, exc, exc_info=True)
            finally:
                if on_finished:
                    on_finished(success)

        threading.Thread(target=worker, name="CastLaunchWorker", daemon=True).start()

    def play(self) -> None:
        self._ensure_active_controller("play")
        if not self.is_controller_valid():
            log.warning("[CAST] Cannot play: media controller validation failed")
            return
        log.info("[CAST] Resume requested device=%s", self.cast_device_name)
        self.active_media_controller.play()

    def pause(self) -> None:
        self._ensure_active_controller("pause")
        if not self.is_controller_valid():
            log.warning("[CAST] Cannot pause: media controller validation failed")
            return
        log.info("[CAST] Pause requested device=%s", self.cast_device_name)
        self.active_media_controller.pause()

    def seek(self, position: float) -> None:
        self._ensure_active_controller("seek")
        if not self.is_controller_valid():
            log.warning("[CAST] Cannot seek: media controller validation failed")
            return
        log.info("[CAST] Seek requested device=%s position=%.1f", self.cast_device_name, position)
        self.active_media_controller.seek(position)

    def set_volume(self, volume: float) -> None:
        with self._session_lock:
            cc = self.active_cast or self._current_cast
            connected = self.cast_connected
            device_name = self.cast_device_name
        if not cc or not connected:
            log.warning("[CAST] Ignoring volume change: cast is not connected")
            return
        try:
            log.info("[CAST] Volume requested device=%s level=%.2f", device_name, volume)
            cc.set_volume(volume)
        except Exception as exc:
            log.error("[CAST] Failed to set volume on %s: %s", device_name, exc)

    def stop_cast(self) -> None:
        with self._session_lock:
            cc = self.active_cast or self._current_cast
            
            # Clear active session references to prevent stale session reuse
            self.cast_connected = False
            self.cast_playing = False
            self.cast_device_name = None
            self.active_cast = None
            self.active_media_controller = None
            
            self._current_cast = None
            self._session = None
            self._last_known_time = 0.0
            self._last_player_state = None
            self._last_media_session_id = None
            self._last_receiver_signature = None

        self._stop_status_poller()

        if not cc:
            return

        def worker() -> None:
            device_name = getattr(cc, "name", "(unknown)")
            try:
                # 11. Add media-controller validation: Check media controller exists
                if cc.media_controller is not None:
                    log.info("[CAST] Stop requested device=%s", device_name)
                    cc.media_controller.stop()
            except Exception as exc:
                log.error("[CAST] Failed to stop media on %s: %s", device_name, exc)

            try:
                if getattr(cc, "app_id", None) == APP_MEDIA_RECEIVER:
                    log.info("[CAST] Quitting receiver app device=%s", device_name)
                    cc.quit_app()
            except Exception as exc:
                log.error("[CAST] Failed to quit receiver app on %s: %s", device_name, exc)

            try:
                cc.disconnect(timeout=5)
                log.info("[CAST] Disconnected from %s", device_name)
            except Exception as exc:
                log.debug("[CAST] Disconnect cleanup failed for %s: %s", device_name, exc)

        threading.Thread(target=worker, name="CastStopWorker", daemon=True).start()

    def _find_service(self, device_name: str):
        if not self._browser:
            return None
        for service in self._browser.devices.values():
            if service.friendly_name == device_name:
                return service
        return None

    def _ensure_device(
        self,
        device_name: str,
        force_rebuild: bool = False,
    ) -> pychromecast.Chromecast:
        if device_name not in self._devices:
            self._update_devices()
        cc = self._devices.get(device_name)
        if (
            not force_rebuild
            and cc is not None
            and cc.socket_client.is_alive()
        ):
            return cc

        service = self._find_service(device_name)
        if not service or not self._browser:
            raise RuntimeError(f"Device '{device_name}' is not available")

        log.info("[CAST] Rebuilding Chromecast handle for %s", device_name)
        cc = pychromecast.get_chromecast_from_cast_info(
            service,
            self._browser.zc,
            tries=None,
            retry_wait=5.0,
            timeout=_CONNECT_TIMEOUT_SECS,
        )
        self._devices[device_name] = cc
        return cc

    def _register_listeners(self, cc: pychromecast.Chromecast) -> None:
        if not getattr(cc, "_torrent_player_receiver_listener_registered", False):
            cc.register_status_listener(self)
            setattr(cc, "_torrent_player_receiver_listener_registered", True)
        if not getattr(cc, "_torrent_player_connection_listener_registered", False):
            cc.register_connection_listener(self)
            setattr(cc, "_torrent_player_connection_listener_registered", True)
        if not getattr(cc.media_controller, "_torrent_player_media_listener_registered", False):
            cc.media_controller.register_status_listener(self)
            setattr(cc.media_controller, "_torrent_player_media_listener_registered", True)

    def _ensure_connected(self, cc: pychromecast.Chromecast, device_name: str) -> None:
        if not cc.socket_client.is_alive():
            log.info("[CAST] Starting socket client for %s", device_name)

        log.info("[CAST] Connecting to %s", device_name)
        cc.wait(timeout=_CONNECT_TIMEOUT_SECS)
        if not cc.socket_client.is_connected:
            raise RuntimeError(f"Socket connection to '{device_name}' did not become ready")

    def _ensure_default_receiver(self, cc: pychromecast.Chromecast, device_name: str) -> None:
        status = cc.status
        if status and status.app_id == APP_MEDIA_RECEIVER and status.transport_id:
            log.info(
                "[CAST] Receiver already ready device=%s transport_id=%s",
                device_name,
                status.transport_id,
            )
            return

        log.info("[CAST] Launching default media receiver on %s", device_name)
        cc.start_app(APP_MEDIA_RECEIVER)

        deadline = time.time() + _RECEIVER_TIMEOUT_SECS
        while time.time() < deadline:
            status = cc.status
            if status and status.app_id == APP_MEDIA_RECEIVER and status.transport_id:
                log.info(
                    "[CAST] Receiver launched device=%s transport_id=%s",
                    device_name,
                    status.transport_id,
                )
                return
            try:
                cc.socket_client.receiver_controller.update_status()
            except Exception as exc:
                log.debug("[CAST] Receiver status refresh failed for %s: %s", device_name, exc)
            time.sleep(_STATUS_WAIT_STEP_SECS)

        raise TimeoutError(f"Timed out waiting for receiver launch on '{device_name}'")

    def _launch_media(
        self,
        cc: pychromecast.Chromecast,
        device_name: str,
        url: str,
        content_type: str,
        title: str,
        current_time: float | None,
    ) -> None:
        mc = cc.media_controller
        previous_session_id = getattr(mc.status, "media_session_id", None)
        log.info(
            "[CAST] Launching media device=%s url=%s content_type=%s start_time=%s",
            device_name,
            url,
            content_type,
            current_time,
        )
        mc.play_media(
            url,
            content_type,
            title=title,
            autoplay=True,
            stream_type="BUFFERED",
            current_time=current_time,
        )

        if not self._wait_for_media_session(mc, url, previous_session_id, _MEDIA_SESSION_TIMEOUT_SECS):
            raise TimeoutError(f"Timed out waiting for media session on '{device_name}'")

        log.info(
            "[CAST] Media session created device=%s session=%s content_id=%s",
            device_name,
            getattr(mc.status, "media_session_id", None),
            getattr(mc.status, "content_id", None),
        )

    def _wait_for_media_session(
        self,
        mc,
        expected_url: str,
        previous_session_id: Optional[int],
        timeout: float,
    ) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                mc.update_status()
            except Exception as exc:
                log.debug("[CAST] Media status refresh failed: %s", exc)

            status = mc.status
            session_id = getattr(status, "media_session_id", None)
            content_id = getattr(status, "content_id", None)
            player_state = getattr(status, "player_state", None)
            if session_id and (
                content_id == expected_url
                or session_id != previous_session_id
                or player_state in {"BUFFERING", "PLAYING", "PAUSED"}
            ):
                return True

            time.sleep(_STATUS_WAIT_STEP_SECS)

        return False

    def _ensure_active_controller(self, action: str):
        with self._session_lock:
            cc = self._current_cast
            session = self._session

        if not cc or not session:
            log.warning("[CAST] Ignoring %s without an active cast session", action)
            return None, None

        try:
            self._ensure_connected(cc, session.device_name)
        except Exception as exc:
            log.warning(
                "[CAST] Reconnect required before %s on %s: %s",
                action,
                session.device_name,
                exc,
            )
            cc = self._recover_current_cast(session)
            if not cc:
                return None, None

        if getattr(cc.media_controller.status, "media_session_id", None):
            return cc.media_controller, session

        if self._wait_for_media_session(
            cc.media_controller,
            session.url,
            getattr(cc.media_controller.status, "media_session_id", None),
            2.0,
        ):
            return cc.media_controller, session

        log.info(
            "[CAST] Media session missing on %s; restoring at %.1fs",
            session.device_name,
            self._last_known_time,
        )
        try:
            self._ensure_default_receiver(cc, session.device_name)
            self._launch_media(
                cc,
                session.device_name,
                session.url,
                session.content_type,
                session.title,
                current_time=max(0.0, self._last_known_time),
            )
        except Exception as exc:
            log.error("[CAST] Failed to restore media session on %s: %s", session.device_name, exc)
            return None, None

        return cc.media_controller, session

    def _recover_current_cast(self, session: _CastSession) -> Optional[pychromecast.Chromecast]:
        try:
            cc = self._ensure_device(session.device_name)
            self._register_listeners(cc)
            self._ensure_connected(cc, session.device_name)
        except Exception as exc:
            log.warning(
                "[CAST] Initial reconnect attempt failed for %s: %s",
                session.device_name,
                exc,
            )
            try:
                cc = self._ensure_device(session.device_name, force_rebuild=True)
                self._register_listeners(cc)
                self._ensure_connected(cc, session.device_name)
            except Exception as rebuild_exc:
                log.error(
                    "[CAST] Failed to reconnect to %s after rebuild: %s",
                    session.device_name,
                    rebuild_exc,
                )
                return None

        with self._session_lock:
            self._current_cast = cc

        log.info("[CAST] Reconnected to %s", session.device_name)
        return cc

    def _refresh_status_async(self) -> None:
        def worker() -> None:
            with self._session_lock:
                cc = self._current_cast
            if not cc:
                return
            try:
                cc.socket_client.receiver_controller.update_status()
                cc.media_controller.update_status()
            except Exception as exc:
                log.debug("[CAST] Status refresh after reconnect failed: %s", exc)

        threading.Thread(target=worker, name="CastStatusRefresh", daemon=True).start()

    def _start_status_poller(self) -> None:
        with self._session_lock:
            if self._status_poll_thread and self._status_poll_thread.is_alive():
                return
            self._status_poll_stop.clear()
            self._status_poll_thread = threading.Thread(
                target=self._status_poll_loop,
                name="CastStatusPoller",
                daemon=True,
            )
            self._status_poll_thread.start()

    def _stop_status_poller(self) -> None:
        self._status_poll_stop.set()
        thread = self._status_poll_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._status_poll_thread = None

    def _status_poll_loop(self) -> None:
        while not self._status_poll_stop.wait(_STATUS_POLL_INTERVAL_SECS):
            with self._session_lock:
                cc = self._current_cast
                session = self._session

            if not cc or not session:
                return

            if not cc.socket_client.is_connected:
                continue

            try:
                cc.socket_client.receiver_controller.update_status()
                cc.media_controller.update_status()
            except Exception as exc:
                log.debug("[CAST] Poll status refresh failed for %s: %s", session.device_name, exc)
