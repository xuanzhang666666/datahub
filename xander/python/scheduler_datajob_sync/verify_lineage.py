"""Verify DataJob upstream lineage resolution against scheduler MySQL metadata."""

from __future__ import annotations

import argparse
import json
import os
import random
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Callable, Sequence

from .datajob_writer import make_scheduler_datajob_urn
from .models import SchedulerJobMetadata
from .mysql_client import SchedulerMysqlClient


@dataclass
class LineageVerifyReport:
    sampled_jobs: int = 0
    jobs_with_expected_edges: int = 0
    resolved_edges: int = 0
    missing_edges: int = 0
    dangling_expected_upstream: int = 0
    samples: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _entity_url(gms_base: str, datajob_urn: str) -> str:
    encoded = urllib.parse.quote(datajob_urn, safe="")
    return (
        f"{gms_base.rstrip('/')}/openapi/v3/entity/dataJob/"
        f"{encoded}?aspects=dataJobInputOutput,dataJobInfo"
    )


def fetch_datajob_aspects(
    gms_url: str,
    datajob_urn: str,
    token: str | None = None,
) -> dict[str, object]:
    req = urllib.request.Request(
        _entity_url(gms_url, datajob_urn),
        headers={"Accept": "application/json"},
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET DataJob HTTP {exc.code}: {detail}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected DataJob response shape")
    return payload


def _input_datajobs(payload: dict[str, object]) -> list[str]:
    aspects = payload.get("aspects")
    if not isinstance(aspects, dict):
        return []
    aspect = aspects.get("dataJobInputOutput")
    if not isinstance(aspect, dict):
        return []
    value = aspect.get("value")
    if not isinstance(value, dict):
        return []
    raw = value.get("inputDatajobs")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]


def verify_jobs(
    jobs: Sequence[SchedulerJobMetadata],
    *,
    gms_url: str,
    token: str | None = None,
    entity_fetcher: Callable[[str], dict[str, object]] | None = None,
) -> LineageVerifyReport:
    fetcher = entity_fetcher or (
        lambda urn: fetch_datajob_aspects(gms_url, urn, token=token)
    )
    report = LineageVerifyReport(sampled_jobs=len(jobs))

    for job in jobs:
        expected = [
            make_scheduler_datajob_urn(dep.upstream_job_display_name)
            for dep in job.job_dependencies
            if job.writes_job_lineage_edges()
        ]
        if not expected:
            continue
        report.jobs_with_expected_edges += 1
        payload = fetcher(make_scheduler_datajob_urn(job.job_display_name))
        actual = set(_input_datajobs(payload))
        resolved = [urn for urn in expected if urn in actual]
        missing = [urn for urn in expected if urn not in actual]
        report.resolved_edges += len(resolved)
        report.missing_edges += len(missing)
        report.samples.append(
            {
                "job_display_name": job.job_display_name,
                "expected_upstream_count": len(expected),
                "resolved_upstream_count": len(resolved),
                "missing_upstreams": missing,
            }
        )
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify scheduler DataJob upstream lineage in DataHub."
    )
    parser.add_argument("--sample-size", type=int, default=20)
    parser.add_argument(
        "--job",
        action="append",
        default=[],
        help="Explicit job_display_name to verify (repeatable).",
    )
    parser.add_argument(
        "--output",
        help="Optional path to write JSON verification report.",
    )
    return parser


def run_verify(
    argv: Sequence[str] | None = None,
    *,
    reader: SchedulerMysqlClient | None = None,
    gms_url: str | None = None,
    token: str | None = None,
) -> LineageVerifyReport:
    args = _parser().parse_args(argv)
    active_reader = reader or SchedulerMysqlClient()
    active_gms_url = gms_url or os.getenv("DATAHUB_GMS_URL", "http://127.0.0.1:8080")
    active_token = token if token is not None else os.getenv("DATAHUB_GMS_TOKEN")

    if args.job:
        jobs = [active_reader.fetch_job(name) for name in args.job]
    else:
        from datetime import datetime

        all_jobs = active_reader.fetch_jobs_updated_since(datetime(2000, 1, 1))
        lineage_jobs = [job for job in all_jobs if job.writes_job_lineage_edges()]
        sample_size = min(args.sample_size, len(lineage_jobs))
        jobs = random.sample(lineage_jobs, sample_size) if sample_size else []

    report = verify_jobs(
        jobs,
        gms_url=active_gms_url,
        token=active_token,
    )
    payload = report.to_dict()
    if report.jobs_with_expected_edges:
        payload["resolve_rate"] = round(
            report.resolved_edges
            / max(report.resolved_edges + report.missing_edges, 1),
            4,
        )
    else:
        payload["resolve_rate"] = 1.0
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    run_verify(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
