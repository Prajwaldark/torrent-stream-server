# Torrent Streaming Player

A lightweight native desktop app that streams torrent video files while downloading — no browser, no Electron.

Built with **Python 3.12+**, **PySide6** (Qt6), **libtorrent**, and **embedded MPV**.

---

## Features

| Feature | Status |
|---|---|
| Magnet link & `.torrent` file input | ✅ |
| Sequential piece downloading | ✅ |
| Smart buffer gate (configurable MB) | ✅ |
| Auto video file detection (mp4/mkv/avi/webm) | ✅ |
| Multi-file torrent selector | ✅ |
| MPV embedded directly in Qt window | ✅ |
| Hardware-accelerated playback | ✅ |
| Seek with piece reprioritisation | ✅ |
| Subtitle track switching | ✅ |
| Cache auto-cleanup on exit | ✅ |
| "Keep downloaded file" option | ✅ |
| Configurable buffer size | ✅ |

---

## Quick Start (Windows)

### Option A — Automated setup

```bat
cd torrent-player
setup.bat
```

### Option B — Manual

```bash
# 1. Create virtual environment
python -m venv .venv
.venv\Scripts\activate

# 2. Install Python packages
pip install -r requirements.txt

# 3. Install mpv (pick one)
winget install mpv
# or download from https://mpv.io/installation/ and add to PATH
# or place mpv-2.dll / libmpv-2.dll next to main.py

# 4. Run
python main.py
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
`python-mpv` links against `libmpv-2.dll` (Windows) or `libmpv.so` (Linux) at runtime.

**Windows**: place `mpv-2.dll` next to `main.py`, or ensure `mpv` is on PATH.  
**Linux**: `sudo apt install libmpv-dev` or `sudo pacman -S mpv`.

### Check all imports without launching the UI
```bash
python main.py --check
```

---

## Configuration

Settings are saved automatically at `%APPDATA%\torrent-player\config.json` (Windows)
or `~/.config/torrent-player/config.json` (Linux/macOS).

| Setting | Default | Description |
|---|---|---|
| `buffer_mb` | `64` | MB to buffer before playback starts |
| `hwdec` | `"auto"` | MPV hardware decode mode |
| `keep_on_exit` | `false` | Keep downloaded file on app close |
| `sequential` | `true` | Sequential piece download |
| `volume` | `80` | Default volume (0–100) |

---

## Project Structure

```
torrent-player/
├── main.py                  # Entry point
├── requirements.txt
├── setup.bat                # Windows one-click installer
├── ui/
│   ├── main_window.py       # Root window + state machine
│   ├── player_widget.py     # MPV render target widget
│   └── controls.py          # Playback controls bar
├── torrent/
│   ├── session.py           # libtorrent QThread worker
│   ├── buffering.py         # Buffer readiness monitor
│   ├── prioritizer.py       # Piece priority around playhead
│   └── file_selector.py     # Video file detection
├── player/
│   ├── mpv_player.py        # python-mpv wrapper
│   └── subtitles.py         # Subtitle track management
├── cache/
│   └── cleanup.py           # Temp dir + atexit cleanup
└── utils/
    ├── config.py            # App config (JSON-backed)
    └── logger.py            # Structured logging
```

---

## Logs

- **Windows**: `%LOCALAPPDATA%\torrent-player\app.log`
- **Linux/macOS**: `~/.local/share/torrent-player/app.log`
