#!/usr/bin/env python3
"""Smoke-test blf_explain_hive_field_lineage on the live DataHub MCP server."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request


def rpc(method: str, params: dict | None, token: str, rpc_id: int = 1) -> dict:
    payload = {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}}
    req = urllib.request.Request(
        os.environ.get("BLF_DATAHUB_MCP_URL", "http://127.0.0.1:9010/mcp"),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_tool_result(response: dict) -> dict:
    if "error" in response:
        raise RuntimeError(f"RPC error: {response['error']}")
    text = response["result"]["content"][0]["text"]
    match = re.search(r"```json\n(.*)\n```", text, re.DOTALL)
    if not match:
        raise RuntimeError("tool result missing JSON block")
    return json.loads(match.group(1))


def main() -> int:
    token = os.environ.get("BLF_DATAHUB_MCP_TOKEN", "").strip()
    if not token:
        print("ERROR: BLF_DATAHUB_MCP_TOKEN is required", file=sys.stderr)
        return 1

    listed = rpc("tools/list", {}, token, rpc_id=1)
    names = [tool["name"] for tool in listed["result"]["tools"]]
    print(f"tools/list: count={len(names)} has_new_tool={'blf_explain_hive_field_lineage' in names}")
    if "blf_explain_hive_field_lineage" not in names:
        return 2

    table = os.environ.get("TEST_TABLE", "default.dim_store_info")
    fields_raw = os.environ.get("TEST_FIELDS", "store_id")
    fields = [item.strip() for item in fields_raw.split(",") if item.strip()]

    called = rpc(
        "tools/call",
        {
            "name": "blf_explain_hive_field_lineage",
            "arguments": {
                "table": table,
                "fields": fields,
                "max_depth": 30,
                "max_paths": 100,
            },
        },
        token,
        rpc_id=2,
    )
    data = parse_tool_result(called)
    print(f"tools/call: success={data.get('success')} table={data.get('table')}")
    summary = data.get("summary") or {}
    print(
        "summary:",
        json.dumps(
            {
                "field_count": summary.get("field_count"),
                "path_count": summary.get("path_count"),
                "stop_reasons": summary.get("stop_reasons"),
                "sample_nodes": (summary.get("paths") or [{}])[0].get("nodes"),
                "sample_source_layer": (summary.get("paths") or [{}])[0].get("source_layer"),
            },
            ensure_ascii=False,
        ),
    )
    print(f"risks={data.get('risks')}")
    paths = summary.get("paths") or []
    for index, path in enumerate(paths[:3]):
        print(
            f"path[{index}]:",
            json.dumps(
                {
                    "nodes": path.get("nodes"),
                    "stop_reason": path.get("stop_reason"),
                    "source_layer": path.get("source_layer"),
                },
                ensure_ascii=False,
            ),
        )
    if not data.get("success"):
        print("error=", data.get("error"))
        print("full=", json.dumps(data, ensure_ascii=False)[:2000])
        return 3
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code}: {body}", file=sys.stderr)
        raise SystemExit(4) from exc
