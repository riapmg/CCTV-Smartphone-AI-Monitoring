"""Request-origin and bearer-token policy for CamFlow LAN ingest."""

from __future__ import annotations


import hmac
from ipaddress import IPv6Address, ip_address


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


def bearer_token_matches(authorization_header: str | None, expected_token: str | None) -> bool:
    """Compare a Bearer credential without timing-sensitive string equality."""
    if not expected_token or not authorization_header:
        return False
    scheme, separator, supplied_token = authorization_header.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not supplied_token:
        return False
    try:
        return hmac.compare_digest(supplied_token, expected_token)
    except TypeError:
        # compare_digest rejects non-ASCII str inputs; treat malformed headers as invalid.
        return False


def remote_request_denial_status(
    remote_addr: str | None,
    path: str,
    authorization_header: str | None = None,
    ingest_token: str | None = None,
    allow_unauthenticated_lan: bool = False,
) -> int | None:
    """Return an HTTP denial status for remote clients, or None when allowed.

    Loopback clients retain the local dashboard/control workflow. Remote clients
    may use the health check without credentials. Remote frame ingest requires a
    configured Bearer token, unless the operator explicitly enabled the legacy
    unauthenticated LAN mode.
    """
    if is_loopback_client(remote_addr):
        return None
    if path == "/ping":
        return None
    if path == "/upload":
        if ingest_token:
            return None if bearer_token_matches(authorization_header, ingest_token) else 401
        return None if allow_unauthenticated_lan else 403
    return 403


def should_reject_remote_request(remote_addr: str | None, path: str) -> bool:
    """Compatibility wrapper: remote ingest is denied without explicit auth/opt-in."""
    return remote_request_denial_status(remote_addr, path) is not None
