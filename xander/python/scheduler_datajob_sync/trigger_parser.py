"""Parse scheduler job XML for trigger type and schedule metadata."""

from __future__ import annotations

import re
from dataclasses import dataclass

TRIGGER_TIMER = "timer"
TRIGGER_JOB_DEPENDENCY = "job_dependency"

_TIMER_TRIGGER_RE = re.compile(
    r"hudson\.triggers\.TimerTrigger.*?<spec>\s*([^<]+?)\s*</spec>",
    re.DOTALL | re.IGNORECASE,
)
_JOB_DEPENDENCY_RE = re.compile(r"job[-_]?dependency", re.IGNORECASE)
_TIME_HOUR_PARAM_RE = re.compile(
    r"<upstreamParams>\s*([^<]+?)\s*</upstreamParams>",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedTrigger:
    trigger_types: list[str]
    cron_schedule: str
    time_hour_param: str


def parse_job_triggers(content_xml: str) -> ParsedTrigger:
    content = (content_xml or "").strip()
    if not content:
        return ParsedTrigger(trigger_types=[], cron_schedule="", time_hour_param="")

    trigger_types: list[str] = []
    cron_schedule = ""
    time_hour_param = ""

    timer_match = _TIMER_TRIGGER_RE.search(content)
    if timer_match or "hudson.triggers.TimerTrigger" in content:
        trigger_types.append(TRIGGER_TIMER)
        if timer_match:
            cron_schedule = timer_match.group(1).strip()

    if _JOB_DEPENDENCY_RE.search(content):
        trigger_types.append(TRIGGER_JOB_DEPENDENCY)
        param_match = _TIME_HOUR_PARAM_RE.search(content)
        if param_match:
            time_hour_param = param_match.group(1).strip()

    return ParsedTrigger(
        trigger_types=trigger_types,
        cron_schedule=cron_schedule,
        time_hour_param=time_hour_param,
    )
