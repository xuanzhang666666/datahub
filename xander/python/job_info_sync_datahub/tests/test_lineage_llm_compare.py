"""lineage_llm_compare 离线单测（不访问外网）。"""

from __future__ import annotations

from job_info_sync_datahub.lineage_llm_compare import (
    _compare_one_side,
    parse_llm_json_object,
    tables_from_llm_payload,
)


def test_parse_llm_json_object_bare() -> None:
    s = '{"target_tables":[{"db":"d","table":"t"}],"upstream_tables":[],"notes":""}'
    d = parse_llm_json_object(s)
    assert d["target_tables"][0]["table"] == "t"


def test_parse_llm_json_object_fence() -> None:
    s = '```json\n{"target_tables":[],"upstream_tables":[{"db":"a","table":"b"}],"notes":"x"}\n```'
    d = parse_llm_json_object(s)
    assert d["upstream_tables"][0]["table"] == "b"


def test_tables_from_llm_payload() -> None:
    payload = {
        "target_tables": [{"db": "Default", "table": "T1"}],
        "upstream_tables": [{"db": "x", "table": "y"}],
        "notes": "",
    }
    t, u = tables_from_llm_payload(payload)
    assert t == {"default.t1"}
    assert u == {"x.y"}


def test_compare_one_side() -> None:
    st, su = {"d.t"}, {"d.a", "d.b"}
    r = _compare_one_side("x", st, su, {"d.t"}, {"d.a"})
    assert r["targets_equal"] is True
    assert r["upstream_only_sqlglot"] == ["d.b"]
