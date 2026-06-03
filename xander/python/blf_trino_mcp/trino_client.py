"""Small Trino DB-API wrapper for BLF Trino MCP."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TrinoConfig:
    host: str = "10.253.7.167"
    port: int = 8081
    user: str = "xuan.zhang"
    catalog: str = "hive"
    schema: str = "default"


class TrinoClientError(RuntimeError):
    """Trino query error."""


class TrinoClient:
    """Thin wrapper around Python trino DB-API."""

    def __init__(self, config: TrinoConfig) -> None:
        self.config = config

    def query(self, sql: str) -> dict[str, Any]:
        """Execute SQL and return columns and rows."""
        try:
            import trino  # type: ignore[import-not-found]
        except ImportError as exc:
            raise TrinoClientError(
                "Missing dependency: trino. Install it with: python3 -m pip install trino"
            ) from exc

        conn = None
        cur = None
        try:
            conn = trino.dbapi.connect(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                catalog=self.config.catalog,
                schema=self.config.schema,
            )
            cur = conn.cursor()
            cur.execute(sql)
            rows = cur.fetchall()
            columns = [
                item[0]
                for item in (cur.description or [])
                if isinstance(item, (tuple, list)) and item
            ]
            return {"columns": columns, "rows": rows}
        except Exception as exc:
            raise TrinoClientError(str(exc)) from exc
        finally:
            if cur is not None:
                cur.close()
            if conn is not None:
                conn.close()
