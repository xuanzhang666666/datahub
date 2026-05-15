"""manual_upstream_lineage：parse_fqtn（无 SDK）与 merge_and_emit（需 acryl-datahub + mock）。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from job_info_sync_datahub.manual_upstream_lineage import merge_and_emit, parse_fqtn
from job_info_sync_datahub.models import TableRef


def _has_datahub_sdk() -> bool:
    try:
        import datahub.ingestion.graph.client  # noqa: F401

        return True
    except ImportError:
        return False


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


@pytest.mark.skipif(not _has_datahub_sdk(), reason="acryl-datahub not installed")
def test_merge_raises_when_downstream_equals_upstream() -> None:
    ref = TableRef("dw", "same")
    with pytest.raises(ValueError, match="相同"):
        merge_and_emit(
            "http://127.0.0.1:8080",
            None,
            ref,
            ref,
            "blf-prod-hive",
            "PROD",
            replace=False,
            dry_run=True,
        )


@pytest.mark.skipif(not _has_datahub_sdk(), reason="acryl-datahub not installed")
def test_merge_skips_when_upstream_already_present() -> None:
    from datahub.metadata.schema_classes import DatasetLineageTypeClass, UpstreamClass, UpstreamLineageClass

    from job_info_sync_datahub.datahub_writer import make_dataset_urn_from_ref

    down = TableRef("ods", "target")
    up = TableRef("ods", "upstream")
    up_urn = make_dataset_urn_from_ref(up, "blf-prod-hive", "PROD")
    existing = UpstreamLineageClass(
        upstreams=[UpstreamClass(dataset=up_urn, type=DatasetLineageTypeClass.TRANSFORMED)]
    )
    with patch("datahub.ingestion.graph.client.DataHubGraph") as g_cls:
        g_inst = MagicMock()
        g_inst.get_aspect.return_value = existing
        g_cls.return_value = g_inst
        with patch("datahub.emitter.rest_emitter.DatahubRestEmitter") as e_cls:
            e_inst = MagicMock()
            e_cls.return_value = e_inst
            wrote, msg = merge_and_emit(
                "http://127.0.0.1:8080",
                None,
                down,
                up,
                "blf-prod-hive",
                "PROD",
                replace=False,
                dry_run=False,
            )
    assert wrote is False
    assert "跳过" in msg
    e_inst.emit_mcp.assert_not_called()


@pytest.mark.skipif(not _has_datahub_sdk(), reason="acryl-datahub not installed")
def test_merge_calls_emit_when_new_upstream() -> None:
    from datahub.metadata.schema_classes import DatasetLineageTypeClass, UpstreamClass, UpstreamLineageClass

    down = TableRef("ods", "target")
    up = TableRef("ods", "newup")
    old_other = TableRef("ods", "other")
    from job_info_sync_datahub.datahub_writer import make_dataset_urn_from_ref

    other_urn = make_dataset_urn_from_ref(old_other, "blf-prod-hive", "PROD")
    existing = UpstreamLineageClass(
        upstreams=[UpstreamClass(dataset=other_urn, type=DatasetLineageTypeClass.TRANSFORMED)]
    )
    with patch("datahub.ingestion.graph.client.DataHubGraph") as g_cls:
        g_inst = MagicMock()
        g_inst.get_aspect.return_value = existing
        g_cls.return_value = g_inst
        with patch("datahub.emitter.rest_emitter.DatahubRestEmitter") as e_cls:
            e_inst = MagicMock()
            e_cls.return_value = e_inst
            wrote, msg = merge_and_emit(
                "http://127.0.0.1:8080",
                None,
                down,
                up,
                "blf-prod-hive",
                "PROD",
                replace=False,
                dry_run=False,
            )
    assert wrote is True
    assert "merge" in msg.lower() or "追加上游" in msg
    e_inst.emit_mcp.assert_called_once()
