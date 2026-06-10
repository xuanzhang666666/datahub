"""独立字段级血缘写入 DataHub（仅 fineGrainedLineages，不改表级 upstreams）。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Set, Tuple

from .field_lineage_datahub_reader import (
    extract_data_availability_flags,
    fetch_schema_fields_with_partitions,
    fetch_structured_properties,
    make_hive_dataset_urn,
)
from .field_lineage_constants import (
    has_explained_transform,
    is_constant_transform_expression,
    is_placeholder_transform_text,
)
from .field_lineage_models import FieldLineageCandidate
from .field_lineage_policy import (
    is_partition_field,
    is_self_dependency,
    normalize_source_field_name,
    normalize_source_table_name,
)
from .structured_properties import URN_DATA_AVAILABILITY_FLAG, sort_data_availability_flags
from .models import TableRef

try:
    from datahub.emitter import mce_builder as builder
    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.emitter.rest_emitter import DatahubRestEmitter
    from datahub.metadata.schema_classes import (
        FineGrainedLineageClass,
        FineGrainedLineageDownstreamTypeClass,
        FineGrainedLineageUpstreamTypeClass,
        UpstreamLineageClass,
    )

    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False


@dataclass(frozen=True)
class GroupedFieldLineage:
    """同一目标字段的多条审核行合并结果。"""

    target_table: str
    target_field: str
    sources: Tuple[Tuple[str, str], ...]
    transform_operation: str
    transform_explanation: str
    confidence: str


def _parse_table_ref(table_name: str) -> TableRef:
    parts = table_name.strip().lower().split(".")
    if len(parts) < 2:
        raise ValueError(f"表名必须为 db.table 格式: {table_name}")
    return TableRef(db=".".join(parts[:-1]), table=parts[-1])


def _confidence_score(confidence: str) -> float:
    mapping = {"HIGH": 1.0, "MEDIUM": 0.8, "LOW": 0.5}
    return mapping.get((confidence or "").upper(), 0.7)


def _pick_transform_expression(rows: List[FieldLineageCandidate]) -> str:
    exprs = [
        r.transform_expression.strip()
        for r in rows
        if r.transform_expression.strip()
        and not is_placeholder_transform_text(r.transform_expression)
    ]
    if not exprs:
        return ""
    unique = list(dict.fromkeys(exprs))
    if len(unique) == 1:
        return unique[0]
    return max(unique, key=len)


def _pick_transform_explanation(rows: List[FieldLineageCandidate]) -> str:
    explanations = [
        r.transform_explanation.strip()
        for r in rows
        if r.transform_explanation.strip()
        and not is_placeholder_transform_text(r.transform_explanation)
    ]
    if not explanations:
        return ""
    unique = list(dict.fromkeys(explanations))
    if len(unique) == 1:
        return unique[0]
    return max(unique, key=len)


def build_transform_operation_for_ui(transform_expression: str, transform_explanation: str) -> str:
    """Combine Chinese explanation and SQL expression for DataHub LOGIC display."""
    expression = transform_expression.strip()
    explanation = transform_explanation.strip()
    if explanation and expression:
        return f"/* 中文解释：{explanation} */\n{expression}"
    if explanation:
        return f"/* 中文解释：{explanation} */"
    return expression


def group_approved_rows(rows: List[FieldLineageCandidate]) -> List[GroupedFieldLineage]:
    """按 (target_table, target_field) 合并；多来源合成 FIELD_SET。"""
    buckets: Dict[Tuple[str, str], List[FieldLineageCandidate]] = {}
    for row in rows:
        if is_partition_field(row.target_field):
            continue
        if is_self_dependency(row.target_table, row.source_table):
            continue
        key = (row.target_table.strip().lower(), row.target_field.strip().lower())
        buckets.setdefault(key, []).append(row)

    grouped: List[GroupedFieldLineage] = []
    for (target_table, target_field), items in sorted(buckets.items()):
        sources: List[Tuple[str, str]] = []
        seen: Set[Tuple[str, str]] = set()
        for item in items:
            src = (
                normalize_source_table_name(item.source_table),
                normalize_source_field_name(item.source_field),
            )
            if not src[0] and not src[1] and is_constant_transform_expression(
                item.transform_expression
            ):
                continue
            if not src[0] or not src[1]:
                continue
            if src in seen:
                continue
            seen.add(src)
            sources.append(src)
        has_constant_transform = any(
            is_constant_transform_expression(item.transform_expression) for item in items
        )
        has_explained_sourceless_transform = any(
            not item.source_table.strip()
            and not item.source_field.strip()
            and has_explained_transform(
                item.transform_expression,
                item.transform_explanation,
            )
            for item in items
        )
        if not sources and not has_constant_transform and not has_explained_sourceless_transform:
            continue
        confidences = [i.confidence for i in items if i.confidence]
        grouped.append(
            GroupedFieldLineage(
                target_table=target_table,
                target_field=target_field,
                sources=tuple(sources),
                transform_operation=build_transform_operation_for_ui(
                    _pick_transform_expression(items),
                    _pick_transform_explanation(items),
                ),
                transform_explanation=_pick_transform_explanation(items),
                confidence=confidences[0] if confidences else "HIGH",
            )
        )
    return grouped


def build_fine_grained_lineage_class(
    group: GroupedFieldLineage,
    platform_instance: str = "blf-prod-hive",
    env: str = "PROD",
) -> FineGrainedLineageClass:
    if not _SDK_AVAILABLE:
        raise RuntimeError("需要安装 acryl-datahub 才能写入字段血缘")

    downstream_urn = make_hive_dataset_urn(group.target_table, platform_instance, env)
    upstream_field_urns = []
    for source_table, source_field in group.sources:
        source_urn = make_hive_dataset_urn(source_table, platform_instance, env)
        upstream_field_urns.append(builder.make_schema_field_urn(source_urn, source_field))

    downstream_field_urn = builder.make_schema_field_urn(downstream_urn, group.target_field)
    kwargs = {
        "upstreamType": FineGrainedLineageUpstreamTypeClass.FIELD_SET,
        "upstreams": upstream_field_urns,
        "downstreamType": FineGrainedLineageDownstreamTypeClass.FIELD,
        "downstreams": [downstream_field_urn],
        "confidenceScore": _confidence_score(group.confidence),
    }
    if group.transform_operation:
        kwargs["transformOperation"] = group.transform_operation
    return FineGrainedLineageClass(**kwargs)


def _field_name_from_schema_field_urn(urn: str) -> Optional[str]:
    if not urn.startswith("urn:li:schemaField:"):
        return None
    inner = urn[len("urn:li:schemaField:") :]
    if "," not in inner:
        return None
    return inner.rsplit(",", 1)[-1].strip().rstrip(")").strip().lower()


def _downstream_fields_from_fine_grained_lineages(
    entries: List[FineGrainedLineageClass],
) -> Set[str]:
    fields: Set[str] = set()
    for entry in entries:
        for downstream in entry.downstreams or []:
            field = _field_name_from_schema_field_urn(downstream)
            if field:
                fields.add(field)
    return fields


def _read_fine_grained_lineages_with_retry(
    graph: object,
    downstream_urn: str,
    *,
    expected_downstream_fields: Set[str],
    max_attempts: int = 5,
    sleep_seconds: int = 3,
) -> List[FineGrainedLineageClass]:
    """Read persisted fine-grained lineage, retrying while GMS has not exposed it."""
    last_entries: List[FineGrainedLineageClass] = []
    for attempt in range(1, max_attempts + 1):
        existing = graph.get_aspect(
            entity_urn=downstream_urn,
            aspect_type=UpstreamLineageClass,
        )
        last_entries = (
            list(existing.fineGrainedLineages)
            if existing and existing.fineGrainedLineages
            else []
        )
        covered = _downstream_fields_from_fine_grained_lineages(last_entries)
        if expected_downstream_fields.issubset(covered):
            return last_entries
        if attempt < max_attempts:
            time.sleep(sleep_seconds)
    return last_entries


def _patch_data_availability_flags(
    gms_url: str,
    dataset_urn: str,
    flags: List[str],
    token: Optional[str] = None,
) -> None:
    body = {
        "patch": [
            {
                "op": "add",
                "path": f"/properties/{URN_DATA_AVAILABILITY_FLAG}",
                "value": {
                    "propertyUrn": URN_DATA_AVAILABILITY_FLAG,
                    "values": [{"string": flag} for flag in flags],
                },
            }
        ],
        "arrayPrimaryKeys": {"properties": ["propertyUrn"]},
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{gms_url.rstrip('/')}/openapi/v3/entity/dataset/"
        f"{urllib.parse.quote(dataset_urn, safe='')}/structuredProperties",
        data=data,
        method="PATCH",
        headers={
            "Content-Type": "application/json-patch+json",
            "Accept": "application/json",
        },
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"PATCH data_availability_flag HTTP {exc.code}: {detail}") from exc


def verify_field_lineage_completeness(
    gms_url: str,
    target_table: str,
    fine_grained_lineages: List[FineGrainedLineageClass],
    token: Optional[str] = None,
    platform_instance: str = "blf-prod-hive",
    env: str = "PROD",
    *,
    mark_available: bool = True,
) -> Dict[str, object]:
    """Check whether every non-partition DDL field has field lineage, then mark it."""
    dataset_urn = make_hive_dataset_urn(target_table, platform_instance, env)
    all_schema_fields, partition_fields = fetch_schema_fields_with_partitions(
        gms_url,
        dataset_urn,
        token=token,
    )
    partition_field_set = {field.strip().lower() for field in partition_fields if field.strip()}
    schema_fields = [
        field.lower()
        for field in all_schema_fields
        if not is_partition_field(field) and field.strip().lower() not in partition_field_set
    ]
    schema_field_set = set(schema_fields)
    covered_fields = _downstream_fields_from_fine_grained_lineages(fine_grained_lineages)
    missing_fields = [
        field for field in schema_fields if field not in covered_fields
    ]
    extra_fields = sorted(covered_fields - schema_field_set)
    is_complete = bool(schema_fields) and not missing_fields

    result: Dict[str, object] = {
        "dataset_urn": dataset_urn,
        "schema_field_count": len(schema_fields),
        "partition_fields": sorted(partition_field_set),
        "covered_field_count": len(schema_field_set.intersection(covered_fields)),
        "missing_fields": missing_fields,
        "extra_downstream_fields": extra_fields,
        "is_complete": is_complete,
        "marked_data_availability_flag": False,
    }
    if not schema_fields:
        result["reason"] = "schemaMetadata 无非分区字段，无法确认字段血缘完整"
        return result
    if not is_complete:
        result["reason"] = "存在 DDL 非分区字段没有字段血缘"
        return result

    payload = fetch_structured_properties(gms_url, dataset_urn, token=token)
    final_flags = sort_data_availability_flags(
        [*extract_data_availability_flags(payload), "字段血缘"]
    )
    result["data_availability_flags_after"] = final_flags
    if mark_available:
        _patch_data_availability_flags(gms_url, dataset_urn, final_flags, token=token)
        result["marked_data_availability_flag"] = True
    return result


def merge_fine_grained_lineages(
    existing: Optional[List[FineGrainedLineageClass]],
    new_entries: List[FineGrainedLineageClass],
    *,
    clear_existing: bool = True,
) -> List[FineGrainedLineageClass]:
    """Merge or replace fine-grained lineage entries from reviewed Excel rows."""
    if clear_existing:
        return list(new_entries)
    new_downstreams = {
        downstream
        for entry in new_entries
        for downstream in (entry.downstreams or [])
    }
    merged = [
        entry
        for entry in (existing or [])
        if not set(entry.downstreams or []).intersection(new_downstreams)
    ]
    seen = {
        (
            tuple(entry.upstreams or []),
            tuple(entry.downstreams or []),
            entry.transformOperation or "",
        )
        for entry in merged
    }
    for entry in new_entries:
        key = (
            tuple(entry.upstreams or []),
            tuple(entry.downstreams or []),
            entry.transformOperation or "",
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(entry)
    return merged


def write_approved_field_lineages(
    gms_url: str,
    approved_rows: List[FieldLineageCandidate],
    token: Optional[str] = None,
    platform_instance: str = "blf-prod-hive",
    env: str = "PROD",
    *,
    dry_run: bool = False,
    clear_existing: bool = True,
) -> Dict[str, object]:
    """将审核通过的 Excel 行写入各目标表的 fineGrainedLineages。"""
    if not dry_run and not _SDK_AVAILABLE:
        raise RuntimeError("需要安装 acryl-datahub 才能写入字段血缘")

    grouped_all = group_approved_rows(approved_rows)
    by_table: Dict[str, List[GroupedFieldLineage]] = {}
    for group in grouped_all:
        by_table.setdefault(group.target_table, []).append(group)

    results: Dict[str, object] = {"tables": {}, "dry_run": dry_run}
    for target_table, groups in sorted(by_table.items()):
        downstream_urn = make_hive_dataset_urn(target_table, platform_instance, env)
        table_result = {
            "target_table": target_table,
            "downstream_urn": downstream_urn,
            "field_count": len(groups),
            "with_transform_operation": sum(1 for g in groups if g.transform_operation),
            "replacement_mode": (
                "clear_import_replace_all_fine_grained_lineages_for_table"
                if clear_existing
                else "merge_update_replace_same_downstream_fields_keep_others"
            ),
            "fields": [
                {
                    "target_field": g.target_field,
                    "source_count": len(g.sources),
                    "transform_operation": g.transform_operation,
                    "transform_explanation": g.transform_explanation,
                    "sources": list(g.sources),
                }
                for g in groups
            ],
        }
        if dry_run:
            results["tables"][target_table] = table_result
            continue

        new_entries = [
            build_fine_grained_lineage_class(g, platform_instance, env) for g in groups
        ]

        from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig

        emitter = DatahubRestEmitter(gms_url, token=token)

        graph = DataHubGraph(DatahubClientConfig(server=gms_url.rstrip("/"), token=token))
        existing: Optional[UpstreamLineageClass] = graph.get_aspect(
            entity_urn=downstream_urn,
            aspect_type=UpstreamLineageClass,
        )
        existing_fg_count = (
            len(existing.fineGrainedLineages)
            if existing and existing.fineGrainedLineages
            else 0
        )
        merged_fg = merge_fine_grained_lineages(
            list(existing.fineGrainedLineages) if existing and existing.fineGrainedLineages else None,
            new_entries,
            clear_existing=clear_existing,
        )
        table_result["cleared_existing_fine_grained_count"] = existing_fg_count

        aspect = UpstreamLineageClass(
            upstreams=list(existing.upstreams) if existing and existing.upstreams else [],
            fineGrainedLineages=merged_fg,
        )
        mcp = MetadataChangeProposalWrapper(entityUrn=downstream_urn, aspect=aspect)
        emitter.emit_mcp(mcp)
        table_result["written"] = True
        table_result["fine_grained_count_after_merge"] = len(merged_fg)
        expected_downstream_fields = {g.target_field for g in groups}
        verify_retry_attempts = int(os.getenv("FIELD_LINEAGE_VERIFY_RETRY_ATTEMPTS", "5"))
        verify_retry_sleep_seconds = int(
            os.getenv("FIELD_LINEAGE_VERIFY_RETRY_SLEEP_SECONDS", "3")
        )
        persisted_fg = _read_fine_grained_lineages_with_retry(
            graph,
            downstream_urn,
            expected_downstream_fields=expected_downstream_fields,
            max_attempts=verify_retry_attempts,
            sleep_seconds=verify_retry_sleep_seconds,
        )
        persisted_downstream_fields = _downstream_fields_from_fine_grained_lineages(
            persisted_fg
        )
        table_result["field_lineage_verify_retry"] = {
            "max_attempts": verify_retry_attempts,
            "sleep_seconds": verify_retry_sleep_seconds,
            "expected_downstream_fields": sorted(expected_downstream_fields),
            "persisted_downstream_fields": sorted(persisted_downstream_fields),
            "persisted_fine_grained_count": len(persisted_fg),
        }
        table_result["field_lineage_completeness"] = verify_field_lineage_completeness(
            gms_url,
            target_table,
            persisted_fg,
            token=token,
            platform_instance=platform_instance,
            env=env,
        )
        results["tables"][target_table] = table_result

    return results
