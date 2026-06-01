"""Documentation section 4 → table lineage extraction."""

from __future__ import annotations

import os
from unittest.mock import patch

from job_info_sync_datahub.lineage_write_policy import evaluate_documentation_lineage


def test_evaluate_documentation_lineage_parses_markdown_table() -> None:
    documentation = """
### 4. 数据来源

| 上游表 | 用途 |
| --- | --- |
| `default.ods_order_source_di` | 订单 |
| `data_takeaway.pdw_logistics_management_distribution_view` | 物流 |
"""
    with patch.dict(os.environ, {"BLF_LINEAGE_SKIP_HIVE_EXISTENCE_CHECK": "1"}):
        lineages, decision, raw = evaluate_documentation_lineage(
            "data_takeaway.pdw_order_target_table_di",
            documentation,
        )

    assert raw["source"] == "documentation"
    assert decision.status == "DOC_EXTRACTED"
    assert decision.write_upstream_lineage is True
    assert len(lineages) == 1
    assert lineages[0].target.full_name == "data_takeaway.pdw_order_target_table_di"
    upstreams = {u.full_name for u in lineages[0].upstreams}
    assert upstreams == {
        "default.ods_order_source_di",
        "data_takeaway.pdw_logistics_management_distribution_view",
    }


def test_evaluate_documentation_lineage_parses_escaped_markdown_table_cells() -> None:
    documentation = """
### 4\\. 数据来源

| 上游表 | 用途 |
| --- | --- |
| data\_md.dm\_md\_dim\_base\_sku\_info\_base\_sku\_v1 | 商品 |
"""
    with patch.dict(os.environ, {"BLF_LINEAGE_SKIP_HIVE_EXISTENCE_CHECK": "1"}):
        lineages, decision, _raw = evaluate_documentation_lineage(
            "data_md.dm_md_features_base_sku_tag_di_v2",
            documentation,
        )

    assert decision.status == "DOC_EXTRACTED"
    assert {u.full_name for u in lineages[0].upstreams} == {
        "data_md.dm_md_dim_base_sku_info_base_sku_v1",
    }


def test_evaluate_documentation_lineage_parses_escaped_section_four_heading() -> None:
    documentation = """
### 4\\. 数据来源

| 上游表 | 用途 |
| --- | --- |
| `data_md.dm_md_dim_base_sku_info_base_sku_v1` | 商品 |
"""
    with patch.dict(os.environ, {"BLF_LINEAGE_SKIP_HIVE_EXISTENCE_CHECK": "1"}):
        lineages, decision, _raw = evaluate_documentation_lineage(
            "data_md.dm_md_features_base_sku_tag_di_v2",
            documentation,
        )

    assert decision.status == "DOC_EXTRACTED"
    assert len(lineages) == 1
    assert {u.full_name for u in lineages[0].upstreams} == {
        "data_md.dm_md_dim_base_sku_info_base_sku_v1",
    }


def test_evaluate_documentation_lineage_drops_missing_upstream_in_hive() -> None:
    documentation = """
### 4. 数据来源

| 上游表 | 用途 |
| --- | --- |
| `default.ods_exists` | ok |
| `default.ods_missing` | ghost |
"""
    existing = {
        "data_takeaway.pdw_order_target_table_di",
        "default.ods_exists",
    }
    with patch(
        "job_info_sync_datahub.hive_table_existence.query_hive_existing_fqtns",
        return_value=existing,
    ):
        lineages, decision, _raw = evaluate_documentation_lineage(
            "data_takeaway.pdw_order_target_table_di",
            documentation,
        )

    assert decision.status == "DOC_EXTRACTED"
    assert decision.write_upstream_lineage is True
    assert {u.full_name for u in lineages[0].upstreams} == {"default.ods_exists"}
    hive_meta = decision.hive_existence or {}
    assert hive_meta.get("stripped_upstreams")
    assert "default.ods_missing" in hive_meta["stripped_upstreams"][0]["missing_upstreams"]


def test_evaluate_documentation_lineage_skips_when_section_missing() -> None:
    lineages, decision, _raw = evaluate_documentation_lineage(
        "data_takeaway.pdw_order_target_table_di",
        "### 1. 表用途概览\n无上游",
    )
    assert lineages == []
    assert decision.status == "SKIP_NO_DOC_SECTION"
    assert decision.write_upstream_lineage is False
