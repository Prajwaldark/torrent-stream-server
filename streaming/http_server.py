"""
streaming/http_server.py — Localhost HTTP front-end for StreamSource.

A ``ThreadingHTTPServer`` bound to ``127.0.0.1:<ephemeral>``. Exposes a
single endpoint, ``/video``, that MPV opens with Range requests. The
handler streams bytes from :class:`StreamSource`, which blocks until the
underlying pieces are downloaded — so MPV never sees zero-padded sparse
regions.

The server is loopback-only (never bound to 0.0.0.0) — there is no auth
in front of it and we do not want LAN clients pulling torrent data.
"""
from __future__ import annotations

import logging
import mimetypes
import os
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple

from streaming.source import StreamSource

log = logging.getLogger("HTTP")

ENDPOINT_PATH = "/video"

# Threshold for treating a Range request as a "real" seek. Below this
# distance from the previous served position, treat it as continuation
# and don't churn the prioritizer's window.
SEEK_THRESHOLD_BYTES = 4 * 1024 * 1024


def guess_content_type(file_path: str) -> str:
    mime, _ = mimetypes.guess_type(file_path)
    if mime and mime != "application/octet-stream":
        return mime
    # Common containers mimetypes might miss
    ext = os.path.splitext(file_path)[1].lower()
    return {
        ".mkv": "video/x-matroska",
        ".webm": "video/webm",
        ".m4v": "video/mp4",
        ".ts": "video/mp2t",
        ".flv": "video/x-flv",
        ".mp4": "video/mp4",
        ".avi": "video/x-msvideo",
    }.get(ext, "video/mp4")


def _parse_range(header: str, total: int) -> Optional[Tuple[int, int]]:
    """
    Parse a single-range ``Range: bytes=START-END`` header.

    Returns inclusive (start, end) byte indices clamped to the file, or
    None if the header is missing/malformed/unsatisfiable.
    """
    if not header:
        return None
    header = header.strip()
    if not header.lower().startswith("bytes="):
        return None
    spec = header[6:].split(",", 1)[0].strip()
    if "-" not in spec:
        return None
    start_s, end_s = spec.split("-", 1)
    try:
        if start_s == "":
            # Suffix range: bytes=-N → last N bytes
            suffix = int(end_s)
            if suffix <= 0:
                return None
            start = max(0, total - suffix)
            end = total - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else total - 1
    except ValueError:
        return None
    if start < 0 or start >= total:
        return None
    end = min(end, total - 1)
    if end < start:
        return None
    return start, end


class _LoopbackThreadingServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with daemon request threads + reusable address."""

    allow_reuse_address = True
    daemon_threads = True
    # Override socket address family / etc. handled by base class.

    # Track the StreamSource so handlers can reach it without globals
    source: Optional[StreamSource] = None
    # Last byte offset we finished serving — used by handlers to detect seeks
    last_end_byte: int = -1
    # Lock guarding last_end_byte updates between concurrent handlers
    state_lock = threading.Lock()
    
    # Tracking active viewers
    active_viewers: int = 0
    viewers_lock = threading.Lock()

    # Throttling limit (bytes per second, 0 = unlimited)
    throttle_rate: int = 0

    # Total connections/reconnect counts
    reconnect_count: int = 0

    def handle_error(self, request, client_address):
        import sys
        exc_type, exc_value, _ = sys.exc_info()
        if exc_type and issubclass(exc_type, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            log.info("[HTTP] Client disconnected during stream read client=%s", client_address[0])
            return
        try:
            super().handle_error(request, client_address)
        except Exception:
            pass

class _StreamHandler(BaseHTTPRequestHandler):
    # Quiet default access log — we'll log via our own logger
    def log_message(self, fmt: str, *args) -> None:  # type: ignore[override]
        log.debug("HTTP %s - " + fmt, self.address_string(), *args)

    def handle(self) -> None:
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError) as exc:
            client_ip = self.client_address[0] if self.client_address else "unknown"
            log.info("[HTTP] Client disconnected during stream read client=%s (%s)", client_ip, str(exc))
        except OSError as exc:
            client_ip = self.client_address[0] if self.client_address else "unknown"
            if getattr(exc, "winerror", None) == 10054 or exc.errno == 10054 or "10054" in str(exc):
                log.info("[HTTP] Client disconnected during stream read client=%s (WinError 10054)", client_ip)
            else:
                log.debug("[HTTP] OSError in request handler client=%s: %s", client_ip, exc)
        except Exception as exc:
            log.debug("[HTTP] General exception in request handler: %s", exc)

    # ------------------------------------------------------------------ #
    #  Request dispatch                                                    #
    # ------------------------------------------------------------------ #

    def do_HEAD(self) -> None:
        try:
            self._serve(head_only=True)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, OSError) as exc:
            if isinstance(exc, OSError) and not isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
                if getattr(exc, "winerror", None) != 10054 and exc.errno != 10054 and "10054" not in str(exc):
                    raise
            log.info("[HTTP] Client disconnected during stream read")

    def do_GET(self) -> None:
        server: _LoopbackThreadingServer = self.server  # type: ignore[assignment]
        if self.path == "/" or self.path == "":
            try:
                self._serve_landing_page()
            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, OSError) as exc:
                if isinstance(exc, OSError) and not isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
                    if getattr(exc, "winerror", None) != 10054 and exc.errno != 10054 and "10054" not in str(exc):
                        raise
                log.info("[HTTP] Client disconnected during stream read")
            return

        with server.viewers_lock:
            server.active_viewers += 1
        try:
            self._serve(head_only=False)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, OSError) as exc:
            if isinstance(exc, OSError) and not isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
                if getattr(exc, "winerror", None) != 10054 and exc.errno != 10054 and "10054" not in str(exc):
                    raise
            log.info("[HTTP] Client disconnected during stream read")
        finally:
            with server.viewers_lock:
                server.active_viewers = max(0, server.active_viewers - 1)

    def _serve_landing_page(self) -> None:
        server: _LoopbackThreadingServer = self.server  # type: ignore[assignment]
        host = self.headers.get("Host", "127.0.0.1")
        video_url = f"http://{host}{ENDPOINT_PATH}"
        vlc_intent = f"intent://{host}{ENDPOINT_PATH}#Intent;package=org.videolan.vlc;type=video/*;scheme=http;end"
        mx_intent = f"intent://{host}{ENDPOINT_PATH}#Intent;package=com.mxtech.videoplayer.ad;type=video/*;scheme=http;end"

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TorrentStream</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #121212; color: #fff; padding: 20px; text-align: center; margin: 0; }}
        h1 {{ margin-top: 10px; color: #1e90ff; }}
        .warning {{ background: #3a2a00; color: #ffb74d; border: 1px solid #ff9800; border-radius: 8px; padding: 15px; margin: 20px auto; max-width: 500px; text-align: left; font-size: 14px; }}
        .btn {{ display: inline-block; background: #1e90ff; color: #fff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; margin: 10px; font-size: 16px; width: 80%; max-width: 300px; }}
        .btn-vlc {{ background: #ff8800; }}
        .btn-mx {{ background: #0088cc; }}
        .btn-browser {{ background: #333; border: 1px solid #555; }}
        .stats {{ color: #aaa; margin-top: 30px; font-size: 14px; }}
    </style>
</head>
<body>
    <h1>⚡ TorrentStream</h1>
    <p>Stream is active and ready.</p>
    
    <div class="warning">
        <strong>⚠️ Browser Limitations</strong><br>
        Browsers have poor native support for MKV files, multiple audio tracks, and advanced subtitles. For the best experience, open the stream in a dedicated media player app.
    </div>

    <a href="{vlc_intent}" class="btn btn-vlc">Open in VLC (Android)</a>
    <a href="{mx_intent}" class="btn btn-mx">Open in MX Player (Android)</a>
    <a href="{video_url}" class="btn btn-browser">Play in Browser</a>
    
    <div class="stats">
        Active Viewers: {server.active_viewers}<br>
        Stream URL: <br>
        <input type="text" value="{video_url}" readonly style="width: 80%; max-width: 300px; padding: 8px; margin-top: 10px; background: #222; color: #fff; border: 1px solid #444; border-radius: 4px; text-align: center;">
    </div>
</body>
</html>
"""
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve(self, head_only: bool) -> None:
        if self.path.split("?", 1)[0] != ENDPOINT_PATH:
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint")
            return


        server: _LoopbackThreadingServer = self.server  # type: ignore[assignment]
        source = server.source
        if source is None or not source.is_attached():
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE,
                            "Stream not ready")
            return

        total = source.file_size
        if total <= 0:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE,
                            "Empty stream")
            return

        content_type = guess_content_type(source.file_path)

        # ── Range header ─────────────────────────────────────────────
        range_header = self.headers.get("Range", "")
        parsed = _parse_range(range_header, total)

        if parsed is None and range_header:
            # Header was supplied but unparseable / unsatisfiable
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{total}")
            self.end_headers()
            return

        if parsed is None:
            # No Range → serve whole file (typical for HEAD probes)
            start, end = 0, total - 1
            status = HTTPStatus.OK
        else:
            start, end = parsed
            status = HTTPStatus.PARTIAL_CONTENT

        # Increment request count
        with server.state_lock:
            server.reconnect_count += 1
            reconnect_count = server.reconnect_count

        client_ip = self.client_address[0]
        # Log request diagnostics
        if range_header:
            log.info("[HTTP] Range request start=%d end=%d client=%s reconnect_count=%d", start, end, client_ip, reconnect_count)
        else:
            log.info("[HTTP] Request whole file client=%s reconnect_count=%d", client_ip, reconnect_count)

        # ── Seek detection ───────────────────────────────────────────
        with server.state_lock:
            prev_end = server.last_end_byte
        is_seek = (
            prev_end < 0
            or start > prev_end + SEEK_THRESHOLD_BYTES
            or start + SEEK_THRESHOLD_BYTES < prev_end
        )
        if is_seek and not head_only:
            log.debug("Range seek detected: prev_end=%d new_start=%d",
                      prev_end, start)
            source.notify_seek(start)

        # ── Response headers ─────────────────────────────────────────
        content_length = end - start + 1
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(content_length))
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header(
                    "Content-Range",
                    f"bytes {start}-{end}/{total}",
                )
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as exc:
            if isinstance(exc, OSError) and not isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
                if getattr(exc, "winerror", None) != 10054 and exc.errno != 10054 and "10054" not in str(exc):
                    raise
            log.info("[HTTP] Client disconnected during stream read")
            return

        if head_only:
            return

        # ── Stream body ──────────────────────────────────────────────
        import time
        sent = 0
        start_time = time.monotonic()
        try:
            for chunk in source.read_range(start, end):
                if not chunk:
                    continue
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as exc:
                    if isinstance(exc, OSError) and not isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
                        if getattr(exc, "winerror", None) != 10054 and exc.errno != 10054 and "10054" not in str(exc):
                            raise
                    log.info("[HTTP] Client disconnected during stream read (sent=%d/%d client=%s)", sent, content_length, client_ip)
                    return
                sent += len(chunk)
                with server.state_lock:
                    server.last_end_byte = start + sent - 1

                # Throttling
                if server.throttle_rate > 0:
                    expected_time = sent / server.throttle_rate
                    elapsed = time.monotonic() - start_time
                    if elapsed < expected_time:
                        time.sleep(expected_time - elapsed)

            log.info("[HTTP] Serve completed: start=%d end=%d client=%s sent=%d bytes_served=%d", start, end, client_ip, sent, sent)
        except Exception as exc:  # noqa: BLE001 — never crash request thread
            log.warning("read_range failed: %s", exc)


class StreamServer:
    """
    Owns the ThreadingHTTPServer thread. Starts on demand, gives back the
    URL MPV should open, and shuts cleanly when the user cancels or the
    app closes.
    """

    def __init__(self, source: StreamSource, bind_all: bool = False) -> None:
        self._source = source
        self._server: Optional[_LoopbackThreadingServer] = None
        self._thread: Optional[threading.Thread] = None
        self._host = "0.0.0.0" if bind_all else "127.0.0.1"
        self._port = 0
        self._throttle_rate = 0

    def set_throttle_rate(self, bytes_per_sec: int) -> None:
        self._throttle_rate = bytes_per_sec
        if self._server:
            self._server.throttle_rate = bytes_per_sec

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def start(self) -> Tuple[str, int, str]:
        """Bind to 127.0.0.1 on an ephemeral port and start serving."""
        if self._server is not None:
            return self._host, self._port, self.url

        server = _LoopbackThreadingServer(
            (self._host, 0),
            _StreamHandler,
        )
        server.source = self._source
        server.last_end_byte = -1
        server.throttle_rate = self._throttle_rate
        # Reading the bound port after the OS picks one for us
        self._port = server.server_address[1]
        self._server = server

        self._thread = threading.Thread(
            target=server.serve_forever,
            name="StreamServer",
            daemon=True,
        )
        self._thread.start()

        log.info("StreamServer listening on %s", self.url)
        return self._host, self._port, self.url

    def stop(self) -> None:
        if self._server is None:
            return
        log.info("StreamServer stopping…")
        try:
            self._server.shutdown()
        except Exception:
            pass
        try:
            self._server.server_close()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._server = None
        self._thread = None

    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self._host == "0.0.0.0" else self._host
        return f"http://{host}:{self._port}{ENDPOINT_PATH}"

    @property
    def active_viewers(self) -> int:
        if self._server:
            with self._server.viewers_lock:
                return self._server.active_viewers
        return 0

