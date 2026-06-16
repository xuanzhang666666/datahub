"""Parse upstream job dependencies from scheduler job content XML."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .trigger_parser import _JOB_DEPENDENCY_RE

_UPSTREAM_PROJECTS_RE = re.compile(
    r"<upstreamProjects>\s*([^<]+?)\s*</upstreamProjects>",
    re.IGNORECASE,
)
# triggerCondition / threshold may be serialized as a self-closing empty tag
# (e.g. `<triggerCondition/>` when the value is blank, as with never_execute_job
# blocker dependencies). Accept both paired and self-closing forms; the capture
# group is None on the self-closing branch.
_JOB_DEPENDENCY_PROPERTY_RE = re.compile(
    r"<com\.wormpex\.dp\.pojo\.JobDependencyProperty>\s*"
    r"<upstreamJobName>\s*([^<]+?)\s*</upstreamJobName>\s*"
    r"(?:<triggerCondition>\s*([^<]*?)\s*</triggerCondition>|<triggerCondition\s*/>)\s*"
    r"(?:<threshold>\s*([^<]*?)\s*</threshold>|<threshold\s*/>)\s*"
    r"</com\.wormpex\.dp\.pojo\.JobDependencyProperty>",
    re.IGNORECASE | re.DOTALL,
)
_JOBS_BLOCK_RE = re.compile(r"<jobs>\s*(.*?)\s*</jobs>", re.DOTALL | re.IGNORECASE)
_CONDITIONS_BLOCK_RE = re.compile(
    r"<conditions>\s*(.*?)\s*</conditions>",
    re.DOTALL | re.IGNORECASE,
)
_STRING_TAG_RE = re.compile(r"<string>\s*([^<]+?)\s*</string>", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedJobDependency:
    upstream_job_display_name: str
    condition: str = ""
    status: str = "SUCCESS"


def _split_upstream_projects(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_string_list(block: str) -> list[str]:
    return [match.group(1).strip() for match in _STRING_TAG_RE.finditer(block)]


def _parse_job_dependency_properties(content: str) -> list[ParsedJobDependency]:
    dependencies: list[ParsedJobDependency] = []
    for match in _JOB_DEPENDENCY_PROPERTY_RE.finditer(content):
        name = match.group(1).strip()
        if not name:
            continue
        condition = (match.group(2) or "").strip()
        status = (match.group(3) or "").strip() or "SUCCESS"
        dependencies.append(
            ParsedJobDependency(
                upstream_job_display_name=name,
                condition=condition,
                status=status,
            )
        )
    return dependencies


def _parse_jobs_and_conditions(content: str) -> list[ParsedJobDependency]:
    jobs_match = _JOBS_BLOCK_RE.search(content)
    if jobs_match is None:
        return []

    jobs = _parse_string_list(jobs_match.group(1))
    if not jobs:
        return []

    conditions_match = _CONDITIONS_BLOCK_RE.search(content)
    conditions = (
        _parse_string_list(conditions_match.group(1)) if conditions_match else []
    )

    dependencies: list[ParsedJobDependency] = []
    for index, name in enumerate(jobs):
        condition = conditions[index] if index < len(conditions) else ""
        dependencies.append(
            ParsedJobDependency(
                upstream_job_display_name=name,
                condition=condition,
            )
        )
    return dependencies


def _parse_upstream_projects_only(content: str) -> list[ParsedJobDependency]:
    projects_match = _UPSTREAM_PROJECTS_RE.search(content)
    if projects_match is None:
        return []

    return [
        ParsedJobDependency(upstream_job_display_name=name)
        for name in _split_upstream_projects(projects_match.group(1))
    ]


def parse_job_dependencies_from_content(
    content_xml: str,
) -> list[ParsedJobDependency] | None:
    content = (content_xml or "").strip()
    if not content or not _JOB_DEPENDENCY_RE.search(content):
        return None

    for parser in (
        _parse_job_dependency_properties,
        _parse_jobs_and_conditions,
        _parse_upstream_projects_only,
    ):
        dependencies = parser(content)
        if dependencies:
            return dependencies

    return []
