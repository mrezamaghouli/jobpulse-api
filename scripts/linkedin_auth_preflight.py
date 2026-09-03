"""LinkedIn auth preflight (Phase 3.4K, Section 6).

Transport parity with real collection: this module resolves its proxy
argument, connect timeout, and read timeout from EXACTLY the same
scripts.search_transport.transport.get_search_transport() the actual
collector (scripts/providers/linkedin_browser_provider.py) uses -- never
the legacy scripts/tor/tor_client.py path, which reads the pre-existing
Tor-infra-is-live flag directly (see app/config.py::get_tor_enabled(),
never referenced by this module or by get_search_transport()) and could
therefore silently select a different network transport than the
collection run this preflight is meant to validate.
This module contains no ControlPort logic and never requests a circuit
change -- no collection-runtime code does (see
scripts/search_transport/executor.py's module docstring); it is a
read-only auth check, nothing more. A proxy transport that is configured
but unreachable fails this preflight closed
(ProxyTransportUnavailableError propagates into the same
LINKEDIN_AUTH_PREFLIGHT_FAILED SystemExit as any other failure below) --
there is no fallback to direct traffic anywhere in this module.
"""
from pathlib import Path
from playwright.sync_api import sync_playwright
from scripts.providers.linkedin_browser_provider import assert_linkedin_authenticated
from scripts.search_transport.transport import get_search_transport


def preflight_linkedin_auth():
    import os

    enabled = os.getenv("LINKEDIN_AUTH_PREFLIGHT", "true").lower() not in ("0", "false", "no")
    if not enabled:
        print("LinkedIn auth preflight skipped by LINKEDIN_AUTH_PREFLIGHT=false")
        return

    state_path = os.getenv("LINKEDIN_STORAGE_STATE", "/app/.auth/linkedin_storage_state.json")

    state_file = Path(state_path)
    if not state_file.exists() or state_file.stat().st_size <= 0:
        raise SystemExit(
            f"LINKEDIN_AUTH_PREFLIGHT_FAILED: storage state missing or empty: {state_path}"
        )

    print("Running LinkedIn auth preflight check...")

    try:
        transport = get_search_transport()

        if transport.mode != "direct":
            print(f"Auth preflight transport mode: {transport.mode}")

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                channel=os.getenv("LINKEDIN_BROWSER_CHANNEL", "chrome"),
                args=["--no-sandbox", "--disable-dev-shm-usage"],
                proxy=transport.playwright_proxy_config,
            )

            context = browser.new_context(storage_state=state_path)
            page = context.new_page()
            page.set_default_timeout(transport.read_timeout_ms)
            page.set_default_navigation_timeout(transport.connect_timeout_ms)

            page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)

            assert_linkedin_authenticated(page, stage="process_search_demand_queue_preflight")

            context.close()
            browser.close()

        print("LinkedIn auth preflight passed.")

    except Exception as exc:
        raise SystemExit(f"LINKEDIN_AUTH_PREFLIGHT_FAILED: {exc}")
