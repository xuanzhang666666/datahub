"""batch_sync ETL script export helpers."""

from __future__ import annotations

from pathlib import Path

from job_info_sync_datahub.batch_sync import (
    describe_final_etl_choice,
    describe_etl_source,
    etl_file_locations,
    export_etl_script_snapshot,
    export_llm_raw_snapshot,
)
from job_info_sync_datahub.export_lineage_excel import build_rows


def test_export_etl_script_snapshot_writes_job_file(tmp_path: Path) -> None:
    exported = export_etl_script_snapshot(
        batch_output_dir=str(tmp_path),
        job_display_name="dw_sku_store_sku_inventory_history_v1",
        job_file_name="dw_sku_store_sku_inventory_history_v1.job",
        etl_content="select * from default.dim_store_info;\n",
    )

    assert exported == str(
        tmp_path
        / "etl_scripts"
        / "dw_sku_store_sku_inventory_history_v1__dw_sku_store_sku_inventory_history_v1.job"
    )
    assert Path(exported).read_text(encoding="utf-8") == "select * from default.dim_store_info;\n"


def test_export_lineage_excel_rows_include_etl_snapshot_path(tmp_path: Path) -> None:
    report = tmp_path / "batch_report.jsonl"
    audit = tmp_path / "lineage_audit.jsonl"
    raw = tmp_path / "llm_raw" / "dw_job.json"
    raw.parent.mkdir()
    raw.write_text('{"lineage":[],"notes":"LLM raw note"}', encoding="utf-8")
    report.write_text(
        '{"job":"dw_job","status":"SKIP","lineage_status":"SKIP_HIVE_TABLE_NOT_FOUND",'
        '"etl_file_export_path":"/workspace/lineage_reports/etl_scripts/dw_job__dw_job.job",'
        f'"llm_raw_export_path":"{raw}"}}\n',
        encoding="utf-8",
    )
    audit.write_text(
        '{"job":"dw_job","trust_score":0,"targets_deepseek":["default.dw_job"],'
        '"sources_deepseek":["default.missing_upstream"]}\n',
        encoding="utf-8",
    )

    rows = build_rows(report, audit)

    assert rows[0][11] == "/workspace/lineage_reports/etl_scripts/dw_job__dw_job.job"
    assert '{"lineage":[],"notes":"LLM raw note"}' in rows[0]
    assert len(rows[0]) == 17


def test_export_lineage_excel_rows_include_check_mode_diff_fields(tmp_path: Path) -> None:
    report = tmp_path / "batch_report.jsonl"
    report.write_text(
        '{"job":"dw_table","status":"FAIL","lineage_status":"CHECK_MISMATCH",'
        '"expected_upstreams":["ods.expected"],"existing_upstreams":["ods.extra"],'
        '"missing_upstreams":["ods.expected"],"extra_upstreams":["ods.extra"],'
        '"source_property":"Etl Script"}\n',
        encoding="utf-8",
    )

    rows = build_rows(report, None)

    assert rows[0][12] == "ods.expected"
    assert rows[0][13] == "ods.extra"
    assert rows[0][14] == "ods.expected"
    assert rows[0][15] == "ods.extra"
    assert rows[0][16] == "Etl Script"


def test_export_llm_raw_snapshot_writes_json(tmp_path: Path) -> None:
    exported = export_llm_raw_snapshot(
        batch_output_dir=str(tmp_path),
        job_display_name="dw_job",
        llm_raw={"lineage": [{"target": {"db": "default", "table": "dw_job"}}]},
    )

    assert exported == str(tmp_path / "llm_raw" / "dw_job.json")
    assert '"dw_job"' in Path(exported).read_text(encoding="utf-8")


def test_describe_etl_source_includes_gitlab_project_and_url() -> None:
    detail = describe_etl_source(
        gitlab_name="thrall",
        project_path="smart-order/thrall",
        etl_file_path="gitlab:jobs/dw_sku/example.job",
        etl_file_source="gitlab_preferred_same",
        ref="master",
    )

    assert "gitlab_project=smart-order/thrall" in detail
    assert "gitlab_url=https://git.corp.bianlifeng.com/smart-order/thrall/-/blob/master/jobs/dw_sku/example.job" in detail


def test_describe_etl_source_includes_local_absolute_path(monkeypatch) -> None:
    monkeypatch.setenv("BLF_ETL_LOCAL_ROOT", "/localfolder")

    detail = describe_etl_source(
        gitlab_name="thrall",
        project_path="smart-order/thrall",
        etl_file_path="localfolder:thrall/jobs/dw_sku/example.job",
        etl_file_source="local",
        ref="master",
    )

    assert "local_path=/localfolder/thrall/jobs/dw_sku/example.job" in detail


def test_describe_final_etl_choice_marks_local_differs_as_localfolder() -> None:
    detail = describe_final_etl_choice(
        gitlab_name="analysis-jobs",
        project_path="data/analysis-jobs",
        etl_file_path="localfolder:analysis-jobs/jobs/dw_sku/example.job",
        etl_file_source="local_differs",
    )

    assert detail == "最终文件来源于 /localfolder/analysis-jobs/jobs/dw_sku/example.job"


def test_etl_file_locations_include_gitlab_and_local_paths(monkeypatch) -> None:
    monkeypatch.setenv("BLF_ETL_LOCAL_ROOT", "/localfolder")

    locations = etl_file_locations(
        gitlab_name="analysis-jobs",
        project_path="data/analysis-jobs",
        etl_file_path="localfolder:analysis-jobs/jobs/dw_sku/dw_sku_store_sku_inventory_history_v1.job",
    )

    assert locations == {
        "file_name": "dw_sku_store_sku_inventory_history_v1.job",
        "gitlab_project": "data/analysis-jobs",
        "gitlab_full_path": (
            "https://git.corp.bianlifeng.com/data/analysis-jobs/-/blob/master/"
            "jobs/dw_sku/dw_sku_store_sku_inventory_history_v1.job"
        ),
        "local_full_path": (
            "/localfolder/analysis-jobs/jobs/dw_sku/"
            "dw_sku_store_sku_inventory_history_v1.job"
        ),
    }
