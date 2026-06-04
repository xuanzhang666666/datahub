from __future__ import annotations

import json
from pathlib import Path

from job_info_sync_datahub.algorithm_field_contract import (
    MISSING_ETL_SCRIPT,
    NO_RUNTIME_VARS,
    OK,
    analyze_table_payload,
    extract_dataset_job_properties,
    parse_execute_shell_variables,
    resolve_etl_script,
    table_names_from_env,
    write_contract_outputs,
)
from job_info_sync_datahub.structured_properties import (
    URN_ETL_SCRIPT,
    URN_EXECUTE_SHELL,
    URN_SCHEDULE_URL,
)


def _payload(
    *,
    etl_script: str = "",
    execute_shell: str = "",
    schedule_url: str = "",
) -> dict:
    return {
        "structuredProperties": {
            "value": {
                "properties": [
                    {"propertyUrn": URN_ETL_SCRIPT, "values": [{"string": etl_script}]},
                    {"propertyUrn": URN_EXECUTE_SHELL, "values": [{"string": execute_shell}]},
                    {"propertyUrn": URN_SCHEDULE_URL, "values": [{"string": schedule_url}]},
                ]
            }
        }
    }


def test_extract_dataset_job_properties_reads_three_structured_properties() -> None:
    props = extract_dataset_job_properties(
        _payload(
            etl_script="```sql\nselect 1\n```",
            execute_shell="```shell\nDATE=20260603 sh run.sh\n```",
            schedule_url="https://schedule/job/a",
        )
    )

    assert props.etl_script == "select 1"
    assert props.execute_shell == "DATE=20260603 sh run.sh"
    assert props.schedule_url == "https://schedule/job/a"


def test_parse_execute_shell_variables_supports_assignments_exports_flags_and_positionals() -> None:
    ctx = parse_execute_shell_variables(
        "DATE=20260603 export DATABASE=default /bin/w-run-task.sh foo/bar "
        "--hive_table_name=dw.target --bizdate 20260602 prod"
    )

    assert ctx.variables["DATE"] == "20260603"
    assert ctx.variables["DATABASE"] == "default"
    assert ctx.variables["hive_table_name"] == "dw.target"
    assert ctx.variables["bizdate"] == "20260602"
    assert ctx.positionals[-1] == "prod"
    assert ctx.variables["DATE_SUB1DAY"] == "20260602"
    assert ctx.variables["FDATE_SUB1DAY"] == "2026-06-02"


def test_resolve_etl_script_uses_execute_shell_variables_and_reports_unresolved() -> None:
    runtime = parse_execute_shell_variables(
        "DATE=20260603 --hive_table_name=dw.target"
    )

    resolved = resolve_etl_script(
        "insert overwrite table ${hive_table_name} "
        "select * from ods.source where dt='${DATE_SUB1DAY}' and shop=$SHOP;"
        "select '{{ DATE }}';",
        runtime,
    )

    assert "dw.target" in resolved.resolved_script
    assert "20260602" in resolved.resolved_script
    assert "20260603" in resolved.resolved_script
    assert "$SHOP" in resolved.resolved_script
    assert "SHOP" in resolved.unresolved_variables


def test_analyze_table_payload_marks_missing_etl_and_no_runtime_vars() -> None:
    missing = analyze_table_payload("dw.target", "urn:dataset", _payload())
    assert missing.status == MISSING_ETL_SCRIPT

    no_runtime = analyze_table_payload(
        "dw.target",
        "urn:dataset",
        _payload(
            etl_script=(
                "$HIVE <<EOF\n"
                "insert overwrite table dw.target select id from ods.source;\n"
                "EOF"
            )
        ),
    )
    assert no_runtime.status == NO_RUNTIME_VARS
    assert no_runtime.table_lineages[0].upstreams[0].full_name == "ods.source"


def test_analyze_table_payload_parses_resolved_etl_field_lineage() -> None:
    result = analyze_table_payload(
        "dw.target",
        "urn:dataset",
        _payload(
            schedule_url="https://schedule/job/target",
            execute_shell="DATE=20260603 TARGET_TABLE=dw.target",
            etl_script=(
                "$HIVE <<EOF\n"
                "insert overwrite table ${TARGET_TABLE} "
                "select s.id as sku_id, s.qty from ods.source s "
                "where s.dt='${DATE_SUB1DAY}';\n"
                "EOF"
            ),
        ),
    )

    assert result.status == OK
    assert result.unresolved_variables == []
    assert result.resolved_etl_script is not None
    assert "20260602" in result.resolved_etl_script
    assert result.table_lineages[0].target.full_name == "dw.target"
    assert result.table_lineages[0].upstreams[0].full_name == "ods.source"
    by_field = {lineage.target_field: lineage for lineage in result.field_lineages}
    assert by_field["sku_id"].mappings[0].source_field == "id"
    assert by_field["qty"].mappings[0].source_field == "qty"


def test_analyze_table_payload_marks_target_fields_without_lineage_as_open_questions() -> None:
    result = analyze_table_payload(
        "dw.target",
        "urn:dataset",
        _payload(
            execute_shell="TARGET_TABLE=dw.target",
            etl_script=(
                "$HIVE <<EOF\n"
                "insert overwrite table ${TARGET_TABLE} select id from ods.source;\n"
                "EOF"
            ),
        ),
        target_fields=["id", "missing_field"],
    )

    assert "missing_field" in result.unresolved_target_fields
    assert any("missing_field" in question for question in result.open_questions)


def test_write_contract_outputs_creates_json_markdown_and_excel(tmp_path: Path) -> None:
    result = analyze_table_payload(
        "dw.target",
        "urn:dataset",
        _payload(
            schedule_url="https://schedule/job/target",
            execute_shell="TARGET_TABLE=dw.target",
            etl_script="$HIVE <<EOF\ninsert overwrite table ${TARGET_TABLE} select id from ods.source;\nEOF",
        ),
    )

    write_contract_outputs([result], tmp_path)

    assert (tmp_path / "field_lineage.json").exists()
    assert (tmp_path / "runtime_context.json").exists()
    assert (tmp_path / "lineage_report.md").exists()
    assert (tmp_path / "open_questions.md").exists()
    assert (tmp_path / "field_contract.xlsx").exists()

    payload = json.loads((tmp_path / "field_lineage.json").read_text())
    assert payload["tables"][0]["table_name"] == "dw.target"
    assert payload["tables"][0]["field_lineages"][0]["target_field"] == "id"


def test_table_names_from_env_accepts_jenkins_multiline_and_comma_separated(monkeypatch) -> None:
    monkeypatch.setenv(
        "TABLE_NAMES",
        "default.table_a\n table_b,ods.table_c，dim.table_d \n# comment\n",
    )

    assert table_names_from_env() == [
        "default.table_a",
        "table_b",
        "ods.table_c",
        "dim.table_d",
    ]
