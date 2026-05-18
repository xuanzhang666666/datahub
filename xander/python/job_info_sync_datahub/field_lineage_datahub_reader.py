"""Read field-lineage inputs from DataHub structuredProperties."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, Optional, Tuple

from .field_lineage_models import FieldLineageInput
from .structured_properties import URN_ETL_SCRIPT, URN_EXECUTE_SHELL

# Jenkins / 人工排查用显示名（与 DataHub structured property 一致）
LABEL_ETL_SCRIPT = "Etl Script (blf.data.warehouse.etl_script)"
LABEL_EXECUTE_SHELL = "Execute Shell (blf.data.schedule.execute_shell)"

_CODE_FENCE_RE = re.compile(r"^\s*```[^\n`]*\n(?P<body>[\s\S]*?)\n?```\s*$")


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


def strip_markdown_code_fence(value: str) -> str:
    """Remove a single Markdown code fence wrapper if present."""
    match = _CODE_FENCE_RE.match(value or "")
    if not match:
        return (value or "").strip()
    return match.group("body").strip()


def structured_properties_url(gms_url: str, dataset_urn: str) -> str:
    encoded = urllib.parse.quote(dataset_urn, safe="")
    return f"{gms_url.rstrip('/')}/openapi/v3/entity/dataset/{encoded}/structuredProperties"


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
    return FieldLineageInput(
        dataset_urn=dataset_urn,
        table_name=table_name.strip().lower(),
        etl_script=etl_script,
        execute_shell=execute_shell,
    )


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
    return extract_field_lineage_input(dataset_urn, table_name, payload)
