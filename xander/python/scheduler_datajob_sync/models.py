"""Data models for scheduler DataJob sync."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping

from .job_dependency_parser import parse_job_dependencies_from_content
from .trigger_parser import TRIGGER_JOB_DEPENDENCY, TRIGGER_TIMER, parse_job_triggers


def decode_maybe_base64(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    try:
        decoded = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return value
    try:
        return decoded.decode("utf-8")
    except UnicodeDecodeError:
        return value


def _string_value(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if value is None:
        return ""
    return str(value)


def _int_value(row: Mapping[str, object], key: str) -> int:
    value = row.get(key)
    if value is None or value == "":
        return 0
    return int(value)


def _datetime_value(row: Mapping[str, object], key: str) -> datetime | None:
    value = row.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        if value.startswith("0000-00-00"):
            return None
        return datetime.fromisoformat(value)
    raise TypeError(f"Unsupported datetime value for {key}: {value!r}")


def parse_upstream_jobs(raw: object) -> list[str]:
    if raw is None:
        return []
    text = str(raw).strip()
    if not text or text in {"UNKNOWN_UPSTREAM_JOBS", "null"}:
        return []
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return [item.strip() for item in text.split(",") if item.strip()]
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if item]


def parse_upstream_conditions(raw: object) -> list[str]:
    if raw is None:
        return []
    text = str(raw).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return [item.strip() for item in text.split(",") if item.strip()]
    if not isinstance(parsed, list):
        return [text]
    return [str(item) for item in parsed]


@dataclass(frozen=True)
class SchedulerJobDependency:
    upstream_job_display_name: str
    condition: str
    status: str = "SUCCESS"


def pair_job_dependencies(
    upstream_jobs: list[str],
    upstream_jobs_conditions: str,
) -> tuple[list[SchedulerJobDependency], int]:
    conditions = parse_upstream_conditions(upstream_jobs_conditions)
    mismatch_count = 0
    if upstream_jobs and conditions and len(conditions) != len(upstream_jobs):
        mismatch_count = abs(len(conditions) - len(upstream_jobs))

    dependencies: list[SchedulerJobDependency] = []
    for index, upstream in enumerate(upstream_jobs):
        name = upstream.strip()
        if not name:
            continue
        condition = conditions[index] if index < len(conditions) else ""
        dependencies.append(
            SchedulerJobDependency(
                upstream_job_display_name=name,
                condition=condition,
            )
        )
    return dependencies, mismatch_count


def _resolve_trigger_types(
    parsed_trigger_types: list[str],
    upstream_jobs: list[str],
) -> list[str]:
    if parsed_trigger_types:
        return parsed_trigger_types
    if upstream_jobs:
        return [TRIGGER_JOB_DEPENDENCY]
    return []


@dataclass
class SchedulerJobMetadata:
    id: int = 0
    job_name: str = ""
    job_display_name: str = ""
    job_owner_name: str = ""
    job_proxy_user: str = ""
    line_business_code: str = ""
    last_build_start_time: datetime | None = None
    created_time: datetime | None = None
    updated_time: datetime | None = None
    content: str = ""
    contacts_name: str = ""
    assigned_node: str = ""
    job_disable: str = ""
    job_priority: str = ""
    build_keep_days: str = ""
    build_keep_num: str = ""
    shell_command: str = ""
    upstream_jobs: list[str] = field(default_factory=list)
    upstream_jobs_conditions: str = ""
    trigger_types: list[str] = field(default_factory=list)
    cron_schedule: str = ""
    time_hour_param: str = ""
    job_dependencies: list[SchedulerJobDependency] = field(default_factory=list)
    dependency_pair_mismatch_count: int = 0
    delay_config: str = ""
    ivr_notify: str = ""
    sms_notify: str = ""
    im_notify: str = ""
    job_size: int = 0
    job_count: int = 0
    build_update_time: datetime | None = None
    batch_exec_time: datetime | None = None

    @classmethod
    def from_mysql_row(cls, row: Mapping[str, object]) -> "SchedulerJobMetadata":
        content = decode_maybe_base64(_string_value(row, "content"))
        mysql_upstream_jobs = parse_upstream_jobs(row.get("upstream_jobs"))
        mysql_upstream_jobs_conditions = _string_value(row, "upstream_jobs_conditions")
        parsed_trigger = parse_job_triggers(content)

        xml_dependencies = parse_job_dependencies_from_content(content)
        if xml_dependencies is not None:
            job_dependencies = [
                SchedulerJobDependency(
                    upstream_job_display_name=dependency.upstream_job_display_name,
                    condition=dependency.condition,
                    status=dependency.status,
                )
                for dependency in xml_dependencies
            ]
            upstream_jobs = [
                dependency.upstream_job_display_name for dependency in xml_dependencies
            ]
            upstream_jobs_conditions = ",".join(
                dependency.condition for dependency in xml_dependencies
            )
            mismatch_count = 0
        else:
            upstream_jobs = mysql_upstream_jobs
            upstream_jobs_conditions = mysql_upstream_jobs_conditions
            job_dependencies, mismatch_count = pair_job_dependencies(
                upstream_jobs,
                upstream_jobs_conditions,
            )

        trigger_types = _resolve_trigger_types(
            parsed_trigger.trigger_types,
            upstream_jobs,
        )
        return cls(
            id=_int_value(row, "id"),
            job_name=_string_value(row, "job_name"),
            job_display_name=_string_value(row, "job_display_name"),
            job_owner_name=_string_value(row, "job_owner_name"),
            job_proxy_user=_string_value(row, "job_proxy_user"),
            line_business_code=_string_value(row, "line_business_code"),
            last_build_start_time=_datetime_value(row, "last_build_start_time"),
            created_time=_datetime_value(row, "created_time"),
            updated_time=_datetime_value(row, "updated_time"),
            content=content,
            contacts_name=_string_value(row, "contacts_name"),
            assigned_node=_string_value(row, "assigned_node"),
            job_disable=_string_value(row, "job_disable"),
            job_priority=_string_value(row, "job_priority"),
            build_keep_days=_string_value(row, "build_keep_days"),
            build_keep_num=_string_value(row, "build_keep_num"),
            shell_command=decode_maybe_base64(_string_value(row, "shell_commond")),
            upstream_jobs=upstream_jobs,
            upstream_jobs_conditions=upstream_jobs_conditions,
            trigger_types=trigger_types,
            cron_schedule=parsed_trigger.cron_schedule,
            time_hour_param=parsed_trigger.time_hour_param,
            job_dependencies=job_dependencies,
            dependency_pair_mismatch_count=mismatch_count,
            delay_config=_string_value(row, "delay_config"),
            ivr_notify=_string_value(row, "ivr_notify"),
            sms_notify=_string_value(row, "sms_notify"),
            im_notify=_string_value(row, "im_notify"),
            job_size=_int_value(row, "job_size"),
            job_count=_int_value(row, "job_count"),
            build_update_time=_datetime_value(row, "build_update_time"),
            batch_exec_time=_datetime_value(row, "batch_exec_time"),
        )

    def writes_job_lineage_edges(self) -> bool:
        return TRIGGER_JOB_DEPENDENCY in self.trigger_types and bool(self.job_dependencies)
