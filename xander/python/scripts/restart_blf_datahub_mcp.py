#!/usr/bin/env python3
"""Restart BLF DataHub MCP on neo4j2."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path


BASE = Path("/data/datahub/scripts")
PID_FILE = BASE / "blf_datahub_mcp.pid"
LOG_FILE = BASE / "blf_datahub_mcp.log"


def main() -> int:
    ps_output = subprocess.run(
        ["ps", "-ef"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    for line in ps_output.splitlines():
        if "python -m blf_datahub_mcp.server" not in line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            os.kill(int(parts[1]), signal.SIGTERM)
        except ProcessLookupError:
            pass
    time.sleep(1)
    with LOG_FILE.open("ab") as log_file:
        proc = subprocess.Popen(
            ["./run_blf_datahub_mcp.sh"],
            cwd=str(BASE),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    PID_FILE.write_text(str(proc.pid))
    time.sleep(2)
    print(f"pid={proc.pid}")
    print(f"running={proc.poll() is None}")
    return 0 if proc.poll() is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
