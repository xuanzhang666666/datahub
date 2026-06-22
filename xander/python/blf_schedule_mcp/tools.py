"""BLF schedule Jenkins MCP tool implementations."""

from __future__ import annotations

import re
from datetime import datetime, timezone
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
