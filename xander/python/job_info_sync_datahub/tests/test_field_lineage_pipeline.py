from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from openpyxl import load_workbook

from job_info_sync_datahub.field_lineage_datahub_reader import (
    extract_field_lineage_input,
    make_hive_dataset_urn,
    missing_field_lineage_source_reason,
    strip_markdown_code_fence,
)
from job_info_sync_datahub.field_lineage_cli import main as field_lineage_cli_main
from job_info_sync_datahub.field_lineage_excel import (
    load_approved_review_rows,
    write_candidate_workbook,
)
from job_info_sync_datahub.field_lineage_llm import (
    build_field_lineage_request_debug_info,
    parse_field_lineage_payload,
)
from job_info_sync_datahub.field_lineage_models import (
    FieldLineageCandidate,
    FieldLineageInput,
    FieldLineageReviewStatus,
)
from job_info_sync_datahub.field_lineage_writer import (
    build_fine_grained_lineage_class,
    build_transform_operation_for_ui,
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


def test_missing_field_lineage_source_reason_when_etl_empty() -> None:
    payload = _structured_properties_payload(etl_script="", execute_shell="")
    reason = missing_field_lineage_source_reason(payload)
    assert reason is not None
    assert "Etl Script" in reason
    assert "Execute Shell" in reason


def test_missing_field_lineage_source_reason_none_when_etl_present() -> None:
    payload = _structured_properties_payload(
        etl_script="select 1",
        execute_shell="",
    )
    assert missing_field_lineage_source_reason(payload) is None


def test_parse_field_lineage_payload_high_confidence_is_approved() -> None:
    payload = json.dumps(
        {
            "target_table": "default.dim_store_info",
            "mappings": [
                {
                    "target_field": "store_id",
                    "source_table": "ods.store_info",
                    "source_field": "id",
                    "transform_expression": "cast(id as bigint)",
                    "transform_explanation": "取 ods.store_info 表中的 id 字段，表示将门店 ID 转为 bigint 类型写入 store_id。",
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
            transform_explanation="取 ods.store_info 表中的 id 字段，表示将门店 ID 转为 bigint 类型写入 store_id。",
            evidence_sql="select cast(id as bigint) as store_id",
            confidence="HIGH",
            llm_notes="direct mapping",
            review_status=FieldLineageReviewStatus.APPROVED,
        )
    ]
    assert parsed.unresolved_fields[0].target_field == "store_name"


def test_parse_field_lineage_payload_non_high_confidence_stays_pending() -> None:
    payload = json.dumps(
        {
            "target_table": "default.dim_store_info",
            "mappings": [
                {
                    "target_field": "store_id",
                    "source_table": "ods.store_info",
                    "source_field": "id",
                    "confidence": "MEDIUM",
                }
            ],
            "unresolved_fields": [],
        }
    )

    parsed = parse_field_lineage_payload(payload)

    assert parsed.mappings[0].review_status == FieldLineageReviewStatus.PENDING


def test_build_field_lineage_request_debug_info_counts_prompt_size() -> None:
    source = FieldLineageInput(
        dataset_urn=make_hive_dataset_urn("default.dim_store_info"),
        table_name="default.dim_store_info",
        etl_script="select store_id from ods.store_info",
        execute_shell="sh dim_store_info",
    )

    debug = build_field_lineage_request_debug_info(
        source_input=source,
        base_v1="https://api.deepseek.com/v1",
        model="deepseek-chat",
        timeout_sec=90,
    )

    assert debug["llm_base"] == "https://api.deepseek.com/v1"
    assert debug["llm_model"] == "deepseek-chat"
    assert debug["timeout_sec"] == 90
    assert debug["user_message_chars"] > len(source.etl_script)
    assert debug["system_prompt_chars"] > 0


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
        transform_explanation="取 ods.store_info 表中的 id 字段，表示将门店 ID 转为 bigint 类型写入 store_id。",
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
    assert "transform_explanation" in headers
    assert ws["A2"].value == "APPROVED"
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
            confidence="MEDIUM",
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
            transform_explanation=(
                "优先取 ods.bach_store 表中的 store_address 字段，"
                "为空时取 ods.hd_store 表中的 store_address 字段，"
                "表示门店地址按优先级兜底合并。"
            ),
        ),
        FieldLineageCandidate(
            target_table="default.dim_store_info",
            target_field="store_address",
            source_table="ods.hd_store",
            source_field="store_address",
            transform_expression="coalesce(bach.store_address, hd.store_address)",
            transform_explanation=(
                "优先取 ods.bach_store 表中的 store_address 字段，"
                "为空时取 ods.hd_store 表中的 store_address 字段，"
                "表示门店地址按优先级兜底合并。"
            ),
        ),
    ]
    grouped = group_approved_rows(rows)
    assert len(grouped) == 1
    assert grouped[0].target_field == "store_address"
    assert len(grouped[0].sources) == 2
    _coalesce_explanation = (
        "优先取 ods.bach_store 表中的 store_address 字段，"
        "为空时取 ods.hd_store 表中的 store_address 字段，"
        "表示门店地址按优先级兜底合并。"
    )
    assert grouped[0].transform_operation == (
        f"/* 中文解释：{_coalesce_explanation} */\n"
        "coalesce(bach.store_address, hd.store_address)"
    )
    assert grouped[0].transform_explanation == _coalesce_explanation


def test_build_transform_operation_for_ui() -> None:
    assert (
        build_transform_operation_for_ui(
            "coalesce(a, b)",
            "优先取 ods.a 表中的 x 字段，为空时取 ods.b 表中的 y 字段，表示按优先级兜底。",
        )
        == "/* 中文解释：优先取 ods.a 表中的 x 字段，为空时取 ods.b 表中的 y 字段，表示按优先级兜底。 */\n"
        "coalesce(a, b)"
    )


@pytest.mark.skipif(
    importlib.util.find_spec("datahub") is None,
    reason="acryl-datahub not installed",
)
def test_build_fine_grained_sets_transform_operation() -> None:
    group = group_approved_rows(
        [
            FieldLineageCandidate(
                target_table="default.dim_store_info",
                target_field="store_id",
                source_table="ods.store_info",
                source_field="id",
                transform_expression="cast(id as bigint)",
                transform_explanation="取 ods.store_info 表中的 id 字段，表示将门店 ID 转为 bigint 类型写入 store_id。",
            )
        ]
    )[0]
    fg = build_fine_grained_lineage_class(group)
    assert fg.transformOperation == (
        "/* 中文解释：取 ods.store_info 表中的 id 字段，表示将门店 ID 转为 bigint 类型写入 store_id。 */\n"
        "cast(id as bigint)"
    )
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


def test_import_reviewed_write_without_approved_exits_4(tmp_path: Path) -> None:
    from job_info_sync_datahub.field_lineage_cli import EXIT_NO_APPROVED_ROWS

    workbook = tmp_path / "review.xlsx"
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
                    confidence="MEDIUM",
                )
            ],
            unresolved_fields=[],
            llm_model="deepseek-test",
        )

    exit_code = field_lineage_cli_main(
        [
            "import-reviewed",
            "--input",
            str(workbook),
            "--write",
            "--gms-url",
            "http://localhost:8080",
        ]
    )

    assert exit_code == EXIT_NO_APPROVED_ROWS
