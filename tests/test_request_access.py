import pytest

from app.core.request_access import is_loopback_client, should_reject_remote_request


@pytest.mark.parametrize("address", ["127.0.0.1", "127.0.0.2", "::1", "::ffff:127.0.0.1"])
def test_loopback_sources_are_local(address):
    assert is_loopback_client(address)
    assert not should_reject_remote_request(address, "/api/system/shutdown")


@pytest.mark.parametrize("address", ["10.0.0.8", "172.16.0.8", "192.168.1.20", "8.8.8.8", "::ffff:192.168.1.20"])
def test_non_loopback_sources_cannot_access_dashboard_or_control(address):
    assert not is_loopback_client(address)
    assert should_reject_remote_request(address, "/")
    assert should_reject_remote_request(address, "/dashboard")
    assert should_reject_remote_request(address, "/stream")
    assert should_reject_remote_request(address, "/api/system/shutdown")
    assert should_reject_remote_request(address, "/api/config")


@pytest.mark.parametrize("address", ["10.0.0.8", "192.168.1.20", "8.8.8.8"])
def test_non_loopback_sources_can_only_reach_ping_and_ingest(address):
    assert not should_reject_remote_request(address, "/ping")
    assert not should_reject_remote_request(address, "/upload")
    assert should_reject_remote_request(address, "/api/status")


@pytest.mark.parametrize("address", [None, "", "not-an-ip"])
def test_missing_or_invalid_remote_address_is_not_treated_as_local(address):
    assert not is_loopback_client(address)
    assert should_reject_remote_request(address, "/api/config")
