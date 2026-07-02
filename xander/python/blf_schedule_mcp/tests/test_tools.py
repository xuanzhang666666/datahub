from __future__ import annotations

from typing import Any

from blf_schedule_mcp.datahub_client import DataHubClientError
from blf_schedule_mcp.tools import (
    diagnose_schedule_job_failure,
    find_long_running_schedule_builds,
    get_schedule_job_build_history,
    get_schedule_job_build_log,
    get_schedule_job_build_status,
    get_schedule_job_last_failure,
    get_schedule_job_queue_stats,
    rebuild_schedule_job_build,
    search_schedule_jobs,
    trigger_schedule_job_single_build,
    trigger_schedule_job_build,
)


class FakeJenkinsClient:
    def __init__(self, result: str = "SUCCESS") -> None:
        self.result = result
        self.log_calls = 0
        self.last_log_max_bytes: int | None = None
        self.log_text = (
            "starting\nERROR: Table not found: default.ods_xxx\n"
            "java.lang.OutOfMemoryError: GC overhead\nfinished"
        )
        self.running_builds: list[dict[str, Any]] = []
        self.queue_items: list[dict[str, Any]] = []
        self.trigger_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.single_build_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.rebuild_calls: list[tuple[str, int, dict[str, Any] | None]] = []

    def get_job_info(self, job_name: str) -> dict[str, Any]:
        return {"builds": [{"number": 4}, {"number": 3}, {"number": 2}, {"number": 1}]}

    def get_build_info(self, job_name: str, build_ref: int | str) -> dict[str, Any]:
        return {
            "number": 4,
            "result": self.result,
            "timestamp": 1781233200000,
            "duration": 47000,
            "url": "https://schedule.corp.bianlifeng.com/job/demo/4/",
            "building": False,
        }

    def get_last_failed_build(self, job_name: str) -> dict[str, Any]:
        return {
            "number": 3,
            "result": "FAILURE",
            "timestamp": 1781233100000,
            "duration": 90000,
            "url": "https://schedule.corp.bianlifeng.com/job/demo/3/",
            "building": False,
        }

    def get_build_history(self, job_name: str, limit: int) -> list[dict[str, Any]]:
        return [
            {"number": 4, "result": "SUCCESS", "timestamp": 1781233200000, "duration": 1000, "url": "", "building": False},
            {"number": 3, "result": "FAILURE", "timestamp": 1781233100000, "duration": 2000, "url": "", "building": False},
            {"number": 2, "result": "SUCCESS", "timestamp": 1781233000000, "duration": 3000, "url": "", "building": False},
        ][:limit]

    def get_build_log(self, job_name: str, build_ref: int | str, *, max_bytes: int = 65536) -> str:
        self.log_calls += 1
        self.last_log_max_bytes = max_bytes
        return self.log_text

    def get_running_builds(self) -> list[dict[str, Any]]:
        return self.running_builds

    def get_queue_items(self) -> list[dict[str, Any]]:
        return self.queue_items

    def trigger_build(
        self,
        job_name: str,
        *,
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.trigger_calls.append((job_name, parameters))
        return {
            "endpoint": "/job/{name}/buildWithParameters",
            "queue_url": "https://schedule.corp.bianlifeng.com/queue/item/456/",
            "queue_id": 456,
            "response_text": "",
        }

    def trigger_single_build(
        self,
        job_name: str,
        *,
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.single_build_calls.append((job_name, parameters))
        return {
            "endpoint": "/job/{name}/build1?delay=0sec&singleBuild=true",
            "queue_url": "https://schedule.corp.bianlifeng.com/queue/item/457/",
            "queue_id": 457,
            "response_text": "",
        }

    def rebuild_build(
        self,
        job_name: str,
        build_number: int,
        *,
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.rebuild_calls.append((job_name, build_number, parameters))
        return {
            "endpoint": "/job/{name}/{build}/rebuild/parameterized",
            "queue_url": "https://schedule.corp.bianlifeng.com/queue/item/458/",
            "queue_id": 458,
            "response_text": "",
        }


def test_get_schedule_job_build_status_success_has_no_risks() -> None:
    result = get_schedule_job_build_status(FakeJenkinsClient("SUCCESS"), job_display_name="demo")
    assert result["success"] is True
    assert result["summary"]["status"] == "success"
    assert result["risks"] == []


def test_get_schedule_job_build_status_failure_has_risk() -> None:
    result = get_schedule_job_build_status(FakeJenkinsClient("FAILURE"), job_display_name="demo")
    assert result["summary"]["status"] == "failed"
    assert result["risks"] == ["最近构建失败，请检查日志"]


def test_get_schedule_job_build_history_success_rate() -> None:
    result = get_schedule_job_build_history(FakeJenkinsClient(), job_display_name="demo", limit=3)
    assert result["summary"]["success_rate"] == "2/3"
    assert result["summary"]["last_success_build"] == 4
    assert result["summary"]["last_failure_build"] == 3


def test_get_schedule_job_last_failure_extracts_error_excerpt() -> None:
    client = FakeJenkinsClient()
    result = get_schedule_job_last_failure(client, job_display_name="demo")
    assert "ERROR: Table not found: default.ods_xxx" in result["summary"]["error_excerpt"]
    assert any("OutOfMemory" in line for line in result["summary"]["error_excerpt"])
    assert client.log_calls == 1


def test_get_schedule_job_build_log_reads_256kb_and_returns_tail() -> None:
    client = FakeJenkinsClient()
    client.log_text = "prefix-" + ("x" * 1200) + "-tail-error"

    result = get_schedule_job_build_log(
        client,
        job_display_name="demo",
        max_chars=1000,
    )

    log = result["summary"]["log"]
    assert client.last_log_max_bytes == 262144
    assert log["text"].endswith("tail-error")
    assert not log["text"].startswith("prefix-")
    assert log["slice"] == "tail"
    assert log["truncated"] is True


def test_diagnose_schedule_job_failure_contains_history_and_last_failure() -> None:
    result = diagnose_schedule_job_failure(FakeJenkinsClient(), job_display_name="demo")
    assert "history" in result["summary"]
    assert "last_failure" in result["summary"]
    assert result["summary"]["history"]["success_rate"] == "2/3"


def test_find_long_running_schedule_builds_returns_builds_over_24h() -> None:
    client = FakeJenkinsClient()
    client.running_builds = [
        {
            "job_display_name": "stuck_job",
            "build_number": 11,
            "started_at_ms": 1_000,
            "executor": "agent-1#0",
            "build_url": "https://schedule/job/stuck_job/11/",
        },
        {
            "job_display_name": "normal_job",
            "build_number": 12,
            "started_at_ms": 80_000_000,
            "executor": "agent-2#0",
            "build_url": "https://schedule/job/normal_job/12/",
        },
    ]

    result = find_long_running_schedule_builds(
        client,
        min_running_hours=24,
        now_ms=90_000_000,
    )

    assert result["success"] is True
    assert result["summary"]["threshold_hours"] == 24
    assert result["summary"]["total_running_builds"] == 2
    assert result["summary"]["long_running_count"] == 1
    stuck = result["summary"]["builds"][0]
    assert stuck["job_display_name"] == "stuck_job"
    assert stuck["build_number"] == 11
    assert stuck["running_hours"] > 24
    assert "长时间运行未退出" in result["risks"][0]


def test_get_schedule_job_queue_stats_aggregates_by_job_name() -> None:
    client = FakeJenkinsClient()
    client.queue_items = [
        {
            "queue_id": 1,
            "job_display_name": "order_daily_job",
            "why": "Waiting for next available executor",
            "blocked": False,
            "buildable": True,
            "in_queue_since_ms": 1_000,
            "jenkins_url": "http://schedule.corp.bianlifeng.com/job/order_daily_job/",
        },
        {
            "queue_id": 2,
            "job_display_name": "order_daily_job",
            "why": "Waiting for next available executor",
            "blocked": False,
            "buildable": True,
            "in_queue_since_ms": 2_000,
            "jenkins_url": "http://schedule.corp.bianlifeng.com/job/order_daily_job/",
        },
        {
            "queue_id": 3,
            "job_display_name": "dim_store_job",
            "why": "Blocked by upstream",
            "blocked": True,
            "buildable": False,
            "in_queue_since_ms": 3_000,
            "jenkins_url": "http://schedule.corp.bianlifeng.com/job/dim_store_job/",
        },
    ]

    result = get_schedule_job_queue_stats(client)

    assert result["success"] is True
    assert result["summary"]["total_queue_items"] == 3
    assert result["summary"]["distinct_jobs"] == 2
    assert result["summary"]["job_counts"] == [
        {
            "job_display_name": "order_daily_job",
            "count": 2,
            "jenkins_url": "http://schedule.corp.bianlifeng.com/job/order_daily_job/",
        },
        {
            "job_display_name": "dim_store_job",
            "count": 1,
            "jenkins_url": "http://schedule.corp.bianlifeng.com/job/dim_store_job/",
        },
    ]
    assert len(result["summary"]["items"]) == 3
    assert result["summary"]["blocked_items"] == 1
    assert any("blocked" in risk for risk in result["risks"])


def test_trigger_schedule_job_build_requires_confirm() -> None:
    client = FakeJenkinsClient()
    result = trigger_schedule_job_build(client, job_display_name="demo")
    assert result["success"] is False
    assert result["error_type"] == "invalid_input"
    assert client.trigger_calls == []


def test_trigger_schedule_job_build_returns_queue_info() -> None:
    client = FakeJenkinsClient()
    result = trigger_schedule_job_build(
        client,
        job_display_name=" demo ",
        parameters={"time_hour": "2026/07/01/20", "dry_run": False},
        confirm=True,
    )
    assert result["success"] is True
    assert result["job_display_name"] == "demo"
    assert result["summary"]["triggered"] is True
    assert result["summary"]["queue_id"] == 456
    assert client.trigger_calls == [
        ("demo", {"time_hour": "2026/07/01/20", "dry_run": False})
    ]


def test_trigger_schedule_job_single_build_requires_confirm() -> None:
    client = FakeJenkinsClient()
    result = trigger_schedule_job_single_build(client, job_display_name="demo")
    assert result["success"] is False
    assert result["error_type"] == "invalid_input"
    assert client.single_build_calls == []


def test_trigger_schedule_job_single_build_returns_queue_info() -> None:
    client = FakeJenkinsClient()
    result = trigger_schedule_job_single_build(
        client,
        job_display_name=" demo ",
        parameters={"time_hour": "2026/07/01/20"},
        confirm=True,
    )
    assert result["success"] is True
    assert result["job_display_name"] == "demo"
    assert result["summary"]["triggered"] is True
    assert result["summary"]["trigger_mode"] == "single_build"
    assert result["summary"]["queue_id"] == 457
    assert client.single_build_calls == [("demo", {"time_hour": "2026/07/01/20"})]


def test_rebuild_schedule_job_build_requires_confirm() -> None:
    client = FakeJenkinsClient()
    result = rebuild_schedule_job_build(client, job_display_name="demo", build_number=10)
    assert result["success"] is False
    assert result["error_type"] == "invalid_input"
    assert client.rebuild_calls == []


def test_rebuild_schedule_job_build_returns_queue_info() -> None:
    client = FakeJenkinsClient()
    result = rebuild_schedule_job_build(
        client,
        job_display_name=" demo ",
        build_number=10,
        parameters={"time_hour": "2026/07/01/20"},
        confirm=True,
    )
    assert result["success"] is True
    assert result["job_display_name"] == "demo"
    assert result["summary"]["triggered"] is True
    assert result["summary"]["trigger_mode"] == "rebuild"
    assert result["summary"]["build_number"] == 10
    assert result["summary"]["queue_id"] == 458
    assert client.rebuild_calls == [("demo", 10, {"time_hour": "2026/07/01/20"})]


class FakeDataHubClient:
    def __init__(self, *, jobs: list[dict[str, Any]] | None = None, total: int | None = None, error: Exception | None = None) -> None:
        self.jobs = jobs or []
        self.total = total if total is not None else len(self.jobs)
        self.error = error
        self.calls: list[tuple[str, int]] = []

    def search_data_jobs(self, name_substring: str, limit: int) -> dict[str, Any]:
        self.calls.append((name_substring, limit))
        if self.error:
            raise self.error
        return {"total": self.total, "jobs": self.jobs[:limit]}


def test_search_schedule_jobs_success() -> None:
    client = FakeDataHubClient(
        jobs=[{"urn": "urn:li:dataJob:1", "name": "order_daily_job", "job_id": "order_daily_job"}],
        total=1,
    )
    result = search_schedule_jobs(client, keyword="order")
    assert result["success"] is True
    assert result["summary"]["total"] == 1
    assert result["summary"]["returned"] == 1
    job = result["summary"]["jobs"][0]
    assert job["job_display_name"] == "order_daily_job"
    assert job["urn"] == "urn:li:dataJob:1"
    assert job["jenkins_url"].endswith("/job/order_daily_job")
    assert result["risks"] == []
    assert client.calls == [("order", 20)]


def test_search_schedule_jobs_empty_result_has_risk_hint() -> None:
    result = search_schedule_jobs(FakeDataHubClient(), keyword="nope")
    assert result["success"] is True
    assert result["summary"]["jobs"] == []
    assert any("更短的子串" in risk for risk in result["risks"])


def test_search_schedule_jobs_bounds_limit() -> None:
    client = FakeDataHubClient()
    search_schedule_jobs(client, keyword="x", limit=9999)
    assert client.calls == [("x", 5000)]


def test_search_schedule_jobs_empty_keyword_is_invalid_input() -> None:
    result = search_schedule_jobs(FakeDataHubClient(), keyword="  ")
    assert result["success"] is False
    assert result["error_type"] == "invalid_input"


def test_search_schedule_jobs_datahub_error_envelope() -> None:
    client = FakeDataHubClient(error=DataHubClientError("GMS down"))
    result = search_schedule_jobs(client, keyword="order")
    assert result["success"] is False
    assert result["error_type"] == "datahub_error"
    assert "GMS down" in result["message"]
