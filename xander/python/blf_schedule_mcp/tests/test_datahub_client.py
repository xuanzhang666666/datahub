from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

from blf_schedule_mcp.datahub_client import (
    DataHubClient,
    DataHubClientError,
    build_structured_query,
    escape_query_substring,
)


def test_build_structured_query_basic() -> None:
    assert build_structured_query("OrderDaily") == "/q name:*orderdaily* OR jobId:*orderdaily*"


def test_build_structured_query_preserves_chinese() -> None:
    assert build_structured_query("订单日报") == "/q name:*订单日报* OR jobId:*订单日报*"


def test_escape_query_substring_special_chars() -> None:
    assert escape_query_substring("a:b*c") == "a\\:b\\*c"
    assert escape_query_substring("x/y?z") == "x\\/y\\?z"
    assert escape_query_substring("a\\b") == "a\\\\b"


def test_build_structured_query_escapes_user_wildcards() -> None:
    # User-supplied `*` and `:` must be literal; only our wrapping `*` are wildcards.
    query = build_structured_query("foo:bar*")
    assert query == "/q name:*foo\\:bar\\** OR jobId:*foo\\:bar\\**"


def test_search_data_jobs_rejects_empty_keyword() -> None:
    client = DataHubClient(gms_url="http://gms")
    with pytest.raises(ValueError):
        client.search_data_jobs("  ", 10)


def test_search_data_jobs_parses_response_and_sends_token() -> None:
    client = DataHubClient(gms_url="http://gms", token="secret")
    payload: dict[str, Any] = {
        "data": {
            "searchAcrossEntities": {
                "total": 2,
                "searchResults": [
                    {"entity": {"urn": "urn:li:dataJob:1", "jobId": "job_a", "properties": {"name": "Job-A"}}},
                    {"entity": {"urn": "urn:li:dataJob:2", "jobId": "job_b", "properties": None}},
                ],
            }
        }
    }
    with mock.patch.object(DataHubClient, "_graphql", return_value=payload) as graphql:
        result = client.search_data_jobs("Job", 10)
    assert result["total"] == 2
    assert result["jobs"][0] == {"urn": "urn:li:dataJob:1", "name": "Job-A", "job_id": "job_a"}
    # name falls back to jobId when properties are missing
    assert result["jobs"][1]["name"] == "job_b"
    variables = graphql.call_args.args[1]
    assert variables["input"]["query"] == "/q name:*job* OR jobId:*job*"
    assert variables["input"]["types"] == ["DATA_JOB"]
    assert variables["input"]["count"] == 10


def test_graphql_errors_raise_client_error() -> None:
    client = DataHubClient(gms_url="http://gms")
    fake_response = mock.MagicMock()
    fake_response.read.return_value = b'{"errors": [{"message": "boom"}]}'
    fake_response.__enter__.return_value = fake_response
    with mock.patch("urllib.request.urlopen", return_value=fake_response):
        with pytest.raises(DataHubClientError, match="boom"):
            client.search_data_jobs("job", 5)
