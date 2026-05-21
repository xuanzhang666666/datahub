"""Table lineage sync from DataHub dataset structured properties."""

from __future__ import annotations

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
        "dw.target",
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
