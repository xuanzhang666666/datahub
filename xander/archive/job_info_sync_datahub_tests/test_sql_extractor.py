"""测试 sql_extractor：变量展开 + SQL block 提取。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from job_info_sync_datahub.sql_extractor import (
    compute_format_date_vars,
    expand_shell_vars,
    extract_sql_blocks,
)
from job_info_sync_datahub.models import ParseStatus


# ---------------------------------------------------------------------------
# expand_shell_vars
# ---------------------------------------------------------------------------


def test_expand_simple_var():
    text = 'TABLE_NAME="pdw_opc_flag_contact"\nINSERT INTO $TABLE_NAME SELECT 1'
    result = expand_shell_vars(text)
    assert "pdw_opc_flag_contact" in result
    assert "$TABLE_NAME" not in result


def test_expand_curly_var():
    text = 'TABLE_NAME="my_table"\nINSERT INTO ${TABLE_NAME} SELECT 1'
    result = expand_shell_vars(text)
    assert "my_table" in result
    assert "${TABLE_NAME}" not in result


def test_expand_longer_var_priority():
    """确保长变量名先展开，避免短名污染。"""
    text = 'TABLE_NAME="t1"\nTABLE_NAME_EXTRA="t2"\nSELECT $TABLE_NAME_EXTRA, $TABLE_NAME'
    result = expand_shell_vars(text)
    assert "t2" in result
    assert "t1" in result


def test_no_expand_lowercase():
    """小写变量不应展开。"""
    text = 'table_name="foo"\nINSERT INTO $table_name SELECT 1'
    result = expand_shell_vars(text)
    assert "$table_name" in result


# ---------------------------------------------------------------------------
# compute_format_date_vars
# ---------------------------------------------------------------------------


def test_compute_format_date_vars_basic():
    """DATE=20260511 应生成正确的衍生变量。"""
    v = compute_format_date_vars("20260511")
    assert v["DATE"] == "20260511"
    assert v["FORMAT_DATE"] == "2026-05-11"
    assert v["DATE_SUB1DAY"] == "20260510"
    assert v["FDATE_SUB1DAY"] == "2026-05-10"
    assert v["DATE_ADD1DAY"] == "20260512"
    assert v["MONTH"] == "202605"
    assert v["FMONTH"] == "2026-05"


def test_compute_format_date_vars_month_boundary():
    """跨月边界测试：2026-03-01 减 1 天应是 2026-02-28。"""
    v = compute_format_date_vars("20260301")
    assert v["DATE_SUB1DAY"] == "20260228"
    assert v["FDATE_SUB1DAY"] == "2026-02-28"


def test_expand_shell_vars_with_date():
    """传入 date_str 后，${DATE} 和 ${FDATE_SUB1DAY} 应被展开为真实值。"""
    sql = "WHERE dt = '${DATE}' AND prev_dt = '${FDATE_SUB1DAY}'"
    result = expand_shell_vars(sql, date_str="20260511")
    assert "'20260511'" in result
    assert "'2026-05-10'" in result
    assert "${DATE}" not in result
    assert "${FDATE_SUB1DAY}" not in result


def test_expand_shell_vars_date_overrides_script_var():
    """脚本内显式赋值 DATE="20200101" 优先于 date_str。"""
    text = 'DATE="20200101"\nWHERE dt = \'${DATE}\''
    result = expand_shell_vars(text, date_str="20260511")
    assert "'20200101'" in result


# ---------------------------------------------------------------------------
# extract_sql_blocks — .job 文件（heredoc）
# ---------------------------------------------------------------------------


_HEREDOC_JOB = """\
TABLE_NAME="pdw_test"
$HIVE <<EOF
INSERT OVERWRITE TABLE $TABLE_NAME
SELECT id, name FROM ods_test WHERE dt = '20240101'
EOF
"""


def test_extract_heredoc_block():
    blocks = extract_sql_blocks(_HEREDOC_JOB, "test.job")
    assert len(blocks) >= 1
    assert any("INSERT" in b.raw_sql.upper() for b in blocks)
    assert all(b.status == ParseStatus.OK for b in blocks)


_INLINE_JOB = """\
$HIVE -e "SELECT count(*) FROM ods_check"
"""


def test_extract_inline_block():
    blocks = extract_sql_blocks(_INLINE_JOB, "test.job")
    assert len(blocks) >= 1


# ---------------------------------------------------------------------------
# extract_sql_blocks — .py 文件（三引号）
# ---------------------------------------------------------------------------


_PY_SCRIPT = '''\
sql = """
INSERT INTO pdw_py_table
SELECT a, b FROM ods_py_source
"""
'''


def test_extract_python_triple_quote():
    blocks = extract_sql_blocks(_PY_SCRIPT, "test.py")
    assert len(blocks) >= 1
    assert any("pdw_py_table" in b.raw_sql for b in blocks)


def test_empty_script_returns_empty():
    blocks = extract_sql_blocks("# just a comment\necho hello", "test.job")
    assert blocks == []


if __name__ == "__main__":
    test_expand_simple_var()
    test_expand_curly_var()
    test_expand_longer_var_priority()
    test_no_expand_lowercase()
    test_compute_format_date_vars_basic()
    test_compute_format_date_vars_month_boundary()
    test_expand_shell_vars_with_date()
    test_expand_shell_vars_date_overrides_script_var()
    test_extract_heredoc_block()
    test_extract_inline_block()
    test_extract_python_triple_quote()
    test_empty_script_returns_empty()
    print("所有 sql_extractor 测试通过")
