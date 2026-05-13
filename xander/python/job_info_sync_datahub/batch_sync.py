#!/usr/bin/env python3
"""batch_sync — 批量同步多个调度作业的血缘到 DataHub。

用法：
  # 同步所有 pdw 开头的作业，并发 8
  python3 batch_sync.py --prefix pdw --concurrency 8 --report /tmp/batch_report.jsonl

  # 从文件读取作业名列表
  python3 batch_sync.py --job-file /tmp/pdw_jobs.txt --concurrency 8 --report /tmp/batch_report.jsonl

  # 只重跑失败的（从上次报告中提取）
  python3 batch_sync.py --retry-failed /tmp/batch_report.jsonl --concurrency 8

输出：
  - /tmp/batch_report.jsonl  每行一个 JSON，记录每个作业的结果
  - 结束时打印汇总表格
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    __package__ = "job_info_sync_datahub"

import trino

from .datahub_writer import DatahubWriter, make_dataset_urn_from_ref
from .gitlab_client import download_etl_file
from .lineage_parser import build_lineage_summary, parse_block_lineage
from .logging_utils import get_logger, setup_logging
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
from .schedule_client import fetch_all_job_metadata, fetch_job_metadata
from .sql_extractor import extract_sql_blocks
from .structured_properties import DEFAULT_EXTRACTORS, run_all_extractors

logger = get_logger("batch")

# 失败原因分类（用于汇总统计）
FAIL_GITLAB = "GITLAB_NOT_FOUND"
FAIL_RUNTIME = "RUNTIME_PARSE"
FAIL_SQL = "SQL_PARSE"
FAIL_WRITE = "DATAHUB_WRITE"
FAIL_SCHEDULE = "SCHEDULE_FETCH"
FAIL_OTHER = "OTHER"

_TRINO_HOST = os.getenv("TRINO_HOST", "10.253.7.167")
_TRINO_PORT = int(os.getenv("TRINO_PORT", "8081"))
_TRINO_USER = os.getenv("TRINO_USER", "xuan.zhang")
_DMP_TABLE = "default.ods_data_platform_dmp_schedule_job_basic_info"


def fetch_all_jobs(prefix: str = "pdw") -> List[str]:
    """从 DMP 查询指定前缀的所有作业名。"""
    conn = trino.dbapi.connect(
        host=_TRINO_HOST, port=_TRINO_PORT, user=_TRINO_USER,
        catalog="hive", schema="default",
    )
    cur = conn.cursor()
    safe = prefix.replace("'", "''")
    cur.execute(f"""
        SELECT job_display_name
        FROM {_DMP_TABLE}
        WHERE dt = (SELECT max(dt) FROM {_DMP_TABLE})
          AND job_display_name LIKE '{safe}%'
        ORDER BY job_display_name
    """)
    rows = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    logger.info("DMP 查询完成: prefix=%s 共 %d 个作业", prefix, len(rows))
    return rows


def _classify_error(msg: str) -> str:
    m = msg.lower()
    if "404" in m or "not found" in m and "gitlab" in m or "候选路径" in m:
        return FAIL_GITLAB
    if "schedule" in m or "dmp" in m or "shell_command" in m:
        return FAIL_SCHEDULE
    if "sql" in m or "sqlglot" in m or "parse" in m:
        return FAIL_SQL
    if "datahub" in m or "write" in m or "patch" in m or "connection" in m:
        return FAIL_WRITE
    if "runtime" in m or "gitlab" in m:
        return FAIL_RUNTIME
    return FAIL_OTHER


def sync_one(
    job_display_name: str,
    gms_url: str,
    gms_token: Optional[str],
    gitlab_token: Optional[str],
    platform_instance: str,
    env: str,
    dry_run: bool,
    prefetched_metadata: Optional[object] = None,
) -> Dict:
    """同步单个作业，返回结果 dict（不抛异常）。

    prefetched_metadata: 批量模式下直接传入 JobMetadata，跳过 Trino 查询。
    """
    result: Dict = {
        "job": job_display_name,
        "ts": datetime.utcnow().isoformat(),
        "status": "OK",
        "fail_category": None,
        "error": None,
        "target_table": None,
        "upstream_count": 0,
        "field_count": 0,
    }
    t0 = time.time()
    try:
        # 1. DMP 元数据（批量模式复用，单作业模式实时查询）
        if prefetched_metadata is not None:
            metadata = prefetched_metadata
        else:
            metadata = fetch_job_metadata(job_display_name)

        # 2. ETL 脚本
        is_inline = not has_real_w_run_task(metadata.shell_command)
        jfn = f"{job_display_name}.sh"  # inline 时的默认文件名

        if is_inline:
            etl_content = metadata.shell_command
            used_path = "<inline>"
        else:
            # 有 runner 调用，从 GitLab 拉取文件
            gitlab_name = extract_gitlab_name(metadata.shell_command)
            project_path = resolve_project_path(gitlab_name)
            job_path, kind = extract_job_path_and_type(metadata.shell_command)
            jfn = job_file_name(job_path, kind)
            job_dir = get_job_dir_name(metadata.shell_command)
            candidates = candidate_gitlab_paths(job_path, jfn, job_dir)
            used_path, etl_content = download_etl_file(
                project_path=project_path,
                candidate_paths=candidates,
                job_file_name=jfn,
                ref="master",
                token=gitlab_token,
            )

        # 3. SQL 解析
        blocks = extract_sql_blocks(etl_content, jfn, date_str=metadata.dt)
        for block in blocks:
            parse_block_lineage(block, job_display_name)

        table_lineages, field_lineages = build_lineage_summary(blocks)

        if not table_lineages:
            failed_blocks = [b for b in blocks if b.status == ParseStatus.SQL_PARSE_FAILED]
            if failed_blocks:
                err = failed_blocks[0].error_detail or "SQL_PARSE_FAILED"
                result["status"] = "FAIL"
                result["fail_category"] = FAIL_SQL
                result["error"] = err[:200]
            else:
                result["status"] = "SKIP"
                result["fail_category"] = None
                result["error"] = "未提取到 SQL block 或无目标表"
            result["elapsed"] = round(time.time() - t0, 1)
            return result

        # 4. 结构化属性
        ctx = JobContext(metadata=metadata)
        ctx.runtime = parse_runtime_context(job_display_name, metadata.shell_command, etl_content, used_path)
        ctx.sql_blocks = blocks
        ctx.table_lineages = table_lineages
        ctx.field_lineages = field_lineages
        props = run_all_extractors(ctx, DEFAULT_EXTRACTORS)
        ctx.structured_properties = props

        # 5. 写入 DataHub
        writer = DatahubWriter(
            gms_url=gms_url, token=gms_token,
            platform_instance=platform_instance, env=env, dry_run=dry_run,
        )
        ok = writer.write_all(
            table_lineages=table_lineages,
            field_lineages=field_lineages,
            props=props,
            job_display_name=job_display_name,
        )
        if not ok:
            result["status"] = "FAIL"
            result["fail_category"] = FAIL_WRITE
            result["error"] = "DataHub write partial failure"
        else:
            result["status"] = "OK"

        result["target_table"] = table_lineages[0].target.full_name if table_lineages else None
        result["upstream_count"] = sum(len(tl.upstreams) for tl in table_lineages)
        result["field_count"] = len(field_lineages)

    except Exception as exc:
        result["status"] = "FAIL"
        result["fail_category"] = _classify_error(str(exc))
        result["error"] = str(exc)[:300]

    result["elapsed"] = round(time.time() - t0, 1)
    return result


def run_batch(
    jobs: List[str],
    report_path: str,
    concurrency: int = 8,
    gms_url: str = "http://datahub-gms:8080",
    gms_token: Optional[str] = None,
    gitlab_token: Optional[str] = None,
    platform_instance: str = "blf-prod-hive",
    env: str = "PROD",
    dry_run: bool = False,
) -> None:
    report_file = Path(report_path)
    report_file.parent.mkdir(parents=True, exist_ok=True)

    # 如果报告文件已存在，跳过已处理的作业
    done: set = set()
    if report_file.exists():
        with open(report_file, encoding="utf-8") as f:
            for line in f:
                try:
                    done.add(json.loads(line)["job"])
                except Exception:
                    pass
        if done:
            logger.info("跳过已处理作业: %d 个", len(done))

    todo = [j for j in jobs if j not in done]
    total = len(todo)
    logger.info("待处理作业: %d / %d", total, len(jobs))

    # 批量预取元数据（jobs 来自 --prefix 时启用）
    meta_map: Optional[Dict] = None
    if getattr(run_batch, "_prefetched_meta", None):
        meta_map = run_batch._prefetched_meta

    counters = {"OK": 0, "SKIP": 0, "FAIL": 0}
    fail_cats: Dict[str, int] = {}
    completed_count = 0

    with open(report_file, "a", encoding="utf-8") as fout:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(
                    sync_one, job, gms_url, gms_token, gitlab_token,
                    platform_instance, env, dry_run,
                    meta_map.get(job) if meta_map else None,
                ): job
                for job in todo
            }
            for future in as_completed(futures):
                res = future.result()
                fout.write(json.dumps(res, ensure_ascii=False) + "\n")
                fout.flush()

                counters[res["status"]] = counters.get(res["status"], 0) + 1
                if res["fail_category"]:
                    fail_cats[res["fail_category"]] = fail_cats.get(res["fail_category"], 0) + 1

                completed_count += 1
                if completed_count % 50 == 0 or completed_count == total:
                    logger.info(
                        "进度 %d/%d  OK=%d SKIP=%d FAIL=%d",
                        completed_count, total,
                        counters["OK"], counters.get("SKIP", 0), counters["FAIL"],
                    )

    # 最终汇总
    logger.info("=" * 60)
    logger.info("批量同步完成: 总计=%d OK=%d SKIP=%d FAIL=%d",
                total, counters["OK"], counters.get("SKIP", 0), counters["FAIL"])
    if fail_cats:
        logger.info("失败分类:")
        for cat, cnt in sorted(fail_cats.items(), key=lambda x: -x[1]):
            logger.info("  %-30s %d", cat, cnt)
    logger.info("报告文件: %s", report_file)


def print_summary(report_path: str) -> None:
    """从报告文件打印汇总统计。"""
    results = []
    with open(report_path, encoding="utf-8") as f:
        for line in f:
            try:
                results.append(json.loads(line))
            except Exception:
                pass

    ok = [r for r in results if r["status"] == "OK"]
    skip = [r for r in results if r["status"] == "SKIP"]
    fail = [r for r in results if r["status"] == "FAIL"]

    print(f"\n{'='*60}")
    print(f"总计: {len(results)}  OK: {len(ok)}  SKIP: {len(skip)}  FAIL: {len(fail)}")

    if fail:
        from collections import Counter
        cats = Counter(r.get("fail_category", "OTHER") for r in fail)
        print("\n失败分类:")
        for cat, cnt in cats.most_common():
            print(f"  {cat:<30} {cnt}")

        print("\n失败详情 (前50):")
        for r in fail[:50]:
            print(f"  [{r.get('fail_category','?')}] {r['job']}: {r.get('error','')[:80]}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--prefix", help="查询 DMP 中指定前缀的作业，如 pdw")
    g.add_argument("--job-file", help="从文件读取作业名列表（每行一个）")
    g.add_argument("--retry-failed", help="从上次报告中提取失败作业重跑")
    g.add_argument("--summary", help="只打印报告文件的汇总统计，不运行")

    p.add_argument("--report", default="/tmp/batch_sync_report.jsonl", help="结果报告文件路径")
    p.add_argument("--concurrency", type=int, default=8, help="并发线程数，默认 8")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--datahub-gms", default=os.getenv("DATAHUB_GMS_URL", "http://datahub-gms:8080"))
    p.add_argument("--token", default=os.getenv("DATAHUB_GMS_TOKEN"))
    p.add_argument("--gitlab-token", default=os.getenv("BLF_GITLAB_PRIVATE_TOKEN"))
    p.add_argument("--platform-instance", default="blf-prod-hive")
    p.add_argument("--env", default="PROD")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging(level=args.log_level)

    if args.summary:
        print_summary(args.summary)
        return 0

    # 收集作业列表（--prefix 时同时批量预取元数据）
    if args.prefix:
        all_meta = fetch_all_job_metadata(args.prefix)
        run_batch._prefetched_meta = {m.job_display_name: m for m in all_meta}
        jobs = list(run_batch._prefetched_meta.keys())
    elif args.job_file:
        jobs = [l.strip() for l in Path(args.job_file).read_text().splitlines() if l.strip()]
        logger.info("从文件读取 %d 个作业: %s", len(jobs), args.job_file)
    else:  # retry-failed
        jobs = []
        with open(args.retry_failed, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    if r["status"] == "FAIL":
                        jobs.append(r["job"])
                except Exception:
                    pass
        logger.info("重跑失败作业: %d 个", len(jobs))
        # 重跑时删除旧的 FAIL 记录，保留 OK/SKIP
        tmp = args.report + ".tmp"
        with open(args.retry_failed, encoding="utf-8") as fin, open(tmp, "w", encoding="utf-8") as fout:
            for line in fin:
                try:
                    r = json.loads(line)
                    if r["status"] != "FAIL":
                        fout.write(line)
                except Exception:
                    pass
        import shutil
        shutil.move(tmp, args.report)

    run_batch(
        jobs=jobs,
        report_path=args.report,
        concurrency=args.concurrency,
        gms_url=args.datahub_gms,
        gms_token=args.token,
        gitlab_token=args.gitlab_token,
        platform_instance=args.platform_instance,
        env=args.env,
        dry_run=args.dry_run,
    )

    print_summary(args.report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
