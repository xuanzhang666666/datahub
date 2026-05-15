"""manual_upstream_lineage.parse_fqtn 单元测试（不依赖 DataHub SDK）。"""

from __future__ import annotations

import pytest

from job_info_sync_datahub.manual_upstream_lineage import parse_fqtn


def test_parse_two_part() -> None:
    r = parse_fqtn("ods.dw_order_v1")
    assert r.db == "ods"
    assert r.table == "dw_order_v1"


def test_parse_three_part() -> None:
    r = parse_fqtn("cat.db.table_x")
    assert r.db == "cat.db"
    assert r.table == "table_x"


def test_parse_single_segment_raises() -> None:
    with pytest.raises(ValueError, match="库.表"):
        parse_fqtn("dw_order_v1")


def test_parse_empty_raises() -> None:
    with pytest.raises(ValueError, match="表名为空"):
        parse_fqtn("  ")


def test_parse_urn_raises() -> None:
    with pytest.raises(ValueError, match="URN"):
        parse_fqtn("urn:li:dataset:(urn:li:dataPlatform:hive,x,PROD)")


def test_parse_dot_only_table_raises() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        parse_fqtn(".foo")


def test_parse_trailing_dot_raises() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        parse_fqtn("db.")
