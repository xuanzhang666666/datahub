#!/usr/bin/env python3
"""Remove wormpex.dw partition-stats structured properties from a dataset via GMS OpenAPI PATCH.

Uses the same endpoint/shape as ``sync_partition_stats_to_datahub_trino.py`` but ``op: remove``.
This avoids GraphQL ``upsertStructuredProperties``, which validates the full aspect and can fail
when definitions for those property URNs are missing.

Example::

  export DATAHUB_GMS_URL=https://your-gms
  export DATAHUB_GMS_TOKEN=...
  python3 xander/python/remove_partition_stats_structured_properties.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_DATAHUB_GMS = os.getenv("DATAHUB_GMS_URL", "http://127.0.0.1:8080")
DEFAULT_DATASET_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:hive,blf-prod-hive.default.dw_order_v1,PROD)"
)
PROP_SNAPSHOT = "urn:li:structuredProperty:wormpex.dw.partitionStatsSnapshot"
PROP_COLLECTED_AT = "urn:li:structuredProperty:wormpex.dw.partitionStatsCollectedAt"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="PATCH-remove wormpex.dw partition stats structured properties on DataHub."
    )
    p.add_argument("--datahub-gms", default=os.getenv("DATAHUB_GMS_URL", DEFAULT_DATAHUB_GMS))
    p.add_argument("--dataset-urn", default=os.getenv("DATASET_URN", DEFAULT_DATASET_URN))
    p.add_argument(
        "--token",
        default=os.getenv("DATAHUB_GMS_TOKEN"),
        help="Bearer token for GMS (or set DATAHUB_GMS_TOKEN)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print JSON body and URL only; do not call DataHub",
    )
    return p.parse_args()


def dataset_structured_properties_url(gms_base: str, dataset_urn: str) -> str:
    encoded = urllib.parse.quote(dataset_urn, safe="")
    base = gms_base.rstrip("/")
    return f"{base}/openapi/v3/entity/dataset/{encoded}/structuredProperties"


def build_remove_patch_body() -> dict:
    return {
        "patch": [
            {"op": "remove", "path": f"/properties/{PROP_SNAPSHOT}"},
            {"op": "remove", "path": f"/properties/{PROP_COLLECTED_AT}"},
        ],
        "arrayPrimaryKeys": {"properties": ["propertyUrn"]},
    }


def patch_remove(gms_url: str, dataset_urn: str, token: str | None) -> None:
    body = build_remove_patch_body()
    data = json.dumps(body).encode("utf-8")
    url = dataset_structured_properties_url(gms_url, dataset_urn)
    req = urllib.request.Request(
        url,
        data=data,
        method="PATCH",
        headers={
            "Content-Type": "application/json-patch+json",
            "Accept": "application/json",
        },
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.read()
            if resp.status != 200:
                raise RuntimeError(f"DataHub PATCH returned HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"DataHub PATCH failed HTTP {e.code}: {detail}") from e


def main() -> int:
    args = parse_args()
    url = dataset_structured_properties_url(args.datahub_gms, args.dataset_urn)
    body = build_remove_patch_body()
    print("URL:", url)
    print("Body:", json.dumps(body, indent=2))

    if args.dry_run:
        return 0

    try:
        patch_remove(args.datahub_gms, args.dataset_urn, args.token)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1

    print("Removed wormpex.dw partition stats structured properties (if present).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
