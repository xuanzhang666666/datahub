from __future__ import annotations

from job_info_sync_datahub.table_documentation_from_dataset_props import (
    load_table_report_state,
    select_tables_for_batch,
)


def test_load_table_report_state_last_row_per_table_wins(tmp_path) -> None:
    report = tmp_path / "table_documentation_report.jsonl"
    report.write_text(
        "\n".join(
            [
                '{"table":"pdw.a","status":"FAIL"}',
                '{"table":"pdw.a","status":"OK","documentation_status":"WRITTEN"}',
                '{"table":"ods.pdw_b","status":"SKIP","documentation_status":"SKIP_VIEW_DATASET"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    state = load_table_report_state(str(report))

    assert state["pdw.a"]["status"] == "OK"
    assert state["ods.pdw_b"]["status"] == "SKIP"


def test_select_tables_for_batch_resume_skips_ok_and_skip(tmp_path) -> None:
    report = tmp_path / "table_documentation_report.jsonl"
    report.write_text(
        '{"table":"pdw.done","status":"OK"}\n{"table":"pdw.skip","status":"SKIP"}\n',
        encoding="utf-8",
    )
    all_tables = ["pdw.done", "pdw.skip", "pdw.pending", "ods.pdw_retry"]

    pending, stats = select_tables_for_batch(
        all_tables,
        resume=True,
        report_path=str(report),
    )

    assert pending == ["pdw.pending", "ods.pdw_retry"]
    assert stats["skipped_completed"] == 2
    assert stats["pending"] == 2
    assert stats["total_in_file"] == 4


def test_select_tables_for_batch_without_resume_returns_all() -> None:
    pending, stats = select_tables_for_batch(
        ["a.b", "c.d"],
        resume=False,
        report_path="/nonexistent/report.jsonl",
    )

    assert pending == ["a.b", "c.d"]
    assert stats["pending"] == 2
    assert stats["skipped_completed"] == 0


def test_select_tables_for_batch_resume_empty_when_all_done(tmp_path) -> None:
    report = tmp_path / "table_documentation_report.jsonl"
    report.write_text('{"table":"pdw.a","status":"OK"}\n', encoding="utf-8")

    pending, stats = select_tables_for_batch(
        ["pdw.a"],
        resume=True,
        report_path=str(report),
    )

    assert pending == []
    assert stats["pending"] == 0
