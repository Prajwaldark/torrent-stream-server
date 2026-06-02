#!/usr/bin/env python
"""
Test script to diagnose Chromecast discovery issues.
"""
import sys
import logging
import time
import threading

# Setup SSLContext monkeypatch for compatibility with legacy devices (e.g. AirReceiver)
import ssl
_orig_new = ssl.SSLContext.__new__
def new_SSLContext(cls, *args, **kwargs):
    context = _orig_new(cls, *args, **kwargs)
    try:
        context.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
    except Exception:
        pass
    try:
        context.set_ciphers('DEFAULT@SECLEVEL=1')
    except Exception:
        pass
    return context
ssl.SSLContext.__new__ = new_SSLContext

# Setup logging to see what's happening
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

try:
    import pychromecast
    import zeroconf
    log.info("✓ pychromecast imported successfully")
    log.info("✓ zeroconf imported successfully")
except ImportError as e:
    log.error(f"✗ Failed to import required packages: {e}")
    sys.exit(1)

# Test 1: Zeroconf initialization
log.info("\n=== Test 1: Zeroconf Initialization ===")
try:
    zconf = zeroconf.Zeroconf()
    log.info("✓ Zeroconf initialized successfully")
except Exception as e:
    log.error(f"✗ Failed to initialize Zeroconf: {e}")
    zconf = None
    sys.exit(1)

# Test 2: SimpleCastListener setup
log.info("\n=== Test 2: SimpleCastListener Setup ===")
discovered_devices = {}

def add_callback(uuid, service):
    # Retrieve cast info from browser devices
    cast_info = browser.devices.get(uuid)
    friendly_name = cast_info.friendly_name if cast_info else "Unknown"
    host = cast_info.host if cast_info else "Unknown"
    log.info(f"ADD: UUID={uuid}, friendly_name={friendly_name}, host={host}")
    discovered_devices[uuid] = service

def remove_callback(uuid, service, cast_info):
    log.info(f"REMOVE: UUID={uuid}")
    if uuid in discovered_devices:
        del discovered_devices[uuid]

def update_callback(uuid, service):
    cast_info = browser.devices.get(uuid)
    friendly_name = cast_info.friendly_name if cast_info else "Unknown"
    log.info(f"UPDATE: UUID={uuid}, friendly_name={friendly_name}")
    discovered_devices[uuid] = service

try:
    listener = pychromecast.SimpleCastListener(add_callback, remove_callback, update_callback)
    log.info("✓ SimpleCastListener created")
except Exception as e:
    log.error(f"✗ Failed to create SimpleCastListener: {e}")
    zconf.close()
    sys.exit(1)

# Test 3: CastBrowser startup
log.info("\n=== Test 3: CastBrowser Startup ===")
try:
    browser = pychromecast.CastBrowser(listener, zconf)
    log.info("✓ CastBrowser created")
    
    browser.start_discovery()
    log.info("✓ CastBrowser discovery started")
except Exception as e:
    log.error(f"✗ Failed to start CastBrowser: {e}", exc_info=True)
    zconf.close()
    sys.exit(1)

# Test 4: Wait and collect devices
log.info("\n=== Test 4: Waiting for Device Discovery (15 seconds) ===")
log.info("Listening for Chromecast devices...")

for i in range(15):
    time.sleep(1)
    log.debug(f"Scan {i+1}/15 - Devices found so far: {len(browser.devices)}")

log.info("\n=== Test 4 Results ===")
if browser.devices:
    log.info(f"✓ Found {len(browser.devices)} device(s):")
    for uuid, service in browser.devices.items():
        log.info(f"  - Name: {service.friendly_name}")
        log.info(f"    Host: {service.host}")
        log.info(f"    Port: {service.port}")
        log.info(f"    UUID: {uuid}")
else:
    log.warning("✗ No Chromecast devices found")

# Test 5: Try connecting to first device if found
if browser.devices:
    log.info("\n=== Test 5: Attempting Connection to First Device ===")
    first_uuid, first_service = next(iter(browser.devices.items()))
    device_name = first_service.friendly_name
    
    try:
        log.info(f"Getting Chromecast object for {device_name}...")
        cc = pychromecast.get_chromecast_from_cast_info(first_service, zconf)
        log.info(f"✓ Chromecast object created")
        
        log.info(f"Waiting for device to be ready...")
        cc.wait()
        log.info(f"✓ Device ready")
        
        log.info(f"Device name: {cc.name}")
        log.info(f"Device UUID: {cc.uuid}")
        
    except Exception as e:
        log.error(f"✗ Failed to connect to {device_name}: {e}", exc_info=True)

# Cleanup
log.info("\n=== Cleanup ===")
try:
    browser.stop_discovery()
    zconf.close()
    log.info("✓ Discovery stopped and Zeroconf closed")
except Exception as e:
    log.error(f"Error during cleanup: {e}")

log.info("\n=== Test Complete ===")
