"""Small stdlib DataHub GraphQL client for BLF schedule MCP."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

# Lucene query_string special characters that must be backslash-escaped so the
# user substring is treated literally inside our own `*...*` wildcard pattern.
_LUCENE_SPECIAL_CHARS = '+-=&|><!(){}[]^"~*?:\\/'

_SEARCH_QUERY = """
query searchDataJobs($input: SearchAcrossEntitiesInput!) {
  searchAcrossEntities(input: $input) {
    total
    searchResults {
      entity {
        urn
        ... on DataJob {
          jobId
          properties {
            name
          }
        }
      }
    }
  }
}
""".strip()


@dataclass(frozen=True)
class DataHubClientError(RuntimeError):
    """Raised for DataHub GraphQL API errors."""

    message: str
    status_code: int | None = None

    def __str__(self) -> str:
        return self.message


def escape_query_substring(substring: str) -> str:
    """Escape Lucene query_string special characters in a user substring."""
    escaped = []
    for char in substring:
        if char in _LUCENE_SPECIAL_CHARS:
            escaped.append("\\" + char)
        else:
            escaped.append(char)
    return "".join(escaped)


def build_structured_query(name_substring: str) -> str:
    """Build a DataHub structured (`/q`) wildcard query for dataJob name search.

    The substring is lowercased because the `name` keyword field uses a
    lowercase normalizer, while older ES versions do not normalize wildcard
    terms at query time.
    """
    escaped = escape_query_substring(name_substring.strip().lower())
    return f"/q name:*{escaped}* OR jobId:*{escaped}*"


@dataclass(frozen=True)
class DataHubClient:
    gms_url: str
    token: str | None = None
    timeout_sec: int = 30

    def search_data_jobs(self, name_substring: str, limit: int) -> dict[str, Any]:
        """Search dataJobs whose name/jobId contains the substring (like '%xx%').

        Returns {"total": int, "jobs": [{"urn", "name", "job_id"}]}.
        """
        if not isinstance(name_substring, str) or not name_substring.strip():
            raise ValueError("keyword is required")
        variables = {
            "input": {
                "types": ["DATA_JOB"],
                "query": build_structured_query(name_substring),
                "start": 0,
                "count": int(limit),
            }
        }
        payload = self._graphql(_SEARCH_QUERY, variables)
        search = (payload.get("data") or {}).get("searchAcrossEntities") or {}
        jobs: list[dict[str, Any]] = []
        for item in search.get("searchResults") or []:
            entity = (item or {}).get("entity") or {}
            properties = entity.get("properties") or {}
            jobs.append(
                {
                    "urn": entity.get("urn") or "",
                    "name": properties.get("name") or entity.get("jobId") or "",
                    "job_id": entity.get("jobId") or "",
                }
            )
        return {"total": int(search.get("total") or 0), "jobs": jobs}

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        url = self.gms_url.rstrip("/") + "/api/graphql"
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "blf-schedule-mcp/0.1",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_sec) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise DataHubClientError(
                f"DataHub GraphQL request failed with HTTP {exc.code}",
                status_code=exc.code,
            ) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise DataHubClientError(f"DataHub GraphQL request failed: {exc}") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DataHubClientError(f"Invalid JSON response from DataHub: {exc}") from exc
        if not isinstance(payload, dict):
            raise DataHubClientError("DataHub GraphQL response is not an object")
        errors = payload.get("errors")
        if errors:
            first = errors[0] if isinstance(errors, list) and errors else {}
            message = first.get("message") if isinstance(first, dict) else str(first)
            raise DataHubClientError(f"DataHub GraphQL error: {message}")
        return payload
