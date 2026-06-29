from __future__ import annotations

import importlib.util
from datetime import date, datetime
from pathlib import Path


def _load_module():
    script_path = Path(__file__).resolve().parents[1] / "list_no_downstream_jobs.py"
    spec = importlib.util.spec_from_file_location("list_no_downstream_jobs", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_jenkins_status_exists_when_batch_time_is_recent() -> None:
    module = _load_module()

    status, batch_time = module.classify_jenkins_job_status(
        "active_job",
        {"active_job": datetime(2026, 6, 22, 9, 30, 0)},
        latest_batch_exec_time=datetime(2026, 6, 22, 10, 0, 0),
    )

    assert status == "存在"
    assert batch_time == "2026-06-22 09:30:00"


def test_jenkins_status_suspects_deleted_when_batch_time_is_stale() -> None:
    module = _load_module()

    status, batch_time = module.classify_jenkins_job_status(
        "stale_job",
        {"stale_job": datetime(2026, 6, 22, 7, 59, 0)},
        latest_batch_exec_time=datetime(2026, 6, 22, 10, 0, 0),
    )

    assert status == "疑似已删除"
    assert batch_time == "2026-06-22 07:59:00"


def test_jenkins_status_exists_at_exact_threshold() -> None:
    module = _load_module()

    status, batch_time = module.classify_jenkins_job_status(
        "threshold_job",
        {"threshold_job": datetime(2026, 6, 22, 8, 0, 0)},
        latest_batch_exec_time=datetime(2026, 6, 22, 10, 0, 0),
    )

    assert status == "存在"
    assert batch_time == "2026-06-22 08:00:00"


def test_jenkins_status_suspects_deleted_when_job_missing() -> None:
    module = _load_module()

    status, batch_time = module.classify_jenkins_job_status(
        "missing_job",
        {"other_job": datetime(2026, 6, 22, 10, 0, 0)},
        latest_batch_exec_time=datetime(2026, 6, 22, 10, 0, 0),
    )

    assert status == "疑似已删除"
    assert batch_time == ""


def test_jenkins_status_suspects_deleted_when_batch_time_is_none() -> None:
    module = _load_module()

    status, batch_time = module.classify_jenkins_job_status(
        "null_job",
        {"null_job": None},
        latest_batch_exec_time=datetime(2026, 6, 22, 10, 0, 0),
    )

    assert status == "疑似已删除"
    assert batch_time == ""


def test_build_history_since_time_uses_one_calendar_month() -> None:
    module = _load_module()

    assert module._one_month_ago_start(date(2026, 6, 27)) == "2026-05-27 00:00:00"
    assert module._one_month_ago_start(date(2026, 3, 31)) == "2026-02-28 00:00:00"


def test_build_history_stats_sql_uses_latest_dt_and_recent_month() -> None:
    module = _load_module()

    sql = module.build_job_build_stats_sql(
        table="default.pdw_data_platform_dmp_schedule_build_history",
        latest_dt="20260627",
        since_time="2026-05-27 00:00:00",
        recent_7d_since_time="2026-06-20 00:00:00",
    )

    assert "WHERE dt = '20260627'" in sql
    assert "build_time >= '2026-05-27 00:00:00'" in sql
    assert "COUNT(DISTINCT build_id) AS build_cnt" in sql
    assert "success_build_rate" in sql
    assert "recent_7d_success_build_cnt" in sql
    assert "build_time >= '2026-06-20 00:00:00'" in sql
    assert "GROUP BY job_name" in sql


def test_normalize_build_history_stats_maps_rows_by_job_name() -> None:
    module = _load_module()

    stats = module.normalize_build_history_stats([
        (
            "pdw_order",
            10,
            8,
            5,
            0.8,
            datetime(2026, 6, 27, 12, 30, 0),
            "queue-a",
            "agent-a",
            "xuan",
            3,
            42,
        )
    ])

    assert stats == {
        "pdw_order": {
            "build_cnt": "10",
            "success_build_cnt": "8",
            "success_build_rate": "0.8000",
            "last_build_time": "2026-06-27 12:30:00",
            "queue": "queue-a",
            "agent": "agent-a",
            "trigger_user": "xuan",
            "avg_rmb": "3",
            "avg_duration_min": "42",
            "recent_7d_success_build_cnt": "5",
        }
    }


def test_excel_numeric_fields_are_converted_to_numbers() -> None:
    module = _load_module()

    assert module._excel_cell_value_and_format("job_size", "1076416512.00") == (
        1026.55,
        "0.00",
    )
    assert module._excel_cell_value_and_format("job_count", "") == ("", None)
    assert module._excel_cell_value_and_format("success_build_rate", "0.8") == (
        0.8,
        "0.00%",
    )
    assert module._excel_cell_value_and_format("avg_rmb", "3.0") == (3, "0")
    assert module._excel_cell_value_and_format("job_name", "pdw_order") == (
        "pdw_order",
        None,
    )
