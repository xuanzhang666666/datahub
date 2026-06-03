from __future__ import annotations

import pytest

from blf_trino_mcp.hive import (
    apply_limit,
    ensure_read_only_sql,
    normalize_hive_table,
    partitions_table_sql,
    qualified_table_sql,
)


def test_normalize_hive_table_defaults_to_default_schema() -> None:
    assert normalize_hive_table("dw_order_v1") == "default.dw_order_v1"
    assert normalize_hive_table("data_sec_dw.dim_store_info") == "data_sec_dw.dim_store_info"


def test_qualified_table_sql_quotes_table_name() -> None:
    assert qualified_table_sql("default.dw_order_v1") == 'hive.default."dw_order_v1"'
    assert (
        partitions_table_sql("default.dw_order_v1")
        == 'hive.default."dw_order_v1$partitions"'
    )


def test_ensure_read_only_sql_rejects_write_and_multiple_statements() -> None:
    assert ensure_read_only_sql("select * from t") == "select * from t"
    with pytest.raises(ValueError):
        ensure_read_only_sql("drop table t")
    with pytest.raises(ValueError):
        ensure_read_only_sql("select 1; drop table t")


def test_apply_limit_adds_limit_for_select_only() -> None:
    assert apply_limit("select * from t", 10).endswith("LIMIT 10")
    assert apply_limit("show create table t", 10) == "show create table t"
    assert apply_limit("select * from t limit 5", 10) == "select * from t limit 5"


def test_apply_limit_caps_at_larger_query_limit() -> None:
    assert apply_limit("select * from t", 200000).endswith("LIMIT 100000")
