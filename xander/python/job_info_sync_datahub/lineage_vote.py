"""lineage_vote — sqlglot 与双 LLM 在「目标表 / 上游表」上的投票与分歧落盘。

规则摘要：
- 若 MiniMax 与 DeepSeek 的表集合一致：采用该 LLM 共识结果（sqlglot 仅作辅助对比写入报告）。
- 否则按「元素得票 ≥2/3」多数表决；若仍无元素达 2 票，则回退为 LLM 并集去掉占位表，再不行则用 sqlglot（去掉 __var__）。
- 若三者在集合层面「两两都不完全相同」：记为需人工复核，并追加写入 discrepancy 文件（JSONL）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .models import TableLineage, TableRef


def _is_var_placeholder(full_name: str) -> bool:
    """sqlglot 变量占位产生的伪表名。"""
    parts = full_name.lower().rsplit(".", 1)
    return len(parts) == 2 and parts[1] == "__var__"


def _norm_set(names: Set[str]) -> Set[str]:
    return {x.strip().lower() for x in names if x.strip()}


def _no_pair_agrees(s: Set[str], m: Set[str], d: Set[str]) -> bool:
    """三者集合两两比较，没有任何一对相等。"""
    s, m, d = _norm_set(s), _norm_set(m), _norm_set(d)
    return s != m and s != d and m != d


def _vote_union_three(s: Set[str], m: Set[str], d: Set[str]) -> Set[str]:
    """对并集中每个表名计数，得票 ≥2 则入选。"""
    s, m, d = _norm_set(s), _norm_set(m), _norm_set(d)
    universe = s | m | d
    chosen: Set[str] = set()
    for t in universe:
        votes = (t in s) + (t in m) + (t in d)
        if votes >= 2:
            chosen.add(t)
    return chosen


def _strip_noise(s: Set[str]) -> Set[str]:
    return {x for x in _norm_set(s) if not _is_var_placeholder(x)}


@dataclass
class LineageVoteOutcome:
    """一次投票的完整结果（便于落盘与写回 ctx）。"""

    selected_targets: Set[str]
    selected_upstreams: Set[str]
    target_strategy: str
    upstream_strategy: str
    sqlglot_targets: Set[str]
    sqlglot_upstream: Set[str]
    minimax_targets: Set[str]
    minimax_upstream: Set[str]
    deepseek_targets: Set[str]
    deepseek_upstream: Set[str]
    sqlglot_auxiliary_note: str = ""
    triple_split_targets: bool = False
    triple_split_upstream: bool = False
    needs_manual_review: bool = False
    manual_review_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "selected_targets": sorted(self.selected_targets),
            "selected_upstreams": sorted(self.selected_upstreams),
            "target_strategy": self.target_strategy,
            "upstream_strategy": self.upstream_strategy,
            "triple_split_targets": self.triple_split_targets,
            "triple_split_upstream": self.triple_split_upstream,
            "sqlglot_targets": sorted(self.sqlglot_targets),
            "sqlglot_upstream": sorted(self.sqlglot_upstream),
            "minimax_targets": sorted(self.minimax_targets),
            "minimax_upstream": sorted(self.minimax_upstream),
            "deepseek_targets": sorted(self.deepseek_targets),
            "deepseek_upstream": sorted(self.deepseek_upstream),
            "sqlglot_auxiliary_note": self.sqlglot_auxiliary_note,
            "needs_manual_review": self.needs_manual_review,
            "manual_review_reasons": self.manual_review_reasons,
        }


def _choose_dimension(
    label: str,
    s: Set[str],
    m: Set[str],
    d: Set[str],
    *,
    prefer_llm_union_fallback: bool,
) -> Tuple[Set[str], str, bool, str]:
    """返回 (chosen, strategy, triple_split, triple_message_or_empty).

    triple_split 为 True 表示 sqlglot / minimax / deepseek 三者集合两两均不完全相同。
    """
    s0, m0, d0 = _norm_set(s), _norm_set(m), _norm_set(d)
    triple = _no_pair_agrees(s0, m0, d0)
    triple_msg = (
        f"{label}: sqlglot / minimax / deepseek 三者集合两两均不完全相同"
        if triple
        else ""
    )

    # 1) 双 LLM 一致 → 相信 LLM
    if m0 == d0:
        chosen = set(m0)
        strategy = "llm_unanimous"
        return chosen, strategy, triple, triple_msg

    # 2) LLM 之一与 sqlglot 一致
    if m0 == s0:
        return set(m0), "minimax_sqlglot_pair", triple, triple_msg
    if d0 == s0:
        return set(d0), "deepseek_sqlglot_pair", triple, triple_msg

    # 3) 元素级 2/3 票
    voted = _vote_union_three(s0, m0, d0)
    if voted:
        return voted, "majority_element_vote", triple, triple_msg

    # 4) 回退：LLM 并集去噪，或 sqlglot 去噪
    if prefer_llm_union_fallback:
        fallback = _strip_noise(m0 | d0)
        if fallback:
            return fallback, "fallback_llm_union_stripped", triple, triple_msg
    fb2 = _strip_noise(s0)
    if fb2:
        return fb2, "fallback_sqlglot_stripped", triple, triple_msg

    return set(), "empty", triple, triple_msg or f"{label}: 无法得到非空集合"


def resolve_lineage_vote(
    sqlglot_targets: Set[str],
    sqlglot_upstream: Set[str],
    minimax_payload: Optional[Dict[str, Any]],
    deepseek_payload: Optional[Dict[str, Any]],
) -> LineageVoteOutcome:
    """根据 sqlglot 集合与两份 LLM JSON 产出投票结果。"""
    from .lineage_write_policy import tables_from_llm_payload

    if minimax_payload is None:
        minimax_payload = {"target_tables": [], "upstream_tables": [], "notes": "minimax unavailable"}
    if deepseek_payload is None:
        deepseek_payload = {"target_tables": [], "upstream_tables": [], "notes": "deepseek unavailable"}

    mt, mu = tables_from_llm_payload(minimax_payload)
    dt, du = tables_from_llm_payload(deepseek_payload)
    st, su = _norm_set(sqlglot_targets), _norm_set(sqlglot_upstream)

    t_chosen, t_strat, t_triple, t_msg = _choose_dimension(
        "targets", st, mt, dt, prefer_llm_union_fallback=True
    )
    u_chosen, u_strat, u_triple, u_msg = _choose_dimension(
        "upstreams", su, mu, du, prefer_llm_union_fallback=True
    )

    manual: List[str] = []
    if t_triple and t_msg:
        manual.append(t_msg)
    if u_triple and u_msg:
        manual.append(u_msg)

    aux = ""
    if mt == dt and (st != mt or su != mu or su != du):
        aux = (
            f"LLM 目标={sorted(mt)} 上游={sorted(mu)}；"
            f"sqlglot 目标={sorted(st)} 上游={sorted(su)}（作辅助对照）"
        )

    return LineageVoteOutcome(
        selected_targets=t_chosen,
        selected_upstreams=u_chosen,
        target_strategy=t_strat,
        upstream_strategy=u_strat,
        sqlglot_targets=st,
        sqlglot_upstream=su,
        minimax_targets=mt,
        minimax_upstream=mu,
        deepseek_targets=dt,
        deepseek_upstream=du,
        sqlglot_auxiliary_note=aux,
        triple_split_targets=t_triple,
        triple_split_upstream=u_triple,
        needs_manual_review=t_triple or u_triple,
        manual_review_reasons=manual,
    )


def full_names_to_refs(names: Set[str]) -> List[TableRef]:
    out: List[TableRef] = []
    for fn in sorted(names):
        parts = fn.split(".", 1)
        if len(parts) != 2:
            continue
        db, tb = parts[0].strip() or "default", parts[1].strip()
        if tb:
            out.append(TableRef(db=db, table=tb))
    return out


def apply_vote_to_table_lineages(
    table_lineages: List[TableLineage],
    vote: LineageVoteOutcome,
) -> List[TableLineage]:
    """用投票后的表集合重建 TableLineage（保留原第一个目标 TableRef 的 db.table 若仍在选中集中）。"""
    if not table_lineages:
        return table_lineages

    primary = table_lineages[0]
    st_sel = vote.selected_targets
    up_sel = vote.selected_upstreams

    if st_sel:
        refs = full_names_to_refs(st_sel)
        if refs:
            want = primary.target.full_name.lower()
            target_ref = next((r for r in refs if r.full_name.lower() == want), refs[0])
        else:
            target_ref = primary.target
    else:
        target_ref = primary.target

    new_tl = TableLineage(
        target=target_ref,
        upstreams=full_names_to_refs(up_sel),
        source_block_indices=list(primary.source_block_indices),
    )
    return [new_tl]


_UNCERTAIN_STRATEGIES = frozenset(
    {
        "empty",
        "fallback_llm_union_stripped",
        "fallback_sqlglot_stripped",
    }
)


def should_skip_lineage_write_to_datahub(vote: LineageVoteOutcome) -> bool:
    """表级血缘（含字段级）是否在不确定：若为 True 则不应写入 DataHub。"""
    if vote.triple_split_targets or vote.triple_split_upstream:
        return True
    if vote.target_strategy in _UNCERTAIN_STRATEGIES:
        return True
    if vote.upstream_strategy in _UNCERTAIN_STRATEGIES:
        return True
    return False


def skip_lineage_write_reasons(vote: LineageVoteOutcome) -> List[str]:
    """人类可读的不写入原因列表。"""
    out: List[str] = []
    if vote.triple_split_targets:
        out.append("目标表：sqlglot / minimax / deepseek 三者集合两两均不一致")
    if vote.triple_split_upstream:
        out.append("上游表：sqlglot / minimax / deepseek 三者集合两两均不一致")
    if vote.target_strategy in _UNCERTAIN_STRATEGIES:
        out.append(f"目标表策略为不确定类型: {vote.target_strategy}")
    if vote.upstream_strategy in _UNCERTAIN_STRATEGIES:
        out.append(f"上游表策略为不确定类型: {vote.upstream_strategy}")
    return out


def append_lineage_deferred_jsonl(
    path: Path,
    job_display_name: str,
    vote: LineageVoteOutcome,
    llm_compare_dict: Optional[Dict[str, Any]] = None,
) -> None:
    """不确定、未写入 DataHub 表血缘时，追加一条 JSONL 供用户事后处理。"""
    if not should_skip_lineage_write_to_datahub(vote):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    rec: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "job": job_display_name,
        "skip_datahub_upstream_lineage": True,
        "skip_reasons": skip_lineage_write_reasons(vote),
        "triple_split_targets": vote.triple_split_targets,
        "triple_split_upstream": vote.triple_split_upstream,
        "vote": vote.to_dict(),
    }
    if llm_compare_dict is not None:
        rec["llm_compare"] = {
            "minimax_raw": llm_compare_dict.get("minimax_raw"),
            "deepseek_raw": llm_compare_dict.get("deepseek_raw"),
            "verdict": llm_compare_dict.get("verdict"),
        }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def append_discrepancy_record(
    path: Path,
    job_display_name: str,
    vote: LineageVoteOutcome,
    llm_compare_dict: Optional[Dict[str, Any]] = None,
) -> None:
    """兼容旧名：等价于 append_lineage_deferred_jsonl。"""
    append_lineage_deferred_jsonl(path, job_display_name, vote, llm_compare_dict)


def default_discrepancy_log_path(output_dir: Optional[str]) -> Path:
    """默认分歧日志路径。"""
    custom = os.environ.get("BLF_LINEAGE_DISCREPANCY_LOG", "").strip()
    if custom:
        return Path(custom)
    if output_dir:
        return Path(output_dir) / "lineage_discrepancies.jsonl"
    return Path.cwd() / "lineage_discrepancies.jsonl"
