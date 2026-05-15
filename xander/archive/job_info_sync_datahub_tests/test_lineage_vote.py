"""lineage_vote 单元测试（无网络）。"""

from __future__ import annotations

from pathlib import Path

from job_info_sync_datahub.lineage_vote import (
    append_lineage_deferred_jsonl,
    append_discrepancy_record,
    resolve_lineage_vote,
    should_skip_lineage_write_to_datahub,
)
from job_info_sync_datahub.lineage_vote import LineageVoteOutcome


def test_llm_unanimous_overrides_sqlglot_upstream() -> None:
    sqlglot_t, sqlglot_u = {"default.t"}, {"default.__var__"}
    mm = {
        "target_tables": [{"db": "default", "table": "t"}],
        "upstream_tables": [{"db": "default", "table": "ods_t"}],
    }
    ds = {
        "target_tables": [{"db": "default", "table": "t"}],
        "upstream_tables": [{"db": "default", "table": "ods_t"}],
    }
    v = resolve_lineage_vote(sqlglot_t, sqlglot_u, mm, ds)
    assert v.target_strategy == "llm_unanimous"
    assert v.upstream_strategy == "llm_unanimous"
    assert v.selected_targets == {"default.t"}
    assert v.selected_upstreams == {"default.ods_t"}
    assert not should_skip_lineage_write_to_datahub(v)


def test_majority_element_vote() -> None:
    st, su = {"default.a"}, {"default.x"}
    mm = {
        "target_tables": [{"db": "default", "table": "a"}],
        "upstream_tables": [
            {"db": "default", "table": "u1"},
            {"db": "default", "table": "u2"},
        ],
    }
    ds = {
        "target_tables": [{"db": "default", "table": "a"}],
        "upstream_tables": [
            {"db": "default", "table": "u1"},
            {"db": "default", "table": "u3"},
        ],
    }
    v = resolve_lineage_vote(st, su, mm, ds)
    assert v.selected_targets == {"default.a"}
    assert "default.u1" in v.selected_upstreams
    assert v.upstream_strategy == "majority_element_vote"


def test_triple_split_skips_datahub() -> None:
    mm = {"target_tables": [{"db": "default", "table": "t2"}], "upstream_tables": []}
    ds = {"target_tables": [{"db": "default", "table": "t3"}], "upstream_tables": []}
    v = resolve_lineage_vote({"default.t1"}, set(), mm, ds)
    assert v.triple_split_targets
    assert should_skip_lineage_write_to_datahub(v)


def test_fallback_strategy_skips_datahub() -> None:
    v = LineageVoteOutcome(
        selected_targets={"default.t"},
        selected_upstreams=set(),
        target_strategy="fallback_sqlglot_stripped",
        upstream_strategy="llm_unanimous",
        sqlglot_targets={"default.t"},
        sqlglot_upstream=set(),
        minimax_targets=set(),
        minimax_upstream=set(),
        deepseek_targets=set(),
        deepseek_upstream=set(),
    )
    assert should_skip_lineage_write_to_datahub(v)


def test_append_deferred_jsonl_when_skip(tmp_path: Path) -> None:
    p = tmp_path / "d.jsonl"
    v_ok = LineageVoteOutcome(
        selected_targets={"default.t"},
        selected_upstreams=set(),
        target_strategy="llm_unanimous",
        upstream_strategy="llm_unanimous",
        sqlglot_targets={"default.t"},
        sqlglot_upstream=set(),
        minimax_targets={"default.t"},
        minimax_upstream=set(),
        deepseek_targets={"default.t"},
        deepseek_upstream=set(),
    )
    append_lineage_deferred_jsonl(p, "job1", v_ok, None)
    assert not p.exists()

    v_skip = LineageVoteOutcome(
        selected_targets={"default.t"},
        selected_upstreams=set(),
        target_strategy="llm_unanimous",
        upstream_strategy="llm_unanimous",
        sqlglot_targets={"default.t"},
        sqlglot_upstream=set(),
        minimax_targets={"default.t"},
        minimax_upstream=set(),
        deepseek_targets={"default.t"},
        deepseek_upstream=set(),
        triple_split_upstream=True,
        needs_manual_review=True,
    )
    append_lineage_deferred_jsonl(p, "job2", v_skip, None)
    assert "skip_datahub_upstream_lineage" in p.read_text(encoding="utf-8")

    # 兼容别名
    append_discrepancy_record(p, "job3", v_skip, None)
    assert p.read_text(encoding="utf-8").count('"job"') >= 2
