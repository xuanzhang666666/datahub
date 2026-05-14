"""dmp_batch_exec_time_online_sql_clause 行为测试。"""

from __future__ import annotations

import pytest

from job_info_sync_datahub.schedule_client import dmp_batch_exec_time_online_sql_clause


@pytest.fixture(autouse=True)
def _clear_dmp_min_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DMP_MIN_BATCH_EXEC_TIME", raising=False)


def test_clause_default_contains_timestamp_and_try_cast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DMP_MIN_BATCH_EXEC_TIME", raising=False)
    s = dmp_batch_exec_time_online_sql_clause()
    assert "TRY_CAST(batch_exec_time AS TIMESTAMP)" in s
    assert "2026-05-14 18:30:11" in s
    assert ">=" in s


def test_clause_empty_env_disables_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DMP_MIN_BATCH_EXEC_TIME", "")
    assert dmp_batch_exec_time_online_sql_clause() == ""


def test_clause_escapes_single_quote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DMP_MIN_BATCH_EXEC_TIME", "2026-01-01 00:00:00")
    s = dmp_batch_exec_time_online_sql_clause()
    assert "TIMESTAMP '2026-01-01 00:00:00'" in s
