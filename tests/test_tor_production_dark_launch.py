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


# ---------------------------------------------------------------------
# 6. Static audit: Production Tor Dark Launch workflow pinning (Phase
#    3.2B). docker-compose.prod.tor.yml's tor-diagnostic service resolves
#    `${JOBPULSE_API_IMAGE:-ghcr.io/mrezamaghouli/jobpulse-api:main}` and
#    `${TOR_IP_CHECK_URL:-https://check.torproject.org/api/ip}` -- Compose
#    only falls back to those mutable defaults when the variable is UNSET
#    in the process environment, so the workflow must always supply both
#    explicitly. These tests prove the workflow does so, without touching
#    scripts/tor/production_dark_launch.sh or docker-compose.prod.tor.yml
#    (the already-deployed production paths) -- see also
#    tests/test_tor_dark_launch_script.py, which proves those files'
#    behavior independently and is untouched by this change.
# ---------------------------------------------------------------------
DARK_LAUNCH_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "tor-dark-launch.yml"
# Phase 3.3C repin: production/API moved to this commit after the Phase
# 3.3A ControlPort hostname-resolution fix was deployed.
PINNED_PRODUCTION_SHA = "0b0290d5dedc9bfc9fba83a1a97f782a10890b06"
PINNED_API_IMAGE = f"ghcr.io/mrezamaghouli/jobpulse-api:{PINNED_PRODUCTION_SHA}"
PINNED_IP_CHECK_URL = "https://check.torproject.org/api/ip"


def _load_dark_launch_workflow():
    import yaml

    return yaml.safe_load(DARK_LAUNCH_WORKFLOW_PATH.read_text())


def _dark_launch_on_block(workflow):
    # PyYAML (1.1 resolver) parses the bare `on:` key as boolean True.
    return workflow["on"] if "on" in workflow else workflow[True]


def _dark_launch_code_only():
    """Strip full-line `#` comments so substring checks below don't
    false-positive on explanatory prose (e.g. this file's own header
    comment naming the mutable `:main` fallback tag it exists to avoid)."""
    return "\n".join(
        line for line in DARK_LAUNCH_WORKFLOW_PATH.read_text().splitlines()
        if not line.strip().startswith("#")
    )


def test_dark_launch_workflow_is_workflow_dispatch_only():
    workflow = _load_dark_launch_workflow()
    on_block = _dark_launch_on_block(workflow)
    assert set(on_block.keys()) == {"workflow_dispatch"}


def test_dark_launch_workflow_source_has_no_other_trigger_keys():
    source = DARK_LAUNCH_WORKFLOW_PATH.read_text()
    for forbidden in ("push:", "pull_request:", "workflow_run:", "schedule:"):
        assert forbidden not in source, forbidden


def test_dark_launch_tor_image_input_still_strictly_immutable():
    source = DARK_LAUNCH_WORKFLOW_PATH.read_text()
    assert r'^ghcr\.io/mrezamaghouli/jobpulse-tor:[0-9a-f]{40}$' in source


def test_dark_launch_workflow_only_declares_tor_image_as_operator_input():
    """PRODUCTION_API_IMAGE / TOR_DARK_LAUNCH_IP_CHECK_URL must be fixed
    job-level constants, never operator-editable workflow_dispatch
    inputs -- an operator-editable field here could target the wrong API
    image or a non-neutral URL by typo, exactly the failure mode
    EXPECTED_PRODUCTION_SHA in tor-secret-provision.yml already avoids.
    Phase 3.2C adds `confirm` as a second, deliberate operator input (see
    test_dark_launch_workflow_inputs_are_exactly_tor_image_and_confirm) --
    `tor_image` remains the only IMAGE-shaped input; this test's original
    purpose (no image/URL-pinning-related input beyond tor_image) still
    holds."""
    workflow = _load_dark_launch_workflow()
    on_block = _dark_launch_on_block(workflow)
    inputs = on_block["workflow_dispatch"]["inputs"]
    assert set(inputs.keys()) == {"tor_image", "confirm"}


def test_dark_launch_production_api_image_is_exact_and_immutable():
    source = DARK_LAUNCH_WORKFLOW_PATH.read_text()
    assert f'PRODUCTION_API_IMAGE: "{PINNED_API_IMAGE}"' in source
    # Never the floating tag this pinning exists to avoid -- excluding
    # explanatory comment prose, which legitimately names it.
    assert "jobpulse-api:main" not in _dark_launch_code_only()


def test_dark_launch_production_api_image_tag_is_exactly_deployed_sha():
    assert PINNED_API_IMAGE == f"ghcr.io/mrezamaghouli/jobpulse-api:{PINNED_PRODUCTION_SHA}"
    assert PINNED_PRODUCTION_SHA not in DARK_LAUNCH_WORKFLOW_PATH.read_text().split(
        "PRODUCTION_API_IMAGE"
    )[0], "sanity: PINNED_PRODUCTION_SHA must actually appear in the PRODUCTION_API_IMAGE value"
    source = DARK_LAUNCH_WORKFLOW_PATH.read_text()
    assert re.search(
        r'PRODUCTION_API_IMAGE:\s*"ghcr\.io/mrezamaghouli/jobpulse-api:' + PINNED_PRODUCTION_SHA + r'"',
        source,
    )


def test_dark_launch_workflow_validates_production_api_image_before_ssh():
    source = DARK_LAUNCH_WORKFLOW_PATH.read_text()
    assert r'^ghcr\.io/mrezamaghouli/jobpulse-api:[0-9a-f]{40}$' in source
    assert f'"$PRODUCTION_API_IMAGE" != "{PINNED_API_IMAGE}"' in source
    # The validation step must run before the SSH step -- proven by
    # ordering: "Validate inputs and secrets" appears earlier in the file
    # than "Run Tor dark-launch on production VM".
    assert source.index("Validate inputs and secrets") < source.index(
        "Run Tor dark-launch on production VM"
    )


def test_dark_launch_workflow_passes_jobpulse_api_image_to_remote_process():
    """Proves the SSH invocation itself sets JOBPULSE_API_IMAGE in the
    remote process environment -- the actual mechanism that overrides
    docker-compose.prod.tor.yml's `:main` fallback, not merely a
    validated-but-unused local variable."""
    source = DARK_LAUNCH_WORKFLOW_PATH.read_text()
    assert "JOBPULSE_API_IMAGE='$PRODUCTION_API_IMAGE'" in source


def test_dark_launch_workflow_never_relies_on_diagnostic_main_fallback():
    """No path in the official workflow may invoke the remote script
    without JOBPULSE_API_IMAGE set -- i.e. there is exactly one ssh
    invocation of production_dark_launch.sh, and it always carries
    JOBPULSE_API_IMAGE."""
    code = _dark_launch_code_only()
    ssh_invocations = code.count("/opt/jobpulse/scripts/tor/production_dark_launch.sh")
    assert ssh_invocations == 1
    assert "JOBPULSE_API_IMAGE=" in code


def test_dark_launch_neutral_url_is_exactly_check_torproject_org():
    source = DARK_LAUNCH_WORKFLOW_PATH.read_text()
    assert f'TOR_DARK_LAUNCH_IP_CHECK_URL: "{PINNED_IP_CHECK_URL}"' in source
    assert f'"$TOR_DARK_LAUNCH_IP_CHECK_URL" != "{PINNED_IP_CHECK_URL}"' in source


def test_dark_launch_workflow_passes_ip_check_url_to_remote_process():
    source = DARK_LAUNCH_WORKFLOW_PATH.read_text()
    assert "TOR_IP_CHECK_URL='$TOR_DARK_LAUNCH_IP_CHECK_URL'" in source


def test_dark_launch_neutral_url_is_not_an_operator_controlled_input():
    """The pinned URL must be a fixed job-level env value, never sourced
    from `inputs.*` -- an arbitrary operator-supplied URL must not be
    possible for the first production dark launch."""
    workflow = _load_dark_launch_workflow()
    job = workflow["jobs"]["dark-launch"]
    env = job["env"]
    assert env["TOR_DARK_LAUNCH_IP_CHECK_URL"] == PINNED_IP_CHECK_URL
    assert "inputs." not in str(env["TOR_DARK_LAUNCH_IP_CHECK_URL"])

    on_block = _dark_launch_on_block(workflow)
    inputs = on_block["workflow_dispatch"]["inputs"]
    assert "tor_ip_check_url" not in {k.lower() for k in inputs}
    assert "diagnostic_url" not in {k.lower() for k in inputs}


def test_dark_launch_ghcr_token_still_travels_via_stdin_not_argv():
    source = DARK_LAUNCH_WORKFLOW_PATH.read_text()
    # GHCR_TOKEN_B64 must be read from stdin (`$(cat)`) inside the remote
    # command string, never assigned a literal value in that same string.
    assert 'GHCR_TOKEN_B64=\\"\\$(cat)\\"' in source
    assert "<<< \"$GHCR_TOKEN_B64\"" in source
    assert "GHCR_TOKEN_B64='" not in source  # never a literal argv assignment


def test_dark_launch_workflow_introduces_no_new_secrets():
    referenced_secrets = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", DARK_LAUNCH_WORKFLOW_PATH.read_text()))
    assert referenced_secrets == {"VM_SSH_KEY", "GHCR_TOKEN"}


def test_dark_launch_workflow_never_references_tor_enabled():
    """PRODUCTION_API_IMAGE/TOR_DARK_LAUNCH_IP_CHECK_URL pinning must not
    touch anything TOR_ENABLED-related -- keeps this workflow out of
    _ALLOWED_TOR_ENABLED_SETTERS above, since it still never references
    that literal at all."""
    assert "TOR_ENABLED" not in DARK_LAUNCH_WORKFLOW_PATH.read_text()


def test_dark_launch_workflow_permissions_and_concurrency_unchanged():
    workflow = _load_dark_launch_workflow()
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "jobpulse-production-tor-dark-launch",
        "cancel-in-progress": False,
    }


@requires_docker_compose
def test_dark_launch_process_env_pinning_overrides_hostile_production_env(compose_validation_dir):
    """Real `docker compose config` regression proof (not merely a static
    text assertion) that the Phase 3.2B pinning actually works the way
    docker-compose.prod.tor.yml's `${VAR:-default}` substitution requires:
    Docker Compose resolves `${VAR}` from the shell/process environment
    BEFORE falling back to a `.env` file in the project directory, so a
    process env value always wins over a conflicting `.env` value -- it
    is never merely "whichever happens to be defined last" or something
    `.env` could override.

    Writes a HOSTILE .env into the compose project directory (exactly
    what a production .env attacker/misconfiguration could contain:
    `jobpulse-api:main` and a non-approved diagnostic URL), then runs
    `docker compose config` with PRODUCTION_API_IMAGE/
    TOR_DARK_LAUNCH_IP_CHECK_URL's pinned values set as real process env
    vars -- exactly what tor-dark-launch.yml's ssh invocation does on the
    remote host via `JOBPULSE_API_IMAGE='...' TOR_IP_CHECK_URL='...' ...`.
    Starts no container."""
    import os

    hostile_env_content = (
        "JOBPULSE_API_IMAGE=ghcr.io/mrezamaghouli/jobpulse-api:main\n"
        "TOR_IP_CHECK_URL=https://example.invalid/not-approved\n"
    )
    (compose_validation_dir / ".env").write_text(hostile_env_content)

    # tor-diagnostic sits behind the tor-ops Compose profile (never
    # auto-started) -- `docker compose config` only resolves it into
    # `services` when that profile is active, via COMPOSE_PROFILES.
    process_env = os.environ.copy()
    process_env["JOBPULSE_TOR_IMAGE"] = "ghcr.io/mrezamaghouli/jobpulse-tor:test-sha"
    process_env["JOBPULSE_API_IMAGE"] = PINNED_API_IMAGE
    process_env["TOR_IP_CHECK_URL"] = PINNED_IP_CHECK_URL
    process_env["COMPOSE_PROFILES"] = "tor-ops"

    config = _run_compose_config(
        compose_validation_dir, "docker-compose.prod.tor.yml", env=process_env,
    )

    tor_diagnostic = config["services"]["tor-diagnostic"]
    assert tor_diagnostic["image"] == PINNED_API_IMAGE
    assert tor_diagnostic["environment"]["TOR_IP_CHECK_URL"] == PINNED_IP_CHECK_URL

    # Sanity: prove the hostile .env value was actually in play (not just
    # an inert file) -- without the process-env override, Compose would
    # have resolved to it instead. This guards against the test silently
    # passing for the wrong reason (e.g. a typo that made the .env file
    # never get read at all).
    unpinned_env = os.environ.copy()
    unpinned_env["JOBPULSE_TOR_IMAGE"] = "ghcr.io/mrezamaghouli/jobpulse-tor:test-sha"
    unpinned_env["COMPOSE_PROFILES"] = "tor-ops"
    unpinned_config = _run_compose_config(
        compose_validation_dir, "docker-compose.prod.tor.yml", env=unpinned_env,
    )
    unpinned_diagnostic = unpinned_config["services"]["tor-diagnostic"]
    assert unpinned_diagnostic["image"] == "ghcr.io/mrezamaghouli/jobpulse-api:main"
    assert unpinned_diagnostic["environment"]["TOR_IP_CHECK_URL"] == "https://example.invalid/not-approved"


# ---------------------------------------------------------------------
# 7. Accurate Tor control-plane DB write-path documentation (Phase 3.2B
#    correction). The dark-launch diagnostic is NOT fully database-
#    read-only: check_bootstrap_status() intentionally persists Phase 2
#    Tor bootstrap/control-plane observability. These tests trace the
#    EXACT write path from the deployed diagnostic entrypoint down to the
#    literal SQL table names, and prove no business-data table or
#    collector/ingestion code is ever reachable from it -- replacing the
#    inaccurate "database untouched" characterization with real evidence.
# ---------------------------------------------------------------------
import inspect

import scripts.tor.circuit_manager as circuit_manager_module
import scripts.tor.verify_tor_connectivity as verify_tor_connectivity_module

_BUSINESS_DATA_TABLES = ("jobs", "companies", "linkedin_query_runs")
_TOR_CONTROL_PLANE_TABLES = ("tor_instances", "tor_circuit_events")


def test_record_bootstrap_functions_only_write_tor_control_plane_tables():
    """record_bootstrap_started/ready/failed (called by
    check_bootstrap_status(), in turn called by
    production_dark_launch_check.py's diagnostic) must only ever
    reference tor_instances/tor_circuit_events in their own source --
    never jobs/companies/linkedin_query_runs. Uses inspect.getsource() on
    the exact three functions so this fails loudly if a future edit adds
    a write to any other table, rather than trusting a one-time manual
    trace to stay correct forever."""
    for name in ("record_bootstrap_started", "record_bootstrap_ready", "record_bootstrap_failed"):
        func = getattr(circuit_manager_module, name)
        source = inspect.getsource(func)

        for table in _BUSINESS_DATA_TABLES:
            assert table not in source, f"{name}() must never reference {table}"

        assert any(table in source for table in _TOR_CONTROL_PLANE_TABLES), (
            f"{name}() is expected to write to one of {_TOR_CONTROL_PLANE_TABLES}"
        )


def test_record_bootstrap_functions_full_operation_set_is_control_plane_only():
    """Phase 3.2B correction: the write path is not merely 'one UPDATE
    per table' -- each record_bootstrap_* call also (a) ensures the
    tor_instances/tor_circuit_events schema exists (CREATE TABLE IF NOT
    EXISTS, via ensure_tor_instances_table/ensure_tor_circuit_events_table),
    (b) upserts the instance row (_ensure_instance_row, INSERT ... ON
    CONFLICT DO NOTHING), (c) UPDATEs tor_instances, and (d) emits a
    bounded-retention event via emit_event() (INSERT into
    tor_circuit_events + a prune DELETE in the same transaction). All
    four operation kinds are acknowledged here explicitly -- none of them
    reach a business-data table."""
    for name in ("record_bootstrap_started", "record_bootstrap_ready", "record_bootstrap_failed"):
        func = getattr(circuit_manager_module, name)
        source = inspect.getsource(func)

        # (a) schema-ensure DDL
        assert "ensure_tor_instances_table(" in source
        assert "ensure_tor_circuit_events_table(" in source
        # (b) row upsert
        assert "_ensure_instance_row(" in source
        # (c) UPDATE tor_instances
        assert "UPDATE tor_instances" in source
        # (d) bounded-retention event emission
        assert "emit_event(" in source

    # The actual DDL lives in module-level SQL string constants, executed
    # (not inlined) by ensure_tor_instances_table()/
    # ensure_tor_circuit_events_table().
    assert "CREATE TABLE IF NOT EXISTS tor_instances" in circuit_manager_module.CREATE_TOR_INSTANCES_TABLE_SQL
    assert "CREATE TABLE IF NOT EXISTS tor_circuit_events" in circuit_manager_module.CREATE_TOR_CIRCUIT_EVENTS_TABLE_SQL

    emit_event_source = inspect.getsource(circuit_manager_module.emit_event)
    assert "INSERT INTO tor_circuit_events" in emit_event_source
    # The bounded-retention prune DELETE is a separate helper, called by
    # emit_event() in the same transaction as the INSERT above.
    assert "_prune_tor_circuit_events_in_transaction(" in emit_event_source
    prune_source = inspect.getsource(circuit_manager_module._prune_tor_circuit_events_in_transaction)
    assert "DELETE FROM tor_circuit_events" in prune_source

    # None of the above operations, across any of the source/DDL blocks
    # inspected in this test, ever reference a business-data table.
    for source in (
        circuit_manager_module.CREATE_TOR_INSTANCES_TABLE_SQL,
        circuit_manager_module.CREATE_TOR_CIRCUIT_EVENTS_TABLE_SQL,
        emit_event_source,
        prune_source,
        *(inspect.getsource(getattr(circuit_manager_module, n))
          for n in ("record_bootstrap_started", "record_bootstrap_ready", "record_bootstrap_failed")),
    ):
        for table in _BUSINESS_DATA_TABLES:
            assert table not in source


def test_check_bootstrap_status_only_calls_record_bootstrap_functions():
    """Pins the exact write-path entrypoint: check_bootstrap_status()
    (called directly by production_dark_launch_check.py) must only ever
    call record_bootstrap_started/ready/failed for persistence -- no
    other circuit_manager write function (e.g. the circuit-tracking
    UPDATE tor_circuits path used elsewhere in that module)."""
    source = inspect.getsource(verify_tor_connectivity_module.check_bootstrap_status)
    for name in ("record_bootstrap_started", "record_bootstrap_ready", "record_bootstrap_failed"):
        assert f"{name}(" in source

    # Negative: this function must not itself touch tor_circuits (the
    # separate per-circuit tracking table used by collection-path code,
    # not by this dark-launch diagnostic).
    assert "tor_circuits" not in source


def test_production_dark_launch_check_never_imports_business_data_modules():
    """Static import-level guard: the diagnostic entrypoint must never
    import anything from a collector/ingestion/business-data module --
    proves there is no code PATH into jobs/companies/linkedin_query_runs
    from this diagnostic, not merely that its current call graph happens
    not to reach one."""
    check_script_path = REPO_ROOT / "scripts" / "tor" / "production_dark_launch_check.py"
    source = check_script_path.read_text()

    forbidden_import_substrings = (
        "collector", "ingest", "linkedin_plan_collect", "run_collection_cycle",
        "run_production_collection", "seed_priority_coverage_queue",
        "process_search_demand_queue",
    )
    import_lines = [
        line for line in source.splitlines()
        if line.strip().startswith("import ") or line.strip().startswith("from ")
    ]
    for line in import_lines:
        for forbidden in forbidden_import_substrings:
            assert forbidden not in line, f"unexpected import in production_dark_launch_check.py: {line}"


def test_inspect_instance_diagnostic_read_path_is_read_only():
    """observability.inspect_instance() (used by the diagnostic ONLY on
    the failure path, to read back the already-persisted error category)
    must never itself write -- no INSERT/UPDATE/DELETE in its source."""
    import scripts.tor.observability as observability_module

    source = inspect.getsource(observability_module.inspect_instance)
    for forbidden in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
        assert forbidden not in source


def test_no_misleading_fully_database_read_only_claim_in_dark_launch_tests():
    """Phase 3.2B correction: the dark-launch diagnostic intentionally
    persists Tor control-plane observability (tor_instances,
    tor_circuit_events) via check_bootstrap_status() -- it is NOT fully
    database-read-only. This test/this file and the workflow file must
    never claim otherwise; container mutation isolation (api/db/frontend
    never restarted/recreated/stopped) and DB control-plane observability
    are two different facts and must not be conflated. The one known
    instance of this inaccurate phrasing lives in the already-deployed
    scripts/tor/production_dark_launch.sh (a single fail() message on the
    diagnostic-failure path) -- out of scope for this pass per the
    Phase 3.2B task boundary (that file is not modified here to avoid
    expanding this fix into the already-deployed production script), and
    is reported separately rather than silently left uncorrected."""
    misleading_phrases = ("database untouched", "no database mutation", "database read-only", "database is read-only")
    for path in (
        REPO_ROOT / "tests" / "test_tor_production_dark_launch.py",
        REPO_ROOT / "tests" / "test_tor_dark_launch_script.py",
        DARK_LAUNCH_WORKFLOW_PATH,
    ):
        text = path.read_text()
        if path.name == "test_tor_production_dark_launch.py":
            # Section 7 above (from its header comment through end of
            # file) legitimately discusses/corrects this exact phrase --
            # exclude that whole self-referential block from the scan so
            # it doesn't false-positive on its own explanation. Anything
            # BEFORE section 7 (the rest of this file) is still scanned.
            marker = "# 7. Accurate Tor control-plane DB write-path documentation"
            text = text[: text.index(marker)]
        text = text.lower()
        for phrase in misleading_phrases:
            assert phrase not in text, f"{path.name} must not claim '{phrase}' for the full dark-launch diagnostic"


# ---------------------------------------------------------------------
# 8. Operator-facing runtime DB side-effect notice (Phase 3.2B hardening
#    pass). Proves tor-dark-launch.yml prints an accurate, non-secret
#    notice to the GitHub Actions log BEFORE the SSH connection is
#    established, distinguishing Tor control-plane observability from
#    business data, and stating that Tor rollback does not revert the
#    observability records.
# ---------------------------------------------------------------------
def test_dark_launch_workflow_contains_db_side_effect_notice():
    code = _dark_launch_code_only()
    assert "tor_instances" in code
    assert "tor_circuit_events" in code


def test_dark_launch_notice_distinguishes_control_plane_from_business_data():
    code = _dark_launch_code_only()
    for business_table in ("jobs", "companies", "linkedin_query_runs"):
        assert business_table in code, f"notice must name {business_table} as data that must not be mutated"


def test_dark_launch_notice_states_rollback_does_not_revert_observability():
    code = _dark_launch_code_only()
    assert "not revert" in code.lower()


def test_dark_launch_notice_appears_before_ssh_key_setup():
    """The notice must print to the log before the production SSH
    connection is even established -- proven by ordering: the notice
    heredoc precedes `mkdir -p ~/.ssh` (the first SSH-setup command) in
    the 'Run Tor dark-launch on production VM' step."""
    code = _dark_launch_code_only()
    assert code.index("tor_instances") < code.index('mkdir -p ~/.ssh')


def test_dark_launch_notice_contains_no_db_credentials_or_secrets():
    code = _dark_launch_code_only()
    for forbidden in ("POSTGRES_PASSWORD", "PGPASSWORD", "postgresql://", "jobpulse_password"):
        assert forbidden not in code


def test_dark_launch_notice_is_a_static_heredoc_not_secret_interpolated():
    """The notice must be a plain, quoted heredoc (`<<'NOTICE'`) -- no
    variable expansion inside it -- so it can never accidentally
    interpolate a secret value (VM_SSH_KEY, GHCR_TOKEN) into the log."""
    source = DARK_LAUNCH_WORKFLOW_PATH.read_text()
    assert "<<'NOTICE'" in source


# ---------------------------------------------------------------------
# 9. First-dark-launch execution guards (Phase 3.2C): a required
#    `confirm` input, exact production-SHA and exact-Tor-image pinning
#    beyond the format regex, and -- the meaty part -- a remote preflight
#    gate that runs INSIDE THE SAME ssh session as production_dark_launch.sh,
#    before it, verifying the VM's actual `git rev-parse HEAD` and the
#    exact tracked dark-launch-critical files against HEAD. The ordering/
#    fail-closed tests below don't just read the YAML text -- they run
#    the outer "Run Tor dark-launch on production VM" step for real
#    (with `ssh`/`ssh-keyscan` stubbed so no real network/SSH occurs) so
#    bash performs its own real variable substitution, capture exactly
#    the command string that would be sent to the VM, then execute THAT
#    captured command against a real, disposable git repo standing in
#    for /opt/jobpulse (the literal `/opt/jobpulse` path is textually
#    substituted for the disposable repo's path -- the only accommodation
#    made for sandboxing; the preflight logic itself is untouched, exact,
#    real text pulled from the current workflow file). No fake docker/
#    curl needed: the preflight itself never calls either, and the
#    downstream production_dark_launch.sh is stood in for by a tiny
#    recorder script that never touches Docker.
# ---------------------------------------------------------------------
# Independent of PINNED_PRODUCTION_SHA above: Phase 3.3A rebuilt only the
# API image, never the Tor image, so the Tor image keeps its own,
# separately-tracked SHA that must NOT move in lockstep with a future
# production/API repin (see test_dark_launch_tor_image_pin_is_independent_
# of_production_sha_pin below).
PINNED_TOR_IMAGE_SHA = "5dffbd669eec52f5283503bb6409a430509175a0"
PINNED_TOR_IMAGE = f"ghcr.io/mrezamaghouli/jobpulse-tor:{PINNED_TOR_IMAGE_SHA}"

_DARK_LAUNCH_CRITICAL_FILES = (
    "scripts/tor/production_dark_launch.sh",
    "scripts/tor/production_dark_launch_check.py",
    "docker-compose.prod.yml",
    "docker-compose.prod.tor.yml",
)


def test_dark_launch_workflow_inputs_are_exactly_tor_image_and_confirm():
    workflow = _load_dark_launch_workflow()
    on_block = _dark_launch_on_block(workflow)
    inputs = on_block["workflow_dispatch"]["inputs"]
    assert set(inputs.keys()) == {"tor_image", "confirm"}
    assert inputs["confirm"]["required"] is True
    assert inputs["confirm"]["type"] == "string"


def test_dark_launch_confirm_must_equal_launch_tor_dark():
    code = _dark_launch_code_only()
    assert 'CONFIRM_INPUT" != "LAUNCH_TOR_DARK"' in code


def test_dark_launch_confirm_checked_before_any_other_validation():
    """Matches the tor-secret-provision.yml discipline: confirm is the
    very first check in the validation step, before even the SSH-key
    presence check."""
    code = _dark_launch_code_only()
    assert code.index('CONFIRM_INPUT" != "LAUNCH_TOR_DARK"') < code.index("VM_SSH_KEY secret is missing")


def test_dark_launch_expected_production_sha_is_exact():
    source = DARK_LAUNCH_WORKFLOW_PATH.read_text()
    assert f'EXPECTED_PRODUCTION_SHA: "{PINNED_PRODUCTION_SHA}"' in source


def test_dark_launch_expected_tor_image_is_exact():
    source = DARK_LAUNCH_WORKFLOW_PATH.read_text()
    assert f'EXPECTED_TOR_IMAGE: "{PINNED_TOR_IMAGE}"' in source


def test_dark_launch_tor_image_pin_is_independent_of_production_sha_pin():
    """Regression guard for Phase 3.3C: production/API and the Tor image
    are rebuilt/deployed on independent schedules (the Phase 3.3A deploy
    moved production/API without rebuilding the Tor image at all) -- the
    Tor image pin must never be silently re-derived from the production
    SHA pin again."""
    assert PINNED_TOR_IMAGE_SHA != PINNED_PRODUCTION_SHA
    assert PINNED_TOR_IMAGE == f"ghcr.io/mrezamaghouli/jobpulse-tor:{PINNED_TOR_IMAGE_SHA}"
    assert PINNED_PRODUCTION_SHA not in PINNED_TOR_IMAGE


def test_dark_launch_expected_production_sha_not_derived_from_github_sha():
    code = _dark_launch_code_only()
    assert "github.sha" not in code


def test_dark_launch_tor_image_regex_still_present():
    source = DARK_LAUNCH_WORKFLOW_PATH.read_text()
    assert r'^ghcr\.io/mrezamaghouli/jobpulse-tor:[0-9a-f]{40}$' in source


def test_dark_launch_tor_image_exact_equality_validation_exists():
    """The regex alone would accept a DIFFERENT, otherwise-valid 40-hex
    SHA -- exact equality against EXPECTED_TOR_IMAGE is the additional
    guard that pins the first dark launch to the one real build."""
    code = _dark_launch_code_only()
    assert 'TOR_IMAGE_INPUT" != "$EXPECTED_TOR_IMAGE"' in code
    # Ordering: regex check must run before the exact-equality check
    # (regex rejects malformed input with a format-specific message;
    # equality then narrows further).
    assert code.index(r'jobpulse-tor:[0-9a-f]{40}$') < code.index('TOR_IMAGE_INPUT" != "$EXPECTED_TOR_IMAGE"')


def test_dark_launch_neither_expected_constant_is_a_workflow_dispatch_input():
    workflow = _load_dark_launch_workflow()
    on_block = _dark_launch_on_block(workflow)
    inputs = on_block["workflow_dispatch"]["inputs"]
    assert "expected_production_sha" not in {k.lower() for k in inputs}
    assert "expected_tor_image" not in {k.lower() for k in inputs}


def test_dark_launch_tracked_file_drift_check_covers_exactly_the_critical_files():
    code = _dark_launch_code_only()
    match = re.search(r"git diff --quiet HEAD -- ([^\n;]+)", code)
    assert match, "expected a `git diff --quiet HEAD -- <files>` invocation"
    listed_files = match.group(1).split()
    assert listed_files == list(_DARK_LAUNCH_CRITICAL_FILES)


def test_dark_launch_preflight_does_not_require_full_working_tree_clean():
    """Must be a scoped `git diff --quiet HEAD -- <exact files>`, never a
    bare `git status`/`git diff --quiet` (no path args) that would also
    fail on production's known untracked runtime paths like state/."""
    code = _dark_launch_code_only()
    assert "git status" not in code
    assert "git diff --quiet HEAD --" in code
    assert "git diff --quiet HEAD\n" not in code


def test_dark_launch_preflight_runs_in_same_ssh_session_before_script():
    """Ordering proof at the text level: the SHA check and file-drift
    check both appear inside the SAME double-quoted ssh command-string
    argument as the final production_dark_launch.sh invocation (not a
    separate ssh call), and both precede it textually within that one
    argument."""
    code = _dark_launch_code_only()
    ssh_arg_match = re.search(r'ssh -i.*?"(set -e.*?production_dark_launch\.sh)"', code, re.DOTALL)
    assert ssh_arg_match, "expected the preflight + launch to share one ssh command-string argument"
    remote_arg = ssh_arg_match.group(1)
    assert "git rev-parse HEAD" in remote_arg
    assert "git diff --quiet HEAD --" in remote_arg
    assert remote_arg.index("git rev-parse HEAD") < remote_arg.index("git diff --quiet HEAD --")
    assert remote_arg.index("git diff --quiet HEAD --") < remote_arg.index("production_dark_launch.sh")


def _capture_remote_command(tmp_path, extra_step_env=None):
    """Runs the REAL 'Run Tor dark-launch on production VM' step from the
    current workflow file, with `ssh`/`ssh-keyscan` stubbed so bash does
    its own real substitution and no network/SSH occurs, and returns the
    exact command string that would have been sent to the VM."""
    import os

    source = DARK_LAUNCH_WORKFLOW_PATH.read_text()
    lines = source.splitlines()

    def extract_run_blocks(lines):
        blocks = []
        i = 0
        while i < len(lines):
            if lines[i].strip() == "run: |":
                indent = len(lines[i]) - len(lines[i].lstrip(" "))
                body_indent = None
                body = []
                j = i + 1
                while j < len(lines):
                    line = lines[j]
                    if line.strip() == "":
                        body.append("")
                        j += 1
                        continue
                    cur_indent = len(line) - len(line.lstrip(" "))
                    if cur_indent <= indent:
                        break
                    if body_indent is None:
                        body_indent = cur_indent
                    body.append(line[body_indent:] if len(line) >= body_indent else line)
                    j += 1
                blocks.append("\n".join(body))
                i = j
            else:
                i += 1
        return blocks

    blocks = extract_run_blocks(lines)
    assert len(blocks) == 2, f"expected exactly 2 run blocks, found {len(blocks)}"
    launch_step_script = tmp_path / "_outer_launch_step.sh"
    launch_step_script.write_text(blocks[1])

    stub_dir = tmp_path / "_stub_bin"
    stub_dir.mkdir()
    capture_file = tmp_path / "_captured_remote_command.txt"

    ssh_stub = stub_dir / "ssh"
    ssh_stub.write_text(
        "#!/usr/bin/env bash\n"
        "for ((i=1; i<=$#; i++)); do last=\"${!i}\"; done\n"
        f"printf '%s' \"$last\" > '{capture_file}'\n"
        "exit 0\n"
    )
    ssh_stub.chmod(0o755)

    keyscan_stub = stub_dir / "ssh-keyscan"
    keyscan_stub.write_text("#!/usr/bin/env bash\nexit 0\n")
    keyscan_stub.chmod(0o755)

    fake_home = tmp_path / "_fake_home"
    fake_home.mkdir()

    env = os.environ.copy()
    env["PATH"] = f"{stub_dir}:{env['PATH']}"
    env["HOME"] = str(fake_home)
    env.update({
        "VM_HOST": "dummy-host",
        "VM_USER": "dummy-user",
        "VM_PORT": "22",
        "VM_SSH_KEY": "dummy-ssh-key",
        "GHCR_TOKEN": "dummy-ghcr-token",
        "TOR_IMAGE_INPUT": PINNED_TOR_IMAGE,
        "PRODUCTION_API_IMAGE": PINNED_API_IMAGE,
        "TOR_DARK_LAUNCH_IP_CHECK_URL": PINNED_IP_CHECK_URL,
        "EXPECTED_PRODUCTION_SHA": PINNED_PRODUCTION_SHA,
    })
    if extra_step_env:
        env.update(extra_step_env)

    result = subprocess.run(
        ["bash", str(launch_step_script)], env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"outer launch step itself failed: {result.stderr}"
    assert capture_file.is_file(), "ssh stub was never invoked"
    return capture_file.read_text()


@pytest.fixture
def fake_production_repo(tmp_path):
    """A disposable git repo standing in for /opt/jobpulse, with the four
    dark-launch-critical tracked files committed. Returns (repo_dir, sha)."""
    repo_dir = tmp_path / "fake_opt_jobpulse"
    repo_dir.mkdir()

    def run_git(*args):
        subprocess.run(["git", *args], cwd=str(repo_dir), check=True, capture_output=True)

    run_git("init", "-q")
    run_git("config", "user.email", "test@example.com")
    run_git("config", "user.name", "Test")

    (repo_dir / "scripts" / "tor").mkdir(parents=True)
    recorder = repo_dir / "scripts" / "tor" / "production_dark_launch.sh"
    recorder.write_text(
        "#!/usr/bin/env bash\n"
        "echo PRODUCTION_DARK_LAUNCH_SH_INVOKED\n"
        "cat > /dev/null\n"  # consume the GHCR_TOKEN_B64 stdin, like the real script would
        "exit 0\n"
    )
    recorder.chmod(0o755)
    (repo_dir / "scripts" / "tor" / "production_dark_launch_check.py").write_text("# stand-in\n")
    (repo_dir / "docker-compose.prod.yml").write_text("services: {}\n")
    (repo_dir / "docker-compose.prod.tor.yml").write_text("services: {}\n")

    run_git("add", "-A")
    run_git("commit", "-q", "-m", "initial")

    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo_dir), check=True, capture_output=True, text=True,
    ).stdout.strip()
    return repo_dir, sha


def _run_captured_command_against_fake_repo(command_text, fake_repo_dir, expected_sha_for_pass=None):
    """Substitutes the hardcoded /opt/jobpulse path for the disposable
    fake repo's path (the only accommodation made for sandboxing) and
    executes the exact captured command, feeding dummy GHCR token bytes
    on stdin exactly like the real ssh <<< redirection does."""
    localized = command_text.replace("/opt/jobpulse", str(fake_repo_dir))
    result = subprocess.run(
        ["bash", "-c", localized], input="ZHVtbXk=", capture_output=True, text=True, timeout=30,
    )
    return result


@requires_docker_compose  # reuses the same subprocess/git-availability discipline as section 3; git itself is required, not docker, but this keeps the marker consistent with "requires a real external CLI"
def test_remote_preflight_blocks_script_on_production_sha_mismatch(tmp_path, fake_production_repo):
    fake_repo_dir, actual_sha = fake_production_repo
    # actual_sha != PINNED_PRODUCTION_SHA (a fresh disposable repo's
    # commit SHA can never coincidentally equal the real pinned value).
    assert actual_sha != PINNED_PRODUCTION_SHA

    command_text = _capture_remote_command(tmp_path)
    result = _run_captured_command_against_fake_repo(command_text, fake_repo_dir)

    assert result.returncode != 0
    assert "TOR_DARK_LAUNCH_PREFLIGHT_FAILED" in result.stderr
    assert "production SHA mismatch" in result.stderr
    assert "PRODUCTION_DARK_LAUNCH_SH_INVOKED" not in result.stdout


def test_remote_preflight_blocks_script_on_tracked_file_drift(tmp_path, fake_production_repo):
    fake_repo_dir, actual_sha = fake_production_repo
    # Dirty one of the four critical tracked files (uncommitted change).
    (fake_repo_dir / "docker-compose.prod.tor.yml").write_text("services: {}\n# drifted\n")

    command_text = _capture_remote_command(tmp_path, extra_step_env={"EXPECTED_PRODUCTION_SHA": actual_sha})
    result = _run_captured_command_against_fake_repo(command_text, fake_repo_dir)

    assert result.returncode != 0
    assert "TOR_DARK_LAUNCH_PREFLIGHT_FAILED" in result.stderr
    assert "tracked dark-launch-critical file(s) differ from HEAD" in result.stderr
    assert "PRODUCTION_DARK_LAUNCH_SH_INVOKED" not in result.stdout


def test_remote_preflight_allows_script_when_clean_and_sha_matches(tmp_path, fake_production_repo):
    fake_repo_dir, actual_sha = fake_production_repo

    command_text = _capture_remote_command(tmp_path, extra_step_env={"EXPECTED_PRODUCTION_SHA": actual_sha})
    result = _run_captured_command_against_fake_repo(command_text, fake_repo_dir)

    assert result.returncode == 0, result.stderr
    assert "TOR_DARK_LAUNCH_PREFLIGHT_OK" in result.stdout
    assert "PRODUCTION_DARK_LAUNCH_SH_INVOKED" in result.stdout


def test_remote_preflight_ignores_untracked_state_directory(tmp_path, fake_production_repo):
    """Production has a known, legitimate untracked state/ directory --
    it must never cause the preflight to fail closed."""
    fake_repo_dir, actual_sha = fake_production_repo
    (fake_repo_dir / "state").mkdir()
    (fake_repo_dir / "state" / "runtime.json").write_text("{}\n")

    command_text = _capture_remote_command(tmp_path, extra_step_env={"EXPECTED_PRODUCTION_SHA": actual_sha})
    result = _run_captured_command_against_fake_repo(command_text, fake_repo_dir)

    assert result.returncode == 0, result.stderr
    assert "TOR_DARK_LAUNCH_PREFLIGHT_OK" in result.stdout
    assert "PRODUCTION_DARK_LAUNCH_SH_INVOKED" in result.stdout


def test_remote_preflight_ignores_drift_in_a_non_critical_tracked_file(tmp_path, fake_production_repo):
    """The drift gate is scoped to exactly the four critical files --
    an uncommitted change to some OTHER tracked file must not block the
    launch (proves the gate isn't accidentally a full working-tree-clean
    requirement in disguise)."""
    fake_repo_dir, _initial_sha = fake_production_repo
    other_file = fake_repo_dir / "README.md"
    other_file.write_text("not one of the four critical files\n")
    subprocess.run(["git", "add", "README.md"], cwd=str(fake_repo_dir), check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-q", "-m", "add readme"],
        cwd=str(fake_repo_dir), check=True, capture_output=True,
    )
    # HEAD moved with the commit above -- re-fetch it rather than reusing
    # the fixture's now-stale initial SHA.
    actual_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(fake_repo_dir), check=True, capture_output=True, text=True,
    ).stdout.strip()
    # Now dirty it (uncommitted) -- a non-critical tracked file drifting.
    other_file.write_text("dirtied after commit\n")

    command_text = _capture_remote_command(tmp_path, extra_step_env={"EXPECTED_PRODUCTION_SHA": actual_sha})
    result = _run_captured_command_against_fake_repo(command_text, fake_repo_dir)

    assert result.returncode == 0, result.stderr
    assert "PRODUCTION_DARK_LAUNCH_SH_INVOKED" in result.stdout


def test_remote_preflight_gates_are_the_only_thing_before_script_invocation():
    """No docker/curl command of any kind may appear in the remote
    command string before the production_dark_launch.sh invocation --
    the preflight is git-only."""
    code = _dark_launch_code_only()
    ssh_arg_match = re.search(r'ssh -i.*?"(set -e.*?)"\s*\\\n\s*<<<', code, re.DOTALL)
    assert ssh_arg_match
    remote_arg = ssh_arg_match.group(1)
    preamble = remote_arg.split("production_dark_launch.sh")[0]
    assert "docker" not in preamble
    assert "curl" not in preamble


def test_dark_launch_phase_3_2b_pinning_still_intact_after_phase_3_2c():
    """Regression guard: none of the Phase 3.2C additions above may have
    weakened the Phase 3.2B API-image/URL pinning."""
    source = DARK_LAUNCH_WORKFLOW_PATH.read_text()
    assert f'PRODUCTION_API_IMAGE: "{PINNED_API_IMAGE}"' in source
    assert f'TOR_DARK_LAUNCH_IP_CHECK_URL: "{PINNED_IP_CHECK_URL}"' in source
    assert "JOBPULSE_API_IMAGE='$PRODUCTION_API_IMAGE'" in source
    assert "TOR_IP_CHECK_URL='$TOR_DARK_LAUNCH_IP_CHECK_URL'" in source


def test_dark_launch_still_workflow_dispatch_only_after_phase_3_2c():
    workflow = _load_dark_launch_workflow()
    on_block = _dark_launch_on_block(workflow)
    assert set(on_block.keys()) == {"workflow_dispatch"}


def test_dark_launch_no_new_secrets_introduced_by_phase_3_2c():
    referenced_secrets = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", DARK_LAUNCH_WORKFLOW_PATH.read_text()))
    assert referenced_secrets == {"VM_SSH_KEY", "GHCR_TOKEN"}
