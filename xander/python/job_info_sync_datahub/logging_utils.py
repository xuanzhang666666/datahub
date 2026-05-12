"""日志工具：统一格式、脱敏规则和阶段错误码。"""

from __future__ import annotations

import logging
import re
from typing import Optional

# 阶段错误码常量
SCHEDULE_FETCH_FAILED = "SCHEDULE_FETCH_FAILED"
RUNTIME_PARSE_FAILED = "RUNTIME_PARSE_FAILED"
GITLAB_FETCH_FAILED = "GITLAB_FETCH_FAILED"
SQL_PARSE_FAILED = "SQL_PARSE_FAILED"
SQL_PARSE_PARTIAL = "SQL_PARSE_PARTIAL"
DATAHUB_WRITE_FAILED = "DATAHUB_WRITE_FAILED"

# 脱敏：匹配常见 token/密码字段
_SENSITIVE_PATTERNS = [
    re.compile(r"(PRIVATE-TOKEN|Authorization|Bearer|password|token|secret)\s*[=:]\s*\S+", re.IGNORECASE),
    # glpat-xxx 格式的 GitLab token
    re.compile(r"glpat-[A-Za-z0-9_\-]+"),
]


def redact(text: str) -> str:
    """对字符串中的敏感信息做脱敏处理。"""
    for pat in _SENSITIVE_PATTERNS:
        text = pat.sub(lambda m: m.group(0).split("=")[0].split(":")[0] + "=<REDACTED>", text)
    return text


def setup_logging(level: str = "INFO", job: Optional[str] = None) -> logging.Logger:
    """初始化日志配置，返回模块级 logger。

    格式包含时间戳、logger 名、级别，便于批量运行时按行解析。
    """
    fmt = "%(asctime)s [%(levelname)s] %(name)s"
    if job:
        fmt += f" [job={job}]"
    fmt += " %(message)s"

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt,
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("job_info_sync_datahub")


def get_logger(name: str) -> logging.Logger:
    """获取子模块 logger，命名空间统一挂在 job_info_sync_datahub 下。"""
    return logging.getLogger(f"job_info_sync_datahub.{name}")


def log_phase_error(logger: logging.Logger, error_code: str, job: str, detail: str) -> None:
    """以统一格式输出阶段失败日志。

    格式：ABNORMAL_JOB  <error_code>  <job>  <detail>
    保持与试点脚本 ABNORMAL_JOB 的 stderr 格式兼容，同时走 logging 体系。
    """
    logger.error("ABNORMAL_JOB\t%s\t%s\t%s", error_code, job, redact(detail))


def log_phase_warning(logger: logging.Logger, error_code: str, job: str, detail: str) -> None:
    """以统一格式输出阶段告警日志。"""
    logger.warning("PARTIAL_JOB\t%s\t%s\t%s", error_code, job, redact(detail))
