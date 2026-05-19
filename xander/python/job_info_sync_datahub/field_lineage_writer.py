"""独立字段级血缘写入 DataHub（仅 fineGrainedLineages，不改表级 upstreams）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from .field_lineage_datahub_reader import make_hive_dataset_urn
from .field_lineage_models import FieldLineageCandidate
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
    exprs = [r.transform_expression.strip() for r in rows if r.transform_expression.strip()]
    if not exprs:
        return ""
    unique = list(dict.fromkeys(exprs))
    if len(unique) == 1:
        return unique[0]
    return max(unique, key=len)


def _pick_transform_explanation(rows: List[FieldLineageCandidate]) -> str:
    explanations = [r.transform_explanation.strip() for r in rows if r.transform_explanation.strip()]
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
        key = (row.target_table.strip().lower(), row.target_field.strip().lower())
        buckets.setdefault(key, []).append(row)

    grouped: List[GroupedFieldLineage] = []
    for (target_table, target_field), items in sorted(buckets.items()):
        sources: List[Tuple[str, str]] = []
        seen: Set[Tuple[str, str]] = set()
        for item in items:
            src = (item.source_table.strip().lower(), item.source_field.strip().lower())
            if not src[0] or not src[1]:
                continue
            if src in seen:
                continue
            seen.add(src)
            sources.append(src)
        if not sources:
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
    return inner.rsplit(",", 1)[-1].strip()


def merge_fine_grained_lineages(
    existing: Optional[List[FineGrainedLineageClass]],
    new_entries: List[FineGrainedLineageClass],
) -> List[FineGrainedLineageClass]:
    """Excel 导入以本次审核结果为准，清空目标表旧字段血缘后再写入。"""
    return list(new_entries)


def write_approved_field_lineages(
    gms_url: str,
    approved_rows: List[FieldLineageCandidate],
    token: Optional[str] = None,
    platform_instance: str = "blf-prod-hive",
    env: str = "PROD",
    *,
    dry_run: bool = False,
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
            "replacement_mode": "replace_all_fine_grained_lineages_for_table",
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
        results["tables"][target_table] = table_result

    return results
