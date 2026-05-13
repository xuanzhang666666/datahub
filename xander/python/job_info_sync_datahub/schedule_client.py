"""schedule_client — 封装 DMP 调度元数据的 Trino 查询。

依赖：trino（pip install trino）
环境变量（可选覆盖）：
  TRINO_HOST      默认 10.253.7.167
  TRINO_PORT      默认 8081
  TRINO_USER      默认 xuan.zhang
  TRINO_CATALOG   默认 hive
  TRINO_SCHEMA    默认 default
"""

from __future__ import annotations

import json
import logging
import os
from typing import List

from .logging_utils import SCHEDULE_FETCH_FAILED, get_logger, log_phase_error
from .models import JobMetadata

try:
    import trino
except ImportError as _e:
    raise SystemExit(
        "缺少依赖：请先执行 python3 -m pip install trino"
    ) from _e

logger = get_logger("schedule_client")

_DMP_TABLE = "default.ods_data_platform_dmp_schedule_job_basic_info"


def _trino_conn() -> "trino.dbapi.Connection":
    return trino.dbapi.connect(
        host=os.getenv("TRINO_HOST", "10.253.7.167"),
        port=int(os.getenv("TRINO_PORT", "8081")),
        user=os.getenv("TRINO_USER", "xuan.zhang"),
        catalog=os.getenv("TRINO_CATALOG", "hive"),
        schema=os.getenv("TRINO_SCHEMA", "default"),
    )


def fetch_all_job_metadata(prefix: str = "pdw") -> List[JobMetadata]:
    """一次性查询指定前缀的所有作业元数据，返回 JobMetadata 列表（批量模式）。"""
    safe = prefix.replace("'", "''")
    sql = f"""
SELECT
    job_display_name,
    job_name,
    try(from_utf8(from_base64(shell_commond))) AS shell_command,
    upstream_jobs,
    dt
FROM {_DMP_TABLE}
WHERE dt = (SELECT max(dt) FROM {_DMP_TABLE})
  AND job_display_name LIKE '{safe}%'
ORDER BY job_display_name
""".strip()

    logger.info("批量查询调度元数据: prefix=%s", prefix)
    conn = _trino_conn()
    cur = conn.cursor()
    try:
        cur.execute(sql)
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    results: List[JobMetadata] = []
    for row in rows:
        name = str(row[0]) if row[0] else ""
        job_name = str(row[1]) if row[1] else ""
        shell = str(row[2]) if row[2] else ""
        upstream_raw = row[3]
        dt = str(row[4]) if row[4] else ""

        if not shell.strip():
            continue  # 跳过 shell_command 为空的作业

        upstream_jobs: List[str] = []
        if upstream_raw and upstream_raw not in ("UNKNOWN_UPSTREAM_JOBS", "null", ""):
            try:
                parsed = json.loads(upstream_raw)
                if isinstance(parsed, list):
                    upstream_jobs = [str(j) for j in parsed if j]
            except (json.JSONDecodeError, TypeError):
                upstream_jobs = [j.strip() for j in str(upstream_raw).split(",") if j.strip()]

        results.append(JobMetadata(
            job_display_name=name,
            job_name=job_name,
            shell_command=shell,
            upstream_jobs=upstream_jobs,
            dt=dt,
        ))

    logger.info("批量查询完成: prefix=%s 有效作业=%d / %d", prefix, len(results), len(rows))
    return results


def fetch_job_metadata(job_display_name: str) -> JobMetadata:
    """从 DMP 调度表中查询指定作业的元数据，返回 JobMetadata。

    如果查不到或 shell_command 为空，抛出 RuntimeError。
    """
    safe_name = job_display_name.replace("'", "''")
    sql = f"""
SELECT
    job_display_name,
    job_name,
    try(from_utf8(from_base64(shell_commond))) AS shell_command,
    upstream_jobs,
    dt
FROM {_DMP_TABLE}
WHERE dt = (SELECT max(dt) FROM {_DMP_TABLE})
  AND job_display_name = '{safe_name}'
""".strip()

    logger.debug("查询调度元数据: job_display_name=%s", job_display_name)
    conn = _trino_conn()
    cur = conn.cursor()
    try:
        cur.execute(sql)
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    if not rows:
        raise RuntimeError(f"DMP 中未找到作业: {job_display_name!r}")

    row = rows[0]
    name, job_name, shell, upstream_raw, dt = (
        str(row[0]),
        str(row[1]) if row[1] else "",
        str(row[2]) if row[2] else "",
        row[3],
        str(row[4]) if row[4] else "",
    )

    if not shell.strip():
        raise RuntimeError(f"作业 {name!r} 的 shell_command 为空")

    # upstream_jobs 可能是 JSON 数组字符串，也可能是逗号分隔或 UNKNOWN_UPSTREAM_JOBS
    upstream_jobs: List[str] = []
    if upstream_raw and upstream_raw not in ("UNKNOWN_UPSTREAM_JOBS", "null", ""):
        try:
            parsed = json.loads(upstream_raw)
            if isinstance(parsed, list):
                upstream_jobs = [str(j) for j in parsed if j]
        except (json.JSONDecodeError, TypeError):
            # 降级：尝试逗号分隔
            upstream_jobs = [j.strip() for j in str(upstream_raw).split(",") if j.strip()]

    logger.info(
        "调度元数据获取成功: job_display_name=%s dt=%s upstream_count=%d",
        name,
        dt,
        len(upstream_jobs),
    )
    return JobMetadata(
        job_display_name=name,
        job_name=job_name,
        shell_command=shell,
        upstream_jobs=upstream_jobs,
        dt=dt,
    )


def fetch_job_metadata_safe(
    job_display_name: str,
    parent_logger: logging.Logger,
) -> JobMetadata | None:
    """fetch_job_metadata 的安全版本：失败时记录日志并返回 None，不抛出异常。"""
    try:
        return fetch_job_metadata(job_display_name)
    except Exception as exc:
        log_phase_error(parent_logger, SCHEDULE_FETCH_FAILED, job_display_name, str(exc))
        return None
