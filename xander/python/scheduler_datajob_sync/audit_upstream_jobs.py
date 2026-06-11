"""Audit scheduler job upstream dependencies before/after DataHub lineage sync."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Sequence

from .models import SchedulerJobMetadata
from .mysql_client import SchedulerMysqlClient
from .trigger_parser import TRIGGER_JOB_DEPENDENCY, TRIGGER_TIMER


@dataclass
class AuditReport:
    total_jobs: int = 0
    timer_jobs: int = 0
    job_dependency_jobs: int = 0
    jobs_with_upstream: int = 0
    jobs_with_lineage_edges: int = 0
    dangling_upstream_refs: list[dict[str, str]] = field(default_factory=list)
    condition_pair_mismatches: list[dict[str, str | int]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def audit_jobs(jobs: Sequence[SchedulerJobMetadata]) -> AuditReport:
    known_names = {job.job_display_name for job in jobs if job.job_display_name}
    report = AuditReport(total_jobs=len(jobs))

    for job in jobs:
        if TRIGGER_TIMER in job.trigger_types:
            report.timer_jobs += 1
        if TRIGGER_JOB_DEPENDENCY in job.trigger_types:
            report.job_dependency_jobs += 1
        if job.upstream_jobs:
            report.jobs_with_upstream += 1
        if job.writes_job_lineage_edges():
            report.jobs_with_lineage_edges += 1
        if job.dependency_pair_mismatch_count > 0:
            report.condition_pair_mismatches.append(
                {
                    "job_display_name": job.job_display_name,
                    "upstream_count": len(job.upstream_jobs),
                    "condition_count": len(job.job_dependencies),
                    "mismatch_count": job.dependency_pair_mismatch_count,
                }
            )
        for dep in job.job_dependencies:
            if dep.upstream_job_display_name not in known_names:
                report.dangling_upstream_refs.append(
                    {
                        "job_display_name": job.job_display_name,
                        "upstream_job_display_name": dep.upstream_job_display_name,
                    }
                )

    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit scheduler upstream job dependencies from MySQL."
    )
    parser.add_argument(
        "--updated-since",
        default="2000-01-01T00:00:00",
        help="Only include jobs updated since ISO timestamp.",
    )
    parser.add_argument(
        "--output",
        help="Optional path to write JSON audit report.",
    )
    return parser


def run_audit(
    argv: Sequence[str] | None = None,
    *,
    reader: SchedulerMysqlClient | None = None,
) -> AuditReport:
    args = _parser().parse_args(argv)
    active_reader = reader or SchedulerMysqlClient()
    since = datetime.fromisoformat(args.updated_since)
    jobs = active_reader.fetch_jobs_updated_since(since)
    report = audit_jobs(jobs)
    payload = report.to_dict()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    run_audit(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
