"""Remove blf-schedule dataJob entities from DataHub that no longer exist in MySQL.

Usage (called by run_scheduler_datajob_sync_daily.sh after full sync):
    python -m scheduler_datajob_sync.cleanup_stale_datajobs [--dry-run | --apply]

Default is --dry-run; pass --apply to actually delete.
"""

from __future__ import annotations

import logging
import os
import sys

from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig

from .datajob_writer import ORCHESTRATOR, FLOW_ID, ENV as _ENV, make_scheduler_datajob_urn
from .mysql_client import _TABLE, _MIN_BATCH_EXEC_TIME, default_connection_factory

log = logging.getLogger(__name__)

_FLOW_URN = f"urn:li:dataFlow:({ORCHESTRATOR},{FLOW_ID},{_ENV})"
# URN prefix for fast name extraction without an extra API call
_URN_PREFIX = f"urn:li:dataJob:({_FLOW_URN},"


def _job_name_from_urn(urn: str) -> str | None:
    """Extract job_display_name from a blf-schedule dataJob URN."""
    if urn.startswith(_URN_PREFIX) and urn.endswith(")"):
        return urn[len(_URN_PREFIX):-1]
    return None


def _get_datahub_job_names(gms_url: str, token: str) -> set[str]:
    """Return the set of all blf-schedule dataJob names currently in DataHub.

    Uses DataHubGraph.get_urns_by_filter which handles internal scrolling and
    bypasses the 10 000-result cap of searchAcrossEntities.
    """
    cfg = DatahubClientConfig(server=gms_url, token=token or None)
    graph = DataHubGraph(cfg)
    names: set[str] = set()
    count = 0
    for urn in graph.get_urns_by_filter(entity_types=["dataJob"], platform="blf-schedule"):
        name = _job_name_from_urn(urn)
        if name:
            names.add(name)
        count += 1
        if count % 1000 == 0:
            print(f"  … fetched {count} DataHub dataJob URNs so far")
    return names


def _get_mysql_active_job_names() -> set[str]:
    """Return the set of active job_display_names from MySQL (above the batch_exec_time cutoff)."""
    conn = default_connection_factory()
    cur = conn.cursor()
    try:
        cur.execute(
            f"SELECT job_display_name FROM {_TABLE} WHERE batch_exec_time > %s",
            (_MIN_BATCH_EXEC_TIME,),
        )
        return {row["job_display_name"] for row in cur.fetchall()}
    finally:
        cur.close()
        conn.close()


def run_cleanup(dry_run: bool = True) -> dict[str, int]:
    gms_url = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
    token = os.environ.get("DATAHUB_GMS_TOKEN", "")

    print(f"[cleanup] Fetching MySQL active jobs (batch_exec_time > {_MIN_BATCH_EXEC_TIME}) …")
    mysql_names = _get_mysql_active_job_names()
    print(f"[cleanup] MySQL active jobs: {len(mysql_names)}")

    print("[cleanup] Fetching DataHub blf-schedule dataJob names …")
    dh_names = _get_datahub_job_names(gms_url, token)
    print(f"[cleanup] DataHub dataJobs: {len(dh_names)}")

    stale_names = dh_names - mysql_names
    print(f"[cleanup] Stale (in DataHub, not in MySQL): {len(stale_names)}")

    graph: DataHubGraph | None = None
    if not dry_run:
        cfg = DatahubClientConfig(server=gms_url, token=token or None)
        graph = DataHubGraph(cfg)

    deleted = failed = 0
    for name in sorted(stale_names):
        urn = make_scheduler_datajob_urn(name)
        if dry_run:
            print(f"  [dry-run] would delete: {name}")
        else:
            try:
                assert graph is not None
                graph.delete_entity(urn=urn, hard=True)
                print(f"  [deleted] {name}")
                deleted += 1
            except Exception as exc:
                print(f"  [error] {name}: {exc}", file=sys.stderr)
                failed += 1

    result = {"mysql": len(mysql_names), "datahub": len(dh_names), "stale": len(stale_names), "deleted": deleted, "failed": failed}
    print(
        f"[cleanup] done — mysql={result['mysql']} datahub={result['datahub']} "
        f"stale={result['stale']} deleted={result['deleted']} failed={result['failed']}"
    )
    return result


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    dry_run = "--apply" not in sys.argv
    if dry_run:
        print("[cleanup] DRY RUN — pass --apply to actually delete")
    run_cleanup(dry_run=dry_run)


if __name__ == "__main__":
    main()
