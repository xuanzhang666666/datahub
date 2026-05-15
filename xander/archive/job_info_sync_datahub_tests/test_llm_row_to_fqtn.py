"""llm_row_to_fqtn 单元测试。"""

from __future__ import annotations

from job_info_sync_datahub.lineage_write_policy import llm_row_to_fqtn


def test_llm_row_default_db_bare_table() -> None:
    assert llm_row_to_fqtn({"table": "pdw_logs_v1"}) == "default.pdw_logs_v1"


def test_llm_row_explicit_db() -> None:
    assert llm_row_to_fqtn({"db": "default", "table": "dw_order_v1"}) == "default.dw_order_v1"


def test_llm_row_embedded_schema_table() -> None:
    assert llm_row_to_fqtn({"table": "default.dw_order_v1"}) == "default.dw_order_v1"


def test_llm_row_ambiguous_returns_none() -> None:
    assert llm_row_to_fqtn({"db": "default", "table": "a.b"}) is None
    assert llm_row_to_fqtn({"table": "a.b.c"}) is None
