"""Table lineage sync from DataHub table structured properties."""

from __future__ import annotations

import json

from job_info_sync_datahub import table_lineage_from_dataset_props as mod
from job_info_sync_datahub.lineage_write_policy import LineageWriteDecision
from job_info_sync_datahub.models import TableLineage, TableRef
from job_info_sync_datahub.structured_properties import URN_ETL_SCRIPT, URN_EXECUTE_SHELL


def _payload(etl_script: str = "", execute_shell: str = "") -> dict:
    return {
        "structuredProperties": {
            "value": {
                "properties": [
                    {"propertyUrn": URN_ETL_SCRIPT, "values": [{"string": etl_script}]},
                    {"propertyUrn": URN_EXECUTE_SHELL, "values": [{"string": execute_shell}]},
                ]
            }
        }
    }


def test_choose_structured_etl_source_prefers_etl_script() -> None:
    source = mod.choose_structured_etl_source(
        _payload(etl_script="insert overwrite table dw.t select * from ods.a;", execute_shell="sh run.sh"),
        input_table="dw.t",
        dataset_urn="urn:dataset:dw.t",
    )

    assert source is not None
    assert source.content == "insert overwrite table dw.t select * from ods.a;"
    assert source.source_property == "Etl Script"
    assert source.job_file_name == "dw.t.structured_property.job"


def test_choose_structured_etl_source_falls_back_to_execute_shell() -> None:
    source = mod.choose_structured_etl_source(
        _payload(execute_shell="hive -e 'insert overwrite table dw.t select * from ods.b'"),
        input_table="dw.t",
        dataset_urn="urn:dataset:dw.t",
    )

    assert source is not None
    assert source.content == "hive -e 'insert overwrite table dw.t select * from ods.b'"
    assert source.source_property == "Execute Shell"
    assert source.job_file_name == "dw.t.structured_property.sh"


def test_choose_structured_etl_source_returns_none_when_both_empty() -> None:
    assert (
        mod.choose_structured_etl_source(
            _payload(),
            input_table="dw.t",
            dataset_urn="urn:dataset:dw.t",
        )
        is None
    )


def test_sync_one_table_skips_view_dataset(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(mod, "is_view_dataset", lambda *args, **kwargs: True)

    result = mod.sync_one_table(
        "data_sec_dw.ods_view_table",
        gms_url="http://localhost:8080",
        token=None,
        platform_instance="blf-prod-hive",
        env="PROD",
        dry_run=True,
        replace_existing_lineage=True,
        llm_timeout_sec=1,
        audit_jsonl=str(tmp_path / "audit.jsonl"),
        batch_output_dir=str(tmp_path),
    )

    assert result["status"] == "SKIP"
    assert result["lineage_status"] == "SKIP_VIEW_DATASET"
    assert result["write_upstream_lineage"] is False


def test_check_mode_reports_missing_and_extra_upstreams_without_writing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(mod, "is_view_dataset", lambda *args, **kwargs: False)
    monkeypatch.setattr(mod, "fetch_aspect_payload", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        mod,
        "_fetch_structured_properties_or_empty",
        lambda *args, **kwargs: _payload(etl_script="insert overwrite table dw.target select * from ods.expected;"),
    )
    decision = LineageWriteDecision(
        write_upstream_lineage=True,
        status="LLM_EXTRACTED",
        reason="ok",
        trust_score=90,
        selected_targets={"dw.target"},
        selected_upstreams={"ods.expected"},
        deepseek_targets={"dw.target"},
        deepseek_upstreams={"ods.expected"},
    )
    monkeypatch.setattr(
        mod,
        "evaluate_llm_only",
        lambda *args, **kwargs: (
            [TableLineage(target=TableRef("dw", "target"), upstreams=[TableRef("ods", "expected")])],
            decision,
            {"lineage": []},
        ),
    )
    monkeypatch.setattr(
        mod,
        "fetch_existing_upstream_names",
        lambda *args, **kwargs: {"ods.extra"},
    )

    class FailingWriter:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("check mode must not construct DatahubWriter")

    monkeypatch.setattr(mod, "DatahubWriter", FailingWriter)

    result = mod.sync_one_table(
        "data_takeaway.pdw_order_target_table_di",
        gms_url="http://localhost:8080",
        token=None,
        platform_instance="blf-prod-hive",
        env="PROD",
        dry_run=False,
        replace_existing_lineage=True,
        check_existing_lineage=True,
        llm_timeout_sec=1,
        audit_jsonl=str(tmp_path / "audit.jsonl"),
        batch_output_dir=str(tmp_path),
    )

    assert result["status"] == "FAIL"
    assert result["fail_category"] == "LINEAGE_MISMATCH"
    assert result["lineage_status"] == "CHECK_MISMATCH"
    assert result["expected_upstreams"] == ["ods.expected"]
    assert result["existing_upstreams"] == ["ods.extra"]
    assert result["missing_upstreams"] == ["ods.expected"]
    assert result["extra_upstreams"] == ["ods.extra"]
    assert result["write_upstream_lineage"] is False


def test_sync_one_table_uses_documentation_section_four(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(mod, "is_view_dataset", lambda *args, **kwargs: False)
    monkeypatch.setenv("BLF_LINEAGE_SKIP_HIVE_EXISTENCE_CHECK", "1")
    documentation = "\n".join(
        [
            "## 表加工逻辑说明",
            "### 1. 表用途概览",
            "### 2. 表结构 DDL",
            "### 4. 数据来源",
            "",
            "| 上游表 | 用途 |",
            "| --- | --- |",
            "| `default.ods_order_source_di` | 订单 |",
            "| `data_takeaway.pdw_logistics_management_distribution_view` | 物流 |",
            "",
            "### 5. 使用到的上游表字段",
        ]
    )
    monkeypatch.setattr(
        mod,
        "fetch_aspect_payload",
        lambda *args, **kwargs: {
            "editableDatasetProperties": {"value": {"description": documentation}}
        },
    )
    monkeypatch.setattr(mod, "fetch_existing_upstream_names", lambda *args, **kwargs: set())

    def _fail_llm(*args, **kwargs):  # noqa: ANN001
        raise AssertionError("evaluate_llm_only should not run when Documentation exists")

    monkeypatch.setattr(mod, "evaluate_llm_only", _fail_llm)

    result = mod.sync_one_table(
        "data_takeaway.pdw_order_target_table_di",
        gms_url="http://localhost:8080",
        token=None,
        platform_instance="blf-prod-hive",
        env="PROD",
        dry_run=True,
        replace_existing_lineage=True,
        llm_timeout_sec=1,
        audit_jsonl=str(tmp_path / "audit.jsonl"),
        batch_output_dir=str(tmp_path),
    )

    assert result["status"] == "OK"
    assert result["source_property"] == "Documentation"
    assert result["lineage_status"] == "DOC_EXTRACTED"
    assert result["upstream_count"] == 2
    assert set(result.get("upstreams_input_table") or []) == {
        "default.ods_order_source_di",
        "data_takeaway.pdw_logistics_management_distribution_view",
    }


def test_summarize_report_prints_only_table_name_for_check_match(capsys, tmp_path) -> None:
    report = tmp_path / "batch_report_table_list.jsonl"
    report.write_text(
        json.dumps(
            {
                "input_table": "dw.matched",
                "status": "OK",
                "lineage_status": "CHECK_MATCH",
                "target_table": "dw.matched",
                "upstream_count": 2,
                "source_property": "Etl Script",
                "etl_file_export_path": "/tmp/etl.job",
                "llm_raw_export_path": "/tmp/llm.json",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    mod.summarize_report(str(report))

    out = capsys.readouterr().out
    assert "总计: 1  OK: 1  SKIP: 0  FAIL: 0" in out
    assert "报告明细:" in out
    assert "  dw.matched\n" in out
    assert "etl_snapshot=/tmp/etl.job" not in out
    assert "llm_raw=/tmp/llm.json" not in out


def test_summarize_report_prints_diff_details_for_check_mismatch(capsys, tmp_path) -> None:
    report = tmp_path / "batch_report_table_list.jsonl"
    report.write_text(
        json.dumps(
            {
                "input_table": "dw.target",
                "status": "FAIL",
                "fail_category": "LINEAGE_MISMATCH",
                "lineage_status": "CHECK_MISMATCH",
                "target_table": "dw.target",
                "upstream_count": 2,
                "source_property": "Etl Script",
                "missing_upstreams": ["ods.missing"],
                "extra_upstreams": ["ods.extra"],
                "etl_file_export_path": "/tmp/etl.job",
                "llm_raw_export_path": "/tmp/llm.json",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    mod.summarize_report(str(report))

    out = capsys.readouterr().out
    assert "总计: 1  OK: 0  SKIP: 0  FAIL: 1" in out
    assert "报告明细:" in out
    assert "[FAIL] table=dw.target lineage=CHECK_MISMATCH target=dw.target upstreams=2 source_property=Etl Script" in out
    assert "missing_upstreams(1): ods.missing" in out
    assert "extra_upstreams(1): ods.extra" in out
    assert "etl_snapshot=/tmp/etl.job" in out
    assert "llm_raw=/tmp/llm.json" in out
