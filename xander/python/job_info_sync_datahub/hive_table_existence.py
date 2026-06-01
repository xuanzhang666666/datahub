"""通过 Trino 查询 Hive information_schema，确认表/视图是否真实存在。

用于血缘写入前校验（``BASE TABLE`` 或 ``VIEW``）：

- ``filter_lineages_require_full_hive_presence``：目标或任一上游不存在则整段丢弃（LLM 路径）。
- ``filter_lineages_drop_missing_upstreams``：仅剔除不存在的上游（Documentation「4. 数据来源」路径）。

查询失败视为「不确定」，不写入血缘。

环境变量 ``BLF_LINEAGE_SKIP_HIVE_EXISTENCE_CHECK=1`` 时跳过校验（仅开发/单测；审计中记 ``skipped``）。
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Any, Dict, List, Set, Tuple

from .models import TableLineage, TableRef
from .schedule_client import _trino_conn


def should_skip_hive_existence_check() -> bool:
    v = os.environ.get("BLF_LINEAGE_SKIP_HIVE_EXISTENCE_CHECK", "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _sql_literal(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _collect_fqtns(lineages: List[TableLineage]) -> Set[str]:
    out: Set[str] = set()
    for tl in lineages:
        out.add(tl.target.full_name.lower())
        for u in tl.upstreams:
            out.add(u.full_name.lower())
    return out


def query_hive_existing_fqtns(fqtns: Set[str], *, chunk_size: int = 120) -> Set[str]:
    """返回在 ``hive.information_schema.tables`` 中存在的 fqtn 集合（小写）。"""
    if not fqtns:
        return set()
    catalog = os.environ.get("TRINO_CATALOG", "hive").strip() or "hive"
    existing: Set[str] = set()
    sorted_fq = sorted(fqtns)
    conn = _trino_conn()
    cur = conn.cursor()
    try:
        for i in range(0, len(sorted_fq), chunk_size):
            chunk = sorted_fq[i : i + chunk_size]
            in_list = ", ".join(_sql_literal(f) for f in chunk)
            sql = f"""
            SELECT lower(concat(cast(table_schema AS varchar), '.', cast(table_name AS varchar)))
            FROM {catalog}.information_schema.tables
            WHERE lower(cast(table_type AS varchar)) IN ('base table', 'view')
              AND lower(concat(cast(table_schema AS varchar), '.', cast(table_name AS varchar))) IN ({in_list})
            """.strip()
            cur.execute(sql)
            for row in cur.fetchall():
                if row and row[0]:
                    existing.add(str(row[0]).lower())
    finally:
        cur.close()
        conn.close()
    return existing


def filter_lineages_require_full_hive_presence(
    lineages: List[TableLineage],
) -> Tuple[List[TableLineage], Dict[str, Any]]:
    """仅保留「目标表存在且所有上游表均存在」的 lineage；否则整段丢弃。

    返回 (过滤后列表, 审计 dict)。
    """
    meta: Dict[str, Any] = {
        "checked": True,
        "skipped": False,
        "removed_lineages": [],
    }
    if should_skip_hive_existence_check():
        meta["skipped"] = True
        meta["reason"] = "BLF_LINEAGE_SKIP_HIVE_EXISTENCE_CHECK"
        return lineages, meta

    need = _collect_fqtns(lineages)
    try:
        existing = query_hive_existing_fqtns(need)
    except Exception as exc:
        meta["error"] = str(exc)[:800]
        meta["checked"] = False
        return [], meta

    meta["requested_count"] = len(need)
    meta["found_count"] = len(existing & need)
    kept: List[TableLineage] = []

    for tl in lineages:
        tgt = tl.target.full_name.lower()
        if tgt not in existing:
            meta["removed_lineages"].append(
                {"target": tl.target.full_name, "reason": "target_not_in_hive"}
            )
            continue
        missing = [u.full_name for u in tl.upstreams if u.full_name.lower() not in existing]
        if missing:
            meta["removed_lineages"].append(
                {
                    "target": tl.target.full_name,
                    "reason": "upstream_not_in_hive",
                    "missing_upstreams": missing,
                }
            )
            continue
        norm_target = TableRef(db=tl.target.db.strip().lower(), table=tl.target.table.strip().lower())
        norm_up = [
            TableRef(db=u.db.strip().lower(), table=u.table.strip().lower()) for u in tl.upstreams
        ]
        kept.append(replace(tl, target=norm_target, upstreams=norm_up))

    meta["kept_lineage_count"] = len(kept)
    return kept, meta


def filter_lineages_drop_missing_upstreams(
    lineages: List[TableLineage],
) -> Tuple[List[TableLineage], Dict[str, Any]]:
    """保留目标表存在的 lineage；Hive 中不存在的上游表逐条剔除（不整段丢弃）。

    若剔除后无有效上游，则丢弃该 lineage。Trino 查询失败时不写入血缘。
    """
    meta: Dict[str, Any] = {
        "checked": True,
        "skipped": False,
        "mode": "drop_missing_upstream",
        "removed_lineages": [],
        "stripped_upstreams": [],
    }
    if should_skip_hive_existence_check():
        meta["skipped"] = True
        meta["reason"] = "BLF_LINEAGE_SKIP_HIVE_EXISTENCE_CHECK"
        return lineages, meta

    need = _collect_fqtns(lineages)
    try:
        existing = query_hive_existing_fqtns(need)
    except Exception as exc:
        meta["error"] = str(exc)[:800]
        meta["checked"] = False
        return [], meta

    meta["requested_count"] = len(need)
    meta["found_count"] = len(existing & need)
    kept: List[TableLineage] = []

    for tl in lineages:
        tgt = tl.target.full_name.lower()
        if tgt not in existing:
            meta["removed_lineages"].append(
                {"target": tl.target.full_name, "reason": "target_not_in_hive"}
            )
            continue

        missing = [u.full_name for u in tl.upstreams if u.full_name.lower() not in existing]
        valid_upstreams = [u for u in tl.upstreams if u.full_name.lower() in existing]
        if missing:
            meta["stripped_upstreams"].append(
                {
                    "target": tl.target.full_name,
                    "reason": "upstream_not_in_hive",
                    "missing_upstreams": missing,
                }
            )
        if not valid_upstreams:
            meta["removed_lineages"].append(
                {
                    "target": tl.target.full_name,
                    "reason": "no_valid_upstream_in_hive",
                    "missing_upstreams": missing,
                }
            )
            continue

        norm_target = TableRef(db=tl.target.db.strip().lower(), table=tl.target.table.strip().lower())
        norm_up = [
            TableRef(db=u.db.strip().lower(), table=u.table.strip().lower()) for u in valid_upstreams
        ]
        kept.append(replace(tl, target=norm_target, upstreams=norm_up))

    meta["kept_lineage_count"] = len(kept)
    return kept, meta
