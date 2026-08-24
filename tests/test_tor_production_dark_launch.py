"""Phase 3 production dark-launch tests.

Three groups, all network-free and Tor-daemon-free (a real Tor container
is exercised separately -- see tests/test_tor_real_integration.py, kept
out of this file so the ordinary fast suite never depends on it):

  1. File-based secret model (app.config.get_tor_control_password_file()/
     get_tor_control_password()).
  2. scripts/tor/production_dark_launch_check.py, fully mocked (no real
     Tor/PostgreSQL/network).
  3. Compose configuration validation, via a real `docker compose config`
     call against temporary copies of the two compose files (this only
     asks Compose to resolve/merge YAML -- it starts no containers, and
     is skipped automatically if the `docker` CLI isn't available).
  4. A static, grep-based regression test proving the closed set of
     files that ever call get_proxy_config()/set TOR_ENABLED, so a
     future change silently widening that set fails CI.

No LinkedIn traffic, no real Tor daemon, no automatic NEWNYM anywhere in
this file.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.config import get_tor_control_password, get_tor_control_password_file
import scripts.tor.production_dark_launch_check as dlc


# ---------------------------------------------------------------------
# 1. File-based secret model
# ---------------------------------------------------------------------

def test_control_password_file_path_getter(monkeypatch):
    monkeypatch.setenv("TOR_CONTROL_PASSWORD_FILE", "/run/secrets/tor_control_password")
    assert get_tor_control_password_file() == "/run/secrets/tor_control_password"


def test_control_password_file_path_getter_defaults_empty(monkeypatch):
    monkeypatch.delenv("TOR_CONTROL_PASSWORD_FILE", raising=False)
    assert get_tor_control_password_file() == ""


def test_control_password_prefers_file_over_env_var(monkeypatch, tmp_path):
    secret_file = tmp_path / "tor_control_password"
    secret_file.write_text("from-the-file\n")

    monkeypatch.setenv("TOR_CONTROL_PASSWORD_FILE", str(secret_file))
    monkeypatch.setenv("TOR_CONTROL_PASSWORD", "from-the-env-var")

    assert get_tor_control_password() == "from-the-file"


def test_control_password_strips_trailing_whitespace_from_file(monkeypatch, tmp_path):
    secret_file = tmp_path / "tor_control_password"
    secret_file.write_text("secret-value\n\n")

    monkeypatch.setenv("TOR_CONTROL_PASSWORD_FILE", str(secret_file))
    monkeypatch.delenv("TOR_CONTROL_PASSWORD", raising=False)

    assert get_tor_control_password() == "secret-value"


def test_control_password_falls_back_to_env_var_when_no_file_configured(monkeypatch):
    monkeypatch.delenv("TOR_CONTROL_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("TOR_CONTROL_PASSWORD", "dev-only-value")

    assert get_tor_control_password() == "dev-only-value"


def test_control_password_raises_clearly_when_file_missing(monkeypatch, tmp_path):
    missing_file = tmp_path / "does_not_exist"
    monkeypatch.setenv("TOR_CONTROL_PASSWORD_FILE", str(missing_file))

    with pytest.raises(RuntimeError, match="could not be read"):
        get_tor_control_password()


def test_control_password_file_error_never_includes_password_value(monkeypatch, tmp_path):
    """Regression guard: the error message names the FILE PATH, never a
    secret value (there is no secret value to leak here since the file
    is missing, but this pins the error message shape so a future edit
    can't accidentally start interpolating file *contents* into it)."""
    missing_file = tmp_path / "does_not_exist"
    monkeypatch.setenv("TOR_CONTROL_PASSWORD_FILE", str(missing_file))

    with pytest.raises(RuntimeError) as excinfo:
        get_tor_control_password()

    assert str(missing_file) in str(excinfo.value)


# ---------------------------------------------------------------------
# 2. scripts/tor/production_dark_launch_check.py (fully mocked)
# ---------------------------------------------------------------------

def test_diagnostic_reports_not_ok_when_tor_disabled_for_itself(monkeypatch):
    monkeypatch.setenv("TOR_ENABLED", "false")

    result = dlc.run_diagnostic()

    assert result["ok"] is False
    assert result["diagnostic_tor_enabled"] is False
    assert result["error_category"] == "tor_not_enabled_for_diagnostic"
    assert dlc._exit_code_for(result) == dlc.EXIT_TOR_NOT_ENABLED_FOR_DIAGNOSTIC


def test_diagnostic_result_never_claims_to_prove_live_api_state(monkeypatch):
    """The result's own `note` field must explicitly disclaim proving
    anything about the live API -- this is the exact architecture
    correction the diagnostic exists to encode, not just document."""
    monkeypatch.setenv("TOR_ENABLED", "false")
    result = dlc.run_diagnostic()

    assert "does NOT prove the live production API uses Tor" in result["note"]


def test_diagnostic_success_path_reports_ok_and_bootstrap_phase(monkeypatch):
    monkeypatch.setenv("TOR_ENABLED", "true")

    with mock.patch.object(dlc, "check_bootstrap_status", return_value="PROGRESS=100"), \
         mock.patch.object(dlc, "check_exit_ip", return_value="203.0.113.5"):
        result = dlc.run_diagnostic()

    assert result["ok"] is True
    assert result["checks"]["control_port_auth_and_bootstrap_100"] is True
    assert result["checks"]["socks_and_neutral_endpoint_verified"] is True
    assert result["bootstrap_phase"] == "PROGRESS=100"
    assert dlc._exit_code_for(result) == dlc.EXIT_OK


def test_diagnostic_bootstrap_failure_reads_authoritative_category(monkeypatch):
    monkeypatch.setenv("TOR_ENABLED", "true")

    with mock.patch.object(dlc, "check_bootstrap_status", side_effect=RuntimeError("not bootstrapped")), \
         mock.patch.object(dlc, "_read_authoritative_error_category", return_value="bootstrap_incomplete"):
        result = dlc.run_diagnostic()

    assert result["ok"] is False
    assert result["error_category"] == "bootstrap_incomplete"
    assert dlc._exit_code_for(result) == dlc.EXIT_BOOTSTRAP_FAILED


def test_diagnostic_control_port_failure_categorized_distinctly(monkeypatch):
    monkeypatch.setenv("TOR_ENABLED", "true")

    with mock.patch.object(dlc, "check_bootstrap_status", side_effect=ConnectionRefusedError("refused")), \
         mock.patch.object(dlc, "_read_authoritative_error_category", return_value="control_port_failure"):
        result = dlc.run_diagnostic()

    assert result["error_category"] == "control_port_failure"
    assert dlc._exit_code_for(result) == dlc.EXIT_CONTROL_PORT_FAILED


def test_diagnostic_falls_back_when_authoritative_category_unreadable(monkeypatch):
    """If the DB becomes unavailable between the bootstrap failure and
    reading back its category, the diagnostic must still return a
    well-formed, non-crashing result with a sensible fallback category."""
    monkeypatch.setenv("TOR_ENABLED", "true")

    with mock.patch.object(dlc, "check_bootstrap_status", side_effect=RuntimeError("boom")), \
         mock.patch.object(dlc, "_read_authoritative_error_category", side_effect=Exception("db down")):
        result = dlc.run_diagnostic()

    assert result["ok"] is False
    assert result["error_category"] == "control_port_or_bootstrap_failure"
    assert dlc._exit_code_for(result) == dlc.EXIT_BOOTSTRAP_FAILED


def test_diagnostic_socks_failure_categorized(monkeypatch):
    monkeypatch.setenv("TOR_ENABLED", "true")

    with mock.patch.object(dlc, "check_bootstrap_status", return_value="PROGRESS=100"), \
         mock.patch.object(dlc, "check_exit_ip", side_effect=RuntimeError("IsTor=false")):
        result = dlc.run_diagnostic()

    assert result["ok"] is False
    assert result["error_category"] == "socks_or_verification_failed"
    assert dlc._exit_code_for(result) == dlc.EXIT_SOCKS_OR_VERIFICATION_FAILED


def test_diagnostic_never_calls_linkedin_or_collector_code():
    """Static guard: the diagnostic module must never IMPORT a LinkedIn
    or collector entry point -- it only ever imports the generic Tor
    connectivity/observability modules. (Prose mentions of "LinkedIn" in
    the module's own docstring, documenting that it does NOT touch
    LinkedIn, are expected and fine -- this checks actual `import`/`from`
    statements via the AST, not the word appearing anywhere in the file.)
    """
    import ast

    source = Path(dlc.__file__).read_text()
    tree = ast.parse(source)

    imported_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.append(node.module)

    for name in imported_names:
        assert "linkedin" not in name.lower(), f"unexpected import: {name}"
        assert "collector" not in name.lower(), f"unexpected import: {name}"


def test_diagnostic_result_is_json_serializable(monkeypatch):
    monkeypatch.setenv("TOR_ENABLED", "true")

    with mock.patch.object(dlc, "check_bootstrap_status", return_value="PROGRESS=100"), \
         mock.patch.object(dlc, "check_exit_ip", return_value="203.0.113.5"):
        result = dlc.run_diagnostic()

    # Must not raise -- proves the result is a plain, secret-free,
    # structured dict of JSON-safe values.
    serialized = json.dumps(result)
    assert "ControlPassword" not in serialized


def test_diagnostic_redacts_configured_password_from_error_message(monkeypatch):
    """Real regression test: deliberately raises an exception whose text
    embeds the EXACT configured Tor control password (library exceptions
    -- stem, psycopg2 -- can legitimately include connection parameters
    in their string representation), and requires the password to be
    fully absent from `error_message` specifically -- not merely from
    some other field, and not via an assertion that silently short-
    circuits. If this test is ever weakened back to something like
    `... or True`, it must fail loudly, not pass vacuously."""
    monkeypatch.setenv("TOR_ENABLED", "true")
    monkeypatch.setenv("TOR_CONTROL_PASSWORD", "super-secret-value")
    monkeypatch.delenv("TOR_CONTROL_PASSWORD_FILE", raising=False)

    poisoned_error = RuntimeError("authentication failed password=super-secret-value")

    with mock.patch.object(dlc, "check_bootstrap_status", side_effect=poisoned_error), \
         mock.patch.object(dlc, "_read_authoritative_error_category", return_value="control_port_failure"):
        result = dlc.run_diagnostic()

    assert result["ok"] is False
    assert result["error_message"] is not None
    assert "super-secret-value" not in result["error_message"]
    assert "super-secret-value" not in json.dumps(result)
    assert "***REDACTED***" in result["error_message"]


def test_diagnostic_redacts_both_tor_and_postgres_passwords_from_error_message(monkeypatch):
    """Real regression test for the complete redaction requirement: a
    single poisoned exception embeds BOTH the exact configured Tor
    ControlPort password AND the exact configured PostgreSQL password
    (e.g. a connection-string exception that legitimately reports both
    endpoints it tried). Both must be fully absent from result JSON,
    with REDACTED markers standing in for each -- not merely one of the
    two, and not via an assertion that could silently short-circuit
    (`or True`) or that excludes error_message from what's checked."""
    monkeypatch.setenv("TOR_ENABLED", "true")
    monkeypatch.setenv("TOR_CONTROL_PASSWORD", "super-secret-tor-value")
    monkeypatch.delenv("TOR_CONTROL_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("POSTGRES_PASSWORD", "super-secret-db-value")

    poisoned_error = RuntimeError(
        "authentication failed: tor control password=super-secret-tor-value "
        "postgresql://jobpulse_user:super-secret-db-value@db:5432/jobpulse unreachable"
    )

    with mock.patch.object(dlc, "check_bootstrap_status", side_effect=poisoned_error), \
         mock.patch.object(dlc, "_read_authoritative_error_category", return_value="control_port_failure"):
        result = dlc.run_diagnostic()

    assert result["ok"] is False
    assert result["error_message"] is not None

    serialized = json.dumps(result)
    for secret in ("super-secret-tor-value", "super-secret-db-value"):
        assert secret not in result["error_message"], f"{secret!r} leaked into error_message"
        assert secret not in serialized, f"{secret!r} leaked into the JSON-serialized result"

    assert result["error_message"].count("***REDACTED***") >= 2


def test_diagnostic_redacts_both_passwords_from_main_stdout_and_stderr(monkeypatch, capsys):
    """Same dual-secret poisoned exception as above, exercised through
    the real `main()` entry point -- the actual stdout/stderr surface an
    operator or CI job would see. Only the initial PostgreSQL
    reachability precheck is mocked out (network-free); the redaction
    itself runs for real against the real, currently-configured
    passwords."""
    monkeypatch.setenv("TOR_ENABLED", "true")
    monkeypatch.setenv("TOR_CONTROL_PASSWORD", "super-secret-tor-value")
    monkeypatch.delenv("TOR_CONTROL_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("POSTGRES_PASSWORD", "super-secret-db-value")
    monkeypatch.setattr(sys, "argv", ["production_dark_launch_check"])

    poisoned_error = RuntimeError(
        "authentication failed: tor control password=super-secret-tor-value "
        "postgresql://jobpulse_user:super-secret-db-value@db:5432/jobpulse unreachable"
    )

    with mock.patch.object(dlc, "psycopg2") as fake_psycopg2, \
         mock.patch.object(dlc, "check_bootstrap_status", side_effect=poisoned_error), \
         mock.patch.object(dlc, "_read_authoritative_error_category", return_value="control_port_failure"):
        fake_psycopg2.connect.return_value = mock.MagicMock()

        with pytest.raises(SystemExit) as excinfo:
            dlc.main()

    assert excinfo.value.code == dlc.EXIT_CONTROL_PORT_FAILED

    captured = capsys.readouterr()
    for secret in ("super-secret-tor-value", "super-secret-db-value"):
        assert secret not in captured.out, f"{secret!r} leaked into stdout"
        assert secret not in captured.err, f"{secret!r} leaked into stderr"
    assert captured.out.count("***REDACTED***") >= 2


def test_diagnostic_redacts_postgres_password_from_database_unavailable_path(monkeypatch, capsys):
    """The database_unavailable path (main()'s own psycopg2.connect
    precheck) is a separate redaction call site from run_diagnostic()'s
    internal ones -- a real psycopg2 OperationalError legitimately
    includes the DSN (and therefore the password) it tried to connect
    with. Proves that path is redacted too, not merely the bootstrap/
    SOCKS ones already covered above."""
    monkeypatch.setenv("TOR_ENABLED", "true")
    monkeypatch.setenv("POSTGRES_PASSWORD", "super-secret-db-value")
    monkeypatch.delenv("TOR_CONTROL_PASSWORD_FILE", raising=False)
    monkeypatch.setattr(sys, "argv", ["production_dark_launch_check"])

    dsn_error = RuntimeError(
        "could not connect to server: FATAL: password authentication failed "
        "for connection postgresql://jobpulse_user:super-secret-db-value@db:5432/jobpulse"
    )

    with mock.patch.object(dlc, "psycopg2") as fake_psycopg2:
        fake_psycopg2.connect.side_effect = dsn_error

        with pytest.raises(SystemExit) as excinfo:
            dlc.main()

    assert excinfo.value.code == dlc.EXIT_DATABASE_UNAVAILABLE

    captured = capsys.readouterr()
    assert "super-secret-db-value" not in captured.out
    assert "super-secret-db-value" not in captured.err
    assert "***REDACTED***" in captured.out


def test_redacted_error_message_is_length_bounded(monkeypatch):
    """Bounds the final sanitized error length -- an unbounded/adversarial
    exception message must not be stored/printed verbatim forever."""
    monkeypatch.setenv("TOR_ENABLED", "true")
    monkeypatch.delenv("TOR_CONTROL_PASSWORD_FILE", raising=False)
    monkeypatch.delenv("TOR_CONTROL_PASSWORD", raising=False)

    huge_message = "x" * 100_000
    with mock.patch.object(dlc, "check_bootstrap_status", side_effect=RuntimeError(huge_message)), \
         mock.patch.object(dlc, "_read_authoritative_error_category", return_value="control_port_failure"):
        result = dlc.run_diagnostic()

    assert result["error_message"] is not None
    assert len(result["error_message"]) < 5000


def test_diagnostic_redacts_password_from_main_stdout_and_stderr(monkeypatch, capsys):
    """Same poisoned exception, exercised through the real `main()` entry
    point -- the actual stdout/stderr surface an operator or CI job would
    see -- with only the initial PostgreSQL reachability precheck mocked
    out so this stays network-free. Proves redaction holds at the actual
    process-output boundary, not just inside the in-memory result dict."""
    monkeypatch.setenv("TOR_ENABLED", "true")
    monkeypatch.setenv("TOR_CONTROL_PASSWORD", "super-secret-value")
    monkeypatch.delenv("TOR_CONTROL_PASSWORD_FILE", raising=False)
    monkeypatch.setattr(sys, "argv", ["production_dark_launch_check"])

    poisoned_error = RuntimeError("authentication failed password=super-secret-value")

    with mock.patch.object(dlc, "psycopg2") as fake_psycopg2, \
         mock.patch.object(dlc, "check_bootstrap_status", side_effect=poisoned_error), \
         mock.patch.object(dlc, "_read_authoritative_error_category", return_value="control_port_failure"):
        fake_psycopg2.connect.return_value = mock.MagicMock()

        with pytest.raises(SystemExit) as excinfo:
            dlc.main()

    assert excinfo.value.code == dlc.EXIT_CONTROL_PORT_FAILED

    captured = capsys.readouterr()
    assert "super-secret-value" not in captured.out
    assert "super-secret-value" not in captured.err
    assert "***REDACTED***" in captured.out


# ---------------------------------------------------------------------
# 3. Compose configuration validation (real `docker compose config`,
#    no containers started)
# ---------------------------------------------------------------------

_DOCKER_AVAILABLE = shutil.which("docker") is not None


def _docker_compose_works() -> bool:
    if not _DOCKER_AVAILABLE:
        return False
    try:
        subprocess.run(
            ["docker", "compose", "version"],
            check=True, capture_output=True, timeout=10,
        )
        return True
    except Exception:
        return False


requires_docker_compose = pytest.mark.skipif(
    not _docker_compose_works(),
    reason="docker compose CLI not available in this environment",
)


@pytest.fixture
def compose_validation_dir(tmp_path):
    """Copies the two production compose files into an isolated tmp dir
    and rewrites their /opt/jobpulse/* absolute paths to point at stub
    files created inside that same tmp dir -- never touches the real
    /opt/jobpulse on this machine. Returns the tmp dir path."""
    opt_stub = tmp_path / "opt_stub"
    opt_stub.mkdir()
    for name in (".admin.env", ".telegram_alert.env", ".admin_token", ".admin_htpasswd", ".tor_control_password"):
        (opt_stub / name).write_text("")
    (opt_stub / "backups").mkdir()

    (tmp_path / ".api_keys.env").write_text("")
    (tmp_path / ".env").write_text("")
    (tmp_path / ".auth").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "nginx.conf").write_text("")

    for filename in ("docker-compose.prod.yml", "docker-compose.prod.tor.yml"):
        original = (REPO_ROOT / filename).read_text()
        rewritten = original.replace("/opt/jobpulse", str(opt_stub))
        (tmp_path / filename).write_text(rewritten)

    return tmp_path


def _run_compose_config(compose_dir, *extra_files, env=None):
    args = ["docker", "compose", "-f", "docker-compose.prod.yml"]
    for f in extra_files:
        args += ["-f", f]
    args += ["config", "--format", "json"]

    result = subprocess.run(
        args, cwd=str(compose_dir), capture_output=True, text=True,
        timeout=30, env=env,
    )
    assert result.returncode == 0, f"docker compose config failed: {result.stderr}"
    return json.loads(result.stdout)


@requires_docker_compose
def test_base_prod_compose_has_no_tor_service(compose_validation_dir):
    config = _run_compose_config(compose_validation_dir)
    assert "tor" not in config["services"]
    assert "tor-diagnostic" not in config["services"]


@requires_docker_compose
def test_base_prod_compose_api_tor_enabled_false(compose_validation_dir):
    config = _run_compose_config(compose_validation_dir)
    assert config["services"]["api"]["environment"]["TOR_ENABLED"] == "false"


@requires_docker_compose
def test_base_prod_compose_api_has_no_tor_env_vars(compose_validation_dir):
    config = _run_compose_config(compose_validation_dir)
    api_env = config["services"]["api"]["environment"]
    for forbidden_key in (
        "TOR_SOCKS_HOST", "TOR_CONTROL_HOST",
        "TOR_CONTROL_PASSWORD", "TOR_CONTROL_PASSWORD_FILE",
    ):
        assert forbidden_key not in api_env


@requires_docker_compose
def test_merged_overlay_adds_only_tor_and_diagnostic_services(compose_validation_dir, monkeypatch):
    monkeypatch.setenv("JOBPULSE_TOR_IMAGE", "ghcr.io/mrezamaghouli/jobpulse-tor:test-sha")
    import os
    config = _run_compose_config(
        compose_validation_dir, "docker-compose.prod.tor.yml", env=os.environ.copy(),
    )
    assert set(config["services"].keys()) == {"api", "db", "frontend", "tor"}


@requires_docker_compose
def test_merged_overlay_api_still_tor_enabled_false_and_no_depends_on_tor(compose_validation_dir, monkeypatch):
    monkeypatch.setenv("JOBPULSE_TOR_IMAGE", "ghcr.io/mrezamaghouli/jobpulse-tor:test-sha")
    import os
    config = _run_compose_config(
        compose_validation_dir, "docker-compose.prod.tor.yml", env=os.environ.copy(),
    )
    api = config["services"]["api"]
    assert api["environment"]["TOR_ENABLED"] == "false"
    assert "tor" not in api.get("depends_on", {})


@requires_docker_compose
def test_tor_service_has_no_published_ports(compose_validation_dir, monkeypatch):
    monkeypatch.setenv("JOBPULSE_TOR_IMAGE", "ghcr.io/mrezamaghouli/jobpulse-tor:test-sha")
    import os
    config = _run_compose_config(
        compose_validation_dir, "docker-compose.prod.tor.yml", env=os.environ.copy(),
    )
    tor = config["services"]["tor"]
    assert "ports" not in tor
    assert set(tor["expose"]) == {"9050", "9051"}


@requires_docker_compose
def test_tor_service_no_privileged_no_host_network_no_docker_socket(compose_validation_dir, monkeypatch):
    monkeypatch.setenv("JOBPULSE_TOR_IMAGE", "ghcr.io/mrezamaghouli/jobpulse-tor:test-sha")
    import os
    config = _run_compose_config(
        compose_validation_dir, "docker-compose.prod.tor.yml", env=os.environ.copy(),
    )
    tor = config["services"]["tor"]
    assert tor.get("privileged", False) is False
    assert tor.get("network_mode") != "host"
    for volume in tor.get("volumes", []):
        assert volume.get("source") != "/var/run/docker.sock"


@requires_docker_compose
def test_tor_service_has_bounded_healthcheck(compose_validation_dir, monkeypatch):
    monkeypatch.setenv("JOBPULSE_TOR_IMAGE", "ghcr.io/mrezamaghouli/jobpulse-tor:test-sha")
    import os
    config = _run_compose_config(
        compose_validation_dir, "docker-compose.prod.tor.yml", env=os.environ.copy(),
    )
    healthcheck = config["services"]["tor"]["healthcheck"]
    assert "Bootstrapped 100%" in " ".join(healthcheck["test"])
    assert healthcheck["retries"] > 0
    assert healthcheck["interval"]
    assert healthcheck["timeout"]


@requires_docker_compose
def test_tor_service_requires_image_var_fail_closed(compose_validation_dir):
    import os
    env = os.environ.copy()
    env.pop("JOBPULSE_TOR_IMAGE", None)
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.prod.yml", "-f", "docker-compose.prod.tor.yml", "config"],
        cwd=str(compose_validation_dir), capture_output=True, text=True, timeout=30, env=env,
    )
    assert result.returncode != 0
    assert "JOBPULSE_TOR_IMAGE" in result.stderr


@requires_docker_compose
def test_tor_service_has_no_build_directive(compose_validation_dir, monkeypatch):
    monkeypatch.setenv("JOBPULSE_TOR_IMAGE", "ghcr.io/mrezamaghouli/jobpulse-tor:test-sha")
    import os
    config = _run_compose_config(
        compose_validation_dir, "docker-compose.prod.tor.yml", env=os.environ.copy(),
    )
    # Resolved-config-level check (not string search over the YAML source,
    # which would also match this test's own explanatory comments about
    # NOT having a build: directive) -- production must consume a
    # prebuilt image, never build in place.
    assert "build" not in config["services"]["tor"]


@requires_docker_compose
def test_tor_diagnostic_service_only_appears_under_tor_ops_profile(compose_validation_dir, monkeypatch):
    monkeypatch.setenv("JOBPULSE_TOR_IMAGE", "ghcr.io/mrezamaghouli/jobpulse-tor:test-sha")
    import os
    env = os.environ.copy()

    default_config = _run_compose_config(compose_validation_dir, "docker-compose.prod.tor.yml", env=env)
    assert "tor-diagnostic" not in default_config["services"]

    result = subprocess.run(
        ["docker", "compose", "--profile", "tor-ops",
         "-f", "docker-compose.prod.yml", "-f", "docker-compose.prod.tor.yml",
         "config", "--format", "json"],
        cwd=str(compose_validation_dir), capture_output=True, text=True, timeout=30, env=env,
    )
    assert result.returncode == 0, result.stderr
    profiled_config = json.loads(result.stdout)
    assert "tor-diagnostic" in profiled_config["services"]
    assert profiled_config["services"]["tor-diagnostic"]["environment"]["TOR_ENABLED"] == "true"


# ---------------------------------------------------------------------
# 4. Static regression guard: closed set of files touching TOR_ENABLED /
#    get_proxy_config -- proves production collection paths stay direct
#    without needing to re-derive the full trace by hand every time.
# ---------------------------------------------------------------------

_ALLOWED_GET_PROXY_CONFIG_CALLERS = {
    "scripts/providers/linkedin_browser_provider.py",
    "scripts/linkedin_auth_preflight.py",
    "scripts/tor/verify_tor_connectivity.py",
    "scripts/tor/tor_client.py",  # defines it
    "tests/test_tor_client_config.py",
    "tests/test_tor_production_dark_launch.py",  # this file's own text search
}

_ALLOWED_TOR_ENABLED_SETTERS = {
    "docker-compose.prod.yml",
    "docker-compose.prod.tor.yml",
    "docker-compose.tor.yml",
    "app/config.py",
    ".env.example",
    "scripts/tor/verify_tor_connectivity.py",  # error message text only, never a collection script
    ".github/workflows/ci.yml",  # compose-config validation assertions only
    "scripts/tor/production_dark_launch.sh",  # dark-launch invariant checks only, invoked only by the workflow_dispatch-only tor-dark-launch.yml, never dispatched by this task
    ".github/workflows/tor-secret-provision.yml",  # Phase 3.1: SSH-invokes the helper below, never sets/dispatches Tor itself
    ".github/scripts/tor/provision_production_secret.sh",  # Phase 3.1: read-only pre/post invariant check (must equal 'false'), invoked only by the workflow_dispatch-only tor-secret-provision.yml, never dispatched by this task
}


def _iter_repo_files(*suffixes):
    for path in REPO_ROOT.rglob("*"):
        if ".git" in path.parts:
            continue
        if path.suffix in suffixes and path.is_file():
            yield path


def test_get_proxy_config_call_sites_are_a_closed_allowlist():
    """If a new file starts calling get_proxy_config(), it must be
    reviewed and explicitly added here -- this is what keeps 'production
    collection paths remain direct' a continuously-enforced invariant
    rather than a one-time audit finding that silently rots."""
    offenders = []

    for path in _iter_repo_files(".py"):
        text = path.read_text(errors="ignore")
        if "get_proxy_config(" in text or "get_proxy_config," in text or "import get_proxy_config" in text:
            rel = str(path.relative_to(REPO_ROOT))
            if rel not in _ALLOWED_GET_PROXY_CONFIG_CALLERS:
                offenders.append(rel)

    assert not offenders, (
        f"Unexpected new caller(s) of get_proxy_config(): {offenders} -- "
        "review whether this new call site could route production "
        "collection traffic through Tor, then add it to "
        "_ALLOWED_GET_PROXY_CONFIG_CALLERS if intentional."
    )


def test_tor_enabled_setters_are_a_closed_allowlist():
    """Same discipline for anything that sets/reads the literal string
    TOR_ENABLED -- proves no collection script (run_collection_cycle*,
    seed/process/reconcile scripts, etc.) has started touching it."""
    offenders = []
    pattern = re.compile(r"TOR_ENABLED")

    for path in _iter_repo_files(".py", ".yml", ".sh"):
        rel = str(path.relative_to(REPO_ROOT))
        if rel.startswith("tests/") or rel == "scripts/tor/production_dark_launch_check.py":
            continue
        text = path.read_text(errors="ignore")
        if pattern.search(text) and rel not in _ALLOWED_TOR_ENABLED_SETTERS:
            offenders.append(rel)

    assert not offenders, (
        f"Unexpected new reference(s) to TOR_ENABLED: {offenders} -- "
        "review whether a collection script has started reading/setting "
        "it, then add to _ALLOWED_TOR_ENABLED_SETTERS if intentional."
    )


def test_no_collection_script_sets_tor_enabled_true():
    """Direct guard on the specific scripts the real production
    collection cycle invokes (see scripts/run_collection_cycle_safe.sh's
    `docker compose exec -T api ...` calls) -- none may ever hardcode
    TOR_ENABLED=true or unset it in a way that would fall through to a
    non-false default."""
    collection_scripts = [
        "scripts/run_production_collection.py",
        "scripts/run_collection_cycle.py",
        "scripts/run_collection_cycle_safe.sh",
        "scripts/linkedin_auto_progress_collect.py",
        "scripts/seed_priority_coverage_queue.py",
        "scripts/process_search_demand_queue.py",
        "scripts/collector_postgres.py",
        "scripts/linkedin_auth_preflight.py",
        "scripts/linkedin_plan_collect.py",
    ]

    for rel in collection_scripts:
        path = REPO_ROOT / rel
        if not path.exists():
            continue
        text = path.read_text(errors="ignore")
        assert "TOR_ENABLED" not in text, f"{rel} must never reference TOR_ENABLED"


# ---------------------------------------------------------------------
# 5. Static audit: the real-Tor CI job must be explicitly opt-in, never a
#    side effect of ordinary push/PR/manual-dispatch-without-opt-in CI.
# ---------------------------------------------------------------------

def _load_ci_workflow():
    import yaml

    with open(REPO_ROOT / ".github" / "workflows" / "ci.yml") as f:
        # PyYAML parses the bare `on:` key as the boolean True unless
        # quoted -- irrelevant to this test (which only inspects trigger
        # *values*, not the key itself), so no special Loader is needed.
        return yaml.safe_load(f)


def test_ci_workflow_has_push_pull_request_and_workflow_dispatch_triggers():
    workflow = _load_ci_workflow()
    triggers = workflow[True]  # PyYAML's bare-word parsing of the `on:` key

    assert "push" in triggers
    assert "pull_request" in triggers
    assert "workflow_dispatch" in triggers

    run_real_tor_input = triggers["workflow_dispatch"]["inputs"]["run_real_tor"]
    assert run_real_tor_input["type"] == "boolean"
    assert run_real_tor_input["default"] is False


def test_real_tor_ci_job_is_explicitly_opt_in_only():
    """Pins the exact gating condition so a future edit can't silently
    loosen it back into running on ordinary push/PR CI. The condition
    must require BOTH a workflow_dispatch event AND the operator's
    explicit run_real_tor=true -- neither alone is sufficient, and an
    ordinary push/pull_request event (where `inputs.run_real_tor` is not
    even defined) must evaluate this condition to skip the job."""
    workflow = _load_ci_workflow()
    job = workflow["jobs"]["tor-real-integration"]

    assert "if" in job, "tor-real-integration must have an explicit if: gate"
    condition = job["if"]

    assert "workflow_dispatch" in condition
    assert "run_real_tor" in condition
    assert condition == "github.event_name == 'workflow_dispatch' && inputs.run_real_tor == true"


def test_deterministic_verify_job_has_no_event_gate():
    """The deterministic focused-Tor-suite job (`verify`) must run
    unconditionally on every push/PR -- it must NOT carry an `if:` that
    could accidentally couple it to the same opt-in gate as the real-Tor
    job, which would silently stop the deterministic suite from running
    on ordinary CI."""
    workflow = _load_ci_workflow()
    job = workflow["jobs"]["verify"]

    assert "if" not in job


def test_only_the_real_tor_job_carries_the_opt_in_gate():
    """Closed-set guard: no OTHER job in this workflow may reference
    `inputs.run_real_tor` -- if a future edit starts gating something
    else on it (accidentally coupling unrelated jobs to the same opt-in
    switch), this fails loudly rather than silently changing what runs
    on ordinary push/PR CI."""
    workflow = _load_ci_workflow()

    offenders = [
        name for name, job in workflow["jobs"].items()
        if name != "tor-real-integration" and "run_real_tor" in str(job.get("if", ""))
    ]
    assert not offenders, f"unexpected run_real_tor gate on job(s): {offenders}"
