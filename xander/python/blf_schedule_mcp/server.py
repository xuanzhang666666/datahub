"""HTTP MCP server for BLF Jenkins schedule tools."""

from __future__ import annotations

import argparse
import functools
import json
import logging
import os
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .datahub_client import DataHubClient
from .jenkins_client import JenkinsClient
from .tools import (
    check_upstream_time_hour_match,
    diagnose_dependency_trigger,
    diagnose_schedule_job_failure,
    find_long_running_schedule_builds,
    format_time_hour_token_tool,
    get_schedule_job_build_history,
    get_schedule_job_build_log,
    get_schedule_job_build_status,
    get_schedule_job_last_failure,
    get_schedule_job_queue_stats,
    is_user_triggered_build,
    parse_trigger_condition_tool,
    parse_upstream_job_params_tool,
    search_schedule_jobs,
)

logger = logging.getLogger("blf_schedule_mcp")

JSON = dict[str, Any]
ToolHandler = Callable[..., JSON]

TOOL_SPECS: dict[str, dict[str, Any]] = {
    "blf_get_schedule_job_build_status": {
        "description": "查询 BLF 调度作业最近或指定构建的执行状态（成功/失败/运行中）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_display_name": {"type": "string", "description": "调度作业名称（Jenkins job 名称）。"},
                "build_ref": {
                    "type": ["string", "integer"],
                    "default": "lastBuild",
                    "description": "构建引用：lastBuild、lastFailedBuild、lastSuccessfulBuild 或构建号。",
                },
            },
            "required": ["job_display_name"],
        },
    },
    "blf_get_schedule_job_build_history": {
        "description": "查询 BLF 调度作业最近 N 次构建历史，含成功率统计；最多返回 50 条以避免 Jenkins 压力过大。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_display_name": {"type": "string", "description": "调度作业名称（Jenkins job 名称）。"},
                "limit": {"type": "integer", "default": 10, "description": "最近构建数量，最大 50。"},
            },
            "required": ["job_display_name"],
        },
    },
    "blf_get_schedule_job_build_log": {
        "description": "读取 BLF 调度作业某次构建的 console 日志尾部；服务端强制限流截断，最多读取并返回 256KB。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_display_name": {"type": "string", "description": "调度作业名称（Jenkins job 名称）。"},
                "build_ref": {"type": ["string", "integer"], "default": "lastBuild", "description": "构建引用或构建号。"},
                "max_chars": {"type": "integer", "default": 8000, "description": "返回日志尾部字符数，上限 262144。"},
            },
            "required": ["job_display_name"],
        },
    },
    "blf_get_schedule_job_last_failure": {
        "description": "读取 BLF 调度作业上次失败构建的日志并自动提取错误行；服务端强制限制日志大小。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_display_name": {"type": "string", "description": "调度作业名称（Jenkins job 名称）。"},
                "error_lines_limit": {"type": "integer", "default": 50, "description": "最多返回多少条错误行，上限 100。"},
                "log_max_chars": {"type": "integer", "default": 6000, "description": "返回日志尾部字符数，上限 262144。"},
            },
            "required": ["job_display_name"],
        },
    },
    "blf_diagnose_schedule_job_failure": {
        "description": "综合诊断调度作业失败原因（最近构建历史 + 上次失败日志 + 错误摘要），限制请求次数和日志大小以保护 Jenkins。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_display_name": {"type": "string", "description": "调度作业名称（Jenkins job 名称）。"},
                "history_limit": {"type": "integer", "default": 5, "description": "最近构建数量，上限 20。"},
                "log_max_chars": {"type": "integer", "default": 8000, "description": "返回日志尾部字符数，上限 262144。"},
            },
            "required": ["job_display_name"],
        },
    },
    "blf_search_schedule_job": {
        "description": "按名称子串模糊搜索 BLF 调度作业（like '%keyword%' 语义，基于 DataHub）。当不知道精确的 job_display_name 时先用此工具找到作业名。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "作业名称中的任意子串（不区分大小写）。"},
                "limit": {"type": "integer", "default": 20, "description": "最多返回条数，上限 5000。"},
            },
            "required": ["keyword"],
        },
    },
    "blf_find_long_running_schedule_builds": {
        "description": "扫描 Jenkins 当前正在运行的 BLF 调度构建，找出运行超过阈值的异常长任务；默认阈值 24 小时。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_running_hours": {"type": "integer", "default": 24, "description": "判定为长时间运行的小时阈值，默认 24。"},
                "max_results": {"type": "integer", "default": 50, "description": "最多返回多少个异常运行构建，上限 500。"},
            },
        },
    },
    "blf_get_schedule_job_queue_stats": {
        "description": "读取 Jenkins 构建队列中的所有 Task，并按 Jenkins 任务名聚合统计排队数量。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_job_results": {
                    "type": "integer",
                    "default": 500,
                    "description": "按 job 聚合后最多返回多少个任务统计项，上限 1000；items 始终返回全部队列 Task。",
                },
            },
        },
    },
    "blf_parse_trigger_condition": {
        "description": "解析 job-dependency-plugin 的 triggerCondition 字符串(如 h = 1 & 2 / d = @$ / h = *$ - 1),"
        " 返回 {date_type, logic_symbol, date_list} 并校验格式。"
        " 端口自 Java: TriggerConditionParser。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "condition_str": {
                    "type": "string",
                    "description": "完整的 triggerCondition 字符串,例如 'h = 1 & 2' 或 'd = @$' 或 'h = *$ - 1'。",
                },
            },
            "required": ["condition_str"],
        },
    },
    "blf_parse_upstream_job_params": {
        "description": "解析 job-dependency-plugin 的 upstreamJobParams 字段(如 A,B 或 A.param,B.3.company)。"
        " 端口自 Java: UpstreamJobParams.getJobParams。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "params_str": {
                    "type": "string",
                    "description": "upstreamJobParams 字段的完整字符串,',' 分隔多个上游参数项。",
                },
            },
            "required": ["params_str"],
        },
    },
    "blf_format_time_hour_token": {
        "description": "按 date_type (HOUR/DAY/WEEK/MONTH) 归一化 time_hour 字符串。"
        " 端口自 Java: getOnlyBuildOnceFormatStringContext + DateUtils。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "token": {"type": "string", "description": "time_hour 字符串,格式 yyyy/MM/dd/HH。"},
                "date_type": {
                    "type": "string",
                    "enum": ["HOUR", "DAY", "WEEK", "MONTH"],
                    "description": "归一化目标粒度。HOUR 不做格式化,DAY/WEEK/MONTH 分别归一化到对应的精度。",
                },
            },
            "required": ["token", "date_type"],
        },
    },
    "blf_check_upstream_time_hour_match": {
        "description": "给定上游任务 + 下游当前的 time_hour + triggerCondition,"
        " 列出该上游最近若干次构建,逐个说明 time_hour 是否满足 condition、result 是否 SUCCESS。"
        " 用于排查'上游跑成功了为什么下游没触发'。"
        " 端口自 Java: AbstractTriggerDownstream.matchTokenFromBuildHistory。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "upstream_job": {"type": "string", "description": "上游 Jenkins job 名。"},
                "time_hour": {"type": "string", "description": "下游任务当前携带的 time_hour,格式 yyyy/MM/dd/HH。"},
                "trigger_condition": {
                    "type": "string",
                    "default": "",
                    "description": "上游对应的 triggerCondition,留空表示只用 time_hour 直接匹配。",
                },
                "build_limit": {"type": "integer", "default": 20, "description": "最多返回多少条上游构建评估结果,上限 50。"},
                "history_limit": {"type": "integer", "default": 30, "description": "从 Jenkins 拉取上游最近构建的数量,上限 50。"},
            },
            "required": ["upstream_job", "time_hour"],
        },
    },
    "blf_diagnose_dependency_trigger": {
        "description": "综合诊断'为什么上游 A 跑完后没触发下游 B'。"
        " 复刻 JobDependencyBuildTrigger.shouldTriggerBuild 的三道闸门:"
        " (1) result 是否满足 threshold; (2) onlyBuildOnce 是否被队列里的同 time_hour 项拦截;"
        " (3) triggerCondition + time_hour 匹配是否成功。"
        " 每一道闸门单独给出通过/失败结论 + 排查建议。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "downstream_job": {"type": "string", "description": "下游 Jenkins job 名。"},
                "time_hour": {"type": "string", "description": "下游任务携带的 time_hour。"},
                "upstream_job": {"type": "string", "description": "上游 Jenkins job 名。"},
                "trigger_condition": {
                    "type": "string",
                    "default": "",
                    "description": "上游对应的 triggerCondition,留空表示只用 time_hour 直接匹配。",
                },
                "threshold": {
                    "type": "string",
                    "enum": ["SUCCESS", "UNSTABLE", "FAILED"],
                    "default": "SUCCESS",
                    "description": "上游 result 的最低要求,对应 Java ResultCondition。",
                },
                "build_ref": {
                    "type": ["string", "integer"],
                    "default": "lastBuild",
                    "description": "上游的构建引用,默认 lastBuild。",
                },
            },
            "required": ["downstream_job", "time_hour", "upstream_job"],
        },
    },
    "blf_is_user_triggered_build": {
        "description": "递归向上游检查 CauseAction 链,判断这次构建最终是否由用户手动触发。"
        " 用于排查 onlyBuildOnce 是否会因为'人工触发'而放行。"
        " 端口自 Java: BuildTriggerUtils.isTriggerByUser。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_display_name": {"type": "string", "description": "Jenkins job 名。"},
                "build_ref": {
                    "type": ["string", "integer"],
                    "default": "lastBuild",
                    "description": "构建引用。",
                },
                "max_depth": {
                    "type": "integer",
                    "default": 20,
                    "description": "上游 cause chain 最大递归深度,上限 100。",
                },
            },
            "required": ["job_display_name"],
        },
    },
}


def load_env_file(path: str) -> None:
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


class BlfScheduleMcpApplication:
    def __init__(
        self,
        *,
        jenkins_client: JenkinsClient,
        datahub_client: DataHubClient,
        mcp_token: str | None,
    ) -> None:
        self.jenkins_client = jenkins_client
        self.datahub_client = datahub_client
        self.mcp_token = mcp_token
        self.handlers: dict[str, ToolHandler] = {
            "blf_get_schedule_job_build_status": functools.partial(get_schedule_job_build_status, jenkins_client),
            "blf_get_schedule_job_build_history": functools.partial(get_schedule_job_build_history, jenkins_client),
            "blf_get_schedule_job_build_log": functools.partial(get_schedule_job_build_log, jenkins_client),
            "blf_get_schedule_job_last_failure": functools.partial(get_schedule_job_last_failure, jenkins_client),
            "blf_diagnose_schedule_job_failure": functools.partial(diagnose_schedule_job_failure, jenkins_client),
            "blf_search_schedule_job": functools.partial(search_schedule_jobs, datahub_client),
            "blf_find_long_running_schedule_builds": functools.partial(find_long_running_schedule_builds, jenkins_client),
            "blf_get_schedule_job_queue_stats": functools.partial(get_schedule_job_queue_stats, jenkins_client),
            "blf_parse_trigger_condition": parse_trigger_condition_tool,
            "blf_parse_upstream_job_params": parse_upstream_job_params_tool,
            "blf_format_time_hour_token": format_time_hour_token_tool,
            "blf_check_upstream_time_hour_match": functools.partial(check_upstream_time_hour_match, jenkins_client),
            "blf_diagnose_dependency_trigger": functools.partial(diagnose_dependency_trigger, jenkins_client),
            "blf_is_user_triggered_build": functools.partial(is_user_triggered_build, jenkins_client),
        }

    def authorized(self, header_value: str | None) -> bool:
        if not self.mcp_token:
            return True
        return secrets.compare_digest(header_value or "", f"Bearer {self.mcp_token}")

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
                    "serverInfo": {"name": "blf-schedule-mcp", "version": "0.1.0"},
                }
            elif method == "tools/list":
                result = {"tools": [{"name": name, **spec} for name, spec in TOOL_SPECS.items()]}
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
        result = self.handlers[name](**arguments)
        return {
            "content": [{"type": "text", "text": _format_tool_text(result)}],
            "isError": not bool(result.get("success", True)),
        }

    @staticmethod
    def _rpc_error(rpc_id: Any, code: int, message: str) -> JSON:
        return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _format_tool_text(result: JSON) -> str:
    return (
        "BLF Schedule MCP 工具返回如下。请按其中 JSON 证据回答用户，不要编造未返回的信息。\n\n"
        "```json\n"
        f"{json.dumps(result, ensure_ascii=False, indent=2)}\n"
        "```"
    )


def make_handler(app: BlfScheduleMcpApplication) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "BlfScheduleMcp/0.1"

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
                self._write_json({"error": "unauthorized"}, status=HTTPStatus.UNAUTHORIZED)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                self._write_json(
                    BlfScheduleMcpApplication._rpc_error(None, -32700, "Invalid JSON"),
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            if isinstance(payload, list):
                responses = [app.handle_rpc(item) for item in payload]
                self._write_json([item for item in responses if item is not None])
                return
            response = app.handle_rpc(payload)
            if response is None:
                self.send_response(HTTPStatus.ACCEPTED)
                self.end_headers()
                return
            self._write_json(response)

        def log_message(self, format: str, *args: Any) -> None:
            logger.info("%s - %s", self.address_string(), format % args)

        def _write_json(self, payload: Any, *, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def build_app() -> BlfScheduleMcpApplication:
    env_file = os.getenv("BLF_SCHEDULE_MCP_ENV_FILE", "/data/datahub/scripts/lineage.env")
    load_env_file(env_file)
    username = os.getenv("BLF_JENKINS_USER")
    token = os.getenv("BLF_JENKINS_TOKEN") or os.getenv("BLF_JENKINS_PASSWORD")
    if not username or not token:
        raise ValueError("BLF_JENKINS_USER and BLF_JENKINS_TOKEN or BLF_JENKINS_PASSWORD are required")
    return BlfScheduleMcpApplication(
        jenkins_client=JenkinsClient(
            base_url=os.getenv("BLF_JENKINS_URL", "http://schedule.corp.bianlifeng.com"),
            username=username,
            token=token,
            timeout_sec=int(os.getenv("BLF_JENKINS_TIMEOUT_SEC", "30")),
        ),
        datahub_client=DataHubClient(
            gms_url=os.getenv("DATAHUB_GMS_URL", "http://localhost:8080"),
            token=os.getenv("DATAHUB_GMS_TOKEN") or None,
            timeout_sec=int(os.getenv("DATAHUB_GMS_TIMEOUT_SEC", "30")),
        ),
        mcp_token=os.getenv("BLF_SCHEDULE_MCP_TOKEN"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run BLF schedule Jenkins MCP server")
    parser.add_argument("--host", default=os.getenv("BLF_SCHEDULE_MCP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("BLF_SCHEDULE_MCP_PORT", "9012")))
    parser.add_argument("--log-level", default=os.getenv("BLF_SCHEDULE_MCP_LOG_LEVEL", "INFO"))
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = build_app()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    logger.info("Starting BLF Schedule MCP server on %s:%s", args.host, args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
