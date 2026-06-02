"""
utils/network.py — Network utilities.
"""
import socket


def get_lan_ip() -> str:
    """
    Returns the primary LAN IP address of this machine.
    """
    try:
        # Doesn't actually send any data, just uses the OS routing table
        # to find the interface used to reach the internet.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
