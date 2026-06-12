"""Command-line entry point for isolated scheduler DataJob sync."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

from .datajob_writer import SchedulerDataJobWriter
from .models import SchedulerJobMetadata
from .mysql_client import SchedulerMysqlClient


@dataclass
class SyncResult:
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    errors: dict[str, str] = field(default_factory=dict)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync scheduler jobs as DataHub DataJob entities.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--job", help="Single scheduler job_display_name to sync.")
    source.add_argument("--prefix", help="Sync scheduler jobs whose display name starts with prefix.")
    source.add_argument("--updated-since", help="Sync jobs updated since ISO timestamp.")
    source.add_argument(
        "--activity-since",
        help=(
            "Sync jobs with recent activity since ISO timestamp "
            "(last_build_start_time, build_update_time)."
        ),
    )
    parser.add_argument(
        "--lineage-only",
        action="store_true",
        help="Only refresh DataJob lineage aspects and dependency metadata.",
    )
    return parser


def _load_jobs(args: argparse.Namespace, reader: SchedulerMysqlClient) -> list[SchedulerJobMetadata]:
    if args.job:
        return [reader.fetch_job(args.job)]
    if args.prefix:
        return reader.fetch_jobs_by_prefix(args.prefix)
    if args.activity_since:
        since = datetime.fromisoformat(args.activity_since)
        return reader.fetch_jobs_activity_since(since)
    since = datetime.fromisoformat(args.updated_since)
    return reader.fetch_jobs_updated_since(since)


def run_sync(
    argv: Sequence[str] | None = None,
    *,
    reader: SchedulerMysqlClient | None = None,
    writer: SchedulerDataJobWriter | None = None,
) -> SyncResult:
    args = _parser().parse_args(argv)
    active_reader = reader or SchedulerMysqlClient()
    active_writer = writer or SchedulerDataJobWriter()

    jobs = _load_jobs(args, active_reader)
    result = SyncResult(total=len(jobs))
    print(f"[start] syncing {result.total} scheduler jobs to DataHub", flush=True)
    mode = "lineage-only" if args.lineage_only else "full"
    print(f"[mode] {mode}", flush=True)
    for index, metadata in enumerate(jobs, start=1):
        try:
            if args.lineage_only:
                active_writer.write_job_lineage(metadata)
            else:
                active_writer.write_job(metadata)
            result.succeeded += 1
        except Exception as exc:
            result.failed += 1
            result.errors[metadata.job_display_name] = str(exc)
            print(
                f"[fail] {metadata.job_display_name}: {exc}",
                flush=True,
            )
        if index % 100 == 0 or index == result.total:
            print(
                f"[progress] {index}/{result.total} "
                f"succeeded={result.succeeded} failed={result.failed}",
                flush=True,
            )
    print(
        f"[done] total={result.total} succeeded={result.succeeded} failed={result.failed}",
        flush=True,
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    result = run_sync(argv)
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
