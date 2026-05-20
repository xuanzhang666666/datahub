"""lineage_write_policy — DeepSeek 表级血缘写入策略、校验与审计字段。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .hive_fqtn_validation import filter_table_lineages_by_hive_fqtn_rules
from .hive_table_existence import (
    filter_lineages_require_full_hive_presence,
    should_skip_hive_existence_check,
)
from .lineage_vote import full_names_to_refs
from .models import TableLineage


# --------------------------------------------------------------------------
# LLM payload 解析
# --------------------------------------------------------------------------

_NOT_VERIFIED_PREFIX = "not_verified_"


def resolve_not_verified_table_alias(fqtn: str) -> str:
    """``db.not_verified_<real_table>`` → ``db.<real_table>``；其它 fqtn 原样返回（小写）。"""
    s = fqtn.strip().lower()
    if s.count(".") != 1:
        return s
    db, tbl = s.split(".", 1)
    if not db or not tbl:
        return s
    if tbl.startswith(_NOT_VERIFIED_PREFIX):
        tbl = tbl[len(_NOT_VERIFIED_PREFIX) :]
    return f"{db}.{tbl}" if tbl else ""


def llm_row_to_fqtn(row: Any) -> Optional[str]:
    """将 LLM JSON 中的 ``{db, table}`` 转为小写 ``db.table``。

    - 未给出库名或为空时，使用 ``default``。
    - 若库名为空且 ``table`` 为单段 ``schema.table``，则拆成库、表。
    - 若已给出库名而 ``table`` 仍含 ``.``，视为不确定，返回 ``None``。
    - ``table`` 中含多个 ``.`` 且库名为空时，返回 ``None``。
    - 表名以 ``not_verified_`` 开头时去掉该前缀（ETL 别名 → 真实 Hive 表名）。
    """
    if not isinstance(row, dict):
        return None
    db_raw = str(row.get("db") or "").strip().lower()
    tbl_raw = str(row.get("table") or "").strip().lower()
    if not tbl_raw:
        return None
    if "." in tbl_raw:
        if db_raw:
            return None
        if tbl_raw.count(".") != 1:
            return None
        a, b = tbl_raw.split(".", 1)
        if not a or not b:
            return None
        db_raw, tbl_raw = a, b
    if not db_raw:
        db_raw = "default"
    resolved = resolve_not_verified_table_alias(f"{db_raw}.{tbl_raw}")
    if not resolved or resolved.endswith("."):
        return None
    return resolved


def _tables_from_llm_payload(payload: Dict[str, Any]) -> Tuple[Set[str], Set[str]]:
    """从 LLM JSON 得到 full_name 集合。"""
    targets: Set[str] = set()
    ups: Set[str] = set()

    def _add(bucket: Set[str], rows: Any) -> None:
        if not isinstance(rows, list):
            return
        for row in rows:
            fq = llm_row_to_fqtn(row)
            if fq:
                bucket.add(fq)

    _add(targets, payload.get("target_tables"))
    _add(ups, payload.get("upstream_tables"))
    return targets, ups


def tables_from_llm_payload(payload: Dict[str, Any]) -> Tuple[Set[str], Set[str]]:
    """公开接口，供 lineage_llm_compare、lineage_vote 复用。"""
    return _tables_from_llm_payload(payload)


def _norm(names: Set[str]) -> Set[str]:
    return {x.strip().lower() for x in names if x and str(x).strip()}


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
    deepseek_targets: Set[str] = field(default_factory=set)
    deepseek_upstreams: Set[str] = field(default_factory=set)
    fqtn_validation: Optional[Dict[str, Any]] = None
    hive_existence: Optional[Dict[str, Any]] = None

    def to_audit_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "lineage_status": self.status,
            "lineage_reason": self.reason,
            "trust_score": self.trust_score,
            "write_upstream_lineage": self.write_upstream_lineage,
            "single_source_name": self.single_source_name,
            "targets_deepseek": sorted(self.deepseek_targets),
            "sources_deepseek": sorted(self.deepseek_upstreams),
            "targets_chosen": sorted(self.selected_targets),
            "sources_chosen": sorted(self.selected_upstreams),
        }
        if self.fqtn_validation is not None:
            out["fqtn_validation"] = self.fqtn_validation
        if self.hive_existence is not None:
            out["hive_existence"] = self.hive_existence
        return out


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


def _parse_lineage_array(raw: Dict[str, Any]) -> List[TableLineage]:
    """解析 LLM 返回的 lineage 数组格式，每个目标表使用自己对应的上游表集合。

    支持新格式（lineage 数组）和旧格式（target_tables + upstream_tables 扁平列表）兜底。
    """
    lineage_items = raw.get("lineage")
    if isinstance(lineage_items, list) and lineage_items:
        result: List[TableLineage] = []
        seen_targets: Dict[str, TableLineage] = {}
        for item in lineage_items:
            if not isinstance(item, dict):
                continue
            tgt_name = llm_row_to_fqtn(item.get("target"))
            if not tgt_name:
                continue
            up_names: Set[str] = set()
            for row in (item.get("upstreams") or []):
                u = llm_row_to_fqtn(row)
                if u:
                    up_names.add(u)
            tgt_refs = full_names_to_refs({tgt_name})
            if not tgt_refs:
                continue
            tgt_ref = tgt_refs[0]
            if tgt_name in seen_targets:
                # 同一目标表出现多次，合并上游
                existing = seen_targets[tgt_name]
                existing_up_names = {u.full_name for u in existing.upstreams}
                merged = existing_up_names | up_names
                existing.upstreams = full_names_to_refs(merged)
            else:
                tl = TableLineage(
                    target=tgt_ref,
                    upstreams=full_names_to_refs(up_names),
                    source_block_indices=[],
                )
                seen_targets[tgt_name] = tl
                result.append(tl)
        return result

    # 旧格式兜底：所有目标表共享全量上游（结构无法区分时退化）
    dt, du = _tables_from_llm_payload(raw)
    dt, du = _norm(dt), _norm(du)
    if not dt:
        return []
    up_refs = full_names_to_refs(du)
    return [
        TableLineage(target=ref, upstreams=up_refs, source_block_indices=[])
        for ref in full_names_to_refs(dt)
    ]


def apply_lineage_filters_from_parsed(
    parsed: List[TableLineage],
    raw: Dict[str, Any],
) -> Tuple[List[TableLineage], LineageWriteDecision]:
    """对 LLM 解析结果做 fqtn 规则与 Hive 存在性过滤，产出与 batch_sync 一致的写入决策。"""
    ds_targets = {tl.target.full_name.lower() for tl in parsed}
    ds_upstreams = {u.full_name.lower() for tl in parsed for u in tl.upstreams}

    if not parsed:
        _, du = _tables_from_llm_payload(raw)
        du = _norm(du)
        decision = LineageWriteDecision(
            write_upstream_lineage=False,
            status="SKIP_NO_TABLES",
            reason="LLM 未提取到目标表",
            trust_score=0,
            selected_targets=set(),
            selected_upstreams=du,
            deepseek_targets=set(),
            deepseek_upstreams=du,
        )
        return [], decision

    table_lineages, fqtn_meta = filter_table_lineages_by_hive_fqtn_rules(parsed)
    if not table_lineages:
        decision = LineageWriteDecision(
            write_upstream_lineage=False,
            status="SKIP_INVALID_FQTN",
            reason="LLM 表名未通过 fqtn 规则校验（库白名单/表层级前缀/下划线数量等），未写入血缘",
            trust_score=0,
            selected_targets=set(),
            selected_upstreams=set(),
            deepseek_targets=set(ds_targets),
            deepseek_upstreams=set(ds_upstreams),
            fqtn_validation=fqtn_meta,
        )
        return [], decision

    hive_exist_meta: Dict[str, Any]
    if should_skip_hive_existence_check():
        hive_exist_meta = {
            "skipped": True,
            "reason": "BLF_LINEAGE_SKIP_HIVE_EXISTENCE_CHECK",
            "checked": False,
        }
    else:
        table_lineages, hive_exist_meta = filter_lineages_require_full_hive_presence(table_lineages)

    if not table_lineages:
        if hive_exist_meta.get("error"):
            decision = LineageWriteDecision(
                write_upstream_lineage=False,
                status="SKIP_HIVE_EXISTENCE_CHECK_FAILED",
                reason="Hive 表存在性校验失败（Trino/元数据异常），未写入血缘",
                trust_score=0,
                selected_targets=set(),
                selected_upstreams=set(),
                deepseek_targets=set(ds_targets),
                deepseek_upstreams=set(ds_upstreams),
                fqtn_validation=fqtn_meta,
                hive_existence=hive_exist_meta,
            )
        else:
            decision = LineageWriteDecision(
                write_upstream_lineage=False,
                status="SKIP_HIVE_TABLE_NOT_FOUND",
                reason="目标或上游在 Hive 中不存在（information_schema），未写入血缘",
                trust_score=0,
                selected_targets=set(),
                selected_upstreams=set(),
                deepseek_targets=set(ds_targets),
                deepseek_upstreams=set(ds_upstreams),
                fqtn_validation=fqtn_meta,
                hive_existence=hive_exist_meta,
            )
        return [], decision

    all_targets = {tl.target.full_name for tl in table_lineages}
    all_upstreams = {u.full_name for tl in table_lineages for u in tl.upstreams}
    had_fqtn_filter = bool(
        fqtn_meta.get("rejected_invalid_targets")
        or fqtn_meta.get("dropped_targets_no_valid_upstream")
        or fqtn_meta.get("stripped_invalid_upstreams")
    )
    had_hive_drop = bool(hive_exist_meta.get("removed_lineages"))
    trust = 90
    if had_fqtn_filter:
        trust = 75
    if had_hive_drop:
        trust = min(trust, 70)
    reason = (
        f"LLM 提取到 {len(table_lineages)} 个目标表（已校验库白名单、表前缀、下划线及 Hive 存在性）"
    )
    if had_fqtn_filter:
        reason += "；部分 fqtn 已按命名规则过滤"
    if had_hive_drop:
        reason += "；部分 lineage 因 Hive 中不存在整段丢弃"
    decision = LineageWriteDecision(
        write_upstream_lineage=True,
        status="LLM_EXTRACTED",
        reason=reason,
        trust_score=trust,
        selected_targets=all_targets,
        selected_upstreams=all_upstreams,
        deepseek_targets=set(ds_targets),
        deepseek_upstreams=set(ds_upstreams),
        fqtn_validation=fqtn_meta,
        hive_existence=hive_exist_meta,
    )
    return table_lineages, decision


def evaluate_llm_only(
    etl_script: str,
    timeout_sec: int = 90,
    job_file_name: str = "",
) -> Tuple[List[TableLineage], LineageWriteDecision, Dict[str, Any]]:
    """仅用 LLM 提取表级血缘。

    LLM 返回 per-target lineage 数组，每个目标表有独立的上游表列表。
    依次应用 fqtn 规则校验与 Hive 表存在性校验（可通过环境变量跳过存在性校验）。
    返回 (table_lineages, decision, raw_payload)。
    """
    from .lineage_llm_compare import call_llm_extract

    raw = call_llm_extract(etl_script, timeout_sec=timeout_sec, job_file_name=job_file_name)
    parsed = _parse_lineage_array(raw)
    table_lineages, decision = apply_lineage_filters_from_parsed(parsed, raw)
    return table_lineages, decision, raw


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
