from __future__ import annotations

import json
from pathlib import Path

from openpyxl import load_workbook

from job_info_sync_datahub.field_lineage_datahub_reader import (
    extract_field_lineage_input,
    make_hive_dataset_urn,
    strip_markdown_code_fence,
)
from job_info_sync_datahub.field_lineage_cli import main as field_lineage_cli_main
from job_info_sync_datahub.field_lineage_excel import (
    load_approved_review_rows,
    write_candidate_workbook,
)
from job_info_sync_datahub.field_lineage_llm import parse_field_lineage_payload
from job_info_sync_datahub.field_lineage_models import (
    FieldLineageCandidate,
    FieldLineageInput,
    FieldLineageReviewStatus,
)
from job_info_sync_datahub.field_lineage_writer import (
    build_fine_grained_lineage_class,
    group_approved_rows,
)
from job_info_sync_datahub.structured_properties import URN_ETL_SCRIPT, URN_EXECUTE_SHELL


def _structured_properties_payload(etl_script: str, execute_shell: str) -> dict:
    return {
        "structuredProperties": {
            "value": {
                "properties": [
                    {
                        "propertyUrn": URN_ETL_SCRIPT,
                        "values": [{"string": etl_script}],
                    },
                    {
                        "propertyUrn": URN_EXECUTE_SHELL,
                        "values": [{"string": execute_shell}],
                    },
                ]
            }
        }
    }


def test_make_hive_dataset_urn_uses_blf_defaults() -> None:
    assert make_hive_dataset_urn("default.dim_store_info") == (
        "urn:li:dataset:(urn:li:dataPlatform:hive,"
        "blf-prod-hive.default.dim_store_info,PROD)"
    )


def test_strip_markdown_code_fence_preserves_inner_script() -> None:
    assert strip_markdown_code_fence("```sql\nselect 1;\n```") == "select 1;"
    assert strip_markdown_code_fence("select 1;") == "select 1;"


def test_extract_field_lineage_input_from_structured_properties() -> None:
    payload = _structured_properties_payload(
        etl_script="```sql\ninsert overwrite table default.dim_store_info select 1;\n```",
        execute_shell="```shell\nsh run_dim_store_info.sh\n```",
    )

    result = extract_field_lineage_input(
        dataset_urn=make_hive_dataset_urn("default.dim_store_info"),
        table_name="default.dim_store_info",
        payload=payload,
    )

    assert result == FieldLineageInput(
        dataset_urn=make_hive_dataset_urn("default.dim_store_info"),
        table_name="default.dim_store_info",
        etl_script="insert overwrite table default.dim_store_info select 1;",
        execute_shell="sh run_dim_store_info.sh",
    )


def test_parse_field_lineage_payload_defaults_review_status_to_pending() -> None:
    payload = json.dumps(
        {
            "target_table": "default.dim_store_info",
            "mappings": [
                {
                    "target_field": "store_id",
                    "source_table": "ods.store_info",
                    "source_field": "id",
                    "transform_expression": "cast(id as bigint)",
                    "evidence_sql": "select cast(id as bigint) as store_id",
                    "confidence": "HIGH",
                    "notes": "direct mapping",
                }
            ],
            "unresolved_fields": [{"target_field": "store_name", "reason": "dynamic SQL"}],
        }
    )

    parsed = parse_field_lineage_payload(payload)

    assert parsed.target_table == "default.dim_store_info"
    assert parsed.mappings == [
        FieldLineageCandidate(
            target_table="default.dim_store_info",
            target_field="store_id",
            source_table="ods.store_info",
            source_field="id",
            transform_expression="cast(id as bigint)",
            evidence_sql="select cast(id as bigint) as store_id",
            confidence="HIGH",
            llm_notes="direct mapping",
            review_status=FieldLineageReviewStatus.PENDING,
        )
    ]
    assert parsed.unresolved_fields[0].target_field == "store_name"


def test_write_candidate_workbook_creates_review_sheets(tmp_path: Path) -> None:
    output = tmp_path / "default_dim_store_info_field_lineage.xlsx"
    source = FieldLineageInput(
        dataset_urn=make_hive_dataset_urn("default.dim_store_info"),
        table_name="default.dim_store_info",
        etl_script="insert overwrite table default.dim_store_info select 1;",
        execute_shell="sh run_dim_store_info.sh",
    )
    candidate = FieldLineageCandidate(
        target_table="default.dim_store_info",
        target_field="store_id",
        source_table="ods.store_info",
        source_field="id",
        transform_expression="cast(id as bigint)",
        evidence_sql="select cast(id as bigint) as store_id",
        confidence="HIGH",
    )

    write_candidate_workbook(
        output,
        source_input=source,
        candidates=[candidate],
        unresolved_fields=[],
        llm_model="deepseek-test",
    )

    wb = load_workbook(output)
    assert wb.sheetnames == ["candidate_lineage", "unresolved_fields", "source_context"]
    ws = wb["candidate_lineage"]
    headers = [cell.value for cell in ws[1]]
    assert headers[:5] == [
        "review_status",
        "target_table",
        "target_field",
        "source_table",
        "source_field",
    ]
    assert ws["A2"].value == "PENDING"
    assert ws["B2"].value == "default.dim_store_info"


def test_load_approved_review_rows_only_returns_approved(tmp_path: Path) -> None:
    output = tmp_path / "review.xlsx"
    source = FieldLineageInput(
        dataset_urn=make_hive_dataset_urn("default.dim_store_info"),
        table_name="default.dim_store_info",
        etl_script="select 1",
        execute_shell="sh run.sh",
    )
    rows = [
        FieldLineageCandidate(
            target_table="default.dim_store_info",
            target_field="approved_field",
            source_table="ods.store_info",
            source_field="id",
            confidence="HIGH",
        ),
        FieldLineageCandidate(
            target_table="default.dim_store_info",
            target_field="pending_field",
            source_table="ods.store_info",
            source_field="name",
            confidence="HIGH",
        ),
    ]
    write_candidate_workbook(
        output,
        source_input=source,
        candidates=rows,
        unresolved_fields=[],
        llm_model="deepseek-test",
    )

    wb = load_workbook(output)
    ws = wb["candidate_lineage"]
    ws["A2"] = "APPROVED"
    ws["A3"] = "PENDING"
    wb.save(output)

    approved = load_approved_review_rows(output)

    assert [row.target_field for row in approved] == ["approved_field"]


def test_group_coalesce_rows_merge_sources_and_transform() -> None:
    rows = [
        FieldLineageCandidate(
            target_table="default.dim_store_info",
            target_field="store_address",
            source_table="ods.bach_store",
            source_field="store_address",
            transform_expression="coalesce(bach.store_address, hd.store_address)",
        ),
        FieldLineageCandidate(
            target_table="default.dim_store_info",
            target_field="store_address",
            source_table="ods.hd_store",
            source_field="store_address",
            transform_expression="coalesce(bach.store_address, hd.store_address)",
        ),
    ]
    grouped = group_approved_rows(rows)
    assert len(grouped) == 1
    assert grouped[0].target_field == "store_address"
    assert len(grouped[0].sources) == 2
    assert grouped[0].transform_operation == "coalesce(bach.store_address, hd.store_address)"


def test_build_fine_grained_sets_transform_operation() -> None:
    group = group_approved_rows(
        [
            FieldLineageCandidate(
                target_table="default.dim_store_info",
                target_field="store_id",
                source_table="ods.store_info",
                source_field="id",
                transform_expression="cast(id as bigint)",
            )
        ]
    )[0]
    fg = build_fine_grained_lineage_class(group)
    assert fg.transformOperation == "cast(id as bigint)"
    assert len(fg.upstreams) == 1


def test_import_reviewed_cli_writes_approved_plan(tmp_path: Path) -> None:
    workbook = tmp_path / "review.xlsx"
    output = tmp_path / "import_plan.json"
    source = FieldLineageInput(
        dataset_urn=make_hive_dataset_urn("default.dim_store_info"),
        table_name="default.dim_store_info",
        etl_script="select 1",
        execute_shell="sh run.sh",
    )
    write_candidate_workbook(
        workbook,
        source_input=source,
        candidates=[
            FieldLineageCandidate(
                target_table="default.dim_store_info",
                target_field="store_id",
                source_table="ods.store_info",
                source_field="id",
                confidence="HIGH",
            )
        ],
        unresolved_fields=[],
        llm_model="deepseek-test",
    )
    wb = load_workbook(workbook)
    wb["candidate_lineage"]["A2"] = "APPROVED"
    wb.save(workbook)

    exit_code = field_lineage_cli_main(
        ["import-reviewed", "--input", str(workbook), "--output", str(output)]
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["dry_run"] is True
    assert payload["approved_rows"] == 1
    assert payload["rows"][0]["target_field"] == "store_id"
    assert "write_result" in payload
