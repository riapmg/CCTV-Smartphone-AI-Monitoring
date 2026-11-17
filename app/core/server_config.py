"""Safe defaults for the local CamFlow dashboard server."""

from collections.abc import Mapping
import ipaddress


DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
ALLOWED_BIND_HOSTS = frozenset({DEFAULT_BIND_HOST, "0.0.0.0"})
RFC1918_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def validate_bind_host(host: str) -> str:
    """Allow loopback, the explicit wildcard, or one RFC1918 IPv4 address."""
    if host in ALLOWED_BIND_HOSTS:
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(
            "SENTINEL_BIND_HOST must be loopback, 0.0.0.0, or a private RFC1918 IPv4 address"
        ) from exc
    if address.version != 4 or not any(address in network for network in RFC1918_NETWORKS):
        raise ValueError(
            "SENTINEL_BIND_HOST must be loopback, 0.0.0.0, or a private RFC1918 IPv4 address"
        )
    return host


def resolve_server_config(environ: Mapping[str, str]) -> tuple[str, int]:
    """Resolve and validate the server bind address and port.

    The unauthenticated dashboard stays on loopback unless an operator
    explicitly opts in to LAN binding with a private RFC1918 address or the 0.0.0.0 wildcard.
    """
    host = environ.get("SENTINEL_BIND_HOST", DEFAULT_BIND_HOST).strip() or DEFAULT_BIND_HOST
    host = validate_bind_host(host)

    try:
        port = int(environ.get("SENTINEL_PORT", str(DEFAULT_PORT)))
    except (TypeError, ValueError) as exc:
        raise ValueError("SENTINEL_PORT must be an integer") from exc

    if not 1 <= port <= 65535:
        raise ValueError("SENTINEL_PORT must be between 1 and 65535")

    return host, port
