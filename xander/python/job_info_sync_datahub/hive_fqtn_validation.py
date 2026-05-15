"""Hive 全限定表名（fqtn = db.table）校验 — LLM 血缘写入前规则。

规则摘要：

1. **形态**：``<database>.<table>``，恰好一个 ``.``，两段非空（比较前 strip；库、表名转小写）。
2. **库名白名单**：库名须在给定的 Hive 库列表中（与内网元数据一致）。
3. **表名层级前缀**：表名以 ``dm`` / ``ods`` / ``pdw`` / ``app`` / ``dw`` / ``mid`` / ``ai`` /
   ``dwa`` / ``dwd`` / ``dim`` 之一开头；较长前缀优先匹配（如 ``dwa`` 先于 ``dw``）。
4. **表名下划线**：表名中 ``_`` 出现次数 **≥ 2**（如 ``dw_order_v1``、``dw_order_ha_v1`` 合法；
   ``dw_v1`` 仅 1 个 ``_`` 不合法）。
5. **表名标识符**：仅含字母、数字、下划线，且**不能以数字开头**（如含 ``${date}`` 或以 ``001_`` 开头不合法）。

不通过则对应 fqtn 不写入（由 ``filter_table_lineages_by_hive_fqtn_rules`` 过滤）。

LLM 解析阶段（``lineage_write_policy.llm_row_to_fqtn``）会将表名 ``not_verified_*`` 别名解析为
去掉前缀后的真实 Hive 表名，再进入本模块校验。
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Dict, List, Tuple

from .models import TableLineage, TableRef

_ALLOWED_HIVE_DATABASES: frozenset[str] = frozenset(
    {
        "bike",
        "data_autobox",
        "data_autobox_dev",
        "data_build",
        "data_build_dev",
        "data_cold",
        "data_countermall",
        "data_default_dev",
        "data_drink",
        "data_equipment",
        "data_everyphant",
        "data_factory",
        "data_factory_dev",
        "data_finance",
        "data_finance_dev",
        "data_fresh",
        "data_gis_h3",
        "data_ic",
        "data_logistics",
        "data_logistics_dev",
        "data_md",
        "data_md_dev",
        "data_or",
        "data_or_dev",
        "data_promotion",
        "data_promotion_dev",
        "data_sec_dw",
        "data_sec_risk",
        "data_shop",
        "data_shop_dev",
        "data_smartorder",
        "data_smartorder_dev",
        "data_supplychain",
        "data_supplychain_dev",
        "data_supplychain_finance",
        "data_support",
        "data_takeaway",
        "data_takeaway_dev",
        "data_tech",
        "data_userinsight",
        "data_userresearch",
        "defa",
        "default",
        "hivemall",
        "hue",
        "iot",
        "kylin_flat_db",
        "report",
        "shelf",
        "yummy",
    }
)

_LAYER_PREFIXES: Tuple[str, ...] = (
    "dwa",
    "dwd",
    "dim",
    "ods",
    "pdw",
    "app",
    "mid",
    "dm",
    "dw",
    "ai",
)
_LAYER_PREFIXES_SORTED: Tuple[str, ...] = tuple(
    sorted(_LAYER_PREFIXES, key=lambda p: (-len(p), p))
)

_TABLE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _table_identifier_valid(table_name: str) -> Tuple[bool, str]:
    """表名仅允许字母/数字/下划线，且不能以数字开头。"""
    t = table_name.strip()
    if not t:
        return False, "empty"
    if t[0].isdigit():
        return False, "table_starts_with_digit"
    if not _TABLE_IDENTIFIER_RE.match(t):
        return False, "table_invalid_identifier"
    return True, ""


def _table_matches_layer_prefix(table_name: str) -> bool:
    t = table_name.strip().lower()
    if not t:
        return False
    return any(t.startswith(p) for p in _LAYER_PREFIXES_SORTED)


def is_valid_hive_fqtn(full_name: str) -> Tuple[bool, str]:
    """满足形态、库白名单、表前缀与下划线条数则 (True, \"\")，否则 (False, 原因码)。"""
    s = full_name.strip()
    if not s:
        return False, "empty"
    if s.count(".") != 1:
        return False, "not_single_dot"
    db, tbl = s.split(".", 1)
    db = db.strip().lower()
    tbl = tbl.strip().lower()
    if not db or not tbl:
        return False, "empty_segment"
    if db not in _ALLOWED_HIVE_DATABASES:
        return False, "db_not_in_allowlist"
    ok_id, id_code = _table_identifier_valid(tbl)
    if not ok_id:
        return False, id_code
    if not _table_matches_layer_prefix(tbl):
        return False, "table_not_layer_prefix"
    if tbl.count("_") < 2:
        return False, "table_underscore_lt2"
    return True, ""


def filter_table_lineages_by_hive_fqtn_rules(
    lineages: List[TableLineage],
) -> Tuple[List[TableLineage], Dict[str, Any]]:
    """过滤 LLM 产出的表级血缘：规则不通过的目标整段丢弃；目标合法则丢弃非法上游。

    若某目标在过滤上游后 **无任何合法上游**，则不保留该条 ``TableLineage``（不写该目标血缘）。

    返回 (保留的 lineage 列表, 审计用 dict)。
    """
    kept: List[TableLineage] = []
    rejected_targets: List[Dict[str, str]] = []
    dropped_no_upstream: List[str] = []
    stripped_upstreams: List[Dict[str, Any]] = []

    for tl in lineages:
        tgt_fn = tl.target.full_name
        ok_t, code_t = is_valid_hive_fqtn(tgt_fn)
        if not ok_t:
            rejected_targets.append({"fqtn": tgt_fn, "reason": code_t})
            continue

        norm_target = TableRef(
            db=tl.target.db.strip().lower(),
            table=tl.target.table.strip().lower(),
        )
        good_upstream: List[TableRef] = []
        bad_upstream: List[Dict[str, str]] = []
        for u in tl.upstreams:
            ufn = u.full_name
            ok_u, code_u = is_valid_hive_fqtn(ufn)
            if ok_u:
                good_upstream.append(
                    TableRef(db=u.db.strip().lower(), table=u.table.strip().lower())
                )
            else:
                bad_upstream.append({"fqtn": ufn, "reason": code_u})

        if bad_upstream:
            stripped_upstreams.append(
                {
                    "target": norm_target.full_name,
                    "removed": bad_upstream,
                }
            )

        if not good_upstream:
            dropped_no_upstream.append(norm_target.full_name)
            continue

        kept.append(
            replace(
                tl,
                target=norm_target,
                upstreams=good_upstream,
            )
        )

    meta: Dict[str, Any] = {
        "allowed_hive_database_count": len(_ALLOWED_HIVE_DATABASES),
        "allowed_table_layer_prefixes": list(_LAYER_PREFIXES_SORTED),
        "rejected_invalid_targets": rejected_targets,
        "dropped_targets_no_valid_upstream": dropped_no_upstream,
        "stripped_invalid_upstreams": stripped_upstreams,
        "kept_lineage_count": len(kept),
    }
    return kept, meta
