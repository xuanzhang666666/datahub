"""Inspect DataJob upstream edge properties stored in DataHub."""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request


def fetch_datajob(gms_url: str, datajob_urn: str, token: str | None) -> dict[str, object]:
    encoded = urllib.parse.quote(datajob_urn, safe="")
    url = (
        f"{gms_url.rstrip('/')}/openapi/v3/entity/dataJob/"
        f"{encoded}?aspects=dataJobInputOutput,dataJobInfo"
    )
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _aspect_value(payload: dict[str, object], aspect_name: str) -> dict[str, object]:
    aspect = payload.get(aspect_name)
    if not isinstance(aspect, dict):
        return {}
    value = aspect.get("value")
    return value if isinstance(value, dict) else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect DataJob inputDatajobEdges in DataHub.")
    parser.add_argument("--job", required=True, help="job_display_name")
    args = parser.parse_args(argv)

    from .datajob_writer import make_scheduler_datajob_urn

    gms_url = os.getenv("DATAHUB_GMS_URL", "http://127.0.0.1:8080")
    token = os.getenv("DATAHUB_GMS_TOKEN")
    urn = make_scheduler_datajob_urn(args.job)
    payload = fetch_datajob(gms_url, urn, token)
    io_value = _aspect_value(payload, "dataJobInputOutput")
    info_value = _aspect_value(payload, "dataJobInfo")

    report = {
        "urn": urn,
        "trigger_type": info_value.get("customProperties", {}).get("trigger_type")
        if isinstance(info_value.get("customProperties"), dict)
        else None,
        "inputDatajobs": io_value.get("inputDatajobs"),
        "inputDatajobEdges": io_value.get("inputDatajobEdges"),
        "scheduler_job_dependencies": info_value.get("customProperties", {}).get(
            "scheduler_job_dependencies"
        )
        if isinstance(info_value.get("customProperties"), dict)
        else None,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
