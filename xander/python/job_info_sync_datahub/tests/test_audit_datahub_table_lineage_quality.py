"""Tests for DataHub table lineage quality audit classification."""

from __future__ import annotations

import json

from job_info_sync_datahub.audit_datahub_table_lineage_quality import (
    FLAG_DDL,
    FLAG_FIELD_LINEAGE,
    FLAG_TABLE_LINEAGE,
    ISSUE_DOCUMENTATION_LINEAGE_MISMATCH,
    ISSUE_ETL_SCRIPT_NO_UPSTREAM,
    ISSUE_FIELD_LINEAGE_DOWNSTREAM_FIELD_NOT_FOUND,
    ISSUE_FIELD_LINEAGE_EXISTS_BUT_FLAG_MISSING,
    ISSUE_FIELD_LINEAGE_FLAG_BUT_NO_FINE_GRAINED,
    ISSUE_FIELD_LINEAGE_INVALID_SCHEMA_FIELD_URN,
    ISSUE_FIELD_LINEAGE_SOURCE_DATASET_NOT_FOUND,
    ISSUE_FIELD_LINEAGE_SOURCE_FIELD_NOT_FOUND,
    ISSUE_FIELD_LINEAGE_SOURCE_NOT_HIVE_PLATFORM,
    ISSUE_FIELD_LINEAGE_SOURCE_NOT_TABLE_UPSTREAM,
    ISSUE_FIELD_LINEAGE_TARGET_COVERAGE_INCOMPLETE,
    ISSUE_FLAG_CLAIMS_LINEAGE_BUT_NO_UPSTREAM,
    ISSUE_FLAG_MISSING_LINEAGE_BUT_HAS_UPSTREAM,
    ISSUE_MISSING_ETL_SCRIPT,
    ISSUE_SELF_DEPENDENCY,
    ISSUE_TARGET_NOT_IN_HIVE,
    ISSUE_UPSTREAM_DATAHUB_ENTITY_NOT_FOUND,
    ISSUE_UPSTREAM_NOT_IN_HIVE,
    ISSUE_VIEW_DEFINITION_NO_UPSTREAM,
    ISSUE_VIEW_FLAG_INCOMPLETE,
    ISSUE_VIEW_MISSING_DEFINITION,
    ISSUE_VIEW_NO_UPSTREAM,
    evaluate_lineage_quality,
    fetch_lineage_facts_from_mysql,
    fetch_schema_fields_from_mysql,
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
    assert result.summary["issue_counts_by_severity"] == {"P0": 2, "P1": 2}
    assert result.issues[0].severity
    assert result.issues[0].fix_hint
    assert result.issues[0].evidence


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


def test_evaluate_lineage_quality_reports_documentation_lineage_mismatch() -> None:
    target = make_hive_dataset_urn("data_dm.dm_target")
    upstream = make_hive_dataset_urn("data_ods.ods_actual")
    description = """
### 4. 数据来源

| 表名 | 说明 |
| --- | --- |
| data_ods.ods_doc_table | 文档上游 |
"""

    result = evaluate_lineage_quality(
        dataset_urns={target, upstream},
        upstreams_by_target={target: [upstream]},
        hive_existing_fqtns={
            "data_dm.dm_target",
            "data_ods.ods_actual",
            "data_ods.ods_doc_table",
        },
        etl_script_dataset_urns={target},
        documentation_by_urn={target: description},
    )

    assert ISSUE_DOCUMENTATION_LINEAGE_MISMATCH in [row.issue_type for row in result.issues]


def test_evaluate_lineage_quality_reports_availability_flag_mismatches() -> None:
    no_upstream = make_hive_dataset_urn("data_dm.dm_no_upstream")
    has_upstream = make_hive_dataset_urn("data_dm.dm_has_upstream")
    upstream = make_hive_dataset_urn("data_ods.ods_source")

    result = evaluate_lineage_quality(
        dataset_urns={no_upstream, has_upstream, upstream},
        upstreams_by_target={has_upstream: [upstream]},
        hive_existing_fqtns={"data_dm.dm_no_upstream", "data_dm.dm_has_upstream", "data_ods.ods_source"},
        data_availability_flags_by_urn={
            no_upstream: {FLAG_TABLE_LINEAGE},
            has_upstream: {FLAG_DDL},
        },
        schema_field_count_by_urn={no_upstream: 1, has_upstream: 1, upstream: 1},
    )

    issue_types = [row.issue_type for row in result.issues]
    assert ISSUE_FLAG_CLAIMS_LINEAGE_BUT_NO_UPSTREAM in issue_types
    assert ISSUE_FLAG_MISSING_LINEAGE_BUT_HAS_UPSTREAM in issue_types


def test_evaluate_lineage_quality_reports_view_definition_issues() -> None:
    missing_definition = make_hive_dataset_urn("data_dw.dw_missing_definition")
    definition_no_upstream = make_hive_dataset_urn("data_dw.dw_definition_no_upstream")
    complete_view = make_hive_dataset_urn("data_dw.dw_complete_view")
    upstream = make_hive_dataset_urn("data_ods.ods_source")

    result = evaluate_lineage_quality(
        dataset_urns={missing_definition, definition_no_upstream, complete_view, upstream},
        upstreams_by_target={complete_view: [upstream]},
        hive_existing_fqtns={
            "data_dw.dw_missing_definition",
            "data_dw.dw_definition_no_upstream",
            "data_dw.dw_complete_view",
            "data_ods.ods_source",
        },
        view_dataset_urns={missing_definition, definition_no_upstream, complete_view},
        view_logic_by_urn={
            missing_definition: "",
            definition_no_upstream: "select * from data_ods.ods_source",
            complete_view: "select * from data_ods.ods_source",
        },
        data_availability_flags_by_urn={
            complete_view: {FLAG_DDL, FLAG_TABLE_LINEAGE},
        },
    )

    issue_types = [row.issue_type for row in result.issues]
    assert ISSUE_VIEW_MISSING_DEFINITION in issue_types
    assert ISSUE_VIEW_DEFINITION_NO_UPSTREAM in issue_types
    assert ISSUE_VIEW_FLAG_INCOMPLETE in issue_types


def _schema_field_urn(dataset_urn: str, field_name: str) -> str:
    return f"urn:li:schemaField:({dataset_urn},{field_name})"


def test_evaluate_lineage_quality_checks_field_source_dataset_and_table_upstream() -> None:
    target = make_hive_dataset_urn("default.mid_store_info_bach")
    correct_upstream = make_hive_dataset_urn("default.pdw_bach_baseinfo_shop_shop")
    wrong_source = make_hive_dataset_urn("pdw.bach_baseinfo_shop_shop")

    result = evaluate_lineage_quality(
        dataset_urns={target, correct_upstream},
        upstreams_by_target={target: [correct_upstream]},
        hive_existing_fqtns={
            "default.mid_store_info_bach",
            "default.pdw_bach_baseinfo_shop_shop",
        },
        data_availability_flags_by_urn={target: {FLAG_FIELD_LINEAGE}},
        schema_fields_by_urn={
            target: {"shop_code"},
            correct_upstream: {"shop_code"},
        },
        fine_grained_lineages_by_target={
            target: [
                {
                    "upstreams": [_schema_field_urn(wrong_source, "shop_code")],
                    "downstreams": [_schema_field_urn(target, "shop_code")],
                }
            ]
        },
    )

    issue_types = [issue.issue_type for issue in result.issues]
    assert ISSUE_FIELD_LINEAGE_SOURCE_DATASET_NOT_FOUND in issue_types
    assert ISSUE_FIELD_LINEAGE_SOURCE_NOT_TABLE_UPSTREAM in issue_types
    assert result.summary["field_lineage_scanned_dataset_count"] == 1


def test_evaluate_lineage_quality_checks_field_urn_schema_and_coverage() -> None:
    target = make_hive_dataset_urn("default.dim_target")
    upstream = make_hive_dataset_urn("default.pdw_source")

    result = evaluate_lineage_quality(
        dataset_urns={target, upstream},
        upstreams_by_target={target: [upstream]},
        hive_existing_fqtns={"default.dim_target", "default.pdw_source"},
        data_availability_flags_by_urn={target: {FLAG_FIELD_LINEAGE}},
        schema_fields_by_urn={
            target: {"id", "name", "missing_target"},
            upstream: {"id"},
        },
        fine_grained_lineages_by_target={
            target: [
                {
                    "upstreams": [
                        _schema_field_urn(upstream, "missing_source"),
                        "not-a-schema-field-urn",
                    ],
                    "downstreams": [
                        _schema_field_urn(target, "id"),
                        _schema_field_urn(target, "unknown_target"),
                    ],
                }
            ]
        },
    )

    issue_types = [issue.issue_type for issue in result.issues]
    assert ISSUE_FIELD_LINEAGE_INVALID_SCHEMA_FIELD_URN in issue_types
    assert ISSUE_FIELD_LINEAGE_SOURCE_FIELD_NOT_FOUND in issue_types
    assert ISSUE_FIELD_LINEAGE_DOWNSTREAM_FIELD_NOT_FOUND in issue_types
    assert ISSUE_FIELD_LINEAGE_TARGET_COVERAGE_INCOMPLETE in issue_types


def test_evaluate_lineage_quality_checks_field_lineage_flag_consistency_and_scope() -> None:
    flagged_without_fine = make_hive_dataset_urn("default.dim_flagged")
    fine_without_flag = make_hive_dataset_urn("default.dim_unflagged")
    not_in_scope = make_hive_dataset_urn("default.dim_not_started")

    result = evaluate_lineage_quality(
        dataset_urns={flagged_without_fine, fine_without_flag, not_in_scope},
        upstreams_by_target={},
        hive_existing_fqtns={
            "default.dim_flagged",
            "default.dim_unflagged",
            "default.dim_not_started",
        },
        data_availability_flags_by_urn={flagged_without_fine: {FLAG_FIELD_LINEAGE}},
        schema_fields_by_urn={
            flagged_without_fine: {"id"},
            fine_without_flag: {"id"},
            not_in_scope: {"id"},
        },
        fine_grained_lineages_by_target={
            fine_without_flag: [
                {
                    "upstreams": [],
                    "downstreams": [_schema_field_urn(fine_without_flag, "id")],
                }
            ]
        },
    )

    issue_types = [issue.issue_type for issue in result.issues]
    assert ISSUE_FIELD_LINEAGE_FLAG_BUT_NO_FINE_GRAINED in issue_types
    assert ISSUE_FIELD_LINEAGE_EXISTS_BUT_FLAG_MISSING in issue_types
    assert result.summary["field_lineage_scanned_dataset_count"] == 2
    assert all(issue.target_table != "default.dim_not_started" for issue in result.issues)


def test_evaluate_lineage_quality_classifies_non_hive_field_source_separately() -> None:
    target = make_hive_dataset_urn("default.dim_target")
    mysql_source = (
        "urn:li:dataset:(urn:li:dataPlatform:mysql,"
        "prod_mysql.baseinfo.shop,PROD)"
    )

    result = evaluate_lineage_quality(
        dataset_urns={target},
        upstreams_by_target={target: [mysql_source]},
        hive_existing_fqtns={"default.dim_target"},
        data_availability_flags_by_urn={target: {FLAG_FIELD_LINEAGE}},
        schema_fields_by_urn={target: {"id"}},
        fine_grained_lineages_by_target={
            target: [
                {
                    "upstreams": [_schema_field_urn(mysql_source, "id")],
                    "downstreams": [_schema_field_urn(target, "id")],
                }
            ]
        },
    )

    issue_types = [issue.issue_type for issue in result.issues]
    assert ISSUE_FIELD_LINEAGE_SOURCE_NOT_HIVE_PLATFORM in issue_types
    assert ISSUE_FIELD_LINEAGE_SOURCE_DATASET_NOT_FOUND not in issue_types


def test_fetch_field_lineage_facts_from_mysql(monkeypatch) -> None:
    target = make_hive_dataset_urn("default.dim_target")
    upstream = make_hive_dataset_urn("default.pdw_source")
    schema_metadata = json.dumps(
        {
            "fields": [
                {"fieldPath": "id"},
                {"fieldPath": "dt", "isPartitioningKey": True},
            ]
        }
    )
    upstream_metadata = json.dumps(
        {
            "upstreams": [{"dataset": upstream}],
            "fineGrainedLineages": [
                {
                    "upstreams": [_schema_field_urn(upstream, "id")],
                    "downstreams": [_schema_field_urn(target, "id")],
                }
            ],
        }
    )
    outputs = iter(
        [
            f"{target}\t{schema_metadata}\n",
            f"{target}\t{upstream_metadata}\n",
        ]
    )
    monkeypatch.setattr(
        "job_info_sync_datahub.audit_datahub_table_lineage_quality._run_mysql_query",
        lambda _sql: next(outputs),
    )

    fields, partitions = fetch_schema_fields_from_mysql("blf-prod-hive", "PROD")
    upstreams, fine_grained = fetch_lineage_facts_from_mysql("blf-prod-hive", "PROD")

    assert fields[target] == {"id", "dt"}
    assert partitions[target] == {"dt"}
    assert upstreams[target] == [upstream]
    assert fine_grained[target][0]["upstreams"] == [_schema_field_urn(upstream, "id")]
