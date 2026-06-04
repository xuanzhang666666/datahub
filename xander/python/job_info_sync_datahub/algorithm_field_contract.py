"""Build A-side algorithm field contracts from DataHub dataset properties.

This module reads the BLF structured properties stored on Hive datasets,
resolves ETL scripts with runtime variables from Execute Shell, parses upstream
table and field lineage, and writes local report artifacts. It does not write
anything back to DataHub.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set

from openpyxl import Workbook

from .field_lineage_datahub_reader import (
    fetch_structured_properties,
    make_hive_dataset_urn,
    strip_markdown_code_fence,
)
from .lineage_parser import build_lineage_summary, parse_block_lineage
from .models import FieldLineage, ParseStatus, SqlBlock, TableLineage
from .query_upstream_lineage import normalize_table_name
from .sql_extractor import compute_format_date_vars, extract_sql_blocks
from .structured_properties import (
    URN_ETL_SCRIPT,
    URN_EXECUTE_SHELL,
    URN_SCHEDULE_URL,
)

OK = "OK"
MISSING_ETL_SCRIPT = "MISSING_ETL_SCRIPT"
NO_RUNTIME_VARS = "NO_RUNTIME_VARS"
SQL_PARSE_FAILED = "SQL_PARSE_FAILED"


@dataclass(frozen=True)
class DatasetJobProperties:
    table_name: str = ""
    dataset_urn: str = ""
    etl_script: str = ""
    execute_shell: str = ""
    schedule_url: str = ""
    property_urns: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeVariableContext:
    raw_execute_shell: str
    variables: Dict[str, str]
    positionals: List[str]
    unparsed_tokens: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResolvedEtlScript:
    original_script: str
    resolved_script: str
    unresolved_variables: List[str]


@dataclass
class TableContractAnalysis:
    table_name: str
    dataset_urn: str
    status: str
    properties: DatasetJobProperties
    runtime_context: RuntimeVariableContext
    resolved_etl_script: Optional[str] = None
    unresolved_variables: List[str] = field(default_factory=list)
    target_fields: List[str] = field(default_factory=list)
    unresolved_target_fields: List[str] = field(default_factory=list)
    sql_blocks: List[SqlBlock] = field(default_factory=list)
    table_lineages: List[TableLineage] = field(default_factory=list)
    field_lineages: List[FieldLineage] = field(default_factory=list)
    open_questions: List[str] = field(default_factory=list)


def _iter_property_assignments(payload: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    current: Any = payload
    for key in ("structuredProperties", "value"):
        if isinstance(current, dict) and key in current:
            current = current[key]
    if isinstance(current, dict) and isinstance(current.get("properties"), list):
        for item in current["properties"]:
            if isinstance(item, dict):
                yield item


def _first_string_value(assignment: Dict[str, Any]) -> str:
    values = assignment.get("values")
    if not isinstance(values, list):
        return ""
    for value in values:
        if isinstance(value, dict) and isinstance(value.get("string"), str):
            return value["string"]
        if isinstance(value, str):
            return value
    return ""


def _property_key(assignment: Dict[str, Any]) -> str:
    property_urn = assignment.get("propertyUrn")
    if isinstance(property_urn, str):
        return property_urn
    prop = assignment.get("property")
    if isinstance(prop, dict):
        urn = prop.get("urn")
        if isinstance(urn, str):
            return urn
        display_name = prop.get("displayName") or prop.get("name")
        if isinstance(display_name, str):
            return display_name
    display_name = assignment.get("displayName") or assignment.get("name")
    return display_name if isinstance(display_name, str) else ""


def extract_dataset_job_properties(
    payload: Dict[str, Any],
    *,
    table_name: str = "",
    dataset_urn: str = "",
) -> DatasetJobProperties:
    """Extract schedule_url, etl_script, and execute_shell from structuredProperties."""
    values: Dict[str, str] = {}
    for assignment in _iter_property_assignments(payload):
        key = _property_key(assignment)
        if key:
            values[key] = _first_string_value(assignment)

    def by_any(*keys: str) -> str:
        for key in keys:
            value = values.get(key, "")
            if value:
                return value
        return ""

    etl_script = strip_markdown_code_fence(
        by_any(URN_ETL_SCRIPT, "Etl Script", "表的etl 脚本", "表的etl脚本")
    )
    execute_shell = strip_markdown_code_fence(
        by_any(URN_EXECUTE_SHELL, "Execute Shell", "调度系统上配置的启动命令")
    )
    schedule_url = by_any(URN_SCHEDULE_URL, "Schedule URL", "调度系统任务链接").strip()

    return DatasetJobProperties(
        table_name=table_name,
        dataset_urn=dataset_urn,
        etl_script=etl_script,
        execute_shell=execute_shell,
        schedule_url=schedule_url,
        property_urns={
            "etl_script": URN_ETL_SCRIPT,
            "execute_shell": URN_EXECUTE_SHELL,
            "schedule_url": URN_SCHEDULE_URL,
        },
    )


def _strip_shell_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_assignment_token(token: str) -> Optional[tuple[str, str]]:
    if "=" not in token or token.startswith("--"):
        return None
    key, value = token.split("=", 1)
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
        return None
    return key, _strip_shell_quotes(value)


def _add_date_derivatives(variables: Dict[str, str]) -> None:
    for key in ("DATE", "date", "dt", "bizdate", "BIZDATE"):
        value = variables.get(key)
        if value and re.match(r"^\d{8}$", value):
            for k, v in compute_format_date_vars(value).items():
                variables.setdefault(k, v)
            return


def parse_execute_shell_variables(execute_shell: str) -> RuntimeVariableContext:
    """Parse runtime variables from Execute Shell.

    Supports ``VAR=value``, ``export VAR=value``, ``--key=value``,
    ``--key value``, and preserves positional tokens for evidence.
    """
    raw = strip_markdown_code_fence(execute_shell or "")
    variables: Dict[str, str] = {}
    positionals: List[str] = []
    unparsed: List[str] = []

    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError:
        tokens = raw.split()
        unparsed.append("SHLEX_PARSE_FAILED")

    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "export":
            if i + 1 < len(tokens):
                parsed = _parse_assignment_token(tokens[i + 1])
                if parsed:
                    variables[parsed[0]] = parsed[1]
                    i += 2
                    continue
            i += 1
            continue

        parsed = _parse_assignment_token(token)
        if parsed:
            variables[parsed[0]] = parsed[1]
            i += 1
            continue

        if token.startswith("--"):
            option = token[2:]
            if "=" in option:
                key, value = option.split("=", 1)
                if key:
                    variables[key] = _strip_shell_quotes(value)
            else:
                key = option
                if key and i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                    variables[key] = _strip_shell_quotes(tokens[i + 1])
                    i += 1
                elif key:
                    variables[key] = ""
            i += 1
            continue

        positionals.append(token)
        i += 1

    _add_date_derivatives(variables)
    return RuntimeVariableContext(
        raw_execute_shell=raw,
        variables=variables,
        positionals=positionals,
        unparsed_tokens=unparsed,
    )


_BRACED_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_DOLLAR_VAR_RE = re.compile(r"(?<!\$)\$([A-Za-z_][A-Za-z0-9_]*)\b")
_JINJA_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_IGNORED_UNRESOLVED_VARS = {
    "HIVE",
    "hive",
    "HDFS",
    "hdfs",
    "PYTHON",
    "python",
    "CHECK",
}


def resolve_etl_script(
    etl_script: str,
    runtime_context: RuntimeVariableContext,
) -> ResolvedEtlScript:
    """Replace ETL script placeholders using runtime variables from Execute Shell."""
    variables = runtime_context.variables
    unresolved: Set[str] = set()

    def replace_var(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in variables:
            return variables[name]
        if name not in _IGNORED_UNRESOLVED_VARS:
            unresolved.add(name)
        return match.group(0)

    resolved = _BRACED_VAR_RE.sub(replace_var, etl_script or "")
    resolved = _DOLLAR_VAR_RE.sub(replace_var, resolved)
    resolved = _JINJA_VAR_RE.sub(replace_var, resolved)
    return ResolvedEtlScript(
        original_script=etl_script or "",
        resolved_script=resolved,
        unresolved_variables=sorted(unresolved),
    )


def _date_for_sql_extractor(runtime_context: RuntimeVariableContext) -> Optional[str]:
    for key in ("DATE", "date", "dt", "bizdate", "BIZDATE"):
        value = runtime_context.variables.get(key)
        if value and re.match(r"^\d{8}$", value):
            return value
    return None


def _job_file_name(table_name: str, etl_script: str) -> str:
    stripped = etl_script.lstrip()
    if stripped.startswith("#!") or re.search(r"\bpython\b|spark\.sql|cursor\.execute", stripped[:500], re.I):
        return f"{table_name}.structured_property.py"
    return f"{table_name}.structured_property.job"


def _filter_lineages_for_target(
    table_lineages: List[TableLineage],
    field_lineages: List[FieldLineage],
    table_name: str,
) -> tuple[List[TableLineage], List[FieldLineage]]:
    target = normalize_table_name(table_name)
    return (
        [lineage for lineage in table_lineages if lineage.target.full_name.lower() == target],
        [lineage for lineage in field_lineages if lineage.target_table.full_name.lower() == target],
    )


def _unresolved_target_fields(
    target_fields: Sequence[str],
    field_lineages: Sequence[FieldLineage],
) -> List[str]:
    parsed_fields = {lineage.target_field.lower() for lineage in field_lineages}
    unresolved: List[str] = []
    for field_name in target_fields:
        normalized = field_name.strip()
        if normalized and normalized.lower() not in parsed_fields:
            unresolved.append(normalized)
    return unresolved


def analyze_table_payload(
    table_name: str,
    dataset_urn: str,
    payload: Dict[str, Any],
    *,
    target_fields: Optional[Sequence[str]] = None,
) -> TableContractAnalysis:
    """Analyze one target table from a DataHub structuredProperties payload."""
    normalized_table = normalize_table_name(table_name)
    props = extract_dataset_job_properties(
        payload,
        table_name=normalized_table,
        dataset_urn=dataset_urn,
    )

    if not props.etl_script.strip():
        return TableContractAnalysis(
            table_name=normalized_table,
            dataset_urn=dataset_urn,
            status=MISSING_ETL_SCRIPT,
            properties=props,
            runtime_context=RuntimeVariableContext(props.execute_shell, {}, []),
            target_fields=list(target_fields or []),
            unresolved_target_fields=list(target_fields or []),
            open_questions=[f"{normalized_table}: missing {URN_ETL_SCRIPT}"],
        )

    runtime = parse_execute_shell_variables(props.execute_shell)
    resolved = resolve_etl_script(props.etl_script, runtime)
    job_file_name = _job_file_name(normalized_table, props.etl_script)
    blocks = extract_sql_blocks(
        resolved.resolved_script,
        job_file_name=job_file_name,
        date_str=_date_for_sql_extractor(runtime),
    )
    parsed_blocks = [parse_block_lineage(block, job_display_name=normalized_table) for block in blocks]
    table_lineages, field_lineages = build_lineage_summary(parsed_blocks)
    table_lineages, field_lineages = _filter_lineages_for_target(
        table_lineages,
        field_lineages,
        normalized_table,
    )

    open_questions: List[str] = []
    if not props.execute_shell.strip():
        open_questions.append(f"{normalized_table}: missing {URN_EXECUTE_SHELL}; parsed static ETL only")
    for variable in resolved.unresolved_variables:
        open_questions.append(f"{normalized_table}: unresolved variable {variable}")
    failed_blocks = [block for block in parsed_blocks if block.status == ParseStatus.SQL_PARSE_FAILED]
    for block in failed_blocks:
        open_questions.append(f"{normalized_table}: SQL block #{block.index} parse failed: {block.error_detail}")

    target_fields_list = list(target_fields or [])
    unresolved_target_fields = _unresolved_target_fields(target_fields_list, field_lineages)
    for field_name in unresolved_target_fields:
        open_questions.append(f"{normalized_table}.{field_name}: target field has no parsed upstream field lineage")

    status = OK
    if not props.execute_shell.strip():
        status = NO_RUNTIME_VARS
    elif failed_blocks and not table_lineages:
        status = SQL_PARSE_FAILED

    return TableContractAnalysis(
        table_name=normalized_table,
        dataset_urn=dataset_urn,
        status=status,
        properties=props,
        runtime_context=runtime,
        resolved_etl_script=resolved.resolved_script,
        unresolved_variables=resolved.unresolved_variables,
        target_fields=target_fields_list,
        unresolved_target_fields=unresolved_target_fields,
        sql_blocks=parsed_blocks,
        table_lineages=table_lineages,
        field_lineages=field_lineages,
        open_questions=open_questions,
    )


StructuredPayloadFetcher = Callable[[str, str], Dict[str, Any]]


def _dataset_aspect_url(gms_url: str, dataset_urn: str, aspect_name: str) -> str:
    encoded = urllib.parse.quote(dataset_urn, safe="")
    return f"{gms_url.rstrip('/')}/openapi/v3/entity/dataset/{encoded}/{aspect_name}"


def fetch_schema_fields(
    gms_url: str,
    dataset_urn: str,
    token: Optional[str] = None,
    timeout_sec: int = 60,
) -> List[str]:
    """Fetch field paths from DataHub schemaMetadata for one dataset."""
    req = urllib.request.Request(
        _dataset_aspect_url(gms_url, dataset_urn, "schemaMetadata"),
        method="GET",
        headers={"Accept": "application/json"},
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise

    current: Any = payload
    for key in ("schemaMetadata", "value"):
        if isinstance(current, dict) and key in current:
            current = current[key]
    fields = current.get("fields") if isinstance(current, dict) else None
    if not isinstance(fields, list):
        return []
    result: List[str] = []
    for field in fields:
        if isinstance(field, dict) and isinstance(field.get("fieldPath"), str):
            result.append(field["fieldPath"])
    return result


def analyze_tables(
    table_names: Sequence[str],
    *,
    gms_url: str,
    token: Optional[str] = None,
    platform_instance: str = "blf-prod-hive",
    env: str = "PROD",
    max_depth: int = 3,
    fetcher: Optional[StructuredPayloadFetcher] = None,
) -> List[TableContractAnalysis]:
    """Analyze target tables and recursively analyze parsed upstream tables."""
    if fetcher is None:
        def fetcher(_: str, urn: str) -> Dict[str, Any]:
            return fetch_structured_properties(gms_url, urn, token=token)

    results: List[TableContractAnalysis] = []
    seen: Set[str] = set()
    queue: List[tuple[str, int]] = [
        (normalize_table_name(table_name), 0) for table_name in table_names
    ]

    while queue:
        table_name, depth = queue.pop(0)
        if table_name in seen or depth > max_depth:
            continue
        seen.add(table_name)

        dataset_urn = make_hive_dataset_urn(table_name, platform_instance, env)
        try:
            payload = fetcher(table_name, dataset_urn)
        except Exception as exc:
            props = DatasetJobProperties(table_name=table_name, dataset_urn=dataset_urn)
            results.append(
                TableContractAnalysis(
                    table_name=table_name,
                    dataset_urn=dataset_urn,
                    status="FETCH_STRUCTURED_PROPERTIES_FAILED",
                    properties=props,
                    runtime_context=RuntimeVariableContext("", {}, []),
                    open_questions=[f"{table_name}: fetch structuredProperties failed: {exc}"],
                )
            )
            continue

        try:
            target_fields = fetch_schema_fields(gms_url, dataset_urn, token=token)
        except Exception:
            target_fields = []

        result = analyze_table_payload(
            table_name,
            dataset_urn,
            payload,
            target_fields=target_fields,
        )
        results.append(result)

        if depth >= max_depth:
            continue
        for lineage in result.table_lineages:
            for upstream in lineage.upstreams:
                upstream_name = normalize_table_name(upstream.full_name)
                if upstream_name not in seen:
                    queue.append((upstream_name, depth + 1))

    return results


def _field_mapping_to_dict(mapping: Any) -> Dict[str, Any]:
    return {
        "source_table": mapping.source_table,
        "source_field": mapping.source_field,
        "expression": mapping.expression,
        "confidence": mapping.confidence.value,
    }


def _table_result_to_json(result: TableContractAnalysis) -> Dict[str, Any]:
    return {
        "table_name": result.table_name,
        "dataset_urn": result.dataset_urn,
        "status": result.status,
        "schedule_url": result.properties.schedule_url,
        "unresolved_variables": result.unresolved_variables,
        "target_fields": result.target_fields,
        "unresolved_target_fields": result.unresolved_target_fields,
        "table_lineages": [
            {
                "target_table": lineage.target.full_name,
                "upstream_tables": [upstream.full_name for upstream in lineage.upstreams],
                "source_block_indices": lineage.source_block_indices,
            }
            for lineage in result.table_lineages
        ],
        "field_lineages": [
            {
                "target_table": lineage.target_table.full_name,
                "target_field": lineage.target_field,
                "confidence": lineage.confidence.value,
                "mappings": [_field_mapping_to_dict(mapping) for mapping in lineage.mappings],
            }
            for lineage in result.field_lineages
        ],
        "open_questions": result.open_questions,
    }


def write_contract_outputs(
    results: Sequence[TableContractAnalysis],
    output_dir: str | Path,
) -> None:
    """Write JSON, Markdown, and Excel artifacts for contract review."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    (out / "field_lineage.json").write_text(
        json.dumps({"tables": [_table_result_to_json(result) for result in results]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out / "runtime_context.json").write_text(
        json.dumps(
            {
                result.table_name: {
                    **asdict(result.runtime_context),
                    "resolved_etl_script_path": f"resolved_sql/{result.table_name.replace('.', '_')}.sql",
                }
                for result in results
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    resolved_dir = out / "resolved_sql"
    resolved_dir.mkdir(exist_ok=True)
    for result in results:
        if result.resolved_etl_script is not None:
            (resolved_dir / f"{result.table_name.replace('.', '_')}.sql").write_text(
                result.resolved_etl_script,
                encoding="utf-8",
            )

    _write_report(results, out / "lineage_report.md")
    _write_open_questions(results, out / "open_questions.md")
    _write_excel(results, out / "field_contract.xlsx")


def _write_report(results: Sequence[TableContractAnalysis], path: Path) -> None:
    lines = ["# A-side algorithm field contract", ""]
    for result in results:
        lines.extend(
            [
                f"## {result.table_name}",
                "",
                f"- status: `{result.status}`",
                f"- dataset_urn: `{result.dataset_urn}`",
                f"- schedule_url: {result.properties.schedule_url or '-'}",
        f"- unresolved_variables: {', '.join(result.unresolved_variables) or '-'}",
                f"- unresolved_target_fields: {', '.join(result.unresolved_target_fields) or '-'}",
                "",
                "| target_field | source_table | source_field | expression | confidence |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for lineage in result.field_lineages:
            for mapping in lineage.mappings:
                lines.append(
                    "| {target} | {source_table} | {source_field} | {expr} | {confidence} |".format(
                        target=lineage.target_field,
                        source_table=mapping.source_table or "-",
                        source_field=mapping.source_field or "-",
                        expr=(mapping.expression or "-").replace("|", "\\|"),
                        confidence=mapping.confidence.value,
                    )
                )
        if not result.field_lineages:
            lines.append("| - | - | - | - | - |")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_open_questions(results: Sequence[TableContractAnalysis], path: Path) -> None:
    lines = ["# Open Questions", ""]
    has_questions = False
    for result in results:
        for question in result.open_questions:
            has_questions = True
            lines.append(f"- {question}")
    if not has_questions:
        lines.append("- 无")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_excel(results: Sequence[TableContractAnalysis], path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "field_contract"
    ws.append(
        [
            "table_name",
            "dataset_urn",
            "status",
            "target_field",
            "source_table",
            "source_field",
            "expression",
            "confidence",
            "schedule_url",
            "unresolved_variables",
            "unresolved_target_field",
        ]
    )
    for result in results:
        if not result.field_lineages:
            ws.append(
                [
                    result.table_name,
                    result.dataset_urn,
                    result.status,
                    "",
                    "",
                    "",
                    "",
                    "",
                    result.properties.schedule_url,
                    ", ".join(result.unresolved_variables),
                    ", ".join(result.unresolved_target_fields),
                ]
            )
            for field_name in result.unresolved_target_fields:
                ws.append(
                    [
                        result.table_name,
                        result.dataset_urn,
                        result.status,
                        field_name,
                        "",
                        "",
                        "",
                        "UNRESOLVED",
                        result.properties.schedule_url,
                        ", ".join(result.unresolved_variables),
                        field_name,
                    ]
                )
            continue
        for lineage in result.field_lineages:
            for mapping in lineage.mappings:
                ws.append(
                    [
                        result.table_name,
                        result.dataset_urn,
                        result.status,
                        lineage.target_field,
                        mapping.source_table or "",
                        mapping.source_field or "",
                        mapping.expression,
                        mapping.confidence.value,
                        result.properties.schedule_url,
                        ", ".join(result.unresolved_variables),
                        "",
                    ]
                )
        for field_name in result.unresolved_target_fields:
            ws.append(
                [
                    result.table_name,
                    result.dataset_urn,
                    result.status,
                    field_name,
                    "",
                    "",
                    "",
                    "UNRESOLVED",
                    result.properties.schedule_url,
                    ", ".join(result.unresolved_variables),
                    field_name,
                ]
            )
    wb.save(path)


def _read_table_names(args: argparse.Namespace) -> List[str]:
    names: List[str] = []
    for value in args.table or []:
        for token in re.split(r"[,，\n]+", value):
            if token.strip():
                names.append(token.strip())
    if args.table_file:
        for line in Path(args.table_file).read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                names.append(stripped)
    if not names:
        names.extend(table_names_from_env())
    return names


def table_names_from_env(env_var: str = "TABLE_NAMES") -> List[str]:
    """Read Jenkins TABLE_NAMES multi-line parameter.

    Supports newline, comma, and Chinese comma separators. Comment-only lines
    beginning with ``#`` are ignored.
    """
    raw = os.getenv(env_var, "")
    names: List[str] = []
    for line in raw.replace("\r", "\n").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for token in re.split(r"[,，]+", stripped):
            table_name = token.strip()
            if table_name and not table_name.startswith("#"):
                names.append(table_name)
    return names


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", action="append", help="Target table name; repeatable or comma-separated")
    parser.add_argument("--table-file", help="File containing target table names")
    parser.add_argument("--gms-url", default=os.getenv("DATAHUB_GMS_URL", "http://localhost:8080"))
    parser.add_argument("--token", default=os.getenv("DATAHUB_GMS_TOKEN"))
    parser.add_argument("--platform-instance", default=os.getenv("BLF_DATAHUB_PLATFORM_INSTANCE", "blf-prod-hive"))
    parser.add_argument("--env", default=os.getenv("DATAHUB_ENV", "PROD"))
    parser.add_argument("--max-depth", type=int, default=int(os.getenv("FIELD_CONTRACT_MAX_DEPTH", "3")))
    parser.add_argument("--output-dir", default=os.getenv("FIELD_CONTRACT_OUTPUT_DIR", "tmp/algorithm_field_contract"))
    args = parser.parse_args(argv)

    table_names = _read_table_names(args)
    if not table_names:
        parser.error("at least one --table, --table-file, or TABLE_NAMES env value is required")

    results = analyze_tables(
        table_names,
        gms_url=args.gms_url,
        token=args.token,
        platform_instance=args.platform_instance,
        env=args.env,
        max_depth=args.max_depth,
    )
    write_contract_outputs(results, args.output_dir)
    print(f"wrote contract outputs to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
