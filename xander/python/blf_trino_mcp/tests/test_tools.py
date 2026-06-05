from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from blf_trino_mcp.tools import (
    get_hive_table_ddl,
    get_hive_table_partitions,
    query_hive_by_natural_language,
    query_hive_sql,
    query_hive_sql_fragment,
)


class FakeTrinoClient:
    def query(self, sql: str) -> dict[str, Any]:
        if sql.startswith("SHOW CREATE TABLE"):
            return {"columns": ["Create Table"], "rows": [("CREATE TABLE x",)]}
        if "$partitions" in sql:
            return {
                "columns": ["dt"],
                "rows": [("20260602",), ("20260601",)],
            }
        if "count(*)" in sql:
            return {"columns": ["cnt"], "rows": [(12,)]}
        if "decimal_test" in sql:
            return {
                "columns": ["amount", "biz_date", "created_at", "items"],
                "rows": [
                    (
                        Decimal("123.45"),
                        date(2026, 6, 5),
                        datetime(2026, 6, 5, 12, 30, 1),
                        [Decimal("1.20"), {"dt": date(2026, 6, 4)}],
                    )
                ],
            }
        return {"columns": ["id"], "rows": [(1,), (2,)]}


def test_get_hive_table_ddl_returns_ddl() -> None:
    result = get_hive_table_ddl(FakeTrinoClient(), table="dw_order_v1")

    assert result["success"] is True
    assert result["table"] == "default.dw_order_v1"
    assert result["summary"]["ddl"] == "CREATE TABLE x"


def test_get_hive_table_partitions_returns_rows() -> None:
    result = get_hive_table_partitions(
        FakeTrinoClient(),
        table="default.dw_order_v1",
        where="dt >= '20260601'",
    )

    assert result["success"] is True
    assert result["summary"]["partitions"][0] == {"dt": "20260602"}


def test_get_hive_table_partitions_caps_large_limit() -> None:
    result = get_hive_table_partitions(
        FakeTrinoClient(),
        table="default.dw_order_v1",
        limit=200000,
    )

    assert result["success"] is True
    assert result["summary"]["limit"] == 100000


def test_query_hive_sql_applies_limit() -> None:
    result = query_hive_sql(FakeTrinoClient(), sql="select id from t", row_limit=1)

    assert result["success"] is True
    assert result["summary"]["sql"].endswith("LIMIT 1")
    assert result["summary"]["row_count"] == 1


def test_query_hive_sql_converts_trino_values_to_json_safe_values() -> None:
    result = query_hive_sql(
        FakeTrinoClient(),
        sql="select * from decimal_test",
        row_limit=10,
    )

    assert result["success"] is True
    row = result["summary"]["rows"][0]
    assert row["amount"] == "123.45"
    assert row["biz_date"] == "2026-06-05"
    assert row["created_at"] == "2026-06-05T12:30:01"
    assert row["items"] == ["1.20", {"dt": "2026-06-04"}]
    json.dumps(result, ensure_ascii=False)


def test_query_hive_sql_fragment_wraps_where_fragment() -> None:
    result = query_hive_sql_fragment(
        FakeTrinoClient(),
        table="dw_order_v1",
        sql_fragment="dt = '20260601'",
    )

    assert result["success"] is True
    assert "WHERE dt = '20260601'" in result["summary"]["sql"]


def test_query_hive_by_natural_language_uses_generated_sql() -> None:
    result = query_hive_by_natural_language(
        FakeTrinoClient(),
        question="查数量",
        generated_sql="select count(*) from default.dw_order_v1",
    )

    assert result["success"] is True
    assert result["summary"]["rows"] == [{"cnt": 12}]
