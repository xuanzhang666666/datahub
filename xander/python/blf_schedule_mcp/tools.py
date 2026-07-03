"""BLF schedule Jenkins MCP tool implementations."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .datahub_client import DataHubClient, DataHubClientError
from .jenkins_client import JenkinsClient, JenkinsClientError

JENKINS_PUBLIC_BASE_URL = "http://schedule.corp.bianlifeng.com"
DEFAULT_LOG_MAX_CHARS = 6000
MAX_LOG_MAX_CHARS = 262144
LOG_FETCH_BYTES = 262144
ERROR_PATTERNS = (
    "ERROR",
    "FATAL",
    "Exception",
    "Traceback",
    "Error:",
    "FAILED",
    "OOM",
    "OutOfMemory",
    "killed",
    "exit code",
)
_ERROR_RE = re.compile("|".join(re.escape(pattern) for pattern in ERROR_PATTERNS), re.IGNORECASE)

# ---------------------------------------------------------------------------
# job-dependency-plugin 依赖逻辑常量
# 端口自 Java: src/main/java/com/wormpex/dp/constant/Constants.java
# ---------------------------------------------------------------------------
TIME_HOUR_TOKEN = "time_hour"
DAY_OF_LAST_MONTH = "#$"
DAY_OF_ANY_MONTH = "@$"
DATE_OF_CALCULATION_CONTEXT = "*$"
DATE_RELY_ON_ONESELF = "%$"
JOB_PARAM_JOINER = "."
MULTI_JOB_PARAM_JOINER = ","

HOUR_START, HOUR_END = 0, 23
DAY_START, DAY_END = 1, 31
WEEK_START, WEEK_END = 1, 7
MONTH_START, MONTH_END = 1, 12

TIME_HOUR_FORMAT = "%Y/%m/%d/%H"
DAY_FORMAT = "%Y/%m/%d"
WEEK_FORMAT = "%Y/%m/%d"
MONTH_FORMAT = "%Y/%m"

DATE_TYPE_RANGES: dict[str, tuple[int, int]] = {
    "HOUR": (HOUR_START, HOUR_END),
    "DAY": (DAY_START, DAY_END),
    "WEEK": (WEEK_START, WEEK_END),
    "MONTH": (MONTH_START, MONTH_END),
}

# 端口自 Java: TriggerConditionParser 中的正则
# 注意: 这些正则在 Java 端有"必须两位数字"的限制(如 h=01 而非 h=1),
# 这里是 1:1 端口,保留原有行为,用于复现 Java 端的格式校验.
TRIGGER_CONDITION_AND_REGEX = re.compile(r"^\s*[H|D|W|M|h|d|w|m]\s*=(\s*..\s*&)+\s*[^|]{2}\s*$")
TRIGGER_CONDITION_OR_REGEX = re.compile(r"^\s*[H|D|W|M|h|d|w|m]\s*=(\s*..\s*\|)+\s*[^|]{2}\s*$")
TRIGGER_CONDITION_EQUAL_REGEX = re.compile(r"^\s*[H|D|W|M|h|d|w|m]\s*=\s*..\s*$")
TRIGGER_CONDITION_DAY_REGEX = re.compile(r"^\s*[Dd]\s*=\s*..\s*$")
TRIGGER_CONDITION_CONTEXT_REGEX = re.compile(r"^\s*[H|D|M|h|d|m]\s*=\s*..\s*([+\-]\s*.{1,2}\s*){0,1}$")


def _job_base(job_display_name: str) -> dict[str, str]:
    name = _normalize_job_display_name(job_display_name)
    return {
        "job_display_name": name,
        "jenkins_url": f"{JENKINS_PUBLIC_BASE_URL}/job/{name}",
    }


def _normalize_job_display_name(job_display_name: str) -> str:
    if not isinstance(job_display_name, str) or not job_display_name.strip():
        raise ValueError("job_display_name is required")
    return job_display_name.strip()


def _error_response(error: Exception, **extra: Any) -> dict[str, Any]:
    error_type = "jenkins_error"
    if isinstance(error, JenkinsClientError):
        if error.status_code == 401:
            error_type = "unauthorized"
        elif error.status_code == 403:
            error_type = "forbidden"
        elif error.status_code == 404:
            error_type = "not_found"
    elif isinstance(error, DataHubClientError):
        error_type = "datahub_error"
        if error.status_code == 401:
            error_type = "unauthorized"
        elif error.status_code == 403:
            error_type = "forbidden"
    elif isinstance(error, ValueError):
        error_type = "invalid_input"
    return {
        "success": False,
        "error_type": error_type,
        "message": str(error),
        **extra,
    }


def _format_build(build: dict[str, Any]) -> dict[str, Any]:
    timestamp_ms = int(build.get("timestamp") or 0)
    started_at = ""
    if timestamp_ms:
        started_at = datetime.fromtimestamp(
            timestamp_ms / 1000,
            tz=timezone.utc,
        ).replace(tzinfo=None).isoformat(timespec="seconds")
    duration_ms = int(build.get("duration") or 0)
    return {
        "number": build.get("number"),
        "result": build.get("result"),
        "started_at": started_at,
        "duration_seconds": round(duration_ms / 1000, 3),
        "is_building": bool(build.get("building")),
        "url": build.get("url") or "",
    }


def _format_timestamp_ms(timestamp_ms: int) -> str:
    if not timestamp_ms:
        return ""
    return (
        datetime.fromtimestamp(
            timestamp_ms / 1000,
            tz=timezone.utc,
        )
        .replace(tzinfo=None)
        .isoformat(timespec="seconds")
    )


def _status_from_build(build: dict[str, Any]) -> str:
    if build.get("building"):
        return "running"
    result = build.get("result")
    if result == "SUCCESS":
        return "success"
    if result == "ABORTED":
        return "aborted"
    if result == "FAILURE":
        return "failed"
    return "unknown"


def _validate_build_ref(build_ref: int | str) -> int | str:
    if isinstance(build_ref, int):
        return build_ref
    if isinstance(build_ref, str):
        stripped = build_ref.strip()
        if stripped in {"lastBuild", "lastFailedBuild", "lastSuccessfulBuild"}:
            return stripped
        if stripped.isdigit():
            return int(stripped)
    raise ValueError("build_ref must be lastBuild, lastFailedBuild, lastSuccessfulBuild, or an integer")


def _bounded_int(value: int, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


def _truncate_log(text: str, max_chars: int) -> dict[str, Any]:
    max_chars = _bounded_int(max_chars, default=DEFAULT_LOG_MAX_CHARS, minimum=1000, maximum=MAX_LOG_MAX_CHARS)
    chars = len(text)
    truncated = chars > max_chars
    return {
        "text": text[-max_chars:],
        "chars": chars,
        "truncated": truncated,
        "omitted_chars": max(chars - max_chars, 0),
        "fetch_limit_bytes": LOG_FETCH_BYTES,
        "slice": "tail",
    }


def _extract_error_lines(log_text: str, limit: int = 50) -> list[str]:
    limit = _bounded_int(limit, default=50, minimum=1, maximum=100)
    results = []
    for line in log_text.splitlines():
        stripped = line.strip()
        if stripped and _ERROR_RE.search(stripped):
            results.append(stripped)
        if len(results) >= limit:
            break
    return results


def search_schedule_jobs(
    client: DataHubClient,
    *,
    keyword: str,
    limit: int = 20,
) -> dict[str, Any]:
    try:
        if not isinstance(keyword, str) or not keyword.strip():
            raise ValueError("keyword is required")
        keyword = keyword.strip()
        limit = _bounded_int(limit, default=20, minimum=1, maximum=5000)
        result = client.search_data_jobs(keyword, limit)
        jobs = [
            {
                "job_display_name": job.get("name") or job.get("job_id") or "",
                "urn": job.get("urn") or "",
                "jenkins_url": f"{JENKINS_PUBLIC_BASE_URL}/job/{job.get('name') or job.get('job_id') or ''}",
            }
            for job in result.get("jobs") or []
        ]
        risks = []
        if not jobs:
            risks.append("未命中任何调度作业，建议尝试更短的子串或换一个关键词")
        return {
            "success": True,
            "summary": {
                "keyword": keyword,
                "total": result.get("total", 0),
                "returned": len(jobs),
                "jobs": jobs,
            },
            "risks": risks,
            "evidence": {
                "interface": "DataHub GraphQL searchAcrossEntities",
                "query_semantics": "name like '%keyword%' OR jobId like '%keyword%'（结构化 wildcard 查询）",
            },
        }
    except Exception as exc:
        return _error_response(exc, keyword=keyword if isinstance(keyword, str) else "")


def get_schedule_job_build_status(
    client: JenkinsClient,
    *,
    job_display_name: str,
    build_ref: int | str = "lastBuild",
) -> dict[str, Any]:
    try:
        base = _job_base(job_display_name)
        ref = _validate_build_ref(build_ref)
        build = client.get_build_info(base["job_display_name"], ref)
        if not build:
            return {
                "success": False,
                **base,
                "error_type": "not_found",
                "message": "Build not found",
                "summary": {},
                "risks": ["未找到指定构建，请检查 build_ref 是否正确"],
                "evidence": {"interface": "Jenkins REST API", "endpoint": f"/job/{{name}}/{ref}/api/json"},
            }
        formatted = _format_build(build)
        status = _status_from_build(build)
        risks = []
        if status == "failed":
            risks.append("最近构建失败，请检查日志")
        elif status == "aborted":
            risks.append("最近构建被中止，请确认是否为人工取消或超时")
        elif status == "running":
            risks.append("构建仍在运行中，结果尚未确定")
        return {
            "success": True,
            **base,
            "summary": {
                "build_number": formatted["number"],
                "result": formatted["result"],
                "status": status,
                "started_at": formatted["started_at"],
                "duration_seconds": formatted["duration_seconds"],
                "is_building": formatted["is_building"],
                "build_url": formatted["url"],
            },
            "risks": risks,
            "evidence": {"interface": "Jenkins REST API", "endpoint": f"/job/{{name}}/{ref}/api/json"},
        }
    except Exception as exc:
        try:
            base = _job_base(job_display_name)
        except Exception:
            base = {"job_display_name": job_display_name}
        return _error_response(exc, **base)


def get_schedule_job_build_history(
    client: JenkinsClient,
    *,
    job_display_name: str,
    limit: int = 10,
) -> dict[str, Any]:
    try:
        base = _job_base(job_display_name)
        limit = _bounded_int(limit, default=10, minimum=1, maximum=50)
        job_info = client.get_job_info(base["job_display_name"])
        builds = client.get_build_history(base["job_display_name"], limit)
        formatted = [_format_build(build) for build in builds]
        successes = [build for build in formatted if build.get("result") == "SUCCESS"]
        failures = [build for build in formatted if build.get("result") == "FAILURE"]
        risks = []
        if failures:
            risks.append(f"最近 {len(formatted)} 次构建中有 {len(failures)} 次失败")
        return {
            "success": True,
            **base,
            "summary": {
                "total_builds": len(job_info.get("builds") or []),
                "returned": len(formatted),
                "builds": formatted,
                "success_rate": f"{len(successes)}/{len(formatted)}",
                "last_success_build": successes[0].get("number") if successes else None,
                "last_failure_build": failures[0].get("number") if failures else None,
            },
            "risks": risks,
            "evidence": {"interface": "Jenkins REST API", "endpoint": "/job/{name}/api/json?tree=builds[...]"},
        }
    except Exception as exc:
        try:
            base = _job_base(job_display_name)
        except Exception:
            base = {"job_display_name": job_display_name}
        return _error_response(exc, **base)


def find_long_running_schedule_builds(
    client: JenkinsClient,
    *,
    min_running_hours: int = 24,
    max_results: int = 50,
    now_ms: int | None = None,
) -> dict[str, Any]:
    try:
        threshold_hours = _bounded_int(min_running_hours, default=24, minimum=1, maximum=24 * 30)
        max_results = _bounded_int(max_results, default=50, minimum=1, maximum=500)
        if now_ms is None:
            now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        running_builds = client.get_running_builds()
        long_running = []
        skipped_without_timestamp = 0
        for build in running_builds:
            started_at_ms = int(build.get("started_at_ms") or 0)
            if not started_at_ms:
                skipped_without_timestamp += 1
                continue
            running_ms = max(now_ms - started_at_ms, 0)
            running_hours = running_ms / 1000 / 60 / 60
            if running_hours < threshold_hours:
                continue
            long_running.append(
                {
                    "job_display_name": build.get("job_display_name") or "",
                    "build_number": build.get("build_number"),
                    "running_hours": round(running_hours, 2),
                    "started_at": _format_timestamp_ms(started_at_ms),
                    "build_url": build.get("build_url") or "",
                    "executor": build.get("executor") or "",
                }
            )
        long_running.sort(key=lambda item: item["running_hours"], reverse=True)
        returned = long_running[:max_results]
        risks = []
        if long_running:
            risks.append(f"发现 {len(long_running)} 个 Jenkins 构建长时间运行未退出，建议检查是否卡死或等待外部资源")
        return {
            "success": True,
            "summary": {
                "threshold_hours": threshold_hours,
                "total_running_builds": len(running_builds),
                "long_running_count": len(long_running),
                "returned": len(returned),
                "skipped_without_timestamp": skipped_without_timestamp,
                "builds": returned,
            },
            "risks": risks,
            "evidence": {
                "interface": "Jenkins REST API",
                "endpoint": "/computer/api/json?tree=computer[executors,currentExecutable]",
            },
        }
    except Exception as exc:
        return _error_response(exc)


def get_schedule_job_queue_stats(
    client: JenkinsClient,
    *,
    max_job_results: int = 500,
) -> dict[str, Any]:
    try:
        max_job_results = _bounded_int(max_job_results, default=500, minimum=1, maximum=1000)
        queue_items = client.get_queue_items()
        counts: dict[str, int] = {}
        job_urls: dict[str, str] = {}
        for item in queue_items:
            job_name = str(item.get("job_display_name") or "").strip()
            if not job_name:
                continue
            counts[job_name] = counts.get(job_name, 0) + 1
            job_urls.setdefault(job_name, str(item.get("jenkins_url") or "").strip())
        job_counts = [
            {
                "job_display_name": job_name,
                "count": count,
                "jenkins_url": job_urls.get(job_name)
                or f"{JENKINS_PUBLIC_BASE_URL}/job/{job_name}",
            }
            for job_name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]
        returned_job_counts = job_counts[:max_job_results]
        blocked_count = sum(1 for item in queue_items if item.get("blocked"))
        risks = []
        if blocked_count:
            risks.append(f"构建队列中有 {blocked_count} 个任务处于 blocked 状态，可能等待上游或资源")
        if len(job_counts) > max_job_results:
            risks.append(
                f"按 job 聚合后有 {len(job_counts)} 个不同任务，仅返回排队数最多的前 {max_job_results} 个"
            )
        return {
            "success": True,
            "summary": {
                "total_queue_items": len(queue_items),
                "distinct_jobs": len(job_counts),
                "blocked_items": blocked_count,
                "returned_job_counts": len(returned_job_counts),
                "job_counts": returned_job_counts,
                "items": queue_items,
            },
            "risks": risks,
            "evidence": {
                "interface": "Jenkins REST API",
                "endpoint": "/queue/api/json?tree=items[id,why,blocked,buildable,inQueueSince,task[...]]",
            },
        }
    except Exception as exc:
        return _error_response(exc)


def trigger_schedule_job_build(
    client: JenkinsClient,
    *,
    job_display_name: str,
    parameters: dict[str, Any] | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    try:
        base = _job_base(job_display_name)
        if confirm is not True:
            raise ValueError("confirm must be true to trigger a schedule job build")
        normalized_parameters = parameters or {}
        trigger = client.trigger_build(
            base["job_display_name"],
            parameters=normalized_parameters,
        )
        risks = ["已向 Jenkins 提交构建请求，请继续查询队列或构建状态确认实际执行结果"]
        if normalized_parameters:
            risks.append("本次为参数化触发，请确认传入参数与该 job 的参数定义一致")
        return {
            "success": True,
            **base,
            "summary": {
                "triggered": True,
                "parameters": normalized_parameters,
                "queue_id": trigger.get("queue_id"),
                "queue_url": trigger.get("queue_url") or "",
            },
            "risks": risks,
            "evidence": {
                "interface": "Jenkins REST API",
                "endpoint": trigger.get("endpoint") or "/job/{name}/build",
            },
        }
    except Exception as exc:
        try:
            base = _job_base(job_display_name)
        except Exception:
            base = {"job_display_name": job_display_name}
        return _error_response(exc, **base)


def trigger_schedule_job_single_build(
    client: JenkinsClient,
    *,
    job_display_name: str,
    parameters: dict[str, Any] | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    try:
        base = _job_base(job_display_name)
        if confirm is not True:
            raise ValueError("confirm must be true to trigger a schedule job single build")
        normalized_parameters = parameters or {}
        trigger = client.trigger_single_build(
            base["job_display_name"],
            parameters=normalized_parameters,
        )
        risks = ["已向 Jenkins 提交单次构建请求；按调度约定，该构建完成后不会触发下游 job"]
        if normalized_parameters:
            risks.append("本次为参数化单次构建，请确认传入参数与该 job 的参数定义一致")
        return {
            "success": True,
            **base,
            "summary": {
                "triggered": True,
                "trigger_mode": "single_build",
                "parameters": normalized_parameters,
                "queue_id": trigger.get("queue_id"),
                "queue_url": trigger.get("queue_url") or "",
            },
            "risks": risks,
            "evidence": {
                "interface": "Jenkins REST API",
                "endpoint": trigger.get("endpoint") or "/job/{name}/build1?delay=0sec&singleBuild=true",
            },
        }
    except Exception as exc:
        try:
            base = _job_base(job_display_name)
        except Exception:
            base = {"job_display_name": job_display_name}
        return _error_response(exc, **base)


def rebuild_schedule_job_build(
    client: JenkinsClient,
    *,
    job_display_name: str,
    build_number: int,
    parameters: dict[str, Any] | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    try:
        base = _job_base(job_display_name)
        if confirm is not True:
            raise ValueError("confirm must be true to rebuild a schedule job build")
        normalized_parameters = parameters or {}
        trigger = client.rebuild_build(
            base["job_display_name"],
            build_number,
            parameters=normalized_parameters,
        )
        risks = ["已向 Jenkins 提交重新构建请求；按调度约定，该构建完成后不会触发下游 job"]
        if normalized_parameters:
            risks.append("本次 rebuild 覆盖了构建参数，请确认参数与原构建及 job 定义一致")
        return {
            "success": True,
            **base,
            "summary": {
                "triggered": True,
                "trigger_mode": "rebuild",
                "build_number": int(build_number),
                "parameters": normalized_parameters,
                "queue_id": trigger.get("queue_id"),
                "queue_url": trigger.get("queue_url") or "",
            },
            "risks": risks,
            "evidence": {
                "interface": "Jenkins REST API",
                "endpoint": trigger.get("endpoint") or "/job/{name}/{build}/rebuild/parameterized",
            },
        }
    except Exception as exc:
        try:
            base = _job_base(job_display_name)
        except Exception:
            base = {"job_display_name": job_display_name}
        return _error_response(exc, **base)


def cancel_schedule_job_build(
    client: JenkinsClient,
    *,
    job_display_name: str,
    build_ref: int | str = "lastBuild",
    queue_id: int | None = None,
    cancel_running: bool = True,
    cancel_queued: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    try:
        base = _job_base(job_display_name)
        if confirm is not True:
            raise ValueError("confirm must be true to cancel schedule job builds")
        running_build_number = None
        running_build_cancelled = False
        cancelled_queue_ids: list[int] = []
        cancelled_queue_items: list[dict[str, Any]] = []
        risks = []

        if cancel_running:
            ref = _validate_build_ref(build_ref)
            build = client.get_build_info(base["job_display_name"], ref)
            if build:
                running_build_number = build.get("number")
                if build.get("building") and running_build_number:
                    client.cancel_build(base["job_display_name"], int(running_build_number))
                    running_build_cancelled = True
                else:
                    risks.append("指定构建当前不在运行中，没有取消正在运行的构建")
            else:
                risks.append("未找到指定构建，没有取消正在运行的构建")

        if cancel_queued:
            queue_items = client.get_queue_items()
            for item in queue_items:
                item_queue_id = item.get("queue_id")
                if not item_queue_id:
                    continue
                if queue_id is not None:
                    if int(item_queue_id) != int(queue_id):
                        continue
                elif str(item.get("job_display_name") or "").strip() != base["job_display_name"]:
                    continue
                client.cancel_queue_item(int(item_queue_id))
                cancelled_queue_ids.append(int(item_queue_id))
                cancelled_queue_items.append(item)
            if queue_id is not None and not cancelled_queue_ids:
                risks.append("未在 Jenkins 队列中找到指定 queue_id")
            elif queue_id is None and not cancelled_queue_ids:
                risks.append("未在 Jenkins 队列中找到该 job 的排队构建")

        if not running_build_cancelled and not cancelled_queue_ids:
            risks.append("没有实际取消任何 Jenkins 构建或队列项")

        return {
            "success": True,
            **base,
            "summary": {
                "running_build_cancelled": running_build_cancelled,
                "running_build_number": running_build_number,
                "cancelled_queue_ids": cancelled_queue_ids,
                "cancelled_queue_items": cancelled_queue_items,
                "cancel_running": bool(cancel_running),
                "cancel_queued": bool(cancel_queued),
            },
            "risks": sorted(set(risks)),
            "evidence": {
                "interface": "Jenkins REST API",
                "endpoints": ["/job/{name}/{build}/stop", "/queue/cancelItem?id={queue_id}"],
            },
        }
    except Exception as exc:
        try:
            base = _job_base(job_display_name)
        except Exception:
            base = {"job_display_name": job_display_name}
        return _error_response(exc, **base)


def get_schedule_job_build_log(
    client: JenkinsClient,
    *,
    job_display_name: str,
    build_ref: int | str = "lastBuild",
    max_chars: int = 8000,
) -> dict[str, Any]:
    try:
        base = _job_base(job_display_name)
        ref = _validate_build_ref(build_ref)
        max_chars = _bounded_int(max_chars, default=8000, minimum=1000, maximum=MAX_LOG_MAX_CHARS)
        build = client.get_build_info(base["job_display_name"], ref)
        if not build:
            return {
                "success": False,
                **base,
                "error_type": "not_found",
                "message": "Build not found",
                "summary": {},
                "risks": ["未找到指定构建，未读取日志"],
                "evidence": {"interface": "Jenkins REST API", "endpoint": f"/job/{{name}}/{ref}/api/json"},
            }
        build_number = build.get("number") or ref
        log_text = client.get_build_log(base["job_display_name"], build_number, max_bytes=LOG_FETCH_BYTES)
        return {
            "success": True,
            **base,
            "summary": {
                "build_number": build.get("number"),
                "result": build.get("result"),
                "log": _truncate_log(log_text, max_chars),
            },
            "risks": [],
            "evidence": {"interface": "Jenkins REST API", "endpoint": "/job/{name}/{build}/consoleText"},
        }
    except Exception as exc:
        try:
            base = _job_base(job_display_name)
        except Exception:
            base = {"job_display_name": job_display_name}
        return _error_response(exc, **base)


def get_schedule_job_last_failure(
    client: JenkinsClient,
    *,
    job_display_name: str,
    error_lines_limit: int = 50,
    log_max_chars: int = 6000,
) -> dict[str, Any]:
    try:
        base = _job_base(job_display_name)
        build = client.get_last_failed_build(base["job_display_name"])
        if not build:
            return {
                "success": True,
                **base,
                "summary": {"build_number": None, "result": None, "error_excerpt": [], "log": _truncate_log("", log_max_chars)},
                "risks": ["未找到失败构建记录"],
                "evidence": {"interface": "Jenkins REST API", "endpoint": "/job/{name}/lastFailedBuild/api/json"},
            }
        formatted = _format_build(build)
        build_number = build.get("number")
        log_text = client.get_build_log(base["job_display_name"], build_number, max_bytes=LOG_FETCH_BYTES)
        error_excerpt = _extract_error_lines(log_text, error_lines_limit)
        risks = ["构建失败"]
        if any("outofmemory" in line.lower() or "oom" in line.lower() for line in error_excerpt):
            risks.append("构建失败，检测到 OOM 异常")
        if any("killed" in line.lower() for line in error_excerpt):
            risks.append("构建失败，检测到进程被 killed")
        return {
            "success": True,
            **base,
            "summary": {
                "build_number": build_number,
                "result": formatted["result"],
                "started_at": formatted["started_at"],
                "duration_seconds": formatted["duration_seconds"],
                "error_excerpt": error_excerpt,
                "log": _truncate_log(log_text, log_max_chars),
            },
            "risks": sorted(set(risks)),
            "evidence": {"interface": "Jenkins REST API", "endpoint": "/job/{name}/lastFailedBuild + consoleText"},
        }
    except Exception as exc:
        try:
            base = _job_base(job_display_name)
        except Exception:
            base = {"job_display_name": job_display_name}
        return _error_response(exc, **base)


def diagnose_schedule_job_failure(
    client: JenkinsClient,
    *,
    job_display_name: str,
    history_limit: int = 5,
    log_max_chars: int = 8000,
) -> dict[str, Any]:
    try:
        base = _job_base(job_display_name)
        history = get_schedule_job_build_history(
            client,
            job_display_name=base["job_display_name"],
            limit=_bounded_int(history_limit, default=5, minimum=1, maximum=20),
        )
        last_failure = get_schedule_job_last_failure(
            client,
            job_display_name=base["job_display_name"],
            error_lines_limit=50,
            log_max_chars=log_max_chars,
        )
        risks = []
        for payload in (history, last_failure):
            risks.extend(payload.get("risks") or [])
        return {
            "success": bool(history.get("success", True) and last_failure.get("success", True)),
            **base,
            "summary": {
                "history": history.get("summary"),
                "last_failure": last_failure.get("summary"),
            },
            "risks": sorted(set(risks)),
            "evidence": {"tools": ["get_schedule_job_build_history", "get_schedule_job_last_failure"]},
        }
    except Exception as exc:
        try:
            base = _job_base(job_display_name)
        except Exception:
            base = {"job_display_name": job_display_name}
        return _error_response(exc, **base)


# ---------------------------------------------------------------------------
# job-dependency-plugin 逻辑端口
# 原始 Java 实现见:
#   src/main/java/com/wormpex/dp/context/TriggerConditionParser.java
#   src/main/java/com/wormpex/dp/action/AbstractTriggerDownstream.java
#   src/main/java/com/wormpex/dp/utils/DateUtils.java
#   src/main/java/com/wormpex/dp/utils/BuildTriggerUtils.java
#   src/main/java/com/wormpex/dp/trigger/JobDependencyBuildTrigger.java
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TriggerConditionInfo:
    """端口自 Java: com.wormpex.dp.pojo.TriggerConditionInfo"""

    date_type: str  # HOUR / DAY / WEEK / MONTH
    logic_symbol: str  # AND / OR / NO
    date_list: list[str]


def _date_type_from_char(char: str) -> str:
    upper = char.upper()
    if upper == "H":
        return "HOUR"
    if upper == "D":
        return "DAY"
    if upper == "W":
        return "WEEK"
    if upper == "M":
        return "MONTH"
    raise ValueError(f"不支持的日期类型字符: {char},仅支持 H/D/W/M")


def _find_logic_symbol(date_expression: str) -> str:
    """端口自 Java: TriggerConditionParser.findLogicSymbolFromExpresstion"""
    if "&" in date_expression:
        return "AND"
    if "|" in date_expression:
        return "OR"
    return "NO"


def _date_split(date_expression: str) -> list[str]:
    """端口自 Java: TriggerConditionParser.dateSplit"""
    if "&" in date_expression:
        parts = date_expression.split("&")
    elif "|" in date_expression:
        parts = date_expression.split("|")
    else:
        parts = [date_expression]
    return [p.strip() for p in parts if p and p.strip()]


def _parse_time_hour_token(token: str) -> datetime:
    if not isinstance(token, str) or not token.strip():
        raise ValueError("time_hour 为空")
    return datetime.strptime(token.strip(), TIME_HOUR_FORMAT)


def _date_append_offset(date_str: str) -> int:
    """解析 `*$ - 1` / `%$ + 1` 这类带偏移量的标记,返回偏移量整数.

    端口自 Java: AbstractTriggerDownstream.dateAppendToContext
    """
    result = 0
    try:
        if "+" in date_str:
            result = int(date_str.split("+", 1)[1].strip())
        elif "-" in date_str:
            result = -int(date_str.split("-", 1)[1].strip())
    except (ValueError, IndexError) as exc:
        raise ValueError(f"无法解析偏移量: {date_str}") from exc
    return result


def _strip_date_marker(date_str: str) -> str:
    """去掉 `%$` / `*$` / `#$` / `@$` 等特殊标记,只保留数字部分(可带正负号)."""
    cleaned = re.sub(r"[*@#%]\$", "", date_str).strip().replace(" ", "")
    if cleaned.startswith("+") or cleaned.startswith("-"):
        return cleaned
    if "+" in cleaned:
        return cleaned.split("+", 1)[0]
    if "-" in cleaned:
        return cleaned.split("-", 1)[0]
    return cleaned


def _check_date_range(date_str: str, date_type: str) -> list[str]:
    """校验单个日期值是否在范围内. 返回错误信息列表(空表示通过)."""
    # 特殊标记不需要数值范围校验
    if DAY_OF_ANY_MONTH in date_str or DAY_OF_LAST_MONTH in date_str:
        return []
    cleaned = _strip_date_marker(date_str)
    if not cleaned:
        return []
    try:
        value = int(cleaned)
    except (ValueError, TypeError):
        return [f"无法将 {date_str!r} 解析为整数"]
    if date_type not in DATE_TYPE_RANGES:
        return [f"不支持的 date_type: {date_type}"]
    min_val, max_val = DATE_TYPE_RANGES[date_type]
    if value < min_val or value > max_val:
        return [f"{date_type} 值 {value} 超出允许范围 {min_val}-{max_val}"]
    return []


def parse_trigger_condition(condition_str: str) -> TriggerConditionInfo:
    """解析 triggerCondition 字符串.

    端口自 Java: TriggerConditionParser.parse + checkBeforeParse + checkAfterParse
    返回结构化对象. 格式不合规时抛出 ValueError.
    """
    if not isinstance(condition_str, str) or not condition_str.strip():
        raise ValueError("triggerCondition 不能为空")
    s = condition_str.strip()
    validation = validate_trigger_condition(s)
    if not validation["valid"]:
        raise ValueError("; ".join(validation["errors"]))

    head, _, tail = s.partition("=")
    date_type = _date_type_from_char(head.strip()[0])
    date_expression = tail.strip()
    return TriggerConditionInfo(
        date_type=date_type,
        logic_symbol=_find_logic_symbol(date_expression),
        date_list=_date_split(date_expression),
    )


def validate_trigger_condition(condition_str: str) -> dict[str, Any]:
    """校验 triggerCondition 字符串格式.

    端口自 Java: TriggerConditionParser.checkBeforeParse + checkAfterParse
    返回 {"valid": bool, "errors": [str, ...]}.
    """
    if not isinstance(condition_str, str) or not condition_str.strip():
        return {"valid": False, "errors": ["triggerCondition 不能为空"], "date_type": None, "logic_symbol": None}

    s = condition_str.strip()
    errors: list[str] = []
    date_type: str | None = None
    logic_symbol: str | None = None

    # ---- checkBeforeParse ----
    if DATE_OF_CALCULATION_CONTEXT in s or DATE_RELY_ON_ONESELF in s:
        if _find_logic_symbol(s) != "NO":
            errors.append(
                f"包含 {DATE_OF_CALCULATION_CONTEXT} 或 {DATE_RELY_ON_ONESELF} 时,只能是单个值,不能使用 & 或 |"
            )
        if not TRIGGER_CONDITION_CONTEXT_REGEX.match(s):
            errors.append(
                f"context 表达式格式不合法,例如 h = *$ - 1. 实际输入: {s}"
            )
    elif DAY_OF_ANY_MONTH in s:
        if not TRIGGER_CONDITION_DAY_REGEX.match(s):
            errors.append(f"@$ 表达式格式不合法,例如 d = @$. 实际输入: {s}")
    elif not (
        TRIGGER_CONDITION_AND_REGEX.match(s)
        or TRIGGER_CONDITION_OR_REGEX.match(s)
        or TRIGGER_CONDITION_EQUAL_REGEX.match(s)
    ):
        errors.append(
            f"triggerCondition 格式不合法: {s}. 支持 H/D/W/M 后面跟 = 和 2 位数字"
            " 或 *$/%$/@$/#$ 标记"
        )

    # ---- checkAfterParse ----
    if not errors:
        head, _, tail = s.partition("=")
        date_type = _date_type_from_char(head.strip()[0])
        date_expression = tail.strip()
        logic_symbol = _find_logic_symbol(date_expression)
        date_list = _date_split(date_expression)

        if DATE_RELY_ON_ONESELF not in s and DATE_OF_CALCULATION_CONTEXT not in s:
            for d in date_list:
                errors.extend(_check_date_range(d, date_type))

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "date_type": date_type,
        "logic_symbol": logic_symbol,
    }


def parse_upstream_job_params(params_str: str) -> list[dict[str, str]]:
    """解析 upstreamJobParams 字段.

    端口自 Java: UpstreamJobParams.getJobParams
    支持的格式:
      - "A,B"           → 一组 plain
      - "A.param"       → JobParam(job=A, param=param)
      - "A.3.company"   → JobDateParam(job=A, date_type=3, param=company)
    """
    if not isinstance(params_str, str) or not params_str.strip():
        return []
    result: list[dict[str, str]] = []
    for raw in params_str.split(MULTI_JOB_PARAM_JOINER):
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split(JOB_PARAM_JOINER)
        if len(parts) == 1:
            result.append({"type": "plain", "raw": raw, "value": raw})
        elif len(parts) == 2:
            result.append({"type": "JobParam", "raw": raw, "job_name": parts[0].strip(), "param": parts[1].strip()})
        elif len(parts) == 3:
            result.append(
                {
                    "type": "JobDateParam",
                    "raw": raw,
                    "job_name": parts[0].strip(),
                    "date_type": parts[1].strip(),
                    "param": parts[2].strip(),
                }
            )
        else:
            raise ValueError(f"参数格式不符合规范: {raw}. 支持 A | A.B | A.B.C")
    return result


def format_time_hour_token(token: str, date_type: str) -> dict[str, Any]:
    """按 date_type 归一化 time_hour 字符串.

    端口自 Java: JobDependencyBuildTrigger.getOnlyBuildOnceFormatStringContext + DateUtils
    """
    if not isinstance(token, str) or not token.strip():
        return {"input": token, "date_type": date_type, "output": token, "parsed": False}
    s = token.strip()
    normalized = (date_type or "HOUR").upper()
    if normalized == "HOUR":
        return {"input": token, "date_type": normalized, "output": s, "parsed": True}
    try:
        parsed = _parse_time_hour_token(s)
    except ValueError as exc:
        return {"input": token, "date_type": normalized, "output": s, "parsed": False, "error": str(exc)}

    if normalized == "DAY":
        out = parsed.strftime(DAY_FORMAT)
    elif normalized == "WEEK":
        monday = parsed - timedelta(days=parsed.weekday())
        out = monday.strftime(WEEK_FORMAT)
    elif normalized == "MONTH":
        out = parsed.strftime(MONTH_FORMAT)
    else:
        return {"input": token, "date_type": normalized, "output": s, "parsed": False, "error": f"不支持的 date_type: {date_type}"}
    return {"input": token, "date_type": normalized, "output": out, "parsed": True}


def _format_expected_hour(token: datetime, hour_value: int, *, absolute: bool) -> str:
    """生成期望的 hour time_hour 字符串.

    端口自 Java: AbstractTriggerDownstream.checkHourCondition vs checkTokenHourCondition.
    absolute=True: 把日期的 hour 强制设为 hour_value (同一日)
    absolute=False: 在 token 当前时间上增加 hour_value 偏移
    """
    if absolute:
        target = token.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=hour_value)
    else:
        target = token + timedelta(hours=hour_value)
    return target.strftime(TIME_HOUR_FORMAT)


def _format_expected_day(token: datetime, day_value: int, *, absolute: bool) -> str:
    if absolute:
        anchor = token.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        target = anchor + timedelta(days=day_value - 1)
    else:
        target = token + timedelta(days=day_value)
    return target.strftime(DAY_FORMAT)


def _format_expected_week(token: datetime, week_value: int) -> str:
    monday = token - timedelta(days=token.weekday())
    target = monday + timedelta(days=week_value - 1)
    return target.strftime(DAY_FORMAT)


def _shift_months(dt: datetime, months: int) -> datetime:
    """按"月"做日期偏移, 1-12 月循环, 2-29 等月末日做最小天数 clamp.

    比 timedelta(days=30*N) 精确,匹配 Java Calendar.add(MONTH, N) 的语义.
    """
    total = dt.month - 1 + months
    new_year = dt.year + total // 12
    new_month = total % 12 + 1
    new_day = min(dt.day, _days_in_month(new_year, new_month))
    return dt.replace(year=new_year, month=new_month, day=new_day)


def _days_in_month(year: int, month: int) -> int:
    if month in {1, 3, 5, 7, 8, 10, 12}:
        return 31
    if month in {4, 6, 9, 11}:
        return 30
    # February
    leap = (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0)
    return 29 if leap else 28


def _format_expected_month(token: datetime, month_value: int, *, absolute: bool) -> str:
    if absolute:
        # Java: set MONTH=0 (Jan), add MONTH+(N-1)
        anchor = token.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        target = _shift_months(anchor, month_value - 1)
    else:
        target = _shift_months(token, month_value)
    return target.strftime(MONTH_FORMAT)


def _parse_date_value(date_str: str) -> tuple[int, bool, bool]:
    """从 triggerCondition 的单个 date 值里提取:
      - 数值 (小时/天/星期/月). 特殊标记 `#$` / `@$` 取 0
      - 是不是 self (%$)
      - 是不是 context-based (*$)

    解析失败时返回 (0, is_self, is_context),调用方应继续处理.
    """
    is_self = DATE_RELY_ON_ONESELF in date_str
    is_context = DATE_OF_CALCULATION_CONTEXT in date_str
    # 纯标记符,不带数值
    if date_str.strip() in {DAY_OF_ANY_MONTH, DAY_OF_LAST_MONTH}:
        return 0, is_self, is_context
    cleaned = _strip_date_marker(date_str)
    try:
        return int(cleaned), is_self, is_context
    except (ValueError, TypeError):
        return 0, is_self, is_context


def _build_expected_tokens(
    info: TriggerConditionInfo,
    token_date: datetime,
) -> list[dict[str, Any]]:
    """根据 conditionInfo 与 token_date 生成期望的 time_hour 候选列表.

    端口自 Java: AbstractTriggerDownstream.checkHourCondition / checkDayCondition /
                  checkTokenHourCondition / checkTokenDayCondition 等.
    每个候选项是 dict: {expected, raw_date, date_value, is_self, is_context}.
    """
    expected: list[dict[str, Any]] = []
    for d in info.date_list:
        try:
            value, is_self, is_context = _parse_date_value(d)
        except ValueError:
            continue
        if info.date_type == "HOUR":
            expected.append(
                {
                    "raw_date": d,
                    "date_value": value,
                    "is_self": is_self,
                    "is_context": is_context,
                    "expected": _format_expected_hour(token_date, value, absolute=not is_self and not is_context),
                }
            )
        elif info.date_type == "DAY":
            if DAY_OF_LAST_MONTH in d:
                if token_date.month == 12:
                    next_month_first = token_date.replace(year=token_date.year + 1, month=1, day=1)
                else:
                    next_month_first = token_date.replace(month=token_date.month + 1, day=1)
                last_day = (next_month_first - timedelta(days=1)).day
                anchor = token_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                expected.append(
                    {
                        "raw_date": d,
                        "date_value": last_day,
                        "is_self": False,
                        "is_context": False,
                        "expected": (anchor + timedelta(days=last_day - 1)).strftime(DAY_FORMAT),
                    }
                )
            elif DAY_OF_ANY_MONTH in d:
                expected.append(
                    {
                        "raw_date": d,
                        "date_value": token_date.day,
                        "is_self": False,
                        "is_context": False,
                        "expected": token_date.strftime(DAY_FORMAT),
                    }
                )
            elif is_self or is_context:
                expected.append(
                    {
                        "raw_date": d,
                        "date_value": value,
                        "is_self": is_self,
                        "is_context": is_context,
                        "expected": _format_expected_day(token_date, value, absolute=False),
                    }
                )
            else:
                expected.append(
                    {
                        "raw_date": d,
                        "date_value": value,
                        "is_self": False,
                        "is_context": False,
                        "expected": _format_expected_day(token_date, value, absolute=True),
                    }
                )
        elif info.date_type == "WEEK":
            expected.append(
                {
                    "raw_date": d,
                    "date_value": value,
                    "is_self": False,
                    "is_context": False,
                    "expected": _format_expected_week(token_date, value),
                }
            )
        elif info.date_type == "MONTH":
            if is_self or is_context:
                expected.append(
                    {
                        "raw_date": d,
                        "date_value": value,
                        "is_self": is_self,
                        "is_context": is_context,
                        "expected": _format_expected_month(token_date, value, absolute=False),
                    }
                )
            else:
                expected.append(
                    {
                        "raw_date": d,
                        "date_value": value,
                        "is_self": False,
                        "is_context": False,
                        "expected": _format_expected_month(token_date, value, absolute=True),
                    }
                )
    return expected


def _match_run_against_expected(
    run_time_hour: str | None,
    expected_list: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """给定一次上游构建的 time_hour 参数和期望列表,判断是否命中."""
    if not run_time_hour:
        return None
    try:
        run_date = _parse_time_hour_token(run_time_hour)
    except ValueError:
        return None
    for exp in expected_list:
        expected_str = exp["expected"]
        if len(expected_str) == len("YYYY/MM/DD/HH"):
            cmp_value = run_date.strftime(TIME_HOUR_FORMAT)
        elif len(expected_str) == len("YYYY/MM/DD"):
            cmp_value = run_date.strftime(DAY_FORMAT)
        elif len(expected_str) == len("YYYY/MM"):
            cmp_value = run_date.strftime(MONTH_FORMAT)
        else:
            cmp_value = run_date.strftime(TIME_HOUR_FORMAT)
        if expected_str == cmp_value:
            return exp
    return None


def _threshold_met(threshold: str, result: str | None) -> bool:
    """判定 build result 是否满足 threshold.

    threshold ∈ {SUCCESS, UNSTABLE, FAILED}, 与 Java ResultCondition 对应.
    Java 侧: SUCCESS → SUCCESS/UNSTABLE 都算; UNSTABLE → UNSTABLE/FAILED; FAILED → FAILED.
    """
    if not result:
        return False
    if threshold == "SUCCESS":
        return result in {"SUCCESS", "UNSTABLE"}
    if threshold == "UNSTABLE":
        return result in {"UNSTABLE", "FAILURE", "FAILED"}
    if threshold == "FAILED":
        return result in {"FAILURE", "FAILED", "ABORTED"}
    return result == threshold


# ---------------------------------------------------------------------------
# 6 个 MCP 工具入口
# ---------------------------------------------------------------------------


def parse_trigger_condition_tool(
    *,
    condition_str: str,
) -> dict[str, Any]:
    """工具: blf_parse_trigger_condition"""
    try:
        if not isinstance(condition_str, str) or not condition_str.strip():
            raise ValueError("condition_str 不能为空")
        info = parse_trigger_condition(condition_str)
        return {
            "success": True,
            "summary": {
                "condition_str": condition_str.strip(),
                "date_type": info.date_type,
                "logic_symbol": info.logic_symbol,
                "date_list": info.date_list,
            },
            "evidence": {"source": "job-dependency-plugin TriggerConditionParser.java"},
        }
    except Exception as exc:
        return _error_response(exc, condition_str=condition_str if isinstance(condition_str, str) else "")


def parse_upstream_job_params_tool(
    *,
    params_str: str,
) -> dict[str, Any]:
    """工具: blf_parse_upstream_job_params"""
    try:
        params = parse_upstream_job_params(params_str)
        return {
            "success": True,
            "summary": {"params_str": params_str, "count": len(params), "params": params},
            "evidence": {"source": "job-dependency-plugin UpstreamJobParams.getJobParams"},
        }
    except Exception as exc:
        return _error_response(exc, params_str=params_str if isinstance(params_str, str) else "")


def format_time_hour_token_tool(
    *,
    token: str,
    date_type: str,
) -> dict[str, Any]:
    """工具: blf_format_time_hour_token"""
    try:
        result = format_time_hour_token(token, date_type)
        return {
            "success": True,
            "summary": result,
            "evidence": {"source": "job-dependency-plugin getOnlyBuildOnceFormatStringContext + DateUtils"},
        }
    except Exception as exc:
        return _error_response(exc)


def check_upstream_time_hour_match(
    client: JenkinsClient,
    *,
    upstream_job: str,
    time_hour: str,
    trigger_condition: str = "",
    build_limit: int = 20,
    history_limit: int = 30,
) -> dict[str, Any]:
    """工具: blf_check_upstream_time_hour_match

    给定上游任务 + 当前下游的 time_hour + triggerCondition,
    列出该上游最近 N 个构建,逐个说明 time_hour 是否满足 condition.
    """
    try:
        if not isinstance(upstream_job, str) or not upstream_job.strip():
            raise ValueError("upstream_job 不能为空")
        upstream = upstream_job.strip()
        if not isinstance(time_hour, str) or not time_hour.strip():
            raise ValueError("time_hour 不能为空")
        build_limit = _bounded_int(build_limit, default=20, minimum=1, maximum=50)
        history_limit = _bounded_int(history_limit, default=30, minimum=1, maximum=50)

        token_date = _parse_time_hour_token(time_hour)
        upstream_history = client.get_build_history(upstream, history_limit)
        candidate_builds = upstream_history[:history_limit]

        expected_list: list[dict[str, Any]] = []
        if trigger_condition and trigger_condition.strip():
            try:
                info = parse_trigger_condition(trigger_condition)
                expected_list = _build_expected_tokens(info, token_date)
            except ValueError as exc:
                return {
                    "success": False,
                    "error_type": "invalid_input",
                    "message": f"trigger_condition 解析失败: {exc}",
                    "summary": {},
                }

        evaluated: list[dict[str, Any]] = []
        match_count = 0
        success_match_count = 0
        for build in candidate_builds[:build_limit]:
            number = build.get("number")
            params = client.get_build_parameters(upstream, number) if number is not None else {}
            run_time_hour = params.get(TIME_HOUR_TOKEN)
            matched_exp: dict[str, Any] | None = None
            if expected_list:
                matched_exp = _match_run_against_expected(run_time_hour, expected_list)
            result = build.get("result")
            condition_match = matched_exp is not None
            if condition_match:
                match_count += 1
                if _threshold_met("SUCCESS", result):
                    success_match_count += 1
            evaluated.append(
                {
                    "build_number": number,
                    "result": result,
                    "time_hour": run_time_hour,
                    "is_building": bool(build.get("building")),
                    "condition_match": condition_match,
                    "matched_expected": matched_exp.get("raw_date") if matched_exp else None,
                    "threshold_met_success": _threshold_met("SUCCESS", result) if condition_match else None,
                }
            )

        risks: list[str] = []
        if trigger_condition and not evaluated:
            risks.append(f"上游 {upstream} 没有可用的构建历史")
        if expected_list and match_count == 0:
            risks.append(f"上游 {upstream} 最近 {len(evaluated)} 次构建里没有任何一次 time_hour 命中条件")
        elif expected_list and success_match_count == 0:
            risks.append(
                f"上游 {upstream} 有 {match_count} 次构建 time_hour 命中,但 result 都不满足 SUCCESS/UNSTABLE,"
                " 因此无法触发下游"
            )

        return {
            "success": True,
            "summary": {
                "upstream_job": upstream,
                "time_hour": time_hour,
                "trigger_condition": trigger_condition or None,
                "expected": expected_list,
                "evaluated_builds": evaluated,
                "match_count": match_count,
                "success_match_count": success_match_count,
            },
            "risks": risks,
            "evidence": {
                "source": "job-dependency-plugin AbstractTriggerDownstream.matchTokenFromBuildHistory",
                "interfaces": [
                    "Jenkins /job/{name}/api/json?tree=builds[...]",
                    "Jenkins /job/{name}/{build}/api/json?tree=actions[parameters[name,value]]",
                ],
            },
        }
    except Exception as exc:
        try:
            base = _job_base(upstream_job)
        except Exception:
            base = {"upstream_job": upstream_job}
        return _error_response(exc, **base)


def diagnose_dependency_trigger(
    client: JenkinsClient,
    *,
    downstream_job: str,
    time_hour: str,
    upstream_job: str,
    trigger_condition: str = "",
    threshold: str = "SUCCESS",
    build_ref: int | str = "lastBuild",
) -> dict[str, Any]:
    """工具: blf_diagnose_dependency_trigger

    综合诊断"为什么上游 A 跑完后没触发下游 B", 复刻 JobDependencyBuildTrigger.shouldTriggerBuild
    的三道闸门, 逐道说明是哪个闸门把链路卡住了.
    """
    try:
        if not isinstance(downstream_job, str) or not downstream_job.strip():
            raise ValueError("downstream_job 不能为空")
        if not isinstance(upstream_job, str) or not upstream_job.strip():
            raise ValueError("upstream_job 不能为空")
        if not isinstance(time_hour, str) or not time_hour.strip():
            raise ValueError("time_hour 不能为空")

        downstream = downstream_job.strip()
        upstream = upstream_job.strip()
        ref = _validate_build_ref(build_ref)

        upstream_build = client.get_build_info(upstream, ref)
        if not upstream_build:
            return {
                "success": False,
                "error_type": "not_found",
                "message": f"未找到上游任务 {upstream} 的构建 {ref}",
                "summary": {},
            }
        upstream_result = upstream_build.get("result")
        upstream_number = upstream_build.get("number")

        gate1_passed = _threshold_met(threshold, upstream_result)
        gate1 = {
            "name": "result_threshold",
            "description": f"上游 {upstream} #{upstream_number} 的 result={upstream_result},threshold={threshold}",
            "passed": gate1_passed,
            "expected": threshold,
            "actual": upstream_result,
        }

        gate2_passed = True
        gate2_evidence: dict[str, Any] = {"queue_items": []}
        try:
            queue_items = client.get_queue_items()
            same_time_hour_items: list[dict[str, Any]] = []
            for item in queue_items:
                if item.get("job_display_name") != downstream:
                    continue
                queue_id = item.get("queue_id")
                if not queue_id:
                    continue
                params = client.get_build_parameters(downstream, queue_id)
                token = params.get(TIME_HOUR_TOKEN)
                same_time_hour_items.append(
                    {"queue_id": queue_id, "time_hour": token, "blocked": item.get("blocked"), "buildable": item.get("buildable")}
                )
                if token and token == time_hour.strip():
                    gate2_passed = False
            gate2_evidence["queue_items"] = same_time_hour_items
        except JenkinsClientError as exc:
            gate2_evidence["error"] = str(exc)
        gate2 = {
            "name": "queue_dedup",
            "description": "下游是否已在队列里存在同 time_hour 的排队项(对应 Java onlyBuildOnce 防重触发)",
            "passed": gate2_passed,
            "evidence": gate2_evidence,
        }

        gate3_passed = False
        gate3_evidence: dict[str, Any] = {}
        if not trigger_condition or not trigger_condition.strip():
            upstream_history = client.get_build_history(upstream, 30)
            for b in upstream_history:
                if b.get("number") == upstream_number:
                    continue
                params = client.get_build_parameters(upstream, b.get("number"))
                if params.get(TIME_HOUR_TOKEN) == time_hour.strip() and _threshold_met("SUCCESS", b.get("result")):
                    gate3_passed = True
                    gate3_evidence = {
                        "strategy": "direct_time_hour_match",
                        "matched_build_number": b.get("number"),
                        "matched_result": b.get("result"),
                    }
                    break
            if not gate3_passed:
                gate3_evidence.setdefault("strategy", "direct_time_hour_match")
                gate3_evidence.setdefault("hint", "上游最近 30 次构建里没有 time_hour 匹配且 result=SUCCESS 的构建")
        else:
            try:
                info = parse_trigger_condition(trigger_condition)
            except ValueError as exc:
                return {
                    "success": False,
                    "error_type": "invalid_input",
                    "message": f"trigger_condition 解析失败: {exc}",
                    "summary": {"gate1": gate1, "gate2": gate2},
                }
            token_date = _parse_time_hour_token(time_hour)
            expected_list = _build_expected_tokens(info, token_date)
            upstream_history = client.get_build_history(upstream, 30)
            matched_any = False
            matched_success = False
            evaluated_count = 0
            for b in upstream_history:
                if b.get("number") == upstream_number:
                    continue
                evaluated_count += 1
                params = client.get_build_parameters(upstream, b.get("number"))
                run_time_hour = params.get(TIME_HOUR_TOKEN)
                matched_exp = _match_run_against_expected(run_time_hour, expected_list)
                if matched_exp:
                    matched_any = True
                    if _threshold_met("SUCCESS", b.get("result")):
                        matched_success = True
                        gate3_evidence = {
                            "strategy": "condition_match",
                            "matched_build_number": b.get("number"),
                            "matched_result": b.get("result"),
                            "matched_date": matched_exp.get("raw_date"),
                        }
                        break
            gate3_passed = matched_success
            gate3_evidence.update(
                {
                    "strategy": "condition_match",
                    "expected": expected_list,
                    "evaluated_count": evaluated_count,
                    "any_time_hour_match": matched_any,
                }
            )

        gate3 = {
            "name": "upstream_condition_match",
            "description": f"上游 {upstream} 历史构建里是否有时 time_hour + condition + result 都满足的 Run",
            "passed": gate3_passed,
            "evidence": gate3_evidence,
        }

        overall_passed = gate1_passed and gate2_passed and gate3_passed

        recommendations: list[str] = []
        if not gate1_passed:
            recommendations.append(
                f"闸门 1 失败: 上游 #{upstream_number} 构建结果 {upstream_result} 不满足 threshold {threshold}."
                " 即便是 task 触发链路一切正常,也不会向下游触发."
            )
        if gate1_passed and not gate2_passed:
            recommendations.append(
                "闸门 2 失败: 下游队列里已存在同 time_hour 的排队项,被 onlyBuildOnce 防重触发拦截."
                " 如果确实需要重复触发,考虑下游任务的 onlyBuildOnce 配置或手动取消队列中的旧项."
            )
        if gate1_passed and gate2_passed and not gate3_passed:
            recommendations.append(
                "闸门 3 失败: 上游历史里没有满足 triggerCondition + result 的构建."
                " 排查方向: (1) 用 blf_check_upstream_time_hour_match 看上游最近的 time_hour 实际值;"
                " (2) 确认 triggerCondition 格式合法(用 blf_parse_trigger_condition);"
                " (3) 检查上游 result 是否达到 SUCCESS/UNSTABLE."
            )

        return {
            "success": True,
            "summary": {
                "downstream_job": downstream,
                "upstream_job": upstream,
                "upstream_build_number": upstream_number,
                "upstream_result": upstream_result,
                "time_hour": time_hour,
                "trigger_condition": trigger_condition or None,
                "threshold": threshold,
                "gates": [gate1, gate2, gate3],
                "overall_passed": overall_passed,
                "recommendations": recommendations,
            },
            "risks": recommendations,
            "evidence": {
                "source": "job-dependency-plugin JobDependencyBuildTrigger.shouldTriggerBuild 三道闸门",
            },
        }
    except Exception as exc:
        return _error_response(exc)


def is_user_triggered_build(
    client: JenkinsClient,
    *,
    job_display_name: str,
    build_ref: int | str = "lastBuild",
    max_depth: int = 20,
) -> dict[str, Any]:
    """工具: blf_is_user_triggered_build

    递归向上游检查 CauseAction 链, 判断这次构建最终是否由用户手动触发.
    端口自 Java: BuildTriggerUtils.isTriggerByUser
    """
    try:
        if not isinstance(job_display_name, str) or not job_display_name.strip():
            raise ValueError("job_display_name 不能为空")
        ref = _validate_build_ref(build_ref)
        max_depth = _bounded_int(max_depth, default=20, minimum=1, maximum=100)

        visited: set[tuple[str, str]] = set()
        chain: list[dict[str, Any]] = []
        current_name = job_display_name.strip()
        current_ref: int | str = ref
        is_user = False
        depth = 0

        while depth < max_depth:
            key = (current_name, str(current_ref))
            if key in visited:
                chain.append({"depth": depth, "note": f"已访问过 {key},停止递归以防环"})
                break
            visited.add(key)

            build = client.get_build_info(current_name, current_ref)
            if not build:
                chain.append({"depth": depth, "job": current_name, "build": current_ref, "note": "构建不存在"})
                break

            actions = build.get("actions") or []
            cause_summary: list[dict[str, Any]] = []
            user_id_found = False
            next_upstream: tuple[str, int | str] | None = None
            for action in actions:
                if not isinstance(action, dict):
                    continue
                _class = action.get("_class") or ""
                if not _class.endswith("CauseAction"):
                    continue
                causes = action.get("causes") or []
                if not isinstance(causes, list):
                    continue
                for cause in causes:
                    if not isinstance(cause, dict):
                        continue
                    short = cause.get("shortDescription") or ""
                    cause_class = cause.get("_class") or ""
                    cause_summary.append(
                        {"class": cause_class.split(".")[-1], "shortDescription": short}
                    )
                    if cause_class.endswith("UserIdCause") or cause_class.endswith("UserCause"):
                        user_id_found = True
                    elif cause_class.endswith("UpstreamCause"):
                        upstream_info = _parse_upstream_cause(short)
                        if upstream_info:
                            next_upstream = (upstream_info["job"], upstream_info["build_number"])

            chain.append(
                {
                    "depth": depth,
                    "job": current_name,
                    "build": current_ref,
                    "result": build.get("result"),
                    "causes": cause_summary,
                    "user_id_found_at_this_level": user_id_found,
                }
            )
            if user_id_found:
                is_user = True
                break
            if next_upstream is None:
                break
            current_name, current_ref = next_upstream
            depth += 1

        return {
            "success": True,
            "summary": {
                "job_display_name": job_display_name,
                "build_ref": build_ref,
                "is_triggered_by_user": is_user,
                "cause_chain": chain,
            },
            "evidence": {"source": "job-dependency-plugin BuildTriggerUtils.isTriggerByUser"},
        }
    except Exception as exc:
        try:
            base = _job_base(job_display_name)
        except Exception:
            base = {"job_display_name": job_display_name}
        return _error_response(exc, **base)


def _parse_upstream_cause(short_description: str) -> dict[str, Any] | None:
    """解析 UpstreamCause 的 shortDescription, 提取上游 job 名和构建号.

    端口自 Java: UpstreamTriggerRun.getRunForUpstreamJobInfo
    注意: Jenkins 在 build number 较大时会插入千分位逗号(如 24,626),
    这里需要兼容.
    """
    m = re.search(r'upstream project\s+"?([^"\s]+?)"?\s+build number\s+([\d,]+)', short_description)
    if m:
        return {"job": m.group(1), "build_number": int(m.group(2).replace(",", ""))}
    m = re.search(r'上游任务\s+"?([^"\s]+?)"?\s+构建\s+([\d,]+)', short_description)
    if m:
        return {"job": m.group(1), "build_number": int(m.group(2).replace(",", ""))}
    return None
