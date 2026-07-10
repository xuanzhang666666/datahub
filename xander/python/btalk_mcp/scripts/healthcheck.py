#!/usr/bin/env python3
"""Monitor btalk-mcp readiness, message freshness, and native database errors."""

import json
import os
import subprocess
import time
from typing import Any


CONTAINER = os.environ.get("BTALK_MCP_CONTAINER", "btalk-mcp")
ALERT_GROUP_ID = os.environ.get(
    "BTALK_ALERT_GROUP_ID", "baa2330062165177b7ede2ec107d0593"
)
STATE_FILE = os.environ.get(
    "BTALK_MONITOR_STATE", "/root/btalk_backups/btalk_monitor_state.json"
)
APP_LOG = os.environ.get(
    "BTALK_APP_LOG", "/root/.btalk/prod/logs/btalk/app.log"
)
STALE_MINUTES = int(os.environ.get("BTALK_STALE_MINUTES", "30"))
FAILURE_THRESHOLD = 2
SESSIONS_PROBE_SCRIPT = """
const client = require('/usr/local/lib/node_modules/@wnpm/btalk-cli/src/cli/client');
client.request({cmd: 'sessions'}).then((response) => {
  const data = response.data || {};
  const sessions = [...(data.normal || []), ...(data.top || [])];
  const latest = Math.max(...sessions.map((item) => Number(item.lastMsgTime) || 0), 0);
  process.stdout.write(JSON.stringify({
    ok: response.ok === true,
    session_count: sessions.length,
    latest_message_time: latest,
    error: response.error || null,
  }));
}).catch((error) => {
  process.stderr.write(error.message || String(error));
  process.exit(1);
});
"""


def run(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=False, text=True, capture_output=True, timeout=timeout)


def get_status() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    proc = run(["docker", "exec", CONTAINER, "btalk", "status"])
    if proc.returncode != 0:
        return None, {"rc": proc.returncode, "stderr": proc.stderr.strip()[-500:]}
    try:
        return json.loads(proc.stdout), None
    except json.JSONDecodeError as exc:
        return None, {"rc": proc.returncode, "parse_error": str(exc), "stdout": proc.stdout[-500:]}


def get_sessions() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    proc = run(["docker", "exec", CONTAINER, "node", "-e", SESSIONS_PROBE_SCRIPT], timeout=60)
    if proc.returncode != 0:
        return None, {"rc": proc.returncode, "stderr": proc.stderr.strip()[-500:]}
    try:
        value = json.loads(proc.stdout)
        if value.get("ok") is not True:
            return None, {"rc": proc.returncode, "error": value.get("error", "sessions request failed")}
        return value, None
    except json.JSONDecodeError as exc:
        return None, {"rc": proc.returncode, "parse_error": str(exc)}


def get_database_crash_count() -> tuple[int | None, dict[str, Any] | None]:
    proc = run(["docker", "exec", CONTAINER, "grep", "-c", "dataBaseCrash", APP_LOG])
    output = proc.stdout.strip()
    if output.isdigit():
        return int(output), None
    return None, {"rc": proc.returncode, "stderr": proc.stderr.strip()[-500:]}


def is_healthy(status: dict[str, Any] | None) -> bool:
    return bool(status and status.get("loggedIn") is True and status.get("state") == "ready")


def latest_message_time(sessions: dict[str, Any] | None) -> int:
    if not sessions:
        return 0
    if "latest_message_time" in sessions:
        return int(sessions.get("latest_message_time", 0) or 0)
    values = [
        int(item.get("lastMsgTime", 0) or 0)
        for item in sessions.get("sessions", [])
        if isinstance(item, dict)
    ]
    return max(values, default=0)


def stale_message_issue(
    latest_message_time_ms: int, now_ms: int | None = None, stale_minutes: int = STALE_MINUTES
) -> dict[str, Any] | None:
    if latest_message_time_ms <= 0:
        return None
    now_ms = now_ms or int(time.time() * 1000)
    age_minutes = (now_ms - latest_message_time_ms) / 60_000
    if age_minutes < stale_minutes:
        return None
    return {
        "code": "message_stale",
        "signature": f"message_stale:{latest_message_time_ms}",
        "detail": f"最新消息已 {age_minutes:.0f} 分钟没有更新",
    }


def should_alert(issues: list[dict[str, Any]], consecutive_failures: int) -> bool:
    if not issues:
        return False
    immediate_codes = {"database_crash", "message_stale"}
    return consecutive_failures >= FAILURE_THRESHOLD or any(
        issue.get("code") in immediate_codes for issue in issues
    )


def load_state() -> dict[str, Any]:
    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            value = json.load(handle)
            return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    directory = os.path.dirname(STATE_FILE)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = f"{STATE_FILE}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False)
    os.replace(temporary, STATE_FILE)


def send_alert(issues: list[dict[str, Any]], latest_time: int) -> dict[str, Any]:
    details = "；".join(issue.get("detail", issue.get("code", "unknown")) for issue in issues)
    latest_display = (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(latest_time / 1000))
        if latest_time
        else "未知"
    )
    message = f"[btalk-mcp监控] 消息链路异常：{details}。最新消息时间：{latest_display}。"
    proc = run(["docker", "exec", CONTAINER, "btalk", "send", ALERT_GROUP_ID, message])
    return {"sent": proc.returncode == 0, "rc": proc.returncode, "stderr": proc.stderr.strip()[-500:]}


def collect_health() -> dict[str, Any]:
    status, status_error = get_status()
    sessions, sessions_error = get_sessions()
    crash_count, crash_error = get_database_crash_count()
    latest_time = latest_message_time(sessions)
    issues: list[dict[str, Any]] = []

    if not is_healthy(status):
        issues.append({"code": "status", "signature": "status", "detail": "daemon 未处于 ready 或未登录"})
    if sessions_error:
        issues.append({"code": "sessions", "signature": "sessions", "detail": "会话列表查询失败"})
    if crash_error:
        issues.append({"code": "app_log", "signature": "app_log", "detail": "无法读取 SDK 业务日志"})
    elif crash_count:
        issues.append({"code": "database_crash", "signature": f"database_crash:{crash_count}", "detail": f"app.log 检测到 {crash_count} 次 dataBaseCrash"})
    stale = stale_message_issue(latest_time)
    if stale:
        issues.append(stale)

    return {
        "status": status,
        "status_error": status_error,
        "session_count": int((sessions or {}).get("session_count", 0) or 0),
        "sessions_error": sessions_error,
        "database_crash_count": crash_count,
        "latest_message_time": latest_time,
        "issues": issues,
    }


def main() -> int:
    state = load_state()
    health = collect_health()
    issues = health["issues"]
    previous_failures = int(state.get("consecutive_failures", 0) or 0)
    consecutive_failures = previous_failures + 1 if issues else 0
    alert_signature = "|".join(issue["signature"] for issue in issues)
    result: dict[str, Any] = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        **health,
        "consecutive_failures": consecutive_failures,
        "restarted": False,
        "alert": None,
    }

    if should_alert(issues, consecutive_failures) and alert_signature != state.get("last_alert_signature"):
        result["alert"] = send_alert(issues, health["latest_message_time"])
        if result["alert"]["sent"]:
            state["last_alert_signature"] = alert_signature

    if any(issue["code"] == "status" for issue in issues) and consecutive_failures >= FAILURE_THRESHOLD:
        restart = run(["docker", "restart", CONTAINER], timeout=60)
        result["restarted"] = restart.returncode == 0
        result["restart_rc"] = restart.returncode
        result["restart_stderr"] = restart.stderr.strip()[-500:]
        time.sleep(12)
        after, after_error = get_status()
        result["healthy_after"] = is_healthy(after)
        result["status_after"] = after
        result["error_after"] = after_error

    state["consecutive_failures"] = consecutive_failures
    state["last_latest_message_time"] = health["latest_message_time"]
    save_state(state)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
