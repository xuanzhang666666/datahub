"""BLF schedule Jenkins MCP tool implementations."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .jenkins_client import JenkinsClient, JenkinsClientError

JENKINS_PUBLIC_BASE_URL = "http://schedule.corp.bianlifeng.com"
DEFAULT_LOG_MAX_CHARS = 6000
MAX_LOG_MAX_CHARS = 12000
LOG_FETCH_BYTES = 65536
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
        "text": text[:max_chars],
        "chars": chars,
        "truncated": truncated,
        "omitted_chars": max(chars - max_chars, 0),
        "fetch_limit_bytes": LOG_FETCH_BYTES,
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
