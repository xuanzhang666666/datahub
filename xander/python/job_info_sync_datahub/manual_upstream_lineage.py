"""为 Hive 数据集追加一条表级上游血缘（默认合并已有 upstreamLineage）。

**Jenkins / 服务器**：优先用 ``python/scripts/run_add_manual_upstream_lineage.sh``，
在 shell 里 ``export TABLE_NAME=... UPSTREAM_NAME=...`` 再 ``sh .../run_add_manual_upstream_lineage.sh``，
无需 ``PYTHONPATH``（与 ``run_batch_lineage_sync.sh`` 用法一致）。GMS/token 可放在 ``lineage.env``。

**直接调模块**（本地排障）::

  cd xander/python && PYTHONPATH=. python3 -m job_info_sync_datahub.manual_upstream_lineage \\
    --table-name dw.dw_order_v1 --upstream-name dw.dw_order_v1_archive

``--table-name`` / ``--upstream-name`` **必须**为 ``库.表``（至少一段库名 + 表名），不支持仅表名。

默认与现有 ``upstreamLineage`` 合并（按 dataset URN 去重），并尽量保留
``fineGrainedLineages``。``--replace`` 则只保留本次指定的单条上游（慎用）。

若上游表在 DataHub 目录中不存在（UI 无法展示血缘边），默认会 **轻量注册**
最小 Dataset（秒级 MCP，不跑完整 HMS ingest）。需要完整 schema 时设
``BLF_LINEAGE_FULL_UPSTREAM_INGEST=1``（可能较慢；``run_add_manual_upstream_lineage.sh`` 默认超时 24h）。
``BLF_LINEAGE_SKIP_UPSTREAM_INGEST=1`` 或 ``--skip-upstream-ingest`` 可跳过。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import List, Optional, Set, Tuple

from .datahub_writer import make_dataset_urn_from_ref, make_hive_dataset_urn
from .hive_single_table_ingest import ensure_upstream_dataset_in_datahub
from .models import TableRef

logger = logging.getLogger(__name__)


def _load_lineage_env_early() -> None:
    raw = os.environ.get("BLF_LINEAGE_ENV_FILE", "").strip()
    if not raw:
        return
    try:
        from pathlib import Path

        from .lineage_llm_compare import load_env_file

        load_env_file(Path(raw), override=False)
    except Exception:
        pass


def parse_fqtn(spec: str) -> TableRef:
    """解析 ``db.table`` 或 ``catalog.db.table``；**必须**含库名（至少一个 ``.``）。"""
    s = spec.strip()
    if not s:
        raise ValueError("表名为空")
    if s.startswith("urn:li:"):
        raise ValueError("不支持在表名参数中直接传 URN；请使用 库.表 形式")
    parts = s.split(".")
    if len(parts) < 2:
        raise ValueError("必须为 库.表 格式（至少含一个点），例如 dw.dw_order_v1")
    db = ".".join(parts[:-1])
    table = parts[-1]
    if not db or not table:
        raise ValueError("库名与表名均不能为空（不接受 .table 或 db. 形式）")
    return TableRef(db=db, table=table)


def _dataset_urn_set(upstreams: Optional[List[object]]) -> Set[str]:
    out: Set[str] = set()
    if not upstreams:
        return out
    for u in upstreams:
        ds = getattr(u, "dataset", None)
        if isinstance(ds, str):
            out.add(ds)
    return out


def merge_and_emit(
    gms_url: str,
    token: Optional[str],
    downstream: TableRef,
    upstream: TableRef,
    platform_instance: str,
    env: str,
    *,
    replace: bool,
    dry_run: bool,
) -> Tuple[bool, str]:
    """合并并写入；返回 (是否执行写入, 人类可读摘要)。"""
    try:
        from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
        from datahub.metadata.schema_classes import DatasetLineageTypeClass, UpstreamClass, UpstreamLineageClass
    except ImportError as e:
        raise RuntimeError(
            "需要安装 acryl-datahub（含 ingestion graph）。"
            "例如: pip install 'acryl-datahub>=0.12'"
        ) from e

    downstream_urn = make_dataset_urn_from_ref(downstream, platform_instance, env)
    upstream_urn = make_dataset_urn_from_ref(upstream, platform_instance, env)

    if downstream_urn == upstream_urn:
        raise ValueError("下游与上游 URN 相同，无需写入")

    graph = DataHubGraph(DatahubClientConfig(server=gms_url.rstrip("/"), token=token))
    existing: Optional[UpstreamLineageClass] = graph.get_aspect(
        entity_urn=downstream_urn,
        aspect_type=UpstreamLineageClass,
    )

    if not replace and existing and existing.upstreams:
        existing_urns = _dataset_urn_set(existing.upstreams)
        if upstream_urn in existing_urns:
            msg = f"已存在上游，跳过: downstream={downstream.full_name} upstream={upstream.full_name}"
            logger.info(msg)
            return False, msg

    merged_upstream_classes: List[UpstreamClass]
    fine_grained = None

    if replace:
        merged_upstream_classes = [
            UpstreamClass(dataset=upstream_urn, type=DatasetLineageTypeClass.TRANSFORMED)
        ]
        summary = (
            f"[replace] downstream={downstream.full_name} -> 仅保留上游 {upstream.full_name} "
            f"(URN {upstream_urn})"
        )
    else:
        seen: Set[str] = set()
        merged_upstream_classes = []
        if existing and existing.upstreams:
            for u in existing.upstreams:
                u_urn = getattr(u, "dataset", None)
                if not isinstance(u_urn, str) or u_urn in seen:
                    continue
                seen.add(u_urn)
                merged_upstream_classes.append(u)
        if upstream_urn not in seen:
            merged_upstream_classes.append(
                UpstreamClass(dataset=upstream_urn, type=DatasetLineageTypeClass.TRANSFORMED)
            )
        if existing and existing.fineGrainedLineages:
            fine_grained = list(existing.fineGrainedLineages)
        summary = (
            f"[merge] downstream={downstream.full_name} 追加上游 {upstream.full_name} "
            f"（合并后共 {len(merged_upstream_classes)} 条表级上游）"
        )

    if dry_run:
        logger.info("[dry-run] %s", summary)
        if fine_grained is not None:
            logger.info("[dry-run] 保留 fineGrainedLineages: %d 条", len(fine_grained))
        return False, summary + " [dry-run]"

    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.emitter.rest_emitter import DatahubRestEmitter

    aspect = UpstreamLineageClass(
        upstreams=merged_upstream_classes,
        fineGrainedLineages=fine_grained,
    )

    mcp = MetadataChangeProposalWrapper(entityUrn=downstream_urn, aspect=aspect)
    emitter = DatahubRestEmitter(gms_url, token=token)
    emitter.emit_mcp(mcp)
    logger.info("upstreamLineage 写入成功: %s", summary)
    return True, summary


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--table-name", required=True, help="下游表：必须为 库.表（如 dw.dw_order_v1）")
    p.add_argument("--upstream-name", required=True, help="上游表：必须为 库.表")
    p.add_argument("--datahub-gms", default=os.getenv("DATAHUB_GMS_URL", "http://127.0.0.1:8080"))
    p.add_argument("--token", default=os.getenv("DATAHUB_GMS_TOKEN"))
    p.add_argument("--platform-instance", default=os.getenv("BLF_DATAHUB_PLATFORM_INSTANCE", "blf-prod-hive"))
    p.add_argument("--env", default=os.getenv("DATAHUB_ENV", "PROD"))
    p.add_argument(
        "--replace",
        action="store_true",
        help="丢弃已有表级上游与字段级血缘，仅写入本次一条上游（危险）",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--skip-upstream-ingest",
        action="store_true",
        help="不自动从 Hive ingest 缺失的上游表（默认会 ingest）",
    )
    p.add_argument(
        "--python",
        default=os.environ.get("LINEAGE_PYTHON") or os.environ.get("HIVE_INGEST_PYTHON"),
        help="执行 datahub ingest 的解释器，默认 LINEAGE_PYTHON 或当前 python",
    )
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


def _skip_upstream_ingest_flag(cli_skip: bool) -> bool:
    if cli_skip:
        return True
    v = os.environ.get("BLF_LINEAGE_SKIP_UPSTREAM_INGEST", "").strip().lower()
    return v in ("1", "true", "yes", "on")


def main() -> int:
    _load_lineage_env_early()
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s %(message)s",
    )
    import logging as _logging

    _logging.getLogger("urllib3.connectionpool").setLevel(_logging.ERROR)
    _logging.getLogger("urllib3.util.retry").setLevel(_logging.ERROR)

    try:
        downstream = parse_fqtn(args.table_name)
        upstream = parse_fqtn(args.upstream_name)
    except ValueError as e:
        logger.error("%s", e)
        return 2

    skip_ingest = _skip_upstream_ingest_flag(args.skip_upstream_ingest)
    py_exec = args.python or sys.executable

    logger.info(
        "GMS=%s downstream=%s upstream=%s platform_instance=%s env=%s "
        "replace=%s dry_run=%s skip_upstream_ingest=%s python=%s",
        args.datahub_gms,
        downstream.full_name,
        upstream.full_name,
        args.platform_instance,
        args.env,
        args.replace,
        args.dry_run,
        skip_ingest,
        py_exec,
    )
    logger.debug(
        "URN preview: downstream=%s upstream=%s",
        make_hive_dataset_urn(downstream.db, downstream.table, args.platform_instance, args.env),
        make_hive_dataset_urn(upstream.db, upstream.table, args.platform_instance, args.env),
    )

    try:
        _, ingest_msg = ensure_upstream_dataset_in_datahub(
            upstream,
            gms_url=args.datahub_gms,
            token=args.token,
            platform_instance=args.platform_instance,
            env=args.env,
            python_executable=py_exec,
            dry_run=args.dry_run,
            skip_ingest=skip_ingest,
        )
        if ingest_msg:
            print(ingest_msg)
        _, msg = merge_and_emit(
            args.datahub_gms,
            args.token,
            downstream,
            upstream,
            args.platform_instance,
            args.env,
            replace=args.replace,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        logger.error("失败: %s", exc)
        return 1

    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
