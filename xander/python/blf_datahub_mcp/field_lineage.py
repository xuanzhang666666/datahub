"""Recursive field-lineage tracing helpers for BLF DataHub MCP."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import re
import urllib.parse
from typing import Any, Protocol

from .hive import DEFAULT_PLATFORM_INSTANCE, make_hive_dataset_urn, normalize_hive_table
from .summarizers import extract_structured_properties, truncate_text

SOURCE_LAYER_REACHED = "SOURCE_LAYER_REACHED"
MISSING_FIELD_LINEAGE = "MISSING_FIELD_LINEAGE"
NO_UPSTREAM = "NO_UPSTREAM"
DATASET_NOT_FOUND = "DATASET_NOT_FOUND"
CYCLE_DETECTED = "CYCLE_DETECTED"
MAX_DEPTH_REACHED = "MAX_DEPTH_REACHED"
INVALID_SCHEMA_FIELD_URN = "INVALID_SCHEMA_FIELD_URN"

COMPLETE = "COMPLETE"
INCOMPLETE = "INCOMPLETE"
CONFIRMED = "CONFIRMED"
RISK_UNCONFIRMED_NODE = "RISK_UNCONFIRMED_NODE"


class FieldLineageClient(Protocol):
    def get_schema_metadata(self, dataset_urn: str) -> dict[str, Any]: ...

    def get_upstream_lineage(self, dataset_urn: str) -> dict[str, Any]: ...

    def get_structured_properties(self, dataset_urn: str) -> dict[str, Any]: ...


@dataclass
class DatasetSnapshot:
    table: str
    dataset_urn: str
    schema_fields: list[str]
    partition_fields: list[str]
    field_descriptions: dict[str, str]
    table_upstreams: list[str]
    field_edges: dict[str, list[tuple[str, str]]]
    field_lineage_confirmed: bool


@dataclass
class TracePath:
    target_table: str
    target_field: str
    target_description: str
    nodes: list[str]
    transform_operations: list[str]
    stop_reason: str
    unconfirmed_nodes: list[str] = field(default_factory=list)
    question: str = ""

    @property
    def final_source(self) -> str:
        return self.nodes[-1] if self.nodes else ""

    @property
    def direct_upstream(self) -> str:
        return self.nodes[1] if len(self.nodes) > 1 else ""

    @property
    def source_layer(self) -> str:
        parsed = _split_field_node(self.final_source)
        if parsed is None:
            return ""
        table_name = parsed[0].rsplit(".", 1)[-1]
        if table_name.startswith("ods_"):
            return "ods"
        if table_name.startswith("pdw_"):
            return "pdw"
        return ""

    @property
    def path_status(self) -> str:
        if self.stop_reason in {SOURCE_LAYER_REACHED, NO_UPSTREAM}:
            return COMPLETE
        return INCOMPLETE

    @property
    def trust_status(self) -> str:
        if self.path_status != COMPLETE:
            return INCOMPLETE
        if self.unconfirmed_nodes:
            return RISK_UNCONFIRMED_NODE
        return CONFIRMED


def trace_hive_field_lineage(
    client: FieldLineageClient,
    *,
    table: str,
    fields: list[str] | None = None,
    max_depth: int = 30,
    max_paths: int = 1000,
    max_transform_chars: int = 1200,
) -> dict[str, Any]:
    """Trace DataHub fine-grained field lineage until ods_/pdw_ table leaves."""
    root_table = normalize_hive_table(table)
    max_depth = max(int(max_depth), 1)
    max_paths = max(int(max_paths), 1)
    max_transform_chars = max(int(max_transform_chars), 0)
    snapshots: dict[str, DatasetSnapshot] = {}
    paths: list[TracePath] = []
    truncated = False

    def load(table_name: str) -> DatasetSnapshot:
        normalized = normalize_hive_table(table_name)
        if normalized not in snapshots:
            snapshots[normalized] = _load_snapshot(client, normalized)
        return snapshots[normalized]

    def append_path(path: TracePath) -> None:
        nonlocal truncated
        if len(paths) >= max_paths:
            truncated = True
            return
        paths.append(path)

    def finish(
        *,
        target_field: str,
        target_description: str,
        nodes: list[str],
        operations: list[str],
        stop_reason: str,
        unconfirmed_nodes: list[str],
        question: str = "",
    ) -> None:
        append_path(
            TracePath(
                target_table=root_table,
                target_field=target_field,
                target_description=target_description,
                nodes=nodes,
                transform_operations=operations,
                stop_reason=stop_reason,
                unconfirmed_nodes=list(dict.fromkeys(unconfirmed_nodes)),
                question=question,
            )
        )

    def walk(
        *,
        target_field: str,
        target_description: str,
        node: str,
        nodes: list[str],
        operations: list[str],
        visited: set[str],
        unconfirmed_nodes: list[str],
        depth: int,
    ) -> None:
        if len(paths) >= max_paths:
            append_path(
                TracePath(
                    target_table=root_table,
                    target_field=target_field,
                    target_description=target_description,
                    nodes=nodes,
                    transform_operations=operations,
                    stop_reason=MAX_DEPTH_REACHED,
                    unconfirmed_nodes=unconfirmed_nodes,
                    question=f"{root_table}.{target_field}: max_paths {max_paths} reached",
                )
            )
            return
        parsed = _split_field_node(node)
        if parsed is None:
            finish(
                target_field=target_field,
                target_description=target_description,
                nodes=nodes,
                operations=operations,
                stop_reason=INVALID_SCHEMA_FIELD_URN,
                unconfirmed_nodes=unconfirmed_nodes,
                question=f"{root_table}.{target_field}: invalid schemaField node {node}",
            )
            return
        table_name, field_name = parsed
        if _source_layer(table_name):
            finish(
                target_field=target_field,
                target_description=target_description,
                nodes=nodes,
                operations=operations,
                stop_reason=SOURCE_LAYER_REACHED,
                unconfirmed_nodes=unconfirmed_nodes,
            )
            return
        try:
            snapshot = load(table_name)
        except Exception as exc:
            finish(
                target_field=target_field,
                target_description=target_description,
                nodes=nodes,
                operations=operations,
                stop_reason=DATASET_NOT_FOUND,
                unconfirmed_nodes=unconfirmed_nodes,
                question=f"{root_table}.{target_field}: dataset unavailable at {node}: {exc}",
            )
            return
        current_unconfirmed = list(unconfirmed_nodes)
        if not snapshot.field_lineage_confirmed:
            current_unconfirmed.append(node)
        if field_name not in snapshot.schema_fields:
            finish(
                target_field=target_field,
                target_description=target_description,
                nodes=nodes,
                operations=operations,
                stop_reason=INVALID_SCHEMA_FIELD_URN,
                unconfirmed_nodes=current_unconfirmed,
                question=f"{root_table}.{target_field}: field {node} not found in schemaMetadata",
            )
            return
        edges = snapshot.field_edges.get(field_name, [])
        if not edges:
            if snapshot.table_upstreams:
                finish(
                    target_field=target_field,
                    target_description=target_description,
                    nodes=nodes,
                    operations=operations,
                    stop_reason=MISSING_FIELD_LINEAGE,
                    unconfirmed_nodes=current_unconfirmed,
                    question=f"{root_table}.{target_field}: {node} has table upstreams but no field lineage",
                )
            else:
                finish(
                    target_field=target_field,
                    target_description=target_description,
                    nodes=nodes,
                    operations=operations,
                    stop_reason=NO_UPSTREAM,
                    unconfirmed_nodes=current_unconfirmed,
                )
            return
        if depth >= max_depth:
            finish(
                target_field=target_field,
                target_description=target_description,
                nodes=nodes,
                operations=operations,
                stop_reason=MAX_DEPTH_REACHED,
                unconfirmed_nodes=current_unconfirmed,
                question=f"{root_table}.{target_field}: max depth {max_depth} reached at {node}",
            )
            return
        for upstream_node, operation in edges:
            if upstream_node in visited:
                finish(
                    target_field=target_field,
                    target_description=target_description,
                    nodes=[*nodes, upstream_node],
                    operations=[*operations, operation],
                    stop_reason=CYCLE_DETECTED,
                    unconfirmed_nodes=current_unconfirmed,
                    question=f"{root_table}.{target_field}: cycle detected at {upstream_node}",
                )
                continue
            walk(
                target_field=target_field,
                target_description=target_description,
                node=upstream_node,
                nodes=[*nodes, upstream_node],
                operations=[*operations, operation],
                visited={*visited, upstream_node},
                unconfirmed_nodes=current_unconfirmed,
                depth=depth + 1,
            )

    root_snapshot = load(root_table)
    target_fields = _requested_fields(root_snapshot, fields)
    for field_name in target_fields:
        node = f"{root_table}.{field_name}"
        walk(
            target_field=field_name,
            target_description=root_snapshot.field_descriptions.get(field_name, ""),
            node=node,
            nodes=[node],
            operations=[],
            visited={node},
            unconfirmed_nodes=[],
            depth=0,
        )

    return _format_result(
        table=root_table,
        max_depth=max_depth,
        max_paths=max_paths,
        max_transform_chars=max_transform_chars,
        paths=paths,
        snapshots=snapshots,
        truncated=truncated,
    )


def _load_snapshot(client: FieldLineageClient, table: str) -> DatasetSnapshot:
    dataset_urn = make_hive_dataset_urn(table)
    schema_payload = client.get_schema_metadata(dataset_urn)
    lineage_payload = client.get_upstream_lineage(dataset_urn)
    structured_payload = client.get_structured_properties(dataset_urn)
    schema_fields = _extract_schema_field_names(schema_payload)
    if not schema_fields:
        raise ValueError("schemaMetadata fields empty")
    lineage_aspect = _unwrap_aspect(lineage_payload, "upstreamLineage")
    field_edges: dict[str, list[tuple[str, str]]] = {}
    for entry in lineage_aspect.get("fineGrainedLineages") or []:
        if not isinstance(entry, dict):
            continue
        operation = entry.get("transformOperation")
        operation_text = operation if isinstance(operation, str) else ""
        upstream_nodes = [
            _schema_field_urn_to_node(upstream_urn)
            for upstream_urn in entry.get("upstreams") or []
        ]
        for downstream_urn in entry.get("downstreams") or []:
            downstream_node = _schema_field_urn_to_node(downstream_urn)
            parsed = _split_field_node(downstream_node or "")
            if parsed is None or parsed[0] != table:
                continue
            field_edges.setdefault(parsed[1], []).extend(
                (upstream_node or f"__invalid__:{upstream_urn}", operation_text)
                for upstream_node, upstream_urn in zip(
                    upstream_nodes, entry.get("upstreams") or []
                )
            )
    structured = extract_structured_properties(structured_payload)
    return DatasetSnapshot(
        table=table,
        dataset_urn=dataset_urn,
        schema_fields=schema_fields,
        partition_fields=_extract_schema_partition_field_names(schema_payload),
        field_descriptions=_extract_schema_field_descriptions(schema_payload),
        table_upstreams=_extract_table_upstreams(lineage_aspect),
        field_edges=field_edges,
        field_lineage_confirmed="字段血缘" in structured["data_availability_flags"],
    )


def _requested_fields(
    snapshot: DatasetSnapshot,
    fields: list[str] | None,
) -> list[str]:
    schema_fields = set(snapshot.schema_fields)
    if fields:
        requested = list(dict.fromkeys(field.strip().lower() for field in fields if field.strip()))
        unknown = sorted(set(requested) - schema_fields)
        if unknown:
            raise ValueError("unknown fields: " + ", ".join(unknown))
        return requested
    partitions = set(snapshot.partition_fields)
    return [field for field in snapshot.schema_fields if field not in partitions]


def _format_result(
    *,
    table: str,
    max_depth: int,
    max_paths: int,
    max_transform_chars: int,
    paths: list[TracePath],
    snapshots: dict[str, DatasetSnapshot],
    truncated: bool,
) -> dict[str, Any]:
    stop_reasons = dict(Counter(path.stop_reason for path in paths))
    grouped: dict[str, list[TracePath]] = {}
    for path in paths:
        grouped.setdefault(path.target_field, []).append(path)
    fields = []
    for field_name, field_paths in sorted(grouped.items()):
        path_statuses = {path.path_status for path in field_paths}
        trust_statuses = {path.trust_status for path in field_paths}
        if path_statuses == {COMPLETE}:
            field_status = COMPLETE
        else:
            field_status = INCOMPLETE
        if INCOMPLETE in trust_statuses:
            trust_status = INCOMPLETE
        elif RISK_UNCONFIRMED_NODE in trust_statuses:
            trust_status = RISK_UNCONFIRMED_NODE
        else:
            trust_status = CONFIRMED
        fields.append(
            {
                "target_table": table,
                "target_field": field_name,
                "target_description": field_paths[0].target_description,
                "path_count": len(field_paths),
                "path_status": field_status,
                "trust_status": trust_status,
                "direct_upstreams": sorted(
                    {path.direct_upstream for path in field_paths if path.direct_upstream}
                ),
                "final_sources": sorted(
                    {path.final_source for path in field_paths if path.final_source}
                ),
                "stop_reasons": dict(Counter(path.stop_reason for path in field_paths)),
                "unconfirmed_nodes": sorted(
                    {node for path in field_paths for node in path.unconfirmed_nodes}
                ),
                "questions": [path.question for path in field_paths if path.question],
            }
        )
    return {
        "table": table,
        "max_depth": max_depth,
        "max_paths": max_paths,
        "field_count": len(fields),
        "path_count": len(paths),
        "truncated": truncated,
        "stop_reasons": stop_reasons,
        "fields": fields,
        "paths": [_format_path(path, max_transform_chars) for path in paths],
        "open_questions": [path.question for path in paths if path.question],
        "visited_tables": sorted(snapshots),
        "unconfirmed_tables": sorted(
            table_name
            for table_name, snapshot in snapshots.items()
            if not snapshot.field_lineage_confirmed
        ),
    }


def _format_path(path: TracePath, max_transform_chars: int) -> dict[str, Any]:
    return {
        "target_table": path.target_table,
        "target_field": path.target_field,
        "nodes": path.nodes,
        "path_length": max(0, len(path.nodes) - 1),
        "direct_upstream": path.direct_upstream,
        "final_source": path.final_source,
        "source_layer": path.source_layer,
        "stop_reason": path.stop_reason,
        "path_status": path.path_status,
        "trust_status": path.trust_status,
        "transform_operations": [
            truncate_text(operation, max_transform_chars)
            for operation in path.transform_operations
            if operation
        ],
        "unconfirmed_nodes": path.unconfirmed_nodes,
        "question": path.question,
    }


def _extract_table_upstreams(lineage_aspect: dict[str, Any]) -> list[str]:
    tables = []
    for item in lineage_aspect.get("upstreams") or []:
        if not isinstance(item, dict) or not isinstance(item.get("dataset"), str):
            continue
        table = _dataset_urn_to_table(item["dataset"])
        if table:
            tables.append(table)
    return sorted(set(tables))


def _extract_schema_field_names(payload: dict[str, Any]) -> list[str]:
    names = []
    seen = set()
    for field in _schema_fields(payload):
        name = _schema_field_name(field)
        if name and name not in seen:
            names.append(name)
            seen.add(name)
    return names


def _extract_schema_partition_field_names(payload: dict[str, Any]) -> list[str]:
    names = []
    for field in _schema_fields(payload):
        name = _schema_field_name(field)
        if name and _schema_field_is_partition(field):
            names.append(name)
    return names


def _extract_schema_field_descriptions(payload: dict[str, Any]) -> dict[str, str]:
    descriptions = {}
    for field in _schema_fields(payload):
        name = _schema_field_name(field)
        description = field.get("description")
        if name:
            descriptions[name] = description if isinstance(description, str) else ""
    return descriptions


def _schema_fields(payload: dict[str, Any]) -> list[dict[str, Any]]:
    current: Any = payload
    for key in ("schemaMetadata", "value"):
        if isinstance(current, dict) and key in current:
            current = current[key]
    fields = current.get("fields") if isinstance(current, dict) else []
    return [field for field in fields if isinstance(field, dict)] if isinstance(fields, list) else []


def _schema_field_name(field: dict[str, Any]) -> str:
    raw_path = field.get("fieldPath")
    if isinstance(raw_path, str) and raw_path.strip():
        cleaned_path = re.sub(r"\[[^\]]+\]\.?", "", raw_path).strip(".")
        if cleaned_path and "." not in cleaned_path:
            return cleaned_path.strip().lower()
    raw_name = field.get("fieldName")
    if isinstance(raw_name, str):
        return raw_name.strip().lower()
    return ""


def _schema_field_is_partition(field: dict[str, Any]) -> bool:
    if field.get("isPartitioningKey") is True:
        return True
    for key in ("nativeDataType", "type", "fieldType"):
        value = field.get(key)
        if isinstance(value, str) and value.strip().lower() == "partition key":
            return True
        if isinstance(value, dict):
            for nested_key in ("type", "nativeDataType", "name"):
                nested_value = value.get(nested_key)
                if isinstance(nested_value, str) and nested_value.strip().lower() == "partition key":
                    return True
    return False


def _unwrap_aspect(payload: dict[str, Any], aspect_name: str) -> dict[str, Any]:
    current: Any = payload
    for key in (aspect_name, "value"):
        if isinstance(current, dict) and key in current:
            current = current[key]
    return current if isinstance(current, dict) else {}


def _schema_field_urn_to_node(urn: Any) -> str | None:
    prefix = "urn:li:schemaField:("
    if not isinstance(urn, str) or not urn.startswith(prefix) or not urn.endswith(")"):
        return None
    inner = urn[len(prefix) : -1]
    if "," not in inner:
        return None
    dataset_urn, field_name = inner.rsplit(",", 1)
    table = _dataset_urn_to_table(dataset_urn.strip())
    field = urllib.parse.unquote(field_name.strip()).lower()
    if not table or not field:
        return None
    return f"{table}.{field}"


def _dataset_urn_to_table(dataset_urn: str) -> str:
    prefix = "urn:li:dataset:("
    if not isinstance(dataset_urn, str) or not dataset_urn.startswith(prefix):
        return ""
    inner = dataset_urn[len(prefix) :].removesuffix(")")
    parts = inner.split(",", 2)
    if len(parts) < 2:
        return ""
    name = parts[1].strip()
    instance_prefix = f"{DEFAULT_PLATFORM_INSTANCE}."
    if not name.startswith(instance_prefix):
        return ""
    return normalize_hive_table(name.removeprefix(instance_prefix))


def _split_field_node(node: str) -> tuple[str, str] | None:
    normalized = (node or "").strip().lower()
    if normalized.startswith("__invalid__:"):
        return None
    parts = normalized.split(".")
    if len(parts) < 3 or not parts[-1]:
        return None
    return ".".join(parts[:-1]), parts[-1]


def _source_layer(table_name: str) -> str:
    table_only = normalize_hive_table(table_name).rsplit(".", 1)[-1]
    if table_only.startswith("ods_"):
        return "ods"
    if table_only.startswith("pdw_"):
        return "pdw"
    return ""
