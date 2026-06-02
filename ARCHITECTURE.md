# Torrent LAN Streaming Server Architecture

This document describes the current implementation in `C:\Projects\torrent-player` and a practical implementation plan based on the code as it exists today.

## Current Architecture

The application is a dedicated torrent-to-network streaming server built on three main layers:

1. `main.py` bootstraps logging, loads persisted config, creates the cache manager, and starts the Qt application.
2. `ui/main_window.py` is the orchestration layer. It owns the UI state machine, displays stream URLs/QR codes, and wires together torrenting, buffering, and HTTP streaming.
3. `torrent/session.py` runs libtorrent in a dedicated `QThread` and emits metadata, stats, and piece-completion signals back to the UI.
4. `streaming/*` turns a partially downloaded torrent file into a safe HTTP stream that external media players (VLC, MPV, Smart TVs) can consume via LAN without reading sparse or missing bytes directly from disk.

The current runtime object graph is:

- `MainWindow`
- `TorrentWorker` inside a `QThread`
- `BufferMonitor`
- `SeekPrioritizer`
- `PieceWaiter`
- `StreamSource`
- `StreamServer` created lazily when the stream is ready
- `CacheManager`
- `AppConfig`

The intended state progression in `MainWindow` is:

- `idle`
- fetching metadata
- selecting file
- buffering
- streaming (server ready)

Important design choice: The application no longer embeds a media player. Playback is entirely delegated to external players or network devices, which request data via HTTP. The HTTP server only serves bytes whose backing pieces are already downloaded.

## Folder Structure

Relevant project structure, excluding `.venv` and `__pycache__`:

```text
torrent-player/
|-- main.py
|-- README.md
|-- requirements.txt
|-- setup.bat
|-- cache/
|   |-- cleanup.py
|   `-- __init__.py
|-- streaming/
|   |-- http_server.py
|   |-- piece_waiter.py
|   |-- source.py
|   `-- __init__.py
|-- torrent/
|   |-- buffering.py
|   |-- file_selector.py
|   |-- prioritizer.py
|   |-- session.py
|   `-- __init__.py
|-- ui/
|   |-- main_window.py
|   `-- __init__.py
`-- utils/
    |-- config.py
    |-- external_player.py
    |-- logger.py
    |-- network.py
    `-- __init__.py
```

Role by folder:

- `ui/`: main window displaying URLs, QR Code, and stats.
- `torrent/`: libtorrent session management, file detection, initial buffer readiness, moving piece priorities.
- `streaming/`: loopback/LAN HTTP server, blocking piece wait logic, safe file reads.
- `cache/`: temporary download directory lifecycle.
- `utils/`: persisted config, logging bootstrap, networking, and external player launching.

## Streaming Flow

1. `main.py` loads `AppConfig`, creates `CacheManager`, starts `QApplication`, then shows `MainWindow`.
2. The user enters a magnet link or `.torrent` path and triggers `_on_stream()`.
3. `_on_stream()` resets UI state, refreshes the cache directory, applies the configured buffer size, starts the torrent thread if needed, and adds the torrent to `TorrentWorker`.
4. When metadata arrives, `TorrentWorker` emits `metadata_ready(files)`.
5. `MainWindow` either auto-selects the only video file or shows a file-picker dialog for multi-file torrents.
6. `_select_file()` tells libtorrent to focus on that file, attaches `BufferMonitor`, attaches `SeekPrioritizer`, and attaches `StreamSource`.
7. `SeekPrioritizer.on_seek_bytes(0)` pushes the initial priority window to the start of the selected file.
8. A 500 ms buffer timer polls `BufferMonitor` and updates the progress bar.
9. Once `BufferMonitor.is_ready()` returns true, `_start_streaming()` starts `StreamServer`, gets Localhost/LAN URLs, generates a QR Code, and reveals the Streaming Panel.
10. Users can click "Open in VLC/MPV", copy the URL, or scan the QR Code on their phone.
11. External clients connect to the HTTP server. Range requests trigger `StreamSource.notify_seek()`, aligning the prioritizer's critical window with the client's playhead.

## Torrent Flow

1. `TorrentWorker._ensure_session()` creates a libtorrent session with DHT, LSD, UPnP, NAT-PMP, and optional rate limits.
2. For magnets, `add_magnet()` parses the URI, sets `save_path`, enables sequential download, adds the torrent, and waits for metadata.
3. For `.torrent` files, `add_torrent_file()` creates `torrent_info` immediately, adds the torrent, and emits metadata without waiting for magnet resolution.
4. The worker loop runs every 200 ms in `run()`.
5. On each tick it:
   - pops libtorrent alerts
   - handles metadata alerts
   - emits `piece_finished` for completed pieces
   - emits torrent errors
   - emits periodic stats
6. `detect_video_files()` filters the torrent file list by known video extensions and builds `FileInfo` records with absolute cache paths.
7. Once the UI chooses a file, `select_file()` sets file priorities so the selected file is `7` and all others are `0`.
8. Pause/resume operate on the current torrent handle.
9. Cancel removes the torrent from the session and clears worker state.

Current behavior is a hybrid of file-level selection plus piece-level reprioritization:

- non-selected files are skipped entirely
- the selected file stays active
- piece priorities within the selected file are moved around the playhead window

## Buffering Logic

`torrent/buffering.py` implements startup buffering.

Current algorithm:

1. `BufferMonitor.attach()` stores the selected file geometry.
2. Target buffer size is `min(config.buffer_bytes, file_size)`.
3. Every 500 ms `MainWindow._poll_buffer()` asks for:
   - `buffer_percent()`
   - `buffer_bytes_downloaded()`
   - `is_ready()`
4. `BufferMonitor` uses `handle.status().total_wanted_done` as its byte counter.
5. Playback starts once downloaded bytes are greater than or equal to the target buffer.

What this gives the app:

- a simple startup gate
- sub-piece progress visibility through libtorrent status
- a single progress number for the UI

What supplements it:

- `StreamSource.buffered_ranges()` scans piece availability for the selected file and gives the UI normalized buffered segments for the seek bar
- `_is_time_buffered()` converts a target playback time into a byte offset and checks `StreamSource.have_byte()`

Important limitation: the startup gate measures downloaded bytes for the selected file, not guaranteed contiguous bytes from the head of the file.

## Prioritization Logic

`torrent/prioritizer.py` forces libtorrent into a streaming-friendly queue model.

Priority tiers:

- `PRIO_SKIP = 0`: pieces outside the selected file
- `PRIO_LOW = 1`: selected-file baseline outside active windows
- `PRIO_MID = 3`: mid-range throughput window
- `PRIO_PREFETCH = 6`: next up after critical pieces
- `PRIO_CRITICAL = 7`: pieces directly ahead of the playhead

Current window sizes:

- critical window: `64 MB`
- prefetch window: `192 MB`
- mid window: `768 MB`

How it works:

1. `attach()` records file offset, size, piece size, first piece, last piece, and an estimated bitrate.
2. The first `on_seek()` or `on_seek_bytes()` call publishes a full baseline with `prioritize_pieces()`.
3. Later updates use delta logic:
   - calculate new window boundaries
   - compare boundary movements to the previous window
   - update only pieces whose tier changed
4. Duration updates from MPV refine the bitrate estimate so time-to-byte mapping becomes more accurate.
5. The UI drives reprioritization from three events:
   - initial file selection
   - user seeks
   - periodic playback tick every 4 seconds
6. The HTTP server can also call `StreamSource.notify_seek()` when it sees a large discontinuous range request.

This design exists to keep the swarm focused on the bytes MPV is likely to request next while still leaving enough mid-range work queued for good throughput.

## HTTP Streaming Logic

The HTTP layer is implemented by `streaming/http_server.py`, `streaming/source.py`, and `streaming/piece_waiter.py`.

Server behavior:

1. `StreamServer.start()` binds `ThreadingHTTPServer` to `127.0.0.1` on an ephemeral port.
2. MPV is pointed at `http://127.0.0.1:<port>/video`.
3. The handler supports `HEAD` and `GET`.
4. It parses a single `Range` header and returns:
   - `200 OK` for whole-file responses
   - `206 Partial Content` for valid ranges
   - `416` for invalid or unsatisfiable ranges
5. It sets `Accept-Ranges: bytes`, `Content-Length`, `Content-Range` where needed, `Cache-Control: no-store`, and `Connection: keep-alive`.

Seek detection:

- the server tracks `last_end_byte`
- if a new request jumps by more than `4 MB`, it treats that as a real seek
- real seeks call `StreamSource.notify_seek(start_byte)`

Byte serving:

1. `StreamSource.read_range(start, end)` opens the selected file path directly.
2. Reads are cut at piece boundaries and further limited to `256 KB` chunks.
3. Before reading a chunk, `StreamSource` checks which torrent piece backs the current file offset.
4. If the piece is not downloaded, `_wait_for_piece()` blocks on `PieceWaiter.wait_for()`.
5. `PieceWaiter` is woken by `TorrentWorker.piece_finished`, which is connected to `PieceWaiter.piece_done()`.
6. Once the piece exists, the HTTP thread reads from disk and writes the bytes to the client socket.

The key invariant in the current design is:

- if a byte is served, its backing piece was reported as available by libtorrent at read time

That invariant is what prevents MPV from reading sparse or invalid regions directly from the partially downloaded file.

## Current Bugs

These are current code-level bugs or architectural defects visible in the implementation today.

1. `TorrentWorker` is moved to a background `QThread`, but many of its methods are still called directly from the UI thread.
   - `add_magnet()`
   - `add_torrent_file()`
   - `select_file()`
   - `pause_torrent()`
   - `resume_torrent()`
   - `cancel()`
   - `get_handle()`
   - `get_torrent_info()`
   This breaks the intended thread boundary and risks races between the UI and libtorrent alert loop.

2. Startup buffering does not prove the beginning of the file is contiguous.
   `BufferMonitor` uses `total_wanted_done`, which measures total downloaded bytes for wanted data, not specifically "all bytes from file offset 0 through buffer target are present". Playback can start with enough aggregate bytes but still stall immediately if head pieces are missing.

3. `AppConfig.connections_limit` and `AppConfig.sequential` are defined but not actually applied.
   The session setup never uses `connections_limit`, and the worker always forces sequential mode regardless of the config flag.

4. HTTP seek detection is global per server, not per client stream.
   `last_end_byte` is shared across handler threads, so concurrent or overlapping MPV requests can be misclassified as seeks and churn piece priorities unnecessarily.

5. `PieceWaiter.wait_for()` decrements timeout in fixed 1-second steps after every wakeup.
   Frequent notifications can consume the timeout budget faster than real elapsed wall-clock time.

6. `StreamSource.read_range()` can busy-loop when libtorrent reports a piece as complete but the file contents are not flushed yet.
   In that case `fh.read()` can return empty, `_wait_for_piece()` succeeds immediately, and the loop retries without any backoff.

7. Documentation and defaults are inconsistent.
   `README.md` lists a default `buffer_mb` of `64`, while `AppConfig` currently defaults to `32`.

8. `player/subtitles.py` appears to be unused duplicate logic.
   Subtitle handling currently lives in `MpvPlayer`, so the standalone subtitle manager is either dead code or an unfinished abstraction.

## TODO List

Implementation plan, ordered by impact:

1. Fix thread ownership around libtorrent.
   Route all torrent mutations and handle reads through queued Qt signals/slots or a thread-safe command queue. Do not let `MainWindow` access libtorrent handles directly.

2. Replace the startup buffer gate with a contiguous-head check.
   Measure whether the first `N` bytes of the selected file are actually backed by available pieces, while still exposing sub-piece progress for the UI.

3. Harden `StreamSource.read_range()` for flush lag and detach races.
   Add wall-clock timeout accounting, short sleep or condition wait on empty reads, and stronger locking around detach/attach transitions.

4. Make HTTP seek detection per request stream instead of global.
   Track position by client socket or request sequence so speculative MPV range probes do not reshuffle priorities incorrectly.

5. Apply all config values consistently.
   Wire in `connections_limit`, honor the `sequential` flag, and document the actual current defaults.

6. Add automated tests for pure logic modules.
   Start with:
   - range parsing in `http_server.py`
   - piece-window math in `prioritizer.py`
   - timeout logic in `piece_waiter.py`
   - file detection in `file_selector.py`
   - buffered-range calculations in `source.py`

7. Add integration tests or harnesses around the orchestration flow.
   Focus on:
   - magnet to metadata flow
   - multi-file selection
   - cancel during buffering
   - seek into unbuffered regions
   - playback restart after stop/end

8. Improve observability for stalls.
   Log why playback is stalled, which piece is being waited on, current priority window boundaries, and whether the wait is due to missing piece data or file flush lag.

9. Remove or integrate dead abstractions.
   Either delete `player/subtitles.py` or make it the single subtitle-management path.

10. Clean up documentation and operational consistency.
    Update `README.md`, sync config defaults, document the HTTP loopback architecture clearly, and describe current known limitations explicitly.

11. Consider exposing richer buffering UX.
    Show "startup buffer", "playhead buffered ahead", and "seek target pending" as separate signals instead of one generic progress bar and waiting state.

12. Revisit lifecycle boundaries.
    Make session reset, stream-server teardown, cache cleanup, and playback restart paths explicit and testable instead of being spread across `_on_stream()`, `_on_cancel()`, and `closeEvent()`.
