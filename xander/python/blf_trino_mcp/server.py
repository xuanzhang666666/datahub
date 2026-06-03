"""HTTP MCP server for BLF Trino Hive query tools."""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .tools import (
    get_hive_table_ddl,
    get_hive_table_partitions,
    query_hive_by_natural_language,
    query_hive_sql,
    query_hive_sql_fragment,
)
from .trino_client import TrinoClient, TrinoConfig

logger = logging.getLogger("blf_trino_mcp")

JSON = dict[str, Any]
ToolHandler = Callable[..., JSON]


TOOL_SPECS: dict[str, dict[str, Any]] = {
    "blf_trino_get_hive_table_ddl": {
        "description": "通过 Trino 查询 Hive 表 DDL，即 SHOW CREATE TABLE 结果。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Hive 表名，支持 db.table 或 table；未写库名时默认使用 default 库。",
                },
                "raw_ddl": {
                    "type": "boolean",
                    "default": False,
                    "description": "是否返回 Trino 原始 DDL；默认 false，会把 COMMENT U& 编码解码成中文。",
                },
            },
            "required": ["table"],
        },
    },
    "blf_trino_get_hive_table_partitions": {
        "description": "通过 Trino Hive $partitions 虚拟表查询 Hive 表分区。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Hive 表名，支持 db.table 或 table；未写库名时默认使用 default 库。",
                },
                "partition_column": {
                    "type": "string",
                    "default": "dt",
                    "description": "用于排序的分区字段名，默认 dt；非标准分区表可改成 date_dt、record_date 等。",
                },
                "where": {
                    "type": "string",
                    "default": "",
                    "description": "可选分区过滤条件，不要写 WHERE 关键字，例如 dt >= '20260401'。",
                },
                "limit": {
                    "type": "integer",
                    "default": 200,
                    "description": "最多返回多少个分区，默认 200，MCP 最大 100000；实际返回量仍可能受响应大小和超时影响。",
                },
            },
            "required": ["table"],
        },
    },
    "blf_trino_query_hive_sql": {
        "description": "执行用户提供的只读 Trino SQL，并返回查询结果。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "只读 SQL，只允许 SELECT/WITH/SHOW/DESCRIBE/EXPLAIN 单语句。",
                },
                "row_limit": {
                    "type": "integer",
                    "default": 100,
                    "description": "最多返回多少行；SELECT/WITH 没有 LIMIT 时会自动追加 LIMIT，MCP 最大 100000。",
                },
            },
            "required": ["sql"],
        },
    },
    "blf_trino_query_hive_sql_fragment": {
        "description": "执行用户提供的 SQL 片段；完整 SQL 直接执行，条件片段会拼成 SELECT * FROM table WHERE ...。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql_fragment": {
                    "type": "string",
                    "description": "SQL 片段或完整只读 SQL。若只是条件片段，例如 dt='20260601'，必须同时传 table。",
                },
                "table": {
                    "type": "string",
                    "default": "",
                    "description": "当 sql_fragment 不是完整 SQL 时使用的 Hive 表名。",
                },
                "row_limit": {
                    "type": "integer",
                    "default": 100,
                    "description": "最多返回多少行；SELECT/WITH 没有 LIMIT 时会自动追加 LIMIT，MCP 最大 100000。",
                },
            },
            "required": ["sql_fragment"],
        },
    },
    "blf_trino_query_hive_by_natural_language": {
        "description": "根据用户自然语言查询 Hive 数据；推荐由调用方 Agent 先生成 generated_sql，再由 MCP 只读执行。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "用户的自然语言问题。",
                },
                "table": {
                    "type": "string",
                    "default": "",
                    "description": "问题涉及的 Hive 表名；未写库名时默认使用 default 库。",
                },
                "generated_sql": {
                    "type": "string",
                    "default": "",
                    "description": "调用方 Agent 根据自然语言生成的只读 SQL；传入后 MCP 直接校验并执行。",
                },
                "row_limit": {
                    "type": "integer",
                    "default": 100,
                    "description": "最多返回多少行；SELECT/WITH 没有 LIMIT 时会自动追加 LIMIT，MCP 最大 100000。",
                },
            },
            "required": ["question"],
        },
    },
}


def load_env_file(path: str) -> None:
    """Load KEY=VALUE lines into os.environ without overwriting existing values."""
    if not path or not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as env_file:
        for line in env_file:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            if stripped.startswith("export "):
                stripped = stripped[len("export ") :]
            key, value = stripped.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


class BlfTrinoMcpApplication:
    """Stateful MCP JSON-RPC application."""

    def __init__(self, *, trino_client: TrinoClient, mcp_token: str | None) -> None:
        self.trino_client = trino_client
        self.mcp_token = mcp_token
        self.handlers: dict[str, ToolHandler] = {
            "blf_trino_get_hive_table_ddl": get_hive_table_ddl,
            "blf_trino_get_hive_table_partitions": get_hive_table_partitions,
            "blf_trino_query_hive_sql": query_hive_sql,
            "blf_trino_query_hive_sql_fragment": query_hive_sql_fragment,
            "blf_trino_query_hive_by_natural_language": query_hive_by_natural_language,
        }

    def authorized(self, header_value: str | None) -> bool:
        if not self.mcp_token:
            return True
        expected = f"Bearer {self.mcp_token}"
        return secrets.compare_digest(header_value or "", expected)

    def handle_rpc(self, payload: JSON) -> JSON | None:
        method = payload.get("method")
        rpc_id = payload.get("id")
        if method == "notifications/initialized":
            return None
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "blf-trino-mcp", "version": "0.1.0"},
                }
            elif method == "tools/list":
                result = {
                    "tools": [
                        {"name": name, **spec} for name, spec in TOOL_SPECS.items()
                    ]
                }
            elif method == "tools/call":
                result = self._call_tool(payload.get("params") or {})
            else:
                return self._rpc_error(rpc_id, -32601, f"Unknown method: {method}")
            return {"jsonrpc": "2.0", "id": rpc_id, "result": result}
        except Exception as exc:
            logger.exception("MCP request failed for method %s", method)
            return self._rpc_error(rpc_id, -32000, str(exc))

    def _call_tool(self, params: JSON) -> JSON:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or name not in self.handlers:
            raise ValueError(f"Unknown tool: {name}")
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        result = self.handlers[name](self.trino_client, **arguments)
        return {
            "content": [
                {
                    "type": "text",
                    "text": _format_tool_text(result),
                }
            ],
            "isError": not bool(result.get("success", True)),
        }

    @staticmethod
    def _rpc_error(rpc_id: Any, code: int, message: str) -> JSON:
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": code, "message": message},
        }


def _format_tool_text(result: JSON) -> str:
    """Format tool output as text so Dify exposes it via the text field."""
    return (
        "BLF Trino MCP 工具返回如下。请按其中 JSON 证据回答用户，不要编造未返回的信息。\n\n"
        "```json\n"
        f"{json.dumps(result, ensure_ascii=False, indent=2)}\n"
        "```"
    )


def make_handler(app: BlfTrinoMcpApplication) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "BlfTrinoMcp/0.1"

        def do_GET(self) -> None:
            if self.path.rstrip("/") == "/health":
                self._write_json({"ok": True})
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            if self.path.rstrip("/") != "/mcp":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not app.authorized(self.headers.get("Authorization")):
                self._write_json(
                    {"error": "unauthorized"},
                    status=HTTPStatus.UNAUTHORIZED,
                )
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                payload = json.loads(raw)
            except Exception:
                self._write_json(
                    BlfTrinoMcpApplication._rpc_error(None, -32700, "Invalid JSON"),
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            if isinstance(payload, list):
                responses = [app.handle_rpc(item) for item in payload]
                responses = [item for item in responses if item is not None]
                self._write_json(responses)
                return
            response = app.handle_rpc(payload)
            if response is None:
                self.send_response(HTTPStatus.ACCEPTED)
                self.end_headers()
                return
            self._write_json(response)

        def log_message(self, format: str, *args: Any) -> None:
            logger.info("%s - %s", self.address_string(), format % args)

        def _write_json(
            self,
            payload: Any,
            *,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def build_app() -> BlfTrinoMcpApplication:
    env_file = os.getenv("BLF_TRINO_MCP_ENV_FILE", "/data/datahub/scripts/lineage.env")
    load_env_file(env_file)
    config = TrinoConfig(
        host=os.getenv("TRINO_HOST", TrinoConfig.host),
        port=int(os.getenv("TRINO_PORT", str(TrinoConfig.port))),
        user=os.getenv("TRINO_USER", TrinoConfig.user),
        catalog=os.getenv("TRINO_CATALOG", TrinoConfig.catalog),
        schema=os.getenv("TRINO_SCHEMA", TrinoConfig.schema),
    )
    token = os.getenv("BLF_TRINO_MCP_TOKEN") or os.getenv("BLF_DATAHUB_MCP_TOKEN")
    return BlfTrinoMcpApplication(
        trino_client=TrinoClient(config),
        mcp_token=token,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run BLF Trino MCP server")
    parser.add_argument("--host", default=os.getenv("BLF_TRINO_MCP_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("BLF_TRINO_MCP_PORT", "9011")),
    )
    parser.add_argument("--log-level", default=os.getenv("BLF_TRINO_MCP_LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = build_app()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    logger.info("Starting BLF Trino MCP server on %s:%s", args.host, args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
