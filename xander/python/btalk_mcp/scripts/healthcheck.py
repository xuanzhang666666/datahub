#!/usr/bin/env python3
"""Check btalk-mcp login health and restart once on failure."""

import json
import subprocess
import time


CONTAINER = "btalk-mcp"


def run(args, timeout=30):
    return subprocess.run(args, check=False, text=True, capture_output=True, timeout=timeout)


def get_status():
    proc = run(["docker", "exec", CONTAINER, "btalk", "status"])
    if proc.returncode != 0:
        return None, {"rc": proc.returncode, "stderr": proc.stderr.strip()[-500:]}
    try:
        return json.loads(proc.stdout), None
    except json.JSONDecodeError as exc:
        return None, {"rc": proc.returncode, "parse_error": str(exc), "stdout": proc.stdout[-500:]}


def is_healthy(status):
    return bool(status and status.get("loggedIn") is True and status.get("state") == "ready")


def main():
    before, before_error = get_status()
    result = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "healthy_before": is_healthy(before),
        "status_before": before,
        "error_before": before_error,
        "restarted": False,
    }

    if result["healthy_before"]:
        print(json.dumps(result, ensure_ascii=False))
        return 0

    restart = run(["docker", "restart", CONTAINER], timeout=60)
    result["restarted"] = restart.returncode == 0
    result["restart_rc"] = restart.returncode
    result["restart_stderr"] = restart.stderr.strip()[-500:]
    time.sleep(12)

    after, after_error = get_status()
    result["healthy_after"] = is_healthy(after)
    result["status_after"] = after
    result["error_after"] = after_error
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["healthy_after"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
