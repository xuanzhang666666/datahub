"""Read field-lineage inputs from DataHub structuredProperties."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import shlex
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .field_lineage_models import FieldLineageInput
from .sql_extractor import compute_format_date_vars
from .structured_properties import (
    URN_DATA_AVAILABILITY_FLAG,
    URN_ETL_SCRIPT,
    URN_EXECUTE_SHELL,
)

# Jenkins / 人工排查用显示名（与 DataHub structured property 一致）
LABEL_ETL_SCRIPT = "Etl Script (blf.data.warehouse.etl_script)"
LABEL_EXECUTE_SHELL = "Execute Shell (blf.data.schedule.execute_shell)"

_CODE_FENCE_RE = re.compile(r"^\s*```[^\n`]*\n(?P<body>[\s\S]*?)\n?```\s*$")
_BRACED_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_DOLLAR_VAR_RE = re.compile(r"(?<!\$)\$([A-Za-z_][A-Za-z0-9_]*)\b")
_JINJA_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_IGNORED_UNRESOLVED_VARS = {
    "CHECK",
    "HDFS",
    "HIVE",
    "PYTHON",
    "hdfs",
    "hive",
    "python",
}
_SHELL_FUNCTION_START_RE = re.compile(
    r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(\s*\))?\s*\{"
)


@dataclass(frozen=True)
class FieldLineagePreparationDebug:
    """Intermediate ETL processing results saved for Jenkins troubleshooting."""

    original_etl_script: str
    execute_shell: str
    execute_shell_variables: Dict[str, str]
    script_variables: Dict[str, str]
    all_variables: Dict[str, str]
    resolved_etl_script: str
    llm_input_etl_script: str
    unresolved_variables: List[str]
    is_python_script: bool
    entry_function: str
    reachable_functions: List[str]
    removed_functions: List[str]


def make_hive_dataset_urn(
    table_name: str,
    platform_instance: str = "blf-prod-hive",
    env: str = "PROD",
) -> str:
    """Build a Hive dataset URN from a `db.table` name using BLF defaults."""
    normalized = table_name.strip().lower()
    if "." not in normalized:
        normalized = f"default.{normalized}"
    return (
        f"urn:li:dataset:(urn:li:dataPlatform:hive,"
        f"{platform_instance}.{normalized},{env})"
    )


def build_target_table_aliases(table_name: str) -> List[str]:
    """Return runtime table names that should be treated as the requested target."""
    normalized = table_name.strip().lower()
    if "." not in normalized:
        normalized = f"default.{normalized}"
    db, table = normalized.rsplit(".", 1)
    aliases: List[str] = []
    if table.startswith("not_verified_"):
        aliases.append(f"{db}.{table.removeprefix('not_verified_')}")
    else:
        aliases.append(f"{db}.not_verified_{table}")
    return [alias for alias in aliases if alias != normalized]


def strip_markdown_code_fence(value: str) -> str:
    """Remove a single Markdown code fence wrapper if present."""
    match = _CODE_FENCE_RE.match(value or "")
    if not match:
        return (value or "").strip()
    return match.group("body").strip()


def structured_properties_url(gms_url: str, dataset_urn: str) -> str:
    encoded = urllib.parse.quote(dataset_urn, safe="")
    return f"{gms_url.rstrip('/')}/openapi/v3/entity/dataset/{encoded}/structuredProperties"


def schema_metadata_url(gms_url: str, dataset_urn: str) -> str:
    encoded = urllib.parse.quote(dataset_urn, safe="")
    return f"{gms_url.rstrip('/')}/openapi/v3/entity/dataset/{encoded}/schemaMetadata"


def fetch_structured_properties(
    gms_url: str,
    dataset_urn: str,
    token: Optional[str] = None,
    timeout_sec: int = 60,
) -> Dict[str, Any]:
    """Fetch the structuredProperties aspect for one dataset."""
    req = urllib.request.Request(
        structured_properties_url(gms_url, dataset_urn),
        method="GET",
        headers={"Accept": "application/json"},
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET structuredProperties HTTP {exc.code}: {detail}") from exc


def fetch_schema_fields(
    gms_url: str,
    dataset_urn: str,
    token: Optional[str] = None,
    timeout_sec: int = 60,
) -> List[str]:
    """Fetch dataset schema fields from DataHub in DDL order."""
    req = urllib.request.Request(
        schema_metadata_url(gms_url, dataset_urn),
        method="GET",
        headers={"Accept": "application/json"},
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return extract_schema_field_names(payload)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET schemaMetadata HTTP {exc.code}: {detail}") from exc


def fetch_schema_fields_with_partitions(
    gms_url: str,
    dataset_urn: str,
    token: Optional[str] = None,
    timeout_sec: int = 60,
) -> Tuple[List[str], List[str]]:
    """Fetch dataset schema fields and partition fields from DataHub."""
    req = urllib.request.Request(
        schema_metadata_url(gms_url, dataset_urn),
        method="GET",
        headers={"Accept": "application/json"},
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return (
                extract_schema_field_names(payload),
                extract_schema_partition_field_names(payload),
            )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET schemaMetadata HTTP {exc.code}: {detail}") from exc


def extract_schema_field_names(payload: Dict[str, Any]) -> List[str]:
    """Extract field names from an OpenAPI schemaMetadata payload."""
    fields = _schema_fields_from_payload(payload)
    if not isinstance(fields, list):
        return []

    names: List[str] = []
    seen: set[str] = set()
    for field in fields:
        if not isinstance(field, dict):
            continue
        name = _schema_field_name(field)
        key = name.lower()
        if name and key not in seen:
            names.append(name.lower())
            seen.add(key)
    return names


def extract_schema_partition_field_names(payload: Dict[str, Any]) -> List[str]:
    """Extract fields whose DataHub schema type marks them as Partition Key."""
    fields = _schema_fields_from_payload(payload)
    if not isinstance(fields, list):
        return []

    names: List[str] = []
    seen: set[str] = set()
    for field in fields:
        if not isinstance(field, dict):
            continue
        name = _schema_field_name(field)
        key = name.lower()
        if not name or key in seen:
            continue
        if _schema_field_is_partition_key(field):
            names.append(name.lower())
            seen.add(key)
    return names


def _schema_fields_from_payload(payload: Dict[str, Any]) -> Any:
    current: Any = payload
    if isinstance(current, dict) and "schemaMetadata" in current:
        current = current["schemaMetadata"]
    if isinstance(current, dict) and "value" in current:
        current = current["value"]
    return current.get("fields") if isinstance(current, dict) else None


def _schema_field_name(field: Dict[str, Any]) -> str:
    raw_path = field.get("fieldPath")
    if isinstance(raw_path, str) and raw_path.strip():
        tail = raw_path.rsplit(".", 1)[-1].strip()
        if tail:
            return tail.lower()
    raw_name = field.get("fieldName")
    if isinstance(raw_name, str):
        return raw_name.strip().lower()
    return ""


def _schema_field_is_partition_key(field: Dict[str, Any]) -> bool:
    candidates: List[str] = []
    for key in ("nativeDataType", "type", "fieldType"):
        value = field.get(key)
        if isinstance(value, str):
            candidates.append(value)
        elif isinstance(value, dict):
            for nested_key in ("type", "nativeDataType", "name"):
                nested_value = value.get(nested_key)
                if isinstance(nested_value, str):
                    candidates.append(nested_value)
    return any(value.strip().lower() == "partition key" for value in candidates)


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


def extract_property_texts(payload: Dict[str, Any]) -> Tuple[str, str]:
    """Read Etl Script / Execute Shell text from a structuredProperties payload."""
    by_urn: Dict[str, str] = {}
    for assignment in _iter_property_assignments(payload):
        property_urn = assignment.get("propertyUrn")
        if isinstance(property_urn, str):
            by_urn[property_urn] = _first_string_value(assignment)
    etl_script = strip_markdown_code_fence(by_urn.get(URN_ETL_SCRIPT, ""))
    execute_shell = strip_markdown_code_fence(by_urn.get(URN_EXECUTE_SHELL, ""))
    return etl_script, execute_shell


def extract_data_availability_flags(payload: Dict[str, Any]) -> List[str]:
    """Read Data Availability Flag values from structuredProperties."""
    flags: List[str] = []
    for assignment in _iter_property_assignments(payload):
        if assignment.get("propertyUrn") != URN_DATA_AVAILABILITY_FLAG:
            continue
        values = assignment.get("values")
        if not isinstance(values, list):
            continue
        for value in values:
            raw = ""
            if isinstance(value, dict) and isinstance(value.get("string"), str):
                raw = value["string"]
            elif isinstance(value, str):
                raw = value
            for token in re.split(r"[,，/、\n]+", raw):
                token = token.strip()
                if token and token not in flags:
                    flags.append(token)
    return flags


def has_confirmed_field_lineage(payload: Dict[str, Any]) -> bool:
    """Return True when Data Availability Flag marks field lineage as confirmed."""
    return "字段血缘" in extract_data_availability_flags(payload)


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


def _unresolved_variables_in_text(text: str, variables: Dict[str, str]) -> List[str]:
    unresolved: set[str] = set()
    for regex in (_BRACED_VAR_RE, _DOLLAR_VAR_RE, _JINJA_VAR_RE):
        for match in regex.finditer(text or ""):
            name = match.group(1)
            if name not in variables and name not in _IGNORED_UNRESOLVED_VARS:
                unresolved.add(name)
    return sorted(unresolved)


def parse_execute_shell_variables(execute_shell: str) -> Dict[str, str]:
    """Parse runtime variables from Execute Shell for ETL placeholder resolution."""
    raw = strip_markdown_code_fence(execute_shell or "")
    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError:
        tokens = raw.split()

    variables: Dict[str, str] = {}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "export" and i + 1 < len(tokens):
            parsed = _parse_assignment_token(tokens[i + 1])
            if parsed:
                variables[parsed[0]] = parsed[1]
                i += 2
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
            elif option and i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                variables[option] = _strip_shell_quotes(tokens[i + 1])
                i += 1
            i += 1
            continue

        i += 1

    _add_date_derivatives(variables)
    return variables


def resolve_etl_script_with_execute_shell(etl_script: str, execute_shell: str) -> str:
    """Replace ETL placeholders with variables parsed from Execute Shell."""
    variables = parse_execute_shell_variables(execute_shell)
    variables.update(_parse_script_assignment_variables(etl_script or "", variables))
    return _replace_variables_in_text(etl_script or "", variables)


def resolve_etl_script_with_debug(
    etl_script: str,
    execute_shell: str,
) -> tuple[str, Dict[str, str], Dict[str, str], Dict[str, str], List[str]]:
    execute_shell_variables = parse_execute_shell_variables(execute_shell)
    script_variables = _parse_script_assignment_variables(
        etl_script or "",
        execute_shell_variables,
    )
    all_variables = dict(execute_shell_variables)
    all_variables.update(script_variables)
    resolved = _replace_variables_in_text(etl_script or "", all_variables)
    unresolved = _unresolved_variables_in_text(resolved, all_variables)
    return resolved, execute_shell_variables, script_variables, all_variables, unresolved


def _parse_script_assignment_variables(
    etl_script: str,
    initial_variables: Dict[str, str],
) -> Dict[str, str]:
    variables = dict(initial_variables)
    parsed_from_script: Dict[str, str] = {}
    for raw_line in etl_script.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("function "):
            continue
        if any(token in line for token in (" ", "\t", "$(", "`")) and not re.match(
            r"^[A-Za-z_][A-Za-z0-9_]*=", line
        ):
            continue
        parsed = _parse_assignment_token(line)
        if not parsed:
            continue
        key, raw_value = parsed
        value = _replace_variables_in_text(raw_value, variables)
        variables[key] = value
        parsed_from_script[key] = value
    return parsed_from_script


def _replace_variables_in_text(text: str, variables: Dict[str, str]) -> str:
    def replace_var(match: re.Match[str]) -> str:
        name = match.group(1)
        return variables.get(name, match.group(0))

    resolved = text or ""
    for _ in range(10):
        previous = resolved
        resolved = _BRACED_VAR_RE.sub(replace_var, resolved)
        resolved = _DOLLAR_VAR_RE.sub(replace_var, resolved)
        resolved = _JINJA_VAR_RE.sub(replace_var, resolved)
        if resolved == previous:
            break
    return resolved


def _looks_like_python_script(etl_script: str, execute_shell: str) -> bool:
    head = "\n".join((etl_script or "").lstrip().splitlines()[:20])
    shell = execute_shell or ""
    return bool(
        re.search(r"\bpython(?:\d(?:\.\d+)?)?\b|\.py\b", shell)
        or head.startswith("#!/usr/bin/env python")
        or head.startswith("#!/usr/bin/python")
        or re.search(r"^\s*(import|from|class|def)\s+", head, re.M)
        or "__name__ == '__main__'" in etl_script
        or '__name__ == "__main__"' in etl_script
    )


def trim_shell_job_to_entry_functions(etl_script: str, table_name: str) -> str:
    """Keep shell globals plus functions reachable from `<table>_run`."""
    table = table_name.strip().lower().split(".")[-1]
    if not table:
        return etl_script
    entry = f"{table}_run"
    functions = _extract_shell_functions(etl_script)
    if entry not in functions:
        return etl_script

    lines = etl_script.splitlines()
    reachable = _reachable_shell_functions(functions, entry, lines)
    kept_lines: List[str] = []
    line_no = 0
    while line_no < len(lines):
        owning_function = None
        for name, span in functions.items():
            if span[0] <= line_no <= span[1]:
                owning_function = name
                break
        if owning_function is None:
            kept_lines.append(lines[line_no])
            line_no += 1
            continue
        start, end = functions[owning_function]
        if owning_function in reachable:
            kept_lines.extend(lines[start : end + 1])
        line_no = end + 1

    compacted = "\n".join(kept_lines).strip()
    return compacted or etl_script


def trim_shell_job_to_entry_functions_with_debug(
    etl_script: str,
    table_name: str,
) -> tuple[str, str, List[str], List[str]]:
    table = table_name.strip().lower().split(".")[-1]
    if not table:
        return etl_script, "", [], []
    entry = f"{table}_run"
    functions = _extract_shell_functions(etl_script)
    if entry not in functions:
        return etl_script, "", [], []
    lines = etl_script.splitlines()
    reachable = sorted(_reachable_shell_functions(functions, entry, lines))
    removed = sorted(set(functions) - set(reachable))
    return trim_shell_job_to_entry_functions(etl_script, table_name), entry, reachable, removed


def strip_commented_sql_for_llm(etl_script: str) -> str:
    """Remove commented SQL fragments before sending ETL text to the LLM."""
    without_block_comments = re.sub(r"/\*[\s\S]*?\*/", "", etl_script or "")
    cleaned_lines: List[str] = []
    for raw_line in without_block_comments.splitlines():
        stripped = raw_line.lstrip()
        if stripped.startswith("--") or stripped.startswith("#"):
            continue
        cleaned_lines.append(re.sub(r"\s+--.*$", "", raw_line.rstrip()))
    return "\n".join(cleaned_lines).strip()


def _extract_shell_functions(etl_script: str) -> Dict[str, tuple[int, int]]:
    lines = etl_script.splitlines()
    functions: Dict[str, tuple[int, int]] = {}
    idx = 0
    while idx < len(lines):
        match = _SHELL_FUNCTION_START_RE.match(lines[idx])
        if not match:
            idx += 1
            continue
        name = match.group(1)
        depth = lines[idx].count("{") - lines[idx].count("}")
        end = idx
        while end + 1 < len(lines) and depth > 0:
            end += 1
            depth += lines[end].count("{") - lines[end].count("}")
        functions[name] = (idx, end)
        idx = end + 1
    return functions


def _reachable_shell_functions(
    functions: Dict[str, tuple[int, int]],
    entry: str,
    lines: List[str],
) -> set[str]:
    function_names = set(functions)
    reachable = {entry}
    queue = [entry]
    while queue:
        current = queue.pop(0)
        start, end = functions[current]
        body = "\n".join(lines[start : end + 1])
        for candidate in function_names - reachable:
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(candidate)}(?![A-Za-z0-9_])", body):
                reachable.add(candidate)
                queue.append(candidate)
    return reachable


def prepare_etl_script_for_llm(
    etl_script: str,
    execute_shell: str,
    table_name: str,
) -> str:
    """Resolve runtime variables and remove unreachable shell-job functions before LLM."""
    resolved = resolve_etl_script_with_execute_shell(etl_script, execute_shell)
    if _looks_like_python_script(resolved, execute_shell):
        return strip_commented_sql_for_llm(resolved)
    return strip_commented_sql_for_llm(
        trim_shell_job_to_entry_functions(resolved, table_name)
    )


def prepare_etl_script_for_llm_with_debug(
    etl_script: str,
    execute_shell: str,
    table_name: str,
) -> FieldLineagePreparationDebug:
    resolved, shell_vars, script_vars, all_vars, unresolved = resolve_etl_script_with_debug(
        etl_script=etl_script,
        execute_shell=execute_shell,
    )
    is_python = _looks_like_python_script(resolved, execute_shell)
    if is_python:
        llm_input = strip_commented_sql_for_llm(resolved)
        entry_function = ""
        reachable_functions: List[str] = []
        removed_functions: List[str] = []
    else:
        llm_input, entry_function, reachable_functions, removed_functions = (
            trim_shell_job_to_entry_functions_with_debug(resolved, table_name)
        )
        llm_input = strip_commented_sql_for_llm(llm_input)
    return FieldLineagePreparationDebug(
        original_etl_script=etl_script or "",
        execute_shell=execute_shell or "",
        execute_shell_variables=shell_vars,
        script_variables=script_vars,
        all_variables=all_vars,
        resolved_etl_script=resolved,
        llm_input_etl_script=llm_input,
        unresolved_variables=unresolved,
        is_python_script=is_python,
        entry_function=entry_function,
        reachable_functions=reachable_functions,
        removed_functions=removed_functions,
    )


def missing_field_lineage_source_reason(payload: Dict[str, Any]) -> Optional[str]:
    """Return a user-facing skip reason when field lineage cannot be parsed (no Etl Script)."""
    etl_script, execute_shell = extract_property_texts(payload)
    if etl_script.strip():
        return None
    missing_labels = []
    if not etl_script.strip():
        missing_labels.append(LABEL_ETL_SCRIPT)
    if not execute_shell.strip():
        missing_labels.append(LABEL_EXECUTE_SHELL)
    labels = ", ".join(missing_labels) if missing_labels else LABEL_ETL_SCRIPT
    return f"structured property 无内容，已跳过字段血缘解析: {labels}"


def extract_field_lineage_input(
    dataset_urn: str,
    table_name: str,
    payload: Dict[str, Any],
) -> FieldLineageInput:
    """Convert a DataHub structuredProperties response into LLM input."""
    etl_script, execute_shell = extract_property_texts(payload)
    resolved_etl_script = prepare_etl_script_for_llm(
        etl_script=etl_script,
        execute_shell=execute_shell,
        table_name=table_name,
    )
    return FieldLineageInput(
        dataset_urn=dataset_urn,
        table_name=table_name.strip().lower(),
        etl_script=resolved_etl_script,
        execute_shell=execute_shell,
        target_table_aliases=build_target_table_aliases(table_name),
    )


def extract_field_lineage_input_with_debug(
    dataset_urn: str,
    table_name: str,
    payload: Dict[str, Any],
) -> tuple[FieldLineageInput, FieldLineagePreparationDebug]:
    """Convert structuredProperties into LLM input and expose intermediate artifacts."""
    etl_script, execute_shell = extract_property_texts(payload)
    debug = prepare_etl_script_for_llm_with_debug(
        etl_script=etl_script,
        execute_shell=execute_shell,
        table_name=table_name,
    )
    source_input = FieldLineageInput(
        dataset_urn=dataset_urn,
        table_name=table_name.strip().lower(),
        etl_script=debug.llm_input_etl_script,
        execute_shell=execute_shell,
        target_table_aliases=build_target_table_aliases(table_name),
    )
    return source_input, debug


def _artifact_suffix(debug: FieldLineagePreparationDebug) -> str:
    return ".py" if debug.is_python_script else ".sql"


def write_field_lineage_debug_artifacts(
    output_dir: Path,
    source_input: FieldLineageInput,
    debug: FieldLineagePreparationDebug,
) -> List[str]:
    """Persist ETL preparation artifacts next to the exported Excel workbook."""
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = _artifact_suffix(debug)
    files = {
        "execute_shell.txt": debug.execute_shell,
        f"original_etl_script{suffix}": debug.original_etl_script,
        f"resolved_etl_script{suffix}": debug.resolved_etl_script,
        f"llm_input_etl_script{suffix}": debug.llm_input_etl_script,
        "runtime_variables.json": json.dumps(
            {
                "execute_shell_variables": debug.execute_shell_variables,
                "script_variables": debug.script_variables,
                "all_variables": debug.all_variables,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        "processing_summary.json": json.dumps(
            {
                "table_name": source_input.table_name,
                "dataset_urn": source_input.dataset_urn,
                "is_python_script": debug.is_python_script,
                "entry_function": debug.entry_function,
                "reachable_functions": debug.reachable_functions,
                "removed_functions": debug.removed_functions,
                "unresolved_variables": debug.unresolved_variables,
                "target_schema_fields": source_input.target_schema_fields,
                "target_schema_field_count": len(source_input.target_schema_fields),
                "target_table_aliases": source_input.target_table_aliases,
                "original_etl_script_chars": len(debug.original_etl_script),
                "resolved_etl_script_chars": len(debug.resolved_etl_script),
                "llm_input_etl_script_chars": len(debug.llm_input_etl_script),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
    }
    written: List[str] = []
    for name, content in files.items():
        (output_dir / name).write_text(content, encoding="utf-8")
        written.append(name)
    return written


def read_field_lineage_input(
    gms_url: str,
    table_name: str,
    token: Optional[str] = None,
    platform_instance: str = "blf-prod-hive",
    env: str = "PROD",
) -> FieldLineageInput:
    """Fetch structured properties and return field-lineage LLM input."""
    dataset_urn = make_hive_dataset_urn(table_name, platform_instance, env)
    payload = fetch_structured_properties(gms_url, dataset_urn, token=token)
    source_input = extract_field_lineage_input(dataset_urn, table_name, payload)
    try:
        schema_fields = fetch_schema_fields(gms_url, dataset_urn, token=token)
    except RuntimeError:
        schema_fields = []
    return FieldLineageInput(
        dataset_urn=source_input.dataset_urn,
        table_name=source_input.table_name,
        etl_script=source_input.etl_script,
        execute_shell=source_input.execute_shell,
        target_schema_fields=schema_fields,
        target_table_aliases=source_input.target_table_aliases,
    )
