#!/usr/bin/env python3
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path("/opt/jobpulse")
BACKUP_DIR = Path(os.getenv("JOBPULSE_BACKUP_DIR", ROOT / "backups/postgres"))
BACKUP_LOG = Path(os.getenv("JOBPULSE_BACKUP_LOG", ROOT / "logs/postgres_backup.log"))
VERIFY_LOG = Path(os.getenv("JOBPULSE_RESTORE_VERIFY_LOG", ROOT / "logs/postgres_restore_verify.log"))
STATUS_FILE = Path(os.getenv("JOBPULSE_BACKUP_STATUS_FILE", ROOT / "logs/postgres_backup_status.json"))

MAX_BACKUP_AGE_HOURS = float(os.getenv("JOBPULSE_MAX_BACKUP_AGE_HOURS", "30"))
MAX_VERIFY_AGE_HOURS = float(os.getenv("JOBPULSE_MAX_VERIFY_AGE_HOURS", "190"))
MAX_TOTAL_BACKUP_SIZE_GB = float(os.getenv("JOBPULSE_MAX_TOTAL_BACKUP_SIZE_GB", "5"))


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        if key and key not in os.environ:
            os.environ[key] = value


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def file_age_hours(path: Path) -> float:
    return max(0.0, (time.time() - path.stat().st_mtime) / 3600)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def backup_inventory() -> dict:
    dumps = sorted(BACKUP_DIR.glob("*.dump"), key=lambda p: p.stat().st_mtime)
    total_size = sum(p.stat().st_size for p in dumps)

    latest_files = []
    for p in reversed(dumps[-5:]):
        latest_files.append({
            "name": p.name,
            "path": str(p),
            "size_bytes": p.stat().st_size,
            "age_hours": round(file_age_hours(p), 2),
            "mtime_utc": datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat(),
            "sha256_exists": Path(str(p) + ".sha256").exists(),
            "list_exists": Path(str(p) + ".list").exists(),
            "manifest_exists": Path(str(p) + ".json").exists(),
        })

    return {
        "backup_count": len(dumps),
        "total_backup_size_bytes": total_size,
        "total_backup_size_gb": round(total_size / 1024 / 1024 / 1024, 3),
        "oldest_backup": dumps[0].name if dumps else None,
        "newest_backup": dumps[-1].name if dumps else None,
        "oldest_backup_age_hours": round(file_age_hours(dumps[0]), 2) if dumps else None,
        "newest_backup_age_hours": round(file_age_hours(dumps[-1]), 2) if dumps else None,
        "latest_files": latest_files,
    }


def latest_dump() -> Path | None:
    dumps = sorted(BACKUP_DIR.glob("*.dump"), key=lambda p: p.stat().st_mtime, reverse=True)
    return dumps[0] if dumps else None


def read_last_ok_time(log_path: Path, ok_marker: str) -> str | None:
    if not log_path.exists():
        return None

    last = None
    for line in log_path.read_text(errors="ignore").splitlines():
        if ok_marker in line:
            match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", line)
            if match:
                last = match.group(1)

    return last


def age_hours_from_log_time(value: str | None) -> float | None:
    if not value:
        return None

    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None

    return max(0.0, (utc_now() - dt).total_seconds() / 3600)


def send_telegram_alert(message: str) -> bool:
    load_env_file(ROOT / ".telegram_alert.env")

    token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN_PROD")
    chat_id = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_ALERT_CHAT_ID")

    if not token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()

    try:
        req = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def main() -> int:
    errors = []
    warnings = []

    inventory = backup_inventory()
    dump = latest_dump()

    status = {
        "checked_at_utc": utc_now().isoformat(),
        "ok": False,
        "backup_dir": str(BACKUP_DIR),
        "inventory": inventory,
        "backup_count": inventory["backup_count"],
        "total_backup_size_bytes": inventory["total_backup_size_bytes"],
        "total_backup_size_gb": inventory["total_backup_size_gb"],
        "oldest_backup": inventory["oldest_backup"],
        "newest_backup": inventory["newest_backup"],
        "latest_backup": None,
        "latest_backup_age_hours": None,
        "latest_backup_size_bytes": None,
        "latest_backup_sha256_ok": None,
        "last_backup_ok_at": read_last_ok_time(BACKUP_LOG, "backup_ok"),
        "last_restore_verify_ok_at": read_last_ok_time(VERIFY_LOG, "restore_verify_ok"),
        "max_backup_age_hours": MAX_BACKUP_AGE_HOURS,
        "max_verify_age_hours": MAX_VERIFY_AGE_HOURS,
        "max_total_backup_size_gb": MAX_TOTAL_BACKUP_SIZE_GB,
        "warnings": warnings,
        "errors": errors,
    }

    if inventory["total_backup_size_gb"] > MAX_TOTAL_BACKUP_SIZE_GB:
        warnings.append(
            f"Total backup size is high: {inventory['total_backup_size_gb']}GB > {MAX_TOTAL_BACKUP_SIZE_GB}GB"
        )

    if not dump:
        errors.append(f"No .dump backup found in {BACKUP_DIR}")
    else:
        age = file_age_hours(dump)
        size = dump.stat().st_size
        sha_file = Path(str(dump) + ".sha256")

        status["latest_backup"] = str(dump)
        status["latest_backup_age_hours"] = round(age, 2)
        status["latest_backup_size_bytes"] = size

        if size <= 0:
            errors.append(f"Latest backup is empty: {dump}")

        if age > MAX_BACKUP_AGE_HOURS:
            errors.append(f"Latest backup is too old: {age:.1f}h > {MAX_BACKUP_AGE_HOURS:.1f}h")

        if not sha_file.exists():
            errors.append(f"Missing sha256 file: {sha_file}")
            status["latest_backup_sha256_ok"] = False
        else:
            expected = sha_file.read_text(errors="ignore").split()[0].strip()
            actual = sha256_file(dump)
            sha_ok = bool(expected and expected == actual)
            status["latest_backup_sha256_ok"] = sha_ok

            if not sha_ok:
                errors.append(f"sha256 mismatch for latest backup: {dump}")

    verify_age = age_hours_from_log_time(status["last_restore_verify_ok_at"])
    status["last_restore_verify_ok_age_hours"] = round(verify_age, 2) if verify_age is not None else None

    if verify_age is None:
        errors.append("No successful restore verification found")
    elif verify_age > MAX_VERIFY_AGE_HOURS:
        errors.append(f"Restore verification is too old: {verify_age:.1f}h > {MAX_VERIFY_AGE_HOURS:.1f}h")

    status["ok"] = not errors

    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(status, indent=2, sort_keys=True))

    print(json.dumps(status, indent=2, sort_keys=True))

    if errors:
        message = (
            "🚨 <b>JobPulse PostgreSQL backup alert</b>\n"
            + "\n".join(f"• {e}" for e in errors)
            + f"\n\nChecked: {status['checked_at_utc']}"
        )
        sent = send_telegram_alert(message)
        status["telegram_alert_sent"] = sent
        STATUS_FILE.write_text(json.dumps(status, indent=2, sort_keys=True))
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
