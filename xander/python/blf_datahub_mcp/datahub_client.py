"""Small read-only DataHub HTTP client for BLF MCP tools."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class DataHubClientError(RuntimeError):
    """Raised when DataHub returns an error response."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class DataHubClient:
    """Read-only DataHub GraphQL/OpenAPI client."""

    gms_url: str
    token: str | None = None
    timeout_sec: int = 60

    def _headers(self, *, content_type: bool = False) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute a read-only GraphQL query."""
        if "mutation" in query.lower():
            raise ValueError("mutations are not allowed in BLF DataHub MCP")
        body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.gms_url.rstrip('/')}/api/graphql",
            data=body,
            method="POST",
            headers=self._headers(content_type=True),
        )
        payload = self._open_json(req)
        errors = payload.get("errors")
        if errors:
            raise DataHubClientError(f"GraphQL errors: {errors}")
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def get_openapi(self, path: str) -> dict[str, Any]:
        """Execute a read-only OpenAPI GET request."""
        req = urllib.request.Request(
            f"{self.gms_url.rstrip('/')}{path}",
            method="GET",
            headers=self._headers(),
        )
        payload = self._open_json(req)
        return payload if isinstance(payload, dict) else {}

    def get_dataset_aspects(
        self,
        dataset_urn: str,
        aspects: list[str],
    ) -> dict[str, Any]:
        """Fetch selected dataset aspects using OpenAPI."""
        encoded = urllib.parse.quote(dataset_urn, safe="")
        aspect_query = urllib.parse.urlencode({"aspects": ",".join(aspects)})
        return self.get_openapi(f"/openapi/v3/entity/dataset/{encoded}?{aspect_query}")

    def get_structured_properties(self, dataset_urn: str) -> dict[str, Any]:
        """Fetch structuredProperties for a dataset."""
        encoded = urllib.parse.quote(dataset_urn, safe="")
        return self.get_openapi(
            f"/openapi/v3/entity/dataset/{encoded}/structuredProperties"
        )

    def _open_json(self, req: urllib.request.Request) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise DataHubClientError(
                f"DataHub HTTP {exc.code}: {detail}",
                status_code=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise DataHubClientError(f"DataHub request failed: {exc}") from exc
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DataHubClientError("DataHub returned non-JSON response") from exc
        return payload if isinstance(payload, dict) else {"value": payload}

