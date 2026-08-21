"""Translates Tor configuration into a Playwright-compatible proxy dict.

Deliberately free of any control-port or circuit-lifecycle logic -- that
lives in scripts/tor/circuit_manager.py, which only the circuit manager
itself may call. This module only answers "what proxy argument (if any)
should browser.launch() receive right now," so collectors stay ignorant
of Tor lifecycle entirely.
"""
from app.config import (
    get_tor_enabled,
    get_tor_socks_host,
    get_tor_socks_port,
)


def get_proxy_config():
    """Returns a Playwright `proxy=` dict, or None when Tor is disabled.

    None preserves the exact pre-Tor behavior of browser.launch() calls
    (no proxy argument at all) -- this is the disable/rollback path.
    """
    if not get_tor_enabled():
        return None

    return {
        "server": f"socks5://{get_tor_socks_host()}:{get_tor_socks_port()}",
    }
