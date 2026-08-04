import contextlib
import fcntl
import json
import math
import os
import socket
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


# All paths are read once at import time via env vars with the original
# production defaults preserved, so tests can point them at a temp
# directory (via monkeypatch.setattr on the module attribute -- env vars
# set after import have no effect on these) while production behavior is
# unchanged if an operator sets nothing. Paths are not user-supplied
# numeric strings, so there's no unsafe-parsing concern here the way
# there is for REQUEST_TIMEOUT_SECONDS/ALLOW_UNCONFIGURED_TELEGRAM below.
ENV_PATH = Path(os.getenv("JOBPULSE_TELEGRAM_ENV_PATH", "/opt/jobpulse/.telegram_alert.env"))
STATE_PATH = Path(os.getenv("JOBPULSE_TELEGRAM_STATE_PATH", "/opt/jobpulse/logs/telegram_alert_state.json"))
LOCK_PATH = Path(os.getenv("JOBPULSE_TELEGRAM_LOCK_PATH", "/opt/jobpulse/logs/telegram_alert_state.lock"))
ADMIN_TOKEN_PATH = Path(os.getenv("JOBPULSE_ADMIN_TOKEN_PATH", "/opt/jobpulse/.admin_token"))

DEFAULT_REQUEST_TIMEOUT_SECONDS = 20.0
MAX_REQUEST_TIMEOUT_SECONDS = 120.0

# Safe, hardcoded literal defaults only -- NOT parsed from the environment
# at import time. `float(os.getenv(...))` at module scope would (a) raise
# ValueError and crash the whole script on a malformed value like "abc",
# and (b) be evaluated before load_env_file(ENV_PATH) runs, so a value set
# only in .telegram_alert.env could never take effect. run_check() below
# re-derives both of these from the environment, via `global`, right after
# load_env_file() runs -- so they reflect the real per-run configuration,
# not a value frozen at import time before the env file was even read.
REQUEST_TIMEOUT_SECONDS = DEFAULT_REQUEST_TIMEOUT_SECONDS
ALLOW_UNCONFIGURED_TELEGRAM = False


def parse_timeout_seconds(raw, default=DEFAULT_REQUEST_TIMEOUT_SECONDS, maximum=MAX_REQUEST_TIMEOUT_SECONDS):
    """Parse a positive, finite timeout in seconds from a raw string.
    Falls back to `default` for anything empty, malformed, non-finite
    (NaN/Infinity via math.isfinite), zero, or negative -- never raises.
    Values above `maximum` are capped. The raw value is never included in
    any returned/logged text -- callers must not echo it either."""
    if raw is None:
        return default

    trimmed = raw.strip()
    if not trimmed:
        return default

    try:
        value = float(trimmed)
    except (ValueError, OverflowError):
        return default

    if not math.isfinite(value):
        return default

    if value <= 0:
        return default

    return min(value, maximum)


def parse_allow_unconfigured_flag(raw) -> bool:
    """Explicit opt-out only: any value other than one of these exact
    (case-insensitive) tokens keeps the fail-closed default of False."""
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes"}


class AlertAlreadyRunningError(Exception):
    """Raised when the inter-process lock is already held by another run."""


def load_env_file(path: Path):
    if not path.exists():
        return

    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def redact(text, secrets: list[str]) -> str:
    """Strip any literal occurrence of a secret value from text before it
    is ever printed, logged, or embedded in an outgoing alert message."""
    text = "" if text is None else str(text)

    for secret in secrets:
        if secret:
            text = text.replace(secret, "***REDACTED***")

    return text


def truncate(text: str, max_len: int = 200) -> str:
    text = "" if text is None else str(text)
    if len(text) <= max_len:
        return text
    return text[:max_len] + "...(truncated)"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextlib.contextmanager
def state_lock(path: Path):
    """Non-blocking inter-process lock. Tied to the file descriptor's
    lifetime: closing it (including on process exit, crash, or signal)
    always releases the OS-level flock -- there is no separate cleanup
    step that can be skipped and leave the lock permanently held."""
    path.parent.mkdir(parents=True, exist_ok=True)

    lock_file = open(path, "a+")
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise AlertAlreadyRunningError(f"Lock held: {path}") from exc

        yield
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_file.close()


def load_state(path: Path) -> dict:
    """Missing, empty, or malformed state always recovers to a safe empty
    dict rather than raising -- a corrupt state file must never crash the
    alert check."""
    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict) -> None:
    """Atomic write: a temp file in the same directory, fsync'd, then
    renamed over the target with os.replace (atomic on POSIX). A reader
    can never observe a partially-written state file, and a crash mid-write
    leaves the previous state file untouched. Raises on any failure --
    callers must treat that as a hard error, not silent success."""
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(prefix=".telegram_alert_state.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def http_json(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as res:
        return json.loads(res.read().decode("utf-8"))


def send_telegram(token: str, chat_id: str, message: str):
    """Thin transport only. Raises on any transport-level problem; the
    caller (attempt_send) is responsible for classifying failures and for
    validating the Telegram-level "ok" field."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")

    req = urllib.request.Request(url, data=data, method="POST")

    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as res:
        return json.loads(res.read().decode("utf-8"))


def attempt_send(token: str, chat_id: str, message: str) -> tuple[bool, str | None]:
    """A send is successful ONLY when the HTTP request succeeds and the
    decoded JSON object has "ok": true. Every other outcome -- network
    error, timeout, non-2xx, malformed JSON, or "ok": false -- is a
    failure, classified into a sanitized category. No exception from this
    function ever escapes to the caller as an uncontrolled traceback."""
    try:
        response = send_telegram(token, chat_id, message)
    except urllib.error.HTTPError:
        return False, "http_status"
    except (socket.timeout, TimeoutError):
        return False, "timeout"
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            return False, "timeout"
        return False, "network"
    except json.JSONDecodeError:
        return False, "malformed_response"
    except Exception:
        return False, "unknown"

    if isinstance(response, dict) and response.get("ok") is True:
        return True, None

    return False, "telegram_rejected"


def build_alert_message(data: dict, alerts: list[dict]) -> str:
    lines = ["\U0001F6A8 <b>JobPulse Alert</b>", ""]

    for item in alerts:
        level = item.get("level", "warning")
        code = item.get("code", "unknown")
        msg = item.get("message", "")
        emoji = "\U0001F6A8" if level == "critical" else "⚠️"
        lines.append(f"{emoji} <b>{code}</b>: {msg}")

    lines.append("")
    lines.append(f"Total jobs: {data.get('jobs', {}).get('total_jobs', '-')}")
    lines.append(f"Jobs seen 1h: {data.get('jobs', {}).get('jobs_seen_last_hour', '-')}")
    lines.append(f"Bad apply: {data.get('bad_apply', {}).get('bad_external_apply_count', '-')}")
    lines.append(
        f"Disk used: {data.get('disk', {}).get('used_percent', '-')}% "
        f"Free: {data.get('disk', {}).get('free_gb', '-')} GB"
    )

    return "\n".join(lines)


def build_recovery_message(data: dict) -> str:
    return (
        "✅ <b>JobPulse recovered</b>\n"
        "All checks passed.\n\n"
        f"Total jobs: {data.get('jobs', {}).get('total_jobs', '-')}\n"
        f"Jobs seen 1h: {data.get('jobs', {}).get('jobs_seen_last_hour', '-')}\n"
        f"Bad apply: {data.get('bad_apply', {}).get('bad_external_apply_count', '-')}\n"
        f"Disk used: {data.get('disk', {}).get('used_percent', '-')}% "
        f"Free: {data.get('disk', {}).get('free_gb', '-')} GB"
    )


def fetch_admin_status(admin_url: str, admin_token: str, secrets: list[str]) -> dict:
    try:
        return http_json(admin_url, headers={"X-Admin-Token": admin_token})
    except Exception as exc:
        return {
            "status": "error",
            "database": "unknown",
            "alerts": [{
                "level": "critical",
                "code": "admin_status_unreachable",
                "message": f"Admin status endpoint failed: {redact(truncate(str(exc)), secrets)}",
            }],
        }


def run_check() -> int:
    load_env_file(ENV_PATH)

    # Re-derived here, not at import time: must happen AFTER
    # load_env_file() so a value configured only in .telegram_alert.env
    # (not already in the process environment) takes effect. Reassigning
    # the module globals (rather than threading a parameter through every
    # call site) keeps http_json()/send_telegram()'s existing signatures
    # unchanged -- Python resolves a bare global name at call time, so
    # they pick up this run's freshly-parsed value automatically.
    global REQUEST_TIMEOUT_SECONDS, ALLOW_UNCONFIGURED_TELEGRAM
    REQUEST_TIMEOUT_SECONDS = parse_timeout_seconds(os.environ.get("JOBPULSE_TELEGRAM_TIMEOUT_SECONDS"))
    ALLOW_UNCONFIGURED_TELEGRAM = parse_allow_unconfigured_flag(
        os.environ.get("JOBPULSE_TELEGRAM_ALLOW_UNCONFIGURED")
    )

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    admin_url = os.getenv("ADMIN_STATUS_URL", "http://127.0.0.1:8000/api/admin/status").strip()

    if not token or not chat_id:
        missing = []
        if not token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not chat_id:
            missing.append("TELEGRAM_CHAT_ID")
        missing_desc = ", ".join(missing)

        if ALLOW_UNCONFIGURED_TELEGRAM:
            print(
                f"Telegram is not configured (missing: {missing_desc}); "
                "JOBPULSE_TELEGRAM_ALLOW_UNCONFIGURED is set, so this is "
                "treated as an explicit, intentional opt-out. Skipping check."
            )
            return 0

        # No secret value is ever included here -- only the names of the
        # missing environment variables, which are not sensitive. An
        # independently scheduled monitor that can't deliver alerts is a
        # failed check, not a silent no-op: fail closed so this shows up
        # as a non-zero exit (e.g. in run_production_alert_checks.sh's
        # combined status) instead of looking identical to "healthy."
        print(
            f"Telegram is not configured (missing: {missing_desc}). Refusing "
            "to report success for an alert check that cannot deliver "
            "notifications. Set JOBPULSE_TELEGRAM_ALLOW_UNCONFIGURED=1 to "
            "explicitly opt out of this check instead."
        )
        return 1

    try:
        admin_token = ADMIN_TOKEN_PATH.read_text().strip()
    except Exception as exc:
        # Never let a missing/unreadable admin-token file surface as a bare
        # traceback -- this is a controlled, reported failure.
        print(f"Cannot read admin token: {redact(truncate(str(exc)), [])}")
        return 1

    secrets = [token, chat_id, admin_token]

    data = fetch_admin_status(admin_url, admin_token, secrets)
    alerts = data.get("alerts") or []

    state = load_state(STATE_PATH)
    delivered_codes = state.get("delivered_codes", [])
    current_codes = sorted(f"{a.get('level')}:{a.get('code')}" for a in alerts)

    if current_codes == delivered_codes:
        print("No alert state change (already delivered).")
        return 0

    now = utc_now_iso()

    if current_codes:
        message = build_alert_message(data, alerts)
        delivered, category = attempt_send(token, chat_id, message)

        if delivered:
            state["delivered_codes"] = current_codes
            state["last_success_at"] = now
            state["last_attempt_at"] = now
            state["last_failure_category"] = None
        else:
            state["last_attempt_at"] = now
            state["last_failure_category"] = category
            # delivered_codes is intentionally left unchanged: a failed
            # delivery must never be recorded as delivered, so the next
            # independent poll retries this exact alert set.

        try:
            save_state(STATE_PATH, state)
        except Exception as exc:
            print(f"State write failed, cannot confirm alert state: {redact(truncate(str(exc)), secrets)}")
            return 1

        if delivered:
            print("Telegram alert sent.")
            return 0

        print(f"Telegram alert delivery failed: category={category}")
        return 1

    # current_codes is empty: the system is healthy right now. Only send a
    # recovery notification if a prior incident was actually confirmed
    # delivered -- never for one whose opening alert never reached Telegram.
    if not delivered_codes:
        print("No alert state change.")
        return 0

    message = build_recovery_message(data)
    delivered, category = attempt_send(token, chat_id, message)

    if delivered:
        state["delivered_codes"] = []
        state["last_success_at"] = now
        state["last_attempt_at"] = now
        state["last_failure_category"] = None
    else:
        state["last_attempt_at"] = now
        state["last_failure_category"] = category
        # delivered_codes is intentionally left unchanged (still marks the
        # prior failure as delivered) so recovery is retried next poll
        # instead of being silently dropped.

    try:
        save_state(STATE_PATH, state)
    except Exception as exc:
        print(f"State write failed, cannot confirm recovery state: {redact(truncate(str(exc)), secrets)}")
        return 1

    if delivered:
        print("Recovery alert sent.")
        return 0

    print(f"Recovery alert delivery failed: category={category}")
    return 1


def main() -> int:
    try:
        with state_lock(LOCK_PATH):
            return run_check()
    except AlertAlreadyRunningError:
        # Deliberate exit 0, not an oversight: this means "another
        # invocation of this exact check is already running," not
        # "monitoring status is unknown." Every HTTP call this script
        # makes -- fetch_admin_status (always, once) and attempt_send (at
        # most once, for an alert or recovery message) -- is bounded by
        # REQUEST_TIMEOUT_SECONDS (parsed safely post env-load, capped at
        # MAX_REQUEST_TIMEOUT_SECONDS), so the lock has a documented,
        # finite maximum hold time of at most two request timeouts (see
        # "Lock-busy exit semantics" in docs/PRODUCTION_RUNBOOK.md for the
        # exact figure) -- contention means a concurrent duplicate that
        # will finish within that bound, not a stuck or skipped check, so
        # treating it as a failure would be a false positive.
        print("send_telegram_alerts: another instance is already running; exiting cleanly.")
        return 0
    except Exception as exc:
        # Last-resort safety net: any truly unexpected failure must still
        # produce a controlled, sanitized one-line report, never a bare
        # traceback as the only failure signal.
        print(f"send_telegram_alerts: unexpected failure: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
