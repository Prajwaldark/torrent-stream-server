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
        "PySide6": "pip install PySide6",
        "libtorrent": "pip install libtorrent  (or python-libtorrent)",
        "qrcode": "pip install qrcode",
        "PIL": "pip install Pillow",
    }
    for module, hint in required.items():
        try:
            importlib.import_module(module)
        except ImportError:
            errors.append(f"  ✗  {module} — {hint}")

    if errors:
        print("Missing dependencies:\n" + "\n".join(errors))
        return False

    print("✓  All imports OK")
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

    from utils.config import AppConfig
    from cache.cleanup import CacheManager

    config = AppConfig.load()
    cache = CacheManager(config.cache_dir)

    # Qt requires QApplication before any widget
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt

    app = QApplication(sys.argv)
    app.setApplicationName("TorrentStream")
    app.setOrganizationName("TorrentStream")

    # Windows: enable ANSI escape codes for coloured console output
    if sys.platform == "win32":
        import os
        os.system("")   # one-liner that enables VT100 in cmd.exe / PowerShell

    from ui.main_window import MainWindow
    window = MainWindow(config, cache)
    window.show()

    log.info("Event loop started")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
