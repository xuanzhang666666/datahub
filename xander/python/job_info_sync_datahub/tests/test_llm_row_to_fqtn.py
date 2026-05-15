"""llm_row_to_fqtn 单元测试。"""

from __future__ import annotations

from job_info_sync_datahub.lineage_write_policy import (
    llm_row_to_fqtn,
    resolve_not_verified_table_alias,
)


def test_llm_row_default_db_bare_table() -> None:
    assert llm_row_to_fqtn({"table": "pdw_logs_v1"}) == "default.pdw_logs_v1"


def test_llm_row_explicit_db() -> None:
    assert llm_row_to_fqtn({"db": "default", "table": "dw_order_v1"}) == "default.dw_order_v1"


def test_llm_row_embedded_schema_table() -> None:
    assert llm_row_to_fqtn({"table": "default.dw_order_v1"}) == "default.dw_order_v1"


def test_llm_row_ambiguous_returns_none() -> None:
    assert llm_row_to_fqtn({"db": "default", "table": "a.b"}) is None
    assert llm_row_to_fqtn({"table": "a.b.c"}) is None


def test_resolve_not_verified_alias_strips_prefix() -> None:
    assert (
        resolve_not_verified_table_alias(
            "data_md.not_verified_dim_base_area_sku_sale_rank_di"
        )
        == "data_md.dim_base_area_sku_sale_rank_di"
    )


def test_resolve_not_verified_alias_unchanged_without_prefix() -> None:
    assert (
        resolve_not_verified_table_alias("data_md.dim_base_area_sku_sale_rank_di")
        == "data_md.dim_base_area_sku_sale_rank_di"
    )


def test_llm_row_not_verified_alias_resolved() -> None:
    assert llm_row_to_fqtn(
        {
            "db": "data_md",
            "table": "not_verified_dim_base_area_sku_sale_rank_di",
        }
    ) == "data_md.dim_base_area_sku_sale_rank_di"


def test_llm_row_not_verified_embedded_schema_table() -> None:
    assert llm_row_to_fqtn(
        {"table": "data_md.not_verified_dim_foo_bar_baz"}
    ) == "data_md.dim_foo_bar_baz"


def test_llm_row_normal_table_unchanged() -> None:
    assert llm_row_to_fqtn(
        {"db": "data_md", "table": "dim_base_area_sku_sale_rank_di"}
    ) == "data_md.dim_base_area_sku_sale_rank_di"


def test_llm_row_not_verified_only_prefix_returns_none() -> None:
    assert llm_row_to_fqtn({"db": "data_md", "table": "not_verified_"}) is None
