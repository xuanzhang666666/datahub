from __future__ import annotations

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
