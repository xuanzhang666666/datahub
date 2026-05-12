"""datahub_writer — 统一写入 DataHub：structuredProperties + upstreamLineage。

两种写入路径：
  1. structuredProperties：通过 OpenAPI PATCH（无需 datahub SDK）
  2. upstreamLineage / fineGrainedLineages：通过 datahub SDK MCP emitter

环境变量：
  DATAHUB_GMS_URL    GMS 地址，默认 http://127.0.0.1:8080
  DATAHUB_GMS_TOKEN  Bearer token（无鉴权可不设）
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional

from .logging_utils import DATAHUB_WRITE_FAILED, get_logger, log_phase_error
from .models import (
    FieldLineage,
    FieldMapping,
    ParseConfidence,
    StructuredPropertyValue,
    TableLineage,
    TableRef,
)

logger = get_logger("datahub_writer")

# --------------------------------------------------------------------------
# DataHub SDK（仅 lineage 写入需要）
# --------------------------------------------------------------------------
try:
    from datahub.emitter import mce_builder as builder
    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.emitter.rest_emitter import DatahubRestEmitter
    from datahub.metadata.schema_classes import (
        DatasetLineageTypeClass,
        FineGrainedLineageClass,
        FineGrainedLineageDownstreamTypeClass,
        FineGrainedLineageUpstreamTypeClass,
        UpstreamClass,
        UpstreamLineageClass,
    )

    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False
    logger.warning(
        "datahub SDK 未安装，upstreamLineage 写入将被跳过。"
        "可执行：pip install acryl-datahub"
    )


# --------------------------------------------------------------------------
# URN 构造
# --------------------------------------------------------------------------


def make_hive_dataset_urn(
    db: str,
    table: str,
    platform_instance: str = "blf-prod-hive",
    env: str = "PROD",
) -> str:
    return (
        f"urn:li:dataset:(urn:li:dataPlatform:hive,"
        f"{platform_instance}.{db}.{table},{env})"
    )


def make_dataset_urn_from_ref(
    ref: TableRef,
    platform_instance: str = "blf-prod-hive",
    env: str = "PROD",
) -> str:
    return make_hive_dataset_urn(ref.db, ref.table, platform_instance, env)


# --------------------------------------------------------------------------
# structuredProperties PATCH
# --------------------------------------------------------------------------


def _structured_props_url(gms_base: str, dataset_urn: str) -> str:
    encoded = urllib.parse.quote(dataset_urn, safe="")
    return f"{gms_base.rstrip('/')}/openapi/v3/entity/dataset/{encoded}/structuredProperties"


def patch_structured_properties(
    gms_url: str,
    dataset_urn: str,
    props: List[StructuredPropertyValue],
    token: Optional[str] = None,
) -> None:
    """将结构化属性批量 PATCH 到单个 dataset，使用 add 操作兼容首次写入。"""
    if not props:
        return

    patch_ops = [
        {
            "op": "add",
            "path": f"/properties/{p.property_urn}",
            "value": {
                "propertyUrn": p.property_urn,
                "values": [{"string": p.string_value}],
            },
        }
        for p in props
    ]
    body = {
        "patch": patch_ops,
        "arrayPrimaryKeys": {"properties": ["propertyUrn"]},
    }
    data = json.dumps(body).encode("utf-8")
    url = _structured_props_url(gms_url, dataset_urn)
    req = urllib.request.Request(
        url,
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
            if resp.status != 200:
                raise RuntimeError(f"PATCH structuredProperties 返回 HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"PATCH structuredProperties HTTP {e.code}: {detail}") from e

    logger.info(
        "structuredProperties PATCH 成功: urn=%s props=%d",
        dataset_urn,
        len(props),
    )


# --------------------------------------------------------------------------
# upstreamLineage MCP emit
# --------------------------------------------------------------------------


def _build_fine_grained(
    downstream_urn: str,
    upstream_urn: str,
    field_lineages: List[FieldLineage],
    target_table_ref: TableRef,
) -> List["FineGrainedLineageClass"]:
    """为指定目标表构建 fineGrainedLineages 列表，只包含高置信度映射。"""
    result = []
    for fl in field_lineages:
        if fl.target_table != target_table_ref:
            continue
        for fm in fl.mappings:
            if fm.confidence != ParseConfidence.HIGH:
                continue
            if fm.source_field is None:
                continue
            src_table_urn = upstream_urn  # 来源表 URN（此处简化取第一个上游，复杂场景可按 fm.source_table 匹配）
            u_field = builder.make_schema_field_urn(src_table_urn, fm.source_field)
            d_field = builder.make_schema_field_urn(downstream_urn, fm.target_field)
            result.append(
                FineGrainedLineageClass(
                    upstreamType=FineGrainedLineageUpstreamTypeClass.FIELD_SET,
                    upstreams=[u_field],
                    downstreamType=FineGrainedLineageDownstreamTypeClass.FIELD,
                    downstreams=[d_field],
                )
            )
    return result


def emit_upstream_lineage(
    gms_url: str,
    table_lineage: TableLineage,
    field_lineages: List[FieldLineage],
    platform_instance: str = "blf-prod-hive",
    env: str = "PROD",
    token: Optional[str] = None,
) -> None:
    """发送单个目标表的 upstreamLineage（含可确认的字段级血缘）。"""
    if not _SDK_AVAILABLE:
        raise RuntimeError(
            "datahub SDK 未安装，无法写入 upstreamLineage。"
            "请执行：pip install acryl-datahub"
        )

    downstream_urn = make_dataset_urn_from_ref(table_lineage.target, platform_instance, env)
    upstream_classes: List[UpstreamClass] = []
    fine_grained: List[FineGrainedLineageClass] = []

    for u_ref in table_lineage.upstreams:
        upstream_urn = make_dataset_urn_from_ref(u_ref, platform_instance, env)
        upstream_classes.append(
            UpstreamClass(
                dataset=upstream_urn,
                type=DatasetLineageTypeClass.TRANSFORMED,
            )
        )
        # 字段级血缘：只写置信度为 HIGH 的映射
        fg = _build_fine_grained(
            downstream_urn, upstream_urn, field_lineages, table_lineage.target
        )
        fine_grained.extend(fg)

    if not upstream_classes:
        logger.debug("目标表 %s 无上游，跳过 upstreamLineage", table_lineage.target.full_name)
        return

    aspect = UpstreamLineageClass(
        upstreams=upstream_classes,
        fineGrainedLineages=fine_grained if fine_grained else None,
    )
    mcp = MetadataChangeProposalWrapper(entityUrn=downstream_urn, aspect=aspect)
    emitter = DatahubRestEmitter(gms_url, token=token)
    emitter.emit_mcp(mcp)

    logger.info(
        "upstreamLineage 写入成功: downstream=%s upstreams=%d fine_grained=%d",
        table_lineage.target.full_name,
        len(upstream_classes),
        len(fine_grained),
    )


# --------------------------------------------------------------------------
# 统一写入入口
# --------------------------------------------------------------------------


class DatahubWriter:
    """统一的 DataHub 写入器，封装所有写入操作并支持 dry-run。"""

    def __init__(
        self,
        gms_url: Optional[str] = None,
        token: Optional[str] = None,
        platform_instance: str = "blf-prod-hive",
        env: str = "PROD",
        dry_run: bool = False,
    ) -> None:
        self.gms_url = gms_url or os.getenv("DATAHUB_GMS_URL", "http://127.0.0.1:8080")
        self.token = token or os.getenv("DATAHUB_GMS_TOKEN")
        self.platform_instance = platform_instance
        self.env = env
        self.dry_run = dry_run

    def write_structured_properties(
        self,
        dataset_urn: str,
        props: List[StructuredPropertyValue],
        job_display_name: str = "",
        parent_logger: Optional[logging.Logger] = None,
    ) -> bool:
        """写入结构化属性，返回是否成功。dry-run 时打印计划但不实际写入。"""
        _log = parent_logger or logger
        if self.dry_run:
            _log.info(
                "[dry-run] structuredProperties 写入计划: urn=%s props=%s",
                dataset_urn,
                [p.property_urn for p in props],
            )
            return True
        try:
            patch_structured_properties(self.gms_url, dataset_urn, props, self.token)
            return True
        except Exception as exc:
            log_phase_error(_log, DATAHUB_WRITE_FAILED, job_display_name, str(exc))
            return False

    def write_lineage(
        self,
        table_lineages: List[TableLineage],
        field_lineages: List[FieldLineage],
        job_display_name: str = "",
        parent_logger: Optional[logging.Logger] = None,
    ) -> bool:
        """写入全部 upstreamLineage，返回是否全部成功。"""
        _log = parent_logger or logger
        if self.dry_run:
            for tl in table_lineages:
                _log.info(
                    "[dry-run] upstreamLineage 写入计划: target=%s upstreams=%s",
                    tl.target.full_name,
                    [u.full_name for u in tl.upstreams],
                )
            return True
        success = True
        for tl in table_lineages:
            try:
                emit_upstream_lineage(
                    self.gms_url,
                    tl,
                    field_lineages,
                    self.platform_instance,
                    self.env,
                    self.token,
                )
            except Exception as exc:
                log_phase_error(
                    _log,
                    DATAHUB_WRITE_FAILED,
                    job_display_name,
                    f"upstreamLineage {tl.target.full_name}: {exc}",
                )
                success = False
        return success

    def write_all(
        self,
        table_lineages: List[TableLineage],
        field_lineages: List[FieldLineage],
        props: List[StructuredPropertyValue],
        job_display_name: str = "",
        parent_logger: Optional[logging.Logger] = None,
    ) -> bool:
        """将结构化属性写入每个目标表，并写入 upstreamLineage。"""
        _log = parent_logger or logger
        all_ok = True

        for tl in table_lineages:
            urn = make_dataset_urn_from_ref(tl.target, self.platform_instance, self.env)
            _log.info("写入目标表: %s  urn=%s", tl.target.full_name, urn)
            ok = self.write_structured_properties(urn, props, job_display_name, _log)
            if not ok:
                all_ok = False

        ok = self.write_lineage(table_lineages, field_lineages, job_display_name, _log)
        if not ok:
            all_ok = False

        return all_ok
