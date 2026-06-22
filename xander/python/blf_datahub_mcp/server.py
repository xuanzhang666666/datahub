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
    explain_hive_field_lineage,
    explain_hive_table_context,
    explain_schedule_job_context,
    get_hive_data_availability_flag,
    get_hive_etl_script,
    get_hive_etl_context,
    get_hive_execute_shell,
    get_hive_lineage,
    get_hive_other_remark,
    get_hive_schedule_url,
    get_hive_structured_properties,
    get_hive_structured_property,
    get_hive_table_profile,
    get_schedule_job_content_xml,
    get_schedule_job_execute_shell,
    get_schedule_job_lineage,
    get_schedule_job_profile,
    search_hive_assets,
    search_schedule_jobs,
)

logger = logging.getLogger("blf_datahub_mcp")

JSON = dict[str, Any]
ToolHandler = Callable[..., JSON]


TOOL_SPECS: dict[str, dict[str, Any]] = {
    "blf_get_hive_table_profile": {
        "description": "从 DataHub 读取 BLF Hive 表画像，包括表说明、字段、owner、标签、结构化属性和治理缺口。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Hive 表名，支持 db.table 或 table；未写库名时默认使用 default 库。",
                },
                "include_fields": {
                    "type": "boolean",
                    "default": True,
                    "description": "是否返回字段列表和字段说明；表很宽时可设为 false 只看表级信息。",
                },
                "field_limit": {
                    "type": "integer",
                    "default": 80,
                    "description": "最多返回多少个字段，默认 80；超过数量会标记为截断。",
                },
            },
            "required": ["table"],
        },
    },
    "blf_get_hive_etl_context": {
        "description": "读取 Hive 表在 DataHub 中的加工上下文，主要包括 Execute Shell 和 Etl Script 两个结构化属性。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Hive 表名，支持 db.table 或 table；未写库名时默认使用 default 库。",
                },
                "max_script_chars": {
                    "type": "integer",
                    "default": 8000,
                    "description": "每段脚本最多返回的字符数，用于避免超长脚本撑爆上下文。",
                },
            },
            "required": ["table"],
        },
    },
    "blf_get_hive_structured_properties": {
        "description": "一次性读取 Hive 表在 DataHub 中的全部已知 BLF 结构化属性。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Hive 表名，支持 db.table 或 table；未写库名时默认使用 default 库。",
                },
                "max_value_chars": {
                    "type": "integer",
                    "default": 4000,
                    "description": "每个属性值最多返回的字符数，超出会截断并标记 omitted_chars。",
                },
            },
            "required": ["table"],
        },
    },
    "blf_get_hive_structured_property": {
        "description": "按属性名读取 Hive 表在 DataHub 中的一个 BLF 结构化属性。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Hive 表名，支持 db.table 或 table；未写库名时默认使用 default 库。",
                },
                "property_name": {
                    "type": "string",
                    "description": "要读取的结构化属性名。",
                    "enum": [
                        "etl_script",
                        "execute_shell",
                        "schedule_url",
                        "data_availability_flag",
                        "other_remark",
                    ],
                },
                "max_value_chars": {
                    "type": "integer",
                    "default": 8000,
                    "description": "属性值最多返回的字符数，超出会截断并标记 omitted_chars。",
                },
            },
            "required": ["table", "property_name"],
        },
    },
    "blf_get_hive_etl_script": {
        "description": "读取 Hive 表的 Etl Script 结构化属性，即 DataHub 中保存的 ETL 脚本内容。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Hive 表名，支持 db.table 或 table；未写库名时默认使用 default 库。",
                },
                "max_value_chars": {
                    "type": "integer",
                    "default": 50000,
                    "description": "脚本最多返回的字符数，默认 50000，超出会截断并标记 omitted_chars。",
                },
            },
            "required": ["table"],
        },
    },
    "blf_get_hive_execute_shell": {
        "description": "读取 Hive 表的 Execute Shell 结构化属性，即调度执行入口或 shell 命令内容。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Hive 表名，支持 db.table 或 table；未写库名时默认使用 default 库。",
                },
                "max_value_chars": {
                    "type": "integer",
                    "default": 8000,
                    "description": "执行命令最多返回的字符数，超出会截断并标记 omitted_chars。",
                },
            },
            "required": ["table"],
        },
    },
    "blf_get_hive_schedule_url": {
        "description": "读取 Hive 表的 Schedule URL 结构化属性，即调度系统任务链接。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Hive 表名，支持 db.table 或 table；未写库名时默认使用 default 库。",
                },
                "max_value_chars": {
                    "type": "integer",
                    "default": 2000,
                    "description": "URL 属性最多返回的字符数，通常保持默认即可。",
                },
            },
            "required": ["table"],
        },
    },
    "blf_get_hive_data_availability_flag": {
        "description": "读取 Hive 表的 Data Availability Flag 结构化属性，用于判断 DDL、表血缘、字段血缘等元数据是否已入库。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Hive 表名，支持 db.table 或 table；未写库名时默认使用 default 库。",
                },
                "max_value_chars": {
                    "type": "integer",
                    "default": 2000,
                    "description": "属性值最多返回的字符数，通常保持默认即可。",
                },
            },
            "required": ["table"],
        },
    },
    "blf_get_hive_other_remark": {
        "description": "读取 Hive 表的 Other Remark 结构化属性，即 DataHub 中保存的其他备注信息。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Hive 表名，支持 db.table 或 table；未写库名时默认使用 default 库。",
                },
                "max_value_chars": {
                    "type": "integer",
                    "default": 8000,
                    "description": "备注内容最多返回的字符数，超出会截断并标记 omitted_chars。",
                },
            },
            "required": ["table"],
        },
    },
    "blf_get_hive_lineage": {
        "description": "读取 Hive 表在 DataHub 中的表级上游、下游血缘和影响面。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Hive 表名，支持 db.table 或 table；未写库名时默认使用 default 库。",
                },
                "direction": {
                    "type": "string",
                    "description": "血缘方向：upstream 查上游，downstream 查下游，both 同时查上下游。",
                    "enum": ["upstream", "downstream", "both"],
                    "default": "both",
                },
                "max_hops": {
                    "type": "integer",
                    "default": 1,
                    "description": "最大血缘跳数；跳数越大结果越多，实际可返回范围可能受 DataHub 服务端支持的 degree 过滤影响。",
                },
                "max_results": {
                    "type": "integer",
                    "default": 50,
                    "description": "每个方向请求返回的血缘实体数量；MCP 不再做 100 条硬限制，实际返回量可能受 DataHub 服务端分页或响应大小限制。",
                },
            },
            "required": ["table"],
        },
    },
    "blf_explain_hive_field_lineage": {
        "description": "递归读取 Hive 表字段级血缘，追溯到表名以 ods_ 或 pdw_ 开头的来源层后停止，并返回结构化证据。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Hive 表名，支持 db.table 或 table；未写库名时默认使用 default 库。",
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选字段名列表；不传时追踪所有非分区字段。",
                },
                "max_depth": {
                    "type": "integer",
                    "default": 30,
                    "description": "单条字段血缘路径最多向上追溯多少跳，默认 30。",
                },
                "max_paths": {
                    "type": "integer",
                    "default": 1000,
                    "description": "最多返回多少条字段血缘路径，默认 1000；超过后结果会截断。",
                },
                "max_transform_chars": {
                    "type": "integer",
                    "default": 1200,
                    "description": "每段 transformOperation 最多返回多少字符，超出会截断。",
                },
            },
            "required": ["table"],
        },
    },
    "blf_search_hive_assets": {
        "description": "在 DataHub 中搜索 BLF Hive 表，并返回候选表、说明和可用性标记。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，可以是表名片段、中文说明或业务关键词。",
                },
                "only_available": {
                    "type": "boolean",
                    "default": True,
                    "description": "是否只返回带 Data Availability Flag 的候选表。",
                },
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "description": "最多返回多少个候选表。",
                },
            },
            "required": ["query"],
        },
    },
    "blf_audit_hive_table": {
        "description": "审计单张 Hive 表在 DataHub 中的元数据完整性，包括 schema、文档、ETL、血缘、owner、tag 和可用性标记。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Hive 表名，支持 db.table 或 table；未写库名时默认使用 default 库。",
                }
            },
            "required": ["table"],
        },
    },
    "blf_explain_hive_table_context": {
        "description": "一次性读取 Hive 表画像、ETL、血缘和风险缺口，供 AI Agent 生成表解释或加工逻辑说明。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Hive 表名，支持 db.table 或 table；未写库名时默认使用 default 库。",
                }
            },
            "required": ["table"],
        },
    },
    "blf_get_schedule_job_profile": {
        "description": "从 DataHub 读取 BLF 调度作业元数据，包括负责人、业务线、触发方式、Cron 计划、上游依赖列表等。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_display_name": {
                    "type": "string",
                    "description": "调度作业名称（job_display_name），即调度系统中显示的作业名。",
                },
                "include_shell": {
                    "type": "boolean",
                    "default": False,
                    "description": "是否在 profile 中附带 execute_shell 内容（截断至 3000 字符）。",
                },
            },
            "required": ["job_display_name"],
        },
    },
    "blf_get_schedule_job_execute_shell": {
        "description": "读取 BLF 调度作业的 Execute Shell 命令全文（存储在 DataHub 结构化属性 job_execute_shell 中）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_display_name": {
                    "type": "string",
                    "description": "调度作业名称（job_display_name）。",
                },
                "max_value_chars": {
                    "type": "integer",
                    "default": 8000,
                    "description": "Shell 内容最多返回的字符数，超出会截断并标记 omitted_chars。",
                },
            },
            "required": ["job_display_name"],
        },
    },
    "blf_get_schedule_job_content_xml": {
        "description": "读取 BLF 调度作业的 Jenkins Job XML 配置全文（存储在 DataHub 结构化属性 job_content_xml 中）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_display_name": {
                    "type": "string",
                    "description": "调度作业名称（job_display_name）。",
                },
                "max_value_chars": {
                    "type": "integer",
                    "default": 8000,
                    "description": "XML 内容最多返回的字符数，超出会截断并标记 omitted_chars。",
                },
            },
            "required": ["job_display_name"],
        },
    },
    "blf_get_schedule_job_lineage": {
        "description": "查询 BLF 调度作业在 DataHub 中的上下游 DataJob 依赖关系（调度 DAG）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_display_name": {
                    "type": "string",
                    "description": "调度作业名称（job_display_name）。",
                },
                "direction": {
                    "type": "string",
                    "description": "依赖方向：upstream 查上游，downstream 查下游，both 同时查上下游。",
                    "enum": ["upstream", "downstream", "both"],
                    "default": "both",
                },
                "max_hops": {
                    "type": "integer",
                    "default": 1,
                    "description": "最大血缘跳数。",
                },
                "max_results": {
                    "type": "integer",
                    "default": 500,
                    "description": "每个方向最多返回的实体数量。",
                },
            },
            "required": ["job_display_name"],
        },
    },
    "blf_search_schedule_jobs": {
        "description": "在 DataHub 中按关键词搜索 BLF 调度作业，返回作业名称、负责人、触发方式等摘要。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，可以是作业名称片段或业务关键词。",
                },
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "description": "最多返回多少个候选作业，上限 200。",
                },
            },
            "required": ["query"],
        },
    },
    "blf_explain_schedule_job_context": {
        "description": "综合读取 BLF 调度作业的画像、执行 Shell 和调度 DAG 依赖，供 AI Agent 解释作业逻辑。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_display_name": {
                    "type": "string",
                    "description": "调度作业名称（job_display_name）。",
                }
            },
            "required": ["job_display_name"],
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
            "blf_get_hive_structured_properties": get_hive_structured_properties,
            "blf_get_hive_structured_property": get_hive_structured_property,
            "blf_get_hive_etl_script": get_hive_etl_script,
            "blf_get_hive_execute_shell": get_hive_execute_shell,
            "blf_get_hive_schedule_url": get_hive_schedule_url,
            "blf_get_hive_data_availability_flag": get_hive_data_availability_flag,
            "blf_get_hive_other_remark": get_hive_other_remark,
            "blf_get_hive_lineage": get_hive_lineage,
            "blf_explain_hive_field_lineage": explain_hive_field_lineage,
            "blf_search_hive_assets": search_hive_assets,
            "blf_audit_hive_table": audit_hive_table,
            "blf_explain_hive_table_context": explain_hive_table_context,
            "blf_get_schedule_job_profile": get_schedule_job_profile,
            "blf_get_schedule_job_execute_shell": get_schedule_job_execute_shell,
            "blf_get_schedule_job_content_xml": get_schedule_job_content_xml,
            "blf_get_schedule_job_lineage": get_schedule_job_lineage,
            "blf_search_schedule_jobs": search_schedule_jobs,
            "blf_explain_schedule_job_context": explain_schedule_job_context,
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
    """Format tool output as non-JSON text so Dify exposes it via the text field."""
    return (
        "DataHub MCP 工具返回如下。请按其中 JSON 证据回答用户，不要编造未返回的信息。\n\n"
        "```json\n"
        f"{json.dumps(result, ensure_ascii=False, indent=2)}\n"
        "```"
    )


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
