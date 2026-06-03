"""Conservative Sqoop import handling for MySQL -> Hive jobs."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Dict, Optional

from .models import TableRef

_ASSIGN_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.+?)\s*$")
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


@dataclass
class SqoopImportInfo:
    target: TableRef
    source_table: str
    source_connect: str = ""
    columns: list[str] = field(default_factory=list)
    raw_command: str = ""

    @property
    def source_display(self) -> str:
        if self.source_connect and self.source_table:
            return f"{self.source_connect}.{self.source_table}"
        return self.source_table or self.source_connect


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def _extract_assignments(script: str) -> Dict[str, str]:
    assignments: Dict[str, str] = {}
    for raw_line in script.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("function "):
            continue
        match = _ASSIGN_RE.match(line)
        if not match:
            continue
        value = _strip_quotes(match.group(2))
        if "$(" in value or "`" in value:
            continue
        assignments[match.group(1)] = value
    return assignments


def _expand_vars(value: str, assignments: Dict[str, str]) -> str:
    rendered = value
    for _ in range(8):
        changed = False

        def repl(match: re.Match[str]) -> str:
            nonlocal changed
            name = match.group(1) or match.group(2)
            replacement = assignments.get(name)
            if replacement is None:
                return match.group(0)
            changed = True
            return replacement

        rendered = _VAR_RE.sub(repl, rendered)
        if not changed:
            break
    return _strip_quotes(rendered)


def _logical_shell_lines(script: str) -> list[str]:
    joined = re.sub(r"\\\s*\n", " ", script)
    return [line.strip() for line in joined.splitlines() if line.strip()]


def _parse_options(tokens: list[str]) -> Dict[str, str]:
    options: Dict[str, str] = {}
    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        if not token.startswith("--"):
            idx += 1
            continue
        if "=" in token:
            key, value = token.split("=", 1)
            options[key] = value
            idx += 1
            continue
        if idx + 1 < len(tokens) and not tokens[idx + 1].startswith("--"):
            options[token] = tokens[idx + 1]
            idx += 2
            continue
        options[token] = ""
        idx += 1
    return options


def _find_sqoop_command(script: str) -> Optional[str]:
    for line in _logical_shell_lines(script):
        lower = line.lower()
        if " import " not in f" {lower} ":
            continue
        if "--hcatalog-table" not in lower:
            continue
        if "sqoop" in lower or "${sqoop}" in lower or "$sqoop" in lower:
            return line
    return None


def detect_sqoop_import(script: str) -> Optional[SqoopImportInfo]:
    command = _find_sqoop_command(script)
    if not command:
        return None

    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    options = _parse_options(tokens)
    assignments = _extract_assignments(script)

    target_table = _expand_vars(options.get("--hcatalog-table", ""), assignments)
    if not target_table:
        return None
    target_db = _expand_vars(options.get("--hcatalog-database", ""), assignments) or "default"
    source_table = _expand_vars(options.get("--table", ""), assignments)
    source_connect = _expand_vars(options.get("--connect", ""), assignments)
    columns_raw = _expand_vars(options.get("--columns", ""), assignments)
    columns = [col.strip() for col in columns_raw.split(",") if col.strip()]

    return SqoopImportInfo(
        target=TableRef(target_db, target_table),
        source_table=source_table,
        source_connect=source_connect,
        columns=columns,
        raw_command=command,
    )


def build_sqoop_documentation(info: SqoopImportInfo) -> str:
    columns = ", ".join(f"`{col}`" for col in info.columns) if info.columns else "未明确"
    source_connect = f"`{info.source_connect}`" if info.source_connect else "未明确"
    source_table = f"`{info.source_table}`" if info.source_table else "未明确"
    return f"""## Sqoop MySQL 同步来源说明

| 项 | 值 |
| --- | --- |
| Hive 目标表 | `{info.target.full_name}` |
| MySQL/JDBC 来源 | {source_connect} |
| MySQL 源表 | {source_table} |
| 同步字段 | {columns} |
| 写入方式 | `sqoop import --hcatalog-database {info.target.db} --hcatalog-table {info.target.table}` |

> 说明：该作业是 MySQL -> Hive 的 Sqoop 导入。当前保守方案只识别 Hive 目标表，并把 MySQL 来源写入 Documentation / 审计报告，不写 DataHub upstreamLineage。
"""


def sqoop_audit_extra(info: SqoopImportInfo) -> Dict[str, object]:
    return {
        "sqoop_detected": True,
        "sqoop_target": info.target.full_name,
        "sqoop_source_connect": info.source_connect,
        "sqoop_source_table": info.source_table,
        "sqoop_columns": info.columns,
        "sqoop_source_display": info.source_display,
    }
