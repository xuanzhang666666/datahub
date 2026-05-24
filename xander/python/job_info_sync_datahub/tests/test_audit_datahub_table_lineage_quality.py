"""Tests for DataHub table lineage quality audit classification."""

from __future__ import annotations

from job_info_sync_datahub.audit_datahub_table_lineage_quality import (
    ISSUE_ETL_SCRIPT_NO_UPSTREAM,
    ISSUE_MISSING_ETL_SCRIPT,
    ISSUE_SELF_DEPENDENCY,
    ISSUE_TARGET_NOT_IN_HIVE,
    ISSUE_UPSTREAM_DATAHUB_ENTITY_NOT_FOUND,
    ISSUE_UPSTREAM_NOT_IN_HIVE,
    ISSUE_VIEW_NO_UPSTREAM,
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


def test_evaluate_lineage_quality_reports_required_table_missing_etl_script() -> None:
    required = make_hive_dataset_urn("data_dw.dw_required")
    allowed_ods = make_hive_dataset_urn("data_ods.ods_allowed")
    allowed_app = make_hive_dataset_urn("data_app.app_allowed")
    view_required = make_hive_dataset_urn("data_dw.dw_view")
    has_etl = make_hive_dataset_urn("data_dm.dm_has_etl")
    upstream = make_hive_dataset_urn("data_ods.ods_source")

    result = evaluate_lineage_quality(
        dataset_urns={required, allowed_ods, allowed_app, view_required, has_etl, upstream},
        upstreams_by_target={view_required: [upstream], has_etl: [upstream]},
        hive_existing_fqtns={
            "data_dw.dw_required",
            "data_ods.ods_allowed",
            "data_app.app_allowed",
            "data_dw.dw_view",
            "data_dm.dm_has_etl",
            "data_ods.ods_source",
        },
        view_dataset_urns={view_required},
        etl_script_dataset_urns={has_etl},
    )

    assert [row.issue_type for row in result.issues] == [ISSUE_MISSING_ETL_SCRIPT]
    assert result.issues[0].target_table == "data_dw.dw_required"


def test_evaluate_lineage_quality_reports_view_without_upstream() -> None:
    view = make_hive_dataset_urn("data_dw.dw_view")

    result = evaluate_lineage_quality(
        dataset_urns={view},
        upstreams_by_target={},
        hive_existing_fqtns={"data_dw.dw_view"},
        view_dataset_urns={view},
        etl_script_dataset_urns=set(),
    )

    assert [row.issue_type for row in result.issues] == [ISSUE_VIEW_NO_UPSTREAM]
    assert result.issues[0].target_table == "data_dw.dw_view"


def test_evaluate_lineage_quality_reports_required_etl_table_without_upstream() -> None:
    required = make_hive_dataset_urn("data_dw.dw_required")
    allowed_ods = make_hive_dataset_urn("data_ods.ods_allowed")
    allowed_ai = make_hive_dataset_urn("data_ai.ai_allowed")
    allowed_app = make_hive_dataset_urn("data_app.app_allowed")
    required_with_upstream = make_hive_dataset_urn("data_dm.dm_with_upstream")
    upstream = make_hive_dataset_urn("data_ods.ods_source")

    result = evaluate_lineage_quality(
        dataset_urns={required, allowed_ods, allowed_ai, allowed_app, required_with_upstream, upstream},
        upstreams_by_target={required_with_upstream: [upstream]},
        hive_existing_fqtns={
            "data_dw.dw_required",
            "data_ods.ods_allowed",
            "data_ai.ai_allowed",
            "data_app.app_allowed",
            "data_dm.dm_with_upstream",
            "data_ods.ods_source",
        },
        view_dataset_urns=set(),
        etl_script_dataset_urns={required, allowed_ods, allowed_ai, allowed_app, required_with_upstream},
    )

    assert [row.issue_type for row in result.issues] == [ISSUE_ETL_SCRIPT_NO_UPSTREAM]
    assert result.issues[0].target_table == "data_dw.dw_required"
