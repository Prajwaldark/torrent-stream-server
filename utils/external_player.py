"""
utils/external_player.py — Utilities for launching external media players.
"""
import logging
import platform
import subprocess
import traceback

log = logging.getLogger("UI")


def launch_vlc(url: str) -> None:
    system = platform.system()
    try:
        if system == "Windows":
            subprocess.Popen(["vlc.exe", url])
        elif system == "Darwin":
            subprocess.Popen(["open", "-a", "VLC", url])
        else:
            subprocess.Popen(["vlc", url])
        log.info("Launched VLC with URL: %s", url)
    except Exception as exc:
        log.error("Failed to launch VLC: %s", exc, exc_info=True)
        raise


def launch_mpv(url: str) -> None:
    system = platform.system()
    try:
        if system == "Windows":
            subprocess.Popen(["mpv.exe", url])
        elif system == "Darwin":
            subprocess.Popen(["open", "-a", "mpv", url])
        else:
            subprocess.Popen(["mpv", url])
        log.info("Launched MPV with URL: %s", url)
    except Exception as exc:
        log.error("Failed to launch MPV: %s", exc, exc_info=True)
        raise
