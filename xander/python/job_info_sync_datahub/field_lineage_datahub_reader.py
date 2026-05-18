"""Read field-lineage inputs from DataHub structuredProperties."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, Optional

from .field_lineage_models import FieldLineageInput
from .structured_properties import URN_ETL_SCRIPT, URN_EXECUTE_SHELL

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


def extract_field_lineage_input(
    dataset_urn: str,
    table_name: str,
    payload: Dict[str, Any],
) -> FieldLineageInput:
    """Convert a DataHub structuredProperties response into LLM input."""
    by_urn: Dict[str, str] = {}
    for assignment in _iter_property_assignments(payload):
        property_urn = assignment.get("propertyUrn")
        if isinstance(property_urn, str):
            by_urn[property_urn] = _first_string_value(assignment)

    etl_script = strip_markdown_code_fence(by_urn.get(URN_ETL_SCRIPT, ""))
    execute_shell = strip_markdown_code_fence(by_urn.get(URN_EXECUTE_SHELL, ""))
    if not etl_script:
        raise RuntimeError(f"dataset 缺少 Etl Script structured property: {dataset_urn}")

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
