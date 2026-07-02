from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
from unittest.mock import patch

import pytest

from blf_schedule_mcp.jenkins_client import JenkinsClient, JenkinsClientError
from blf_schedule_mcp.jenkins_client import _normalize_build_parameters
from blf_schedule_mcp.tools import _extract_error_lines


class _FakeResponse:
    def __init__(self, data: bytes, headers: dict[str, str] | None = None) -> None:
        self.data = data
        self.headers = headers or {}

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.data if size < 0 else self.data[:size]


def _http_error(status_code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://jenkins.example/job/foo/api/json",
        code=status_code,
        msg="error",
        hdrs={},
        fp=io.BytesIO(b""),
    )


def test_open_json_returns_empty_dict_for_404() -> None:
    client = JenkinsClient(base_url="https://jenkins.example", username="u", token="t")
    with patch("urllib.request.urlopen", side_effect=_http_error(404)):
        assert client._open_json("job/foo/missing/api/json") == {}


def test_open_json_raises_for_401() -> None:
    client = JenkinsClient(base_url="https://jenkins.example", username="u", token="t")
    with patch("urllib.request.urlopen", side_effect=_http_error(401)):
        with pytest.raises(JenkinsClientError) as exc:
            client._open_json("job/foo/api/json")
    assert exc.value.status_code == 401


def test_open_json_parses_object() -> None:
    client = JenkinsClient(base_url="https://jenkins.example", username="u", token="t")
    with patch("urllib.request.urlopen", return_value=_FakeResponse(b'{"name":"foo"}')):
        assert client._open_json("job/foo/api/json") == {"name": "foo"}


def test_get_build_log_reads_progressive_text_tail() -> None:
    client = JenkinsClient(base_url="https://jenkins.example", username="u", token="t")
    calls: list[str] = []

    def fake_urlopen(request, timeout):  # noqa: ANN001, ANN202
        calls.append(request.full_url)
        if "start=0" in request.full_url:
            return _FakeResponse(b"x", {"X-Text-Size": "2000"})
        if "start=976" in request.full_url:
            return _FakeResponse(b"tail log")
        raise AssertionError(f"unexpected url: {request.full_url}")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        assert client.get_build_log("demo", 4, max_bytes=1024) == "tail log"

    assert calls == [
        "https://jenkins.example/job/demo/4/logText/progressiveText?start=0",
        "https://jenkins.example/job/demo/4/logText/progressiveText?start=976",
    ]


def test_get_running_builds_parses_computer_executors() -> None:
    payload = {
        "computer": [
            {
                "displayName": "agent-1",
                "executors": [
                    {
                        "currentExecutable": {
                            "number": 11,
                            "fullDisplayName": "stuck_job #11",
                            "timestamp": 1_000,
                            "url": "https://jenkins.example/job/stuck_job/11/",
                        }
                    },
                    {"currentExecutable": None},
                ],
                "oneOffExecutors": [
                    {
                        "currentExecutable": {
                            "number": 12,
                            "fullDisplayName": "folder » one_off_job #12",
                            "timestamp": 2_000,
                            "url": "https://jenkins.example/job/folder/job/one_off_job/12/",
                        }
                    }
                ],
            }
        ]
    }
    client = JenkinsClient(base_url="https://jenkins.example", username="u", token="t")

    with patch("urllib.request.urlopen", return_value=_FakeResponse(json.dumps(payload).encode("utf-8"))):
        builds = client.get_running_builds()

    assert builds == [
        {
            "job_display_name": "stuck_job",
            "build_number": 11,
            "started_at_ms": 1_000,
            "build_url": "https://jenkins.example/job/stuck_job/11/",
            "executor": "agent-1#0",
        },
        {
            "job_display_name": "folder » one_off_job",
            "build_number": 12,
            "started_at_ms": 2_000,
            "build_url": "https://jenkins.example/job/folder/job/one_off_job/12/",
            "executor": "agent-1#oneOff0",
        },
    ]


def test_get_queue_items_parses_task_job_names() -> None:
    payload = {
        "items": [
            {
                "id": 101,
                "why": "Waiting for next available executor",
                "blocked": False,
                "buildable": True,
                "inQueueSince": 1_700_000_000_000,
                "task": {
                    "name": "order_daily_job",
                    "url": "https://jenkins.example/job/order_daily_job/",
                },
            },
            {
                "id": 102,
                "why": "Blocked by upstream",
                "blocked": True,
                "buildable": False,
                "inQueueSince": 1_700_000_100_000,
                "task": {
                    "name": "one_off_job",
                    "url": "https://jenkins.example/job/folder/job/one_off_job/",
                },
            },
        ]
    }
    client = JenkinsClient(base_url="https://jenkins.example", username="u", token="t")

    with patch("urllib.request.urlopen", return_value=_FakeResponse(json.dumps(payload).encode("utf-8"))):
        items = client.get_queue_items()

    assert items == [
        {
            "queue_id": 101,
            "job_display_name": "order_daily_job",
            "why": "Waiting for next available executor",
            "blocked": False,
            "buildable": True,
            "in_queue_since_ms": 1_700_000_000_000,
            "jenkins_url": "https://jenkins.example/job/order_daily_job/",
        },
        {
            "queue_id": 102,
            "job_display_name": "folder/one_off_job",
            "why": "Blocked by upstream",
            "blocked": True,
            "buildable": False,
            "in_queue_since_ms": 1_700_000_100_000,
            "jenkins_url": "https://jenkins.example/job/folder/job/one_off_job/",
        },
    ]


def test_trigger_build_posts_build_with_parameters_and_reads_queue_location() -> None:
    client = JenkinsClient(base_url="https://jenkins.example", username="u", token="t")
    requests = []

    def fake_urlopen(request, timeout):  # noqa: ANN001, ANN202
        requests.append(request)
        return _FakeResponse(b"", {"Location": "https://jenkins.example/queue/item/321/"})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = client.trigger_build(
            "demo",
            parameters={"time_hour": "2026/07/01/20", "retry": 1},
        )

    assert requests[0].full_url == "https://jenkins.example/job/demo/buildWithParameters"
    assert requests[0].get_method() == "POST"
    assert requests[0].data == b"time_hour=2026%2F07%2F01%2F20&retry=1"
    assert result["queue_id"] == 321
    assert result["queue_url"] == "https://jenkins.example/queue/item/321/"


def test_trigger_build_posts_plain_build_without_parameters() -> None:
    client = JenkinsClient(base_url="https://jenkins.example", username="u", token="t")
    requests = []

    def fake_urlopen(request, timeout):  # noqa: ANN001, ANN202
        requests.append(request)
        return _FakeResponse(b"", {"Location": "https://jenkins.example/queue/item/322/"})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = client.trigger_build("demo")

    assert requests[0].full_url == "https://jenkins.example/job/demo/build"
    assert requests[0].get_method() == "POST"
    assert requests[0].data == b""
    assert result["queue_id"] == 322


def test_trigger_build_retries_with_crumb_after_403() -> None:
    client = JenkinsClient(base_url="https://jenkins.example", username="u", token="t")
    requests = []

    def fake_urlopen(request, timeout):  # noqa: ANN001, ANN202
        requests.append(request)
        if len(requests) == 1:
            raise _http_error(403)
        if "crumbIssuer/api/json" in request.full_url:
            return _FakeResponse(b'{"crumbRequestField":"Jenkins-Crumb","crumb":"abc"}')
        return _FakeResponse(b"", {"Location": "https://jenkins.example/queue/item/323/"})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = client.trigger_build("demo")

    assert requests[0].full_url == "https://jenkins.example/job/demo/build"
    assert requests[1].full_url == "https://jenkins.example/crumbIssuer/api/json"
    assert requests[2].headers["Jenkins-crumb"] == "abc"
    assert result["queue_id"] == 323


def test_trigger_single_build_posts_build1_with_single_build_flag() -> None:
    client = JenkinsClient(base_url="https://jenkins.example", username="u", token="t")
    requests = []

    def fake_urlopen(request, timeout):  # noqa: ANN001, ANN202
        requests.append(request)
        return _FakeResponse(b"", {"Location": "https://jenkins.example/queue/item/324/"})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = client.trigger_single_build(
            "demo",
            parameters={"time_hour": "2026/07/01/20"},
        )

    assert requests[0].full_url == "https://jenkins.example/job/demo/build1?delay=0sec&singleBuild=true"
    assert requests[0].get_method() == "POST"
    form = urllib.parse.parse_qs(requests[0].data.decode("utf-8"))
    submitted = json.loads(form["json"][0])
    assert submitted == {
        "parameter": [{"name": "time_hour", "value": "2026/07/01/20"}],
        "statusCode": "201",
    }
    assert result["queue_id"] == 324


def test_rebuild_build_posts_parameterized_rebuild_for_build_number() -> None:
    client = JenkinsClient(base_url="https://jenkins.example", username="u", token="t")
    requests = []

    def fake_urlopen(request, timeout):  # noqa: ANN001, ANN202
        requests.append(request)
        return _FakeResponse(b"", {"Location": "https://jenkins.example/queue/item/325/"})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = client.rebuild_build(
            "demo",
            10,
            parameters={"time_hour": "2026/07/01/20"},
        )

    assert requests[0].full_url == "https://jenkins.example/job/demo/10/rebuild/parameterized"
    assert requests[0].get_method() == "POST"
    assert requests[0].data == b"time_hour=2026%2F07%2F01%2F20"
    assert result["queue_id"] == 325


def test_normalize_build_parameters_rejects_nested_values() -> None:
    assert _normalize_build_parameters({"a": 1, "b": False, "c": None}) == {
        "a": "1",
        "b": "False",
        "c": "",
    }
    with pytest.raises(ValueError):
        _normalize_build_parameters({"nested": {"x": 1}})


def test_extract_error_lines_matches_keywords() -> None:
    log_text = """
starting
ERROR: Table not found: default.ods_xxx
ignored
Exception in thread main
finished
"""
    assert _extract_error_lines(log_text, limit=10) == [
        "ERROR: Table not found: default.ods_xxx",
        "Exception in thread main",
    ]
