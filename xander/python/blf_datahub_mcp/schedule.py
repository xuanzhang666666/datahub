"""BLF scheduler DataJob naming helpers for DataHub MCP."""

from __future__ import annotations

import urllib.parse

ORCHESTRATOR = "blf-schedule"
FLOW_ID = "blf-schedule"
ENV = "PROD"

_SCHEDULE_WEB_BASE = "https://schedule.corp.bianlifeng.com"


def make_scheduler_datajob_urn(job_display_name: str) -> str:
    """Build the DataHub DataJob URN for a BLF scheduler job."""
    if not job_display_name or not job_display_name.strip():
        raise ValueError("job_display_name cannot be empty")
    flow_urn = f"urn:li:dataFlow:({ORCHESTRATOR},{FLOW_ID},{ENV})"
    return f"urn:li:dataJob:({flow_urn},{job_display_name.strip()})"


def make_datahub_datajob_url(datajob_urn: str, public_base_url: str) -> str:
    """Build the DataHub UI URL for a DataJob URN (path prefix: /tasks/)."""
    encoded = urllib.parse.quote(datajob_urn, safe="")
    return f"{public_base_url.rstrip().rstrip('/')}/tasks/{encoded}"


def scheduler_web_url(job_display_name: str) -> str:
    """Build the BLF scheduler web URL for a job."""
    return f"{_SCHEDULE_WEB_BASE}/job/{job_display_name.strip()}"


def job_base(job_display_name: str, public_base_url: str) -> dict[str, str]:
    """Build the standard base dict for schedule job tool responses."""
    urn = make_scheduler_datajob_urn(job_display_name)
    return {
        "job_display_name": job_display_name.strip(),
        "datajob_urn": urn,
        "datahub_url": make_datahub_datajob_url(urn, public_base_url),
        "schedule_url": scheduler_web_url(job_display_name),
    }
