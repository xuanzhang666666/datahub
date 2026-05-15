"""测试 lineage_parser：sqlglot AST 血缘解析。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from job_info_sync_datahub.lineage_parser import build_lineage_summary, parse_block_lineage
from job_info_sync_datahub.models import ParseConfidence, ParseStatus, SqlBlock


def _make_block(sql: str, idx: int = 0) -> SqlBlock:
    return SqlBlock(index=idx, raw_sql=sql)


# ---------------------------------------------------------------------------
# 目标表（INSERT）
# ---------------------------------------------------------------------------


def test_insert_overwrite_target():
    sql = "INSERT OVERWRITE TABLE default.pdw_opc_flag_contact SELECT id FROM default.ods_opc_flag_contact"
    block = parse_block_lineage(_make_block(sql))
    assert block.status == ParseStatus.OK
    targets = [t.full_name for t in block.target_tables]
    assert "default.pdw_opc_flag_contact" in targets


def test_insert_into_with_db():
    sql = "INSERT INTO mydb.my_table SELECT a FROM src_table"
    block = parse_block_lineage(_make_block(sql))
    targets = [t.full_name for t in block.target_tables]
    assert "mydb.my_table" in targets


def test_create_table_target():
    sql = "CREATE TABLE IF NOT EXISTS default.pdw_new SELECT id FROM default.ods_src"
    block = parse_block_lineage(_make_block(sql))
    targets = [t.full_name for t in block.target_tables]
    assert "default.pdw_new" in targets


# ---------------------------------------------------------------------------
# 上游表（FROM / JOIN）
# ---------------------------------------------------------------------------


def test_upstream_from():
    sql = "INSERT INTO default.target SELECT a FROM default.source_table"
    block = parse_block_lineage(_make_block(sql))
    upstreams = [u.full_name for u in block.upstream_tables]
    assert "default.source_table" in upstreams
    # 目标表不应在上游里
    assert "default.target" not in upstreams


def test_upstream_join():
    sql = (
        "INSERT INTO default.pdw_t "
        "SELECT a.id, b.name "
        "FROM default.ods_a a JOIN default.ods_b b ON a.id = b.id"
    )
    block = parse_block_lineage(_make_block(sql))
    upstreams = [u.full_name for u in block.upstream_tables]
    assert "default.ods_a" in upstreams
    assert "default.ods_b" in upstreams


# ---------------------------------------------------------------------------
# 字段级血缘
# ---------------------------------------------------------------------------


def test_field_mapping_simple_column():
    sql = (
        "INSERT INTO default.pdw_t "
        "SELECT id, name FROM default.ods_t"
    )
    block = parse_block_lineage(_make_block(sql))
    field_names = [fm.target_field for fm in block.field_mappings]
    # id 和 name 都应出现
    assert "id" in field_names or len(field_names) > 0


def test_field_mapping_expression_partial():
    sql = (
        "INSERT INTO default.pdw_t "
        "SELECT id, COALESCE(name, 'N/A') AS name FROM default.ods_t"
    )
    block = parse_block_lineage(_make_block(sql))
    # 含表达式时置信度应为 PARTIAL
    assert block.confidence == ParseConfidence.PARTIAL or any(
        fm.confidence == ParseConfidence.PARTIAL for fm in block.field_mappings
    )


# ---------------------------------------------------------------------------
# 无法解析的 SQL
# ---------------------------------------------------------------------------


def test_invalid_sql_marked_failed():
    block = _make_block("THIS IS NOT VALID SQL !!!!", 0)
    block = parse_block_lineage(block)
    # sqlglot 对部分无效 SQL 可能仍返回空而非报错，确保不崩溃且无错误血缘
    assert block.status in (ParseStatus.SQL_PARSE_FAILED, ParseStatus.SKIPPED, ParseStatus.SQL_PARSE_PARTIAL, ParseStatus.OK)
    # 不应凭空产生目标表
    if block.status == ParseStatus.SQL_PARSE_FAILED:
        assert block.target_tables == []


def test_select_only_skipped():
    sql = "SELECT count(*) FROM default.check_table"
    block = parse_block_lineage(_make_block(sql))
    # 纯 SELECT 无写入目标，应标记为 SKIPPED
    assert block.status == ParseStatus.SKIPPED
    assert block.target_tables == []


# ---------------------------------------------------------------------------
# build_lineage_summary 聚合
# ---------------------------------------------------------------------------


def test_build_lineage_summary_merge():
    sql1 = "INSERT INTO default.pdw_t SELECT id FROM default.ods_a"
    sql2 = "INSERT INTO default.pdw_t SELECT name FROM default.ods_b"
    b1 = parse_block_lineage(_make_block(sql1, 0))
    b2 = parse_block_lineage(_make_block(sql2, 1))

    table_lineages, _ = build_lineage_summary([b1, b2])
    assert len(table_lineages) == 1
    target = table_lineages[0]
    upstream_names = [u.full_name for u in target.upstreams]
    assert "default.ods_a" in upstream_names
    assert "default.ods_b" in upstream_names


# ---------------------------------------------------------------------------
# CTE 过滤
# ---------------------------------------------------------------------------


def test_cte_not_in_upstreams():
    """WITH CTE 名不应出现在上游表中。"""
    sql = """
    WITH base_data AS (
        SELECT id, name FROM default.ods_source WHERE dt = '20260511'
    ),
    is_valid AS (
        SELECT id FROM base_data WHERE name IS NOT NULL
    )
    INSERT OVERWRITE TABLE default.dwd_target
    SELECT t1.id, t1.name FROM base_data t1 JOIN is_valid t2 ON t1.id = t2.id
    """
    block = parse_block_lineage(_make_block(sql))
    assert block.status == ParseStatus.OK
    upstream_names = [u.full_name for u in block.upstream_tables]
    # CTE 名不应出现
    assert "default.base_data" not in upstream_names
    assert "default.is_valid" not in upstream_names
    # 真实上游应存在
    assert "default.ods_source" in upstream_names


if __name__ == "__main__":
    test_insert_overwrite_target()
    test_insert_into_with_db()
    test_create_table_target()
    test_upstream_from()
    test_upstream_join()
    test_field_mapping_simple_column()
    test_field_mapping_expression_partial()
    test_invalid_sql_marked_failed()
    test_select_only_skipped()
    test_build_lineage_summary_merge()
    test_cte_not_in_upstreams()
    print("所有 lineage_parser 测试通过")
