#!/usr/bin/env python3
"""Audit current DataHub Hive table-level lineage quality.

The audit is read-only. It scans DataHub dataset upstreamLineage aspects and
checks whether lineage endpoints still exist in DataHub and Hive.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

from .field_lineage_datahub_reader import make_hive_dataset_urn
from .hive_table_existence import query_hive_existing_fqtns
from .query_upstream_lineage import urn_to_table_name

DEFAULT_PLATFORM_INSTANCE = "blf-prod-hive"
DEFAULT_ENV = "PROD"

ISSUE_TARGET_NOT_IN_HIVE = "target_not_in_hive"
ISSUE_UPSTREAM_DATAHUB_ENTITY_NOT_FOUND = "upstream_datahub_entity_not_found"
ISSUE_UPSTREAM_NOT_IN_HIVE = "upstream_not_in_hive"
ISSUE_SELF_DEPENDENCY = "self_dependency"
ISSUE_UPSTREAM_NOT_HIVE_PLATFORM = "upstream_not_hive_platform"
ISSUE_NON_ODS_NO_UPSTREAM = "non_ods_no_upstream"

_LINEAGE_TABLE_PREFIXES_WITH_UPSTREAM = (
    "app_",
    "dm_",
    "dim_",
    "dw_",
    "dwa_",
    "dwd_",
    "mid_",
    "pdw_",
)


@dataclass(frozen=True)
class QualityIssue:
    issue_type: str
    target_table: str
    upstream_table: str = ""
    target_urn: str = ""
    upstream_urn: str = ""
    reason: str = ""
    detail: str = ""


@dataclass(frozen=True)
class AuditResult:
    summary: Dict[str, Any]
    issues: List[QualityIssue]


def _is_hive_dataset_urn(urn: str, platform_instance: str, env: str) -> bool:
    return (
        urn.startswith("urn:li:dataset:(urn:li:dataPlatform:hive,")
        and f",{env})" in urn
        and f",{platform_instance}." in urn
    )


def _should_expect_upstream(table_name: str) -> bool:
    table = table_name.rsplit(".", 1)[-1].lower()
    return table.startswith(_LINEAGE_TABLE_PREFIXES_WITH_UPSTREAM)


def _collect_needed_hive_tables(
    dataset_urns: Set[str],
    upstreams_by_target: Mapping[str, Sequence[str]],
    platform_instance: str,
    env: str,
) -> Set[str]:
    needed = {
        urn_to_table_name(urn, platform_instance).lower()
        for urn in dataset_urns
        if _is_hive_dataset_urn(urn, platform_instance, env)
    }
    for upstreams in upstreams_by_target.values():
        for upstream_urn in upstreams:
            if _is_hive_dataset_urn(upstream_urn, platform_instance, env):
                needed.add(urn_to_table_name(upstream_urn, platform_instance).lower())
    return {x for x in needed if "." in x}


def evaluate_lineage_quality(
    dataset_urns: Set[str],
    upstreams_by_target: Mapping[str, Sequence[str]],
    hive_existing_fqtns: Set[str],
    *,
    platform_instance: str = DEFAULT_PLATFORM_INSTANCE,
    env: str = DEFAULT_ENV,
    include_no_upstream: bool = False,
    check_datahub_entity_existence: bool = True,
) -> AuditResult:
    """Classify lineage quality issues from already fetched DataHub/Hive facts."""
    normalized_dataset_urns = {urn.strip() for urn in dataset_urns if urn and urn.strip()}
    normalized_hive = {name.strip().lower() for name in hive_existing_fqtns if name.strip()}
    issues: List[QualityIssue] = []
    upstream_edge_count = 0

    for target_urn in sorted(normalized_dataset_urns):
        target_table = urn_to_table_name(target_urn, platform_instance).lower()
        upstreams = list(upstreams_by_target.get(target_urn, []))

        if target_table not in normalized_hive:
            issues.append(
                QualityIssue(
                    issue_type=ISSUE_TARGET_NOT_IN_HIVE,
                    target_table=target_table,
                    target_urn=target_urn,
                    reason="目标表在 Hive information_schema 中不存在",
                )
            )

        if include_no_upstream and not upstreams and _should_expect_upstream(target_table):
            issues.append(
                QualityIssue(
                    issue_type=ISSUE_NON_ODS_NO_UPSTREAM,
                    target_table=target_table,
                    target_urn=target_urn,
                    reason="非 ODS/源层表没有 DataHub 表级上游血缘",
                )
            )

        for upstream_urn in upstreams:
            if not upstream_urn:
                continue
            upstream_edge_count += 1
            upstream_table = urn_to_table_name(upstream_urn, platform_instance).lower()

            if upstream_urn == target_urn or upstream_table == target_table:
                issues.append(
                    QualityIssue(
                        issue_type=ISSUE_SELF_DEPENDENCY,
                        target_table=target_table,
                        upstream_table=upstream_table,
                        target_urn=target_urn,
                        upstream_urn=upstream_urn,
                        reason="目标表依赖自身",
                    )
                )
                continue

            if not _is_hive_dataset_urn(upstream_urn, platform_instance, env):
                issues.append(
                    QualityIssue(
                        issue_type=ISSUE_UPSTREAM_NOT_HIVE_PLATFORM,
                        target_table=target_table,
                        upstream_table=upstream_table,
                        target_urn=target_urn,
                        upstream_urn=upstream_urn,
                        reason="上游不是当前 Hive platform instance/env 的 dataset",
                    )
                )
                continue

            if check_datahub_entity_existence and upstream_urn not in normalized_dataset_urns:
                issues.append(
                    QualityIssue(
                        issue_type=ISSUE_UPSTREAM_DATAHUB_ENTITY_NOT_FOUND,
                        target_table=target_table,
                        upstream_table=upstream_table,
                        target_urn=target_urn,
                        upstream_urn=upstream_urn,
                        reason="上游 DataHub dataset 实体不存在或已软删除",
                    )
                )

            if upstream_table not in normalized_hive:
                issues.append(
                    QualityIssue(
                        issue_type=ISSUE_UPSTREAM_NOT_IN_HIVE,
                        target_table=target_table,
                        upstream_table=upstream_table,
                        target_urn=target_urn,
                        upstream_urn=upstream_urn,
                        reason="上游表在 Hive information_schema 中不存在",
                    )
                )

    issue_counts = Counter(issue.issue_type for issue in issues)
    summary: Dict[str, Any] = {
        "scanned_dataset_count": len(normalized_dataset_urns),
        "lineage_target_count": len(upstreams_by_target),
        "upstream_edge_count": upstream_edge_count,
        "issue_count": len(issues),
        "issue_counts_by_type": dict(sorted(issue_counts.items())),
        "datahub_entity_existence_check_complete": check_datahub_entity_existence,
    }
    return AuditResult(summary=summary, issues=issues)


def _make_graph(gms_url: str, token: Optional[str]) -> Any:
    try:
        from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
    except ImportError as exc:
        raise RuntimeError("需要安装 acryl-datahub: pip install acryl-datahub") from exc

    return DataHubGraph(DatahubClientConfig(server=gms_url.rstrip("/"), token=token))


def fetch_dataset_urns(
    graph: Any,
    *,
    platform_instance: str,
    env: str,
    query: str,
    batch_size: int,
    max_datasets: int,
) -> Set[str]:
    urns: Set[str] = set()
    for urn in graph.get_urns_by_filter(
        entity_types=["dataset"],
        platform="hive",
        platform_instance=platform_instance,
        env=env,
        query=query,
        batch_size=batch_size,
    ):
        if isinstance(urn, str) and urn:
            urns.add(urn)
        if max_datasets > 0 and len(urns) >= max_datasets:
            break
    return urns


def fetch_upstreams_by_target(graph: Any, dataset_urns: Iterable[str]) -> Dict[str, List[str]]:
    try:
        from datahub.metadata.schema_classes import UpstreamLineageClass
    except ImportError as exc:
        raise RuntimeError("需要安装 acryl-datahub: pip install acryl-datahub") from exc

    out: Dict[str, List[str]] = {}
    for idx, target_urn in enumerate(sorted(dataset_urns), start=1):
        if idx % 500 == 0:
            print(f"[INFO] 已读取 upstreamLineage: {idx} 个 dataset", file=sys.stderr)
        try:
            lineage = graph.get_aspect(
                entity_urn=target_urn,
                aspect_type=UpstreamLineageClass,
            )
        except Exception as exc:
            print(f"[WARN] 读取 upstreamLineage 失败: {target_urn} err={exc}", file=sys.stderr)
            continue
        upstreams = []
        for upstream in getattr(lineage, "upstreams", []) or []:
            upstream_urn = getattr(upstream, "dataset", None)
            if isinstance(upstream_urn, str) and upstream_urn:
                upstreams.append(upstream_urn)
        if upstreams:
            out[target_urn] = upstreams
    return out


def write_jsonl(path: Path, result: AuditResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"summary": result.summary}, ensure_ascii=False) + "\n")
        for issue in result.issues:
            fh.write(json.dumps(asdict(issue), ensure_ascii=False) + "\n")


def write_xlsx(path: Path, result: AuditResult) -> None:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError as exc:
        raise SystemExit("请先安装: pip install openpyxl") from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws_summary = wb.active
    ws_summary.title = "summary"
    ws_summary.append(["指标", "值"])
    for key, value in result.summary.items():
        if isinstance(value, dict):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        ws_summary.append([key, value])

    ws_issues = wb.create_sheet("issues")
    headers = [
        "问题类型",
        "目标表",
        "上游表",
        "原因",
        "明细",
        "目标URN",
        "上游URN",
    ]
    ws_issues.append(headers)
    for issue in result.issues:
        ws_issues.append(
            [
                issue.issue_type,
                issue.target_table,
                issue.upstream_table,
                issue.reason,
                issue.detail,
                issue.target_urn,
                issue.upstream_urn,
            ]
        )

    for ws in (ws_summary, ws_issues):
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="D9E1F2")
        for col_idx, _ in enumerate(ws[1], start=1):
            col_letter = get_column_letter(col_idx)
            max_len = 10
            for cell in ws[col_letter]:
                if cell.value:
                    max_len = min(max(max_len, len(str(cell.value))), 80)
            ws.column_dimensions[col_letter].width = max_len + 2

    wb.save(path)


def run(
    *,
    gms_url: str,
    token: Optional[str],
    platform_instance: str,
    env: str,
    query: str,
    batch_size: int,
    max_datasets: int,
    hive_chunk_size: int,
    include_no_upstream: bool,
    jsonl_path: Path,
    xlsx_path: Path,
) -> AuditResult:
    graph = _make_graph(gms_url, token)
    print(
        f"[INFO] 扫描 DataHub datasets: platform=hive platform_instance={platform_instance} env={env} query={query!r}"
    )
    dataset_urns = fetch_dataset_urns(
        graph,
        platform_instance=platform_instance,
        env=env,
        query=query,
        batch_size=batch_size,
        max_datasets=max_datasets,
    )
    print(f"[INFO] DataHub dataset 数量: {len(dataset_urns)}")

    upstreams_by_target = fetch_upstreams_by_target(graph, dataset_urns)
    edge_count = sum(len(v) for v in upstreams_by_target.values())
    print(f"[INFO] 有表级上游血缘的目标表: {len(upstreams_by_target)} edge_count={edge_count}")

    needed_hive_tables = _collect_needed_hive_tables(
        dataset_urns,
        upstreams_by_target,
        platform_instance,
        env,
    )
    print(f"[INFO] Hive information_schema 待校验表数量: {len(needed_hive_tables)}")
    hive_existing = query_hive_existing_fqtns(needed_hive_tables, chunk_size=hive_chunk_size)
    print(f"[INFO] Hive information_schema 存在表数量: {len(hive_existing)}")

    result = evaluate_lineage_quality(
        dataset_urns=dataset_urns,
        upstreams_by_target=upstreams_by_target,
        hive_existing_fqtns=hive_existing,
        platform_instance=platform_instance,
        env=env,
        include_no_upstream=include_no_upstream,
        check_datahub_entity_existence=max_datasets <= 0,
    )
    write_jsonl(jsonl_path, result)
    write_xlsx(xlsx_path, result)

    print(f"[INFO] issue_count={result.summary['issue_count']}")
    for issue_type, count in result.summary["issue_counts_by_type"].items():
        print(f"[INFO]   {issue_type}: {count}")
    print(f"[DONE] jsonl: {jsonl_path}")
    print(f"[DONE] xlsx: {xlsx_path}")
    return result


def _parse_bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gms-url", default=os.getenv("DATAHUB_GMS_URL", "http://localhost:8080"))
    p.add_argument("--token", default=os.getenv("DATAHUB_GMS_TOKEN"))
    p.add_argument(
        "--platform-instance",
        default=os.getenv("BLF_DATAHUB_PLATFORM_INSTANCE", DEFAULT_PLATFORM_INSTANCE),
    )
    p.add_argument("--env", default=os.getenv("DATAHUB_ENV", DEFAULT_ENV))
    p.add_argument("--query", default=os.getenv("LINEAGE_QUALITY_QUERY", "*"))
    p.add_argument("--batch-size", type=int, default=int(os.getenv("BATCH_SIZE", "2000")))
    p.add_argument("--max-datasets", type=int, default=int(os.getenv("MAX_DATASETS", "0")))
    p.add_argument(
        "--hive-chunk-size",
        type=int,
        default=int(os.getenv("HIVE_CHUNK_SIZE", "1000")),
        help="Hive information_schema IN 查询分批大小，默认 1000",
    )
    p.add_argument(
        "--include-no-upstream",
        action="store_true",
        default=_parse_bool_env("CHECK_NO_UPSTREAM", False),
        help="额外检查非 ODS/源层表无上游血缘；可能产生较多结果",
    )
    p.add_argument("--jsonl", required=True, type=Path)
    p.add_argument("--xlsx", required=True, type=Path)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    try:
        run(
            gms_url=args.gms_url,
            token=args.token,
            platform_instance=args.platform_instance,
            env=args.env,
            query=args.query,
            batch_size=args.batch_size,
            max_datasets=args.max_datasets,
            hive_chunk_size=args.hive_chunk_size,
            include_no_upstream=args.include_no_upstream,
            jsonl_path=args.jsonl,
            xlsx_path=args.xlsx,
        )
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
