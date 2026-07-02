#!/usr/bin/env python3
"""Keep btalk-mcp's optional search indexes small.

The MCP no longer exposes message_search, so search/presearch SQLite files are
treated as disposable cache. This script removes them when they exceed a size
threshold and restarts the container so SQLite files are not deleted while open.
"""

import glob
import json
import os
import subprocess
import sys
import time


CONTAINER = os.environ.get("BTALK_MCP_CONTAINER", "btalk-mcp")
THRESHOLD_BYTES = int(os.environ.get("BTALK_SEARCH_MAX_BYTES", str(512 * 1024 * 1024)))
PATTERNS = [
    "/root/.btalk/prod/databases/xuan.zhang.search.db*",
    "/root/.btalk/prod/databases/xuan.zhang.presearch.db*",
]


def run(args):
    return subprocess.run(args, check=False, text=True, capture_output=True)


def collect_files():
    files = []
    for pattern in PATTERNS:
        files.extend(glob.glob(pattern))
    return sorted(set(files))


def main():
    force = "--force" in sys.argv
    files = collect_files()
    total = sum(os.path.getsize(path) for path in files if os.path.exists(path))
    result = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "threshold_bytes": THRESHOLD_BYTES,
        "search_bytes": total,
        "file_count": len(files),
        "removed": [],
        "restarted": False,
    }

    if not force and total <= THRESHOLD_BYTES:
        print(json.dumps(result, ensure_ascii=False))
        return 0

    stop = run(["docker", "stop", CONTAINER])
    result["stop_rc"] = stop.returncode
    result["stop_stderr"] = stop.stderr.strip()[-500:]

    for path in files:
        try:
            size = os.path.getsize(path)
            os.unlink(path)
            result["removed"].append({"path": path, "bytes": size})
        except FileNotFoundError:
            pass

    start = run(["docker", "start", CONTAINER])
    result["start_rc"] = start.returncode
    result["start_stderr"] = start.stderr.strip()[-500:]
    result["restarted"] = start.returncode == 0

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["restarted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
