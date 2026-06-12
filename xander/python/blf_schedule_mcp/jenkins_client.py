"""Small stdlib Jenkins REST API client for BLF schedule MCP."""

from __future__ import annotations

import base64
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
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

    def _open(self, path: str, *, max_bytes: int) -> bytes:
        url = self._url(path)
        request = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_sec) as response:
                return response.read(max_bytes + 1)[:max_bytes]
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return b""
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
