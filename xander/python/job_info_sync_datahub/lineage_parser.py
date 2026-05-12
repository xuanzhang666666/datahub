"""lineage_parser — 使用 sqlglot AST 解析 SQL blocks 的表级和字段级血缘。

强约束：
  - 目标表和上游表必须从 AST 节点读取，禁止正则。
  - sqlglot 为硬依赖，未安装时启动即报错。
  - 无法解析时记录 SQL_PARSE_FAILED 日志并标记 block，不产出错误血缘。

支持的 SQL 方言：HiveQL（sqlglot dialect="hive"）
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Set, Tuple

from .logging_utils import SQL_PARSE_FAILED, SQL_PARSE_PARTIAL, get_logger, log_phase_warning
from .models import (
    FieldLineage,
    FieldMapping,
    ParseConfidence,
    ParseStatus,
    SqlBlock,
    TableLineage,
    TableRef,
)

try:
    import sqlglot
    import sqlglot.expressions as exp
except ImportError as _e:
    raise SystemExit(
        "缺少依赖：请先执行 python3 -m pip install sqlglot\n"
        "sqlglot 是解析 SQL 血缘的硬依赖，无法降级到正则。"
    ) from _e

logger = get_logger("lineage_parser")


# ---------------------------------------------------------------------------
# 内部工具函数
# ---------------------------------------------------------------------------


def _normalize_table_ref(node: exp.Table) -> Optional[TableRef]:
    """从 sqlglot Table 节点提取 TableRef，缺库名时默认 default。"""
    table_name = node.name
    if not table_name:
        return None
    db = node.db or "default"
    return TableRef(db=db, table=table_name)


def _extract_target_tables(statement: exp.Expression) -> List[TableRef]:
    """从 AST 中提取写入目标表（INSERT/CREATE）。"""
    targets: List[TableRef] = []

    # INSERT INTO / INSERT OVERWRITE
    if isinstance(statement, (exp.Insert,)):
        tbl = statement.find(exp.Table)
        if tbl:
            ref = _normalize_table_ref(tbl)
            if ref:
                targets.append(ref)

    # CREATE TABLE / CREATE EXTERNAL TABLE
    elif isinstance(statement, exp.Create):
        kind = statement.args.get("kind", "")
        if isinstance(kind, str) and kind.upper() == "TABLE":
            tbl = statement.find(exp.Table)
            if tbl:
                ref = _normalize_table_ref(tbl)
                if ref:
                    targets.append(ref)

    return targets


def _extract_upstream_tables(statement: exp.Expression) -> Set[TableRef]:
    """从 AST 的 FROM / JOIN 节点提取所有上游表引用。"""
    upstreams: Set[TableRef] = set()
    for tbl in statement.find_all(exp.Table):
        ref = _normalize_table_ref(tbl)
        if ref:
            upstreams.add(ref)
    return upstreams


def _table_alias_map(statement: exp.Expression) -> Dict[str, TableRef]:
    """构建 alias → TableRef 的映射，用于字段溯源时解析表别名。"""
    alias_map: Dict[str, TableRef] = {}
    for tbl in statement.find_all(exp.Table):
        ref = _normalize_table_ref(tbl)
        if ref and tbl.alias:
            alias_map[tbl.alias.lower()] = ref
    return alias_map


def _column_source(
    col: exp.Expression,
    alias_map: Dict[str, TableRef],
) -> Tuple[Optional[str], Optional[str]]:
    """从列表达式中尝试提取 (table_full_name, field_name)。"""
    if isinstance(col, exp.Column):
        field = col.name
        tbl_token = col.table
        if tbl_token:
            ref = alias_map.get(tbl_token.lower())
            src_table = ref.full_name if ref else tbl_token
        else:
            src_table = None
        return src_table, field
    return None, None


def _extract_field_mappings(
    statement: exp.Expression,
    target_ref: TableRef,
    alias_map: Dict[str, TableRef],
) -> Tuple[List[FieldMapping], ParseConfidence]:
    """从 INSERT ... SELECT 的 SELECT 列表提取字段级映射。

    返回 (mappings, confidence)：
      - HIGH：所有列都能确定来源
      - PARTIAL：部分列无法确定（表达式、*、子查询等）
    """
    mappings: List[FieldMapping] = []
    confidence = ParseConfidence.HIGH

    # 只处理 INSERT ... SELECT 形式
    if not isinstance(statement, exp.Insert):
        return mappings, confidence

    select_node = statement.find(exp.Select)
    if select_node is None:
        return mappings, confidence

    select_exprs = select_node.expressions
    insert_cols: List[str] = []

    # INSERT INTO t (col1, col2, ...) SELECT ... 取列名
    schema_node = statement.find(exp.Schema)
    if schema_node:
        insert_cols = [c.name for c in schema_node.find_all(exp.Column)]

    for i, sel_expr in enumerate(select_exprs):
        # 获取别名或位置列名
        if isinstance(sel_expr, exp.Alias):
            target_field = sel_expr.alias
            inner = sel_expr.this
        else:
            inner = sel_expr
            # 优先使用 INSERT 列列表；其次对简单列引用使用列名本身；最后使用位置占位符
            if i < len(insert_cols):
                target_field = insert_cols[i]
            elif isinstance(inner, exp.Column):
                target_field = inner.name
            else:
                target_field = f"_col_{i}"

        expression_text = inner.sql(dialect="hive")

        # 尝试提取来源
        src_table, src_field = _column_source(inner, alias_map)

        if isinstance(inner, exp.Star):
            confidence = ParseConfidence.PARTIAL
            mappings.append(
                FieldMapping(
                    target_field=target_field or "*",
                    source_table=None,
                    source_field=None,
                    expression="*",
                    confidence=ParseConfidence.PARTIAL,
                )
            )
            continue

        # 复杂表达式：无法精确溯源
        if src_field is None and not isinstance(inner, exp.Column):
            confidence = ParseConfidence.PARTIAL
            mappings.append(
                FieldMapping(
                    target_field=target_field,
                    source_table=src_table,
                    source_field=None,
                    expression=expression_text,
                    confidence=ParseConfidence.PARTIAL,
                )
            )
        else:
            mappings.append(
                FieldMapping(
                    target_field=target_field,
                    source_table=src_table,
                    source_field=src_field,
                    expression=expression_text,
                    confidence=ParseConfidence.HIGH,
                )
            )

    return mappings, confidence


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------


def parse_block_lineage(
    block: SqlBlock,
    job_display_name: str = "",
    parent_logger: Optional[logging.Logger] = None,
) -> SqlBlock:
    """解析单个 SqlBlock 的血缘，结果写回 block 并返回。

    失败时 block.status = SQL_PARSE_FAILED，不修改 target_tables/upstream_tables。
    """
    _log = parent_logger or logger

    try:
        statements = sqlglot.parse(block.raw_sql, dialect="hive", error_level=sqlglot.ErrorLevel.WARN)
    except Exception as exc:
        block.status = ParseStatus.SQL_PARSE_FAILED
        block.error_detail = str(exc)
        log_phase_warning(
            _log, SQL_PARSE_FAILED, job_display_name,
            f"block #{block.index} sqlglot.parse 异常: {exc}"
        )
        return block

    if not statements:
        block.status = ParseStatus.SKIPPED
        logger.debug("block #%d 解析结果为空（可能是注释或空语句），跳过", block.index)
        return block

    all_targets: List[TableRef] = []
    all_upstreams: Set[TableRef] = set()
    all_field_mappings: List[FieldMapping] = []
    overall_confidence = ParseConfidence.HIGH
    parse_errors: List[str] = []

    for stmt in statements:
        if stmt is None:
            continue

        try:
            targets = _extract_target_tables(stmt)
            upstreams = _extract_upstream_tables(stmt)

            # 上游表中排除目标表本身（INSERT OVERWRITE 自引用等场景）
            target_set = set(targets)
            upstreams -= target_set

            all_targets.extend(targets)
            all_upstreams.update(upstreams)

            # 字段级：只对每个目标表解析
            alias_map = _table_alias_map(stmt)
            for t_ref in targets:
                mappings, conf = _extract_field_mappings(stmt, t_ref, alias_map)
                all_field_mappings.extend(mappings)
                if conf == ParseConfidence.PARTIAL:
                    overall_confidence = ParseConfidence.PARTIAL

        except Exception as exc:
            parse_errors.append(str(exc))
            overall_confidence = ParseConfidence.PARTIAL
            logger.debug("block #%d 子语句解析异常: %s", block.index, exc)

    if parse_errors:
        block.status = ParseStatus.SQL_PARSE_PARTIAL
        block.error_detail = "; ".join(parse_errors)
        log_phase_warning(
            _log, SQL_PARSE_PARTIAL, job_display_name,
            f"block #{block.index} 部分语句解析失败: {block.error_detail}"
        )
    elif not all_targets:
        # 无写入目标（SELECT-only、质检 SQL 等），标记为跳过
        block.status = ParseStatus.SKIPPED
        logger.debug("block #%d 无写入目标，标记为 SKIPPED", block.index)
    else:
        block.status = ParseStatus.OK

    block.target_tables = list(dict.fromkeys(all_targets))
    block.upstream_tables = list(all_upstreams)
    block.field_mappings = all_field_mappings
    block.confidence = overall_confidence

    logger.info(
        "block #%d 解析: targets=%s upstreams=%s fields=%d confidence=%s",
        block.index,
        [t.full_name for t in block.target_tables],
        [u.full_name for u in block.upstream_tables],
        len(block.field_mappings),
        block.confidence.value,
    )
    return block


def build_lineage_summary(
    blocks: List[SqlBlock],
) -> Tuple[List[TableLineage], List[FieldLineage]]:
    """汇总所有 block 的解析结果，去重合并为 TableLineage 和 FieldLineage。"""
    # target_full_name → TableLineage
    table_map: Dict[str, TableLineage] = {}
    # (target_full_name, field_name) → FieldLineage
    field_map: Dict[Tuple[str, str], FieldLineage] = {}

    for block in blocks:
        if block.status in (ParseStatus.SQL_PARSE_FAILED, ParseStatus.SKIPPED):
            continue

        for t_ref in block.target_tables:
            key = t_ref.full_name
            if key not in table_map:
                table_map[key] = TableLineage(target=t_ref, source_block_indices=[block.index])
            else:
                if block.index not in table_map[key].source_block_indices:
                    table_map[key].source_block_indices.append(block.index)

            for u_ref in block.upstream_tables:
                if u_ref not in table_map[key].upstreams:
                    table_map[key].upstreams.append(u_ref)

        for fm in block.field_mappings:
            # 将 field mapping 关联到对应目标表
            for t_ref in block.target_tables:
                fk = (t_ref.full_name, fm.target_field)
                if fk not in field_map:
                    field_map[fk] = FieldLineage(
                        target_table=t_ref,
                        target_field=fm.target_field,
                        mappings=[fm],
                        confidence=fm.confidence,
                    )
                else:
                    field_map[fk].mappings.append(fm)
                    if fm.confidence == ParseConfidence.PARTIAL:
                        field_map[fk].confidence = ParseConfidence.PARTIAL

    table_lineages = list(table_map.values())
    field_lineages = list(field_map.values())

    logger.info(
        "血缘汇总完成: table_lineages=%d field_lineages=%d",
        len(table_lineages),
        len(field_lineages),
    )
    return table_lineages, field_lineages
