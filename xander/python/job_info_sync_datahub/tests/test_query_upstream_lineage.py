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


def test_run_groups_missing_properties_and_returns_success(monkeypatch, capsys) -> None:
    upstream_etl_missing = q.make_hive_dataset_urn("table_db.etl_missing")
    upstream_shell_missing = q.make_hive_dataset_urn("table_db.shell_missing")
    upstream_both_missing = q.make_hive_dataset_urn("table_db.both_missing")

    monkeypatch.setattr(
        q,
        "fetch_all_upstream_urns",
        lambda *args, **kwargs: {
            upstream_etl_missing,
            upstream_shell_missing,
            upstream_both_missing,
        },
    )

    def _check_structured_properties(*args, **kwargs):
        urn = args[2]
        table_name = q.urn_to_table_name(urn)
        if table_name == "table_db.etl_missing":
            return False, True
        if table_name == "table_db.shell_missing":
            return True, False
        return False, False

    monkeypatch.setattr(q, "check_structured_properties", _check_structured_properties)

    def _raise_404(*args, **kwargs):
        raise urllib.error.HTTPError(
            url="http://gms/openapi/v3/entity/dataset/urn/viewProperties",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr(urllib.request, "urlopen", _raise_404)

    assert q.run(["target_db.target_table"], "http://gms") == 0

    output = capsys.readouterr().out
    assert "缺少: Etl Script 的如下：" in output
    assert "  table_db.etl_missing" in output
    assert "  table_db.both_missing" in output
    assert "缺少: Execute Shell 的如下：" in output
    assert "  table_db.shell_missing" in output
    assert "  table_db.both_missing" in output


def test_run_prints_data_availability_flag_for_sorted_upstream_tables(monkeypatch, capsys) -> None:
    upstream_a = q.make_hive_dataset_urn("table_db.a_upstream")
    upstream_b = q.make_hive_dataset_urn("table_db.b_upstream")

    monkeypatch.setattr(q, "fetch_all_upstream_urns", lambda *args, **kwargs: {upstream_b, upstream_a})

    def _fetch_structured_properties(*args, **kwargs):
        urn = args[1]
        table_name = q.urn_to_table_name(urn)
        flag = "表DDL, 表血缘" if table_name == "table_db.a_upstream" else ""
        return {
            "structuredProperties": {
                "value": {
                    "properties": [
                        {
                            "propertyUrn": q.URN_DATA_AVAILABILITY_FLAG,
                            "values": [{"string": flag}],
                        }
                    ]
                }
            }
        }

    monkeypatch.setattr(q, "fetch_structured_properties", _fetch_structured_properties)

    assert q.run(["target_db.target_table"], "http://gms", skip_check_props=True) == 0

    output = capsys.readouterr().out
    first = output.index("  table_db.a_upstream\tdata_availability_flag=表DDL, 表血缘")
    second = output.index("  table_db.b_upstream\tdata_availability_flag=-")
    assert first < second
