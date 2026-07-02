#!/usr/bin/env python3
"""Build DataHub governance usage metadata from Hive and Trino SQL audit logs.

The module is intentionally split into pure parsing/aggregation helpers and a
small CLI wrapper. Unit tests cover the pure helpers; production runs can use
``--dry-run`` to inspect the JSON report before emitting DataHub MCPs.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

try:
    import sqlglot
    import sqlglot.expressions as exp
except ImportError as exc:  # pragma: no cover - exercised in deployment envs.
    raise SystemExit("缺少依赖：请先安装 sqlglot") from exc

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    __package__ = "job_info_sync_datahub"


IGNORED_PREFIXES = (
    "use",
    "set",
    "add jar",
    "add file",
    "create temporary function",
    "create temp function",
    "drop temporary function",
    "drop temp function",
    "show",
    "describe",
    "desc",
    "explain",
)


def make_hive_dataset_urn(
    db: str,
    table: str,
    platform_instance: str = "blf-prod-hive",
    env: str = "PROD",
) -> str:
    return (
        f"urn:li:dataset:(urn:li:dataPlatform:hive,"
        f"{platform_instance}.{db}.{table},{env})"
    )


@dataclasses.dataclass(frozen=True)
class AuditSqlRecord:
    engine: str
    user: str
    source: str
    query_id: Optional[str]
    executed_at: str
    sql_text: str
    sql_is_base64: bool = False
    state: Optional[str] = None
    used_tables: Optional[str] = None
    duration_seconds: Optional[float] = None
    raw_input_bytes: Optional[int] = None
    peak_memory_bytes: Optional[int] = None
    remote_user_address: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class ParsedSqlStatement:
    engine: str
    kind: str
    statement: str
    fingerprint: str
    user: str
    source: str
    executed_at: str
    query_id: Optional[str]
    state: Optional[str]
    read_tables: set[str]
    write_tables: set[str]
    fields_by_table: dict[str, set[str]]
    duration_seconds: Optional[float] = None
    raw_input_bytes: Optional[int] = None
    peak_memory_bytes: Optional[int] = None
    remote_user_address: Optional[str] = None


@dataclasses.dataclass
class ParseReport:
    total_statements: int = 0
    filtered_statements: int = 0
    parsed_statements: int = 0
    failed_statements: int = 0
    used_tables_fallback_statements: int = 0
    fallback_statements: int = 0
    default_db_tables: int = 0


@dataclasses.dataclass
class ParsedAuditRecord:
    record: AuditSqlRecord
    statements: list[ParsedSqlStatement]
    report: ParseReport


@dataclasses.dataclass
class DatasetUsage:
    table: str
    query_count: int = 0
    users: Counter[str] = dataclasses.field(default_factory=Counter)
    sources: Counter[str] = dataclasses.field(default_factory=Counter)
    fields: Counter[str] = dataclasses.field(default_factory=Counter)
    fingerprints: Counter[str] = dataclasses.field(default_factory=Counter)
    engines: Counter[str] = dataclasses.field(default_factory=Counter)


@dataclasses.dataclass(frozen=True)
class OperationEvent:
    table: str
    operation_type: str
    user: str
    source: str
    engine: str
    executed_at: str
    fingerprint: str
    query_id: Optional[str]


@dataclasses.dataclass
class AggregatedUsage:
    datasets: dict[str, DatasetUsage] = dataclasses.field(default_factory=dict)
    operations: list[OperationEvent] = dataclasses.field(default_factory=list)
    fingerprints: Counter[str] = dataclasses.field(default_factory=Counter)
    failed_queries: int = 0
    parse_report: ParseReport = dataclasses.field(default_factory=ParseReport)


class FileOperationCheckpoint:
    """Append-only local checkpoint for idempotent Operation emission."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._keys: Optional[set[str]] = None

    def _load(self) -> set[str]:
        if self._keys is not None:
            return self._keys
        keys: set[str] = set()
        if self.path.exists():
            with self.path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    key = row.get("operation_key")
                    if isinstance(key, str) and key:
                        keys.add(key)
        self._keys = keys
        return keys

    def contains(self, op: OperationEvent) -> bool:
        return operation_key(op) in self._load()

    def mark_written(self, op: OperationEvent) -> None:
        key = operation_key(op)
        keys = self._load()
        if key in keys:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = dataclasses.asdict(op)
        row["operation_key"] = key
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        keys.add(key)


def decode_sql(text: str, *, is_base64: bool) -> str:
    if not is_base64:
        return text or ""
    if not text:
        return ""
    return base64.b64decode(text).decode("utf-8", errors="replace")


def operation_key(op: OperationEvent) -> str:
    raw = "\x1f".join(
        [
            op.engine,
            op.table,
            op.operation_type,
            op.executed_at,
            op.fingerprint,
            op.query_id or "",
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def filter_new_operations(
    operations: list[OperationEvent],
    checkpoint: FileOperationCheckpoint,
) -> list[OperationEvent]:
    return [op for op in operations if not checkpoint.contains(op)]


def _strip_comments(sql: str) -> str:
    lines: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        if " --" in line:
            line = line[: line.index(" --")]
        lines.append(line)
    return "\n".join(lines)


def split_sql_statements(sql: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    quote: Optional[str] = None
    escape = False
    for char in sql:
        current.append(char)
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in ("'", '"', "`"):
            quote = char
            continue
        if char == ";":
            statement = "".join(current[:-1]).strip()
            if statement:
                statements.append(statement)
            current = []
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def _normalized_prefix(statement: str) -> str:
    return re.sub(r"\s+", " ", _strip_comments(statement).strip().lower())


def _is_ignored_statement(statement: str) -> bool:
    normalized = _normalized_prefix(statement)
    return not normalized or any(
        normalized == prefix or normalized.startswith(prefix + " ")
        for prefix in IGNORED_PREFIXES
    )


def _clean_identifier(value: str) -> str:
    return value.strip("`\" ").lower()


def _table_name(table: exp.Table, default_db: str, report: ParseReport) -> str:
    db = _clean_identifier(table.db) if table.db else default_db
    name = _clean_identifier(table.name)
    if not table.db:
        report.default_db_tables += 1
    return f"{db}.{name}"


def _cte_names(expression: exp.Expression) -> set[str]:
    return {
        cte.alias.lower()
        for cte in expression.find_all(exp.CTE)
        if cte.alias
    }


def _target_tables(expression: exp.Expression, default_db: str, report: ParseReport) -> set[str]:
    targets: set[str] = set()
    if isinstance(expression, exp.Insert) and isinstance(expression.this, exp.Table):
        targets.add(_table_name(expression.this, default_db, report))
    elif isinstance(expression, exp.Create):
        kind = str(expression.args.get("kind") or "").upper()
        if kind == "TABLE" and isinstance(expression.this, exp.Table):
            targets.add(_table_name(expression.this, default_db, report))
    elif isinstance(expression, (exp.Alter, exp.Drop, exp.Analyze)):
        target = expression.args.get("this")
        if isinstance(target, exp.Table):
            targets.add(_table_name(target, default_db, report))
    return targets


def _all_tables(expression: exp.Expression, default_db: str, report: ParseReport) -> set[str]:
    ctes = _cte_names(expression)
    tables: set[str] = set()
    for table in expression.find_all(exp.Table):
        if table.name.lower() in ctes:
            continue
        tables.add(_table_name(table, default_db, report))
    return tables


def _alias_map(expression: exp.Expression, default_db: str, report: ParseReport) -> dict[str, str]:
    ctes = _cte_names(expression)
    mapping: dict[str, str] = {}
    for table in expression.find_all(exp.Table):
        if table.name.lower() in ctes:
            continue
        full_name = _table_name(table, default_db, report)
        mapping[table.name.lower()] = full_name
        if table.alias:
            mapping[table.alias.lower()] = full_name
    return mapping


def _field_usage(
    expression: exp.Expression,
    read_tables: set[str],
    aliases: dict[str, str],
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    single_table = next(iter(read_tables)) if len(read_tables) == 1 else None
    for column in expression.find_all(exp.Column):
        field = _clean_identifier(column.name)
        if not field or field == "*" or not re.match(r"^[a-zA-Z_]\w*$", field):
            continue
        table_name: Optional[str] = None
        if column.table:
            table_name = aliases.get(column.table.lower())
        elif single_table:
            table_name = single_table
        if table_name and table_name in read_tables:
            result[table_name].add(field)
    return dict(result)


def _parse_with_dialects(
    statement: str,
    engine: str,
) -> tuple[exp.Expression, bool]:
    dialects = ["hive", "presto"] if engine == "hive" else ["presto", "hive"]
    last_exc: Optional[Exception] = None
    for i, dialect in enumerate(dialects):
        try:
            return sqlglot.parse_one(statement, read=dialect), i > 0
        except Exception as exc:
            last_exc = exc
    assert last_exc is not None
    raise last_exc


def _fallback_msck(statement: str, default_db: str, report: ParseReport) -> Optional[ParsedSqlStatement]:
    match = re.match(
        r"^\s*msck\s+repair\s+table\s+(`?[\w]+`?\.)?`?([\w]+)`?\s*$",
        statement,
        re.IGNORECASE,
    )
    if not match:
        return None
    db = _clean_identifier((match.group(1) or "").rstrip(".")) or default_db
    if not match.group(1):
        report.default_db_tables += 1
    table = f"{db}.{_clean_identifier(match.group(2))}"
    return ParsedSqlStatement(
        engine="hive",
        kind="msck",
        statement=statement,
        fingerprint=fingerprint_sql(statement),
        user="",
        source="",
        executed_at="",
        query_id=None,
        state=None,
        read_tables=set(),
        write_tables={table},
        fields_by_table={},
    )


def _fallback_alter_concatenate(
    statement: str,
    default_db: str,
    report: ParseReport,
) -> Optional[ParsedSqlStatement]:
    match = re.match(
        r"^\s*alter\s+table\s+(`?[\w]+`?\.)?`?([\w]+)`?\s+partition\s*\(.+\)\s+concatenate\s*$",
        statement,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    db = _clean_identifier((match.group(1) or "").rstrip(".")) or default_db
    if not match.group(1):
        report.default_db_tables += 1
    table = f"{db}.{_clean_identifier(match.group(2))}"
    return ParsedSqlStatement(
        engine="hive",
        kind="alter",
        statement=statement,
        fingerprint=fingerprint_sql(statement),
        user="",
        source="",
        executed_at="",
        query_id=None,
        state=None,
        read_tables=set(),
        write_tables={table},
        fields_by_table={},
    )


def _statement_kind(expression: exp.Expression, statement: str) -> str:
    if isinstance(expression, exp.Insert):
        return "insert"
    if isinstance(expression, exp.Select):
        return "select"
    if isinstance(expression, exp.Create):
        return "create"
    if isinstance(expression, exp.Alter):
        return "alter"
    if isinstance(expression, exp.Drop):
        return "drop"
    if isinstance(expression, exp.Analyze):
        return "analyze"
    normalized = _normalized_prefix(statement)
    if normalized.startswith("with "):
        return "select"
    if normalized.startswith("msck "):
        return "msck"
    return "other"


def parse_statement(
    statement: str,
    *,
    engine: str,
    user: str,
    source: str,
    executed_at: str,
    query_id: Optional[str],
    state: Optional[str],
    default_db: str,
    duration_seconds: Optional[float] = None,
    raw_input_bytes: Optional[int] = None,
    peak_memory_bytes: Optional[int] = None,
    remote_user_address: Optional[str] = None,
    report: Optional[ParseReport] = None,
) -> ParsedSqlStatement:
    report = report or ParseReport()
    msck = _fallback_msck(statement, default_db, report)
    if msck:
        return dataclasses.replace(
            msck,
            engine=engine,
            user=user,
            source=source,
            executed_at=executed_at,
            query_id=query_id,
            state=state,
            duration_seconds=duration_seconds,
            raw_input_bytes=raw_input_bytes,
            peak_memory_bytes=peak_memory_bytes,
            remote_user_address=remote_user_address,
        )
    if _normalized_prefix(statement).startswith("msck repair table"):
        raise ValueError("MSCK REPAIR TABLE missing table name")
    alter_concatenate = _fallback_alter_concatenate(statement, default_db, report)
    if alter_concatenate:
        return dataclasses.replace(
            alter_concatenate,
            engine=engine,
            user=user,
            source=source,
            executed_at=executed_at,
            query_id=query_id,
            state=state,
            duration_seconds=duration_seconds,
            raw_input_bytes=raw_input_bytes,
            peak_memory_bytes=peak_memory_bytes,
            remote_user_address=remote_user_address,
        )
    try:
        expression, used_fallback = _parse_with_dialects(statement, engine)
        if used_fallback:
            report.fallback_statements += 1
    except Exception:
        fallback = _fallback_msck(statement, default_db, report)
        if fallback:
            return dataclasses.replace(
                fallback,
                engine=engine,
                user=user,
                source=source,
                executed_at=executed_at,
                query_id=query_id,
                state=state,
                duration_seconds=duration_seconds,
                raw_input_bytes=raw_input_bytes,
                peak_memory_bytes=peak_memory_bytes,
                remote_user_address=remote_user_address,
            )
        raise

    write_tables = _target_tables(expression, default_db, report)
    read_tables = _all_tables(expression, default_db, report) - write_tables
    aliases = _alias_map(expression, default_db, report)
    return ParsedSqlStatement(
        engine=engine,
        kind=_statement_kind(expression, statement),
        statement=statement,
        fingerprint=fingerprint_sql(statement),
        user=user,
        source=source,
        executed_at=executed_at,
        query_id=query_id,
        state=state,
        read_tables=read_tables,
        write_tables=write_tables,
        fields_by_table=_field_usage(expression, read_tables, aliases),
        duration_seconds=duration_seconds,
        raw_input_bytes=raw_input_bytes,
        peak_memory_bytes=peak_memory_bytes,
        remote_user_address=remote_user_address,
    )


def parse_audit_record(record: AuditSqlRecord) -> ParsedAuditRecord:
    sql = decode_sql(record.sql_text, is_base64=record.sql_is_base64)
    statements = split_sql_statements(sql)
    report = ParseReport(total_statements=len(statements))
    parsed: list[ParsedSqlStatement] = []
    for statement in statements:
        if _is_ignored_statement(statement):
            report.filtered_statements += 1
            continue
        try:
            parsed_statement = parse_statement(
                _strip_comments(statement).strip(),
                engine=record.engine,
                user=record.user,
                source=record.source,
                executed_at=record.executed_at,
                query_id=record.query_id,
                state=record.state,
                default_db="default",
                duration_seconds=record.duration_seconds,
                raw_input_bytes=record.raw_input_bytes,
                peak_memory_bytes=record.peak_memory_bytes,
                remote_user_address=record.remote_user_address,
                report=report,
            )
            parsed.append(parsed_statement)
            report.parsed_statements += 1
        except Exception:
            report.failed_statements += 1
            fallback_tables = _used_tables(record.used_tables, "default")
            if fallback_tables:
                report.used_tables_fallback_statements += 1
            for used_table in fallback_tables:
                parsed.append(
                    ParsedSqlStatement(
                        engine=record.engine,
                        kind="unknown",
                        statement=statement,
                        fingerprint=fingerprint_sql(statement),
                        user=record.user,
                        source=record.source,
                        executed_at=record.executed_at,
                        query_id=record.query_id,
                        state=record.state,
                        read_tables={used_table},
                        write_tables=set(),
                        fields_by_table={},
                        duration_seconds=record.duration_seconds,
                        raw_input_bytes=record.raw_input_bytes,
                        peak_memory_bytes=record.peak_memory_bytes,
                        remote_user_address=record.remote_user_address,
                    )
                )
    return ParsedAuditRecord(record=record, statements=parsed, report=report)


def _used_tables(value: Optional[str], default_db: str) -> set[str]:
    if not value:
        return set()
    tables: set[str] = set()
    for token in re.split(r"[,;\s]+", value):
        token = _clean_identifier(token)
        if not token:
            continue
        if "." not in token:
            token = f"{default_db}.{token}"
        tables.add(token)
    return tables


def fingerprint_sql(statement: str) -> str:
    normalized = _strip_comments(statement).lower()
    normalized = re.sub(r"`([^`]+)`", r"\1", normalized)
    normalized = re.sub(r"'(?:\\'|[^'])*'", "?", normalized)
    normalized = re.sub(r'"(?:\\"|[^"])*"', "?", normalized)
    normalized = re.sub(r"\b20\d{6}\b", "?", normalized)
    normalized = re.sub(r"\b20\d{2}-\d{2}-\d{2}\b", "?", normalized)
    normalized = re.sub(r"\b\d+\b", "?", normalized)
    normalized = re.sub(r"tmp_[a-z0-9_]*_\?(_\?)*", "tmp_?", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def _operation_type(kind: str) -> Optional[str]:
    return {
        "insert": "INSERT",
        "create": "CREATE",
        "alter": "ALTER",
        "drop": "DROP",
        "msck": "CUSTOM",
        "analyze": "CUSTOM",
    }.get(kind)


def aggregate_usage(records: Iterable[ParsedAuditRecord]) -> AggregatedUsage:
    aggregated = AggregatedUsage()
    for record in records:
        aggregated.parse_report.total_statements += record.report.total_statements
        aggregated.parse_report.filtered_statements += record.report.filtered_statements
        aggregated.parse_report.parsed_statements += record.report.parsed_statements
        aggregated.parse_report.failed_statements += record.report.failed_statements
        aggregated.parse_report.used_tables_fallback_statements += (
            record.report.used_tables_fallback_statements
        )
        aggregated.parse_report.fallback_statements += record.report.fallback_statements
        aggregated.parse_report.default_db_tables += record.report.default_db_tables
        if record.record.state and record.record.state.upper() != "FINISHED":
            aggregated.failed_queries += 1
            continue
        for statement in record.statements:
            aggregated.fingerprints[statement.fingerprint] += 1
            for table in statement.read_tables:
                usage = aggregated.datasets.setdefault(table, DatasetUsage(table=table))
                usage.query_count += 1
                usage.users[statement.user] += 1
                usage.sources[statement.source] += 1
                usage.fingerprints[statement.fingerprint] += 1
                usage.engines[statement.engine] += 1
                for field in statement.fields_by_table.get(table, set()):
                    usage.fields[field] += 1
            op_type = _operation_type(statement.kind)
            if op_type:
                for table in statement.write_tables:
                    aggregated.operations.append(
                        OperationEvent(
                            table=table,
                            operation_type=op_type,
                            user=statement.user,
                            source=statement.source,
                            engine=statement.engine,
                            executed_at=statement.executed_at,
                            fingerprint=statement.fingerprint,
                            query_id=statement.query_id,
                        )
                    )
    return aggregated


def _bytes_from_text(value: object) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.match(r"^([0-9.]+)\s*([KMGT]?B)?$", text, re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "B").upper()
    scale = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}[unit]
    return int(number * scale)


def _float_or_none(value: object) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _mysql_connection(database: str):
    try:
        import pymysql
    except ImportError as exc:  # pragma: no cover - deployment dependency.
        raise RuntimeError("缺少依赖：请先安装 pymysql") from exc
    return pymysql.connect(
        host=os.getenv("SCHEDULER_MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("SCHEDULER_MYSQL_PORT", "3306")),
        user=os.getenv("SCHEDULER_MYSQL_USER", ""),
        password=os.getenv("SCHEDULER_MYSQL_PASSWORD", ""),
        database=database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def _fetch_hive_records(conn, date: str, limit: Optional[int]) -> list[AuditSqlRecord]:
    sql = (
        "SELECT source, `sql`, create_time, `user`, date, used_tables "
        "FROM hive_sql_audit FORCE INDEX(index_hive_sql_audit_date) WHERE date=%s"
    )
    params: list[object] = [date]
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        return [
            AuditSqlRecord(
                engine="hive",
                user=str(row.get("user") or ""),
                source=str(row.get("source") or ""),
                query_id=None,
                executed_at=str(row.get("create_time") or row.get("date") or ""),
                sql_text=str(row.get("sql") or ""),
                sql_is_base64=True,
                used_tables=str(row.get("used_tables") or "") or None,
            )
            for row in cur.fetchall()
        ]


def _day_time_bounds(date: str) -> tuple[str, str]:
    start = datetime.strptime(date, "%Y-%m-%d")
    end = start + timedelta(days=1)
    return (
        start.strftime("%Y-%m-%d 00:00:00"),
        end.strftime("%Y-%m-%d 00:00:00"),
    )


def _fetch_trino_records(conn, dt: str, limit: Optional[int]) -> list[AuditSqlRecord]:
    start_time, end_time = _day_time_bounds(dt)
    sql = (
        "SELECT `user`, source, state, remote_user_address, duration, start_time, "
        "create_time, query_sql, query_id, peak_total_memory_reservation, raw_input_data_size "
        "FROM trino_query_history FORCE INDEX(idx_create_time) "
        "WHERE create_time >= %s AND create_time < %s ORDER BY create_time"
    )
    params: list[object] = [start_time, end_time]
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        return [
            AuditSqlRecord(
                engine="trino",
                user=str(row.get("user") or ""),
                source=str(row.get("source") or ""),
                query_id=str(row.get("query_id") or "") or None,
                executed_at=str(row.get("start_time") or row.get("create_time") or ""),
                sql_text=str(row.get("query_sql") or ""),
                state=str(row.get("state") or "") or None,
                duration_seconds=_float_or_none(row.get("duration")),
                raw_input_bytes=_bytes_from_text(row.get("raw_input_data_size")),
                peak_memory_bytes=_bytes_from_text(row.get("peak_total_memory_reservation")),
                remote_user_address=str(row.get("remote_user_address") or "") or None,
            )
            for row in cur.fetchall()
        ]


def _counter_dict(counter: Counter[str], limit: int = 20) -> dict[str, int]:
    return dict(counter.most_common(limit))


def build_report(usage: AggregatedUsage) -> dict[str, object]:
    hot_tables = sorted(
        usage.datasets.values(),
        key=lambda item: item.query_count,
        reverse=True,
    )[:100]
    return {
        "parse_report": dataclasses.asdict(usage.parse_report),
        "dataset_count": len(usage.datasets),
        "operation_count": len(usage.operations),
        "failed_queries": usage.failed_queries,
        "top_fingerprints": _counter_dict(usage.fingerprints, 50),
        "hot_tables": [
            {
                "table": item.table,
                "query_count": item.query_count,
                "users": _counter_dict(item.users),
                "sources": _counter_dict(item.sources),
                "engines": _counter_dict(item.engines),
                "fields": _counter_dict(item.fields),
            }
            for item in hot_tables
        ],
        "operations": [
            {**dataclasses.asdict(op), "operation_key": operation_key(op)}
            for op in usage.operations[:500]
        ],
    }


def emit_to_datahub(
    usage: AggregatedUsage,
    *,
    gms_url: Optional[str],
    token: Optional[str],
    platform_instance: str,
    env: str,
    bucket_date: str,
    operation_checkpoint: Optional[FileOperationCheckpoint] = None,
) -> None:
    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.emitter.rest_emitter import DatahubRestEmitter
    from datahub.metadata.schema_classes import (
        DatasetFieldUsageCountsClass,
        DatasetUsageStatisticsClass,
        DatasetUserUsageCountsClass,
        OperationClass,
        OperationSourceTypeClass,
        OperationTypeClass,
        TimeWindowSizeClass,
    )

    start = datetime.strptime(bucket_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    emitter = DatahubRestEmitter(gms_url or os.getenv("DATAHUB_GMS_URL", "http://127.0.0.1:8080"), token=token)
    for item in usage.datasets.values():
        db, table = item.table.split(".", 1)
        urn = make_hive_dataset_urn(db, table, platform_instance=platform_instance, env=env)
        aspect = DatasetUsageStatisticsClass(
            timestampMillis=int(start.timestamp() * 1000),
            eventGranularity=TimeWindowSizeClass(unit="DAY", multiple=1),
            uniqueUserCount=len(item.users),
            totalSqlQueries=item.query_count,
            topSqlQueries=None,
            userCounts=[
                DatasetUserUsageCountsClass(
                    user=f"urn:li:corpuser:{user}",
                    count=count,
                    userEmail=None,
                )
                for user, count in item.users.most_common(100)
            ],
            fieldCounts=[
                DatasetFieldUsageCountsClass(fieldPath=field, count=count)
                for field, count in item.fields.most_common(500)
            ],
        )
        emitter.emit_mcp(MetadataChangeProposalWrapper(entityUrn=urn, aspect=aspect))
    op_type_by_name = {
        "INSERT": OperationTypeClass.INSERT,
        "CREATE": OperationTypeClass.CREATE,
        "ALTER": OperationTypeClass.ALTER,
        "DROP": OperationTypeClass.DROP,
        "CUSTOM": OperationTypeClass.CUSTOM,
    }
    operations = usage.operations
    if operation_checkpoint is not None:
        operations = filter_new_operations(operations, operation_checkpoint)
    for op in operations:
        db, table = op.table.split(".", 1)
        urn = make_hive_dataset_urn(db, table, platform_instance=platform_instance, env=env)
        ts = int(datetime.fromisoformat(op.executed_at.replace("Z", "+00:00")).timestamp() * 1000)
        aspect = OperationClass(
            timestampMillis=ts,
            operationType=op_type_by_name.get(op.operation_type, OperationTypeClass.CUSTOM),
            customOperationType=op.operation_type if op.operation_type == "CUSTOM" else None,
            lastUpdatedTimestamp=ts,
            actor=f"urn:li:corpuser:{op.user}" if op.user else None,
            sourceType=OperationSourceTypeClass.DATA_PLATFORM,
        )
        emitter.emit_mcp(MetadataChangeProposalWrapper(entityUrn=urn, aspect=aspect))
        if operation_checkpoint is not None:
            operation_checkpoint.mark_written(op)


def run(args: argparse.Namespace) -> int:
    conn = _mysql_connection(args.database)
    try:
        records: list[AuditSqlRecord] = []
        if args.engine in ("hive", "both"):
            records.extend(_fetch_hive_records(conn, args.date, args.limit))
        if args.engine in ("trino", "both"):
            records.extend(_fetch_trino_records(conn, args.date, args.limit))
    finally:
        conn.close()
    parsed = [parse_audit_record(record) for record in records]
    usage = aggregate_usage(parsed)
    report = build_report(usage)
    report["input_records"] = len(records)
    report["date"] = args.date
    report["engine"] = args.engine
    if args.output:
        Path(args.output).write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.emit:
        operation_checkpoint = (
            FileOperationCheckpoint(Path(args.operation_checkpoint))
            if args.operation_checkpoint
            else None
        )
        emit_to_datahub(
            usage,
            gms_url=args.gms_url,
            token=args.gms_token,
            platform_instance=args.platform_instance,
            env=args.env,
            bucket_date=args.date,
            operation_checkpoint=operation_checkpoint,
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="Execution date, e.g. 2026-07-01")
    parser.add_argument("--engine", choices=["hive", "trino", "both"], default="both")
    parser.add_argument("--database", default=os.getenv("SCHEDULER_MYSQL_DATABASE", "data_platform"))
    parser.add_argument("--limit", type=int, default=None, help="Optional row limit for smoke tests")
    parser.add_argument("--output", help="Write JSON report to this path")
    parser.add_argument("--emit", action="store_true", help="Emit DatasetUsageStatistics and Operation MCPs")
    parser.add_argument("--gms-url", default=os.getenv("DATAHUB_GMS_URL"))
    parser.add_argument("--gms-token", default=os.getenv("DATAHUB_GMS_TOKEN"))
    parser.add_argument("--platform-instance", default=os.getenv("BLF_DATAHUB_PLATFORM_INSTANCE", "blf-prod-hive"))
    parser.add_argument("--env", default="PROD")
    parser.add_argument(
        "--operation-checkpoint",
        help="Local JSONL checkpoint file for deduplicating Operation emission",
    )
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
