"""BLF Trino MCP tool implementations."""

from __future__ import annotations

import re
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from .ddl_unicode import maybe_decode_show_create_ddl
from .hive import (
    MAX_QUERY_ROWS,
    apply_limit,
    ensure_read_only_sql,
    partitions_table_sql,
    qualified_table_sql,
    split_hive_table,
)
from .trino_client import TrinoClient, TrinoClientError


def _error_response(error: Exception, **extra: Any) -> dict[str, Any]:
    error_type = "trino_error"
    if isinstance(error, ValueError):
        error_type = "invalid_input"
    elif isinstance(error, TrinoClientError):
        message = str(error).lower()
        if "not found" in message or "does not exist" in message:
            error_type = "not_found"
        elif "access denied" in message or "permission" in message:
            error_type = "forbidden"
        elif "syntax" in message:
            error_type = "syntax_error"
    return {
        "success": False,
        "error_type": error_type,
        "message": str(error),
        **extra,
    }


def _rows_as_dicts(columns: list[str], rows: list[Any]) -> list[dict[str, Any]]:
    items = []
    for row in rows:
        values = list(row) if isinstance(row, (tuple, list)) else [row]
        safe_values = [_json_safe_value(value) for value in values]
        if columns and len(columns) == len(values):
            items.append(dict(zip(columns, safe_values)))
        else:
            items.append({"row": safe_values})
    return items


def _json_safe_value(value: Any) -> Any:
    """Convert Trino DB-API values to JSON-serializable values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _query_with_limit(
    client: TrinoClient,
    sql: str,
    *,
    row_limit: int,
) -> dict[str, Any]:
    final_sql = apply_limit(sql, row_limit)
    payload = client.query(final_sql)
    columns = [str(item) for item in payload.get("columns") or []]
    rows = list(payload.get("rows") or [])
    max_rows = min(max(int(row_limit), 1), MAX_QUERY_ROWS)
    returned = rows[:max_rows]
    return {
        "sql": final_sql,
        "columns": columns,
        "rows": _rows_as_dicts(columns, returned),
        "row_count": len(returned),
        "truncated": len(rows) > len(returned),
    }


def get_hive_table_ddl(
    client: TrinoClient,
    *,
    table: str,
    raw_ddl: bool = False,
) -> dict[str, Any]:
    """Return SHOW CREATE TABLE output."""
    try:
        database, table_name = split_hive_table(table)
        sql = f"SHOW CREATE TABLE {qualified_table_sql(table)}"
        payload = client.query(sql)
        rows = list(payload.get("rows") or [])
        ddl = rows[0][0] if rows and rows[0] else ""
        if isinstance(ddl, str) and not raw_ddl:
            ddl = maybe_decode_show_create_ddl(ddl)
        return {
            "success": True,
            "table": f"{database}.{table_name}",
            "summary": {"ddl": ddl, "raw_ddl": raw_ddl},
            "evidence": {"sql": sql, "interface": "Trino SHOW CREATE TABLE"},
        }
    except Exception as exc:
        return _error_response(exc, table=table)


def get_hive_table_partitions(
    client: TrinoClient,
    *,
    table: str,
    partition_column: str = "dt",
    where: str = "",
    limit: int = 200,
) -> dict[str, Any]:
    """Return Hive partition values via Trino $partitions virtual table."""
    try:
        database, table_name = split_hive_table(table)
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", partition_column):
            raise ValueError("partition_column must be a simple Hive identifier")
        max_rows = min(max(int(limit), 1), MAX_QUERY_ROWS)
        where_sql = f" WHERE {where.strip()}" if where.strip() else ""
        if where_sql:
            ensure_read_only_sql(f"SELECT 1 FROM x{where_sql}")
        sql = (
            f"SELECT * FROM {partitions_table_sql(table)}"
            f"{where_sql} ORDER BY {partition_column} DESC LIMIT {max_rows}"
        )
        payload = client.query(sql)
        columns = [str(item) for item in payload.get("columns") or []]
        rows = list(payload.get("rows") or [])
        return {
            "success": True,
            "table": f"{database}.{table_name}",
            "summary": {
                "partition_column": partition_column,
                "where": where,
                "limit": max_rows,
                "columns": columns,
                "partitions": _rows_as_dicts(columns, rows),
                "row_count": len(rows),
            },
            "risks": []
            if rows
            else ["未返回分区；表可能不是分区表，或 where 条件过滤后无分区"],
            "evidence": {"sql": sql, "interface": "Trino Hive $partitions"},
        }
    except Exception as exc:
        return _error_response(exc, table=table)


def query_hive_sql(
    client: TrinoClient,
    *,
    sql: str,
    row_limit: int = 100,
) -> dict[str, Any]:
    """Execute one read-only SQL statement."""
    try:
        result = _query_with_limit(client, sql, row_limit=row_limit)
        return {
            "success": True,
            "summary": result,
            "risks": ["结果已按 row_limit 截断"] if result["truncated"] else [],
            "evidence": {"interface": "Trino read-only SQL"},
        }
    except Exception as exc:
        return _error_response(exc, sql=sql)


def query_hive_sql_fragment(
    client: TrinoClient,
    *,
    sql_fragment: str,
    table: str = "",
    row_limit: int = 100,
) -> dict[str, Any]:
    """Execute a user-provided SQL fragment or full read-only SQL."""
    try:
        fragment = (sql_fragment or "").strip()
        if not fragment:
            raise ValueError("sql_fragment cannot be empty")
        if re.match(r"^\s*(select|with|show|describe|desc|explain)\b", fragment, re.I):
            sql = fragment
        else:
            if not table:
                raise ValueError("table is required when sql_fragment is not a full SQL")
            sql = f"SELECT * FROM {qualified_table_sql(table)} WHERE {fragment}"
        result = _query_with_limit(client, sql, row_limit=row_limit)
        return {
            "success": True,
            "table": table or None,
            "summary": result,
            "risks": ["结果已按 row_limit 截断"] if result["truncated"] else [],
            "evidence": {"interface": "Trino SQL fragment"},
        }
    except Exception as exc:
        return _error_response(exc, sql_fragment=sql_fragment, table=table)


def query_hive_by_natural_language(
    client: TrinoClient,
    *,
    question: str,
    table: str = "",
    generated_sql: str = "",
    row_limit: int = 100,
) -> dict[str, Any]:
    """Run SQL generated from a user question, with conservative fallback rules."""
    try:
        sql = generated_sql.strip() if generated_sql else _rule_based_sql(question, table)
        result = _query_with_limit(client, sql, row_limit=row_limit)
        risks = []
        if not generated_sql:
            risks.append("未传入 generated_sql，MCP 只使用保守规则生成 SQL")
        if result["truncated"]:
            risks.append("结果已按 row_limit 截断")
        return {
            "success": True,
            "question": question,
            "table": table or None,
            "summary": result,
            "risks": risks,
            "evidence": {
                "interface": "Trino natural language query",
                "sql_source": "generated_sql" if generated_sql else "rule_based_fallback",
            },
        }
    except Exception as exc:
        return _error_response(exc, question=question, table=table)


def _rule_based_sql(question: str, table: str) -> str:
    text = (question or "").strip().lower()
    if not table:
        table_match = re.search(
            r"\b([a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*|[a-zA-Z_][a-zA-Z0-9_]*)\b",
            text,
        )
        if table_match:
            table = table_match.group(1)
    if not table:
        raise ValueError("table or generated_sql is required for natural language query")
    table_sql = qualified_table_sql(table)
    if any(token in text for token in ("count", "数量", "多少条", "总数")):
        return f"SELECT count(*) AS cnt FROM {table_sql}"
    if any(token in text for token in ("最新分区", "最大分区", "latest partition", "max partition")):
        return f"SELECT * FROM {partitions_table_sql(table)} ORDER BY dt DESC LIMIT 1"
    limit_match = re.search(r"(?:前|top|limit)\s*(\d+)", text)
    limit = int(limit_match.group(1)) if limit_match else 20
    return f"SELECT * FROM {table_sql} LIMIT {min(max(limit, 1), 1000)}"
