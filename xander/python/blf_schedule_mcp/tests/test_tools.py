from __future__ import annotations

from typing import Any

from blf_schedule_mcp.tools import (
    diagnose_schedule_job_failure,
    get_schedule_job_build_history,
    get_schedule_job_build_status,
    get_schedule_job_last_failure,
)


class FakeJenkinsClient:
    def __init__(self, result: str = "SUCCESS") -> None:
        self.result = result
        self.log_calls = 0

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
        return "starting\nERROR: Table not found: default.ods_xxx\njava.lang.OutOfMemoryError: GC overhead\nfinished"


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


def test_diagnose_schedule_job_failure_contains_history_and_last_failure() -> None:
    result = diagnose_schedule_job_failure(FakeJenkinsClient(), job_display_name="demo")
    assert "history" in result["summary"]
    assert "last_failure" in result["summary"]
    assert result["summary"]["history"]["success_rate"] == "2/3"
