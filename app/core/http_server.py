"""HTTP listener lifecycle for the local dashboard and CamFlow LAN ingest."""

from __future__ import annotations

import threading

from app.core.server_config import resolve_listener_hosts


def serve_on_resolved_interfaces(app, host: str, port: int, server_factory=None) -> None:
    """Serve the dashboard on loopback and optional ingest on one LAN interface.

    Separate sockets preserve a local dashboard when the phone sender uses a
    selected private interface. The Flask request policy still blocks remote
    dashboard/control routes and requires the configured Bearer token for
    remote frame ingest. The optional factory is injectable for deterministic
    lifecycle tests; production uses Werkzeug's threaded WSGI server.
    """
    if server_factory is None:
        from werkzeug.serving import make_server

        server_factory = make_server

    listener_hosts = resolve_listener_hosts(host)
    servers = []
    lan_thread = None
    try:
        for bind_host in listener_hosts:
            servers.append(server_factory(bind_host, port, app, threaded=True))

        if len(servers) == 1:
            servers[0].serve_forever()
            return

        lan_server = servers[1]
        lan_thread = threading.Thread(
            target=lan_server.serve_forever,
            name="camflow-lan-http",
            daemon=True,
        )
        lan_thread.start()
        try:
            servers[0].serve_forever()
        finally:
            if lan_thread.is_alive():
                lan_server.shutdown()
                lan_thread.join(timeout=3.0)
    finally:
        for http_server in servers:
            http_server.server_close()
