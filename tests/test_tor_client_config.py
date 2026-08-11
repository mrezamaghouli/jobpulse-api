"""Tests for scripts/tor/tor_client.py -- pure config-to-proxy-dict
translation. No network, no Tor process, no PostgreSQL required.

The one property this module must guarantee: when TOR_ENABLED is not
explicitly set to a truthy value, get_proxy_config() returns None, which
is exactly what a browser.launch() call received before Tor support
existed. That is the disable/rollback path for the whole feature.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.tor.tor_client import get_proxy_config


def test_proxy_config_is_none_when_tor_disabled(monkeypatch):
    monkeypatch.setenv("TOR_ENABLED", "false")
    assert get_proxy_config() is None


def test_proxy_config_defaults_to_disabled_when_unset(monkeypatch):
    monkeypatch.delenv("TOR_ENABLED", raising=False)
    assert get_proxy_config() is None


def test_proxy_config_builds_socks5_url_when_enabled(monkeypatch):
    monkeypatch.setenv("TOR_ENABLED", "true")
    monkeypatch.setenv("TOR_SOCKS_HOST", "tor")
    monkeypatch.setenv("TOR_SOCKS_PORT", "9050")

    assert get_proxy_config() == {"server": "socks5://tor:9050"}


def test_proxy_config_uses_default_host_and_port(monkeypatch):
    monkeypatch.setenv("TOR_ENABLED", "true")
    monkeypatch.delenv("TOR_SOCKS_HOST", raising=False)
    monkeypatch.delenv("TOR_SOCKS_PORT", raising=False)

    assert get_proxy_config() == {"server": "socks5://127.0.0.1:9050"}


def test_proxy_config_accepts_common_truthy_spellings(monkeypatch):
    for value in ["true", "1", "yes", "on", "TRUE", "On"]:
        monkeypatch.setenv("TOR_ENABLED", value)
        assert get_proxy_config() is not None, f"expected enabled for {value!r}"


def test_proxy_config_rejects_non_truthy_spellings(monkeypatch):
    for value in ["", "no", "0", "off", "disabled"]:
        monkeypatch.setenv("TOR_ENABLED", value)
        assert get_proxy_config() is None, f"expected disabled for {value!r}"
