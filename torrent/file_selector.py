"""
torrent/file_selector.py — Detect and expose video files inside a torrent.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List

log = logging.getLogger("TORRENT")

VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {".mp4", ".mkv", ".avi", ".webm", ".mov", ".m4v", ".ts", ".flv"}
)


@dataclass
class FileInfo:
    """Metadata for a single file inside a torrent."""
    index: int          # libtorrent file index
    name: str           # display name (basename)
    path: str           # full relative path inside torrent
    size: int           # bytes
    abs_path: str = ""  # absolute path (set after download_path is known)

    @property
    def size_mb(self) -> float:
        return self.size / (1024 * 1024)

    @property
    def size_gb(self) -> float:
        return self.size / (1024 ** 3)

    def human_size(self) -> str:
        if self.size >= 1024 ** 3:
            return f"{self.size_gb:.2f} GB"
        if self.size >= 1024 ** 2:
            return f"{self.size_mb:.1f} MB"
        return f"{self.size / 1024:.0f} KB"

    def is_video(self) -> bool:
        return Path(self.path).suffix.lower() in VIDEO_EXTENSIONS


def detect_video_files(torrent_info, download_path: str) -> List[FileInfo]:
    """
    Return a list of :class:`FileInfo` objects for every video file
    found in *torrent_info*.

    Parameters
    ----------
    torrent_info:
        A ``libtorrent.torrent_info`` object (already populated).
    download_path:
        The root directory where the torrent is being saved.
    """
    files: List[FileInfo] = []
    storage = torrent_info.files()
    num_files = storage.num_files()

    for i in range(num_files):
        rel_path = storage.file_path(i)
        size = storage.file_size(i)
        ext = Path(rel_path).suffix.lower()
        if ext in VIDEO_EXTENSIONS:
            abs_path = str(Path(download_path) / rel_path)
            info = FileInfo(
                index=i,
                name=Path(rel_path).name,
                path=rel_path,
                size=size,
                abs_path=abs_path,
            )
            files.append(info)
            log.debug("Found video file [%d]: %s  (%s)", i, info.name, info.human_size())

    log.info("Detected %d video file(s) in torrent", len(files))
    return files
