"""lineage_write_policy — sqlglot + DeepSeek 表级血缘写入策略、信任度评分与审计字段。

规则（表级，不含字段级）：
1. sqlglot 与 DeepSeek 目标表集合 + 来源表集合均一致 → NORMAL_AGREE，写入，trust=100
2. 两者目标表有部分交集或不一致 → DISAGREE_DEEPSEEK_WINS，以 DeepSeek 为准写入，trust=Jaccard 分
3. 仅 DeepSeek 解析到目标表（sqlglot 为空）→ ABNORMAL_SINGLE_DEEPSEEK，写入 DeepSeek 结果
4. 仅 sqlglot 解析到目标表（DeepSeek 为空）→ ABNORMAL_SINGLE_SQLGLOT，写入 sqlglot 结果
5. 两者均无目标表 → SKIP_NO_TABLES，不写
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .lineage_vote import full_names_to_refs
from .models import TableLineage


# --------------------------------------------------------------------------
# LLM payload 解析（无 sqlglot 依赖）
# --------------------------------------------------------------------------

def _tables_from_llm_payload(payload: Dict[str, Any]) -> Tuple[Set[str], Set[str]]:
    """从 LLM JSON 得到 full_name 集合（无 sqlglot 依赖）。"""
    targets: Set[str] = set()
    ups: Set[str] = set()

    def _add(bucket: Set[str], rows: Any) -> None:
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            db = str(row.get("db") or "default").strip() or "default"
            tbl = str(row.get("table") or "").strip()
            if not tbl:
                continue
            bucket.add(f"{db.lower()}.{tbl.lower()}")

    _add(targets, payload.get("target_tables"))
    _add(ups, payload.get("upstream_tables"))
    return targets, ups


def tables_from_llm_payload(payload: Dict[str, Any]) -> Tuple[Set[str], Set[str]]:
    """公开接口，供 lineage_llm_compare、lineage_vote 复用。"""
    return _tables_from_llm_payload(payload)


def _norm(names: Set[str]) -> Set[str]:
    return {x.strip().lower() for x in names if x and str(x).strip()}


# --------------------------------------------------------------------------
# 信任度评分
# --------------------------------------------------------------------------

def _jaccard(a: Set[str], b: Set[str]) -> float:
    """两集合 Jaccard 相似度；均为空视为完全一致（1.0）。"""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def compute_trust_score(
    sqlglot_targets: Set[str],
    sqlglot_upstreams: Set[str],
    deepseek_targets: Set[str],
    deepseek_upstreams: Set[str],
) -> int:
    """计算 sqlglot 与 DeepSeek 结果的信任度（0-100）。

    规则：
    - 目标表 Jaccard × 50 + 来源表 Jaccard × 50，四舍五入
    - 两者完全一致 → 100
    - 完全不相交 → 0
    - 目标一致、来源不一致（或反之）→ 50
    """
    j_t = _jaccard(_norm(sqlglot_targets), _norm(deepseek_targets))
    j_u = _jaccard(_norm(sqlglot_upstreams), _norm(deepseek_upstreams))
    return round((j_t * 50 + j_u * 50))


# --------------------------------------------------------------------------
# 策略数据类
# --------------------------------------------------------------------------

@dataclass
class LineageWriteDecision:
    """表级血缘写入策略判定结果。"""

    write_upstream_lineage: bool
    status: str
    reason: str
    trust_score: int = 0
    selected_targets: Set[str] = field(default_factory=set)
    selected_upstreams: Set[str] = field(default_factory=set)
    single_source_name: Optional[str] = None
    sqlglot_targets: Set[str] = field(default_factory=set)
    sqlglot_upstreams: Set[str] = field(default_factory=set)
    deepseek_targets: Set[str] = field(default_factory=set)
    deepseek_upstreams: Set[str] = field(default_factory=set)

    def to_audit_dict(self) -> Dict[str, Any]:
        return {
            "lineage_status": self.status,
            "lineage_reason": self.reason,
            "trust_score": self.trust_score,
            "write_upstream_lineage": self.write_upstream_lineage,
            "single_source_name": self.single_source_name,
            "targets_sqlglot": sorted(self.sqlglot_targets),
            "sources_sqlglot": sorted(self.sqlglot_upstreams),
            "targets_deepseek": sorted(self.deepseek_targets),
            "sources_deepseek": sorted(self.deepseek_upstreams),
            "targets_chosen": sorted(self.selected_targets),
            "sources_chosen": sorted(self.selected_upstreams),
        }


# --------------------------------------------------------------------------
# 核心策略决策
# --------------------------------------------------------------------------

def decide_lineage_write_policy(
    sqlglot_targets: Set[str],
    sqlglot_upstreams: Set[str],
    deepseek_payload: Optional[Dict[str, Any]],
) -> LineageWriteDecision:
    """根据 sqlglot 集合与 DeepSeek LLM JSON 产出写入策略判定。

    DeepSeek 为权威来源；sqlglot 作对照并参与信任度计算。
    """
    st, su = _norm(sqlglot_targets), _norm(sqlglot_upstreams)
    if deepseek_payload is None:
        deepseek_payload = {"target_tables": [], "upstream_tables": []}
    dt, du = _tables_from_llm_payload(deepseek_payload)
    dt, du = _norm(dt), _norm(du)

    trust = compute_trust_score(st, su, dt, du)

    base = LineageWriteDecision(
        write_upstream_lineage=False,
        status="SKIP_NO_TABLES",
        reason="",
        trust_score=trust,
        sqlglot_targets=st,
        sqlglot_upstreams=su,
        deepseek_targets=dt,
        deepseek_upstreams=du,
    )

    has_s = bool(st)
    has_d = bool(dt)

    # 规则 5：均无目标表
    if not has_s and not has_d:
        base.status = "SKIP_NO_TABLES"
        base.reason = "sqlglot 与 DeepSeek 均未解析到目标表"
        return base

    # 规则 1：完全一致
    if st == dt and su == du:
        base.write_upstream_lineage = True
        base.status = "NORMAL_AGREE"
        base.reason = "sqlglot 与 DeepSeek 目标表、来源表集合完全一致"
        base.selected_targets = set(dt)
        base.selected_upstreams = set(du)
        return base

    # 规则 3：仅 DeepSeek 有目标表
    if has_d and not has_s:
        base.write_upstream_lineage = True
        base.status = "ABNORMAL_SINGLE_DEEPSEEK"
        base.reason = "sqlglot 未解析到目标表，采用 DeepSeek 结果"
        base.single_source_name = "deepseek"
        base.selected_targets = set(dt)
        base.selected_upstreams = set(du)
        return base

    # 规则 4：仅 sqlglot 有目标表
    if has_s and not has_d:
        base.write_upstream_lineage = True
        base.status = "ABNORMAL_SINGLE_SQLGLOT"
        base.reason = "DeepSeek 未返回目标表，采用 sqlglot 结果"
        base.single_source_name = "sqlglot"
        base.selected_targets = set(st)
        base.selected_upstreams = set(su)
        return base

    # 规则 2：两者均有目标表但不完全一致 → DeepSeek 为准
    base.write_upstream_lineage = True
    base.status = "DISAGREE_DEEPSEEK_WINS"
    base.reason = (
        f"目标/来源存在差异，以 DeepSeek 为准。"
        f" sqlglot T={sorted(st)} U={sorted(su)};"
        f" deepseek T={sorted(dt)} U={sorted(du)}"
    )
    base.selected_targets = set(dt)
    base.selected_upstreams = set(du)
    return base


# --------------------------------------------------------------------------
# TableLineage 重建
# --------------------------------------------------------------------------

def apply_decision_to_table_lineages(
    table_lineages: List[TableLineage],
    decision: LineageWriteDecision,
) -> List[TableLineage]:
    """用策略选中的表集合重建 TableLineage（sqlglot 为空时也可仅由 DeepSeek 集合构造）。"""
    if not decision.write_upstream_lineage:
        return table_lineages
    st_sel = decision.selected_targets
    up_sel = decision.selected_upstreams
    if not st_sel:
        return table_lineages
    refs = full_names_to_refs(st_sel)
    if not refs:
        return table_lineages
    if not table_lineages:
        return [
            TableLineage(
                target=refs[0],
                upstreams=full_names_to_refs(up_sel),
                source_block_indices=[],
            )
        ]
    primary = table_lineages[0]
    want = primary.target.full_name.lower()
    target_ref = next((r for r in refs if r.full_name.lower() == want), refs[0])
    return [
        TableLineage(
            target=target_ref,
            upstreams=full_names_to_refs(up_sel),
            source_block_indices=list(primary.source_block_indices),
        )
    ]


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------

def default_audit_log_path(output_dir: Optional[str]) -> Path:
    custom = os.environ.get("BLF_LINEAGE_AUDIT_JSONL", "").strip()
    if custom:
        return Path(custom)
    if output_dir:
        return Path(output_dir) / "lineage_audit.jsonl"
    return Path.cwd() / "lineage_audit.jsonl"


def evaluate_lineage_write_vote(
    etl_content: str,
    jfn: str,
    date_str: Optional[str],
    table_lineages: List[TableLineage],
    timeout_sec: int = 90,
) -> Tuple[List[TableLineage], LineageWriteDecision, Dict[str, Any]]:
    """调用 DeepSeek 对比并应用表级写入策略，返回（更新后的）table_lineages、判定、llm 字典。"""
    from .lineage_llm_compare import run_compare

    rep = run_compare(etl_content, jfn, date_str, timeout_sec=timeout_sec)
    llm_dict = rep.to_dict()

    st_set: Set[str] = set()
    su_set: Set[str] = set()
    for tl in table_lineages:
        st_set.add(tl.target.full_name)
        for u in tl.upstreams:
            su_set.add(u.full_name)

    decision = decide_lineage_write_policy(
        st_set,
        su_set,
        llm_dict.get("deepseek_raw"),
    )
    new_tls = apply_decision_to_table_lineages(table_lineages, decision)
    return new_tls, decision, llm_dict


def append_lineage_audit_jsonl(
    path: Path,
    job: str,
    decision: LineageWriteDecision,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """追加一行审计 JSONL（目标表、来源表、信任度、状态、原因等）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    rec: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "job": job,
        **decision.to_audit_dict(),
    }
    if extra:
        rec.update(extra)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
