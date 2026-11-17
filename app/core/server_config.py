"""Safe defaults for the local CamFlow dashboard server."""

from collections.abc import Mapping


DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
ALLOWED_BIND_HOSTS = frozenset({DEFAULT_BIND_HOST, "0.0.0.0"})


def resolve_server_config(environ: Mapping[str, str]) -> tuple[str, int]:
    """Resolve and validate the server bind address and port.

    The unauthenticated dashboard stays on loopback unless an operator
    explicitly opts in to LAN binding with SENTINEL_BIND_HOST=0.0.0.0.
    """
    host = environ.get("SENTINEL_BIND_HOST", DEFAULT_BIND_HOST).strip() or DEFAULT_BIND_HOST
    if host not in ALLOWED_BIND_HOSTS:
        raise ValueError("SENTINEL_BIND_HOST must be 127.0.0.1 or 0.0.0.0")

    try:
        port = int(environ.get("SENTINEL_PORT", str(DEFAULT_PORT)))
    except (TypeError, ValueError) as exc:
        raise ValueError("SENTINEL_PORT must be an integer") from exc

    if not 1 <= port <= 65535:
        raise ValueError("SENTINEL_PORT must be between 1 and 65535")

    return host, port
