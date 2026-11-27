"""Safe defaults and LAN-ingest credential validation for CamFlow."""

from __future__ import annotations

from collections.abc import Mapping
import ipaddress


DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
LAN_UNAUTHENTICATED_OPT_IN = "SENTINEL_ALLOW_UNAUTHENTICATED_LAN"
INGEST_TOKEN_ENV = "CAMFLOW_INGEST_TOKEN"
ALLOWED_BIND_HOSTS = frozenset({DEFAULT_BIND_HOST})
RFC1918_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def validate_ingest_token(token: str | None) -> str | None:
    """Accept an optional 32-256 character visible-ASCII bearer token."""
    if token is None or token == "":
        return None
    if not 32 <= len(token) <= 256 or any(not 33 <= ord(char) <= 126 for char in token):
        raise ValueError(
            "CAMFLOW_INGEST_TOKEN must contain 32-256 visible ASCII characters with no whitespace"
        )
    return token


def validate_bind_host(host: str) -> str:
    """Allow loopback or one explicitly selected RFC1918 IPv4 interface."""
    if host in ALLOWED_BIND_HOSTS:
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(
            "SENTINEL_BIND_HOST must be loopback or a private RFC1918 IPv4 address"
        ) from exc
    if address.version != 4 or not any(address in network for network in RFC1918_NETWORKS):
        raise ValueError(
            "SENTINEL_BIND_HOST must be loopback or a private RFC1918 IPv4 address"
        )
    return host


def resolve_listener_hosts(host: str) -> tuple[str, ...]:
    """Bind the local dashboard and, when selected, one separate LAN interface."""
    selected_host = validate_bind_host(host)
    if selected_host == DEFAULT_BIND_HOST:
        return (DEFAULT_BIND_HOST,)
    return (DEFAULT_BIND_HOST, selected_host)


def resolve_server_config(environ: Mapping[str, str]) -> tuple[str, int]:
    """Resolve and validate the server bind address and port.

    Loopback is the default. LAN mode requires a specific private RFC1918 IPv4
    interface and either a valid CAMFLOW_INGEST_TOKEN or the explicit legacy
    acknowledgement for unauthenticated ingest.
    """
    host = environ.get("SENTINEL_BIND_HOST", DEFAULT_BIND_HOST).strip() or DEFAULT_BIND_HOST
    host = validate_bind_host(host)
    token = validate_ingest_token(environ.get(INGEST_TOKEN_ENV))
    allow_unauthenticated_lan = (
        environ.get(LAN_UNAUTHENTICATED_OPT_IN, "").strip() == "1"
    )
    if host != DEFAULT_BIND_HOST and not token and not allow_unauthenticated_lan:
        raise ValueError(
            "LAN binding requires CAMFLOW_INGEST_TOKEN (recommended) or "
            "SENTINEL_ALLOW_UNAUTHENTICATED_LAN=1 for temporary legacy mode"
        )

    try:
        port = int(environ.get("SENTINEL_PORT", str(DEFAULT_PORT)))
    except (TypeError, ValueError) as exc:
        raise ValueError("SENTINEL_PORT must be an integer") from exc

    if not 1 <= port <= 65535:
        raise ValueError("SENTINEL_PORT must be between 1 and 65535")

    return host, port
