"""Tests for DataHub table lineage quality audit classification."""

from __future__ import annotations

from job_info_sync_datahub.audit_datahub_table_lineage_quality import (
    ISSUE_SELF_DEPENDENCY,
    ISSUE_TARGET_NOT_IN_HIVE,
    ISSUE_UPSTREAM_DATAHUB_ENTITY_NOT_FOUND,
    ISSUE_UPSTREAM_NOT_IN_HIVE,
    evaluate_lineage_quality,
)
from job_info_sync_datahub.field_lineage_datahub_reader import make_hive_dataset_urn


def test_evaluate_lineage_quality_classifies_missing_and_invalid_edges() -> None:
    target = make_hive_dataset_urn("dw.target_table")
    upstream_ok = make_hive_dataset_urn("ods.source_ok")
    upstream_missing_datahub = make_hive_dataset_urn("ods.source_not_in_datahub")
    upstream_missing_hive = make_hive_dataset_urn("ods.source_not_in_hive")

    result = evaluate_lineage_quality(
        dataset_urns={target, upstream_ok, upstream_missing_hive},
        upstreams_by_target={
            target: [
                upstream_ok,
                upstream_missing_datahub,
                upstream_missing_hive,
                target,
            ]
        },
        hive_existing_fqtns={
            "ods.source_ok",
            "ods.source_not_in_datahub",
            "dw.target_table",
        },
    )

    issue_types = [row.issue_type for row in result.issues]
    assert issue_types == [
        ISSUE_UPSTREAM_DATAHUB_ENTITY_NOT_FOUND,
        ISSUE_UPSTREAM_NOT_IN_HIVE,
        ISSUE_SELF_DEPENDENCY,
        ISSUE_TARGET_NOT_IN_HIVE,
    ]

    assert result.summary["scanned_dataset_count"] == 3
    assert result.summary["lineage_target_count"] == 1
    assert result.summary["upstream_edge_count"] == 4
    assert result.summary["issue_count"] == 4


def test_evaluate_lineage_quality_reports_target_missing_from_hive_once() -> None:
    target = make_hive_dataset_urn("dw.target_missing")
    upstream = make_hive_dataset_urn("ods.source_ok")

    result = evaluate_lineage_quality(
        dataset_urns={target, upstream},
        upstreams_by_target={target: [upstream]},
        hive_existing_fqtns={"ods.source_ok"},
    )

    assert [row.issue_type for row in result.issues] == [ISSUE_TARGET_NOT_IN_HIVE]
    assert result.issues[0].target_table == "dw.target_missing"
