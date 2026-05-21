"""DatahubWriter upstreamLineage replace / merge modes."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from job_info_sync_datahub.datahub_writer import make_dataset_urn_from_ref
from job_info_sync_datahub.models import TableLineage, TableRef


def _has_datahub_sdk() -> bool:
    try:
        import datahub.ingestion.graph.client  # noqa: F401

        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _has_datahub_sdk(), reason="acryl-datahub not installed")
def test_emit_upstream_lineage_replace_mode_writes_only_new_upstreams() -> None:
    from datahub.metadata.schema_classes import DatasetLineageTypeClass, UpstreamClass, UpstreamLineageClass

    from job_info_sync_datahub.datahub_writer import emit_upstream_lineage

    target = TableRef("dw", "target")
    old = TableRef("ods", "old_upstream")
    new = TableRef("ods", "new_upstream")
    old_urn = make_dataset_urn_from_ref(old)
    new_urn = make_dataset_urn_from_ref(new)
    existing = UpstreamLineageClass(
        upstreams=[UpstreamClass(dataset=old_urn, type=DatasetLineageTypeClass.TRANSFORMED)]
    )

    with patch("datahub.ingestion.graph.client.DataHubGraph") as g_cls:
        g_inst = MagicMock()
        g_inst.get_aspect.return_value = existing
        g_cls.return_value = g_inst
        with patch("job_info_sync_datahub.datahub_writer.DatahubRestEmitter") as e_cls:
            e_inst = MagicMock()
            e_cls.return_value = e_inst

            emit_upstream_lineage(
                "http://localhost:8080",
                TableLineage(target=target, upstreams=[new]),
                [],
                replace_existing_lineage=True,
            )

    emitted = e_inst.emit_mcp.call_args.args[0].aspect
    assert [u.dataset for u in emitted.upstreams] == [new_urn]
    g_inst.get_aspect.assert_not_called()


@pytest.mark.skipif(not _has_datahub_sdk(), reason="acryl-datahub not installed")
def test_emit_upstream_lineage_merge_mode_keeps_existing_upstreams_and_fine_grained() -> None:
    from datahub.emitter import mce_builder as builder
    from datahub.metadata.schema_classes import (
        DatasetLineageTypeClass,
        FineGrainedLineageClass,
        FineGrainedLineageDownstreamTypeClass,
        FineGrainedLineageUpstreamTypeClass,
        UpstreamClass,
        UpstreamLineageClass,
    )

    from job_info_sync_datahub.datahub_writer import emit_upstream_lineage

    target = TableRef("dw", "target")
    old = TableRef("ods", "old_upstream")
    new = TableRef("ods", "new_upstream")
    target_urn = make_dataset_urn_from_ref(target)
    old_urn = make_dataset_urn_from_ref(old)
    new_urn = make_dataset_urn_from_ref(new)
    fine_grained = [
        FineGrainedLineageClass(
            upstreamType=FineGrainedLineageUpstreamTypeClass.FIELD_SET,
            upstreams=[builder.make_schema_field_urn(old_urn, "source_col")],
            downstreamType=FineGrainedLineageDownstreamTypeClass.FIELD,
            downstreams=[builder.make_schema_field_urn(target_urn, "target_col")],
        )
    ]
    existing = UpstreamLineageClass(
        upstreams=[UpstreamClass(dataset=old_urn, type=DatasetLineageTypeClass.TRANSFORMED)],
        fineGrainedLineages=fine_grained,
    )

    with patch("datahub.ingestion.graph.client.DataHubGraph") as g_cls:
        g_inst = MagicMock()
        g_inst.get_aspect.return_value = existing
        g_cls.return_value = g_inst
        with patch("job_info_sync_datahub.datahub_writer.DatahubRestEmitter") as e_cls:
            e_inst = MagicMock()
            e_cls.return_value = e_inst

            emit_upstream_lineage(
                "http://localhost:8080",
                TableLineage(target=target, upstreams=[new]),
                [],
                replace_existing_lineage=False,
            )

    emitted = e_inst.emit_mcp.call_args.args[0].aspect
    assert [u.dataset for u in emitted.upstreams] == [old_urn, new_urn]
    assert emitted.fineGrainedLineages == fine_grained
    g_inst.get_aspect.assert_called_once()
