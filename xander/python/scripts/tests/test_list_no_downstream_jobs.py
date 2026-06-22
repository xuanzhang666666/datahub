from __future__ import annotations

import importlib.util
from datetime import datetime
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
