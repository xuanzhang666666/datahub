#!/usr/bin/env python3
"""Fetch recent Hive partitions + row counts via Trino, write to DataHub structured properties.

Deploy/run helpers (this repo, under xander/): xander/run/run_partition_stats_on_neo4j2.sh (on-server),
xander/run/exec_partition_sync_via_bastion.example.sh (laptop via bastion).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

# China Standard Time (no DST); avoids tzdata requirement in minimal images.
BEIJING_TZ = timezone(timedelta(hours=8))

try:
    import trino
except ImportError as exc:  # pragma: no cover - runtime dependency check
    print(
        "Missing dependency: trino.\n"
        "Install it with: python3 -m pip install trino",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc

DEFAULT_TRINO_HOST = "10.253.7.167"
DEFAULT_TRINO_PORT = 8081
DEFAULT_TRINO_USER = "xuan.zhang"
DEFAULT_TRINO_CATALOG = "hive"
DEFAULT_TRINO_SCHEMA = "default"
DEFAULT_TABLE = "dw_order_v1"
DEFAULT_PARTITION_COL = "dt"
DEFAULT_LIMIT = 30

DEFAULT_DATAHUB_GMS = os.getenv("DATAHUB_GMS_URL", "http://127.0.0.1:8080")
DEFAULT_DATASET_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:hive,blf-prod-hive.default.dw_order_v1,PROD)"
)
PROP_SNAPSHOT = "urn:li:structuredProperty:wormpex.dw.partitionStatsSnapshot"
PROP_COLLECTED_AT = "urn:li:structuredProperty:wormpex.dw.partitionStatsCollectedAt"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Query partition stats with Trino, PATCH wormpex structured properties on DataHub."
    )
    p.add_argument("--host", default=os.getenv("TRINO_HOST", DEFAULT_TRINO_HOST))
    p.add_argument("--port", type=int, default=int(os.getenv("TRINO_PORT", DEFAULT_TRINO_PORT)))
    p.add_argument("--user", default=os.getenv("TRINO_USER", DEFAULT_TRINO_USER))
    p.add_argument(
        "--password",
        default=os.getenv("TRINO_PASSWORD"),
        help=(
            "Optional Basic auth password. Most internal coordinators use username-only "
            "(leave unset); set TRINO_PASSWORD only if yours requires HTTP basic auth."
        ),
    )
    p.add_argument("--catalog", default=os.getenv("TRINO_CATALOG", DEFAULT_TRINO_CATALOG))
    p.add_argument("--schema", default=os.getenv("TRINO_SCHEMA", DEFAULT_TRINO_SCHEMA))
    p.add_argument("--table", default=os.getenv("HIVE_TABLE", DEFAULT_TABLE))
    p.add_argument(
        "--partition-col",
        default=os.getenv("PARTITION_COL", DEFAULT_PARTITION_COL),
        help="Hive partition column name (default: dt)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=int(os.getenv("PARTITION_LIMIT", DEFAULT_LIMIT)),
        help="Max number of partition values (ordered DESC by partition key string)",
    )
    p.add_argument("--datahub-gms", default=os.getenv("DATAHUB_GMS_URL", DEFAULT_DATAHUB_GMS))
    p.add_argument("--dataset-urn", default=os.getenv("DATASET_URN", DEFAULT_DATASET_URN))
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print JSON only; do not call DataHub",
    )
    p.add_argument(
        "--token",
        default=os.getenv("DATAHUB_GMS_TOKEN"),
        help="Optional Bearer token for GMS",
    )
    return p.parse_args()


def build_count_sql(table: str, partition_col: str, limit: int) -> str:
    """One aggregation; largest partition keys first (string sort).

    Requires Trino connection ``catalog`` / ``schema`` to match the target table
    (same as trino.dbapi.connect catalog=..., schema=...).
    """
    return f"""
SELECT CAST({partition_col} AS varchar) AS pkey, COUNT(*) AS row_count
FROM {table}
GROUP BY {partition_col}
ORDER BY CAST({partition_col} AS varchar) DESC
LIMIT {int(limit)}
""".strip()


def fetch_partition_stats(args: argparse.Namespace) -> tuple[list[dict[str, Any]], str]:
    auth = None
    if args.password:
        auth = trino.auth.BasicAuthentication(args.user, args.password)

    conn = trino.dbapi.connect(
        host=args.host,
        port=args.port,
        user=args.user,
        catalog=args.catalog,
        schema=args.schema,
        auth=auth,
    )
    sql = build_count_sql(args.table, args.partition_col, args.limit)
    cur = conn.cursor()
    try:
        cur.execute(sql)
        rows = cur.fetchall()
        pk = args.partition_col
        partitions = [{pk: str(r[0]), "rows": int(r[1])} for r in rows]
    finally:
        cur.close()
        conn.close()

    collected_at = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return partitions, collected_at


def dataset_structured_properties_url(gms_base: str, dataset_urn: str) -> str:
    encoded = urllib.parse.quote(dataset_urn, safe="")
    base = gms_base.rstrip("/")
    return f"{base}/openapi/v3/entity/dataset/{encoded}/structuredProperties"


def patch_structured_properties(
    gms_url: str,
    dataset_urn: str,
    snapshot_json: str,
    collected_at: str,
    token: str | None,
) -> None:
    body = {
        "patch": [
            {
                "op": "replace",
                "path": f"/properties/{PROP_SNAPSHOT}",
                "value": {
                    "propertyUrn": PROP_SNAPSHOT,
                    "values": [{"string": snapshot_json}],
                },
            },
            {
                "op": "replace",
                "path": f"/properties/{PROP_COLLECTED_AT}",
                "value": {
                    "propertyUrn": PROP_COLLECTED_AT,
                    "values": [{"string": collected_at}],
                },
            },
        ],
        "arrayPrimaryKeys": {"properties": ["propertyUrn"]},
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        dataset_structured_properties_url(gms_url, dataset_urn),
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
    sql = build_count_sql(args.table, args.partition_col, args.limit)
    print("Trino SQL:\n", sql, "\n", sep="")

    partitions, collected_at = fetch_partition_stats(args)
    payload = {
        "schemaVersion": 1,
        "table": f"{args.catalog}.{args.schema}.{args.table}",
        "partitionCol": args.partition_col,
        "partitions": partitions,
    }
    snapshot_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    print("Collected at (Beijing):", collected_at)
    print("Snapshot JSON length:", len(snapshot_json))
    if len(snapshot_json) > 8000:
        print(
            "Warning: JSON is large; if PATCH fails, shorten --limit or store summary only.",
            file=sys.stderr,
        )

    if args.dry_run:
        print(snapshot_json)
        return 0

    try:
        patch_structured_properties(
            args.datahub_gms,
            args.dataset_urn,
            snapshot_json,
            collected_at,
            args.token,
        )
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1

    print("DataHub structured properties updated OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
