import pytest

from app.core.server_config import resolve_server_config


def test_server_defaults_to_loopback_and_default_port():
    assert resolve_server_config({}) == ("127.0.0.1", 8000)


def test_blank_bind_host_stays_on_loopback():
    assert resolve_server_config({"SENTINEL_BIND_HOST": "  "}) == ("127.0.0.1", 8000)


def test_lan_bind_requires_explicit_private_interface_and_unauthenticated_opt_in():
    assert resolve_server_config({
        "SENTINEL_BIND_HOST": "192.168.1.10",
        "SENTINEL_ALLOW_UNAUTHENTICATED_LAN": "1",
    }) == ("192.168.1.10", 8000)


def test_lan_bind_is_rejected_without_unauthenticated_acknowledgement():
    with pytest.raises(ValueError, match="LAN binding is unauthenticated"):
        resolve_server_config({"SENTINEL_BIND_HOST": "192.168.1.10"})


@pytest.mark.parametrize("opt_in", ["true", "yes", "0"])
def test_lan_bind_rejects_non_exact_unauthenticated_acknowledgement(opt_in):
    with pytest.raises(ValueError, match="LAN binding is unauthenticated"):
        resolve_server_config({
            "SENTINEL_BIND_HOST": "192.168.1.10",
            "SENTINEL_ALLOW_UNAUTHENTICATED_LAN": opt_in,
        })


def test_custom_valid_port_is_accepted():
    assert resolve_server_config({"SENTINEL_PORT": "8123"}) == ("127.0.0.1", 8123)


@pytest.mark.parametrize("host", ["10.1.2.3", "172.16.0.10", "192.168.1.10"])
def test_private_lan_interface_is_accepted(host):
    assert resolve_server_config({
        "SENTINEL_BIND_HOST": host,
        "SENTINEL_ALLOW_UNAUTHENTICATED_LAN": "1",
    }) == (host, 8000)


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "192.0.2.10", "8.8.8.8", "169.254.1.2", "camera.local", "::1", "127.0.0.2"],
)
def test_non_private_or_ambiguous_bind_host_is_rejected(host):
    with pytest.raises(ValueError, match="private RFC1918"):
        resolve_server_config({"SENTINEL_BIND_HOST": host})


def test_non_integer_port_is_rejected():
    with pytest.raises(ValueError, match="must be an integer"):
        resolve_server_config({"SENTINEL_PORT": "eighty"})


@pytest.mark.parametrize("port", ["0", "-1", "65536"])
def test_out_of_range_port_is_rejected(port):
    with pytest.raises(ValueError, match="between 1 and 65535"):
        resolve_server_config({"SENTINEL_PORT": port})
