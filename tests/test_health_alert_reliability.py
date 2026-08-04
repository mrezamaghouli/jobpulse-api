"""
Tests for the production alert reliability remediation (phase 1):

  - scripts/send_telegram_alerts.py
  - scripts/production_health_alert.sh
  - scripts/run_production_alert_checks.sh

Root cause being regression-guarded here: both scripts used to persist
their cooldown/dedup state BEFORE confirming Telegram delivery succeeded
(a bare timestamp write / dict-comparison write that ran unconditionally,
with the actual HTTP call attempted afterward). A failed delivery -- for
any reason: network error, timeout, non-2xx, malformed JSON, or a
`{"ok": false}` body -- therefore silently looked identical to a
successful one from the state file's perspective, suppressing every
subsequent alert for the incident until the cooldown window elapsed (or,
for the Python script's set-based dedup, until the underlying alert set
happened to change) even though nothing was ever actually delivered.

No real Telegram, PostgreSQL, Docker, or production HTTP endpoint is ever
contacted in this file. The shell script is exercised via subprocess
against fake `curl`/`docker` binaries placed first on PATH; the Python
script is exercised by importing the real module and monkeypatching its
transport functions and file-path constants.
"""

import fcntl
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

sys.path.insert(0, str(REPO_ROOT))
import scripts.send_telegram_alerts as sta  # noqa: E402


# =====================================================================
# bash -n syntax checks
# =====================================================================


@pytest.mark.parametrize("script_name", [
    "production_health_alert.sh",
    "run_production_alert_checks.sh",
])
def test_shell_scripts_pass_bash_syntax_check(script_name):
    result = subprocess.run(
        ["bash", "-n", str(SCRIPTS / script_name)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_run_production_alert_checks_is_executable_on_disk():
    """The documented cron example (see docs/PRODUCTION_RUNBOOK.md) invokes
    this wrapper directly (`/opt/jobpulse/scripts/run_production_alert_checks.sh`),
    not via `bash scripts/...` -- so it must carry the executable bit,
    matching scripts/production_health_alert.sh. This is a filesystem-mode
    check (not a `git ls-files -s` check) so it also protects against an
    editor/tool silently stripping the bit before a later `git add`."""
    path = SCRIPTS / "run_production_alert_checks.sh"
    mode = path.stat().st_mode
    assert mode & stat.S_IXUSR, f"{path} is missing the owner-executable bit"


# =====================================================================
# Shared fake `curl` / `docker` binaries for the shell-script tests
# =====================================================================

FAKE_CURL = r"""#!/usr/bin/env bash
# Fake curl: no network access. Behavior controlled via env vars:
#   FAKE_API_HEALTH_RESULT=ok|fail
#   FAKE_TELEGRAM_SCENARIO=success|network_failure|timeout|non_2xx|ok_false|malformed
args=("$@")

if [ "$1" = "--help" ] && [ "$2" = "all" ]; then
  echo "     --fail-with-body              Fail on HTTP errors but save the body"
  exit 0
fi

is_telegram=0
out_file=""
for ((i = 0; i < ${#args[@]}; i++)); do
  if [ "${args[$i]}" = "-X" ] && [ "${args[$((i+1))]}" = "POST" ]; then
    is_telegram=1
  fi
  if [ "${args[$i]}" = "-o" ]; then
    out_file="${args[$((i+1))]}"
  fi
done

if [ "$is_telegram" = "1" ]; then
  scenario="${FAKE_TELEGRAM_SCENARIO:-success}"
  case "$scenario" in
    success)
      printf '{"ok":true,"result":{"message_id":1}}' > "$out_file"
      printf '200'
      # Defaults to 0 (real success) -- overridable via
      # FAKE_TELEGRAM_CURL_EXIT_CODE to simulate curl itself reporting a
      # non-zero exit despite writing a 200/ok:true body (e.g. 23: local
      # write failure, 18: partial/truncated transfer), independent of
      # the HTTP status and body content curl happened to produce.
      exit "${FAKE_TELEGRAM_CURL_EXIT_CODE:-0}"
      ;;
    network_failure)
      : > "$out_file"
      echo "curl: (6) Could not resolve host: api.telegram.org" >&2
      printf '000'
      exit 6
      ;;
    timeout)
      : > "$out_file"
      echo "curl: (28) Operation timed out after 20000 milliseconds" >&2
      printf '000'
      exit 28
      ;;
    non_2xx)
      printf '{"ok":false,"error_code":500,"description":"Internal Server Error"}' > "$out_file"
      printf '500'
      exit 0
      ;;
    ok_false)
      printf '{"ok":false,"error_code":400,"description":"Bad Request: chat not found"}' > "$out_file"
      printf '200'
      exit 0
      ;;
    malformed)
      printf '<html>502 Bad Gateway</html>' > "$out_file"
      printf '200'
      exit 0
      ;;
  esac
fi

if [ "${FAKE_API_HEALTH_RESULT:-ok}" = "ok" ]; then
  echo '{"status":"ok"}'
  exit 0
else
  echo "curl: (7) Failed to connect to localhost port 80: Connection refused" >&2
  exit 7
fi
"""

FAKE_DOCKER = r"""#!/usr/bin/env bash
# Fake docker: no real Docker daemon is touched.
if [ "$1" = "compose" ]; then
  shift
  shift 2  # drop "-f" "<compose file>"
  sub="$1"; shift
  case "$sub" in
    ps)
      if [ "$1" = "-q" ]; then
        if [ "${FAKE_DOCKER_SERVICE_MISSING:-0}" = "1" ]; then
          echo ""
        else
          echo "fakecid123"
        fi
      else
        echo "fake compose ps output"
      fi
      ;;
    exec)
      echo "${FAKE_DOCKER_JOBS_COUNT:-42}"
      ;;
  esac
elif [ "$1" = "inspect" ]; then
  echo "${FAKE_DOCKER_SERVICE_STATE:-running}"
fi
"""

# A worst-case fake curl that deliberately reflects the live
# TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID values back into its error text
# and response bodies for every failure category. Real Telegram/network
# errors would not normally do this, but this stub exists specifically to
# prove production_health_alert.sh's redact() strips secrets from every
# logged code path even in a worst-case leaky-upstream scenario -- not
# just the happy path where the secret never appears in the first place.
FAKE_CURL_LEAKY = r"""#!/usr/bin/env bash
args=("$@")

if [ "$1" = "--help" ] && [ "$2" = "all" ]; then
  echo "     --fail-with-body              Fail on HTTP errors but save the body"
  exit 0
fi

is_telegram=0
out_file=""
for ((i = 0; i < ${#args[@]}; i++)); do
  if [ "${args[$i]}" = "-X" ] && [ "${args[$((i+1))]}" = "POST" ]; then
    is_telegram=1
  fi
  if [ "${args[$i]}" = "-o" ]; then
    out_file="${args[$((i+1))]}"
  fi
done

if [ "$is_telegram" = "1" ]; then
  scenario="${FAKE_TELEGRAM_SCENARIO:-success}"
  case "$scenario" in
    network_failure)
      : > "$out_file"
      echo "curl: (6) Could not resolve host for bot${TELEGRAM_BOT_TOKEN:-} chat=${TELEGRAM_CHAT_ID:-}" >&2
      printf '000'
      exit 6
      ;;
    timeout)
      : > "$out_file"
      echo "curl: (28) timed out contacting bot${TELEGRAM_BOT_TOKEN:-} chat=${TELEGRAM_CHAT_ID:-}" >&2
      printf '000'
      exit 28
      ;;
    non_2xx)
      printf '{"ok":false,"error_code":500,"description":"Internal Server Error for bot%s chat=%s"}' \
        "${TELEGRAM_BOT_TOKEN:-}" "${TELEGRAM_CHAT_ID:-}" > "$out_file"
      printf '500'
      exit 0
      ;;
    ok_false)
      printf '{"ok":false,"error_code":400,"description":"Bad Request: chat=%s not found for bot%s"}' \
        "${TELEGRAM_CHAT_ID:-}" "${TELEGRAM_BOT_TOKEN:-}" > "$out_file"
      printf '200'
      exit 0
      ;;
    malformed)
      printf '<html>502 Bad Gateway for bot%s chat=%s</html>' \
        "${TELEGRAM_BOT_TOKEN:-}" "${TELEGRAM_CHAT_ID:-}" > "$out_file"
      printf '200'
      exit 0
      ;;
  esac
fi

if [ "${FAKE_API_HEALTH_RESULT:-ok}" = "ok" ]; then
  echo '{"status":"ok"}'
  exit 0
else
  echo "curl: (7) Failed to connect to localhost port 80: Connection refused" >&2
  exit 7
fi
"""

# Like FAKE_DOCKER, but can be made to hang forever on one specific
# subcommand via FAKE_DOCKER_HANG_ON=ps_q|inspect|exec|ps_context. Right
# before sleeping it writes its own PID to FAKE_DOCKER_HANG_MARKER, so a
# test can prove (a) the hang actually started, and (b) after the
# wrapping `timeout` call fires, that exact PID is no longer alive --
# i.e. no orphan process survives.
FAKE_DOCKER_HANGABLE = r"""#!/usr/bin/env bash
_hang_if_requested() {
  if [ "${FAKE_DOCKER_HANG_ON:-}" = "$1" ]; then
    if [ -n "${FAKE_DOCKER_HANG_MARKER:-}" ]; then
      echo "$$" > "${FAKE_DOCKER_HANG_MARKER}"
    fi
    sleep 999999
  fi
}

if [ "$1" = "compose" ]; then
  shift; shift 2
  sub="$1"; shift
  case "$sub" in
    ps)
      if [ "$1" = "-q" ]; then
        _hang_if_requested ps_q
        if [ "${FAKE_DOCKER_SERVICE_MISSING:-0}" = "1" ]; then
          echo ""
        else
          echo "fakecid123"
        fi
      else
        _hang_if_requested ps_context
        echo "fake compose ps output"
      fi
      ;;
    exec)
      _hang_if_requested exec
      echo "${FAKE_DOCKER_JOBS_COUNT:-42}"
      ;;
  esac
elif [ "$1" = "inspect" ]; then
  _hang_if_requested inspect
  echo "${FAKE_DOCKER_SERVICE_STATE:-running}"
fi
"""


def _write_executable(path: Path, content: str):
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def alert_sandbox(tmp_path):
    """Isolated PATH (fake curl/docker) + isolated ROOT for
    production_health_alert.sh. No real Docker/network/Telegram involved."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "curl", FAKE_CURL)
    _write_executable(bin_dir / "docker", FAKE_DOCKER)

    root_dir = tmp_path / "root"
    root_dir.mkdir()

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["JOBPULSE_ALERT_ROOT"] = str(root_dir)
    env["TELEGRAM_BOT_TOKEN"] = "test-bot-token"
    env["TELEGRAM_CHAT_ID"] = "test-chat-id"
    env["FAKE_API_HEALTH_RESULT"] = "ok"
    env["FAKE_DOCKER_JOBS_COUNT"] = "100"
    env["HEALTH_ALERT_COOLDOWN_SECONDS"] = "3600"

    return {"env": env, "root": root_dir}


def run_health_alert(sandbox, **env_overrides):
    env = dict(sandbox["env"])
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(SCRIPTS / "production_health_alert.sh")],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def state_file(sandbox) -> Path:
    return sandbox["root"] / "state" / "health_alert_state.json"


def read_state(sandbox) -> dict:
    path = state_file(sandbox)
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def unhealthy(sandbox):
    sandbox["env"]["FAKE_API_HEALTH_RESULT"] = "fail"


@pytest.fixture
def leaky_alert_sandbox(tmp_path):
    """Same as alert_sandbox, but curl is the worst-case FAKE_CURL_LEAKY
    stub that echoes the live secrets back into its own error/response
    text -- used only to prove redact() strips them regardless."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "curl", FAKE_CURL_LEAKY)
    _write_executable(bin_dir / "docker", FAKE_DOCKER)

    root_dir = tmp_path / "root"
    root_dir.mkdir()

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["JOBPULSE_ALERT_ROOT"] = str(root_dir)
    env["TELEGRAM_BOT_TOKEN"] = "super-secret-bot-token-98765"
    env["TELEGRAM_CHAT_ID"] = "super-secret-chat-id-12345"
    env["FAKE_API_HEALTH_RESULT"] = "fail"
    env["FAKE_DOCKER_JOBS_COUNT"] = "100"
    env["HEALTH_ALERT_COOLDOWN_SECONDS"] = "3600"

    return {"env": env, "root": root_dir}


@pytest.mark.parametrize("scenario", ["network_failure", "timeout", "non_2xx", "ok_false", "malformed"])
def test_shell_secrets_never_appear_in_output_or_logs_for_any_failure_category(leaky_alert_sandbox, scenario):
    """Even against a worst-case upstream that reflects the bot token and
    chat id back in its error text / response body, redact() must strip
    both from every place this script writes: captured stdout, captured
    stderr, and the persistent alert log file."""
    token = leaky_alert_sandbox["env"]["TELEGRAM_BOT_TOKEN"]
    chat_id = leaky_alert_sandbox["env"]["TELEGRAM_CHAT_ID"]
    leaky_alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = scenario

    result = run_health_alert(leaky_alert_sandbox)

    log_path = leaky_alert_sandbox["root"] / "logs" / "health_alert.log"
    log_content = log_path.read_text() if log_path.exists() else ""

    for surface_name, surface in (
        ("stdout", result.stdout),
        ("stderr", result.stderr),
        ("alert_log", log_content),
    ):
        assert token not in surface, f"bot token leaked into {surface_name} for scenario={scenario}"
        assert chat_id not in surface, f"chat id leaked into {surface_name} for scenario={scenario}"

    # The failure must still be visibly reported (not silently swallowed)
    # -- redaction must remove secrets, not remove diagnostics entirely.
    assert "telegram_send_failed" in result.stdout


def test_shell_telegram_api_url_is_never_logged(leaky_alert_sandbox):
    """The Telegram API URL itself (https://api.telegram.org/bot<TOKEN>/...)
    must never be logged, independent of the token-redaction check above --
    guards against some future log line embedding the constructed URL."""
    leaky_alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = "ok_false"
    result = run_health_alert(leaky_alert_sandbox)

    log_path = leaky_alert_sandbox["root"] / "logs" / "health_alert.log"
    log_content = log_path.read_text() if log_path.exists() else ""

    for surface in (result.stdout, result.stderr, log_content):
        assert "api.telegram.org" not in surface


def test_shell_raw_response_body_is_never_logged_only_bounded_description(leaky_alert_sandbox):
    """Logging must carry a sanitized, bounded description -- never a dump
    of the raw response body. The malformed-body scenario's literal HTML
    payload must not appear verbatim in any log output."""
    leaky_alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = "malformed"
    result = run_health_alert(leaky_alert_sandbox)

    log_path = leaky_alert_sandbox["root"] / "logs" / "health_alert.log"
    log_content = log_path.read_text() if log_path.exists() else ""

    assert "<html>502 Bad Gateway" not in result.stdout
    assert "<html>502 Bad Gateway" not in log_content
    assert "unparseable_response_body" in result.stdout


# =====================================================================
# Shell alert tests
# =====================================================================


def test_shell_successful_telegram_writes_cooldown_only_after_success(alert_sandbox):
    unhealthy(alert_sandbox)
    alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = "success"

    result = run_health_alert(alert_sandbox)

    assert result.returncode == 1  # health check itself still failed
    state = read_state(alert_sandbox)
    assert state["failure_delivered"] is True
    assert state["last_success_epoch"] is not None
    assert "telegram_send_ok" in result.stdout


def test_shell_curl_exit_zero_with_2xx_and_ok_true_is_success(alert_sandbox):
    """Positive control for the curl-exit-status tests below: the genuine
    success path (curl exit 0, HTTP 200, ok:true) must still work."""
    unhealthy(alert_sandbox)
    alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = "success"
    alert_sandbox["env"]["FAKE_TELEGRAM_CURL_EXIT_CODE"] = "0"

    result = run_health_alert(alert_sandbox)

    assert "telegram_send_ok" in result.stdout
    state = read_state(alert_sandbox)
    assert state["failure_delivered"] is True
    assert state["last_success_epoch"] is not None


@pytest.mark.parametrize("curl_exit_code", [23, 18])
def test_shell_nonzero_curl_exit_with_2xx_and_ok_true_body_is_failure(alert_sandbox, curl_exit_code):
    """Regression guard: a curl exit code of 23 (local write failure) or
    18 (partial/truncated transfer) must be treated as a failed delivery
    even though curl still printed HTTP 200 and wrote a body containing
    "ok": true -- a 2xx status and an ok:true body are each necessary but
    not sufficient; curl's own exit code must also be exactly 0."""
    unhealthy(alert_sandbox)
    alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = "success"
    alert_sandbox["env"]["FAKE_TELEGRAM_CURL_EXIT_CODE"] = str(curl_exit_code)

    result = run_health_alert(alert_sandbox)

    assert "telegram_send_ok" not in result.stdout, (
        f"curl exit {curl_exit_code} with a 200/ok:true body must never be logged as a successful send"
    )
    assert f"category=transport_error" in result.stdout
    assert f"curl_exit={curl_exit_code}" in result.stdout

    state = read_state(alert_sandbox)
    assert state["failure_delivered"] is False, (
        "a non-zero curl exit must never set failure_delivered=true (successful-delivery state)"
    )
    assert state["last_success_epoch"] is None, (
        "a non-zero curl exit must never write the successful-delivery cooldown"
    )
    assert result.returncode != 0


@pytest.mark.parametrize("curl_exit_code", [23, 18])
def test_shell_nonzero_curl_exit_leaves_incident_retry_eligible(alert_sandbox, curl_exit_code):
    """The next poll must retry immediately -- a contradictory 200/ok:true
    body with a non-zero curl exit must not be mistaken for an already
    -delivered incident and suppressed by cooldown."""
    unhealthy(alert_sandbox)
    alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = "success"
    alert_sandbox["env"]["FAKE_TELEGRAM_CURL_EXIT_CODE"] = str(curl_exit_code)

    first = run_health_alert(alert_sandbox)
    assert "telegram_send_ok" not in first.stdout

    # Next poll: curl now genuinely succeeds -- must actually attempt
    # (not be suppressed as if the prior attempt had already delivered).
    alert_sandbox["env"]["FAKE_TELEGRAM_CURL_EXIT_CODE"] = "0"
    second = run_health_alert(alert_sandbox)

    assert "health_failed_but_alert_in_cooldown" not in second.stdout, (
        "a non-zero curl exit must not suppress the very next retry via cooldown"
    )
    assert "telegram_send_ok" in second.stdout
    state = read_state(alert_sandbox)
    assert state["failure_delivered"] is True


@pytest.mark.parametrize("curl_exit_code", [23, 18])
def test_shell_nonzero_curl_exit_does_not_leak_secrets_or_body(alert_sandbox, curl_exit_code):
    """The transport_error branch must never log the token, chat id, API
    URL, raw response body, or request payload -- only the non-sensitive
    http_code and curl's own exit code."""
    token = alert_sandbox["env"]["TELEGRAM_BOT_TOKEN"]
    chat_id = alert_sandbox["env"]["TELEGRAM_CHAT_ID"]
    unhealthy(alert_sandbox)
    alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = "success"
    alert_sandbox["env"]["FAKE_TELEGRAM_CURL_EXIT_CODE"] = str(curl_exit_code)

    result = run_health_alert(alert_sandbox)

    log_path = alert_sandbox["root"] / "logs" / "health_alert.log"
    log_content = log_path.read_text() if log_path.exists() else ""

    for surface_name, surface in (
        ("stdout", result.stdout),
        ("stderr", result.stderr),
        ("alert_log", log_content),
    ):
        assert token not in surface, f"bot token leaked into {surface_name}"
        assert chat_id not in surface, f"chat id leaked into {surface_name}"
        assert "api.telegram.org" not in surface, f"API URL leaked into {surface_name}"
        assert '{"ok":true' not in surface, f"raw response body leaked into {surface_name}"


@pytest.mark.parametrize("scenario", ["network_failure", "timeout", "non_2xx", "ok_false", "malformed"])
def test_shell_failed_delivery_does_not_write_successful_cooldown(alert_sandbox, scenario):
    unhealthy(alert_sandbox)
    alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = scenario

    result = run_health_alert(alert_sandbox)

    assert result.returncode == 1
    state = read_state(alert_sandbox)
    assert state["failure_delivered"] is False
    assert state["last_success_epoch"] is None
    assert "health_alert_check_finished status=FAILED delivered=false" in result.stdout


def test_shell_retry_is_not_suppressed_after_a_failed_delivery(alert_sandbox):
    """This is the core regression guard: a failed delivery must leave the
    incident immediately eligible for retry on the next poll -- the old
    bug wrote the cooldown timestamp regardless of outcome."""
    unhealthy(alert_sandbox)
    alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = "network_failure"

    first = run_health_alert(alert_sandbox)
    assert "health_failed_but_alert_in_cooldown" not in first.stdout

    second = run_health_alert(alert_sandbox)
    assert "health_failed_but_alert_in_cooldown" not in second.stdout, (
        "a failed delivery must not suppress the very next retry"
    )
    assert "telegram_send_failed" in second.stdout


def test_shell_successful_delivery_then_suppresses_within_cooldown(alert_sandbox):
    unhealthy(alert_sandbox)
    alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = "success"
    first = run_health_alert(alert_sandbox)
    assert "telegram_send_ok" in first.stdout

    # If a second delivery were attempted here it would use this failing
    # scenario -- so seeing no telegram_send_* line at all proves it was
    # suppressed by cooldown, not that it happened to fail again.
    alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = "network_failure"
    second = run_health_alert(alert_sandbox)

    assert "health_failed_but_alert_in_cooldown" in second.stdout
    assert "telegram_send_failed" not in second.stdout
    assert "telegram_send_ok" not in second.stdout


def test_shell_corrupt_timestamp_does_not_crash_arithmetic(alert_sandbox):
    unhealthy(alert_sandbox)
    alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = "success"
    run_health_alert(alert_sandbox)  # seed a real state file

    path = state_file(alert_sandbox)
    corrupt = json.loads(path.read_text())
    corrupt["last_success_epoch"] = "not-a-number"
    path.write_text(json.dumps(corrupt))

    result = run_health_alert(alert_sandbox)

    assert "syntax error" not in result.stderr
    assert "value too great for base" not in result.stderr
    assert result.returncode in (0, 1)


def test_shell_recovery_not_sent_when_failure_alert_was_never_delivered(alert_sandbox):
    unhealthy(alert_sandbox)
    alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = "network_failure"
    run_health_alert(alert_sandbox)  # failure detected, delivery fails

    alert_sandbox["env"]["FAKE_API_HEALTH_RESULT"] = "ok"
    # If recovery were attempted, this failing scenario would show up in
    # the log as a delivery attempt/failure; its total absence proves no
    # recovery message was ever attempted.
    alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = "network_failure"

    result = run_health_alert(alert_sandbox)

    assert result.returncode == 0
    assert "telegram_send_failed" not in result.stdout
    assert "recovery_delivery_failed" not in result.stdout
    assert "recovery_delivered=" not in result.stdout


def test_shell_successful_failure_alert_then_healthy_sends_one_recovery(alert_sandbox):
    unhealthy(alert_sandbox)
    alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = "success"
    run_health_alert(alert_sandbox)

    alert_sandbox["env"]["FAKE_API_HEALTH_RESULT"] = "ok"
    recovered = run_health_alert(alert_sandbox)
    assert "recovery_delivered=true" in recovered.stdout

    state = read_state(alert_sandbox)
    assert state["failure_delivered"] is False

    still_healthy = run_health_alert(alert_sandbox)
    assert "telegram_send_ok" not in still_healthy.stdout
    assert "recovery" not in still_healthy.stdout


def test_shell_failed_recovery_is_retried_on_every_subsequent_healthy_poll(alert_sandbox):
    """Regression guard for the stuck-incident bug: the recovery retry gate
    previously required last_detected_status == "FAILED", but a failed
    recovery attempt sets last_detected_status=OK (the real, current
    status) while leaving failure_delivered=1 -- so the gate silently
    closed forever and no recovery was ever retried, even though the
    incident's opening alert was delivered and never followed up.

    Sequence: poll 1 unhealthy (opening alert delivered) -> poll 2 healthy
    (recovery delivery fails) -> poll 3 healthy (recovery delivery
    succeeds). Poll 3 must still attempt and deliver the recovery."""
    unhealthy(alert_sandbox)
    alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = "success"
    poll1 = run_health_alert(alert_sandbox)
    assert poll1.returncode == 1
    assert "telegram_send_ok" in poll1.stdout
    state1 = read_state(alert_sandbox)
    assert state1["failure_delivered"] is True
    assert state1["last_detected_status"] == "FAILED"

    alert_sandbox["env"]["FAKE_API_HEALTH_RESULT"] = "ok"
    alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = "network_failure"
    poll2 = run_health_alert(alert_sandbox)
    assert poll2.returncode == 1
    assert "recovery_delivery_failed" in poll2.stdout
    state2 = read_state(alert_sandbox)
    assert state2["failure_delivered"] is True, (
        "a failed recovery delivery must leave the recovery-pending marker set"
    )
    assert state2["last_detected_status"] == "OK", (
        "last_detected_status must reflect the real current status even "
        "though recovery delivery failed"
    )

    alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = "success"
    poll3 = run_health_alert(alert_sandbox)
    assert poll3.returncode == 0
    assert "recovery_delivered=true" in poll3.stdout, (
        "a failed recovery must remain retry-eligible on every subsequent "
        "healthy poll until confirmed delivered -- it must not be "
        "permanently suppressed"
    )
    state3 = read_state(alert_sandbox)
    assert state3["failure_delivered"] is False


def test_shell_recovery_gate_does_not_require_failed_status_to_have_survived(alert_sandbox):
    """The recovery-pending marker (failure_delivered) is the sole gate for
    attempting a recovery send -- last_detected_status is only an
    observational field and must never be required to equal "FAILED" for
    the retry to fire, since a prior failed recovery attempt legitimately
    sets it to "OK" while the incident is still open."""
    state_dir = alert_sandbox["root"] / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file(alert_sandbox).write_text(json.dumps({
        "last_detected_status": "OK",
        "failure_delivered": True,
        "last_success_epoch": 0,
        "last_attempt_epoch": 0,
        "last_failure_category": "network",
    }))

    alert_sandbox["env"]["FAKE_API_HEALTH_RESULT"] = "ok"
    alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = "success"
    result = run_health_alert(alert_sandbox)

    assert "recovery_delivered=true" in result.stdout
    state = read_state(alert_sandbox)
    assert state["failure_delivered"] is False


def test_shell_concurrent_invocation_is_rejected_cleanly(alert_sandbox):
    state_dir = alert_sandbox["root"] / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "health_alert.lock"

    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = run_health_alert(alert_sandbox)
        assert result.returncode == 0
        assert "health_alert_already_running" in result.stdout
        # No state must have been written by the blocked invocation.
        assert not state_file(alert_sandbox).exists()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    # Lock must be free again immediately after the holder releases it.
    follow_up = run_health_alert(alert_sandbox)
    assert "health_alert_already_running" not in follow_up.stdout


def test_shell_lock_busy_exit_zero_is_intentional_not_unknown_status(alert_sandbox):
    """Decision record: exit 0 on lock-busy means "another invocation of
    this exact check is already running," not "status unknown." This is
    deliberate (see the comment at the flock check in
    production_health_alert.sh and docs/PRODUCTION_RUNBOOK.md's
    "Lock-busy exit semantics" section) precisely because every external
    call this script makes -- except the un-timed-out docker/psql
    jobs-count query, a documented pre-existing caveat -- is bounded by an
    explicit timeout, so contention is only ever a transient overlap, not
    a stuck check. This test intentionally does NOT change behavior; it
    exists to guard the decision itself against being "fixed" by style
    alone in a future change."""
    state_dir = alert_sandbox["root"] / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "health_alert.lock"

    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = run_health_alert(alert_sandbox)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    assert result.returncode == 0, (
        "lock-busy must exit 0 -- a concurrent duplicate run is not a "
        "check failure"
    )


def test_shell_state_writes_are_valid_json_and_leave_no_stray_temp_files(alert_sandbox):
    unhealthy(alert_sandbox)
    alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = "success"
    run_health_alert(alert_sandbox)

    path = state_file(alert_sandbox)
    data = json.loads(path.read_text())  # must not raise
    assert isinstance(data, dict)

    leftover_temp_files = [
        p for p in path.parent.iterdir()
        if p.name.startswith(".health_alert_state.") and p != path
    ]
    assert leftover_temp_files == []


def test_shell_missing_state_dir_creation_failure_is_a_clear_error(tmp_path):
    """An unwritable state directory must produce a clear, non-zero exit
    -- never silently proceed as if state were being tracked."""
    unwritable_parent = tmp_path / "unwritable"
    unwritable_parent.mkdir()
    unwritable_parent.chmod(0o500)  # no write permission

    env = dict(os.environ)
    env["JOBPULSE_ALERT_ROOT"] = str(tmp_path / "root")
    env["JOBPULSE_ALERT_STATE_DIR"] = str(unwritable_parent / "state")
    env["TELEGRAM_BOT_TOKEN"] = "t"
    env["TELEGRAM_CHAT_ID"] = "c"

    try:
        result = subprocess.run(
            ["bash", str(SCRIPTS / "production_health_alert.sh")],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode != 0
        assert "state_dir_create_failed" in result.stderr
    finally:
        unwritable_parent.chmod(0o700)


# =====================================================================
# Bounded execution: no Docker/DB command may hang production_health_alert.sh
# indefinitely (formerly unbounded -- reproduced and fixed in this pass).
# =====================================================================


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


@pytest.fixture
def hangable_alert_sandbox(tmp_path):
    """Like alert_sandbox, but docker is FAKE_DOCKER_HANGABLE -- healthy
    by default (so exactly one call can be made to hang per test, in
    isolation) with short, explicit timeout/kill-after settings so tests
    run quickly instead of waiting out the 15s production default."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "curl", FAKE_CURL)
    _write_executable(bin_dir / "docker", FAKE_DOCKER_HANGABLE)

    root_dir = tmp_path / "root"
    root_dir.mkdir()

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["JOBPULSE_ALERT_ROOT"] = str(root_dir)
    env["TELEGRAM_BOT_TOKEN"] = "test-bot-token"
    env["TELEGRAM_CHAT_ID"] = "test-chat-id"
    env["FAKE_API_HEALTH_RESULT"] = "ok"
    env["FAKE_TELEGRAM_SCENARIO"] = "success"
    env["FAKE_DOCKER_JOBS_COUNT"] = "100"
    env["HEALTH_ALERT_COOLDOWN_SECONDS"] = "3600"
    env["HEALTH_ALERT_DOCKER_COMMAND_TIMEOUT_SECONDS"] = "2"
    env["HEALTH_ALERT_DB_QUERY_TIMEOUT_SECONDS"] = "2"
    env["HEALTH_ALERT_KILL_AFTER_SECONDS"] = "1"
    env["FAKE_DOCKER_HANG_MARKER"] = str(tmp_path / "hang_pid")

    return {"env": env, "root": root_dir, "hang_marker": tmp_path / "hang_pid"}


def _wait_for_hang_pid(sandbox, timeout=10) -> int:
    marker = sandbox["hang_marker"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        if marker.exists():
            content = marker.read_text().strip()
            if content:
                return int(content)
        time.sleep(0.1)
    raise AssertionError(f"hang marker {marker} was never written -- the hang never started")


@pytest.mark.parametrize("hang_on,expect_service_missing_log", [
    ("ps_q", "ERROR docker_ps_q_failed"),
    ("inspect", "ERROR docker_inspect_failed"),
])
def test_shell_hanging_docker_service_check_is_terminated(hangable_alert_sandbox, hang_on, expect_service_missing_log):
    hangable_alert_sandbox["env"]["FAKE_DOCKER_HANG_ON"] = hang_on

    result = run_health_alert(hangable_alert_sandbox)

    hang_pid = _wait_for_hang_pid(hangable_alert_sandbox)
    assert expect_service_missing_log in result.stdout
    assert "category=timeout" in result.stdout
    assert not _pid_alive(hang_pid), "the hung docker process (and its process group) must be killed -- no orphan"
    assert result.returncode != 0, "a Docker command timeout must mark the health check failed"


def test_shell_hanging_db_query_is_terminated(hangable_alert_sandbox):
    hangable_alert_sandbox["env"]["FAKE_DOCKER_HANG_ON"] = "exec"

    result = run_health_alert(hangable_alert_sandbox)

    hang_pid = _wait_for_hang_pid(hangable_alert_sandbox)
    assert "ERROR jobs_count_failed category=timeout" in result.stdout
    assert not _pid_alive(hang_pid)
    assert result.returncode != 0


def test_shell_hanging_docker_ps_context_is_terminated(hangable_alert_sandbox):
    """The `docker compose ps` call used to build the FAILED alert message
    body (not the primary health signal) must also be bounded -- a hang
    here must not block message construction or the whole check."""
    unhealthy(hangable_alert_sandbox)  # ensures the FAILED branch (which builds this context) is reached
    hangable_alert_sandbox["env"]["FAKE_DOCKER_HANG_ON"] = "ps_context"

    result = run_health_alert(hangable_alert_sandbox)

    hang_pid = _wait_for_hang_pid(hangable_alert_sandbox)
    assert not _pid_alive(hang_pid)
    assert "docker_ps_context_failed category=timeout" in result.stdout
    # The check still completes and still delivers the (fallback-context) alert.
    assert "telegram_send_ok" in result.stdout
    assert result.returncode != 0


def test_shell_lock_released_after_timeout_and_subsequent_invocation_performs_real_check(hangable_alert_sandbox):
    hangable_alert_sandbox["env"]["FAKE_DOCKER_HANG_ON"] = "ps_q"

    first = run_health_alert(hangable_alert_sandbox)
    _wait_for_hang_pid(hangable_alert_sandbox)
    assert "category=timeout" in first.stdout

    # No hang requested this time -- a genuinely fresh, complete run.
    hangable_alert_sandbox["env"]["FAKE_DOCKER_HANG_ON"] = ""
    second = run_health_alert(hangable_alert_sandbox)

    assert "health_alert_already_running" not in second.stdout, (
        "the lock must be released after the timeout -- not held forever"
    )
    assert "health_alert_check_finished status=OK" in second.stdout
    assert second.returncode == 0


def test_shell_missing_timeout_utility_fails_clearly(tmp_path):
    """If GNU `timeout` is not on PATH, every Docker check must fail
    immediately and clearly -- never fall back to running docker
    unbounded. Built via a restricted PATH containing symlinks to every
    binary the script needs EXCEPT `timeout` (and using the (non-hanging)
    fake curl/docker so no real Docker/network is touched)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "curl", FAKE_CURL)
    _write_executable(bin_dir / "docker", FAKE_DOCKER)

    restricted_bin = tmp_path / "restricted_bin"
    restricted_bin.mkdir()
    for name in ("bash", "date", "tee", "mktemp", "df", "awk", "grep",
                 "hostname", "tail", "cat", "flock", "python3", "sh", "rm", "mkdir"):
        real = shutil.which(name)
        if real:
            (restricted_bin / name).symlink_to(real)
    # Deliberately no `timeout` symlink.
    (restricted_bin / "curl").symlink_to(bin_dir / "curl")
    (restricted_bin / "docker").symlink_to(bin_dir / "docker")

    root_dir = tmp_path / "root"
    root_dir.mkdir()

    env = {
        "PATH": str(restricted_bin),
        "JOBPULSE_ALERT_ROOT": str(root_dir),
        "TELEGRAM_BOT_TOKEN": "t",
        "TELEGRAM_CHAT_ID": "c",
        "FAKE_API_HEALTH_RESULT": "ok",
        "FAKE_DOCKER_JOBS_COUNT": "100",
        "HOME": os.environ.get("HOME", "/tmp"),
    }

    result = subprocess.run(
        ["bash", str(SCRIPTS / "production_health_alert.sh")],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30,
    )

    assert "category=timeout_utility_missing" in result.stdout
    assert result.returncode != 0, "missing `timeout` must fail clearly and non-zero, never run docker unbounded"


@pytest.mark.parametrize("bad_value", ["abc", "", "-5", "3.5", "  ", "99999999999999999999", "NaN"])
def test_shell_malformed_docker_timeout_vars_fall_back_safely(alert_sandbox, bad_value):
    """Malformed HEALTH_ALERT_DOCKER_COMMAND_TIMEOUT_SECONDS /
    HEALTH_ALERT_DB_QUERY_TIMEOUT_SECONDS / HEALTH_ALERT_KILL_AFTER_SECONDS
    must fall back to the documented default rather than crashing bash
    arithmetic (e.g. `[ "$x" -ge "$min" ]` on a huge digit string) or
    disabling the timeout protection entirely."""
    env = alert_sandbox["env"]
    env["HEALTH_ALERT_DOCKER_COMMAND_TIMEOUT_SECONDS"] = bad_value
    env["HEALTH_ALERT_DB_QUERY_TIMEOUT_SECONDS"] = bad_value
    env["HEALTH_ALERT_KILL_AFTER_SECONDS"] = bad_value

    result = run_health_alert(alert_sandbox)

    assert "syntax error" not in result.stderr
    assert "value too great for base" not in result.stderr
    if bad_value.strip():
        assert bad_value not in result.stdout, "a malformed duration value must never be echoed into logs"
    assert "health_alert_check_finished status=OK" in result.stdout


def test_shell_excessively_large_docker_timeout_is_capped(alert_sandbox):
    env = alert_sandbox["env"]
    env["HEALTH_ALERT_DOCKER_COMMAND_TIMEOUT_SECONDS"] = "999999"

    result = run_health_alert(alert_sandbox)

    assert "syntax error" not in result.stderr
    assert "999999" not in result.stdout
    assert "health_alert_check_finished status=OK" in result.stdout


# =====================================================================
# validate_duration_seconds must trim only LEADING/TRAILING whitespace,
# never internal whitespace (a prior `tr -d '[:space:]'` implementation
# silently turned a malformed "1 5" into the valid-looking "15"). Tested
# directly against the function as extracted from each script, since the
# distinction matters only at the numeric-value level, not observable
# through the full script's pass/fail behavior when the "bug" and
# "correct" outcomes can coincidentally produce the same number.
# =====================================================================


def _call_validate_duration_seconds(script_path: Path, raw: str, default: str, min_: str, max_: str) -> str:
    result = subprocess.run(
        ["bash", "-c", (
            f'source <(sed -n "/^validate_duration_seconds() {{/,/^}}/p" {script_path}); '
            'validate_duration_seconds "$1" "$2" "$3" "$4"'
        ), "--", raw, default, min_, max_],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize("script_name", [
    "production_health_alert.sh",
    "run_production_alert_checks.sh",
])
class TestValidateDurationSecondsWhitespace:
    def test_leading_and_trailing_whitespace_is_trimmed(self, script_name):
        assert _call_validate_duration_seconds(SCRIPTS / script_name, "  15  ", "99", "1", "120") == "15"

    def test_tab_padding_is_trimmed(self, script_name):
        assert _call_validate_duration_seconds(SCRIPTS / script_name, "\t15\t", "99", "1", "120") == "15"

    def test_internal_space_is_rejected(self, script_name):
        """The core regression guard: "1 5" must fall back to <default>,
        never be silently parsed as 15."""
        assert _call_validate_duration_seconds(SCRIPTS / script_name, "1 5", "99", "1", "120") == "99"

    def test_internal_tab_is_rejected(self, script_name):
        raw = "1\t5"
        assert _call_validate_duration_seconds(SCRIPTS / script_name, raw, "99", "1", "120") == "99"

    def test_internal_newline_is_rejected(self, script_name):
        raw = "1\n5"
        assert _call_validate_duration_seconds(SCRIPTS / script_name, raw, "99", "1", "120") == "99"

    def test_plain_value_still_works(self, script_name):
        assert _call_validate_duration_seconds(SCRIPTS / script_name, "15", "99", "1", "120") == "15"

    def test_empty_falls_back_to_default(self, script_name):
        assert _call_validate_duration_seconds(SCRIPTS / script_name, "", "99", "1", "120") == "99"

    def test_whitespace_only_falls_back_to_default(self, script_name):
        assert _call_validate_duration_seconds(SCRIPTS / script_name, "   ", "99", "1", "120") == "99"

    def test_negative_still_rejected(self, script_name):
        assert _call_validate_duration_seconds(SCRIPTS / script_name, "-5", "99", "1", "120") == "99"

    def test_zero_still_rejected(self, script_name):
        assert _call_validate_duration_seconds(SCRIPTS / script_name, "0", "99", "1", "120") == "99"

    def test_out_of_range_high_still_rejected(self, script_name):
        assert _call_validate_duration_seconds(SCRIPTS / script_name, "150", "99", "1", "120") == "99"

    def test_overflow_length_still_rejected_without_crashing(self, script_name):
        assert _call_validate_duration_seconds(SCRIPTS / script_name, "9" * 30, "99", "1", "120") == "99"

    def test_in_range_value_within_bounds(self, script_name):
        assert _call_validate_duration_seconds(SCRIPTS / script_name, "42", "99", "1", "120") == "42"


# =====================================================================
# Regression guards: state parsing must never evaluate untrusted JSON
# content as shell source (formerly `eval "$(load_state)"`).
# =====================================================================


def _seed_state(sandbox, **fields):
    path = state_file(sandbox)
    path.parent.mkdir(parents=True, exist_ok=True)
    base = {
        "last_detected_status": None,
        "failure_delivered": False,
        "last_success_epoch": None,
        "last_attempt_epoch": None,
        "last_failure_category": None,
    }
    base.update(fields)
    path.write_text(json.dumps(base))


INJECTION_PAYLOADS = [
    ("semicolon_comment", "FAILED; touch {marker} #"),
    ("backtick", "FAILED `touch {marker}`"),
    ("dollar_paren", "FAILED $(touch {marker})"),
    ("dollar_paren_nested", "FAILED $(bash -c 'touch {marker}')"),
    ("quote_break", "FAILED\"; touch {marker}; echo \""),
    ("single_quote_break", "FAILED'; touch {marker}; echo '"),
    ("newline_injection", "FAILED\ntouch {marker}\n#"),
    ("dollar_var_and_backtick", "FAILED $HOME `touch {marker}`"),
]


@pytest.mark.parametrize("name,template", INJECTION_PAYLOADS, ids=[c[0] for c in INJECTION_PAYLOADS])
def test_shell_malicious_state_json_cannot_execute_a_command(alert_sandbox, name, template):
    """The state file is data written by this same script, but must be
    treated as untrusted: a corrupted, hand-edited, or (in a worse-case
    compromise scenario) attacker-written state file must never be able
    to escalate a file-write into command execution just because this
    script later reads it."""
    marker = alert_sandbox["root"] / f"pwned_{name}"
    payload = template.format(marker=marker)

    _seed_state(
        alert_sandbox,
        last_detected_status=payload,
        failure_delivered=True,
        last_success_epoch=1,
        last_attempt_epoch=1,
    )

    unhealthy(alert_sandbox)
    alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = "success"
    result = run_health_alert(alert_sandbox)

    assert not marker.exists(), (
        f"shell injection via state field succeeded for payload: {payload!r}"
    )
    assert "syntax error" not in result.stderr


def test_shell_malicious_last_failure_category_cannot_execute_a_command(alert_sandbox):
    marker = alert_sandbox["root"] / "pwned_category"
    _seed_state(
        alert_sandbox,
        last_detected_status="OK",
        failure_delivered=False,
        last_failure_category=f"network`touch {marker}`",
    )

    result = run_health_alert(alert_sandbox)

    assert not marker.exists()
    assert "syntax error" not in result.stderr
    assert result.returncode == 0


def test_shell_state_field_with_null_byte_does_not_corrupt_field_alignment(alert_sandbox):
    """A JSON string can decode to Python text containing a literal NUL
    character (json.dumps encodes it as \\u0000). Since the field
    transport itself is NUL-delimited, such a value must not be able to
    shift the field boundaries and misassign a later field's value."""
    _seed_state(
        alert_sandbox,
        last_detected_status="FAILED\x00INJECTED",
        failure_delivered=True,
        last_success_epoch=1,
        last_attempt_epoch=1,
        last_failure_category="marker_category",
    )

    unhealthy(alert_sandbox)
    alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = "network_failure"
    result = run_health_alert(alert_sandbox)

    # Must not crash, and must not misroute "marker_category" into the
    # wrong field as a side effect of NUL-byte field-boundary confusion.
    assert "syntax error" not in result.stderr
    assert result.returncode in (0, 1)


@pytest.mark.parametrize("bad_failure_delivered", ["true", "1", 1, "yes", ["x"], {"a": 1}])
def test_shell_non_boolean_failure_delivered_falls_back_to_false(alert_sandbox, bad_failure_delivered):
    """Type validation: only the JSON literal `true` (Python True) may set
    the recovery-pending marker. Any other JSON type/value (a string, an
    int, a list, an object) must fall back to the safe default (false),
    never be passed through or crash the comparison."""
    _seed_state(alert_sandbox, last_detected_status="FAILED", failure_delivered=bad_failure_delivered)

    unhealthy(alert_sandbox)
    alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = "network_failure"
    result = run_health_alert(alert_sandbox)

    assert "syntax error" not in result.stderr
    # A fresh FAILED->FAILED poll with an untrusted/invalid delivered flag
    # must not be treated as "already delivered, in cooldown" -- it must
    # attempt delivery like a fresh incident.
    assert result.returncode in (0, 1)


@pytest.mark.parametrize("bad_epoch", [
    "not-a-number",
    "1234; touch /tmp/should-not-run",
    [1, 2, 3],
    {"a": 1},
    3.5,
    True,
])
def test_shell_non_integer_epoch_fields_fall_back_safely(alert_sandbox, bad_epoch):
    """Type validation: last_success_epoch/last_attempt_epoch must be a
    real (non-bool) int or they fall back to empty, which the script's
    existing numeric-regex guard then normalizes to 0 before any bash
    arithmetic -- never a crash, never a passthrough of non-numeric text
    into `$(( ... ))`."""
    _seed_state(
        alert_sandbox,
        last_detected_status="FAILED",
        failure_delivered=True,
        last_success_epoch=bad_epoch,
        last_attempt_epoch=bad_epoch,
    )

    unhealthy(alert_sandbox)
    alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = "network_failure"
    result = run_health_alert(alert_sandbox)

    assert "syntax error" not in result.stderr
    assert "value too great for base" not in result.stderr
    assert result.returncode in (0, 1)


def test_shell_malformed_top_level_state_json_falls_back_safely(alert_sandbox):
    path = state_file(alert_sandbox)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json::: ")

    unhealthy(alert_sandbox)
    alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = "success"
    result = run_health_alert(alert_sandbox)

    assert "syntax error" not in result.stderr
    assert result.returncode == 1  # health check still failed; delivery succeeded
    state = read_state(alert_sandbox)
    assert state["failure_delivered"] is True


def test_shell_empty_state_file_falls_back_safely(alert_sandbox):
    path = state_file(alert_sandbox)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")

    unhealthy(alert_sandbox)
    alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = "success"
    result = run_health_alert(alert_sandbox)

    assert "syntax error" not in result.stderr
    assert result.returncode == 1
    state = read_state(alert_sandbox)
    assert state["failure_delivered"] is True


def test_shell_missing_state_file_falls_back_safely(alert_sandbox):
    assert not state_file(alert_sandbox).exists()

    unhealthy(alert_sandbox)
    alert_sandbox["env"]["FAKE_TELEGRAM_SCENARIO"] = "success"
    result = run_health_alert(alert_sandbox)

    assert "syntax error" not in result.stderr
    assert result.returncode == 1
    state = read_state(alert_sandbox)
    assert state["failure_delivered"] is True


# =====================================================================
# Python sender tests (scripts/send_telegram_alerts.py)
# =====================================================================


@pytest.fixture
def sta_sandbox(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    lock_path = tmp_path / "state.lock"
    env_path = tmp_path / "nonexistent.env"

    monkeypatch.setattr(sta, "STATE_PATH", state_path)
    monkeypatch.setattr(sta, "LOCK_PATH", lock_path)
    monkeypatch.setattr(sta, "ENV_PATH", env_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat-id")

    original_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if self == sta.ADMIN_TOKEN_PATH:
            return "test-admin-token"
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    return {"state_path": state_path, "lock_path": lock_path}


CRITICAL_ALERT_PAYLOAD = {
    "jobs": {"total_jobs": 1, "jobs_seen_last_hour": 0},
    "bad_apply": {"bad_external_apply_count": 0},
    "disk": {"used_percent": 1, "free_gb": 1},
    "alerts": [{
        "level": "critical",
        "code": "collection_cron_stale",
        "message": "Collection cron appears stale.",
    }],
}
HEALTHY_PAYLOAD = {**CRITICAL_ALERT_PAYLOAD, "alerts": []}


def read_sta_state(sandbox) -> dict:
    path = sandbox["state_path"]
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def run_sta(monkeypatch, payload, sender):
    monkeypatch.setattr(sta, "http_json", lambda url, headers=None: dict(payload))
    monkeypatch.setattr(sta, "send_telegram", sender)
    return sta.main()


def send_ok(token, chat_id, message):
    return {"ok": True, "result": {"message_id": 1}}


def send_not_ok(token, chat_id, message):
    return {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}


def send_network_failure(token, chat_id, message):
    raise urllib.error.URLError("Could not resolve host")


def send_timeout(token, chat_id, message):
    raise socket.timeout("timed out")


def send_http_error(token, chat_id, message):
    raise urllib.error.HTTPError("https://api.telegram.org/x", 500, "Internal Server Error", None, None)


def send_malformed(token, chat_id, message):
    raise json.JSONDecodeError("Expecting value", "<html>", 0)


def test_python_successful_send_records_delivered_code_set(sta_sandbox, monkeypatch):
    rc = run_sta(monkeypatch, CRITICAL_ALERT_PAYLOAD, send_ok)
    assert rc == 0

    state = read_sta_state(sta_sandbox)
    assert state["delivered_codes"] == ["critical:collection_cron_stale"]
    assert state["last_success_at"] is not None


@pytest.mark.parametrize("sender,expected_category", [
    (send_network_failure, "network"),
    (send_timeout, "timeout"),
    (send_http_error, "http_status"),
    (send_malformed, "malformed_response"),
    (send_not_ok, "telegram_rejected"),
])
def test_python_failed_delivery_does_not_record_delivered_code_set(sta_sandbox, monkeypatch, sender, expected_category):
    rc = run_sta(monkeypatch, CRITICAL_ALERT_PAYLOAD, sender)
    assert rc != 0

    state = read_sta_state(sta_sandbox)
    assert "delivered_codes" not in state or state["delivered_codes"] != ["critical:collection_cron_stale"]
    assert state.get("last_failure_category") == expected_category


def test_python_same_incident_retries_after_failed_delivery(sta_sandbox, monkeypatch):
    rc1 = run_sta(monkeypatch, CRITICAL_ALERT_PAYLOAD, send_network_failure)
    assert rc1 != 0

    rc2 = run_sta(monkeypatch, CRITICAL_ALERT_PAYLOAD, send_ok)
    assert rc2 == 0

    state = read_sta_state(sta_sandbox)
    assert state["delivered_codes"] == ["critical:collection_cron_stale"]


def test_python_unchanged_delivered_incident_is_deduplicated(sta_sandbox, monkeypatch):
    run_sta(monkeypatch, CRITICAL_ALERT_PAYLOAD, send_ok)

    def fail_if_called(token, chat_id, message):
        raise AssertionError("sender must not be called for a deduplicated, unchanged incident")

    rc = run_sta(monkeypatch, CRITICAL_ALERT_PAYLOAD, fail_if_called)
    assert rc == 0


def test_python_newly_added_incident_code_triggers_another_send(sta_sandbox, monkeypatch):
    run_sta(monkeypatch, CRITICAL_ALERT_PAYLOAD, send_ok)

    expanded_payload = {
        **CRITICAL_ALERT_PAYLOAD,
        "alerts": CRITICAL_ALERT_PAYLOAD["alerts"] + [
            {"level": "warning", "code": "no_jobs_seen_1h", "message": "no jobs"},
        ],
    }

    sent = {}

    def capture_send(token, chat_id, message):
        sent["called"] = True
        return {"ok": True}

    rc = run_sta(monkeypatch, expanded_payload, capture_send)
    assert rc == 0
    assert sent.get("called") is True

    state = read_sta_state(sta_sandbox)
    assert state["delivered_codes"] == sorted([
        "critical:collection_cron_stale",
        "warning:no_jobs_seen_1h",
    ])


def test_python_recovery_sent_only_for_previously_delivered_incident(sta_sandbox, monkeypatch):
    def fail_if_called(token, chat_id, message):
        raise AssertionError("must not attempt recovery when nothing was ever delivered")

    # No prior delivered incident exists yet.
    rc = run_sta(monkeypatch, HEALTHY_PAYLOAD, fail_if_called)
    assert rc == 0


def test_python_failed_recovery_delivery_does_not_clear_delivered_state(sta_sandbox, monkeypatch):
    run_sta(monkeypatch, CRITICAL_ALERT_PAYLOAD, send_ok)
    assert read_sta_state(sta_sandbox)["delivered_codes"]

    rc = run_sta(monkeypatch, HEALTHY_PAYLOAD, send_network_failure)
    assert rc != 0

    state = read_sta_state(sta_sandbox)
    assert state["delivered_codes"] == ["critical:collection_cron_stale"], (
        "a failed recovery delivery must not clear the delivered-failure marker"
    )


def test_python_successful_recovery_clears_state(sta_sandbox, monkeypatch):
    run_sta(monkeypatch, CRITICAL_ALERT_PAYLOAD, send_ok)

    rc = run_sta(monkeypatch, HEALTHY_PAYLOAD, send_ok)
    assert rc == 0

    state = read_sta_state(sta_sandbox)
    assert state["delivered_codes"] == []


def test_python_corrupt_state_recovers_safely(sta_sandbox, monkeypatch):
    sta_sandbox["state_path"].write_text("{not valid json::: ")

    rc = run_sta(monkeypatch, CRITICAL_ALERT_PAYLOAD, send_ok)
    assert rc == 0

    state = read_sta_state(sta_sandbox)
    assert state["delivered_codes"] == ["critical:collection_cron_stale"]


def test_python_atomic_state_write_leaves_valid_json_and_no_stray_temp_files(sta_sandbox, monkeypatch):
    run_sta(monkeypatch, CRITICAL_ALERT_PAYLOAD, send_ok)

    path = sta_sandbox["state_path"]
    data = json.loads(path.read_text())  # must not raise
    assert isinstance(data, dict)

    leftovers = [
        p for p in path.parent.iterdir()
        if p.name.startswith(".telegram_alert_state.") and p != path
    ]
    assert leftovers == []


def test_python_lock_prevents_concurrent_runs(sta_sandbox, monkeypatch):
    lock_path = sta_sandbox["lock_path"]
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    holder = open(lock_path, "a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        def fail_if_called(token, chat_id, message):
            raise AssertionError("must not run the check body while lock is held elsewhere")

        rc = run_sta(monkeypatch, CRITICAL_ALERT_PAYLOAD, fail_if_called)
        assert rc == 0  # clean exit, not an error
        assert not sta_sandbox["state_path"].exists()
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()

    # Lock is released once the holder's file object is closed.
    rc = run_sta(monkeypatch, CRITICAL_ALERT_PAYLOAD, send_ok)
    assert rc == 0


def test_python_lock_busy_exit_zero_is_intentional_not_unknown_status(sta_sandbox, monkeypatch):
    """Decision record: exit 0 on AlertAlreadyRunningError means "another
    invocation of this exact check is already running," not "status
    unknown." Deliberate (see the comment at the except clause in
    send_telegram_alerts.py:main and docs/PRODUCTION_RUNBOOK.md's
    "Lock-busy exit semantics" section) because the script's one HTTP call
    is bounded by REQUEST_TIMEOUT_SECONDS, so lock contention is only ever
    a transient overlap, never a silently stuck check."""
    lock_path = sta_sandbox["lock_path"]
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    holder = open(lock_path, "a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        rc = run_sta(monkeypatch, CRITICAL_ALERT_PAYLOAD, send_ok)
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()

    assert rc == 0, "lock-busy must exit 0 -- a concurrent duplicate run is not a check failure"


def test_python_no_secret_appears_in_captured_output(sta_sandbox, monkeypatch, capsys):
    secret_token = "test-bot-token"
    secret_admin_token = "test-admin-token"

    def leaky_failure(token, chat_id, message):
        assert token == secret_token
        raise urllib.error.URLError(f"connection refused for token {token}")

    monkeypatch.setattr(sta, "http_json", lambda url, headers=None: dict(CRITICAL_ALERT_PAYLOAD))
    monkeypatch.setattr(sta, "send_telegram", leaky_failure)
    sta.main()

    captured = capsys.readouterr()
    assert secret_token not in captured.out
    assert secret_token not in captured.err
    assert secret_admin_token not in captured.out
    assert secret_admin_token not in captured.err


def test_python_does_not_print_sent_for_ok_false(sta_sandbox, monkeypatch, capsys):
    run_sta(monkeypatch, CRITICAL_ALERT_PAYLOAD, send_not_ok)
    captured = capsys.readouterr()
    assert "Telegram alert sent." not in captured.out


def test_python_missing_admin_token_is_a_controlled_failure_not_a_traceback(monkeypatch, tmp_path):
    monkeypatch.setattr(sta, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(sta, "LOCK_PATH", tmp_path / "state.lock")
    monkeypatch.setattr(sta, "ENV_PATH", tmp_path / "nope.env")
    monkeypatch.setattr(sta, "ADMIN_TOKEN_PATH", tmp_path / "does_not_exist_admin_token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")

    rc = sta.main()  # must not raise
    assert rc == 1


# =====================================================================
# Safe Python configuration parsing: REQUEST_TIMEOUT_SECONDS /
# ALLOW_UNCONFIGURED_TELEGRAM must be parsed AFTER load_env_file() runs
# (not frozen as an unsafe import-time constant), reject malformed/
# non-finite/non-positive values without raising, and never leak a raw
# malformed value into output.
# =====================================================================


@pytest.mark.parametrize("raw,expected", [
    ("abc", 20.0),
    ("", 20.0),
    ("   ", 20.0),
    ("nan", 20.0),
    ("NaN", 20.0),
    ("inf", 20.0),
    ("Infinity", 20.0),
    ("-inf", 20.0),
    ("-Infinity", 20.0),
    ("0", 20.0),
    ("0.0", 20.0),
    ("-5", 20.0),
    ("-0.001", 20.0),
    ("  15  ", 15.0),
    ("15", 15.0),
    ("999999", 120.0),
    ("1e400", 20.0),  # parses as inf in Python float(), must be rejected
])
def test_python_parse_timeout_seconds_edge_cases(raw, expected):
    assert sta.parse_timeout_seconds(raw) == expected


def test_python_parse_timeout_seconds_none_uses_default():
    assert sta.parse_timeout_seconds(None) == 20.0


@pytest.mark.parametrize("raw,expected", [
    (None, False),
    ("", False),
    ("0", False),
    ("false", False),
    ("no", False),
    ("garbage", False),
    ("1", True),
    ("true", True),
    ("TRUE", True),
    ("yes", True),
    ("  yes  ", True),
])
def test_python_parse_allow_unconfigured_flag(raw, expected):
    assert sta.parse_allow_unconfigured_flag(raw) is expected


def test_python_malformed_timeout_env_var_falls_back_to_default_end_to_end(sta_sandbox, monkeypatch):
    """End-to-end via run_check(): a malformed JOBPULSE_TELEGRAM_TIMEOUT_SECONDS
    must not crash the script (the old `float(os.getenv(...))` at import
    time would have raised ValueError) -- it must fall back to the
    default and the check must still complete normally."""
    monkeypatch.setenv("JOBPULSE_TELEGRAM_TIMEOUT_SECONDS", "not-a-number")
    rc = run_sta(monkeypatch, CRITICAL_ALERT_PAYLOAD, send_ok)
    assert rc == 0
    assert sta.REQUEST_TIMEOUT_SECONDS == 20.0


@pytest.mark.parametrize("raw", ["nan", "Infinity", "-inf", "0", "-5"])
def test_python_non_finite_or_non_positive_timeout_env_var_falls_back(sta_sandbox, monkeypatch, raw):
    monkeypatch.setenv("JOBPULSE_TELEGRAM_TIMEOUT_SECONDS", raw)
    rc = run_sta(monkeypatch, CRITICAL_ALERT_PAYLOAD, send_ok)
    assert rc == 0
    assert sta.REQUEST_TIMEOUT_SECONDS == 20.0


def test_python_whitespace_padded_timeout_env_var_is_parsed(sta_sandbox, monkeypatch):
    monkeypatch.setenv("JOBPULSE_TELEGRAM_TIMEOUT_SECONDS", "  7.5  ")
    rc = run_sta(monkeypatch, CRITICAL_ALERT_PAYLOAD, send_ok)
    assert rc == 0
    assert sta.REQUEST_TIMEOUT_SECONDS == 7.5


def test_python_excessively_large_timeout_env_var_is_capped(sta_sandbox, monkeypatch):
    monkeypatch.setenv("JOBPULSE_TELEGRAM_TIMEOUT_SECONDS", "999999")
    rc = run_sta(monkeypatch, CRITICAL_ALERT_PAYLOAD, send_ok)
    assert rc == 0
    assert sta.REQUEST_TIMEOUT_SECONDS == sta.MAX_REQUEST_TIMEOUT_SECONDS


def test_python_timeout_env_var_is_reevaluated_after_load_env_file(sta_sandbox, monkeypatch, tmp_path):
    """A timeout value present ONLY in the .telegram_alert.env file (never
    in the process environment) must still take effect -- proving the
    parse happens after load_env_file(), not at import time before the
    file is even read."""
    env_file = tmp_path / "telegram_alert.env"
    env_file.write_text("JOBPULSE_TELEGRAM_TIMEOUT_SECONDS=33\n")
    monkeypatch.setattr(sta, "ENV_PATH", env_file)
    monkeypatch.delenv("JOBPULSE_TELEGRAM_TIMEOUT_SECONDS", raising=False)

    rc = run_sta(monkeypatch, CRITICAL_ALERT_PAYLOAD, send_ok)
    assert rc == 0
    assert sta.REQUEST_TIMEOUT_SECONDS == 33.0


def test_python_allow_unconfigured_flag_in_env_file_is_read_after_load(sta_sandbox, monkeypatch, tmp_path):
    """Same proof as above, for JOBPULSE_TELEGRAM_ALLOW_UNCONFIGURED: a
    value configured only in .telegram_alert.env must behave consistently
    with one set directly in the process environment."""
    env_file = tmp_path / "telegram_alert.env"
    env_file.write_text("JOBPULSE_TELEGRAM_ALLOW_UNCONFIGURED=1\n")
    monkeypatch.setattr(sta, "ENV_PATH", env_file)
    monkeypatch.delenv("JOBPULSE_TELEGRAM_ALLOW_UNCONFIGURED", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    rc = sta.main()
    assert rc == 0  # opt-out honored
    assert sta.ALLOW_UNCONFIGURED_TELEGRAM is True


def test_python_default_remains_fail_closed_without_any_opt_out_configured(sta_sandbox, monkeypatch, tmp_path):
    env_file = tmp_path / "telegram_alert.env"
    env_file.write_text("# no opt-out configured here\n")
    monkeypatch.setattr(sta, "ENV_PATH", env_file)
    monkeypatch.delenv("JOBPULSE_TELEGRAM_ALLOW_UNCONFIGURED", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    rc = sta.main()
    assert rc != 0
    assert sta.ALLOW_UNCONFIGURED_TELEGRAM is False


def test_python_no_raw_malformed_timeout_value_appears_in_output(sta_sandbox, monkeypatch, capsys):
    """The raw (malformed) env var value must never be echoed into any
    printed output, even indirectly."""
    suspicious_marker = "malformed_timeout_marker_XYZ_not_a_number"
    monkeypatch.setenv("JOBPULSE_TELEGRAM_TIMEOUT_SECONDS", suspicious_marker)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    sta.main()
    captured = capsys.readouterr()
    assert suspicious_marker not in captured.out
    assert suspicious_marker not in captured.err


# =====================================================================
# Missing Telegram configuration must fail closed (non-zero), not be
# silently reported as a successful check.
# =====================================================================


def _missing_config_env(monkeypatch, tmp_path):
    monkeypatch.setattr(sta, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(sta, "LOCK_PATH", tmp_path / "state.lock")
    monkeypatch.setattr(sta, "ENV_PATH", tmp_path / "nope.env")
    monkeypatch.delenv("JOBPULSE_TELEGRAM_ALLOW_UNCONFIGURED", raising=False)
    monkeypatch.setattr(sta, "ALLOW_UNCONFIGURED_TELEGRAM", False)


def test_python_missing_bot_token_fails_closed(monkeypatch, tmp_path):
    _missing_config_env(monkeypatch, tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat-id")

    def fail_if_called(token, chat_id, message):
        raise AssertionError("must not attempt to send with no bot token configured")

    monkeypatch.setattr(sta, "send_telegram", fail_if_called)

    rc = sta.main()
    assert rc != 0
    assert not (tmp_path / "state.json").exists()


def test_python_missing_chat_id_fails_closed(monkeypatch, tmp_path):
    _missing_config_env(monkeypatch, tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    def fail_if_called(token, chat_id, message):
        raise AssertionError("must not attempt to send with no chat id configured")

    monkeypatch.setattr(sta, "send_telegram", fail_if_called)

    rc = sta.main()
    assert rc != 0
    assert not (tmp_path / "state.json").exists()


def test_python_missing_both_credentials_fails_closed(monkeypatch, tmp_path):
    _missing_config_env(monkeypatch, tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    rc = sta.main()
    assert rc != 0
    assert not (tmp_path / "state.json").exists()


def test_python_missing_config_message_names_missing_vars_not_values(monkeypatch, tmp_path, capsys):
    _missing_config_env(monkeypatch, tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat-id")

    sta.main()
    captured = capsys.readouterr()
    assert "TELEGRAM_BOT_TOKEN" in captured.out
    assert "test-chat-id" not in captured.out  # the configured value is not a secret here, but never echo values


def test_python_explicit_opt_out_flag_allows_missing_config_to_exit_zero(monkeypatch, tmp_path):
    """Fail-closed is the default; an explicit opt-out flag is the only
    way to get a zero exit with no Telegram configuration, and it must be
    an intentional, documented choice -- not the silent prior default.

    Sets the env var (not the module attribute) deliberately: the opt-out
    flag is now re-evaluated from the environment inside run_check(),
    AFTER load_env_file() runs, so a value configured only in
    .telegram_alert.env takes effect -- pinning the module attribute
    directly would no longer prove that path."""
    monkeypatch.setattr(sta, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(sta, "LOCK_PATH", tmp_path / "state.lock")
    monkeypatch.setattr(sta, "ENV_PATH", tmp_path / "nope.env")
    monkeypatch.setenv("JOBPULSE_TELEGRAM_ALLOW_UNCONFIGURED", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    rc = sta.main()
    assert rc == 0
    assert not (tmp_path / "state.json").exists()


def test_wrapper_combined_exit_is_nonzero_when_real_telegram_script_has_no_config(tmp_path):
    """End-to-end with the REAL scripts/send_telegram_alerts.py (not a
    stub): a wrapper run where the infrastructure check passes but
    Telegram is unconfigured must report overall failure, since the
    collector/admin-status half of monitoring silently did nothing."""
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    _write_executable(stub_dir / "health.sh", STUB_HEALTH_SCRIPT)

    root_dir = tmp_path / "root"
    root_dir.mkdir()

    env = {k: v for k, v in os.environ.items() if not k.startswith("TELEGRAM_")}
    env["JOBPULSE_ALERT_ROOT"] = str(root_dir)
    env["JOBPULSE_HEALTH_ALERT_SCRIPT"] = str(stub_dir / "health.sh")
    env["JOBPULSE_TELEGRAM_ALERTS_SCRIPT"] = str(SCRIPTS / "send_telegram_alerts.py")
    env["STUB_HEALTH_EXIT"] = "0"
    env["JOBPULSE_TELEGRAM_STATE_PATH"] = str(tmp_path / "telegram_state.json")
    env["JOBPULSE_TELEGRAM_LOCK_PATH"] = str(tmp_path / "telegram_state.lock")
    env["JOBPULSE_TELEGRAM_ENV_PATH"] = str(tmp_path / "nonexistent.env")
    env.pop("JOBPULSE_TELEGRAM_ALLOW_UNCONFIGURED", None)

    result = subprocess.run(
        ["bash", str(SCRIPTS / "run_production_alert_checks.sh")],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30,
    )

    assert result.returncode != 0, (
        "missing Telegram config in the real sender must make the "
        "wrapper's combined exit status non-zero"
    )
    assert "fake health check ran" in result.stdout
    assert not (tmp_path / "telegram_state.json").exists()


# =====================================================================
# Wrapper tests (scripts/run_production_alert_checks.sh)
# =====================================================================


STUB_HEALTH_SCRIPT = """#!/usr/bin/env bash
echo "fake health check ran"
exit "${STUB_HEALTH_EXIT:-0}"
"""

STUB_TELEGRAM_SCRIPT = """import os, sys
print("fake telegram check ran")
sys.exit(int(os.environ.get("STUB_TELEGRAM_EXIT", "0")))
"""


@pytest.fixture
def wrapper_sandbox(tmp_path):
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    _write_executable(stub_dir / "health.sh", STUB_HEALTH_SCRIPT)
    (stub_dir / "telegram.py").write_text(STUB_TELEGRAM_SCRIPT)

    root_dir = tmp_path / "root"
    root_dir.mkdir()

    env = dict(os.environ)
    env["JOBPULSE_ALERT_ROOT"] = str(root_dir)
    env["JOBPULSE_HEALTH_ALERT_SCRIPT"] = str(stub_dir / "health.sh")
    env["JOBPULSE_TELEGRAM_ALERTS_SCRIPT"] = str(stub_dir / "telegram.py")
    env["STUB_HEALTH_EXIT"] = "0"
    env["STUB_TELEGRAM_EXIT"] = "0"

    return {"env": env, "root": root_dir}


def run_wrapper(sandbox, **env_overrides):
    env = dict(sandbox["env"])
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(SCRIPTS / "run_production_alert_checks.sh")],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30,
    )


def test_wrapper_runs_both_checks_when_first_fails(wrapper_sandbox):
    result = run_wrapper(wrapper_sandbox, STUB_HEALTH_EXIT="1", STUB_TELEGRAM_EXIT="0")

    assert "fake health check ran" in result.stdout
    assert "fake telegram check ran" in result.stdout, (
        "the second (collector/admin-status) check must run even when the "
        "first (infrastructure) check fails"
    )


@pytest.mark.parametrize("health_exit,telegram_exit,expected_rc", [
    ("0", "0", 0),
    ("1", "0", 1),
    ("0", "1", 1),
    ("1", "1", 1),
])
def test_wrapper_combined_exit_status(wrapper_sandbox, health_exit, telegram_exit, expected_rc):
    result = run_wrapper(wrapper_sandbox, STUB_HEALTH_EXIT=health_exit, STUB_TELEGRAM_EXIT=telegram_exit)
    assert result.returncode == expected_rc


def test_wrapper_top_level_lock_prevents_overlapping_runs(wrapper_sandbox):
    state_dir = wrapper_sandbox["root"] / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "run_production_alert_checks.lock"

    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = run_wrapper(wrapper_sandbox)
        assert result.returncode == 0
        assert "run_production_alert_checks_already_running" in result.stdout
        assert "fake health check ran" not in result.stdout
        assert "fake telegram check ran" not in result.stdout
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def test_wrapper_lock_busy_exit_zero_is_intentional_not_unknown_status(wrapper_sandbox):
    """Decision record: exit 0 on wrapper-lock-busy means "another wrapper
    invocation is already running both checks," not "status unknown."
    Deliberate (see the comment at the flock check in
    run_production_alert_checks.sh and docs/PRODUCTION_RUNBOOK.md's
    "Lock-busy exit semantics" section) because both sub-scripts bound
    their own external calls with explicit timeouts, so a full wrapper
    run normally completes well within a typical cron interval."""
    state_dir = wrapper_sandbox["root"] / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "run_production_alert_checks.lock"

    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = run_wrapper(wrapper_sandbox)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    assert result.returncode == 0, (
        "wrapper lock-busy must exit 0 -- a concurrent duplicate run is not a check failure"
    )


# =====================================================================
# Bounded execution: each wrapper child gets its own independent
# deadline (formerly unbounded -- reproduced and fixed in this pass:
# a hung health-check child previously blocked the wrapper forever and
# the telegram/admin-status child never ran even once).
# =====================================================================


HANGING_HEALTH_SCRIPT = """#!/usr/bin/env bash
echo "$$" > "$HANG_MARKER"
sleep 999999
"""

HANGING_TELEGRAM_SCRIPT = """import os, sys
with open(os.environ["HANG_MARKER"], "w") as f:
    f.write(str(os.getpid()))
sys.stdout.flush()
import time
time.sleep(999999)
"""


@pytest.fixture
def hangable_wrapper_sandbox(tmp_path):
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    _write_executable(stub_dir / "health.sh", STUB_HEALTH_SCRIPT)
    (stub_dir / "telegram.py").write_text(STUB_TELEGRAM_SCRIPT)
    _write_executable(stub_dir / "hanging_health.sh", HANGING_HEALTH_SCRIPT)
    (stub_dir / "hanging_telegram.py").write_text(HANGING_TELEGRAM_SCRIPT)

    root_dir = tmp_path / "root"
    root_dir.mkdir()

    env = dict(os.environ)
    env["JOBPULSE_ALERT_ROOT"] = str(root_dir)
    env["JOBPULSE_HEALTH_ALERT_SCRIPT"] = str(stub_dir / "health.sh")
    env["JOBPULSE_TELEGRAM_ALERTS_SCRIPT"] = str(stub_dir / "telegram.py")
    env["STUB_HEALTH_EXIT"] = "0"
    env["STUB_TELEGRAM_EXIT"] = "0"
    env["JOBPULSE_ALERT_COMPONENT_TIMEOUT_SECONDS"] = "2"
    env["JOBPULSE_ALERT_COMPONENT_KILL_AFTER_SECONDS"] = "1"
    env["HANG_MARKER"] = str(tmp_path / "hang_pid")

    return {"env": env, "root": root_dir, "stub_dir": stub_dir, "hang_marker": tmp_path / "hang_pid"}


def test_wrapper_hanging_first_component_times_out_and_second_still_runs(hangable_wrapper_sandbox):
    sandbox = hangable_wrapper_sandbox
    result = run_wrapper(
        sandbox,
        JOBPULSE_HEALTH_ALERT_SCRIPT=str(sandbox["stub_dir"] / "hanging_health.sh"),
    )

    hang_pid = _wait_for_hang_pid(sandbox)
    assert "category=timeout" in result.stdout
    assert "fake telegram check ran" in result.stdout, (
        "the second component must still run even though the first hung"
    )
    assert not _pid_alive(hang_pid), "the hung first component must be killed -- no orphan"
    assert result.returncode != 0


def test_wrapper_hanging_second_component_times_out(hangable_wrapper_sandbox):
    sandbox = hangable_wrapper_sandbox
    result = run_wrapper(
        sandbox,
        JOBPULSE_TELEGRAM_ALERTS_SCRIPT=str(sandbox["stub_dir"] / "hanging_telegram.py"),
    )

    hang_pid = _wait_for_hang_pid(sandbox)
    assert "fake health check ran" in result.stdout
    assert "telegram_alert_check_exit_code=124 category=timeout" in result.stdout
    assert not _pid_alive(hang_pid), "the hung second component must be killed -- no orphan"
    assert result.returncode != 0


def test_wrapper_combined_exit_is_nonzero_when_a_component_times_out(hangable_wrapper_sandbox):
    sandbox = hangable_wrapper_sandbox
    result = run_wrapper(
        sandbox,
        JOBPULSE_HEALTH_ALERT_SCRIPT=str(sandbox["stub_dir"] / "hanging_health.sh"),
    )
    _wait_for_hang_pid(sandbox)
    assert result.returncode != 0


def test_wrapper_lock_released_after_component_timeout_and_subsequent_run_works(hangable_wrapper_sandbox):
    sandbox = hangable_wrapper_sandbox
    first = run_wrapper(
        sandbox,
        JOBPULSE_HEALTH_ALERT_SCRIPT=str(sandbox["stub_dir"] / "hanging_health.sh"),
    )
    _wait_for_hang_pid(sandbox)
    assert "category=timeout" in first.stdout

    (sandbox["hang_marker"]).unlink(missing_ok=True)
    second = run_wrapper(sandbox)  # back to the normal, fast stubs

    assert "run_production_alert_checks_already_running" not in second.stdout, (
        "the wrapper lock must be released after a component timeout"
    )
    assert "fake health check ran" in second.stdout
    assert "fake telegram check ran" in second.stdout
    assert second.returncode == 0


def test_wrapper_missing_timeout_utility_fails_clearly(tmp_path):
    """If GNU `timeout` is not on PATH, both components must fail
    immediately and clearly -- never run unbounded."""
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    _write_executable(stub_dir / "health.sh", STUB_HEALTH_SCRIPT)
    (stub_dir / "telegram.py").write_text(STUB_TELEGRAM_SCRIPT)

    restricted_bin = tmp_path / "restricted_bin"
    restricted_bin.mkdir()
    for name in ("bash", "date", "tee", "mkdir", "python3", "flock", "sh"):
        real = shutil.which(name)
        if real:
            (restricted_bin / name).symlink_to(real)
    # Deliberately no `timeout` symlink.

    root_dir = tmp_path / "root"
    root_dir.mkdir()

    env = {
        "PATH": str(restricted_bin),
        "JOBPULSE_ALERT_ROOT": str(root_dir),
        "JOBPULSE_HEALTH_ALERT_SCRIPT": str(stub_dir / "health.sh"),
        "JOBPULSE_TELEGRAM_ALERTS_SCRIPT": str(stub_dir / "telegram.py"),
        "STUB_HEALTH_EXIT": "0",
        "STUB_TELEGRAM_EXIT": "0",
        "HOME": os.environ.get("HOME", "/tmp"),
    }

    result = subprocess.run(
        ["bash", str(SCRIPTS / "run_production_alert_checks.sh")],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30,
    )

    assert "category=timeout_utility_missing" in result.stdout
    assert result.returncode != 0
    assert "fake health check ran" not in result.stdout, "must not run the component unbounded"
    assert "fake telegram check ran" not in result.stdout, "must not run the component unbounded"


def test_wrapper_never_touches_real_docker_or_network(wrapper_sandbox):
    """The wrapper itself only invokes the two configured sub-script paths
    -- it has no direct docker/curl/network dependency of its own. This
    test's stubs contain no docker/curl/network calls at all, so a clean
    run proves the wrapper doesn't add any such dependency."""
    result = run_wrapper(wrapper_sandbox)
    assert result.returncode == 0
    assert "docker" not in result.stdout.lower()
