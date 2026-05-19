"""query_upstream_lineage view-table structured property checks."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from job_info_sync_datahub import query_upstream_lineage as q


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_run_allows_view_upstreams_with_empty_etl_properties(monkeypatch) -> None:
    upstream = q.make_hive_dataset_urn("view_db.some_view")

    monkeypatch.setattr(q, "fetch_all_upstream_urns", lambda *args, **kwargs: {upstream})
    monkeypatch.setattr(q, "check_structured_properties", lambda *args, **kwargs: (False, False))
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: _FakeResponse(
            {
                "viewProperties": {
                    "value": {
                        "materialized": False,
                        "viewLogic": "select 1",
                        "viewLanguage": "SQL",
                    }
                }
            }
        ),
    )

    assert q.run(["target_db.target_table"], "http://gms") == 0


def test_run_still_fails_non_view_upstreams_with_empty_etl_properties(monkeypatch) -> None:
    upstream = q.make_hive_dataset_urn("table_db.some_table")

    monkeypatch.setattr(q, "fetch_all_upstream_urns", lambda *args, **kwargs: {upstream})
    monkeypatch.setattr(q, "check_structured_properties", lambda *args, **kwargs: (False, False))

    def _raise_404(*args, **kwargs):
        raise urllib.error.HTTPError(
            url="http://gms/openapi/v3/entity/dataset/urn/viewProperties",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr(urllib.request, "urlopen", _raise_404)

    assert q.run(["target_db.target_table"], "http://gms") == 3
