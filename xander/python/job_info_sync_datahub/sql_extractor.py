"""sql_extractor — 从 ETL 脚本文本中提取 SQL blocks。

支持格式：
  - Shell heredoc：$HIVE <<EOF ... EOF  /  $HIVE -e <<EOF "..." EOF
  - Shell inline：$HIVE -e "..."  /  hive -e "..."
  - Python 三引号字符串

变量展开（两层）：
  1. 脚本内 ALL_CAPS="value" 单行赋值（如 TABLE_NAME="dw_order_info_v3_di"）
  2. analysis-jobs format_date.cnf 衍生变量（DATE_SUBxDAY / FDATE_* / MONTH* 等）
     —— 使用传入的 date_str（YYYYMMDD）计算，与集群行为一致

多行变量赋值（CHECK_DATA_SQL / ADD_PARTITION_SQL 等）中的 SQL 内容不作为血缘来源，
通过过滤 ${...} 引用语句避免其污染 sqlglot 解析。
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Dict, List, Optional

from .logging_utils import get_logger
from .models import ParseStatus, SqlBlock

logger = get_logger("sql_extractor")

# ---------------------------------------------------------------------------
# 正则模式
# ---------------------------------------------------------------------------

# VARNAME="value" 或 VARNAME='value'（单行大写赋值）
_SHELL_VAR_SINGLE_LINE_RE = re.compile(
    r"""^\s*([A-Z][A-Z0-9_]*)\s*=\s*["']([^"'\n]*)["']\s*$""",
    re.MULTILINE,
)

# $HIVE <<EOF ... EOF  /  $HIVE -e <<EOF "..." EOF（兼容有无 -e，<<-EOF，尾部引号，缩进 EOF）
_HEREDOC_RE = re.compile(
    r"""\$(?:HIVE|hive)\s+(?:-e\s+)?<<[-]?\s*(\w+)\s*"?\n(.*?)\n[ \t]*\1\b""",
    re.DOTALL,
)

# $HIVE -e "..." / hive -e "..."（inline，含无 $ 前缀的 shell_command 内联格式）
_INLINE_DOUBLE_RE = re.compile(
    r'\$?(?:HIVE|hive)\s+-e\s+"((?:[^"\\]|\\.)*)"',
    re.DOTALL,
)
_INLINE_SINGLE_RE = re.compile(
    r"\$?(?:HIVE|hive)\s+-e\s+'((?:[^'\\]|\\.)*)'",
    re.DOTALL,
)

# Python 三引号 SQL 块
_PY_TRIPLE_DOUBLE_RE = re.compile(r'"""(.*?)"""', re.DOTALL)
_PY_TRIPLE_SINGLE_RE = re.compile(r"'''(.*?)'''", re.DOTALL)

_SQL_KEYWORDS = re.compile(r"\b(SELECT|INSERT|CREATE|DROP|ALTER|WITH)\b", re.IGNORECASE)

# 匹配仅由未展开 shell 变量 + 可选注释 + 空白组成的伪语句
_UNRESOLVED_VAR_ONLY_RE = re.compile(
    r"^\s*(?:--[^\n]*)?\s*(?:\$\{[^}]+\}\s*)+\s*$",
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# Python 版 format_date.cnf — 从 DATE(YYYYMMDD) 计算所有衍生变量
# ---------------------------------------------------------------------------

def compute_format_date_vars(date_str: str) -> Dict[str, str]:
    """用 Python datetime 计算 analysis-jobs format_date.cnf 中的所有日期衍生变量。

    date_str: YYYYMMDD 格式字符串（对应集群 $DATE 变量，通常来自 DMP dt 字段）
    返回变量名 → 值的字典，全部为字符串。
    """
    try:
        base = date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
    except (ValueError, IndexError):
        logger.warning("compute_format_date_vars: 无法解析日期 %r，跳过日期变量展开", date_str)
        return {}

    def ymd(d: date) -> str:
        return d.strftime("%Y%m%d")

    def fdate(d: date) -> str:
        return d.strftime("%Y-%m-%d")

    def ym(d: date) -> str:
        return d.strftime("%Y%m")

    def fym(d: date) -> str:
        return d.strftime("%Y-%m")

    def add_months(d: date, months: int) -> date:
        m = d.month + months
        y = d.year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        import calendar
        day = min(d.day, calendar.monthrange(y, m)[1])
        return date(y, m, day)

    vars: Dict[str, str] = {
        "DATE": ymd(base),
        "FORMAT_DATE": fdate(base),
    }

    # DATE_SUBxDAY / DATE_ADDxDAY
    for n in [0,1,2,3,4,5,6,7,10,13,14,15,16,21,28,30,31,45,60,75,90,105,120,135,150,165,180,400]:
        vars[f"DATE_SUB{n}DAY"] = ymd(base - timedelta(days=n))
    for n in [1,2,3,4,5,6,7,8,9,15,30]:
        vars[f"DATE_ADD{n}DAY"] = ymd(base + timedelta(days=n))

    # FDATE_SUBxDAY / FDATE_ADDxDAY
    for n in [0,1,2,3,4,5,6,7,8,9,10,13,14,15,20,21,27,28,30,31,34,41,45,48,55,59,60,62,63,90,91,100,120,150,180]:
        vars[f"FDATE_SUB{n}DAY"] = fdate(base - timedelta(days=n))
    for n in [1,2,3,4,5,6,7,8,9,10,14,15,30,38,45,56]:
        vars[f"FDATE_ADD{n}DAY"] = fdate(base + timedelta(days=n))

    # MONTH / FMONTH 系列
    vars["MONTH"] = ym(base)
    vars["FMONTH"] = fym(base)
    for n in [1,2,3,4,5,6]:
        vars[f"DATE_SUB{n}MONTH"] = ymd(add_months(base, -n))
        vars[f"DATE_ADD{n}MONTH"] = ymd(add_months(base, n))
        vars[f"FDATE_SUB{n}MONTH"] = fdate(add_months(base, -n))
        vars[f"FDATE_ADD{n}MONTH"] = fdate(add_months(base, n))
        vars[f"MONTH_SUB{n}MONTH"] = ym(add_months(base, -n))
        vars[f"MONTH_ADD{n}MONTH"] = ym(add_months(base, n))
        vars[f"FMONTH_SUB{n}MONTH"] = fym(add_months(base, -n))
        vars[f"FMONTH_ADD{n}MONTH"] = fym(add_months(base, n))

    vars["DATE_SUB1YEAR"] = ymd(add_months(base, -12))
    vars["DATE_ADD1YEAR"] = ymd(add_months(base, 12))
    vars["FDATE_SUB1YEAR"] = fdate(add_months(base, -12))
    vars["FDATE_ADD1YEAR"] = fdate(add_months(base, 12))

    return vars


# ---------------------------------------------------------------------------
# 变量展开
# ---------------------------------------------------------------------------

def expand_shell_vars(text: str, date_str: Optional[str] = None) -> str:
    """两层变量展开：

    1. 脚本内 ALL_CAPS="value" 单行赋值
    2. format_date.cnf 衍生日期变量（若提供 date_str）

    优先处理较长变量名，避免短名称提前替换污染更长名称。
    """
    subst: Dict[str, str] = {}

    # 层 1：脚本内单行赋值
    for m in _SHELL_VAR_SINGLE_LINE_RE.finditer(text):
        k, v = m.group(1), m.group(2)
        if k and v is not None:
            subst[k] = v

    # 层 2：format_date.cnf 日期衍生变量（不覆盖层 1 中已有的同名变量）
    if date_str:
        date_vars = compute_format_date_vars(date_str)
        for k, v in date_vars.items():
            if k not in subst:
                subst[k] = v

    out = text
    for k in sorted(subst, key=len, reverse=True):
        v = subst[k]
        out = out.replace("${" + k + "}", v)
        out = out.replace("$" + k, v)
    return out


# ---------------------------------------------------------------------------
# SQL 文本清理
# ---------------------------------------------------------------------------

def _clean_sql_text(text: str) -> str:
    """清理从 heredoc / inline 提取的 SQL 文本：

    1. 去掉 $HIVE -e <<EOF "..." 格式遗留的尾部引号。
    2. 按 ; 拆分，过滤掉仅含未展开 shell 变量（${VAR}）的伪语句和纯注释语句。
    3. 将残余的 ${...} / $VAR 占位符替换为 SQL 字面量，避免 sqlglot tokenize 报错。
    """
    # 去掉末尾多余的 "（$HIVE -e <<EOF "..." 格式遗留）
    cleaned = text.strip().rstrip('"').strip()

    stmts = cleaned.split(";")
    kept: List[str] = []
    for stmt in stmts:
        s = stmt.strip()
        if not s:
            continue
        # 纯注释行，无 SQL 关键词，跳过
        if all(line.strip().startswith("--") or not line.strip() for line in s.splitlines()):
            continue
        # 仅由 ${VAR} 变量引用构成的伪语句（如 ${CHECK_DATA_SQL}），跳过
        if _UNRESOLVED_VAR_ONLY_RE.match(s):
            continue
        kept.append(s)

    result = ";\n".join(kept)

    # 残余 ${...} / $VARNAME → 合法 SQL 字面量占位符
    result = re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", "'__var__'", result)
    result = re.sub(r"\$[A-Za-z_][A-Za-z0-9_]*\b", "'__var__'", result)

    return result


def _is_sql_block(text: str) -> bool:
    return bool(_SQL_KEYWORDS.search(text))


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------

def extract_sql_blocks(
    etl_content: str,
    job_file_name: str = "",
    date_str: Optional[str] = None,
) -> List[SqlBlock]:
    """从 ETL 脚本文本中提取所有 SQL blocks，完成变量展开和清理。

    参数：
      etl_content   脚本原文
      job_file_name 文件名（用于判断 .py vs .job 提取策略，以及日志）
      date_str      调度业务日期 YYYYMMDD（来自 JobMetadata.dt），用于展开日期变量
    """
    expanded = expand_shell_vars(etl_content, date_str=date_str)
    raw_sqls: List[str] = []

    is_python = job_file_name.endswith(".py")

    if is_python:
        for pat in (_PY_TRIPLE_DOUBLE_RE, _PY_TRIPLE_SINGLE_RE):
            for m in pat.finditer(expanded):
                text = m.group(1).strip()
                if _is_sql_block(text):
                    raw_sqls.append(text)
    else:
        # heredoc 优先，再 inline
        for m in _HEREDOC_RE.finditer(expanded):
            text = m.group(2).strip()
            if _is_sql_block(text):
                raw_sqls.append(text)
        for pat in (_INLINE_DOUBLE_RE, _INLINE_SINGLE_RE):
            for m in pat.finditer(expanded):
                text = m.group(1).strip()
                if _is_sql_block(text):
                    raw_sqls.append(text)

    if not raw_sqls:
        logger.warning(
            "未从脚本中提取到 SQL blocks: file=%s is_python=%s",
            job_file_name,
            is_python,
        )

    blocks: List[SqlBlock] = []
    for i, sql in enumerate(raw_sqls):
        cleaned = _clean_sql_text(sql)
        if not cleaned:
            logger.debug("SQL block #%d 清理后为空，跳过", i)
            continue
        blocks.append(SqlBlock(index=i, raw_sql=cleaned, status=ParseStatus.OK))
        logger.debug(
            "SQL block #%d 提取完成，长度=%d（原始=%d）",
            i, len(cleaned), len(sql),
        )

    logger.info("共提取 %d 个 SQL block，文件=%s", len(blocks), job_file_name)
    return blocks
