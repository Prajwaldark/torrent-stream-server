# Torrent Stream Server

A lightweight native desktop app that serves torrent video files over the network while downloading them. It acts as a dedicated torrent-to-HTTP streaming server, allowing you to cast videos directly to a Chromecast or play them locally via VLC/MPV without needing to download the entire file first.

Built with **Python 3.12+**, **PySide6** (Qt6), and **libtorrent**.

---

## Features

| Feature | Status |
|---|---|
| Magnet link & `.torrent` file input | ✅ |
| Sequential piece downloading | ✅ |
| Smart buffer gate & playhead prioritisation | ✅ |
| Auto video file detection (mp4/mkv/avi/webm) | ✅ |
| Multi-file torrent selector | ✅ |
| Seamless Chromecast Discovery & Casting | ✅ |
| Stream to VLC / MPV via LAN HTTP Stream | ✅ |
| Hardware-accelerated streaming | ✅ |
| Seek support with dynamic reprioritisation | ✅ |
| Cache auto-cleanup on exit | ✅ |
| Background performance diagnostics | ✅ |

---

## Quick Start (Windows)

### Option A — Automated setup (Recommended)

Just run the setup script to automatically create a virtual environment, upgrade pip, and install all dependencies:

```bat
cd torrent-player
setup.bat
```

Run the application:
```bat
.\.venv\Scripts\python main.py
```

### Option B — Manual Installation

```bash
# 1. Create virtual environment
python -m venv .venv

# 2. Activate it (Windows)
.venv\Scripts\activate
# For Linux/macOS use: source .venv/bin/activate

# 3. Install Python packages
pip install -r requirements.txt

# 4. Run
python main.py
```

---

## How to Reuse & Integrate

This project acts as an excellent foundation for any Python application requiring torrent streaming. The architecture cleanly separates the UI, the torrent engine, and the HTTP streaming server.

### Key Components to Reuse:
- **`torrent/session.py`**: The `TorrentWorker` class manages the `libtorrent` session in a dedicated QThread. It handles adding magnets/torrents, selecting files, and emitting stats.
- **`torrent/prioritizer.py` & `buffering.py`**: Handles piece prioritization to ensure sequential downloading around the playhead, which is crucial for smooth playback.
- **`streaming/http_server.py` & `source.py`**: An HTTP server that serves bytes from the partially downloaded file. It translates HTTP Range requests into libtorrent piece wait events.
- **`utils/cast.py`**: A robust `CastManager` that handles Chromecast discovery, connection, playback control, and status polling.

### Example: Running headless stream server
You can rip out the HTTP server and torrent worker to build a headless service. The core stream server (`streaming/http_server.py`) can bind to `0.0.0.0` and serve video to any LAN client:
```python
from streaming.source import StreamSource
from streaming.http_server import StreamServer

# Assume 'source' is hooked up to a TorrentWorker
server = StreamServer(source, bind_all=True)
ip, port, url = server.start()
print(f"Streaming at {url}")
```

---

## Troubleshooting

### `libtorrent` not found
Try the alternate package name:
```bash
pip install python-libtorrent
```
Or install via conda:
```bash
conda install -c conda-forge libtorrent
```

### `mpv` / `libmpv` not found
If you plan to use MPV integration (optional), `python-mpv` links against `libmpv-2.dll` (Windows) or `libmpv.so` (Linux) at runtime.
**Windows**: place `mpv-2.dll` next to `main.py`, or ensure `mpv` is on PATH.  
**Linux**: `sudo apt install libmpv-dev` or `sudo pacman -S mpv`.

### Check all imports without launching the UI
```bash
python main.py --check
```

---

## Configuration

Settings are saved automatically at `%APPDATA%\TorrentStreamPlayer\settings.json` (Windows)
or `~/.config/TorrentStreamPlayer/settings.json` (Linux/macOS).

| Setting | Default | Description |
|---|---|---|
| `startup_buffer_mb` | `16` | MB to buffer before the stream server starts serving data |
| `buffer_mb` | `96` | Preferred rolling playback buffer after startup |
| `keep_on_exit` | `false` | Keep downloaded files in cache after app close |
| `sequential` | `true` | Force sequential piece downloading |
| `bind_all_interfaces`| `true` | HTTP server binds to 0.0.0.0 for LAN streaming and Chromecast |

---

## Project Structure

```text
torrent-player/
├── main.py                  # Entry point
├── requirements.txt         # Python dependencies
├── setup.bat                # Windows setup script
├── ui/
│   └── main_window.py       # Main orchestration layer & UI
├── torrent/
│   ├── session.py           # libtorrent QThread worker
│   ├── buffering.py         # Buffer readiness logic
│   ├── prioritizer.py       # Piece prioritization engine
│   └── file_selector.py     # Video file detection
├── streaming/
│   ├── http_server.py       # HTTP Range-request stream server
│   ├── source.py            # Stream source / disk reader
│   └── piece_waiter.py      # Blocking waiter for downloaded pieces
├── cache/
│   └── cleanup.py           # Cache lifecycle and deletion
└── utils/
    ├── cast.py              # Chromecast discovery & control
    ├── settings.py          # App config manager
    ├── logger.py            # Structured logging
    └── external_player.py   # VLC/MPV launch wrappers
```

---

## Logs

Logs are written to standard output. When running `main.py`, look at the console for detailed `INFO` and `DEBUG` events, including background performance diagnostics and startup metrics.
