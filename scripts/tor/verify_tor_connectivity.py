"""Manual Phase-1 diagnostic: confirms Tor has actually finished
bootstrapping, then launches a browser through the configured Tor SOCKS
proxy and reports the observed exit IP via an authorized, configurable
IP-check endpoint (TOR_IP_CHECK_URL, defaults to the Tor Project's own
check service). Never contacts LinkedIn or any collector target -- this
is Tor connectivity verification only.

The Docker healthcheck (docker-compose.tor.yml) already gates the `tor`
service on Tor's own "Bootstrapped 100%" log line before dependent
containers start, but that only proves bootstrap finished as of the
last healthcheck tick, not right now -- and Docker isn't the only way
this module gets run against a Tor instance. check_bootstrap_status()
below is this command's own, explicit, immediate confirmation: it makes
"Tor isn't ready yet" a clearly attributed failure with the raw phase
text, instead of it surfacing later as an ambiguous Playwright
navigation timeout on the SOCKS-routed check.

Uses Controller.get_info() to query the standard Tor control-protocol
GETINFO key `status/bootstrap-phase` -- get_info() itself is verified
against the installed stem 1.8.2 API; the GETINFO key name is a Tor
control-protocol detail (not a stem API), and has NOT been verified
against a live Tor process in this offline session -- confirm it during
actual Phase 1 runtime testing before relying on it operationally.

Usage:
    python -m scripts.tor.verify_tor_connectivity
    python -m scripts.tor.verify_tor_connectivity --rotate --circuit-id default
"""
import argparse
import json

import psycopg2
from playwright.sync_api import sync_playwright
from stem.control import Controller

from app.config import (
    get_postgres_config,
    get_tor_control_host,
    get_tor_control_password,
    get_tor_control_port,
    get_tor_ip_check_url,
)
from scripts.tor.circuit_manager import (
    DEFAULT_CIRCUIT_KEY,
    ERROR_CATEGORY_BOOTSTRAP_INCOMPLETE,
    ERROR_CATEGORY_CONTROL_PORT_FAILURE,
    _instance_lock_key,
    record_bootstrap_failed,
    record_bootstrap_ready,
    record_bootstrap_started,
    rotate_circuit,
)
from scripts.tor.tor_client import get_proxy_config


class TorVerificationError(RuntimeError):
    """Raised when the exit-IP check response cannot be trusted as proof
    of routing through Tor. A manual/direct (non-Tor) endpoint, or a
    Tor-check endpoint that itself reports IsTor=false, must never be
    mistaken for successful Tor verification -- so invalid JSON, a
    missing/non-true IsTor, and a missing/empty IP are all raised
    explicitly rather than papered over with a best-effort fallback."""


def check_bootstrap_status() -> str:
    """Raises RuntimeError (with the raw phase text) if Tor has not
    reported PROGRESS=100 yet. Returns the raw phase text on success.

    Also persists this bootstrap observation via the control-plane side
    (scripts/tor/circuit_manager.py's record_bootstrap_started/ready/
    failed) -- NEVER via the docker/tor container itself, which stays
    PostgreSQL-credential-free. Only a normalized error_category ever
    reaches the database; the raw phase text / exception here is for the
    caller (this function's return value / raised exception) only, and
    is never itself written to tor_circuit_events.

    This function is invoked explicitly (CLI `main()` below, or a
    direct call) -- never automatically from collector traffic, and
    never as a side effect of ordinary job-search requests.
    """
    control_host = get_tor_control_host()
    control_port = get_tor_control_port()
    instance_key = _instance_lock_key(control_host, control_port)

    connection = psycopg2.connect(**get_postgres_config())

    try:
        record_bootstrap_started(connection, instance_key)

        control_password = get_tor_control_password()

        try:
            with Controller.from_port(
                address=control_host,
                port=control_port,
            ) as controller:
                if control_password:
                    controller.authenticate(password=control_password)
                else:
                    controller.authenticate()

                phase = controller.get_info("status/bootstrap-phase", "")
        except Exception:
            record_bootstrap_failed(connection, instance_key, ERROR_CATEGORY_CONTROL_PORT_FAILURE)
            raise

        if "PROGRESS=100" not in phase:
            record_bootstrap_failed(connection, instance_key, ERROR_CATEGORY_BOOTSTRAP_INCOMPLETE)
            raise RuntimeError(f"Tor has not finished bootstrapping yet: {phase!r}")

        record_bootstrap_ready(connection, instance_key)
        return phase

    finally:
        connection.close()


def check_exit_ip() -> str:
    """Returns the observed Tor exit IP as a non-empty string.

    Raises TorVerificationError -- never returns a placeholder or
    guessed value -- unless the response is valid JSON with IsTor
    exactly `True` (not merely truthy) and a non-empty string IP. This
    is what makes the check load-bearing: a non-Tor endpoint, a broken
    JSON body, or an endpoint honestly reporting IsTor=false must always
    surface as a clear failure, never as a technical "success" carrying
    an unverified IP.
    """
    proxy_config = get_proxy_config()

    if proxy_config is None:
        raise RuntimeError(
            "TOR_ENABLED is false -- set TOR_ENABLED=true before running this check."
        )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(proxy=proxy_config, headless=True)

        try:
            page = browser.new_page()

            page.goto(get_tor_ip_check_url(), wait_until="commit", timeout=30000)

            body_text = page.locator("body").inner_text(timeout=10000)
        finally:
            browser.close()

    return parse_tor_ip_check_response(body_text)


def parse_tor_ip_check_response(body_text: str) -> str:
    """Pure parsing/validation of the Tor IP-check endpoint's response
    body -- factored out of check_exit_ip() so it can be exercised by
    tests without Playwright or a real network call."""
    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError as error:
        raise TorVerificationError(
            f"Tor IP check endpoint did not return valid JSON: {error}"
        ) from error

    if not isinstance(payload, dict):
        raise TorVerificationError(
            f"Tor IP check endpoint returned non-object JSON: {type(payload).__name__}"
        )

    is_tor = payload.get("IsTor")

    if is_tor is not True:
        raise TorVerificationError(
            f"Tor IP check endpoint reports IsTor={is_tor!r} -- traffic is NOT "
            "confirmed routed through Tor"
        )

    ip = payload.get("IP")

    if not ip or not isinstance(ip, str):
        raise TorVerificationError(
            f"Tor IP check endpoint did not return a usable IP: {ip!r}"
        )

    return ip


def main():
    parser = argparse.ArgumentParser(description="Verify outbound connectivity through Tor.")
    parser.add_argument("--rotate", action="store_true", help="Request a new circuit identity first.")
    parser.add_argument("--circuit-id", default=DEFAULT_CIRCUIT_KEY)
    args = parser.parse_args()

    phase = check_bootstrap_status()
    print(f"Tor bootstrap status: {phase.strip()}")

    if args.rotate:
        result = rotate_circuit(circuit_key=args.circuit_id, verify_fn=check_exit_ip)
        print(f"Rotated circuit '{args.circuit_id}': {result}")
        return

    exit_ip = check_exit_ip()
    print(f"Observed Tor exit IP: {exit_ip}")


if __name__ == "__main__":
    main()
