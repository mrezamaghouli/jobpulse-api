"""
Regression tests for the PostgreSQL backup + restore-verification workflow.

Before this fix:
  - scripts/verify_latest_backup.sh searched the wrong directory
    (/opt/jobpulse/backups instead of /opt/jobpulse/backups/postgres) for the
    wrong extension (jobpulse_*.sql instead of *.dump), so it never found the
    custom-format dumps actually produced by backup_postgres_prod.sh.
  - Its restore step piped a custom-format dump into `psql`, which is the
    wrong tool for `pg_dump -Fc` output, and contained a corrupted
    redirection (`2>& -f ...`) that silently killed the script mid-restore.
  - backup_postgres_prod.sh validated the dump (`pg_restore -l`) AFTER
    renaming it to its final filename and writing its sha256 checksum, so a
    structurally corrupt dump could be promoted with a checksum that matched
    the corrupt bytes -- check_postgres_backups.py would then report it as
    healthy.

These tests exercise the real scripts against a fake `docker` binary (no real
Postgres, Docker daemon, or production credentials are touched) to prove the
fixed ordering and tool selection.
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

CHANGED_SCRIPTS = [
    SCRIPTS / "backup_postgres_prod.sh",
    SCRIPTS / "verify_latest_backup.sh",
]

MAGIC = "PGDMP_FAKE_CUSTOM_FORMAT_V1"

FAKE_DOCKER = r"""#!/usr/bin/env bash
# Fake docker CLI for tests: only understands
# "compose -f FILE exec -T SERVICE <cmd...>". No real container is touched.
set -u

MAGIC="__MAGIC__"
MODE="${FAKE_PGDUMP_MODE:-ok}"

log_call() {
  if [[ -n "${FAKE_DOCKER_CALL_LOG:-}" ]]; then
    echo "$1" >> "$FAKE_DOCKER_CALL_LOG"
  fi
}

if [[ "${1:-}" != "compose" ]]; then
  echo "fake-docker: unsupported invocation: $*" >&2
  exit 99
fi
shift
[[ "${1:-}" == "-f" ]] && { shift; shift; }
if [[ "${1:-}" != "exec" ]]; then
  echo "fake-docker: expected exec, got: $*" >&2
  exit 99
fi
shift
[[ "${1:-}" == "-T" ]] && shift
shift # service name

cmd="${1:-}"
shift || true
log_call "$cmd"

case "$cmd" in
  pg_dump)
    case "$MODE" in
      fail) echo "fake-docker pg_dump: simulated connection failure" >&2; exit 1 ;;
      empty) exit 0 ;;
      corrupt) printf 'NOT_A_REAL_DUMP_GARBLED_BYTES' ;;
      ok|*) printf '%s' "$MAGIC"; printf 'FAKEDUMPCONTENT_TABLE_jobs_ROWS_42' ;;
    esac
    ;;

  pg_restore)
    stdin_data="$(cat)"
    if [[ "$*" == *"-l"* ]]; then
      if [[ "$stdin_data" == "$MAGIC"* ]]; then
        echo "; Archive created at fake-time"
        echo "1; 0 0 TABLE public jobs fake_user"
        exit 0
      else
        echo "pg_restore: error: input file does not appear to be a valid archive" >&2
        exit 1
      fi
    else
      if [[ "$stdin_data" == "$MAGIC"* ]]; then
        exit 0
      else
        echo "pg_restore: error: input file does not appear to be a valid archive" >&2
        exit 1
      fi
    fi
    ;;

  psql)
    has_c=0
    for a in "$@"; do
      if [[ "$a" == "-c" || "$a" == "-tAc" ]]; then has_c=1; fi
    done
    if [[ $has_c -eq 1 ]]; then
      last="${*: -1}"
      if [[ "$last" == *"COUNT(*)"* && "$last" == *"information_schema.tables"* ]]; then
        echo "3"
      elif [[ "$last" == *"to_regclass"* ]]; then
        echo "42"
      else
        echo "0"
      fi
      exit 0
    fi
    stdin_data="$(cat)"
    if [[ "$stdin_data" == "$MAGIC"* ]]; then
      echo "psql:<stdin>:1: ERROR:  syntax error at or near \"\$PGDMP_FAKE\"" >&2
      exit 3
    else
      exit 0
    fi
    ;;

  createdb|dropdb)
    if [[ "$cmd" == "dropdb" && "${FAKE_DROPDB_MODE:-ok}" == "fail" ]]; then
      echo "fake-docker dropdb: simulated failure" >&2
      exit 1
    fi
    exit 0
    ;;

  *)
    echo "fake-docker: unsupported command: $cmd $*" >&2
    exit 99
    ;;
esac
""".replace("__MAGIC__", MAGIC)


@pytest.fixture
def sandbox(tmp_path):
    """A disposable sandbox: fake `docker` on PATH, isolated backup/log dirs."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_bin = bin_dir / "docker"
    docker_bin.write_text(FAKE_DOCKER)
    docker_bin.chmod(docker_bin.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    backup_dir = tmp_path / "backups" / "postgres"
    backup_dir.mkdir(parents=True)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    compose_file = tmp_path / "docker-compose.prod.yml"
    compose_file.write_text("services: {}\n")
    call_log = tmp_path / "docker_calls.log"

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["COMPOSE_FILE"] = str(compose_file)
    env["JOBPULSE_DB_SERVICE"] = "db"
    env["POSTGRES_DB"] = "jobpulse"
    env["POSTGRES_USER"] = "jobpulse_user"
    env["JOBPULSE_BACKUP_DIR"] = str(backup_dir)
    env["JOBPULSE_BACKUP_LOG"] = str(log_dir / "postgres_backup.log")
    env["JOBPULSE_VERIFY_DB"] = "jobpulse_restore_test"
    env["FAKE_DOCKER_CALL_LOG"] = str(call_log)

    return {
        "env": env,
        "backup_dir": backup_dir,
        "call_log": call_log,
    }


def run_backup(sandbox, mode="ok"):
    env = dict(sandbox["env"])
    env["FAKE_PGDUMP_MODE"] = mode
    return subprocess.run(
        ["bash", str(SCRIPTS / "backup_postgres_prod.sh")],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def run_verify(sandbox, extra_env=None):
    env = dict(sandbox["env"])
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPTS / "verify_latest_backup.sh")],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def dump_files(sandbox):
    return sorted(p.name for p in sandbox["backup_dir"].iterdir())


@pytest.mark.parametrize("script_path", CHANGED_SCRIPTS, ids=lambda p: p.name)
def test_shell_scripts_pass_bash_syntax_check(script_path):
    result = subprocess.run(
        ["bash", "-n", str(script_path)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_backup_script_has_no_html_entity_corruption():
    for script_path in CHANGED_SCRIPTS:
        text = script_path.read_text()
        assert "&amp;" not in text, f"{script_path.name} contains corrupted HTML entity &amp;"


def test_successful_backup_promotes_dump_with_checksum_and_list(sandbox):
    result = run_backup(sandbox, mode="ok")
    assert result.returncode == 0, result.stderr

    names = dump_files(sandbox)
    assert any(n.endswith(".dump") for n in names)
    assert any(n.endswith(".dump.sha256") for n in names)
    assert any(n.endswith(".dump.list") for n in names)
    assert any(n.endswith(".dump.json") for n in names)
    assert not any(n.endswith(".tmp") for n in names), "no temp files should survive a successful run"

    dump_path = next(p for p in sandbox["backup_dir"].iterdir() if p.name.endswith(".dump"))
    sha_path = Path(str(dump_path) + ".sha256")
    manifest = json.loads(Path(str(dump_path) + ".json").read_text())
    assert manifest["format"] == "pg_dump custom"
    assert sha_path.read_text().split()[0]  # a real checksum was written


def test_corrupted_dump_is_rejected_before_promotion(sandbox):
    result = run_backup(sandbox, mode="corrupt")
    assert result.returncode != 0
    assert dump_files(sandbox) == [], (
        "a dump that fails pg_restore -l validation must not be promoted "
        "to a final filename or checksum"
    )


def test_failed_pg_dump_creates_no_final_backup(sandbox):
    result = run_backup(sandbox, mode="fail")
    assert result.returncode != 0
    assert dump_files(sandbox) == []


def test_empty_pg_dump_output_creates_no_final_backup(sandbox):
    result = run_backup(sandbox, mode="empty")
    assert result.returncode != 0
    assert dump_files(sandbox) == []


def test_failed_validation_does_not_create_a_checksum(sandbox):
    run_backup(sandbox, mode="corrupt")
    assert not any(n.endswith(".sha256") for n in dump_files(sandbox))


def test_verifier_rejects_missing_dump_directory(sandbox):
    result = run_verify(sandbox)
    assert result.returncode != 0
    assert "No .dump backup found" in result.stdout + result.stderr


def test_verifier_ignores_wrong_extension_files(sandbox):
    (sandbox["backup_dir"] / "jobpulse_20260101_000000.sql").write_text("SELECT 1;\n")
    result = run_verify(sandbox)
    assert result.returncode != 0
    assert "No .dump backup found" in result.stdout + result.stderr


def test_verifier_selects_newest_dump_and_restores_with_pg_restore(sandbox):
    run_backup(sandbox, mode="ok")
    older = next(p for p in sandbox["backup_dir"].iterdir() if p.name.endswith(".dump"))

    newer = sandbox["backup_dir"] / "jobpulse_99999999T999999Z.dump"
    newer.write_bytes((MAGIC + "NEWER_FAKE_DUMP").encode())
    os.utime(newer, (older.stat().st_mtime + 100, older.stat().st_mtime + 100))

    sandbox["call_log"].write_text("")
    result = run_verify(sandbox)

    assert result.returncode == 0, result.stderr
    assert str(newer) in result.stdout

    calls = sandbox["call_log"].read_text().splitlines()
    assert "pg_restore" in calls, "restore must use pg_restore for a custom-format dump"


def test_verifier_fails_loudly_on_corrupt_newest_dump(sandbox):
    run_backup(sandbox, mode="ok")
    good = next(p for p in sandbox["backup_dir"].iterdir() if p.name.endswith(".dump"))

    corrupt = sandbox["backup_dir"] / "jobpulse_99999999T999999Z.dump"
    corrupt.write_text("GARBAGE_NOT_A_DUMP")
    os.utime(corrupt, (good.stat().st_mtime + 100, good.stat().st_mtime + 100))

    result = run_verify(sandbox)
    assert result.returncode != 0
    assert "does not appear to be a valid archive" in result.stdout + result.stderr


def test_full_workflow_recovers_after_a_corrupt_backup_attempt(sandbox):
    """A failed backup attempt must never clobber the last known-good backup."""
    first = run_backup(sandbox, mode="ok")
    assert first.returncode == 0
    good_names = set(dump_files(sandbox))

    second = run_backup(sandbox, mode="corrupt")
    assert second.returncode != 0
    assert set(dump_files(sandbox)) == good_names, "last known-good backup must survive a failed attempt"

    verify_result = run_verify(sandbox)
    assert verify_result.returncode == 0, verify_result.stderr


# --- Restore-verification log-file hardening -----------------------------
#
# Before this fix, a hypothetical fixed path such as
# /tmp/jobpulse_restore_test.log could fail under Linux's
# fs.protected_regular behavior if a pre-existing file at that path was
# owned by another user (a real failure seen in production: "Permission
# denied"). The fix replaces the fixed path with a unique file created via
# `mktemp`, restricts its permissions, and always removes it -- and the
# test-database cleanup itself -- through a single EXIT trap.

FIXED_RESTORE_LOG_PATH = "/tmp/jobpulse_restore_test.log"
VERIFY_SCRIPT = SCRIPTS / "verify_latest_backup.sh"


def _make_corrupt_dump_be_the_newest(sandbox):
    run_backup(sandbox, mode="ok")
    good = next(p for p in sandbox["backup_dir"].iterdir() if p.name.endswith(".dump"))
    corrupt = sandbox["backup_dir"] / "jobpulse_99999999T999999Z.dump"
    corrupt.write_text("GARBAGE_NOT_A_DUMP")
    os.utime(corrupt, (good.stat().st_mtime + 100, good.stat().st_mtime + 100))
    return corrupt


def test_verify_script_does_not_reference_fixed_tmp_log_path():
    text = VERIFY_SCRIPT.read_text()
    assert FIXED_RESTORE_LOG_PATH not in text


def test_verify_script_creates_restore_log_with_mktemp():
    text = VERIFY_SCRIPT.read_text()
    assert "mktemp" in text
    assert "RESTORE_LOG" in text


def test_verify_script_traps_database_cleanup_on_exit():
    text = VERIFY_SCRIPT.read_text()
    assert "trap cleanup EXIT" in text


def test_verifier_restore_pipeline_failure_returns_nonzero(sandbox):
    _make_corrupt_dump_be_the_newest(sandbox)
    result = run_verify(sandbox)
    assert result.returncode != 0


def test_verifier_restore_failure_prints_bounded_diagnostic_tail(sandbox):
    _make_corrupt_dump_be_the_newest(sandbox)
    result = run_verify(sandbox)
    combined = result.stdout + result.stderr
    assert "does not appear to be a valid archive" in combined
    assert "restore log" in combined.lower()
    # Bounded: the diagnostic excerpt must be a small, fixed-size tail,
    # not the entire log dumped to the console.
    assert combined.count("does not appear to be a valid archive") <= 2


def test_verifier_removes_temp_restore_log_on_success(sandbox, tmp_path):
    scratch_tmpdir = tmp_path / "script_tmp"
    scratch_tmpdir.mkdir()
    run_backup(sandbox, mode="ok")

    result = run_verify(sandbox, extra_env={"TMPDIR": str(scratch_tmpdir)})

    assert result.returncode == 0, result.stderr
    leftovers = list(scratch_tmpdir.glob("jobpulse_restore_test.*"))
    assert leftovers == [], f"temp restore log(s) left behind: {leftovers}"


def test_verifier_removes_temp_restore_log_on_failure(sandbox, tmp_path):
    scratch_tmpdir = tmp_path / "script_tmp"
    scratch_tmpdir.mkdir()
    _make_corrupt_dump_be_the_newest(sandbox)

    result = run_verify(sandbox, extra_env={"TMPDIR": str(scratch_tmpdir)})

    assert result.returncode != 0
    leftovers = list(scratch_tmpdir.glob("jobpulse_restore_test.*"))
    assert leftovers == [], f"temp restore log(s) left behind after failure: {leftovers}"


def test_verifier_attempts_database_cleanup_after_post_creation_failure(sandbox):
    _make_corrupt_dump_be_the_newest(sandbox)
    sandbox["call_log"].write_text("")

    result = run_verify(sandbox)
    assert result.returncode != 0

    calls = sandbox["call_log"].read_text().splitlines()
    assert "createdb" in calls, "test database must have been created before the restore was attempted"
    createdb_idx = calls.index("createdb")
    trailing_dropdb = [i for i, c in enumerate(calls) if c == "dropdb" and i > createdb_idx]
    assert trailing_dropdb, "cleanup must attempt to drop the test database after a post-creation failure"


def test_verifier_cleanup_failure_does_not_mask_restore_failure(sandbox):
    _make_corrupt_dump_be_the_newest(sandbox)

    result = run_verify(sandbox, extra_env={"FAKE_DROPDB_MODE": "fail"})

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "does not appear to be a valid archive" in combined, (
        "the original restore failure must still be reported"
    )
    assert "warning" in combined.lower() and "drop" in combined.lower(), (
        "a cleanup failure must be reported, not silently swallowed"
    )


def test_verifier_ignores_preexisting_hostile_file_at_fixed_path(sandbox, tmp_path):
    scratch_tmpdir = tmp_path / "script_tmp"
    scratch_tmpdir.mkdir()
    hostile = scratch_tmpdir / "jobpulse_restore_test.log"
    hostile.write_text("PRE-EXISTING HOSTILE CONTENT OWNED BY SOMEONE ELSE")

    run_backup(sandbox, mode="ok")
    result = run_verify(sandbox, extra_env={"TMPDIR": str(scratch_tmpdir)})

    assert result.returncode == 0, result.stderr
    assert hostile.read_text() == "PRE-EXISTING HOSTILE CONTENT OWNED BY SOMEONE ELSE", (
        "the script must never touch a pre-existing file at the old fixed log path"
    )


def test_verifier_ignores_preexisting_hostile_symlink_at_fixed_path(sandbox, tmp_path):
    scratch_tmpdir = tmp_path / "script_tmp"
    scratch_tmpdir.mkdir()
    symlink_target = tmp_path / "should-never-be-created-or-followed"
    hostile_symlink = scratch_tmpdir / "jobpulse_restore_test.log"
    hostile_symlink.symlink_to(symlink_target)

    run_backup(sandbox, mode="ok")
    result = run_verify(sandbox, extra_env={"TMPDIR": str(scratch_tmpdir)})

    assert result.returncode == 0, result.stderr
    assert hostile_symlink.is_symlink()
    assert not symlink_target.exists(), "the script must never write through the hostile symlink's target"
