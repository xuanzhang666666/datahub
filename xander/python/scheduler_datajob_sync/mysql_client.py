"""MySQL reader for scheduler DataJob sync."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Callable, Protocol

from .models import SchedulerJobMetadata


class CursorLike(Protocol):
    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        ...

    def fetchall(self) -> list[dict[str, object]]:
        ...

    def close(self) -> None:
        ...


class ConnectionLike(Protocol):
    def cursor(self) -> CursorLike:
        ...

    def close(self) -> None:
        ...


ConnectionFactory = Callable[[], ConnectionLike]

_TABLE = "dmp_schedule_job_basic_info"
_COLUMNS = (
    "id, job_name, job_display_name, job_owner_name, job_proxy_user, "
    "line_business_code, last_build_start_time, created_time, updated_time, "
    "content, contacts_name, assigned_node, job_disable, job_priority, "
    "build_keep_days, build_keep_num, shell_commond, upstream_jobs, "
    "upstream_jobs_conditions, delay_config, ivr_notify, sms_notify, im_notify, "
    "job_size, job_count, build_update_time, batch_exec_time"
)
_ACTIVITY_TIME_CLAUSE = (
    "(last_build_start_time >= %s OR build_update_time >= %s)"
)
# Jobs with batch_exec_time at or before this timestamp are historical/deleted entries
# that should not appear in DataHub. Default matches the hourly-sync epoch in prod.
# Override via env var SCHEDULER_MIN_BATCH_EXEC_TIME.
_MIN_BATCH_EXEC_TIME: str = os.getenv(
    "SCHEDULER_MIN_BATCH_EXEC_TIME", "2026-06-11 07:17:06"
)


def default_connection_factory() -> ConnectionLike:
    try:
        import pymysql
    except ImportError as exc:
        raise RuntimeError("缺少依赖：请先安装 pymysql") from exc
    return pymysql.connect(
        host=os.getenv("SCHEDULER_MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("SCHEDULER_MYSQL_PORT", "3306")),
        user=os.getenv("SCHEDULER_MYSQL_USER", ""),
        password=os.getenv("SCHEDULER_MYSQL_PASSWORD", ""),
        database=os.getenv("SCHEDULER_MYSQL_DATABASE", ""),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


class SchedulerMysqlClient:
    def __init__(
        self,
        connection_factory: ConnectionFactory = default_connection_factory,
    ) -> None:
        self._connection_factory = connection_factory

    def fetch_job(self, job_display_name: str) -> SchedulerJobMetadata:
        jobs = self._query(
            f"SELECT {_COLUMNS} FROM {_TABLE} WHERE job_display_name = %s",
            (job_display_name,),
        )
        if not jobs:
            raise RuntimeError(f"未找到调度作业: {job_display_name}")
        return jobs[0]

    def fetch_jobs_by_prefix(self, prefix: str) -> list[SchedulerJobMetadata]:
        return self._query(
            f"SELECT {_COLUMNS} FROM {_TABLE} "
            "WHERE job_display_name LIKE %s "
            "AND batch_exec_time > %s "
            "ORDER BY job_display_name",
            (f"{prefix}%", _MIN_BATCH_EXEC_TIME),
        )

    def fetch_jobs_updated_since(self, since: datetime) -> list[SchedulerJobMetadata]:
        return self._query(
            f"SELECT {_COLUMNS} FROM {_TABLE} "
            "WHERE (updated_time >= %s OR batch_exec_time >= %s) "
            "AND batch_exec_time > %s "
            "ORDER BY updated_time, job_display_name",
            (since, since, _MIN_BATCH_EXEC_TIME),
        )

    def fetch_jobs_activity_since(self, since: datetime) -> list[SchedulerJobMetadata]:
        return self._query(
            f"SELECT {_COLUMNS} FROM {_TABLE} "
            f"WHERE {_ACTIVITY_TIME_CLAUSE} "
            "ORDER BY batch_exec_time DESC, job_display_name",
            (since, since),
        )

    def _query(
        self,
        sql: str,
        params: tuple[object, ...],
    ) -> list[SchedulerJobMetadata]:
        conn = self._connection_factory()
        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            return [
                SchedulerJobMetadata.from_mysql_row(row)
                for row in cur.fetchall()
            ]
        finally:
            cur.close()
            conn.close()
