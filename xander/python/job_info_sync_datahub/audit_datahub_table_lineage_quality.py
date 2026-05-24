#!/usr/bin/env python3
"""Audit current DataHub Hive table-level lineage quality.

The audit is read-only. It scans DataHub dataset upstreamLineage aspects and
checks whether lineage endpoints still exist in DataHub and Hive.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

from .field_lineage_datahub_reader import make_hive_dataset_urn
from .hive_table_existence import query_hive_existing_fqtns
from .query_upstream_lineage import urn_to_table_name
from .structured_properties import URN_ETL_SCRIPT

DEFAULT_PLATFORM_INSTANCE = "blf-prod-hive"
DEFAULT_ENV = "PROD"

ISSUE_TARGET_NOT_IN_HIVE = "target_not_in_hive"
ISSUE_UPSTREAM_DATAHUB_ENTITY_NOT_FOUND = "upstream_datahub_entity_not_found"
ISSUE_UPSTREAM_NOT_IN_HIVE = "upstream_not_in_hive"
ISSUE_SELF_DEPENDENCY = "self_dependency"
ISSUE_UPSTREAM_NOT_HIVE_PLATFORM = "upstream_not_hive_platform"
ISSUE_NON_ODS_NO_UPSTREAM = "non_ods_no_upstream"
ISSUE_MISSING_ETL_SCRIPT = "missing_etl_script"
ISSUE_VIEW_NO_UPSTREAM = "view_no_upstream"
ISSUE_ETL_SCRIPT_NO_UPSTREAM = "etl_script_no_upstream"

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
_TABLE_PREFIXES_ALLOW_MISSING_ETL = ("ods", "ai", "app")
_TABLE_PREFIXES_REQUIRE_ETL = ("dwa", "dwd", "pdim", "dim", "pdw", "mid", "dm", "dw")


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


def _should_expect_etl_script(table_name: str) -> bool:
    table = table_name.rsplit(".", 1)[-1].lower()
    if table.startswith(_TABLE_PREFIXES_ALLOW_MISSING_ETL):
        return False
    return table.startswith(_TABLE_PREFIXES_REQUIRE_ETL)


def _strip_markdown_code_fence(value: str) -> str:
    text = (value or "").strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2:
            return "\n".join(lines[1:-1]).strip()
    return text


def _has_meaningful_etl_script(metadata: str) -> bool:
    try:
        payload = json.loads(metadata)
    except json.JSONDecodeError:
        return False
    properties = payload.get("properties")
    if not isinstance(properties, list):
        return False
    for prop in properties:
        if not isinstance(prop, dict) or prop.get("propertyUrn") != URN_ETL_SCRIPT:
            continue
        values = prop.get("values")
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, dict):
                text = value.get("string", "")
            elif isinstance(value, str):
                text = value
            else:
                text = ""
            stripped = _strip_markdown_code_fence(text)
            if stripped and stripped != "无":
                return True
    return False


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
    view_dataset_urns: Optional[Set[str]] = None,
    etl_script_dataset_urns: Optional[Set[str]] = None,
) -> AuditResult:
    """Classify lineage quality issues from already fetched DataHub/Hive facts."""
    normalized_dataset_urns = {urn.strip() for urn in dataset_urns if urn and urn.strip()}
    normalized_hive = {name.strip().lower() for name in hive_existing_fqtns if name.strip()}
    normalized_views = {urn.strip() for urn in (view_dataset_urns or set()) if urn and urn.strip()}
    normalized_etl = {urn.strip() for urn in (etl_script_dataset_urns or set()) if urn and urn.strip()}
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

        if (
            etl_script_dataset_urns is not None
            and target_urn not in normalized_views
            and _should_expect_etl_script(target_table)
            and target_urn not in normalized_etl
        ):
            issues.append(
                QualityIssue(
                    issue_type=ISSUE_MISSING_ETL_SCRIPT,
                    target_table=target_table,
                    target_urn=target_urn,
                    reason="非 view 且按表名前缀应维护 Etl Script，但结构化属性 blf.data.warehouse.etl_script 缺失或无有效内容",
                    detail="允许缺失前缀: ods, ai, app；要求存在前缀: dwa, dwd, pdim, dim, pdw, mid, dm, dw",
                )
            )

        if target_urn in normalized_views and not upstreams:
            issues.append(
                QualityIssue(
                    issue_type=ISSUE_VIEW_NO_UPSTREAM,
                    target_table=target_table,
                    target_urn=target_urn,
                    reason="view dataset 没有 DataHub 表级上游血缘",
                )
            )

        if (
            etl_script_dataset_urns is not None
            and target_urn not in normalized_views
            and target_urn in normalized_etl
            and not upstreams
            and _should_expect_etl_script(target_table)
        ):
            issues.append(
                QualityIssue(
                    issue_type=ISSUE_ETL_SCRIPT_NO_UPSTREAM,
                    target_table=target_table,
                    target_urn=target_urn,
                    reason="Etl Script 有有效内容且按表名前缀应建立上游血缘，但没有 DataHub 表级上游表",
                    detail="允许无上游前缀: ods, ai, app；要求有上游前缀: dwa, dwd, pdim, dim, pdw, mid, dm, dw",
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
        "etl_script_presence_check_complete": etl_script_dataset_urns is not None,
        "view_dataset_count": len(normalized_views),
        "dataset_with_etl_script_count": len(normalized_etl),
    }
    return AuditResult(summary=summary, issues=issues)


def _mysql_host_base_cmd() -> List[str]:
    mysql_bin = os.getenv("DATAHUB_MYSQL_BIN") or shutil.which("mysql") or "/opt/anaconda3/bin/mysql"
    host = os.getenv("DATAHUB_MYSQL_HOST", "127.0.0.1")
    port = os.getenv("DATAHUB_MYSQL_PORT", "3306")
    user = os.getenv("DATAHUB_MYSQL_USER", "root")
    password = os.getenv("DATAHUB_MYSQL_PASSWORD", "datahub")
    database = os.getenv("DATAHUB_MYSQL_DATABASE", "datahub")
    return [
        mysql_bin,
        "-h",
        host,
        "-P",
        port,
        f"-u{user}",
        f"-p{password}",
        "-D",
        database,
        "--batch",
        "--raw",
        "--skip-column-names",
    ]


def _mysql_docker_base_cmd() -> List[str]:
    container = os.getenv("DATAHUB_MYSQL_CONTAINER", "datahub-mysql-1")
    user = os.getenv("DATAHUB_MYSQL_USER", "root")
    password = os.getenv("DATAHUB_MYSQL_PASSWORD", "datahub")
    database = os.getenv("DATAHUB_MYSQL_DATABASE", "datahub")
    return [
        "docker",
        "exec",
        container,
        "mysql",
        f"-u{user}",
        f"-p{password}",
        "-D",
        database,
        "--batch",
        "--raw",
        "--skip-column-names",
    ]


def _mysql_candidate_cmds() -> List[List[str]]:
    mode = os.getenv("DATAHUB_MYSQL_MODE", "host").strip().lower()
    if mode == "docker":
        return [_mysql_docker_base_cmd()]
    if mode == "host":
        return [_mysql_host_base_cmd(), _mysql_docker_base_cmd()]
    return [_mysql_host_base_cmd(), _mysql_docker_base_cmd()]


def _run_mysql_query(sql: str) -> str:
    errors: List[str] = []
    for base_cmd in _mysql_candidate_cmds():
        proc = subprocess.run(base_cmd + ["-e", sql], capture_output=True)
        if proc.returncode == 0:
            return proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        stdout = proc.stdout.decode("utf-8", errors="replace").strip()
        errors.append(f"cmd={base_cmd[0]} exit={proc.returncode}: {(stderr or stdout or 'no output')[:1000]}")
    raise RuntimeError("MySQL 查询失败: " + " | ".join(errors)[:2000])


def _parse_tab_rows(output: str, expected_columns: int) -> List[tuple[str, ...]]:
    rows: List[tuple[str, ...]] = []
    for raw in output.splitlines():
        if not raw.strip():
            continue
        parts = raw.split("\t", expected_columns - 1)
        if len(parts) == expected_columns:
            rows.append(tuple(parts))
    return rows


def fetch_view_dataset_urns_from_mysql(platform_instance: str, env: str) -> Set[str]:
    sql = (
        "select urn "
        "from metadata_aspect_v2 "
        "where aspect='viewProperties' "
        "and version=0 "
        "and urn like 'urn:li:dataset:(urn:li:dataPlatform:hive,"
        f"{platform_instance}.%,{env})'"
    )
    return {row[0] for row in _parse_tab_rows(_run_mysql_query(sql), 1)}


def fetch_etl_script_dataset_urns_from_mysql(platform_instance: str, env: str) -> Set[str]:
    sql = (
        "select urn, metadata "
        "from metadata_aspect_v2 "
        "where aspect='structuredProperties' "
        "and version=0 "
        "and urn like 'urn:li:dataset:(urn:li:dataPlatform:hive,"
        f"{platform_instance}.%,{env})'"
    )
    out: Set[str] = set()
    for urn, metadata in _parse_tab_rows(_run_mysql_query(sql), 2):
        if _has_meaningful_etl_script(metadata):
            out.add(urn)
    return out


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

    try:
        view_dataset_urns = fetch_view_dataset_urns_from_mysql(platform_instance, env)
        etl_script_dataset_urns = fetch_etl_script_dataset_urns_from_mysql(platform_instance, env)
        print(
            f"[INFO] Etl Script 结构化属性检查: views={len(view_dataset_urns)} "
            f"datasets_with_etl_script={len(etl_script_dataset_urns)}"
        )
    except Exception as exc:
        print(f"[WARN] Etl Script 结构化属性检查准备失败，跳过该检查: {exc}", file=sys.stderr)
        view_dataset_urns = None
        etl_script_dataset_urns = None

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
        view_dataset_urns=view_dataset_urns,
        etl_script_dataset_urns=etl_script_dataset_urns,
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
