"""
utils/network.py — Network utilities.
"""
import ipaddress
import socket


def _is_usable_ipv4(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version != 4 or ip.is_loopback or ip.is_unspecified or ip.is_link_local:
        return False
    return True


def _iter_candidate_ipv4_addresses() -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except Exception:
        infos = []

    for family, _, _, _, sockaddr in infos:
        if family != socket.AF_INET or not sockaddr:
            continue
        address = sockaddr[0]
        if address in seen or not _is_usable_ipv4(address):
            continue
        seen.add(address)
        candidates.append(address)

    # Prefer RFC1918 LAN addresses before any public address.
    private = [addr for addr in candidates if ipaddress.ip_address(addr).is_private]
    return private or candidates


def get_lan_ip(target_host: str | None = None) -> str:
    """
    Returns the best local IPv4 address to advertise for LAN streaming.

    If ``target_host`` is supplied, the OS routing table is queried for the
    interface that would be used to reach that specific device. This is more
    reliable on VPN / multi-adapter systems than always probing a public IP.
    """
    probe_target = target_host or "8.8.8.8"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((probe_target, 80))
            address = s.getsockname()[0]
            if _is_usable_ipv4(address):
                return address
    except Exception:
        pass

    for candidate in _iter_candidate_ipv4_addresses():
        return candidate

    return "127.0.0.1"
