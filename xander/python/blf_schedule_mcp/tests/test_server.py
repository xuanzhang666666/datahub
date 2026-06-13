from __future__ import annotations

import json
from typing import Any

from blf_schedule_mcp.server import TOOL_SPECS, BlfScheduleMcpApplication


class FakeJenkinsClient:
    def get_build_info(self, job_name: str, build_ref: int | str) -> dict[str, Any]:
        return {
            "number": 7,
            "result": "SUCCESS",
            "timestamp": 1781233200000,
            "duration": 1000,
            "url": "",
            "building": False,
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


def _build_app() -> tuple[BlfScheduleMcpApplication, FakeDataHubClient]:
    datahub_client = FakeDataHubClient()
    app = BlfScheduleMcpApplication(
        jenkins_client=FakeJenkinsClient(),  # type: ignore[arg-type]
        datahub_client=datahub_client,  # type: ignore[arg-type]
        mcp_token=None,
    )
    return app, datahub_client


def test_tools_list_includes_search_tool() -> None:
    app, _ = _build_app()
    response = app.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response is not None
    names = {tool["name"] for tool in response["result"]["tools"]}
    assert "blf_search_schedule_job" in names
    assert set(TOOL_SPECS) == names


def test_tools_call_dispatches_search_to_datahub_client() -> None:
    app, datahub_client = _build_app()
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
    app, datahub_client = _build_app()
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
