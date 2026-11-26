"""Request-origin policy for the unauthenticated CamFlow LAN mode."""

from ipaddress import IPv6Address, ip_address


LAN_INGEST_PATHS = frozenset({"/ping", "/upload"})


def is_loopback_client(remote_addr: str | None) -> bool:
    """Return True only for a valid loopback source address."""
    if not remote_addr:
        return False
    try:
        address = ip_address(remote_addr)
    except ValueError:
        return False
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def should_reject_remote_request(remote_addr: str | None, path: str) -> bool:
    """Keep dashboard/control routes local while allowing CamFlow ping and ingest."""
    return not is_loopback_client(remote_addr) and path not in LAN_INGEST_PATHS
