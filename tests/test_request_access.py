import pytest

from app.core.request_access import (
    bearer_token_matches,
    is_loopback_client,
    remote_request_denial_status,
    should_reject_remote_request,
)


@pytest.mark.parametrize("address", ["127.0.0.1", "127.0.0.2", "::1", "::ffff:127.0.0.1"])
def test_loopback_sources_are_local(address):
    assert is_loopback_client(address)
    assert remote_request_denial_status(address, "/api/system/shutdown") is None


@pytest.mark.parametrize("address", ["10.0.0.8", "172.16.0.8", "192.168.1.20", "8.8.8.8", "::ffff:192.168.1.20"])
def test_non_loopback_sources_cannot_access_dashboard_or_control(address):
    assert not is_loopback_client(address)
    assert remote_request_denial_status(address, "/") == 403
    assert remote_request_denial_status(address, "/dashboard") == 403
    assert remote_request_denial_status(address, "/stream") == 403
    assert remote_request_denial_status(address, "/api/system/shutdown") == 403
    assert remote_request_denial_status(address, "/api/config") == 403


@pytest.mark.parametrize("address", ["10.0.0.8", "192.168.1.20", "8.8.8.8"])
def test_remote_ping_is_available_without_credentials(address):
    assert remote_request_denial_status(address, "/ping") is None


def test_remote_upload_requires_a_bearer_token_when_configured():
    token = "A" * 64
    assert remote_request_denial_status("192.168.1.20", "/upload", None, token) == 401
    assert remote_request_denial_status("192.168.1.20", "/upload", "Basic abc", token) == 401
    assert remote_request_denial_status("192.168.1.20", "/upload", f"Bearer {token.lower()}", token) == 401
    assert remote_request_denial_status("192.168.1.20", "/upload", f"Bearer {token}", token) is None


def test_remote_upload_is_denied_without_token_or_legacy_opt_in():
    assert remote_request_denial_status("192.168.1.20", "/upload") == 403
    assert should_reject_remote_request("192.168.1.20", "/upload")


def test_legacy_unauthenticated_ingest_requires_explicit_opt_in():
    assert remote_request_denial_status(
        "192.168.1.20", "/upload", allow_unauthenticated_lan=True
    ) is None


def test_bearer_token_comparison_rejects_empty_and_malformed_headers():
    token = "B" * 64
    assert not bearer_token_matches(None, token)
    assert not bearer_token_matches("Bearer", token)
    assert not bearer_token_matches("Bearer  " + token, token)
    assert not bearer_token_matches("Bearer " + token, None)
    assert bearer_token_matches("bearer " + token, token)


@pytest.mark.parametrize("address", [None, "", "not-an-ip"])
def test_missing_or_invalid_remote_address_is_not_treated_as_local(address):
    assert not is_loopback_client(address)
    assert remote_request_denial_status(address, "/api/config") == 403
