#!/usr/bin/env python3
"""Batch table lineage sync using DataHub table structured properties as ETL source."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    __package__ = "job_info_sync_datahub"

from .batch_sync import export_etl_script_snapshot, export_llm_raw_snapshot
from .datahub_writer import DatahubWriter
from .field_lineage_datahub_reader import (
    extract_property_texts,
    fetch_structured_properties,
    make_hive_dataset_urn,
)
from .check_dataset_availability import (
    editable_description_from_payload,
    fetch_aspect_payload,
)
from .lineage_write_policy import (
    _parse_lineage_array,
    append_lineage_audit_jsonl,
    evaluate_documentation_lineage,
    evaluate_llm_only,
    llm_row_to_fqtn,
)
from .models import TableLineage, TableRef
from .logging_utils import get_logger, setup_logging
from .query_upstream_lineage import (
    is_llm_generated_description,
    is_view_dataset,
    normalize_table_name,
)

logger = get_logger("table_lineage_from_dataset_props")

FAIL_DATAHUB_READ = "DATAHUB_READ"
FAIL_LLM = "LLM_ERROR"
FAIL_WRITE = "DATAHUB_WRITE"
FAIL_LINEAGE_MISMATCH = "LINEAGE_MISMATCH"


@dataclass(frozen=True)
class DatasetStructuredEtlSource:
    input_table: str
    dataset_urn: str
    content: str
    source_property: str
    job_file_name: str


def choose_structured_etl_source(
    payload: Dict[str, Any],
    *,
    input_table: str,
    dataset_urn: str,
) -> Optional[DatasetStructuredEtlSource]:
    """Prefer Etl Script, then Execute Shell, from a structuredProperties payload."""
    etl_script, execute_shell = extract_property_texts(payload)
    if etl_script.strip():
        return DatasetStructuredEtlSource(
            input_table=input_table,
            dataset_urn=dataset_urn,
            content=etl_script,
            source_property="Etl Script",
            job_file_name=f"{input_table}.structured_property.job",
        )
    if execute_shell.strip():
        return DatasetStructuredEtlSource(
            input_table=input_table,
            dataset_urn=dataset_urn,
            content=execute_shell,
            source_property="Execute Shell",
            job_file_name=f"{input_table}.structured_property.sh",
        )
    return None


def _table_ref_from_input(input_table: str) -> TableRef:
    if "." in input_table:
        db_name, table_name = input_table.split(".", 1)
        return TableRef(db_name, table_name)
    return TableRef("default", input_table)


def load_table_names(path: str) -> List[str]:
    """Load newline-delimited table names, ignoring blank lines and comments."""
    out: List[str] = []
    seen = set()
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        normalized = normalize_table_name(line)
        if normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def _base_result(input_table: str, dataset_urn: str) -> Dict[str, Any]:
    return {
        "job": input_table,
        "input_table": input_table,
        "dataset_urn": dataset_urn,
        "ts": datetime.now(timezone.utc).isoformat(),
        "status": "OK",
        "fail_category": None,
        "error": None,
        "target_table": None,
        "upstream_count": 0,
        "upstream_count_input_table": 0,
        "upstreams_input_table": None,
        "lineage_sources_deepseek": None,
        "lineage_dropped_by_fqtn": None,
        "lineage_dropped_by_hive": None,
        "field_count": 0,
        "lineage_status": None,
        "lineage_reason": None,
        "write_upstream_lineage": None,
        "lineage_targets_chosen": None,
        "lineage_sources_chosen": None,
        "trust_score": None,
        "etl_file_path": None,
        "etl_file_source": None,
        "etl_file_export_path": None,
        "llm_raw_export_path": None,
        "source_property": None,
        "replace_existing_lineage": None,
    }


def _normalize_table_key(name: str) -> str:
    return normalize_table_name(name).strip().lower()


def find_lineage_for_input_table(
    table_lineages: List[TableLineage],
    input_table: str,
) -> Optional[TableLineage]:
    key = _normalize_table_key(input_table)
    for tl in table_lineages:
        if tl.target.full_name.lower() == key:
            return tl
    return None


def _upstream_names_for_target_from_raw(raw: Dict[str, Any], input_table: str) -> set[str]:
    """LLM 原始 JSON 中，指定目标表对应的上游（未做 fqtn/Hive 过滤）。"""
    key = _normalize_table_key(input_table)
    lineage_items = raw.get("lineage")
    if isinstance(lineage_items, list) and lineage_items:
        names: set[str] = set()
        for item in lineage_items:
            if not isinstance(item, dict):
                continue
            tgt = llm_row_to_fqtn(item.get("target"))
            if tgt != key:
                continue
            for row in item.get("upstreams") or []:
                u = llm_row_to_fqtn(row)
                if u:
                    names.add(u)
        return names
    parsed = _parse_lineage_array(raw)
    tl = find_lineage_for_input_table(parsed, input_table)
    if tl:
        return {u.full_name for u in tl.upstreams}
    return set()


def _fqtn_dropped_for_target(fqtn_meta: Optional[Dict[str, Any]], input_table: str) -> List[Dict[str, str]]:
    if not fqtn_meta:
        return []
    key = _normalize_table_key(input_table)
    out: List[Dict[str, str]] = []
    for block in fqtn_meta.get("stripped_invalid_upstreams") or []:
        if (block.get("target") or "").lower() != key:
            continue
        for item in block.get("removed") or []:
            if isinstance(item, dict):
                out.append({"fqtn": item.get("fqtn", ""), "reason": item.get("reason", "")})
    return out


def _hive_drop_for_target(hive_meta: Optional[Dict[str, Any]], input_table: str) -> Optional[Dict[str, Any]]:
    if not hive_meta:
        return None
    key = _normalize_table_key(input_table)
    for bucket in ("stripped_upstreams", "removed_lineages"):
        for item in hive_meta.get(bucket) or []:
            if (item.get("target") or "").lower() == key:
                return item
    return None


def attach_lineage_diagnosis(
    result: Dict[str, Any],
    *,
    input_table: str,
    table_lineages: List[TableLineage],
    decision,
    llm_raw: Dict[str, Any],
) -> None:
    """把「为何上游变少」的分解写入 result，并修正 upstream_count 为当前表口径。"""
    input_tl = find_lineage_for_input_table(table_lineages, input_table)
    input_upstreams = sorted(u.full_name for u in input_tl.upstreams) if input_tl else []
    llm_upstreams = sorted(_upstream_names_for_target_from_raw(llm_raw, input_table))
    fqtn_dropped = _fqtn_dropped_for_target(decision.fqtn_validation, input_table)
    hive_drop = _hive_drop_for_target(decision.hive_existence, input_table)

    result["upstream_count_input_table"] = len(input_upstreams)
    result["upstreams_input_table"] = input_upstreams
    result["upstream_count"] = len(input_upstreams)
    result["lineage_sources_deepseek"] = llm_upstreams
    result["lineage_dropped_by_fqtn"] = fqtn_dropped
    result["lineage_dropped_by_hive"] = hive_drop
    result["lineage_targets_chosen"] = sorted(decision.selected_targets)
    result["lineage_sources_chosen"] = sorted(decision.selected_upstreams)
    if input_tl:
        result["target_table"] = input_tl.target.full_name


def print_lineage_diagnosis(result: Dict[str, Any]) -> None:
    """在 Jenkins 日志中打印可读的上下游差异分解。"""
    table = result.get("input_table") or "-"
    print(f"  [DIAG] table={table} source_property={result.get('source_property') or '-'}")
    llm_n = len(result.get("lineage_sources_deepseek") or [])
    final_n = result.get("upstream_count_input_table") or 0
    print(f"  [DIAG] upstreams: LLM(未过滤)={llm_n} -> 写入前(当前表)={final_n}")
    if llm_n > final_n:
        print(f"  [DIAG] 共减少 {llm_n - final_n} 个上游，见下方 fqtn / Hive 明细")
    for item in result.get("lineage_dropped_by_fqtn") or []:
        print(f"  [DIAG] fqtn过滤: {item.get('fqtn')} reason={item.get('reason')}")
    hive_drop = result.get("lineage_dropped_by_hive")
    if isinstance(hive_drop, dict):
        print(
            f"  [DIAG] Hive过滤: reason={hive_drop.get('reason')} "
            f"missing={hive_drop.get('missing_upstreams')}"
        )
    if not result.get("upstreams_input_table") and llm_n:
        print(
            "  [DIAG] 提示: LLM 有上游但当前表最终为 0，"
            "可能目标表名与 ETL 中不一致，或整段 lineage 被 Hive 丢弃"
        )
    if result.get("upstreams_input_table"):
        print(f"  [DIAG] 最终上游: {result.get('upstreams_input_table')}")
    if result.get("llm_raw_export_path"):
        print(f"  [DIAG] llm_raw: {result.get('llm_raw_export_path')}")


def _empty_structured_properties_payload() -> Dict[str, Any]:
    return {"structuredProperties": {"value": {"properties": []}}}


def _fetch_structured_properties_or_empty(
    gms_url: str,
    dataset_urn: str,
    token: Optional[str],
) -> Dict[str, Any]:
    try:
        return fetch_structured_properties(gms_url, dataset_urn, token=token)
    except RuntimeError as exc:
        if "HTTP 404" in str(exc):
            return _empty_structured_properties_payload()
        raise


def fetch_existing_upstream_names(
    gms_url: str,
    token: Optional[str],
    target: TableRef,
    platform_instance: str,
    env: str,
) -> set[str]:
    """Read current DataHub upstreamLineage upstream dataset names for a target table."""
    try:
        from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
        from datahub.metadata.schema_classes import UpstreamLineageClass
    except ImportError as exc:
        raise RuntimeError("需要安装 acryl-datahub 才能检查现有 upstreamLineage") from exc

    from .datahub_writer import make_dataset_urn_from_ref
    from .query_upstream_lineage import urn_to_table_name

    target_urn = make_dataset_urn_from_ref(target, platform_instance, env)
    graph = DataHubGraph(DatahubClientConfig(server=gms_url.rstrip("/"), token=token))
    existing = graph.get_aspect(entity_urn=target_urn, aspect_type=UpstreamLineageClass)
    if not existing or not existing.upstreams:
        return set()
    names: set[str] = set()
    for upstream in existing.upstreams:
        upstream_urn = getattr(upstream, "dataset", None)
        if isinstance(upstream_urn, str) and upstream_urn:
            names.add(urn_to_table_name(upstream_urn, platform_instance).lower())
    return names


def compare_existing_lineage(
    table_lineages: List[TableLineage],
    *,
    gms_url: str,
    token: Optional[str],
    platform_instance: str,
    env: str,
) -> Dict[str, Any]:
    """Compare parsed lineage with current DataHub upstreamLineage."""
    expected: set[str] = set()
    existing: set[str] = set()
    target_existing: Dict[str, List[str]] = {}
    for tl in table_lineages:
        expected.update(u.full_name.lower() for u in tl.upstreams)
        current = fetch_existing_upstream_names(
            gms_url,
            token,
            tl.target,
            platform_instance,
            env,
        )
        existing.update(current)
        target_existing[tl.target.full_name] = sorted(current)

    missing = expected - existing
    extra = existing - expected
    return {
        "expected_upstreams": sorted(expected),
        "existing_upstreams": sorted(existing),
        "missing_upstreams": sorted(missing),
        "extra_upstreams": sorted(extra),
        "target_existing_upstreams": target_existing,
        "matched": not missing and not extra,
    }


def sync_one_table(
    table_name: str,
    *,
    gms_url: str,
    token: Optional[str],
    platform_instance: str,
    env: str,
    dry_run: bool,
    replace_existing_lineage: bool,
    llm_timeout_sec: int,
    audit_jsonl: Optional[str],
    batch_output_dir: Optional[str],
    check_existing_lineage: bool = False,
) -> Dict[str, Any]:
    input_table = normalize_table_name(table_name)
    dataset_urn = make_hive_dataset_urn(input_table, platform_instance, env)
    result = _base_result(input_table, dataset_urn)
    result["replace_existing_lineage"] = replace_existing_lineage
    t0 = time.time()

    try:
        if is_view_dataset(gms_url, token, dataset_urn):
            result.update(
                status="SKIP",
                lineage_status="SKIP_VIEW_DATASET",
                lineage_reason="目标 dataset 是 view，表结构化属性批量血缘不处理 view",
                write_upstream_lineage=False,
                error="目标 dataset 是 view，已跳过",
            )
            return result

        doc_payload = fetch_aspect_payload(
            gms_url, dataset_urn, "editableDatasetProperties", token=token
        )
        description = editable_description_from_payload(doc_payload)
        use_documentation = is_llm_generated_description(description)
        source_property: str

        if use_documentation:
            result["source_property"] = "Documentation"
            result["etl_file_path"] = f"datahub_documentation:{input_table}"
            result["etl_file_source"] = "datahub_documentation"
            logger.info(
                "Documentation 含「4. 数据来源」，从该小节解析血缘: table=%s urn=%s",
                input_table,
                dataset_urn,
            )
            result["etl_file_export_path"] = export_etl_script_snapshot(
                batch_output_dir=batch_output_dir,
                job_display_name=input_table,
                job_file_name=f"{input_table}.documentation.md",
                etl_content=description,
            )
            existing_upstream_names = fetch_existing_upstream_names(
                gms_url,
                token,
                _table_ref_from_input(input_table),
                platform_instance,
                env,
            )
            source_property = "Documentation"
            try:
                table_lineages, decision, llm_raw = evaluate_documentation_lineage(
                    input_table,
                    description,
                    existing_upstream_names=existing_upstream_names,
                )
            except Exception as exc:
                result.update(
                    status="FAIL",
                    fail_category=FAIL_DATAHUB_READ,
                    error=str(exc)[:500],
                    lineage_status="DOC_PARSE_ERROR",
                    lineage_reason=str(exc)[:500],
                    write_upstream_lineage=False,
                )
                return result
        else:
            payload = _fetch_structured_properties_or_empty(gms_url, dataset_urn, token)
            source = choose_structured_etl_source(
                payload,
                input_table=input_table,
                dataset_urn=dataset_urn,
            )
            if source is None:
                result.update(
                    status="SKIP",
                    lineage_status="SKIP_NO_STRUCTURED_ETL_SOURCE",
                    lineage_reason=(
                        "无 LLM Documentation，且 structuredProperties 中 "
                        "Etl Script / Execute Shell 均为空"
                    ),
                    write_upstream_lineage=False,
                    error="无可用于血缘解析的 Documentation 或 ETL 内容",
                )
                return result

            result["etl_file_path"] = (
                f"datahub_structured_property:{input_table}:{source.source_property}"
            )
            result["etl_file_source"] = (
                f"datahub_structured_{source.source_property.lower().replace(' ', '_')}"
            )
            source_property = source.source_property
            result["source_property"] = source_property
            logger.info(
                "从 DataHub 表结构化属性读取 ETL 内容: table=%s property=%s urn=%s",
                input_table,
                source.source_property,
                dataset_urn,
            )
            result["etl_file_export_path"] = export_etl_script_snapshot(
                batch_output_dir=batch_output_dir,
                job_display_name=input_table,
                job_file_name=source.job_file_name,
                etl_content=source.content,
            )
            logger.info(
                "最终采用 DataHub 表结构化属性 ETL 内容: table=%s property=%s snapshot=%s",
                input_table,
                source.source_property,
                result["etl_file_export_path"] or "-",
            )

            try:
                table_lineages, decision, llm_raw = evaluate_llm_only(
                    source.content,
                    timeout_sec=llm_timeout_sec,
                    job_file_name=source.job_file_name,
                )
            except Exception as exc:
                result.update(
                    status="FAIL",
                    fail_category=FAIL_LLM,
                    error=str(exc)[:500],
                    lineage_status="LLM_ERROR",
                    lineage_reason=str(exc)[:500],
                    write_upstream_lineage=False,
                )
                return result

        result["llm_raw_export_path"] = export_llm_raw_snapshot(
            batch_output_dir=batch_output_dir,
            job_display_name=input_table,
            llm_raw=llm_raw,
        )
        result["lineage_status"] = decision.status
        result["lineage_reason"] = decision.reason
        result["write_upstream_lineage"] = decision.write_upstream_lineage
        result["trust_score"] = decision.trust_score
        attach_lineage_diagnosis(
            result,
            input_table=input_table,
            table_lineages=table_lineages,
            decision=decision,
            llm_raw=llm_raw,
        )
        print_lineage_diagnosis(result)
        if audit_jsonl:
            append_lineage_audit_jsonl(
                Path(audit_jsonl),
                input_table,
                decision,
                extra={
                    "input_table": input_table,
                    "dataset_urn": dataset_urn,
                    "source_property": source_property,
                },
            )

        if not table_lineages:
            result.update(
                status="SKIP",
                error=decision.reason,
            )
            return result

        if not decision.write_upstream_lineage:
            result.update(
                status="SKIP",
                error=decision.reason,
            )
            return result

        if check_existing_lineage:
            comparison = compare_existing_lineage(
                table_lineages,
                gms_url=gms_url,
                token=token,
                platform_instance=platform_instance,
                env=env,
            )
            result.update(comparison)
            result["write_upstream_lineage"] = False
            if comparison["matched"]:
                result.update(
                    status="OK",
                    lineage_status="CHECK_MATCH",
                    lineage_reason=f"现有 DataHub 表级血缘与 {source_property} 解析结果一致",
                )
            else:
                result.update(
                    status="FAIL",
                    fail_category=FAIL_LINEAGE_MISMATCH,
                    lineage_status="CHECK_MISMATCH",
                    lineage_reason=(
                        f"现有 DataHub 表级血缘与 {source_property} 解析结果不一致；"
                        f"missing={len(comparison['missing_upstreams'])} extra={len(comparison['extra_upstreams'])}"
                    ),
                    error="现有 DataHub 表级血缘与解析结果不一致",
                )
            attach_lineage_diagnosis(
                result,
                input_table=input_table,
                table_lineages=table_lineages,
                decision=decision,
                llm_raw=llm_raw,
            )
            print_lineage_diagnosis(result)
            return result

        writer = DatahubWriter(
            gms_url=gms_url,
            token=token,
            platform_instance=platform_instance,
            env=env,
            dry_run=dry_run,
        )
        ok = writer.write_lineage(
            table_lineages=table_lineages,
            field_lineages=[],
            job_display_name=input_table,
            skip=False,
            replace_existing_lineage=replace_existing_lineage,
        )
        if not ok:
            result.update(
                status="FAIL",
                fail_category=FAIL_WRITE,
                error="DataHub upstreamLineage write failed",
            )
        else:
            result["status"] = "OK"
        attach_lineage_diagnosis(
            result,
            input_table=input_table,
            table_lineages=table_lineages,
            decision=decision,
            llm_raw=llm_raw,
        )
        print_lineage_diagnosis(result)
        return result
    except Exception as exc:
        result.update(
            status="FAIL",
            fail_category=FAIL_DATAHUB_READ,
            error=str(exc)[:500],
            write_upstream_lineage=False,
        )
        return result
    finally:
        result["elapsed"] = round(time.time() - t0, 1)


def append_jsonl(path: str, row: Dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _format_name_list(values: Any, limit: int = 20) -> str:
    if not isinstance(values, list) or not values:
        return "-"
    shown = [str(v) for v in values[:limit]]
    suffix = "" if len(values) <= limit else f" ... (+{len(values) - limit})"
    return ", ".join(shown) + suffix


def summarize_report(path: str) -> None:
    rows: List[Dict[str, Any]] = []
    p = Path(path)
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    ok = [r for r in rows if r.get("status") == "OK"]
    skip = [r for r in rows if r.get("status") == "SKIP"]
    fail = [r for r in rows if r.get("status") == "FAIL"]
    print(f"\n{'=' * 60}")
    print(f"总计: {len(rows)}  OK: {len(ok)}  SKIP: {len(skip)}  FAIL: {len(fail)}")

    if not rows:
        return

    print("\n报告明细:")
    for r in rows:
        table_name = r.get("input_table") or r.get("job") or "-"
        lineage_status = r.get("lineage_status") or "-"
        missing = r.get("missing_upstreams")
        extra = r.get("extra_upstreams")

        if lineage_status == "CHECK_MATCH":
            print(f"  {table_name}")
            continue

        parts = [
            f"[{r.get('status') or '-'}]",
            f"table={table_name}",
            f"lineage={lineage_status}",
            f"target={r.get('target_table') or '-'}",
            f"upstreams={r.get('upstream_count') or 0}",
            f"source_property={r.get('source_property') or '-'}",
        ]
        print("  " + " ".join(parts))
        if r.get("lineage_sources_deepseek") is not None:
            llm_n = len(r.get("lineage_sources_deepseek") or [])
            fin_n = r.get("upstream_count_input_table") or 0
            print(f"      upstreams_llm={llm_n} upstreams_final={fin_n}")
            if llm_n > fin_n:
                print(f"      upstreams_reduced={llm_n - fin_n} (see fqtn/Hive fields in jsonl)")
        for item in r.get("lineage_dropped_by_fqtn") or []:
            print(f"      fqtn_dropped: {item.get('fqtn')} ({item.get('reason')})")
        hive_drop = r.get("lineage_dropped_by_hive")
        if isinstance(hive_drop, dict):
            print(
                f"      hive_dropped: reason={hive_drop.get('reason')} "
                f"missing={_format_name_list(hive_drop.get('missing_upstreams'))}"
            )
        if r.get("upstreams_input_table"):
            print(f"      upstreams_final_list: {_format_name_list(r.get('upstreams_input_table'))}")
        if r.get("lineage_reason"):
            print(f"      reason={str(r.get('lineage_reason'))[:200]}")
        if r.get("error"):
            print(f"      error={str(r.get('error'))[:200]}")
        if isinstance(missing, list):
            print(f"      missing_upstreams({len(missing)}): {_format_name_list(missing)}")
        if isinstance(extra, list):
            print(f"      extra_upstreams({len(extra)}): {_format_name_list(extra)}")
        if r.get("etl_file_export_path"):
            print(f"      etl_snapshot={r.get('etl_file_export_path')}")
        if r.get("llm_raw_export_path"):
            print(f"      llm_raw={r.get('llm_raw_export_path')}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--table-file", required=True, help="每行一个目标表 db.table")
    p.add_argument("--report", required=True, help="结果报告 JSONL")
    p.add_argument("--audit-jsonl", default=None, help="血缘审计 JSONL")
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--datahub-gms", default=os.getenv("DATAHUB_GMS_URL", "http://datahub-gms:8080"))
    p.add_argument("--token", default=os.getenv("DATAHUB_GMS_TOKEN"))
    p.add_argument("--platform-instance", default=os.getenv("BLF_DATAHUB_PLATFORM_INSTANCE", "blf-prod-hive"))
    p.add_argument("--env", default=os.getenv("DATAHUB_ENV", "PROD"))
    p.add_argument("--llm-timeout", type=int, default=90)
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument(
        "--merge-existing-lineage",
        action="store_true",
        help="合并已有 upstreamLineage；未设置时默认替换已有表级血缘",
    )
    p.add_argument(
        "--check-existing-lineage",
        action="store_true",
        help="只检查现有 upstreamLineage 与 structuredProperties 解析结果是否一致，不写入 DataHub",
    )
    return p.parse_args()


def _load_lineage_env_early() -> None:
    raw = os.environ.get("BLF_LINEAGE_ENV_FILE", "").strip()
    if not raw:
        return
    try:
        from .lineage_llm_compare import load_env_file

        load_env_file(Path(raw), override=False)
    except Exception:
        pass


def main() -> int:
    _load_lineage_env_early()
    args = parse_args()
    setup_logging(level=args.log_level)
    logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)
    logging.getLogger("urllib3.util.retry").setLevel(logging.ERROR)

    tables = load_table_names(args.table_file)
    if not tables:
        logger.error("表名单为空: %s", args.table_file)
        return 2

    logger.info(
        "待处理表: %d replace_existing_lineage=%s dry_run=%s",
        len(tables),
        not args.merge_existing_lineage,
        args.dry_run,
    )

    completed = 0
    ok = 0
    skip = 0
    fail = 0
    output_dir = str(Path(args.report).resolve().parent)

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = {
            pool.submit(
                sync_one_table,
                table,
                gms_url=args.datahub_gms,
                token=args.token,
                platform_instance=args.platform_instance,
                env=args.env,
                dry_run=args.dry_run,
                replace_existing_lineage=not args.merge_existing_lineage,
                check_existing_lineage=args.check_existing_lineage,
                llm_timeout_sec=args.llm_timeout,
                audit_jsonl=args.audit_jsonl,
                batch_output_dir=output_dir,
            ): table
            for table in tables
        }
        for fut in as_completed(futures):
            row = fut.result()
            append_jsonl(args.report, row)
            completed += 1
            status = row.get("status")
            if status == "OK":
                ok += 1
            elif status == "SKIP":
                skip += 1
            else:
                fail += 1
            logger.info(
                "[PROGRESS] %d/%d table=%s status=%s target=%s upstreams=%s OK=%d SKIP=%d FAIL=%d lineage=%s source_property=%s",
                completed,
                len(tables),
                row.get("input_table") or row.get("job"),
                status,
                row.get("target_table") or "-",
                row.get("upstream_count") or 0,
                ok,
                skip,
                fail,
                row.get("lineage_status") or "-",
                row.get("source_property") or "-",
            )

    summarize_report(args.report)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
