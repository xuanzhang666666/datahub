"""HTTP MCP server for BLF DataHub Hive metadata tools."""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .datahub_client import DataHubClient
from .hive import DEFAULT_PUBLIC_BASE_URL
from .tools import (
    audit_hive_table,
    explain_hive_table_context,
    get_hive_etl_context,
    get_hive_lineage,
    get_hive_table_profile,
    search_hive_assets,
)

logger = logging.getLogger("blf_datahub_mcp")

JSON = dict[str, Any]
ToolHandler = Callable[..., JSON]


TOOL_SPECS: dict[str, dict[str, Any]] = {
    "blf_get_hive_table_profile": {
        "description": "Get BLF Hive table profile from DataHub metadata.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table": {"type": "string"},
                "include_fields": {"type": "boolean", "default": True},
                "field_limit": {"type": "integer", "default": 80},
            },
            "required": ["table"],
        },
    },
    "blf_get_hive_etl_context": {
        "description": "Get Execute Shell and Etl Script structured properties for a BLF Hive table.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table": {"type": "string"},
                "max_script_chars": {"type": "integer", "default": 8000},
            },
            "required": ["table"],
        },
    },
    "blf_get_hive_lineage": {
        "description": "Get table-level upstream/downstream DataHub lineage for a BLF Hive table.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table": {"type": "string"},
                "direction": {
                    "type": "string",
                    "enum": ["upstream", "downstream", "both"],
                    "default": "both",
                },
                "max_hops": {"type": "integer", "default": 1},
                "max_results": {"type": "integer", "default": 50},
            },
            "required": ["table"],
        },
    },
    "blf_search_hive_assets": {
        "description": "Search BLF Hive datasets in DataHub and return compact recommendations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "only_available": {"type": "boolean", "default": True},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
    "blf_audit_hive_table": {
        "description": "Audit DataHub metadata completeness for one BLF Hive table.",
        "inputSchema": {
            "type": "object",
            "properties": {"table": {"type": "string"}},
            "required": ["table"],
        },
    },
    "blf_explain_hive_table_context": {
        "description": "Return profile, ETL, lineage, and risk context for one BLF Hive table.",
        "inputSchema": {
            "type": "object",
            "properties": {"table": {"type": "string"}},
            "required": ["table"],
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
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            os.environ.setdefault(key, value)


class BlfMcpApplication:
    """Stateful MCP JSON-RPC application."""

    def __init__(
        self,
        *,
        datahub_client: DataHubClient,
        public_base_url: str,
        mcp_token: str | None,
    ) -> None:
        self.datahub_client = datahub_client
        self.public_base_url = public_base_url
        self.mcp_token = mcp_token
        self.handlers: dict[str, ToolHandler] = {
            "blf_get_hive_table_profile": get_hive_table_profile,
            "blf_get_hive_etl_context": get_hive_etl_context,
            "blf_get_hive_lineage": get_hive_lineage,
            "blf_search_hive_assets": search_hive_assets,
            "blf_audit_hive_table": audit_hive_table,
            "blf_explain_hive_table_context": explain_hive_table_context,
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
                    "serverInfo": {"name": "blf-datahub-hive-mcp", "version": "0.1.0"},
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
        result = self.handlers[name](
            self.datahub_client,
            public_base_url=self.public_base_url,
            **arguments,
        )
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, ensure_ascii=False, indent=2),
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


def make_handler(app: BlfMcpApplication) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "BlfDataHubMcp/0.1"

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
                    BlfMcpApplication._rpc_error(None, -32700, "Invalid JSON"),
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


def build_app() -> BlfMcpApplication:
    env_file = os.getenv("BLF_DATAHUB_MCP_ENV_FILE", "/data/datahub/scripts/lineage.env")
    load_env_file(env_file)
    gms_url = os.getenv("DATAHUB_GMS_URL", "http://localhost:8080")
    token = os.getenv("DATAHUB_GMS_TOKEN")
    public_base_url = os.getenv("DATAHUB_PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL)
    mcp_token = os.getenv("BLF_DATAHUB_MCP_TOKEN")
    return BlfMcpApplication(
        datahub_client=DataHubClient(gms_url=gms_url, token=token),
        public_base_url=public_base_url,
        mcp_token=mcp_token,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run BLF DataHub Hive MCP server")
    parser.add_argument("--host", default=os.getenv("BLF_DATAHUB_MCP_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("BLF_DATAHUB_MCP_PORT", "9010")),
    )
    parser.add_argument("--log-level", default=os.getenv("BLF_DATAHUB_MCP_LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = build_app()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    logger.info("Starting BLF DataHub MCP server on %s:%s", args.host, args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()

