"""Operator-only Tor dark-launch diagnostic (Phase 3).

Runs ONLY as a separate, explicitly-invoked diagnostic process (see
docker-compose.prod.tor.yml's `tor-diagnostic` service, gated behind the
`tor-ops` Compose profile so it never starts as part of an ordinary
`up`/`up -d tor`). That process is the ONLY place in the entire
production compose graph where TOR_ENABLED=true is ever set -- the live
api/db/frontend services (docker-compose.prod.yml) and the tor service
itself (docker-compose.prod.tor.yml) never set it.

IMPORTANT: this script's result describes ONLY its own process's Tor
configuration and connectivity. A successful run here does NOT prove
anything about the live production API's runtime configuration --
`diagnostic_tor_enabled` in the result refers to THIS process, and the
result's own `note` field says so explicitly. Verifying the live API's
TOR_ENABLED value is a SEPARATE check against the actually-running api
container/compose config (e.g. `docker exec jobpulse-api-prod env | grep
TOR_ENABLED`, or `docker compose config`) -- never inferred from this
script.

Reuses, rather than reimplements, the existing Phase 1/2 modules:
  - scripts/tor/verify_tor_connectivity.py's check_bootstrap_status()
    (ControlPort auth + PROGRESS=100 confirmation, persists the
    observation via circuit_manager.record_bootstrap_started/ready/failed)
    and check_exit_ip() (SOCKS connectivity + neutral IsTor=true check).
  - scripts/tor/observability.py's inspect_instance() (read-only) to read
    back the authoritative, already-persisted error category on failure,
    rather than guessing one from exception type.

Never contacts LinkedIn or any collector target -- check_exit_ip() only
ever calls the neutral, configurable TOR_IP_CHECK_URL. Never sends
NEWNYM, never rotates/quarantines/recovers a circuit. Never logs or
prints the ControlPort password (this module never even holds a
reference to it -- app.config.get_tor_control_password() /
Controller.authenticate() are the only things that do, and neither is
ever passed through this module's own logging).

Usage:
    python -m scripts.tor.production_dark_launch_check
    python -m scripts.tor.production_dark_launch_check --timeout-seconds 60
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone

import psycopg2

from app.config import (
    get_postgres_config,
    get_tor_control_host,
    get_tor_control_password,
    get_tor_control_port,
    get_tor_enabled,
    get_tor_ip_check_url,
    get_tor_socks_host,
    get_tor_socks_port,
)
from scripts.tor.observability import inspect_instance
from scripts.tor.verify_tor_connectivity import check_bootstrap_status, check_exit_ip

# Categorized, stable exit codes -- an operator/CI script consuming this
# diagnostic can branch on these without parsing free-text output.
EXIT_OK = 0
EXIT_TOR_NOT_ENABLED_FOR_DIAGNOSTIC = 2
EXIT_BOOTSTRAP_FAILED = 3
EXIT_CONTROL_PORT_FAILED = 4
EXIT_SOCKS_OR_VERIFICATION_FAILED = 5
EXIT_DATABASE_UNAVAILABLE = 6
EXIT_UNEXPECTED_ERROR = 7

_DEFAULT_TIMEOUT_SECONDS = 45


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_MAX_REDACTED_ERROR_LENGTH = 4000


def _resolve_known_secret_values_for_redaction():
    """Best-effort collection of EVERY credential value that could appear
    in a diagnostic exception's text, used ONLY to redact them out of
    error text before that text is ever stored in the result dict or
    printed -- never itself logged, printed, or otherwise exposed by this
    function. Covers both the Tor ControlPort password and the PostgreSQL
    password from get_postgres_config() (which also covers
    credential-bearing PostgreSQL DSN/URL forms: a DSN embeds the same
    literal password substring, so redacting that substring redacts it
    out of a DSN too, wherever it appears). If reading any one of these
    fails (e.g. TOR_CONTROL_PASSWORD_FILE unreadable), that one is simply
    omitted rather than raising: a failure to resolve a secret for
    redaction purposes must never itself surface as a new, unredacted
    error."""
    secrets = []

    try:
        tor_password = get_tor_control_password()
        if tor_password:
            secrets.append(tor_password)
    except Exception:
        pass

    try:
        postgres_password = get_postgres_config().get("password")
        if postgres_password:
            secrets.append(postgres_password)
    except Exception:
        pass

    return secrets


def _redact_error_text(text) -> str:
    """Central redaction point for EVERY diagnostic error message before
    it reaches the result dict (and therefore stdout/JSON/stderr) --
    applied uniformly to database-unavailable, control-port/bootstrap,
    SOCKS/verification, and unexpected-error text alike, so no failure
    path can bypass it. Replaces every currently-configured credential
    value known to this module (Tor ControlPort password, PostgreSQL
    password) with a fixed, non-secret marker, then bounds the result's
    length so an unbounded/adversarial exception message can't blow up
    stored/printed output. A falsy/empty secret value is deliberately
    never used as a replacement target -- `str.replace(text, "")` against
    every text would corrupt unrelated messages, and an empty value is
    not a secret worth redacting anyway."""
    if not text:
        return text

    for secret in _resolve_known_secret_values_for_redaction():
        text = text.replace(secret, "***REDACTED***")

    if len(text) > _MAX_REDACTED_ERROR_LENGTH:
        text = text[:_MAX_REDACTED_ERROR_LENGTH] + "...***TRUNCATED***"

    return text


def _read_authoritative_error_category() -> str:
    """After check_bootstrap_status() fails, it has already persisted a
    normalized error_category to tor_instances.last_bootstrap_error_category
    (via circuit_manager.record_bootstrap_failed) -- read that back
    through the existing read-only observability module rather than
    re-deriving a category from the caught exception's type, which would
    be a fragile, duplicate source of truth. Returns None if it can't be
    read (e.g. the database became unavailable between the failure and
    this read) -- callers must handle that."""
    connection = psycopg2.connect(**get_postgres_config())

    try:
        instance_state = inspect_instance(connection)
        return instance_state.get("last_bootstrap_error_category")
    finally:
        connection.close()


def run_diagnostic(timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Runs the full dark-launch diagnostic sequence and returns a
    structured, secret-free result dict. Never raises for an expected
    Tor/DB failure -- those are captured in the returned dict's
    `error_category`/`error_message`/`ok` fields so main() can map them to
    a stable exit code; only a genuinely unexpected exception propagates
    to main()'s own outer handler."""
    started_at_monotonic = time.monotonic()

    result = {
        "diagnostic_tor_enabled": get_tor_enabled(),
        "note": (
            "This result describes ONLY this diagnostic process's own Tor "
            "configuration and connectivity. It does NOT prove the live "
            "production API uses Tor -- verify the API's TOR_ENABLED value "
            "independently against the running api container/compose config."
        ),
        "started_at": _utc_now_iso(),
        "checked_at": None,
        "socks_endpoint": f"{get_tor_socks_host()}:{get_tor_socks_port()}",
        "control_endpoint": f"{get_tor_control_host()}:{get_tor_control_port()}",
        "tor_ip_check_url": get_tor_ip_check_url(),
        "checks": {
            "tor_enabled_for_diagnostic": False,
            "control_port_auth_and_bootstrap_100": False,
            "socks_and_neutral_endpoint_verified": False,
        },
        "bootstrap_phase": None,
        "error_category": None,
        "error_message": None,
        "ok": False,
    }

    if not get_tor_enabled():
        result["error_category"] = "tor_not_enabled_for_diagnostic"
        result["error_message"] = (
            "TOR_ENABLED is false in THIS diagnostic process's own "
            "environment. Set TOR_ENABLED=true for the diagnostic process "
            "only (see docker-compose.prod.tor.yml's tor-diagnostic "
            "service) -- this is expected and correct for the live "
            "production API, which must stay TOR_ENABLED=false."
        )
        result["checked_at"] = _utc_now_iso()
        return result

    result["checks"]["tor_enabled_for_diagnostic"] = True

    if time.monotonic() - started_at_monotonic > timeout_seconds:
        result["error_category"] = "diagnostic_timeout"
        result["error_message"] = "Timeout exceeded before any check could run."
        result["checked_at"] = _utc_now_iso()
        return result

    # --- ControlPort auth + bootstrap PROGRESS=100 ---
    # (check_bootstrap_status() performs both and persists the Phase 2
    # bootstrap observation itself.)
    try:
        phase = check_bootstrap_status()
        result["checks"]["control_port_auth_and_bootstrap_100"] = True
        result["bootstrap_phase"] = phase.strip()
    except Exception as error:
        try:
            authoritative_category = _read_authoritative_error_category()
        except Exception:
            authoritative_category = None

        result["error_category"] = authoritative_category or "control_port_or_bootstrap_failure"
        result["error_message"] = _redact_error_text(str(error))
        result["checked_at"] = _utc_now_iso()
        return result

    if time.monotonic() - started_at_monotonic > timeout_seconds:
        result["error_category"] = "diagnostic_timeout"
        result["error_message"] = "Timeout exceeded after bootstrap check, before SOCKS check."
        result["checked_at"] = _utc_now_iso()
        return result

    # --- SOCKS connectivity + neutral Tor-check endpoint (never LinkedIn) ---
    try:
        check_exit_ip()
        result["checks"]["socks_and_neutral_endpoint_verified"] = True
    except Exception as error:
        result["error_category"] = "socks_or_verification_failed"
        result["error_message"] = _redact_error_text(str(error))
        result["checked_at"] = _utc_now_iso()
        return result

    result["ok"] = True
    result["checked_at"] = _utc_now_iso()
    return result


def _exit_code_for(result: dict) -> int:
    if result["ok"]:
        return EXIT_OK

    category = result["error_category"]

    if category == "tor_not_enabled_for_diagnostic":
        return EXIT_TOR_NOT_ENABLED_FOR_DIAGNOSTIC
    if category == "diagnostic_timeout":
        return EXIT_BOOTSTRAP_FAILED
    if category == "control_port_failure":
        return EXIT_CONTROL_PORT_FAILED
    if category == "bootstrap_incomplete":
        return EXIT_BOOTSTRAP_FAILED
    if category == "control_port_or_bootstrap_failure":
        # Authoritative category could not be read back (e.g. DB became
        # unavailable between the failure and the read) -- fall back to
        # the least-specific of the two bootstrap-stage exit codes.
        return EXIT_BOOTSTRAP_FAILED
    if category == "socks_or_verification_failed":
        return EXIT_SOCKS_OR_VERIFICATION_FAILED

    return EXIT_UNEXPECTED_ERROR


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Operator-only Tor dark-launch diagnostic. Verifies THIS "
            "process's own Tor connectivity (ControlPort auth, bootstrap, "
            "SOCKS, neutral IsTor check), persists the Phase 2 bootstrap "
            "observation, and prints a structured JSON result. Never "
            "touches LinkedIn or collector code; never rotates a circuit."
        )
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=_DEFAULT_TIMEOUT_SECONDS,
        help="Bounded overall timeout for the whole diagnostic sequence.",
    )
    args = parser.parse_args()

    # Fail fast, with a category distinct from any Tor-side failure, if
    # PostgreSQL (needed to persist the Phase 2 bootstrap observation)
    # isn't reachable at all.
    try:
        connection = psycopg2.connect(**get_postgres_config())
        connection.close()
    except Exception as error:
        result = {
            "diagnostic_tor_enabled": get_tor_enabled(),
            "ok": False,
            "error_category": "database_unavailable",
            "error_message": _redact_error_text(str(error)),
            "checked_at": _utc_now_iso(),
        }
        print(json.dumps(result, indent=2))
        sys.exit(EXIT_DATABASE_UNAVAILABLE)

    try:
        result = run_diagnostic(timeout_seconds=args.timeout_seconds)
    except Exception as error:
        result = {
            "diagnostic_tor_enabled": get_tor_enabled(),
            "ok": False,
            "error_category": "unexpected_error",
            "error_message": _redact_error_text(str(error)),
            "checked_at": _utc_now_iso(),
        }
        print(json.dumps(result, indent=2))
        sys.exit(EXIT_UNEXPECTED_ERROR)

    print(json.dumps(result, indent=2))
    sys.exit(_exit_code_for(result))


if __name__ == "__main__":
    main()
