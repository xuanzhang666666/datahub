from __future__ import annotations

import json
from typing import Any

from blf_schedule_mcp.server import TOOL_SPECS, BlfScheduleMcpApplication


class FakeJenkinsClient:
    def __init__(self) -> None:
        self.running_build_calls = 0
        self.queue_calls = 0
        self.trigger_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.single_build_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.rebuild_calls: list[tuple[str, int, dict[str, Any] | None]] = []

    def get_build_info(self, job_name: str, build_ref: int | str) -> dict[str, Any]:
        return {
            "number": 7,
            "result": "SUCCESS",
            "timestamp": 1781233200000,
            "duration": 1000,
            "url": "",
            "building": False,
        }

    def get_running_builds(self) -> list[dict[str, Any]]:
        self.running_build_calls += 1
        return []

    def get_queue_items(self) -> list[dict[str, Any]]:
        self.queue_calls += 1
        return []

    def trigger_build(
        self,
        job_name: str,
        *,
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.trigger_calls.append((job_name, parameters))
        return {
            "endpoint": "/job/{name}/buildWithParameters",
            "queue_url": "https://jenkins.example/queue/item/123/",
            "queue_id": 123,
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
            "queue_url": "https://jenkins.example/queue/item/124/",
            "queue_id": 124,
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
            "queue_url": "https://jenkins.example/queue/item/125/",
            "queue_id": 125,
            "response_text": "",
        }


class FakeDataHubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def search_data_jobs(self, name_substring: str, limit: int) -> dict[str, Any]:
        self.calls.append((name_substring, limit))
        return {
            "total": 1,
            "jobs": [{"urn": "urn:li:dataJob:1", "name": "order_daily_job", "job_id": "order_daily_job"}],
        }


def _build_app() -> tuple[BlfScheduleMcpApplication, FakeDataHubClient, FakeJenkinsClient]:
    datahub_client = FakeDataHubClient()
    jenkins_client = FakeJenkinsClient()
    app = BlfScheduleMcpApplication(
        jenkins_client=jenkins_client,  # type: ignore[arg-type]
        datahub_client=datahub_client,  # type: ignore[arg-type]
        mcp_token=None,
    )
    return app, datahub_client, jenkins_client


def test_tools_list_includes_search_tool() -> None:
    app, _, _ = _build_app()
    response = app.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response is not None
    names = {tool["name"] for tool in response["result"]["tools"]}
    assert "blf_search_schedule_job" in names
    assert "blf_find_long_running_schedule_builds" in names
    assert "blf_get_schedule_job_queue_stats" in names
    assert "blf_trigger_schedule_job_build" in names
    assert "blf_trigger_schedule_job_single_build" in names
    assert "blf_rebuild_schedule_job_build" in names
    assert set(TOOL_SPECS) == names


def test_tools_call_dispatches_search_to_datahub_client() -> None:
    app, datahub_client, _ = _build_app()
    response = app.handle_rpc(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "blf_search_schedule_job", "arguments": {"keyword": "order"}},
        }
    )
    assert response is not None
    assert datahub_client.calls == [("order", 20)]
    text = response["result"]["content"][0]["text"]
    payload = json.loads(text.split("```json\n", 1)[1].rsplit("\n```", 1)[0])
    assert payload["success"] is True
    assert payload["summary"]["jobs"][0]["job_display_name"] == "order_daily_job"


def test_tools_call_dispatches_jenkins_tool_without_datahub() -> None:
    app, datahub_client, _ = _build_app()
    response = app.handle_rpc(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "blf_get_schedule_job_build_status",
                "arguments": {"job_display_name": "demo"},
            },
        }
    )
    assert response is not None
    assert response["result"]["isError"] is False
    assert datahub_client.calls == []


def test_tools_call_dispatches_long_running_build_scan_to_jenkins() -> None:
    app, datahub_client, jenkins_client = _build_app()
    response = app.handle_rpc(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "blf_find_long_running_schedule_builds",
                "arguments": {"min_running_hours": 24},
            },
        }
    )
    assert response is not None
    assert response["result"]["isError"] is False
    assert jenkins_client.running_build_calls == 1
    assert datahub_client.calls == []


def test_tools_call_dispatches_queue_stats_to_jenkins() -> None:
    app, datahub_client, jenkins_client = _build_app()
    response = app.handle_rpc(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "blf_get_schedule_job_queue_stats", "arguments": {}},
        }
    )
    assert response is not None
    assert response["result"]["isError"] is False
    assert jenkins_client.queue_calls == 1
    assert datahub_client.calls == []


def test_tools_call_requires_confirm_before_triggering_build() -> None:
    app, _, jenkins_client = _build_app()
    response = app.handle_rpc(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "blf_trigger_schedule_job_build",
                "arguments": {"job_display_name": "demo", "confirm": False},
            },
        }
    )
    assert response is not None
    assert response["result"]["isError"] is True
    assert jenkins_client.trigger_calls == []


def test_tools_call_dispatches_trigger_build_to_jenkins() -> None:
    app, datahub_client, jenkins_client = _build_app()
    response = app.handle_rpc(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "blf_trigger_schedule_job_build",
                "arguments": {
                    "job_display_name": "demo",
                    "parameters": {"time_hour": "2026/07/01/20"},
                    "confirm": True,
                },
            },
        }
    )
    assert response is not None
    assert response["result"]["isError"] is False
    assert jenkins_client.trigger_calls == [("demo", {"time_hour": "2026/07/01/20"})]
    assert datahub_client.calls == []


def test_tools_call_dispatches_single_build_to_jenkins() -> None:
    app, datahub_client, jenkins_client = _build_app()
    response = app.handle_rpc(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "blf_trigger_schedule_job_single_build",
                "arguments": {
                    "job_display_name": "demo",
                    "parameters": {"time_hour": "2026/07/01/20"},
                    "confirm": True,
                },
            },
        }
    )
    assert response is not None
    assert response["result"]["isError"] is False
    assert jenkins_client.single_build_calls == [("demo", {"time_hour": "2026/07/01/20"})]
    assert datahub_client.calls == []


def test_tools_call_dispatches_rebuild_to_jenkins() -> None:
    app, datahub_client, jenkins_client = _build_app()
    response = app.handle_rpc(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "blf_rebuild_schedule_job_build",
                "arguments": {
                    "job_display_name": "demo",
                    "build_number": 10,
                    "parameters": {"time_hour": "2026/07/01/20"},
                    "confirm": True,
                },
            },
        }
    )
    assert response is not None
    assert response["result"]["isError"] is False
    assert jenkins_client.rebuild_calls == [("demo", 10, {"time_hour": "2026/07/01/20"})]
    assert datahub_client.calls == []
