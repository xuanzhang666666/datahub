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
from typing import Any, Dict, List, Optional

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    __package__ = "job_info_sync_datahub"

import trino

from .datahub_writer import DatahubWriter, make_dataset_urn_from_ref
from .gitlab_client import download_etl_file
from .logging_utils import get_logger, setup_logging
from .models import JobContext
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
from .schedule_client import (
    dmp_batch_exec_time_online_sql_clause,
    fetch_all_job_metadata,
    fetch_job_metadata,
)
from .structured_properties import collect_structured_properties_for_sync

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
    online_clause = dmp_batch_exec_time_online_sql_clause()
    sql = f"""SELECT job_display_name
        FROM {_DMP_TABLE}
        WHERE dt = (SELECT max(dt) FROM {_DMP_TABLE})
          AND job_display_name LIKE '{safe}%'
{online_clause}ORDER BY job_display_name""".strip()
    cur.execute(sql)
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
    lineage_vote: bool = False,
    llm_timeout_sec: int = 90,
    audit_jsonl: Optional[str] = None,
    discrepancy_log: Optional[str] = None,
    batch_output_dir: Optional[str] = None,
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
        "lineage_status": None,
        "lineage_reason": None,
        "write_upstream_lineage": None,
        "lineage_targets_chosen": None,
        "lineage_sources_chosen": None,
        "trust_score": None,
        "etl_file_path": None,
        "etl_file_source": None,
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
            result["etl_file_path"] = used_path
            result["etl_file_source"] = "inline"
        else:
            # 有 runner 调用，从 GitLab / localfolder 双源拉取
            gitlab_name = extract_gitlab_name(metadata.shell_command)
            project_path = resolve_project_path(gitlab_name)
            job_path, kind = extract_job_path_and_type(metadata.shell_command)
            jfn = job_file_name(job_path, kind)
            job_dir = get_job_dir_name(metadata.shell_command)
            candidates = candidate_gitlab_paths(job_path, jfn, job_dir)
            used_path, etl_content, etl_source = download_etl_file(
                gitlab_name=gitlab_name,
                project_path=project_path,
                candidate_paths=candidates,
                job_file_name=jfn,
                ref="master",
                token=gitlab_token,
            )
            result["etl_file_path"] = used_path
            result["etl_file_source"] = etl_source

        # 3. LLM 表级血缘提取
        from .lineage_write_policy import (
            append_lineage_audit_jsonl,
            default_audit_log_path,
            evaluate_llm_only,
        )

        skip_upstream_lineage = False
        skip_upstream_lineage_reason = ""
        table_lineages: list = []
        field_lineages: list = []
        lineage_decision = None
        try:
            table_lineages, lineage_decision, _llm_raw = evaluate_llm_only(
                etl_content,
                timeout_sec=llm_timeout_sec,
                job_file_name=jfn,
            )
            skip_upstream_lineage = not lineage_decision.write_upstream_lineage
            skip_upstream_lineage_reason = lineage_decision.reason
            result["lineage_status"] = lineage_decision.status
            result["lineage_reason"] = lineage_decision.reason
            result["write_upstream_lineage"] = lineage_decision.write_upstream_lineage
            result["lineage_targets_chosen"] = sorted(lineage_decision.selected_targets)
            result["lineage_sources_chosen"] = sorted(lineage_decision.selected_upstreams)
            result["trust_score"] = lineage_decision.trust_score
            audit_path = (
                Path(audit_jsonl)
                if audit_jsonl
                else (Path(discrepancy_log) if discrepancy_log else default_audit_log_path(batch_output_dir))
            )
            append_lineage_audit_jsonl(audit_path, job_display_name, lineage_decision)
        except Exception as exc:
            result["lineage_status"] = "LLM_ERROR"
            result["lineage_reason"] = str(exc)[:500]
            result["write_upstream_lineage"] = False
            logger.warning("批量作业 %s LLM 提取失败: %s", job_display_name, exc)

        if not table_lineages:
            result["status"] = "SKIP"
            result["fail_category"] = None
            result["error"] = (
                lineage_decision.reason
                if lineage_decision is not None and lineage_decision.reason
                else (result.get("lineage_reason") or "LLM 未提取到目标表")
            )
            result["elapsed"] = round(time.time() - t0, 1)
            return result

        # 4. 结构化属性
        ctx = JobContext(metadata=metadata)
        ctx.runtime = parse_runtime_context(job_display_name, metadata.shell_command, etl_content, used_path)
        ctx.sql_blocks = []
        ctx.table_lineages = table_lineages
        ctx.field_lineages = field_lineages
        write_lineage = (
            lineage_decision is not None and lineage_decision.write_upstream_lineage
        )
        props = collect_structured_properties_for_sync(
            ctx,
            table_lineages=table_lineages,
            write_upstream_lineage=write_lineage,
        )
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
            skip_upstream_lineage=skip_upstream_lineage,
            skip_upstream_lineage_reason=skip_upstream_lineage_reason,
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
    lineage_vote: bool = False,
    llm_timeout_sec: int = 90,
    audit_jsonl: Optional[str] = None,
    discrepancy_log: Optional[str] = None,
) -> None:
    report_file = Path(report_path)
    report_parent = str(report_file.parent)
    report_file.parent.mkdir(parents=True, exist_ok=True)

    # 若报告已存在：仅跳过「最后一条记录」为 OK/SKIP 的作业；FAIL 会在下次继续跑（避免旧失败永远卡住）
    job_last_status: Dict[str, str] = {}
    done: set = set()
    if report_file.exists():
        with open(report_file, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    job_last_status[r["job"]] = r["status"]
                except Exception:
                    pass
        done = {j for j, st in job_last_status.items() if st in ("OK", "SKIP")}
        if done:
            logger.info("跳过已成功或业务 SKIP 的作业: %d 个（历史 FAIL 将重试）", len(done))

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
                    sync_one,
                    job,
                    gms_url,
                    gms_token,
                    gitlab_token,
                    platform_instance,
                    env,
                    dry_run,
                    meta_map.get(job) if meta_map else None,
                    lineage_vote,
                    llm_timeout_sec,
                    audit_jsonl,
                    discrepancy_log,
                    report_parent,
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
                job = res.get("job", "?")
                st = res.get("status", "?")
                elapsed = res.get("elapsed", 0)
                ls = res.get("lineage_status") or "-"
                trust = res.get("trust_score")
                trust_s = str(trust) if trust is not None else "-"
                tgt = res.get("target_table") or "-"
                upc = res.get("upstream_count", 0)
                err = (res.get("error") or "")[:120]
                etl_src = res.get("etl_file_source")
                etl_path = res.get("etl_file_path")
                etl_part = ""
                if etl_src or etl_path:
                    etl_part = f" etl_src={etl_src or '-'} etl={etl_path or '-'}"
                extra = ""
                if st == "FAIL" and err:
                    extra = f" err={err!r}{etl_part}"
                elif ls not in (None, "-", "") and ls != "LLM_POLICY_ERROR":
                    extra = f" lineage={ls} trust={trust_s}{etl_part}"
                elif ls == "LLM_POLICY_ERROR" and err:
                    extra = f" lineage={ls} err={err!r}{etl_part}"
                elif etl_part:
                    extra = etl_part
                logger.info(
                    "[PROGRESS] %d/%d job=%s status=%s elapsed=%ss target=%s upstreams=%d OK=%d SKIP=%d FAIL=%d%s",
                    completed_count,
                    total,
                    job,
                    st,
                    elapsed,
                    tgt,
                    upc,
                    counters["OK"],
                    counters.get("SKIP", 0),
                    counters["FAIL"],
                    extra,
                )
                if completed_count % 50 == 0 or completed_count == total:
                    logger.info(
                        "进度汇总 %d/%d  OK=%d SKIP=%d FAIL=%d",
                        completed_count,
                        total,
                        counters["OK"],
                        counters.get("SKIP", 0),
                        counters["FAIL"],
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
    """从报告文件打印汇总统计。同一 job 多行时取最后一行状态（与跳过逻辑一致）。"""
    job_last: Dict[str, Dict[str, Any]] = {}
    with open(report_path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
                job_last[r["job"]] = r
            except Exception:
                pass
    results = list(job_last.values())

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
    p.add_argument(
        "--lineage-vote",
        action="store_true",
        help="已废弃（LLM 提取现为默认行为），保留此参数仅为向后兼容，无实际效果",
    )
    p.add_argument("--llm-timeout", type=int, default=90, help="DeepSeek HTTP 超时秒数，默认 90")
    p.add_argument("--audit-jsonl", default=None, help="血缘审计 JSONL；默认可设 BLF_LINEAGE_AUDIT_JSONL")
    p.add_argument(
        "--discrepancy-log",
        default=None,
        help="血缘审计 JSONL（与 --audit-jsonl 并存时以后者为准）；可设 BLF_LINEAGE_DISCREPANCY_LOG",
    )
    return p.parse_args()


def _load_lineage_env_early() -> None:
    """若设置了 BLF_LINEAGE_ENV_FILE，在解析 CLI 之前加载（与 Jenkins / docker 脚本一致）。"""
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
    # 屏蔽 DataHub SDK 和 urllib3 的重试 WARNING（正常失败已在 datahub_writer 层用 ERROR 记录）
    import logging as _logging
    _logging.getLogger("urllib3.connectionpool").setLevel(_logging.ERROR)
    _logging.getLogger("urllib3.util.retry").setLevel(_logging.ERROR)

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
        lineage_vote=args.lineage_vote,
        llm_timeout_sec=args.llm_timeout,
        audit_jsonl=args.audit_jsonl,
        discrepancy_log=args.discrepancy_log,
    )

    print_summary(args.report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
