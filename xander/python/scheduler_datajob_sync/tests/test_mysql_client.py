from datetime import datetime

from scheduler_datajob_sync.models import parse_upstream_jobs
from scheduler_datajob_sync.mysql_client import SchedulerMysqlClient


class FakeCursor:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.closed = False

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.executed.append((sql, params))

    def fetchall(self) -> list[dict[str, object]]:
        return self.rows

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.cursor_obj = FakeCursor(rows)
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self.cursor_obj

    def close(self) -> None:
        self.closed = True


def _row(job_display_name: str = "PDW_Example_Job") -> dict[str, object]:
    return {
        "id": 1,
        "job_name": job_display_name.lower(),
        "job_display_name": job_display_name,
        "shell_commond": "echo hello",
        "upstream_jobs": '["upstream_a"]',
    }


def test_parse_upstream_jobs_accepts_json_commas_and_unknown_values() -> None:
    assert parse_upstream_jobs('["job_a", "job_b"]') == ["job_a", "job_b"]
    assert parse_upstream_jobs("job_a, job_b") == ["job_a", "job_b"]
    assert parse_upstream_jobs("UNKNOWN_UPSTREAM_JOBS") == []
    assert parse_upstream_jobs("") == []
    assert parse_upstream_jobs(None) == []


def test_fetch_job_queries_by_display_name_and_maps_metadata() -> None:
    connection = FakeConnection([_row()])
    client = SchedulerMysqlClient(connection_factory=lambda: connection)

    metadata = client.fetch_job("PDW_Example_Job")

    assert metadata.job_display_name == "PDW_Example_Job"
    sql, params = connection.cursor_obj.executed[0]
    assert "FROM dmp_schedule_job_basic_info" in sql
    assert "job_display_name = %s" in sql
    assert "batch_exec_time >= %s" in sql
    assert params == ("2026-06-11 20:14:42", "PDW_Example_Job")
    assert connection.cursor_obj.closed
    assert connection.closed


def test_fetch_jobs_by_prefix_uses_like_parameter() -> None:
    connection = FakeConnection([_row("PDW_A"), _row("PDW_B")])
    client = SchedulerMysqlClient(connection_factory=lambda: connection)

    jobs = client.fetch_jobs_by_prefix("PDW_")

    assert [job.job_display_name for job in jobs] == ["PDW_A", "PDW_B"]
    sql, params = connection.cursor_obj.executed[0]
    assert "job_display_name LIKE %s" in sql
    assert "batch_exec_time >= %s" in sql
    assert params == ("2026-06-11 20:14:42", "PDW_%")


def test_fetch_jobs_activity_since_filters_build_and_batch_times() -> None:
    connection = FakeConnection([_row("PDW_Active")])
    client = SchedulerMysqlClient(connection_factory=lambda: connection)
    since = datetime(2026, 6, 12, 8, 0, 0)

    jobs = client.fetch_jobs_activity_since(since)

    assert [job.job_display_name for job in jobs] == ["PDW_Active"]
    sql, params = connection.cursor_obj.executed[0]
    assert "last_build_start_time >= %s" in sql
    assert "build_update_time >= %s" in sql
    assert params == ("2026-06-11 20:14:42", since, since)


def test_fetch_jobs_updated_since_filters_updated_or_batch_exec_time() -> None:
    connection = FakeConnection([_row("PDW_Updated")])
    client = SchedulerMysqlClient(connection_factory=lambda: connection)
    since = datetime(2026, 6, 10, 0, 0, 0)

    jobs = client.fetch_jobs_updated_since(since)

    assert [job.job_display_name for job in jobs] == ["PDW_Updated"]
    sql, params = connection.cursor_obj.executed[0]
    assert "updated_time >= %s" in sql
    assert "batch_exec_time >= %s" in sql
    assert params == ("2026-06-11 20:14:42", since, since)


def test_client_allows_overriding_min_batch_exec_time() -> None:
    connection = FakeConnection([_row()])
    client = SchedulerMysqlClient(
        connection_factory=lambda: connection,
        min_batch_exec_time="2026-06-12 00:00:00",
    )

    client.fetch_job("PDW_Example_Job")

    _sql, params = connection.cursor_obj.executed[0]
    assert params == ("2026-06-12 00:00:00", "PDW_Example_Job")
