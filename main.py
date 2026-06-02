"""
main.py — Application entry point.

Usage:
    python main.py
    python main.py --check     (import check only, no UI)
"""
from __future__ import annotations

import argparse
import sys
import os

# Prepend the script directory to Windows PATH so that any local DLLs (like libtorrent)
# are automatically discovered by python bindings.
if sys.platform == "win32":
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.environ["PATH"] = script_dir + os.pathsep + os.environ["PATH"]


def _check_imports() -> bool:
    """Verify all required packages are importable before launching the UI."""
    import importlib
    errors = []
    required = {
        "PySide6": "pip install PySide6-Essentials",
        "libtorrent": "pip install libtorrent  (or python-libtorrent)",
        "qrcode": "pip install qrcode",
        "PIL": "pip install Pillow",
    }
    for module, hint in required.items():
        try:
            importlib.import_module(module)
        except ImportError:
            errors.append(f"  [X]  {module} - {hint}")

    if errors:
        print("Missing dependencies:\n" + "\n".join(errors))
        return False

    print("[OK] All imports OK")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Torrent Streaming Player")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check imports and exit without starting the UI",
    )
    args = parser.parse_args()

    if args.check:
        return 0 if _check_imports() else 1

    # ------------------------------------------------------------------ #
    #  Full startup                                                        #
    # ------------------------------------------------------------------ #

    from utils.logger import setup_logging
    setup_logging()

    import logging
    log = logging.getLogger(__name__)
    log.info("Starting Torrent Streaming Player…")

    import time
    t0 = time.time()
    from utils.settings import SettingsManager
    from cache.cleanup import CacheManager

    config = SettingsManager.load()
    cache = CacheManager(config.cache_dir)
    
    t1 = time.time()
    log.info("[STARTUP] Cache & Config init: %.3fs", t1 - t0)
    
    log.warning(
        "Performance Tip: If you experience lag, add exclusions in Windows Defender "
        "for the project folder (%%CD%%) and cache folder (%s) to prevent real-time "
        "disk scanning overhead during torrent downloads.",
        config.cache_dir
    )

    # Qt requires QApplication before any widget
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt

    t2 = time.time()
    app = QApplication(sys.argv)
    app.setApplicationName("TorrentStream")
    app.setOrganizationName("TorrentStream")
    t3 = time.time()
    log.info("[STARTUP] Qt Application init: %.3fs", t3 - t2)

    # Windows: enable ANSI escape codes for coloured console output
    if sys.platform == "win32":
        import os
        os.system("")   # one-liner that enables VT100 in cmd.exe / PowerShell

    t4 = time.time()
    from ui.main_window import MainWindow
    window = MainWindow(config, cache)
    t5 = time.time()
    log.info("[STARTUP] Main Window creation: %.3fs", t5 - t4)
    
    window.show()
    t6 = time.time()
    log.info("[STARTUP] Window show: %.3fs", t6 - t5)
    log.info("[STARTUP] Total startup time: %.3fs", t6 - t0)

    # Background performance diagnostics
    import threading
    import psutil
    import time
    
    def run_diagnostics():
        process = psutil.Process(os.getpid())
        while True:
            try:
                cpu_percent = process.cpu_percent(interval=1.0)
                memory_info = process.memory_info()
                mb = memory_info.rss / (1024 * 1024)
                # Count active threads
                thread_count = threading.active_count()
                log.info("[PERF] CPU: %.1f%% | Memory: %.1f MB | Threads: %d", cpu_percent, mb, thread_count)
            except Exception as e:
                log.debug("[PERF] Diagnostics error: %s", e)
            time.sleep(10)
            
    threading.Thread(target=run_diagnostics, daemon=True, name="DiagnosticsThread").start()

    log.info("Event loop started")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
