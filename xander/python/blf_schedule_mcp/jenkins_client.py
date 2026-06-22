"""Small stdlib Jenkins REST API client for BLF schedule MCP."""

from __future__ import annotations

import base64
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import Message
from typing import Any


@dataclass(frozen=True)
class JenkinsClientError(RuntimeError):
    """Raised for Jenkins API errors except missing builds."""

    message: str
    status_code: int | None = None

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class JenkinsClient:
    base_url: str
    username: str | None = None
    token: str | None = None
    timeout_sec: int = 30

    def get_job_info(self, job_name: str) -> dict[str, Any]:
        return self._open_json(self._job_path(job_name, "api/json"))

    def get_last_build(self, job_name: str) -> dict[str, Any]:
        return self.get_build_info(job_name, "lastBuild")

    def get_last_failed_build(self, job_name: str) -> dict[str, Any]:
        return self.get_build_info(job_name, "lastFailedBuild")

    def get_last_successful_build(self, job_name: str) -> dict[str, Any]:
        return self.get_build_info(job_name, "lastSuccessfulBuild")

    def get_build_info(self, job_name: str, build_ref: int | str) -> dict[str, Any]:
        return self._open_json(self._job_path(job_name, str(build_ref), "api/json"))

    def get_build_log(
        self,
        job_name: str,
        build_ref: int | str,
        *,
        max_bytes: int = 65536,
    ) -> str:
        max_bytes = min(max(int(max_bytes), 1024), 262144)
        try:
            return self._open_progressive_text_tail(
                self._job_path(job_name, str(build_ref), "logText/progressiveText"),
                max_bytes=max_bytes,
            )
        except JenkinsClientError:
            return self._open_text(
                self._job_path(job_name, str(build_ref), "consoleText"),
                max_bytes=max_bytes,
            )

    def get_build_history(self, job_name: str, limit: int) -> list[dict[str, Any]]:
        limit = min(max(int(limit), 1), 50)
        tree = (
            f"builds[number,result,timestamp,duration,url,building]{{0,{limit}}},"
            "lastBuild[number],lastFailedBuild[number],lastSuccessfulBuild[number]"
        )
        path = self._job_path(job_name, "api/json") + "?" + urllib.parse.urlencode({"tree": tree})
        payload = self._open_json(path)
        builds = payload.get("builds") if isinstance(payload, dict) else None
        return builds if isinstance(builds, list) else []

    def get_running_builds(self) -> list[dict[str, Any]]:
        tree = (
            "computer[displayName,"
            "executors[currentExecutable[number,fullDisplayName,timestamp,url]],"
            "oneOffExecutors[currentExecutable[number,fullDisplayName,timestamp,url]]]"
        )
        payload = self._open_json("computer/api/json?" + urllib.parse.urlencode({"tree": tree}))
        computers = payload.get("computer") if isinstance(payload, dict) else None
        if not isinstance(computers, list):
            return []
        builds: list[dict[str, Any]] = []
        for computer in computers:
            if not isinstance(computer, dict):
                continue
            display_name = str(computer.get("displayName") or "built-in")
            for executor_type, executors in (
                ("", computer.get("executors")),
                ("oneOff", computer.get("oneOffExecutors")),
            ):
                if not isinstance(executors, list):
                    continue
                for index, executor in enumerate(executors):
                    if not isinstance(executor, dict):
                        continue
                    build = _format_running_build(
                        executor.get("currentExecutable"),
                        executor=f"{display_name}#{executor_type}{index}",
                    )
                    if build:
                        builds.append(build)
        return builds

    def get_queue_items(self) -> list[dict[str, Any]]:
        tree = (
            "items[id,why,blocked,buildable,inQueueSince,"
            "task[name,url,fullDisplayName]]"
        )
        payload = self._open_json("queue/api/json?" + urllib.parse.urlencode({"tree": tree}))
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        queue_items: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            formatted = _format_queue_item(item)
            if formatted:
                queue_items.append(formatted)
        return queue_items

    def _open_json(self, path: str) -> dict[str, Any]:
        raw = self._open(path, max_bytes=262144)
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise JenkinsClientError(f"Invalid JSON response from Jenkins: {exc}") from exc
        if not isinstance(payload, dict):
            raise JenkinsClientError("Jenkins JSON response is not an object")
        return payload

    def _open_text(self, path: str, *, max_bytes: int) -> str:
        raw = self._open(path, max_bytes=max_bytes)
        return raw.decode("utf-8", errors="replace")

    def _open_progressive_text_tail(self, path: str, *, max_bytes: int) -> str:
        _, headers = self._open_with_headers(
            path + "?" + urllib.parse.urlencode({"start": 0}),
            max_bytes=1,
        )
        text_size = _header_int(headers, "X-Text-Size")
        if text_size is None:
            raise JenkinsClientError("Jenkins progressiveText response missing X-Text-Size")
        start = max(text_size - max_bytes, 0)
        raw, _ = self._open_with_headers(
            path + "?" + urllib.parse.urlencode({"start": start}),
            max_bytes=max_bytes,
        )
        return raw.decode("utf-8", errors="replace")

    def _open(self, path: str, *, max_bytes: int) -> bytes:
        raw, _ = self._open_with_headers(path, max_bytes=max_bytes)
        return raw

    def _open_with_headers(self, path: str, *, max_bytes: int) -> tuple[bytes, Message]:
        url = self._url(path)
        request = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_sec) as response:
                return response.read(max_bytes + 1)[:max_bytes], response.headers
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return b"", Message()
            raise JenkinsClientError(
                f"Jenkins API request failed with HTTP {exc.code}",
                status_code=exc.code,
            ) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise JenkinsClientError(f"Jenkins API request failed: {exc}") from exc

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": "blf-schedule-mcp/0.1"}
        if self.username and self.token:
            raw = f"{self.username}:{self.token}".encode("utf-8")
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        return headers

    def _url(self, path: str) -> str:
        return self.base_url.rstrip("/") + "/" + path.lstrip("/")

    @staticmethod
    def _job_path(job_name: str, *parts: str) -> str:
        if not job_name or not job_name.strip():
            raise ValueError("job_display_name is required")
        encoded = urllib.parse.quote(job_name.strip(), safe="")
        suffix = "/".join(urllib.parse.quote(part, safe="/") for part in parts)
        return f"job/{encoded}/{suffix}" if suffix else f"job/{encoded}"


def _header_int(headers: Message, name: str) -> int | None:
    value = headers.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _format_running_build(executable: Any, *, executor: str) -> dict[str, Any] | None:
    if not isinstance(executable, dict):
        return None
    full_display_name = str(executable.get("fullDisplayName") or "").strip()
    job_display_name = re.sub(r"\s+#\d+\s*$", "", full_display_name).strip()
    return {
        "job_display_name": job_display_name,
        "build_number": executable.get("number"),
        "started_at_ms": int(executable.get("timestamp") or 0),
        "build_url": executable.get("url") or "",
        "executor": executor,
    }


def _format_queue_item(item: dict[str, Any]) -> dict[str, Any] | None:
    task = item.get("task")
    if not isinstance(task, dict):
        return None
    job_display_name = _job_display_name_from_task(task)
    if not job_display_name:
        return None
    return {
        "queue_id": item.get("id"),
        "job_display_name": job_display_name,
        "why": str(item.get("why") or "").strip(),
        "blocked": bool(item.get("blocked")),
        "buildable": bool(item.get("buildable")),
        "in_queue_since_ms": int(item.get("inQueueSince") or 0),
        "jenkins_url": str(task.get("url") or "").strip(),
    }


def _job_display_name_from_task(task: dict[str, Any]) -> str:
    url = str(task.get("url") or "").strip()
    marker = "/job/"
    if marker in url:
        path = url.split(marker, 1)[1].strip("/")
        if path:
            parts = [part for part in path.split("/job/") if part]
            if len(parts) > 1:
                return "/".join(parts)
            return parts[0]
    full_display_name = str(task.get("fullDisplayName") or "").strip()
    if full_display_name:
        return full_display_name
    return str(task.get("name") or "").strip()
