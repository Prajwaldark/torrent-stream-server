#!/usr/bin/env python
"""
Minimal test to verify Cast button clicks are registered.
"""
import sys
import logging

logging.basicConfig(level=logging.DEBUG, format='%(name)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

# Test imports first
try:
    from PySide6.QtWidgets import QApplication, QPushButton, QVBoxLayout, QWidget
    from PySide6.QtCore import Slot
    log.info("✓ PySide6 imported")
except ImportError as e:
    log.error(f"✗ Failed to import PySide6: {e}")
    sys.exit(1)

try:
    from utils.cast import CastManager
    log.info("✓ CastManager imported")
except ImportError as e:
    log.error(f"✗ Failed to import CastManager: {e}")
    sys.exit(1)

class TestCastApp(QWidget):
    def __init__(self):
        super().__init__()
        self._cast_manager = CastManager()
        
        layout = QVBoxLayout(self)
        
        btn1 = QPushButton("Refresh Devices")
        btn1.clicked.connect(self._on_refresh)
        layout.addWidget(btn1)
        
        btn2 = QPushButton("Start Discovery")
        btn2.clicked.connect(self._on_start_discovery)
        layout.addWidget(btn2)
        
        self.setWindowTitle("Cast UI Test")
        self.resize(300, 150)
    
    @Slot()
    def _on_refresh(self):
        log.info("Refresh button clicked!")
        self._cast_manager.stop_discovery()
        self._cast_manager.start_discovery(self._on_devices_changed)
    
    @Slot()
    def _on_start_discovery(self):
        log.info("Start Discovery button clicked!")
        self._cast_manager.start_discovery(self._on_devices_changed)
    
    def _on_devices_changed(self, devices: list[str]):
        log.info("Devices changed callback: %s", devices)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = TestCastApp()
    window.show()
    log.info("UI Test window opened. Try clicking the buttons...")
    sys.exit(app.exec())
