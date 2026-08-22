"""Real, disposable-Tor-container integration tests for the Phase 3
production dark-launch overlay.

Deliberately SEPARATE from tests/test_tor_production_dark_launch.py (the
fast, network-free suite) and from the rest of the repository's ordinary
unit tests: these tests build the real docker/tor image, start real
containers, and require the real Tor network to bootstrap -- exactly the
kind of slower/flakier, network-dependent work that must never gate the
ordinary fast CI run.

Skipped automatically unless:
  - the `docker` CLI is reachable, AND
  - the RUN_REAL_TOR_INTEGRATION=1 environment variable is set.

No LinkedIn traffic anywhere in this file. No NEWNYM. Uses only the
neutral https://check.torproject.org/api/ip endpoint (or
TOR_IP_CHECK_URL override) for the SOCKS-routed connectivity check, via a
throwaway curl container -- not Playwright/Chromium, to keep this
integration layer lightweight.

Every container/network/image this file creates is named with a
`jobpulse-tor-it-` prefix and is removed in a `finally` block, so a
failed run never leaves stray state behind. Never touches any
`*-prod` container already running on the host.
"""
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

_DOCKER_AVAILABLE = shutil.which("docker") is not None
_OPT_IN = os.environ.get("RUN_REAL_TOR_INTEGRATION") == "1"

pytestmark = pytest.mark.skipif(
    not (_DOCKER_AVAILABLE and _OPT_IN),
    reason=(
        "Real Tor integration tests require RUN_REAL_TOR_INTEGRATION=1 "
        "and a reachable docker CLI -- skipped by default so ordinary "
        "CI/unit runs never depend on real Tor network bootstrap."
    ),
)

IMAGE_TAG = "jobpulse-tor-it:local"
SECURITY_ARGS = [
    "--read-only",
    "--cap-drop", "ALL",
    "--cap-add", "CHOWN",
    "--cap-add", "DAC_OVERRIDE",
    "--cap-add", "FOWNER",
    "--cap-add", "SETUID",
    "--cap-add", "SETGID",
    "--security-opt", "no-new-privileges",
    "--tmpfs", "/run/tor:mode=0755",
    "--tmpfs", "/var/lib/tor:mode=0700",
    "--tmpfs", "/var/log/tor:mode=0755",
]


def _docker(*args, check=True, timeout=60):
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=check,
    )


@pytest.fixture(scope="module")
def tor_image():
    _docker("build", "-t", IMAGE_TAG, str(REPO_ROOT / "docker" / "tor"), timeout=300)
    return IMAGE_TAG


@pytest.fixture
def secret_file(tmp_path):
    path = tmp_path / "tor_control_password"
    path.write_text("integration_test_password")
    path.chmod(0o600)
    return path


def _wait_for_health(container_name, timeout_seconds=180):
    deadline = time.monotonic() + timeout_seconds
    last_status = None
    while time.monotonic() < deadline:
        result = _docker(
            "inspect", "--format", "{{.State.Health.Status}}", container_name,
            check=False,
        )
        last_status = result.stdout.strip()
        if last_status == "healthy":
            return last_status
        time.sleep(2)
    return last_status


@pytest.fixture
def running_tor_container(tor_image, secret_file):
    name = f"jobpulse-tor-it-{uuid.uuid4().hex[:8]}"
    _docker(
        "run", "-d", "--name", name,
        *SECURITY_ARGS,
        "-v", f"{secret_file}:/run/secrets/tor_control_password:ro",
        "-e", "TOR_CONTROL_PASSWORD_FILE=/run/secrets/tor_control_password",
        "--health-cmd", 'grep -q "Bootstrapped 100%" /var/log/tor/notices.log',
        "--health-interval", "3s", "--health-retries", "30",
        "--health-start-period", "20s", "--health-timeout", "5s",
        tor_image,
    )
    try:
        yield name
    finally:
        _docker("rm", "-f", name, check=False)


def test_real_container_reaches_bootstrap_100(running_tor_container):
    status = _wait_for_health(running_tor_container, timeout_seconds=180)
    assert status == "healthy", f"Tor container never reported healthy (last status: {status})"

    logs = _docker("logs", running_tor_container).stdout
    assert "Bootstrapped 100%" in logs


def test_real_container_control_port_authenticates(running_tor_container):
    _wait_for_health(running_tor_container, timeout_seconds=180)

    result = _docker(
        "exec", running_tor_container, "sh", "-c",
        'printf \'AUTHENTICATE "integration_test_password"\\r\\nGETINFO status/bootstrap-phase\\r\\nQUIT\\r\\n\' '
        "| nc -w 5 127.0.0.1 9051",
    )
    assert "250 OK" in result.stdout
    assert "PROGRESS=100" in result.stdout


def test_real_container_wrong_control_password_rejected(running_tor_container):
    _wait_for_health(running_tor_container, timeout_seconds=180)

    result = _docker(
        "exec", running_tor_container, "sh", "-c",
        'printf \'AUTHENTICATE "wrong_password_entirely"\\r\\nQUIT\\r\\n\' '
        "| nc -w 5 127.0.0.1 9051",
    )
    assert "250 OK" not in result.stdout


def test_real_container_rootfs_is_actually_read_only(running_tor_container):
    result = _docker(
        "exec", running_tor_container, "sh", "-c", "touch /etc/should_fail 2>&1",
        check=False,
    )
    assert result.returncode != 0
    assert "Read-only file system" in (result.stdout + result.stderr)


def test_real_container_socks_neutral_connectivity(running_tor_container):
    """Routes through the real, running Tor container's SOCKS port via a
    throwaway curl container attached to the SAME network namespace
    (`--network container:<tor>`) -- proves genuine SOCKS-routed Tor
    connectivity without needing Playwright/Chromium in this test layer.
    Neutral endpoint only, never LinkedIn.

    Bounded retry (max 3 attempts, ~20s per-attempt timeout, 2s delay
    between attempts): a previous full run of this file observed exactly
    this test fail once with a transient exit-node TLS hiccup (`curl: (35)
    TLS connect error: ... unexpected eof while reading`), then pass
    cleanly on an immediate isolated retry -- i.e. real Tor circuit/exit-
    node flakiness, not a defect in the container/SOCKS setup. This retry
    is scoped to ONLY this one curl invocation: bootstrap, ControlPort
    auth, and container-health failures are covered by separate tests
    above that do NOT retry, so a genuine bootstrap/auth/config failure
    here still fails on the very first attempt of ITS OWN test, never
    silently retried into a false success by this loop."""
    _wait_for_health(running_tor_container, timeout_seconds=180)

    check_url = os.environ.get("TOR_IP_CHECK_URL", "https://check.torproject.org/api/ip")

    max_attempts = 3
    last_result = None

    for attempt in range(1, max_attempts + 1):
        last_result = _docker(
            "run", "--rm",
            "--network", f"container:{running_tor_container}",
            "curlimages/curl:latest",
            "curl", "-fsS", "--socks5-hostname", "127.0.0.1:9050",
            "--max-time", "20", check_url,
            check=False, timeout=40,
        )

        if last_result.returncode == 0 and '"IsTor":true' in last_result.stdout.replace(" ", ""):
            return

        if attempt < max_attempts:
            time.sleep(2)

    assert last_result.returncode == 0, (
        f"SOCKS-routed curl failed after {max_attempts} attempts: {last_result.stderr}"
    )
    assert '"IsTor":true' in last_result.stdout.replace(" ", "")


def test_real_container_restart_does_not_report_stale_health(running_tor_container):
    """Regression proof for docker/tor/entrypoint.sh's notices.log
    truncation: after `docker restart`, health must NOT stay/report
    healthy from the PREVIOUS process's bootstrap line -- it must
    transition away from healthy and only return once the NEW process
    has actually re-bootstrapped to 100%."""
    _wait_for_health(running_tor_container, timeout_seconds=180)

    _docker("restart", running_tor_container, timeout=30)

    # Immediately after restart, health must not already claim "healthy"
    # from a stale log line still sitting in the tmpfs-mounted directory.
    immediate_status = _docker(
        "inspect", "--format", "{{.State.Health.Status}}", running_tor_container,
        check=False,
    ).stdout.strip()
    assert immediate_status != "healthy", (
        "Health reported 'healthy' immediately after restart -- this would mean "
        "the healthcheck read a stale 'Bootstrapped 100%' line from the previous "
        "process instead of entrypoint.sh's notices.log truncation working."
    )

    final_status = _wait_for_health(running_tor_container, timeout_seconds=180)
    assert final_status == "healthy", "Tor did not re-bootstrap to healthy after restart"


def test_real_container_no_published_host_ports(running_tor_container):
    result = _docker(
        "inspect", "--format", "{{json .NetworkSettings.Ports}}", running_tor_container,
    )
    import json
    ports = json.loads(result.stdout)
    for port_spec, bindings in (ports or {}).items():
        assert not bindings, f"Unexpected host port binding for {port_spec}: {bindings}"


def test_prod_api_container_health_independent_of_tor_container(running_tor_container):
    """If a real jobpulse-api-prod container happens to be running on
    this host (e.g. a local mirror of production), its health must be
    completely unaffected by this test's disposable Tor container being
    started, stopped, or restarted -- proving Tor's presence/absence
    cannot affect API availability. Read-only: never starts, stops, or
    restarts the real api container itself."""
    check = _docker("inspect", "--format", "{{.State.Health.Status}}", "jobpulse-api-prod", check=False)
    if check.returncode != 0:
        pytest.skip("No jobpulse-api-prod container present on this host to observe.")

    before = check.stdout.strip()

    _docker("stop", running_tor_container, timeout=15)
    time.sleep(2)
    during = _docker(
        "inspect", "--format", "{{.State.Health.Status}}", "jobpulse-api-prod", check=False,
    ).stdout.strip()

    assert during == before == "healthy", (
        f"jobpulse-api-prod health changed while the (unrelated) Tor test "
        f"container was stopped: before={before!r} during={during!r}"
    )
