"""
utils/config.py — Application configuration.

Stores all tuneable parameters. Loaded from / saved to a JSON file
so settings survive between sessions.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


def _default_cache_dir() -> str:
    import tempfile
    return str(Path(tempfile.gettempdir()) / "torrent_stream_cache")


def _config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path.home() / ".config"
    cfg_dir = base / "torrent-player"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    return cfg_dir / "config.json"


@dataclass
class AppConfig:
    # Buffering
    buffer_mb: int = 32
    """Minimum megabytes to buffer before playback starts."""

    # Download
    cache_dir: str = field(default_factory=_default_cache_dir)
    """Temporary directory used for in-progress downloads."""

    sequential: bool = True
    """Force sequential piece download order."""

    keep_on_exit: bool = False
    """If True, downloaded files are kept after the app closes."""

    bind_all_interfaces: bool = True
    """If True, HTTP server binds to 0.0.0.0 for LAN streaming."""

    # Torrent session tuning
    max_upload_rate: int = 0       # 0 = unlimited
    max_download_rate: int = 0     # 0 = unlimited
    connections_limit: int = 200

    # ------------------------------------------------------------------ #
    #  Persistence                                                         #
    # ------------------------------------------------------------------ #

    @classmethod
    def load(cls) -> "AppConfig":
        path = _config_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                # Only keep keys that exist in the dataclass to avoid errors
                # when old config files have stale keys.
                valid_keys = {f for f in cls.__dataclass_fields__}
                filtered = {k: v for k, v in data.items() if k in valid_keys}
                return cls(**filtered)
            except Exception:
                pass
        return cls()

    def save(self) -> None:
        path = _config_path()
        path.write_text(
            json.dumps(asdict(self), indent=2), encoding="utf-8"
        )

    @property
    def buffer_bytes(self) -> int:
        return self.buffer_mb * 1024 * 1024
