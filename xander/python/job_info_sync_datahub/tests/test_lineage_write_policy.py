"""lineage_write_policy 单元测试（无网络）。"""

from __future__ import annotations

import pytest

from job_info_sync_datahub.lineage_write_policy import (
    LineageWriteDecision,
    apply_decision_to_table_lineages,
    compute_trust_score,
    decide_lineage_write_policy,
)
from job_info_sync_datahub.models import TableRef


# --------------------------------------------------------------------------
# compute_trust_score
# --------------------------------------------------------------------------

def test_trust_score_identical() -> None:
    assert compute_trust_score({"a.t"}, {"a.u"}, {"a.t"}, {"a.u"}) == 100


def test_trust_score_completely_different() -> None:
    assert compute_trust_score({"a.t1"}, {"a.u1"}, {"a.t2"}, {"a.u2"}) == 0


def test_trust_score_both_empty() -> None:
    assert compute_trust_score(set(), set(), set(), set()) == 100


def test_trust_score_targets_match_upstream_empty_both() -> None:
    # targets 一致(Jaccard=1.0)，upstreams 均空(Jaccard=1.0) → 100
    assert compute_trust_score({"a.t"}, set(), {"a.t"}, set()) == 100


def test_trust_score_targets_match_upstream_mismatch() -> None:
    # targets 完全一致 Jaccard=1.0，upstreams 不重叠 Jaccard=0 → 50
    score = compute_trust_score({"a.t"}, {"a.u1"}, {"a.t"}, {"a.u2"})
    assert score == 50


def test_trust_score_partial_overlap() -> None:
    # targets: {a,b} vs {b,c} → |{b}|/|{a,b,c}| = 1/3 ≈ 0.333
    # upstreams both empty → 1.0
    # (0.333*50 + 1.0*50) = 66.65 → round → 67
    score = compute_trust_score({"a", "b"}, set(), {"b", "c"}, set())
    assert score == 67


# --------------------------------------------------------------------------
# decide_lineage_write_policy — 核心规则
# --------------------------------------------------------------------------

def test_normal_agree() -> None:
    """sqlglot 与 DeepSeek 完全一致 → NORMAL_AGREE，trust=100。"""
    ds = {
        "target_tables": [{"db": "default", "table": "t"}],
        "upstream_tables": [{"db": "default", "table": "s"}],
    }
    d = decide_lineage_write_policy({"default.t"}, {"default.s"}, ds)
    assert d.status == "NORMAL_AGREE"
    assert d.write_upstream_lineage
    assert d.trust_score == 100
    assert d.selected_targets == {"default.t"}
    assert d.selected_upstreams == {"default.s"}


def test_abnormal_single_sqlglot() -> None:
    """DeepSeek 无结果 → ABNORMAL_SINGLE_SQLGLOT，写 sqlglot。"""
    d = decide_lineage_write_policy({"default.t"}, {"default.u"}, None)
    assert d.status == "ABNORMAL_SINGLE_SQLGLOT"
    assert d.single_source_name == "sqlglot"
    assert d.write_upstream_lineage
    assert d.selected_targets == {"default.t"}
    assert d.trust_score == 0


def test_abnormal_single_deepseek() -> None:
    """sqlglot 无结果，DeepSeek 有结果 → ABNORMAL_SINGLE_DEEPSEEK，写 DeepSeek。
    targets: {} vs {"default.t"} → Jaccard=0; upstreams: {} vs {} → Jaccard=1.0 → trust=50
    """
    ds = {
        "target_tables": [{"db": "default", "table": "t"}],
        "upstream_tables": [],
    }
    d = decide_lineage_write_policy(set(), set(), ds)
    assert d.status == "ABNORMAL_SINGLE_DEEPSEEK"
    assert d.single_source_name == "deepseek"
    assert d.write_upstream_lineage
    assert d.selected_targets == {"default.t"}
    assert d.trust_score == 50  # targets diverge(0), upstreams both empty(1.0) → avg 50


def test_skip_no_tables() -> None:
    """均无结果 → SKIP_NO_TABLES，不写。"""
    d = decide_lineage_write_policy(set(), set(), None)
    assert d.status == "SKIP_NO_TABLES"
    assert not d.write_upstream_lineage
    assert d.trust_score == 100  # 均空视为一致


def test_skip_no_tables_empty_payload() -> None:
    """DeepSeek 返回空列表 → SKIP_NO_TABLES。"""
    ds = {"target_tables": [], "upstream_tables": []}
    d = decide_lineage_write_policy(set(), set(), ds)
    assert d.status == "SKIP_NO_TABLES"
    assert not d.write_upstream_lineage


def test_disagree_deepseek_wins() -> None:
    """两者均有目标表但不一致 → DISAGREE_DEEPSEEK_WINS，以 DeepSeek 为准。
    targets: {"t_sg"} vs {"t_ds"} → Jaccard=0; upstreams: {} vs {} → 1.0 → trust=50
    """
    ds = {
        "target_tables": [{"db": "default", "table": "t_ds"}],
        "upstream_tables": [],
    }
    d = decide_lineage_write_policy({"default.t_sg"}, set(), ds)
    assert d.status == "DISAGREE_DEEPSEEK_WINS"
    assert d.write_upstream_lineage
    assert d.selected_targets == {"default.t_ds"}
    assert d.trust_score == 50  # targets diverge(0), upstreams both empty(1.0) → avg 50


def test_disagree_partial_overlap_trust() -> None:
    """目标表有交集，上游不同 → DISAGREE_DEEPSEEK_WINS，信任度介于 0-100。"""
    ds = {
        "target_tables": [{"db": "a", "table": "t1"}],
        "upstream_tables": [{"db": "a", "table": "u2"}],
    }
    d = decide_lineage_write_policy({"a.t1"}, {"a.u1"}, ds)
    assert d.status == "DISAGREE_DEEPSEEK_WINS"
    # targets Jaccard=1.0, upstreams Jaccard=0 → trust=50
    assert d.trust_score == 50


# --------------------------------------------------------------------------
# apply_decision_to_table_lineages
# --------------------------------------------------------------------------

def test_apply_from_empty_lineages() -> None:
    dec = LineageWriteDecision(
        write_upstream_lineage=True,
        status="NORMAL_AGREE",
        reason="test",
        selected_targets={"default.t"},
        selected_upstreams={"default.s"},
    )
    out = apply_decision_to_table_lineages([], dec)
    assert len(out) == 1
    assert out[0].target == TableRef(db="default", table="t")
    assert len(out[0].upstreams) == 1


def test_disagree_trust_zero_when_both_diverge() -> None:
    """targets 和 upstreams 全不一致 → trust=0。"""
    ds = {
        "target_tables": [{"db": "a", "table": "t2"}],
        "upstream_tables": [{"db": "a", "table": "u2"}],
    }
    d = decide_lineage_write_policy({"a.t1"}, {"a.u1"}, ds)
    assert d.status == "DISAGREE_DEEPSEEK_WINS"
    assert d.trust_score == 0


def test_apply_no_write_returns_original() -> None:
    dec = LineageWriteDecision(
        write_upstream_lineage=False,
        status="SKIP_NO_TABLES",
        reason="",
    )
    out = apply_decision_to_table_lineages([], dec)
    assert out == []
