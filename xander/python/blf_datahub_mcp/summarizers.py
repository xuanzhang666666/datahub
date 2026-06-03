"""Rule-based summarizers for BLF DataHub MCP responses."""

from __future__ import annotations

import re
from typing import Any, Iterable

URN_ETL_SCRIPT = "urn:li:structuredProperty:blf.data.warehouse.etl_script"
URN_EXECUTE_SHELL = "urn:li:structuredProperty:blf.data.schedule.execute_shell"
URN_SCHEDULE_URL = "urn:li:structuredProperty:blf.data.schedule.schedule_url"
URN_DATA_AVAILABILITY_FLAG = (
    "urn:li:structuredProperty:blf.data.warehouse.data_availability_flag"
)
URN_OTHER_REMARK = "urn:li:structuredProperty:blf.data.warehouse.other_remark"

DATA_AVAILABILITY_FLAG_ORDER = ("DDL", "表血缘", "字段血缘")
STRUCTURED_PROPERTY_DEFINITIONS: dict[str, dict[str, Any]] = {
    "etl_script": {
        "urn": URN_ETL_SCRIPT,
        "display_name": "Etl Script",
        "strip_code_fence": True,
    },
    "execute_shell": {
        "urn": URN_EXECUTE_SHELL,
        "display_name": "Execute Shell",
        "strip_code_fence": True,
    },
    "schedule_url": {
        "urn": URN_SCHEDULE_URL,
        "display_name": "Schedule URL",
        "strip_code_fence": False,
    },
    "data_availability_flag": {
        "urn": URN_DATA_AVAILABILITY_FLAG,
        "display_name": "Data Availability Flag",
        "strip_code_fence": False,
    },
    "other_remark": {
        "urn": URN_OTHER_REMARK,
        "display_name": "Other Remark",
        "strip_code_fence": False,
    },
}

_CODE_FENCE_RE = re.compile(r"^\s*```[^\n`]*\n(?P<body>[\s\S]*?)\n?```\s*$")
_TABLE_REF_RE = re.compile(
    r"\b(?:from|join|insert\s+overwrite\s+table|insert\s+into\s+table|"
    r"insert\s+into|alter\s+table)\s+([a-zA-Z_][\w]*\.[a-zA-Z_][\w]*)",
    re.IGNORECASE,
)


def strip_markdown_code_fence(value: str) -> str:
    """Remove one surrounding Markdown code fence."""
    match = _CODE_FENCE_RE.match(value or "")
    if not match:
        return (value or "").strip()
    return match.group("body").strip()


def truncate_text(value: str, max_chars: int) -> dict[str, Any]:
    """Return a text plus truncation metadata."""
    text = value or ""
    if max_chars < 0:
        max_chars = 0
    if len(text) <= max_chars:
        return {"text": text, "chars": len(text), "truncated": False}
    return {
        "text": text[:max_chars],
        "chars": len(text),
        "truncated": True,
        "omitted_chars": len(text) - max_chars,
    }


def sort_data_availability_flags(flags: Iterable[str]) -> list[str]:
    """Normalize and sort availability flags in stable BLF display order."""
    unique: set[str] = set()
    for flag in flags:
        for token in re.split(r"[,，/、\n]+", str(flag)):
            token = token.strip()
            if token:
                unique.add(token)
    ordered = [flag for flag in DATA_AVAILABILITY_FLAG_ORDER if flag in unique]
    ordered.extend(sorted(unique - set(ordered)))
    return ordered


def iter_structured_property_assignments(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Yield structured property assignments from common OpenAPI response shapes."""
    current: Any = payload
    for key in ("structuredProperties", "value"):
        if isinstance(current, dict) and key in current:
            current = current[key]
    if isinstance(current, dict) and isinstance(current.get("properties"), list):
        for item in current["properties"]:
            if isinstance(item, dict):
                yield item


def first_string_value(assignment: dict[str, Any]) -> str:
    """Read the first string value from one structured property assignment."""
    values = assignment.get("values")
    if not isinstance(values, list):
        return ""
    for value in values:
        if isinstance(value, dict) and isinstance(value.get("string"), str):
            return value["string"]
        if isinstance(value, str):
            return value
    return ""


def extract_structured_properties(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract known BLF structured properties into a compact dict."""
    all_properties = extract_all_structured_properties(payload)
    etl_script = all_properties["known"]["etl_script"]["first_value"]
    execute_shell = all_properties["known"]["execute_shell"]["first_value"]
    flags = sort_data_availability_flags(
        all_properties["known"]["data_availability_flag"]["values"]
    )
    schedule_url = all_properties["known"]["schedule_url"]["first_value"]
    other_remark = all_properties["known"]["other_remark"]["first_value"]
    return {
        "etl_script": etl_script,
        "execute_shell": execute_shell,
        "data_availability_flags": flags,
        "schedule_url": schedule_url,
        "other_remark": other_remark,
        "known_properties": all_properties["known"],
        "raw_property_urns": all_properties["raw_property_urns"],
    }


def extract_all_structured_properties(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract every known BLF structured property and preserve unknown URNs."""
    by_urn = _structured_properties_by_urn(payload)
    known: dict[str, dict[str, Any]] = {}
    for name, definition in STRUCTURED_PROPERTY_DEFINITIONS.items():
        urn = str(definition["urn"])
        raw_values = by_urn.get(urn, [])
        values = [
            strip_markdown_code_fence(value)
            if definition.get("strip_code_fence")
            else value.strip()
            for value in raw_values
        ]
        known[name] = {
            "property_name": name,
            "property_urn": urn,
            "display_name": definition["display_name"],
            "values": values,
            "first_value": values[0] if values else "",
            "exists": bool(values),
            "raw_value_count": len(raw_values),
        }
    known_urns = {
        str(item["urn"]) for item in STRUCTURED_PROPERTY_DEFINITIONS.values()
    }
    unknown_urns = sorted(set(by_urn) - known_urns)
    return {
        "known": known,
        "unknown": {urn: by_urn[urn] for urn in unknown_urns},
        "raw_property_urns": sorted(by_urn),
    }


def normalize_structured_property_name(property_name: str) -> str:
    """Normalize a user/tool property name or URN to a known BLF property key."""
    token = (property_name or "").strip()
    aliases = {
        "etl": "etl_script",
        "script": "etl_script",
        "shell": "execute_shell",
        "execute": "execute_shell",
        "schedule": "schedule_url",
        "url": "schedule_url",
        "availability": "data_availability_flag",
        "availability_flag": "data_availability_flag",
        "data_availability_flags": "data_availability_flag",
        "flag": "data_availability_flag",
        "remark": "other_remark",
        "other": "other_remark",
    }
    if token in STRUCTURED_PROPERTY_DEFINITIONS:
        return token
    if token in aliases:
        return aliases[token]
    for name, definition in STRUCTURED_PROPERTY_DEFINITIONS.items():
        if token == definition["urn"]:
            return name
        if token.endswith(str(definition["urn"]).split(":")[-1]):
            return name
    raise ValueError(
        "property_name must be one of: "
        + ", ".join(sorted(STRUCTURED_PROPERTY_DEFINITIONS))
    )


def _structured_properties_by_urn(payload: dict[str, Any]) -> dict[str, list[str]]:
    by_urn: dict[str, list[str]] = {}
    for assignment in iter_structured_property_assignments(payload):
        property_urn = assignment.get("propertyUrn")
        if not isinstance(property_urn, str):
            continue
        values = []
        raw_values = assignment.get("values")
        if isinstance(raw_values, list):
            for value in raw_values:
                if isinstance(value, dict) and isinstance(value.get("string"), str):
                    values.append(value["string"])
                elif isinstance(value, str):
                    values.append(value)
        by_urn[property_urn] = values
    return by_urn


def extract_table_refs(text: str, limit: int = 30) -> list[str]:
    """Extract visible db.table references from SQL/shell text."""
    refs = []
    seen = set()
    for match in _TABLE_REF_RE.finditer(text or ""):
        ref = match.group(1).lower()
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)
        if len(refs) >= limit:
            break
    return refs


def aspect_value(aspects_payload: dict[str, Any], aspect_name: str) -> dict[str, Any]:
    """Get an aspect value from OpenAPI entity response shapes."""
    aspect = aspects_payload.get(aspect_name)
    if not isinstance(aspect, dict):
        aspects = aspects_payload.get("aspects")
        if isinstance(aspects, dict):
            aspect = aspects.get(aspect_name)
    if not isinstance(aspect, dict):
        return {}
    value = aspect.get("value")
    return value if isinstance(value, dict) else aspect


def summarize_schema_fields(
    schema_metadata: dict[str, Any],
    field_limit: int,
) -> dict[str, Any]:
    """Summarize schema fields from schemaMetadata."""
    fields = schema_metadata.get("fields")
    if not isinstance(fields, list):
        fields = []
    items = []
    for field in fields[: max(field_limit, 0)]:
        if not isinstance(field, dict):
            continue
        items.append(
            {
                "fieldPath": field.get("fieldPath"),
                "nativeDataType": field.get("nativeDataType"),
                "type": field.get("type"),
                "description": field.get("description"),
                "nullable": field.get("nullable"),
            }
        )
    return {
        "total": len(fields),
        "returned": len(items),
        "truncated": len(fields) > len(items),
        "fields": items,
    }


def governance_gaps(
    *,
    has_schema: bool,
    has_documentation: bool,
    has_etl_script: bool,
    has_execute_shell: bool,
    has_lineage: bool | None = None,
    has_owner: bool | None = None,
    has_tags: bool | None = None,
    availability_flags: list[str] | None = None,
) -> list[str]:
    """Build user-facing governance gap messages."""
    gaps = []
    if not has_schema:
        gaps.append("DataHub 未返回 schemaMetadata")
    if not has_documentation:
        gaps.append("缺少 DataHub 表文档或 editable description")
    if not has_etl_script:
        gaps.append("缺少 Etl Script structured property")
    if not has_execute_shell:
        gaps.append("缺少 Execute Shell structured property")
    if has_lineage is False:
        gaps.append("DataHub 表级血缘为空或未摄入")
    if has_owner is False:
        gaps.append("缺少 owner")
    if has_tags is False:
        gaps.append("缺少 tag/glossary/domain 等治理标记")
    flags = availability_flags or []
    for flag in DATA_AVAILABILITY_FLAG_ORDER:
        if flag not in flags:
            gaps.append(f"availability flag 缺少 {flag}")
    return gaps
