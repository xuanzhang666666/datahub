from __future__ import annotations

import io
import urllib.error
from unittest.mock import patch

import pytest

from blf_schedule_mcp.jenkins_client import JenkinsClient, JenkinsClientError
from blf_schedule_mcp.tools import _extract_error_lines


class _FakeResponse:
    def __init__(self, data: bytes) -> None:
        self.data = data

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
