"""hive_fqtn_validation 单元测试。"""

from __future__ import annotations

from job_info_sync_datahub.hive_fqtn_validation import (
    filter_table_lineages_by_hive_fqtn_rules,
    is_valid_hive_fqtn,
)
from job_info_sync_datahub.models import TableLineage, TableRef


def test_is_valid_table_prefix_default_dw() -> None:
    assert is_valid_hive_fqtn("default.dw_order_v1") == (True, "")


def test_is_valid_rejects_tmp_table() -> None:
    assert is_valid_hive_fqtn("default.tmp_330") == (False, "table_not_layer_prefix")


def test_is_valid_rejects_dw_v1_one_underscore() -> None:
    assert is_valid_hive_fqtn("default.dw_v1") == (False, "table_underscore_lt2")


def test_dwa_before_dw_prefix() -> None:
    assert is_valid_hive_fqtn("default.dwa_sales_x") == (True, "")


def test_rejects_db_not_in_allowlist() -> None:
    assert is_valid_hive_fqtn("pdw.dw_order_v1")[0] is False


def test_is_valid_structure() -> None:
    assert is_valid_hive_fqtn("")[0] is False
    assert is_valid_hive_fqtn("default")[0] is False
    assert is_valid_hive_fqtn("default.")[0] is False
    assert is_valid_hive_fqtn(".dw_t")[0] is False
    assert is_valid_hive_fqtn("a.b.c")[0] is False


def test_filter_table_lineages() -> None:
    tl = TableLineage(
        target=TableRef(db="default", table="dw_order_v1"),
        upstreams=[
            TableRef(db="default", table="ods_src_x_y"),
            TableRef(db="default", table="tmp_1_2"),
        ],
        source_block_indices=[],
    )
    kept, meta = filter_table_lineages_by_hive_fqtn_rules([tl])
    assert len(kept) == 1
    assert kept[0].target.full_name == "default.dw_order_v1"
    assert [u.full_name for u in kept[0].upstreams] == ["default.ods_src_x_y"]
    assert len(meta["stripped_invalid_upstreams"]) == 1
