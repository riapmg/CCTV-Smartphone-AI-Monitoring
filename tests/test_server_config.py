import pytest

from app.core.server_config import resolve_server_config


def test_server_defaults_to_loopback_and_default_port():
    assert resolve_server_config({}) == ("127.0.0.1", 8000)


def test_blank_bind_host_stays_on_loopback():
    assert resolve_server_config({"SENTINEL_BIND_HOST": "  "}) == ("127.0.0.1", 8000)


def test_lan_bind_requires_explicit_host_opt_in():
    assert resolve_server_config({"SENTINEL_BIND_HOST": "0.0.0.0"}) == ("0.0.0.0", 8000)


def test_custom_valid_port_is_accepted():
    assert resolve_server_config({"SENTINEL_PORT": "8123"}) == ("127.0.0.1", 8123)


def test_invalid_bind_host_is_rejected():
    with pytest.raises(ValueError, match="SENTINEL_BIND_HOST"):
        resolve_server_config({"SENTINEL_BIND_HOST": "192.168.1.10"})


def test_non_integer_port_is_rejected():
    with pytest.raises(ValueError, match="must be an integer"):
        resolve_server_config({"SENTINEL_PORT": "eighty"})


@pytest.mark.parametrize("port", ["0", "-1", "65536"])
def test_out_of_range_port_is_rejected(port):
    with pytest.raises(ValueError, match="between 1 and 65535"):
        resolve_server_config({"SENTINEL_PORT": port})
