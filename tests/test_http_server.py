import threading

import pytest

from app.core.http_server import serve_on_resolved_interfaces


class FakeServer:
    def __init__(self, host, shared):
        self.host = host
        self.shared = shared
        self.stop_event = threading.Event()
        self.closed = False
        self.shutdown_called = False

    def serve_forever(self):
        if self.host == "127.0.0.1":
            lan_started = self.shared.get("lan_started")
            if lan_started is not None:
                assert lan_started.wait(timeout=2)
            return
        self.shared["lan_started"].set()
        self.stop_event.wait(timeout=2)

    def shutdown(self):
        self.shutdown_called = True
        self.stop_event.set()

    def server_close(self):
        self.closed = True


def make_factory(shared, fail_host=None):
    def factory(host, port, app, threaded):
        assert port == 8000
        assert threaded is True
        if host == fail_host:
            raise OSError("simulated bind failure")
        server = FakeServer(host, shared)
        shared.setdefault("servers", []).append(server)
        return server

    return factory


def test_loopback_mode_starts_and_closes_one_listener():
    shared = {}
    serve_on_resolved_interfaces(
        object(), "127.0.0.1", 8000, server_factory=make_factory(shared)
    )

    assert [server.host for server in shared["servers"]] == ["127.0.0.1"]
    assert shared["servers"][0].closed
    assert not shared["servers"][0].shutdown_called


def test_lan_mode_keeps_loopback_dashboard_and_stops_lan_listener():
    shared = {"lan_started": threading.Event()}
    serve_on_resolved_interfaces(
        object(), "192.168.1.10", 8000, server_factory=make_factory(shared)
    )

    assert [server.host for server in shared["servers"]] == [
        "127.0.0.1",
        "192.168.1.10",
    ]
    assert all(server.closed for server in shared["servers"])
    assert shared["servers"][1].shutdown_called


def test_lan_bind_failure_closes_the_already_created_loopback_listener():
    shared = {}
    with pytest.raises(OSError, match="simulated bind failure"):
        serve_on_resolved_interfaces(
            object(),
            "192.168.1.10",
            8000,
            server_factory=make_factory(shared, fail_host="192.168.1.10"),
        )

    assert len(shared["servers"]) == 1
    assert shared["servers"][0].host == "127.0.0.1"
    assert shared["servers"][0].closed
