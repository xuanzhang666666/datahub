#!/usr/bin/env python3
"""Generate dataset Documentation from DataHub structured ETL properties."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    __package__ = "job_info_sync_datahub"

from .field_lineage_datahub_reader import (
    extract_property_texts,
    fetch_structured_properties,
    make_hive_dataset_urn,
)
from .check_dataset_availability import normalize_upstream_table_ref
from .hive_fqtn_validation import HIVE_TABLE_LAYER_PREFIXES, table_name_has_layer_prefix
from .hive_table_existence import query_hive_existing_fqtns, should_skip_hive_existence_check
from .lineage_llm_compare import _prune_shell_job_to_entrypoint
from .llm_client import llm_user_message_max_chars
from .llm_client import call_openai_compatible_chat_text, get_llm_config, parse_llm_json_object
from .logging_utils import get_logger, setup_logging
from .query_upstream_lineage import (
    is_llm_generated_description,
    is_view_dataset,
    normalize_table_name,
)

logger = get_logger("table_documentation_from_dataset_props")

AUTO_DOC_START = "<!-- DATAHUB_AUTO_PROCESSING_DOC_START -->"
AUTO_DOC_END = "<!-- DATAHUB_AUTO_PROCESSING_DOC_END -->"
FAIL_DATAHUB_READ = "DATAHUB_READ"
FAIL_DDL = "DDL_ERROR"
FAIL_LLM = "LLM_ERROR"
FAIL_WRITE = "DATAHUB_WRITE"
_TRINO_U_ESCAPE_RE = re.compile(r"\\([0-9A-Fa-f]{4})")
_TRINO_COMMENT_U_AMP_RE = re.compile(r"COMMENT\s+U&'((?:[^'\\]|\\.)*?)'", re.IGNORECASE | re.DOTALL)
_BARE_HIVE_TABLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_DEFAULT_DB_QUALIFY_PREFIXES_TEXT = " / ".join(HIVE_TABLE_LAYER_PREFIXES)
_DATA_SOURCE_HIVE_FLAG_COLUMN = "是否 Hive 表"
_SECTION_4_HEADING_RE = re.compile(
    r"^\s{0,3}#{1,6}\s*4(?:\\?\.|[.、])\s*数据来源\s*$"
)
_SECTION_4_INLINE_RE = re.compile(r"4(?:\\?\.|[.、])\s*数据来源")
_SECTION_NEXT_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*\d+(?:\\?\.|[.、])\s+")

SYSTEM_PROMPT = f"""你是便利蜂数据仓库专家，擅长阅读 Hive SQL、Spark SQL、Python 和 shell 调度脚本。
你会收到一个 Hive 表的 Execute Shell、Etl Script 和完整 DDL。请直接输出 Markdown 文档，不要输出 JSON，不要使用额外解释。

Markdown 必须严格包含以下章节：

## 表加工逻辑说明

### 1. 表用途概览
简述该表产出的业务含义。

### 2. 表结构 DDL
用 sql 代码块原样放入目标表完整 DDL。

### 3. 调度入口
说明 Execute Shell 中实际启动命令、入口函数/脚本、关键参数。

### 4. 数据来源
必须使用下面的 Markdown 表格形式列出主要上游表及用途，表头固定为：
| 上游表 | 用途 |
| --- | --- |
上游表必须写 `库.表` 全名。脚本里已写 `库.表` 的按原样；脚本里未写库名前缀且表名以数仓层级前缀（{_DEFAULT_DB_QUALIFY_PREFIXES_TEXT}）开头的，写 `default.表名`；其它裸表名不要补库名，禁止根据 `use` 语句、同脚本其它引用或数仓规范推测库名。
禁止在表格外添加“可能为 xxx 库”“推测”“保留原样”等说明。
上游表不能省略表名后缀，不能把 `pdw_opc_flag_city_info` 写成 `pdw_opc_flag_city`。
SQL 中出现过的所有来源表都必须列出，包括 CTE 内引用过但最终 insert overwrite 未真实使用的来源表。
如果某个来源表只在未参与最终写入的 CTE / 临时逻辑中出现，也要保留在表格中，并在「用途」列标记“脚本中定义但最终写入未使用”。
`not_verified_` 开头的表是待校验结果表或临时结果表，不是业务计算上游，不要放入 4. 数据来源。

### 5. 使用到的上游表字段
必须输出 Markdown 表格，表头固定为：
| 上游表 | 字段 | 在本表加工中的用途 | 相关逻辑/表达式 |
| --- | --- | --- | --- |
无法确认字段时填“未明确”，不能编造字段。

### 6. 加工步骤
按执行顺序说明核心 SQL / Python 处理逻辑，包括 join、过滤、聚合、窗口函数、union、临时表流转。

### 7. 写入目标
说明目标表、分区字段、写入方式。

### 8. 关键口径
总结业务规则、过滤条件、字段口径、特殊分支。

### 9. 注意事项
说明动态表名、不确定逻辑、未能确认的入口或依赖。

要求：
- 只解释实际入口会执行的逻辑，不要解释 backup、历史废弃、未被入口调用的函数。
- 不要编造脚本中不存在的上游表、字段或业务口径。
- 未带库名前缀且表名以数仓层级前缀开头的上游表写 `default.表名`；其它裸表名不要补库名，禁止推测或解释库名归属。
- 内容用中文，适合 DataHub Documentation 页面直接展示。
"""


@dataclass(frozen=True)
class DocumentationSource:
    table_name: str
    dataset_urn: str
    execute_shell: str
    etl_script: str
    ddl: str
    ddl_error: Optional[str] = None


def load_table_names(path: str) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        normalized = normalize_table_name(line)
        if normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


_TERMINAL_BATCH_STATUSES = frozenset({"OK", "SKIP"})


def load_table_report_state(report_path: str) -> Dict[str, Dict[str, Any]]:
    """Load jsonl report; last row per table wins."""
    state: Dict[str, Dict[str, Any]] = {}
    path = Path(report_path)
    if not path.is_file():
        return state
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        table = row.get("table")
        if not isinstance(table, str) or not table.strip():
            continue
        state[normalize_table_name(table)] = row
    return state


def select_tables_for_batch(
    tables: List[str],
    *,
    resume: bool,
    report_path: str,
) -> tuple[List[str], Dict[str, int]]:
    """Return tables still to process. On resume, skip OK/SKIP (incl. DRY_RUN / WRITTEN)."""
    normalized = [normalize_table_name(t) for t in tables]
    total = len(normalized)
    if not resume:
        return normalized, {
            "total_in_file": total,
            "skipped_completed": 0,
            "pending": total,
        }

    completed = {
        t
        for t, row in load_table_report_state(report_path).items()
        if row.get("status") in _TERMINAL_BATCH_STATUSES
    }
    pending = [t for t in normalized if t not in completed]
    return pending, {
        "total_in_file": total,
        "skipped_completed": len(completed),
        "pending": len(pending),
    }


def merge_documentation(existing: str, generated: str, *, action: str) -> str:
    action = action.strip().lower()
    generated = generated.strip()
    if action == "overwrite":
        return generated
    if action != "append":
        raise ValueError("DOC_WRITE_ACTION 只能为 append 或 overwrite")

    block = f"{AUTO_DOC_START}\n{generated}\n{AUTO_DOC_END}"
    existing = (existing or "").strip()
    pattern = re.compile(
        re.escape(AUTO_DOC_START) + r"[\s\S]*?" + re.escape(AUTO_DOC_END),
        re.M,
    )
    if pattern.search(existing):
        return pattern.sub(block, existing).strip()
    if not existing:
        return block
    return f"{existing}\n\n{block}"


def _called_python_functions(func: ast.FunctionDef, function_names: Set[str]) -> Set[str]:
    return _function_calls_in_ast(func, function_names)


def _function_calls_in_ast(node: ast.AST, function_names: Set[str]) -> Set[str]:
    calls: Set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = ""
            if isinstance(child.func, ast.Name):
                name = child.func.id
            elif isinstance(child.func, ast.Attribute):
                name = child.func.attr
            if name in function_names:
                calls.add(name)
    return calls


def _is_dunder_main_guard(test: ast.AST) -> bool:
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False
    left, right = test.left, test.comparators[0]
    if not isinstance(left, ast.Name) or left.id != "__name__":
        return False
    if isinstance(right, ast.Constant):
        return right.value == "__main__"
    if hasattr(ast, "Str") and isinstance(right, ast.Str):
        return right.s == "__main__"
    return False


def _entrypoints_from_module_level(tree: ast.Module, function_names: Set[str]) -> List[str]:
    """Collect function entrypoints invoked from module-level code or ``if __name__ == '__main__'``."""
    found: List[str] = []
    for node in tree.body:
        blocks: List[ast.AST] = []
        if isinstance(node, ast.If) and _is_dunder_main_guard(node.test):
            blocks.extend(node.body)
            blocks.extend(node.orelse)
        elif not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            blocks.append(node)
        for block in blocks:
            for name in sorted(_function_calls_in_ast(block, function_names)):
                if name not in found:
                    found.append(name)
    return found


def _entrypoints_from_execute_shell(execute_shell: str, function_names: Set[str]) -> List[str]:
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", execute_shell or "")
    out: List[str] = []
    for token in tokens:
        if token in function_names and token not in out:
            out.append(token)
    return out


def prune_python_script_to_entrypoint(etl_script: str, execute_shell: str = "") -> str:
    try:
        tree = ast.parse(etl_script)
    except SyntaxError:
        return etl_script

    lines = etl_script.splitlines()
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and hasattr(node, "lineno") and hasattr(node, "end_lineno")
    }
    if not functions:
        return etl_script

    entrypoints: List[str] = []
    for candidate in (
        *_entrypoints_from_execute_shell(execute_shell, set(functions)),
        *_entrypoints_from_module_level(tree, set(functions)),
    ):
        if candidate not in entrypoints:
            entrypoints.append(candidate)
    if not entrypoints and "main" in functions:
        entrypoints = ["main"]
    if not entrypoints:
        return etl_script

    reachable: Set[str] = set()
    queue = list(entrypoints)
    while queue:
        name = queue.pop(0)
        if name in reachable or name not in functions:
            continue
        reachable.add(name)
        for called in sorted(_called_python_functions(functions[name], set(functions))):
            if called not in reachable:
                queue.append(called)

    function_ranges = [
        (node.lineno, node.end_lineno)
        for node in functions.values()
        if node.lineno is not None and node.end_lineno is not None
    ]
    top_level_lines = []
    for idx, line in enumerate(lines, start=1):
        if any(start <= idx <= end for start, end in function_ranges):
            continue
        if line.strip():
            top_level_lines.append(line)

    kept = [
        "\n".join(lines[node.lineno - 1 : node.end_lineno]).strip()
        for name, node in functions.items()
        if name in reachable
    ]
    if not kept:
        return etl_script
    header = (
        f"# DataHub documentation parser: only Python functions reachable from "
        f"entrypoint(s) {', '.join(entrypoints)} are included.\n"
        "# Unreachable functions are omitted to avoid parsing backup/dead code."
    )
    parts = [header]
    if top_level_lines:
        parts.append("\n".join(top_level_lines).strip())
    parts.extend(kept)
    return "\n\n".join(p for p in parts if p.strip())


def _looks_like_python(script: str) -> bool:
    if not script.strip():
        return False
    try:
        tree = ast.parse(script)
    except SyntaxError:
        return False
    return any(isinstance(node, (ast.FunctionDef, ast.Import, ast.ImportFrom, ast.ClassDef)) for node in tree.body)


def prune_etl_for_prompt(etl_script: str, execute_shell: str, table_name: str) -> str:
    if _looks_like_python(etl_script):
        return prune_python_script_to_entrypoint(etl_script, execute_shell)
    return _prune_shell_job_to_entrypoint(etl_script, f"{table_name}.job")


def strip_commented_logic_for_prompt(script: str) -> str:
    without_block_comments = re.sub(r"/\*[\s\S]*?\*/", "", script)
    cleaned_lines: List[str] = []
    for raw_line in without_block_comments.splitlines():
        stripped = raw_line.lstrip()
        if stripped.startswith("--") or stripped.startswith("#"):
            continue
        cleaned = re.sub(r"\s+--.*$", "", raw_line.rstrip())
        cleaned_lines.append(cleaned)
    return "\n".join(cleaned_lines)


def expand_prompt_variables(script: str) -> str:
    """Expand simple shell constants before asking the LLM to document table names."""
    assignments: Dict[str, str] = {}
    assignment_re = re.compile(
        r"""^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(?:"([^"\n]*)"|'([^'\n]*)'|([A-Za-z0-9_./-]+))\s*$"""
    )

    def replace_vars(value: str) -> str:
        def repl(match: re.Match[str]) -> str:
            name = match.group(1) or match.group(2)
            return assignments.get(name, match.group(0))

        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", repl, value)

    def normalize_assignment_value(value: str) -> str:
        expanded = replace_vars(value)
        if re.match(r"^[A-Za-z][A-Za-z0-9_]*_dev$", expanded):
            return expanded.removesuffix("_dev")
        return expanded

    for raw_line in script.splitlines():
        match = assignment_re.match(raw_line.strip())
        if not match:
            continue
        name = match.group(1)
        value = next(group for group in match.groups()[1:] if group is not None)
        assignments[name] = normalize_assignment_value(value)

    return replace_vars(script) if assignments else script


def expand_generated_markdown_variables(markdown: str, etl_script: str) -> str:
    """Apply ETL variable expansion to generated Markdown as a final guardrail."""
    expanded_etl = expand_prompt_variables(etl_script)
    assignments: Dict[str, str] = {}
    assignment_re = re.compile(
        r"""^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(?:"([^"\n]*)"|'([^'\n]*)'|([A-Za-z0-9_./-]+))\s*$"""
    )
    for raw_line in expanded_etl.splitlines():
        match = assignment_re.match(raw_line.strip())
        if not match:
            continue
        name = match.group(1)
        value = next(group for group in match.groups()[1:] if group is not None)
        assignments[name] = value
    if not assignments:
        return markdown

    def repl(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return assignments.get(name, match.group(0))

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", repl, markdown)


def documentation_default_database(etl_script: str = "") -> str:
    """未带库名前缀的上游表统一补 ``default``，不根据 ``use`` 语句或其它引用推测库名。"""
    del etl_script
    return "default"


def _qualify_table_cell(cell: str, default_database: str) -> str:
    stripped = cell.strip()
    if not stripped or "." in stripped or stripped in {"---", "上游表"}:
        return cell

    prefix = ""
    suffix = ""
    token = stripped
    if token.startswith("`") and token.endswith("`") and len(token) >= 2:
        prefix = "`"
        suffix = "`"
        token = token[1:-1].strip()
    if "." in token or not _BARE_HIVE_TABLE_RE.match(token):
        return cell
    if not table_name_has_layer_prefix(token):
        return cell
    return cell.replace(stripped, f"{prefix}{default_database}.{token}{suffix}", 1)


def qualify_unqualified_markdown_table_names(markdown: str, default_database: str) -> str:
    """Qualify bare upstream table names in generated Markdown table first columns."""
    if not default_database:
        return markdown

    lines: List[str] = []
    for line in markdown.splitlines():
        if not line.lstrip().startswith("|"):
            lines.append(line)
            continue
        cells = line.split("|")
        if len(cells) < 4:
            lines.append(line)
            continue
        first_data_cell = cells[1]
        qualified = _qualify_table_cell(first_data_cell, default_database)
        if qualified != first_data_cell:
            cells[1] = qualified
            line = "|".join(cells)
        lines.append(line)
    return "\n".join(lines)


_DB_SPECULATION_LINE_RE = re.compile(
    r"(推测|可能为|保留原样|根据现有数仓规范|实际库名可能|以脚本中出现的原始表名为准|"
    r"未带库名前缀.*根据|根据脚本解析规则.*保留)"
)


def strip_database_speculation_disclaimers(markdown: str) -> str:
    """Remove LLM prose lines that speculate about upstream database names."""
    kept: List[str] = []
    for line in markdown.splitlines():
        if _DB_SPECULATION_LINE_RE.search(line):
            continue
        kept.append(line)
    return "\n".join(kept)


def _data_source_section_line_range(lines: List[str]) -> Optional[tuple[int, int]]:
    start: Optional[int] = None
    for idx, line in enumerate(lines):
        if _SECTION_4_HEADING_RE.match(line.strip()):
            start = idx
            break
    if start is None:
        for idx, line in enumerate(lines):
            if _SECTION_4_INLINE_RE.search(line):
                start = idx
                break
    if start is None:
        return None
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        if _SECTION_NEXT_HEADING_RE.match(lines[idx].strip()):
            end = idx
            break
    return start, end


def _split_markdown_table_cells(line: str) -> Optional[List[str]]:
    if not line.lstrip().startswith("|"):
        return None
    parts = line.split("|")
    if len(parts) < 3:
        return None
    return [part.strip() for part in parts[1:-1]]


def _format_markdown_table_row(cells: List[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _is_markdown_separator_row(cells: List[str]) -> bool:
    if not cells:
        return False
    return all(set(cell.strip()) <= {"-", " "} for cell in cells)


def _is_upstream_table_header_row(cells: List[str]) -> bool:
    if not cells:
        return False
    first = cells[0].strip().strip("`").lower()
    return first in {"上游表", "upstream"}


def _header_hive_flag_column_index(cells: List[str]) -> Optional[int]:
    for idx, cell in enumerate(cells):
        if cell.strip() == _DATA_SOURCE_HIVE_FLAG_COLUMN:
            return idx
    return None


def _hive_existence_label(
    fqtn: Optional[str],
    *,
    existing: Optional[Set[str]],
    check_skipped: bool,
    check_failed: bool,
) -> str:
    if not fqtn:
        return "-"
    if check_skipped:
        return "-"
    if check_failed or existing is None:
        return "未校验"
    return "是" if fqtn.lower() in existing else "否"


def annotate_data_source_hive_existence(markdown: str) -> str:
    """在「4. 数据来源」Markdown 表格中增加/更新「是否 Hive 表」列（Trino information_schema）。"""
    lines = markdown.splitlines()
    bounds = _data_source_section_line_range(lines)
    if bounds is None:
        return markdown

    start, end = bounds
    section = lines[start:end]
    fqtns: Set[str] = set()
    row_fqtns: List[Optional[str]] = []
    for line in section:
        cells = _split_markdown_table_cells(line)
        if cells is None or _is_markdown_separator_row(cells) or _is_upstream_table_header_row(cells):
            continue
        fqtn = normalize_upstream_table_ref(cells[0])
        row_fqtns.append(fqtn or None)
        if fqtn:
            fqtns.add(fqtn)

    check_skipped = should_skip_hive_existence_check()
    existing: Optional[Set[str]] = set()
    check_failed = False
    if fqtns and not check_skipped:
        try:
            existing = query_hive_existing_fqtns(fqtns)
        except Exception as exc:
            logger.warning("Hive 存在性标注失败，「是否 Hive 表」列填未校验: %s", exc)
            existing = None
            check_failed = True

    new_section: List[str] = []
    data_row_idx = 0
    for line in section:
        cells = _split_markdown_table_cells(line)
        if cells is None:
            new_section.append(line)
            continue

        if _is_markdown_separator_row(cells):
            flag_idx = _header_hive_flag_column_index(cells)
            if flag_idx is None:
                cells = [*cells, "---"]
            else:
                cells[flag_idx] = "---"
            new_section.append(_format_markdown_table_row(cells))
            continue

        if _is_upstream_table_header_row(cells):
            flag_idx = _header_hive_flag_column_index(cells)
            if flag_idx is None:
                cells = [*cells, _DATA_SOURCE_HIVE_FLAG_COLUMN]
            else:
                cells[flag_idx] = _DATA_SOURCE_HIVE_FLAG_COLUMN
            new_section.append(_format_markdown_table_row(cells))
            continue

        fqtn = row_fqtns[data_row_idx] if data_row_idx < len(row_fqtns) else None
        data_row_idx += 1
        label = _hive_existence_label(
            fqtn,
            existing=existing,
            check_skipped=check_skipped,
            check_failed=check_failed,
        )
        flag_idx = _header_hive_flag_column_index(cells)
        if flag_idx is None:
            cells = [*cells, label]
        else:
            cells[flag_idx] = label
        new_section.append(_format_markdown_table_row(cells))

    merged = lines[:start] + new_section + lines[end:]
    return "\n".join(merged)


def build_llm_user_message(
    *,
    table_name: str,
    dataset_urn: str,
    execute_shell: str,
    etl_script: str,
    ddl: str,
    max_chars: Optional[int] = None,
) -> str:
    limit = llm_user_message_max_chars(
        system_prompt_chars=len(SYSTEM_PROMPT),
        explicit_max_chars=max_chars,
    )
    pruned_etl = strip_commented_logic_for_prompt(
        expand_prompt_variables(prune_etl_for_prompt(etl_script, execute_shell, table_name))
    )
    body = f"""目标表：{table_name}
Dataset URN：{dataset_urn}
表名库名解析规则（必须严格遵守，禁止推测）：
1. 脚本里已写 `库.表` 的，Documentation 中按原样写 `库.表`。
2. 脚本里未写库名前缀且表名以数仓层级前缀（{_DEFAULT_DB_QUALIFY_PREFIXES_TEXT}）开头的，写 `default.表名`。
3. 其它裸表名不要补库名，不要写入 4. 数据来源。
4. 禁止根据 `use` 语句、同脚本其它带库名引用、目标表所在库或数仓规范推测库名。
5. 禁止在表格外写“可能为 xxx 库”“推测”“保留原样”等说明性文字。

## Execute Shell
```shell
{execute_shell.strip() or "无"}
```

## Etl Script
```text
{pruned_etl.strip() or "无"}
```

## 目标表完整 DDL
```sql
{ddl.strip() or "未获取到 DDL"}
```

请基于以上内容生成 Markdown。必须包含：
- ### 2. 表结构 DDL
- ### 4. 数据来源
- 数据来源必须使用 Markdown 表格，表头固定为：| 上游表 | 用途 |（「是否 Hive 表」列由系统根据 Trino 自动填写，无需生成）
- 数据来源表格第二行固定为：| --- | --- |
- 数据来源里的上游表必须写 `库.表` 全名；未带库名且表名以层级前缀开头的写 `default.表名`，其它裸表名不要补库名。
- 禁止推测库名，禁止在表格外解释库名归属。
- SQL 中出现过的所有来源表都必须列出，不能因为最终 insert overwrite 未引用对应 CTE 就隐去。
- 对只在未参与最终写入的 CTE / 临时逻辑中出现的来源表，在用途列标记“脚本中定义但最终写入未使用”。
- `not_verified_` 开头的待校验结果表不是业务计算上游，不要放入 4. 数据来源。
- ### 5. 使用到的上游表字段
- Markdown 表格表头：| 上游表 | 字段 | 在本表加工中的用途 | 相关逻辑/表达式 |
- 无法确认字段时填“未明确”，不能编造字段。
"""
    if len(body) > limit:
        body = body[:limit] + "\n... [truncated]"
    return body


def call_llm_generate_documentation(
    source: DocumentationSource,
    *,
    timeout_sec: int,
) -> Dict[str, Any]:
    cfg = get_llm_config()
    user_message = build_llm_user_message(
        table_name=source.table_name,
        dataset_urn=source.dataset_urn,
        execute_shell=source.execute_shell,
        etl_script=source.etl_script,
        ddl=source.ddl,
    )
    return call_openai_compatible_chat_text(
        cfg,
        system_prompt=SYSTEM_PROMPT,
        user_message=user_message,
        timeout_sec=timeout_sec,
    )


def markdown_from_llm_payload(payload: Dict[str, Any]) -> str:
    markdown = payload.get("markdown")
    if not isinstance(markdown, str):
        content = payload.get("content")
        if isinstance(content, str):
            try:
                parsed = parse_llm_json_object(content)
                markdown = parsed.get("markdown")
            except Exception:
                markdown = content
    if not isinstance(markdown, str) or not markdown.strip():
        raise RuntimeError("LLM 返回缺少 markdown 字段")
    return markdown.strip()


def decode_trino_u_string(body: str) -> str:
    def repl(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 16))

    return _TRINO_U_ESCAPE_RE.sub(repl, body)


def decode_trino_ddl_unicode_comments(ddl: str) -> str:
    def sub_comment(match: re.Match[str]) -> str:
        decoded = decode_trino_u_string(match.group(1))
        return "COMMENT '" + decoded.replace("'", "''") + "'"

    return _TRINO_COMMENT_U_AMP_RE.sub(sub_comment, ddl)


def fetch_table_ddl(table_name: str) -> str:
    try:
        import trino
    except ImportError as exc:
        raise RuntimeError("缺少 trino Python 依赖，无法查询 DDL") from exc

    host = os.getenv("TRINO_HOST", "10.253.7.167")
    port = int(os.getenv("TRINO_PORT", "8081"))
    user = os.getenv("TRINO_USER", os.getenv("USER", "datahub"))
    catalog = os.getenv("TRINO_CATALOG", "hive")
    schema = os.getenv("TRINO_SCHEMA", "default")
    conn = trino.dbapi.connect(
        host=host,
        port=port,
        user=user,
        catalog=catalog,
        schema=schema,
    )
    cur = conn.cursor()
    cur.execute(f"SHOW CREATE TABLE {table_name}")
    rows = cur.fetchall()
    if not rows:
        return ""
    value = rows[0][0]
    ddl = value if isinstance(value, str) else str(value)
    return decode_trino_ddl_unicode_comments(ddl)



def _export_text(output_dir: Optional[str], subdir: str, table_name: str, suffix: str, content: str) -> Optional[str]:
    if not output_dir:
        return None
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", table_name)
    path = Path(output_dir) / subdir / f"{safe}.{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


def _export_json(output_dir: Optional[str], subdir: str, table_name: str, suffix: str, payload: Dict[str, Any]) -> Optional[str]:
    return _export_text(
        output_dir,
        subdir,
        table_name,
        suffix,
        json.dumps(payload, ensure_ascii=False, indent=2),
    )


def fetch_existing_editable_description(
    gms_url: str,
    dataset_urn: str,
    token: Optional[str],
) -> str:
    try:
        from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
        from datahub.metadata.schema_classes import EditableDatasetPropertiesClass
    except ImportError as exc:
        raise RuntimeError("需要安装 acryl-datahub 才能读取 Documentation") from exc

    graph = DataHubGraph(DatahubClientConfig(server=gms_url.rstrip("/"), token=token))
    aspect = graph.get_aspect(entity_urn=dataset_urn, aspect_type=EditableDatasetPropertiesClass)
    description = getattr(aspect, "description", "") if aspect else ""
    return description if isinstance(description, str) else ""


def with_updated_editable_description(
    existing_aspect: Optional[Any],
    description: str,
    aspect_cls: Any,
) -> Any:
    aspect = existing_aspect if existing_aspect is not None else aspect_cls()
    setattr(aspect, "description", description)
    return aspect


def write_editable_description(
    gms_url: str,
    dataset_urn: str,
    description: str,
    token: Optional[str],
) -> None:
    try:
        from datahub.emitter.mcp import MetadataChangeProposalWrapper
        from datahub.emitter.rest_emitter import DatahubRestEmitter
        from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
        from datahub.metadata.schema_classes import EditableDatasetPropertiesClass
    except ImportError as exc:
        raise RuntimeError("需要安装 acryl-datahub 才能写入 Documentation") from exc

    graph = DataHubGraph(DatahubClientConfig(server=gms_url.rstrip("/"), token=token))
    existing_aspect = graph.get_aspect(entity_urn=dataset_urn, aspect_type=EditableDatasetPropertiesClass)
    aspect = with_updated_editable_description(existing_aspect, description, EditableDatasetPropertiesClass)
    emitter = DatahubRestEmitter(gms_url.rstrip("/"), token=token)
    emitter.emit_mcp(MetadataChangeProposalWrapper(entityUrn=dataset_urn, aspect=aspect))


def _base_result(table_name: str, dataset_urn: str) -> Dict[str, Any]:
    return {
        "table": table_name,
        "dataset_urn": dataset_urn,
        "ts": datetime.now(timezone.utc).isoformat(),
        "status": "OK",
        "fail_category": None,
        "error": None,
        "documentation_status": None,
        "documentation_reason": None,
        "write_documentation": None,
        "doc_write_action": None,
        "ddl_error": None,
        "markdown_export_path": None,
        "ddl_export_path": None,
        "llm_raw_export_path": None,
        "elapsed": None,
    }


def sync_one_table_documentation(
    table_name: str,
    *,
    gms_url: str,
    token: Optional[str],
    platform_instance: str,
    env: str,
    dry_run: bool,
    action: str,
    llm_timeout_sec: int,
    output_dir: Optional[str],
    skip_if_llm_doc_exists: bool = False,
) -> Dict[str, Any]:
    input_table = normalize_table_name(table_name)
    dataset_urn = make_hive_dataset_urn(input_table, platform_instance, env)
    result = _base_result(input_table, dataset_urn)
    result["doc_write_action"] = action
    t0 = time.time()

    try:
        if is_view_dataset(gms_url, token, dataset_urn):
            result.update(
                status="SKIP",
                documentation_status="SKIP_VIEW_DATASET",
                documentation_reason="目标 dataset 是 view，Documentation 生成仅处理 table",
                write_documentation=False,
            )
            return result

        existing = fetch_existing_editable_description(gms_url, dataset_urn, token)
        if skip_if_llm_doc_exists and is_llm_generated_description(existing):
            result.update(
                status="SKIP",
                documentation_status="SKIP_LLM_DOC_EXISTS",
                documentation_reason="Documentation 已含 LLM 生成内容，跳过写入",
                write_documentation=False,
                elapsed=round(time.time() - t0, 1),
            )
            return result

        payload = fetch_structured_properties(gms_url, dataset_urn, token=token)
        etl_script, execute_shell = extract_property_texts(payload)
    except Exception as exc:
        result.update(
            status="FAIL",
            fail_category=FAIL_DATAHUB_READ,
            error=str(exc)[:500],
            documentation_status="DATAHUB_READ_ERROR",
            write_documentation=False,
        )
        return result

    ddl = ""
    try:
        ddl = fetch_table_ddl(input_table)
        result["ddl_export_path"] = _export_text(output_dir, "ddl", input_table, "sql", ddl)
    except Exception as exc:
        result["ddl_error"] = str(exc)[:500]
        logger.warning("DDL 查询失败: table=%s err=%s", input_table, exc)

    if not etl_script.strip() and not execute_shell.strip() and not ddl.strip():
        result.update(
            status="SKIP",
            documentation_status="SKIP_NO_DOC_SOURCE",
            documentation_reason="Etl Script / Execute Shell / DDL 均为空或不可用",
            write_documentation=False,
        )
        return result

    source = DocumentationSource(
        table_name=input_table,
        dataset_urn=dataset_urn,
        execute_shell=execute_shell,
        etl_script=etl_script,
        ddl=ddl,
        ddl_error=result.get("ddl_error"),
    )

    try:
        llm_raw = call_llm_generate_documentation(source, timeout_sec=llm_timeout_sec)
        result["llm_raw_export_path"] = _export_json(output_dir, "llm_raw", input_table, "json", llm_raw)
        markdown = expand_generated_markdown_variables(markdown_from_llm_payload(llm_raw), etl_script)
        markdown = qualify_unqualified_markdown_table_names(
            markdown,
            documentation_default_database(etl_script),
        )
        markdown = strip_database_speculation_disclaimers(markdown)
        markdown = annotate_data_source_hive_existence(markdown)
        final_doc = merge_documentation(existing, markdown, action=action)
        result["markdown_export_path"] = _export_text(output_dir, "markdown", input_table, "md", final_doc)
    except Exception as exc:
        result.update(
            status="FAIL",
            fail_category=FAIL_LLM,
            error=str(exc)[:500],
            documentation_status="LLM_ERROR",
            write_documentation=False,
        )
        return result

    if dry_run:
        result.update(
            status="OK",
            documentation_status="DRY_RUN",
            documentation_reason="已生成 Markdown 预览，未写入 DataHub",
            write_documentation=False,
            elapsed=round(time.time() - t0, 1),
        )
        return result

    try:
        write_editable_description(gms_url, dataset_urn, final_doc, token)
    except Exception as exc:
        result.update(
            status="FAIL",
            fail_category=FAIL_WRITE,
            error=str(exc)[:500],
            documentation_status="DATAHUB_WRITE_ERROR",
            write_documentation=False,
        )
        return result

    result.update(
        status="OK",
        documentation_status="WRITTEN",
        documentation_reason="Documentation 已写入 editableDatasetProperties.description",
        write_documentation=True,
        elapsed=round(time.time() - t0, 1),
    )
    return result


def _write_jsonl(path: str, row: Dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def run_batch(args: argparse.Namespace) -> int:
    setup_logging()
    all_tables = load_table_names(args.table_file)
    tables, resume_stats = select_tables_for_batch(
        all_tables,
        resume=args.resume,
        report_path=args.report,
    )
    if args.resume:
        logger.info(
            "断点续跑: 名单 %d 张，已完成跳过 %d 张，待处理 %d 张（报告 %s）",
            resume_stats["total_in_file"],
            resume_stats["skipped_completed"],
            resume_stats["pending"],
            args.report,
        )
    if not tables:
        print("\n" + "=" * 60)
        print("没有待处理表（断点续跑：名单内表均已在报告中为 OK/SKIP）")
        return 0
    logger.info(
        "待处理表: %d dry_run=%s action=%s skip_if_llm_doc_exists=%s "
        "max_consecutive_llm_failures=%d resume=%s",
        len(tables),
        args.dry_run,
        args.action,
        args.skip_if_llm_doc_exists,
        args.max_consecutive_llm_failures,
        args.resume,
    )
    output_dir = str(Path(args.report).parent)

    ok = skip = fail = 0
    consecutive_llm_failures = 0
    aborted_reason: Optional[str] = None
    rows: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(args.concurrency, 1)) as pool:
        futures = {
            pool.submit(
                sync_one_table_documentation,
                table,
                gms_url=args.datahub_gms,
                token=args.token,
                platform_instance=args.platform_instance,
                env=args.env,
                dry_run=args.dry_run,
                action=args.action,
                llm_timeout_sec=args.llm_timeout,
                output_dir=output_dir,
                skip_if_llm_doc_exists=args.skip_if_llm_doc_exists,
            ): table
            for table in tables
        }
        for idx, fut in enumerate(as_completed(futures), start=1):
            row = fut.result()
            rows.append(row)
            _write_jsonl(args.report, row)
            status = row.get("status")
            if status == "OK":
                ok += 1
            elif status == "SKIP":
                skip += 1
            else:
                fail += 1
            if row.get("fail_category") == FAIL_LLM:
                consecutive_llm_failures += 1
            else:
                consecutive_llm_failures = 0
            logger.info(
                "[PROGRESS] %d/%d table=%s status=%s doc=%s write=%s OK=%d SKIP=%d FAIL=%d llm_fail_streak=%d markdown=%s",
                idx,
                len(tables),
                row.get("table"),
                status,
                row.get("documentation_status"),
                row.get("write_documentation"),
                ok,
                skip,
                fail,
                consecutive_llm_failures,
                row.get("markdown_export_path") or "-",
            )
            if (
                args.max_consecutive_llm_failures > 0
                and consecutive_llm_failures >= args.max_consecutive_llm_failures
            ):
                aborted_reason = (
                    f"连续 {consecutive_llm_failures} 个表调用 LLM 失败，停止后续 Documentation 生成"
                )
                logger.error(aborted_reason)
                for pending in futures:
                    if not pending.done():
                        pending.cancel()
                break

    print(f"\n{'=' * 60}")
    print(f"总计: {len(rows)}  OK: {ok}  SKIP: {skip}  FAIL: {fail}")
    if aborted_reason:
        print(f"ABORTED: {aborted_reason}")
    for row in rows:
        print(
            f"  [{row.get('status')}] table={row.get('table')} "
            f"doc={row.get('documentation_status')} markdown={row.get('markdown_export_path') or '-'}"
        )
        if row.get("error"):
            print(f"      error={row.get('error')}")
        if row.get("ddl_error"):
            print(f"      ddl_error={row.get('ddl_error')}")
    if aborted_reason:
        return 2
    return 0 if fail == 0 else 1


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--table-file", required=True)
    p.add_argument("--report", required=True)
    p.add_argument(
        "--resume",
        action="store_true",
        default=os.getenv("RESUME", "").strip() in ("1", "true", "yes"),
        help="断点续跑：跳过报告中 status 为 OK/SKIP 的表（环境变量 RESUME=1）",
    )
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--action", choices=["append", "overwrite"], default=os.getenv("DOC_WRITE_ACTION", "append"))
    p.add_argument(
        "--skip-if-llm-doc-exists",
        action="store_true",
        default=os.getenv("SKIP_IF_LLM_DOC_EXISTS", "").strip().lower() in ("1", "true", "yes", "on"),
        help="Documentation 已含 LLM 生成内容时跳过（不调用 LLM、不写入；环境变量 SKIP_IF_LLM_DOC_EXISTS=1）",
    )
    p.add_argument("--llm-timeout", type=int, default=int(os.getenv("LLM_TIMEOUT", "90")))
    p.add_argument(
        "--max-consecutive-llm-failures",
        type=int,
        default=int(os.getenv("MAX_CONSECUTIVE_LLM_FAILURES", "3")),
        help="连续多少个 LLM_ERROR 后停止；<=0 表示不启用",
    )
    p.add_argument("--datahub-gms", default=os.getenv("DATAHUB_GMS_URL", "http://localhost:8080"))
    p.add_argument("--token", default=os.getenv("DATAHUB_GMS_TOKEN"))
    p.add_argument("--platform-instance", default=os.getenv("BLF_DATAHUB_PLATFORM_INSTANCE", "blf-prod-hive"))
    p.add_argument("--env", default=os.getenv("DATAHUB_ENV", "PROD"))
    return p.parse_args()


def main() -> int:
    return run_batch(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
