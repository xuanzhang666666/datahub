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


def test_evaluate_documentation_lineage_skips_when_section_missing() -> None:
    lineages, decision, _raw = evaluate_documentation_lineage(
        "data_takeaway.pdw_order_target_table_di",
        "### 1. 表用途概览\n无上游",
    )
    assert lineages == []
    assert decision.status == "SKIP_NO_DOC_SECTION"
    assert decision.write_upstream_lineage is False
