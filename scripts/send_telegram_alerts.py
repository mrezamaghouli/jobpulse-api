import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ENV_PATH = Path("/opt/jobpulse/.telegram_alert.env")
STATE_PATH = Path("/opt/jobpulse/logs/telegram_alert_state.json")


def load_env_file(path: Path):
    if not path.exists():
        return

    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def http_json(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=20) as res:
        return json.loads(res.read().decode("utf-8"))


def send_telegram(token: str, chat_id: str, message: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")

    req = urllib.request.Request(url, data=data, method="POST")

    with urllib.request.urlopen(req, timeout=20) as res:
        return json.loads(res.read().decode("utf-8"))


def load_state():
    if not STATE_PATH.exists():
        return {}

    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def main():
    load_env_file(ENV_PATH)

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    admin_url = os.getenv("ADMIN_STATUS_URL", "http://127.0.0.1:8000/api/admin/status").strip()
    admin_token = Path("/opt/jobpulse/.admin_token").read_text().strip()

    if not token or not chat_id:
        print("Telegram is not configured. Fill TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        return 0

    try:
        data = http_json(admin_url, headers={"X-Admin-Token": admin_token})
    except Exception as exc:
        data = {
            "status": "error",
            "database": "unknown",
            "alerts": [{
                "level": "critical",
                "code": "admin_status_unreachable",
                "message": f"Admin status endpoint failed: {exc}",
            }],
        }

    alerts = data.get("alerts") or []
    state = load_state()

    current_codes = sorted(f"{a.get('level')}:{a.get('code')}" for a in alerts)
    last_codes = state.get("last_codes", [])

    if current_codes == last_codes:
        print("No alert state change.")
        return 0

    state["last_codes"] = current_codes
    save_state(state)

    if not alerts:
        message = (
            "✅ <b>JobPulse recovered</b>\n"
            "All checks passed.\n\n"
            f"Total jobs: {data.get('jobs', {}).get('total_jobs', '-')}\n"
            f"Jobs seen 1h: {data.get('jobs', {}).get('jobs_seen_last_hour', '-')}\n"
            f"Bad apply: {data.get('bad_apply', {}).get('bad_external_apply_count', '-')}\n"
            f"Disk used: {data.get('disk', {}).get('used_percent', '-')}% "
            f"Free: {data.get('disk', {}).get('free_gb', '-')} GB"
        )
    else:
        lines = ["🚨 <b>JobPulse Alert</b>", ""]

        for item in alerts:
            level = item.get("level", "warning")
            code = item.get("code", "unknown")
            msg = item.get("message", "")
            emoji = "🚨" if level == "critical" else "⚠️"
            lines.append(f"{emoji} <b>{code}</b>: {msg}")

        lines.append("")
        lines.append(f"Total jobs: {data.get('jobs', {}).get('total_jobs', '-')}")
        lines.append(f"Jobs seen 1h: {data.get('jobs', {}).get('jobs_seen_last_hour', '-')}")
        lines.append(f"Bad apply: {data.get('bad_apply', {}).get('bad_external_apply_count', '-')}")
        lines.append(
            f"Disk used: {data.get('disk', {}).get('used_percent', '-')}% "
            f"Free: {data.get('disk', {}).get('free_gb', '-')} GB"
        )

        message = "\n".join(lines)

    send_telegram(token, chat_id, message)
    print("Telegram alert sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
