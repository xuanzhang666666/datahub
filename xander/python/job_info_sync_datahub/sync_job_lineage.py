#!/usr/bin/env python3
"""sync_job_lineage — 通用调度作业血缘入湖 DataHub CLI。

根据传入的 job_display_name，自动完成：
  1. 查询 DMP 调度元数据（Trino）
  2. 解析 shell_command → 拉取 GitLab ETL 脚本
  3. 提取 SQL blocks → sqlglot AST 解析目标表、上游表、字段级血缘
  4. 运行所有结构化属性 extractor
  5. 写入 DataHub（structuredProperties + upstreamLineage）

快速上手：
  # 仅解析，不写入 DataHub（dry-run），产物存到 /tmp/job_lineage/<job>
  python3 sync_job_lineage.py --job pdw_opc_flag_contact --dry-run --output-dir /tmp/job_lineage

  # 正式写入
  export DATAHUB_GMS_URL=http://datahub-gms:8080
  export BLF_GITLAB_PRIVATE_TOKEN=glpat-xxx
  python3 sync_job_lineage.py --job pdw_opc_flag_contact

必要依赖：
  pip install trino sqlglot acryl-datahub
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

# 同包模块导入（脚本也可直接用 python3 sync_job_lineage.py 运行）
if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    __package__ = "job_info_sync_datahub"

from .datahub_writer import DatahubWriter, make_dataset_urn_from_ref
from .gitlab_client import download_etl_file
from .lineage_parser import build_lineage_summary, parse_block_lineage
from .logging_utils import (
    GITLAB_FETCH_FAILED,
    RUNTIME_PARSE_FAILED,
    SCHEDULE_FETCH_FAILED,
    get_logger,
    log_phase_error,
    setup_logging,
)
from .models import JobContext, ParseStatus
from .runtime_parser import (
    candidate_gitlab_paths,
    extract_gitlab_name,
    extract_job_path_and_type,
    get_job_dir_name,
    has_real_w_run_task,
    job_file_name,
    parse_runtime_context,
    resolve_project_path,
)
from .schedule_client import fetch_job_metadata
from .sql_extractor import extract_sql_blocks
from .structured_properties import DEFAULT_EXTRACTORS, run_all_extractors

logger = get_logger("main")


# --------------------------------------------------------------------------
# 产物输出
# --------------------------------------------------------------------------


def _save_outputs(ctx: JobContext, output_dir: Path) -> None:
    """将解析中间产物保存到 output_dir，便于 dry-run 后复盘。"""
    job = ctx.metadata.job_display_name
    out = output_dir / job
    out.mkdir(parents=True, exist_ok=True)

    def _write(name: str, obj: object) -> None:
        p = out / name
        p.write_text(json.dumps(obj, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
        logger.debug("产物写入: %s", p)

    _write(
        "metadata.json",
        {
            "job_display_name": ctx.metadata.job_display_name,
            "job_name": ctx.metadata.job_name,
            "upstream_jobs": ctx.metadata.upstream_jobs,
            "dt": ctx.metadata.dt,
        },
    )

    if ctx.runtime:
        _write(
            "runtime_context.json",
            {
                "gitlab_name": ctx.runtime.gitlab_name,
                "project_path": ctx.runtime.project_path,
                "job_path": ctx.runtime.job_path,
                "job_type": ctx.runtime.job_type,
                "job_file_name": ctx.runtime.job_file_name,
                "gitlab_file_path": ctx.runtime.gitlab_file_path,
                "runtime_params": ctx.runtime.runtime_params,
            },
        )

    _write(
        "sql_blocks.json",
        [
            {
                "index": b.index,
                "status": b.status.value,
                "error_detail": b.error_detail,
                "targets": [t.full_name for t in b.target_tables],
                "upstreams": [u.full_name for u in b.upstream_tables],
                "fields": len(b.field_mappings),
                "confidence": b.confidence.value,
            }
            for b in ctx.sql_blocks
        ],
    )

    _write(
        "lineage.json",
        {
            "table_lineages": [
                {
                    "target": tl.target.full_name,
                    "upstreams": [u.full_name for u in tl.upstreams],
                }
                for tl in ctx.table_lineages
            ],
            "field_lineages": [
                {
                    "target_table": fl.target_table.full_name,
                    "target_field": fl.target_field,
                    "confidence": fl.confidence.value,
                    "mappings": [
                        {
                            "source_table": fm.source_table,
                            "source_field": fm.source_field,
                            "expression": fm.expression,
                            "confidence": fm.confidence.value,
                        }
                        for fm in fl.mappings
                    ],
                }
                for fl in ctx.field_lineages
            ],
        },
    )

    _write(
        "write_plan.json",
        {
            "structured_properties": [
                {"urn": p.property_urn, "value_chars": len(p.string_value)}
                for p in ctx.structured_properties
            ],
        },
    )

    logger.info("产物已写入: %s", out)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def run(
    job_display_name: str,
    dry_run: bool = False,
    output_dir: Optional[str] = None,
    gms_url: Optional[str] = None,
    gms_token: Optional[str] = None,
    gitlab_token: Optional[str] = None,
    gitlab_ref: str = "master",
    gitlab_file_path: Optional[str] = None,
    etl_file: Optional[str] = None,
    platform_instance: str = "blf-prod-hive",
    env: str = "PROD",
    llm_compare: bool = False,
    llm_timeout_sec: int = 240,
    lineage_vote: bool = False,
    discrepancy_log: Optional[str] = None,
    audit_jsonl: Optional[str] = None,
) -> int:
    """主流程编排，返回 exit code（0=成功，非 0=失败）。"""

    # ---- 1. 查询调度元数据 ----
    logger.info("=== 阶段 1/5：查询 DMP 调度元数据 ===")
    try:
        metadata = fetch_job_metadata(job_display_name)
    except Exception as exc:
        log_phase_error(logger, SCHEDULE_FETCH_FAILED, job_display_name, str(exc))
        return 3

    ctx = JobContext(metadata=metadata)

    # ---- 2. 解析 shell_command，拉取 ETL 脚本 ----
    logger.info("=== 阶段 2/5：解析 shell_command + 拉取 ETL 脚本 ===")
    try:
        gitlab_name = extract_gitlab_name(metadata.shell_command)
        project_path = resolve_project_path(gitlab_name)
        job_path, kind = extract_job_path_and_type(metadata.shell_command)
        jfn = job_file_name(job_path, kind)
        job_dir = get_job_dir_name(metadata.shell_command)
        candidates = candidate_gitlab_paths(job_path, jfn, job_dir)

        is_inline = not has_real_w_run_task(metadata.shell_command)

        if etl_file:
            with open(etl_file, encoding="utf-8", errors="replace") as fh:
                etl_content = fh.read()
            used_path = etl_file
            logger.info("使用本地 ETL 文件: %s", etl_file)
        elif is_inline:
            # w-run-task.sh 只出现在注释中，ETL SQL 内联在 shell_command 本身
            etl_content = metadata.shell_command
            used_path = "<inline:shell_command>"
            logger.info(
                "w-run-task.sh 仅为注释引用，使用 shell_command 内联 SQL 作为 ETL 来源 "
                "(job_path 参考: %s)",
                job_path,
            )
        else:
            used_path, etl_content = download_etl_file(
                project_path=project_path,
                candidate_paths=candidates,
                job_file_name=jfn,
                ref=gitlab_ref,
                explicit_path=gitlab_file_path,
                token=gitlab_token,
            )
            logger.info("GitLab 文件拉取成功: %s", used_path)

        ctx.runtime = parse_runtime_context(
            job_display_name=job_display_name,
            shell_command=metadata.shell_command,
            etl_content=etl_content,
            gitlab_file_path=used_path,
        )
    except Exception as exc:
        if "gitlab" in str(exc).lower() or "project" in str(exc).lower():
            log_phase_error(logger, GITLAB_FETCH_FAILED, job_display_name, str(exc))
        else:
            log_phase_error(logger, RUNTIME_PARSE_FAILED, job_display_name, str(exc))
        return 3

    # ---- 3. 提取 SQL blocks + AST 血缘解析 ----
    logger.info("=== 阶段 3/5：提取 SQL blocks + sqlglot AST 血缘解析 ===")
    blocks = extract_sql_blocks(etl_content, jfn, date_str=metadata.dt)
    for block in blocks:
        parse_block_lineage(block, job_display_name, logger)
    ctx.sql_blocks = blocks

    successful_blocks = [b for b in blocks if b.status not in (ParseStatus.SQL_PARSE_FAILED, ParseStatus.SKIPPED)]
    if not successful_blocks:
        logger.warning(
            "ABNORMAL_JOB\tSQL_PARSE_FAILED\t%s\t所有 SQL block 解析失败或为空，无血缘数据",
            job_display_name,
        )
        # 继续执行（至少写入结构化属性）

    table_lineages, field_lineages = build_lineage_summary(blocks)
    ctx.table_lineages = table_lineages
    ctx.field_lineages = field_lineages
    logger.info(
        "血缘解析完成: target_tables=%d upstream_relations=%d field_mappings=%d",
        len(table_lineages),
        sum(len(tl.upstreams) for tl in table_lineages),
        len(field_lineages),
    )

    if not table_lineages and not lineage_vote:
        logger.warning(
            "ABNORMAL_JOB\tSQL_PARSE_FAILED\t%s\t未解析到任何目标表，请确认 ETL 脚本格式",
            job_display_name,
        )
        return 3

    # ---- 3b 可选：双 LLM + 表级写入策略（会按需改写 table_lineages；审计落 JSONL）----
    llm_dict: Optional[dict] = None
    lineage_decision = None
    if lineage_vote or llm_compare:
        from .lineage_llm_compare import run_compare

        logger.info(
            "=== 可选：DeepSeek 与 sqlglot 表级对比%s ===",
            " + 写入策略与审计" if lineage_vote else "",
        )
        try:
            if lineage_vote:
                from .lineage_write_policy import (
                    append_lineage_audit_jsonl,
                    default_audit_log_path,
                    evaluate_lineage_write_vote,
                )

                table_lineages, lineage_decision, llm_dict = evaluate_lineage_write_vote(
                    etl_content,
                    jfn,
                    metadata.dt or None,
                    table_lineages,
                    timeout_sec=llm_timeout_sec,
                )
                ctx.table_lineages = table_lineages
                logger.info(
                    "表级血缘策略: status=%s write_upstream=%s targets=%s upstreams=%s",
                    lineage_decision.status,
                    lineage_decision.write_upstream_lineage,
                    sorted(lineage_decision.selected_targets),
                    sorted(lineage_decision.selected_upstreams),
                )
                audit_path = (
                    Path(audit_jsonl)
                    if audit_jsonl
                    else (Path(discrepancy_log) if discrepancy_log else default_audit_log_path(output_dir))
                )
                append_lineage_audit_jsonl(
                    audit_path,
                    job_display_name,
                    lineage_decision,
                    extra={"llm_verdict": llm_dict.get("verdict")},
                )
                logger.info("血缘审计已追加: %s", audit_path)
                if output_dir:
                    dout = Path(output_dir) / job_display_name / "lineage_write_decision.json"
                    dout.parent.mkdir(parents=True, exist_ok=True)
                    dout.write_text(
                        json.dumps(lineage_decision.to_audit_dict(), ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    logger.info("策略产物: %s", dout)
            elif llm_compare:
                rep = run_compare(etl_content, jfn, metadata.dt or None, timeout_sec=llm_timeout_sec)
                llm_dict = rep.to_dict()
            if output_dir and llm_dict is not None:
                lout = Path(output_dir) / job_display_name / "llm_compare.json"
                lout.parent.mkdir(parents=True, exist_ok=True)
                lout.write_text(json.dumps(llm_dict, ensure_ascii=False, indent=2), encoding="utf-8")
                logger.info("LLM 对比产物: %s", lout)
            if llm_dict is not None:
                logger.info(
                    "LLM 对比 verdict=%s sqlglot_upstream=%s",
                    llm_dict.get("verdict"),
                    llm_dict.get("sqlglot_upstream"),
                )
        except Exception as exc:
            logger.warning("LLM/策略失败（沿用 sqlglot 血缘）: %s", exc)
            if lineage_vote:
                try:
                    from .lineage_write_policy import (
                        LineageWriteDecision,
                        append_lineage_audit_jsonl,
                        default_audit_log_path,
                    )
                    st_set = {tl.target.full_name for tl in table_lineages}
                    su_set = {u.full_name for tl in table_lineages for u in tl.upstreams}
                    err_decision = LineageWriteDecision(
                        write_upstream_lineage=True,
                        status="LLM_POLICY_ERROR",
                        reason=str(exc)[:500],
                        selected_targets=st_set,
                        selected_upstreams=su_set,
                        sqlglot_targets=st_set,
                        sqlglot_upstreams=su_set,
                    )
                    audit_path = (
                        Path(audit_jsonl)
                        if audit_jsonl
                        else (Path(discrepancy_log) if discrepancy_log else default_audit_log_path(output_dir))
                    )
                    append_lineage_audit_jsonl(
                        audit_path, job_display_name, err_decision,
                        extra={"llm_error": str(exc)[:500]},
                    )
                except Exception:
                    pass

    if not table_lineages:
        logger.warning(
            "ABNORMAL_JOB\tSQL_PARSE_FAILED\t%s\t未解析到任何目标表（含 LLM 回填后仍为空）",
            job_display_name,
        )
        return 3

    skip_upstream_lineage = False
    skip_upstream_lineage_reason = ""
    if lineage_vote and lineage_decision is not None and not lineage_decision.write_upstream_lineage:
        skip_upstream_lineage = True
        skip_upstream_lineage_reason = lineage_decision.reason

    # ---- 4. 运行结构化属性 extractor ----
    logger.info("=== 阶段 4/5：运行结构化属性 extractors ===")
    props = run_all_extractors(ctx, DEFAULT_EXTRACTORS)
    ctx.structured_properties = props
    logger.info("结构化属性收集完成: %d 条", len(props))

    # ---- 保存中间产物（含投票后的 lineage）----
    if output_dir:
        _save_outputs(ctx, Path(output_dir))

    # ---- 5. 写入 DataHub ----
    logger.info("=== 阶段 5/5：写入 DataHub%s ===", " [dry-run]" if dry_run else "")
    writer = DatahubWriter(
        gms_url=gms_url,
        token=gms_token,
        platform_instance=platform_instance,
        env=env,
        dry_run=dry_run,
    )
    ok = writer.write_all(
        table_lineages=table_lineages,
        field_lineages=field_lineages,
        props=props,
        job_display_name=job_display_name,
        parent_logger=logger,
        skip_upstream_lineage=skip_upstream_lineage,
        skip_upstream_lineage_reason=skip_upstream_lineage_reason,
    )

    if ok:
        logger.info("✓ 作业 %s 处理完成", job_display_name)
        return 0
    else:
        logger.error("✗ 作业 %s 写入 DataHub 部分失败，详情见上方日志", job_display_name)
        return 4


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--job",
        required=True,
        help="调度系统中的 job_display_name，例如 pdw_opc_flag_contact",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="完整解析但不写入 DataHub，结合 --output-dir 使用可保存中间产物",
    )
    p.add_argument(
        "--output-dir",
        default=None,
        help="产物输出目录（metadata.json / lineage.json 等），不指定则不保存",
    )
    p.add_argument(
        "--datahub-gms",
        default=os.getenv("DATAHUB_GMS_URL", "http://127.0.0.1:8080"),
        help="DataHub GMS 地址（默认读取 DATAHUB_GMS_URL 环境变量）",
    )
    p.add_argument(
        "--token",
        default=os.getenv("DATAHUB_GMS_TOKEN"),
        help="DataHub GMS Bearer token（默认读取 DATAHUB_GMS_TOKEN 环境变量）",
    )
    p.add_argument(
        "--gitlab-token",
        default=os.getenv("BLF_GITLAB_PRIVATE_TOKEN"),
        help="GitLab Private token（默认读取 BLF_GITLAB_PRIVATE_TOKEN 环境变量）",
    )
    p.add_argument("--gitlab-ref", default="master", help="GitLab 分支，默认 master")
    p.add_argument(
        "--gitlab-file-path",
        default=None,
        help="显式指定 GitLab 仓库内文件路径，跳过自动候选逻辑",
    )
    p.add_argument(
        "--etl-file",
        default=None,
        help="直接读取本地 ETL 文件，跳过 GitLab 拉取（调试用）",
    )
    p.add_argument("--platform-instance", default="blf-prod-hive")
    p.add_argument("--env", default="PROD")
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别，默认 INFO",
    )
    p.add_argument(
        "--llm-compare",
        action="store_true",
        help="在血缘解析后调用 DeepSeek 与 sqlglot 对比，写入 llm_compare.json（需 .env 中 DEEPSEEK_API_KEY；建议配合 --output-dir）",
    )
    p.add_argument(
        "--llm-timeout",
        type=int,
        default=90,
        help="DeepSeek HTTP 超时秒数，默认 90",
    )
    p.add_argument(
        "--lineage-vote",
        action="store_true",
        help="DeepSeek+sqlglot 表级策略：完全一致则写(trust=100)；单源则写(异常)；全无目标不写。审计见 --audit-jsonl",
    )
    p.add_argument(
        "--discrepancy-log",
        default=None,
        help="血缘审计 JSONL（与 --audit-jsonl 并存时以后者为准）；可设 BLF_LINEAGE_DISCREPANCY_LOG",
    )
    p.add_argument(
        "--audit-jsonl",
        default=None,
        help="血缘审计 JSONL 路径；默认可设 BLF_LINEAGE_AUDIT_JSONL 或 <output-dir>/lineage_audit.jsonl",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging(level=args.log_level, job=args.job)
    return run(
        job_display_name=args.job,
        dry_run=args.dry_run,
        output_dir=args.output_dir,
        gms_url=args.datahub_gms,
        gms_token=args.token,
        gitlab_token=args.gitlab_token,
        gitlab_ref=args.gitlab_ref,
        gitlab_file_path=args.gitlab_file_path,
        etl_file=args.etl_file,
        platform_instance=args.platform_instance,
        env=args.env,
        llm_compare=args.llm_compare,
        llm_timeout_sec=args.llm_timeout,
        lineage_vote=args.lineage_vote,
        discrepancy_log=args.discrepancy_log,
        audit_jsonl=args.audit_jsonl,
    )


if __name__ == "__main__":
    raise SystemExit(main())
