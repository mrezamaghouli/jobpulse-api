"""Transport abstraction (Phase 3.4K, Section 2/3).

The single place transport selection happens. Collectors ask
get_search_transport() for a SearchTransport and use ONLY the fields it
exposes (playwright_proxy_config, connect_timeout_ms, read_timeout_ms) --
they never read SEARCH_TRANSPORT/TOR_ENABLED/TOR_SOCKS_* themselves, and
never call scripts/tor/circuit_manager.py or a ControlPort directly. That
keeps Tor-specific knowledge (SOCKS URL shape, control-port existence)
entirely out of collector/business logic.

Proxy mode is gated by SEARCH_TRANSPORT=proxy alone -- it deliberately
does NOT also check TOR_ENABLED. TOR_ENABLED is the pre-existing
Tor-infra-is-live flag (docker-compose/dark-launch/monitoring), and this
repository has an actively-enforced invariant, proven by
tests/test_tor_production_dark_launch.py::test_tor_enabled_setters_are_a_closed_allowlist
and ::test_no_collection_script_sets_tor_enabled_true, that NO collection
code path may read or depend on TOR_ENABLED -- it exists purely for
observability/monitoring of the Tor capability, never for gating traffic.
Coupling this module to it would violate that invariant. SEARCH_TRANSPORT
is this phase's own, independent, purpose-built rollout flag and is
sufficient on its own: it defaults to "direct", and moving production
traffic to proxy requires an explicit, reviewed change to that one value
(see Section 0 of the Phase 3.4K spec).

Proxy mode also probes that the configured SOCKS port actually accepts a
TCP connection before returning -- a fast, cheap, fail-explicit check
that catches "Tor sidecar isn't actually running" even though
SEARCH_TRANSPORT=proxy was requested. If SEARCH_TRANSPORT=proxy is
requested and that probe fails, this raises
ProxyTransportUnavailableError. There is no except-and-fall-back-to-direct
anywhere in this module or in any caller: an unavailable proxy is a
failure, surfaced up through the existing collector-result / queue-retry
pipeline (see scripts/process_search_demand_queue.py), never a silent
downgrade.
"""
import socket
from dataclasses import dataclass
from typing import Optional

from app.config import (
    get_search_transport_direct_connect_timeout_ms,
    get_search_transport_direct_read_timeout_ms,
    get_search_transport_mode,
    get_search_transport_proxy_connect_timeout_ms,
    get_search_transport_proxy_probe_timeout_ms,
    get_search_transport_proxy_read_timeout_ms,
    get_tor_socks_host,
    get_tor_socks_port,
)


MODE_DIRECT = "direct"
MODE_PROXY = "proxy"


class ProxyTransportUnavailableError(RuntimeError):
    """Raised when SEARCH_TRANSPORT=proxy is requested but the configured
    SOCKS port refused/timed out a TCP connect. Callers must let this
    propagate -- it must never be caught to silently retry on direct
    transport instead."""


@dataclass(frozen=True)
class SearchTransport:
    """mode is "direct" or "proxy". playwright_proxy_config is exactly the
    dict browser.launch(proxy=...) expects, or None for direct mode (the
    same None that meant "no proxy argument at all" before this phase --
    the disable/rollback shape is unchanged). connect_timeout_ms governs
    page.goto()/navigation timeouts; read_timeout_ms governs Playwright's
    default per-action timeout (selector waits, clicks) -- see module docs
    in scripts/search_transport/executor.py for why this split exists and
    exactly what it does and does not map onto in Playwright."""

    mode: str
    playwright_proxy_config: Optional[dict]
    connect_timeout_ms: int
    read_timeout_ms: int


def _probe_proxy_reachable(host: str, port: int, timeout_ms: int) -> None:
    timeout_seconds = max(0.001, timeout_ms / 1000.0)

    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return
    except OSError as error:
        raise ProxyTransportUnavailableError(
            f"SEARCH_TRANSPORT=proxy: SOCKS proxy {host}:{port} is not reachable "
            f"(TCP connect failed within {timeout_ms}ms): {error}"
        ) from error


def get_search_transport(mode: Optional[str] = None) -> SearchTransport:
    """mode overrides SEARCH_TRANSPORT for tests/benchmarking; production
    callers pass nothing and get the configured value (default "direct")."""
    resolved_mode = (mode or get_search_transport_mode()).strip().lower()

    if resolved_mode == MODE_DIRECT:
        return SearchTransport(
            mode=MODE_DIRECT,
            playwright_proxy_config=None,
            connect_timeout_ms=get_search_transport_direct_connect_timeout_ms(),
            read_timeout_ms=get_search_transport_direct_read_timeout_ms(),
        )

    if resolved_mode != MODE_PROXY:
        raise ValueError(f"Unknown SEARCH_TRANSPORT mode: {resolved_mode!r}")

    socks_host = get_tor_socks_host()
    socks_port = get_tor_socks_port()

    _probe_proxy_reachable(
        socks_host, socks_port, get_search_transport_proxy_probe_timeout_ms(),
    )

    return SearchTransport(
        mode=MODE_PROXY,
        playwright_proxy_config={"server": f"socks5://{socks_host}:{socks_port}"},
        connect_timeout_ms=get_search_transport_proxy_connect_timeout_ms(),
        read_timeout_ms=get_search_transport_proxy_read_timeout_ms(),
    )
