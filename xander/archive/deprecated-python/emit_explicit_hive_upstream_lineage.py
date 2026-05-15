#!/usr/bin/env python3
"""Emit UpstreamLineage (table + optional field-level) to DataHub via REST.

Hive Metastore ingestion does not infer ODS→PDW table edges for normal tables.
Use this after you know upstream datasets.

Fine-grained mode (--fine-grained-same-name) maps listed columns to the SAME field name on the
upstream table. Many pdw tables do not match ods column-for-column (renames, expressions,
drops); only use it after checking DDL or ETL. Without --columns, a built-in list applies only
to the hand-verified pair default.pdw_opc_flag_contact / default.ods_opc_flag_contact.

Example (neo4j2 actions container):

  export DATAHUB_GMS_URL=http://datahub-gms:8080
  python3 emit_explicit_hive_upstream_lineage.py \\
    --platform-instance blf-prod-hive \\
    --downstream-db-table default.pdw_opc_flag_contact \\
    --upstream-db-table default.ods_opc_flag_contact \\
    --fine-grained-same-name
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Sequence

from datahub.emitter import mce_builder as builder
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.metadata.schema_classes import (
    DatasetLineageTypeClass,
    FineGrainedLineageClass,
    FineGrainedLineageDownstreamTypeClass,
    FineGrainedLineageUpstreamTypeClass,
    UpstreamClass,
    UpstreamLineageClass,
)

# Verified once via Trino SHOW CREATE: identical column names/order vs ods_opc_flag_contact.
# Do not reuse this tuple for other tables.
_PDW_OPC_FLAG_CONTACT_COLUMNS: Sequence[str] = (
    "id",
    "flag_code",
    "types",
    "name",
    "phone",
    "creator",
    "submit_user_id",
    "create_time",
    "update_time",
    "state",
    "create_reason",
    "invalid_type",
    "invalid_detail",
    "work_type_path",
    "work_type_value",
    "dt",
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Emit explicit Hive upstream lineage (REST).")
    p.add_argument(
        "--datahub-gms",
        default=os.getenv("DATAHUB_GMS_URL", "http://127.0.0.1:8080"),
    )
    p.add_argument("--token", default=os.getenv("DATAHUB_GMS_TOKEN"))
    p.add_argument("--platform-instance", default="blf-prod-hive")
    p.add_argument("--env", default="PROD")
    p.add_argument(
        "--downstream-db-table",
        required=True,
        help="Hive db.table for downstream, e.g. default.pdw_opc_flag_contact",
    )
    p.add_argument(
        "--upstream-db-table",
        action="append",
        dest="upstreams",
        required=True,
        help="Hive db.table upstream (repeat for multiple)",
    )
    p.add_argument(
        "--fine-grained-same-name",
        action="store_true",
        help="Emit FIELD-level mappings: for each name in --columns (or the built-in list only "
        "for pdw_opc_flag_contact/ods_opc_flag_contact), upstream field N → downstream field N. "
        "Wrong if ods→pdw renames or computes columns; use coarse-only or curated --columns. "
        "Requires one upstream unless --fine-grained-upstream is set.",
    )
    p.add_argument(
        "--fine-grained-upstream",
        default=None,
        help="db.table of the upstream used for field URNs when multiple coarse upstreams exist",
    )
    p.add_argument(
        "--columns",
        default=None,
        help="Comma-separated downstream field paths for --fine-grained-same-name (each must "
        "match an upstream field name you intend to map 1:1). Required for all pairs except "
        "the built-in pdw_opc_flag_contact example.",
    )
    return p.parse_args()


def _parse_columns(arg: str | None) -> List[str]:
    if not arg:
        return []
    return [c.strip() for c in arg.split(",") if c.strip()]


def _default_columns_for_pair(downstream: str, upstreams: List[str]) -> List[str]:
    if (
        downstream == "default.pdw_opc_flag_contact"
        and upstreams == ["default.ods_opc_flag_contact"]
    ):
        return list(_PDW_OPC_FLAG_CONTACT_COLUMNS)
    return []


def _field_lineage_same_name(
    downstream_urn: str,
    upstream_urn: str,
    columns: Sequence[str],
) -> List[FineGrainedLineageClass]:
    out: List[FineGrainedLineageClass] = []
    for col in columns:
        u_f = builder.make_schema_field_urn(upstream_urn, col)
        d_f = builder.make_schema_field_urn(downstream_urn, col)
        out.append(
            FineGrainedLineageClass(
                upstreamType=FineGrainedLineageUpstreamTypeClass.FIELD_SET,
                upstreams=[u_f],
                downstreamType=FineGrainedLineageDownstreamTypeClass.FIELD,
                downstreams=[d_f],
            )
        )
    return out


def main() -> int:
    args = parse_args()
    downstream_urn = builder.make_dataset_urn_with_platform_instance(
        platform="hive",
        name=args.downstream_db_table,
        platform_instance=args.platform_instance,
        env=args.env,
    )
    upstreams = [
        UpstreamClass(
            dataset=builder.make_dataset_urn_with_platform_instance(
                platform="hive",
                name=u,
                platform_instance=args.platform_instance,
                env=args.env,
            ),
            type=DatasetLineageTypeClass.TRANSFORMED,
        )
        for u in args.upstreams
    ]

    fine_list: List[FineGrainedLineageClass] | None = None
    if args.fine_grained_same_name:
        if args.fine_grained_upstream:
            u_tbl = args.fine_grained_upstream
            if u_tbl not in args.upstreams:
                print(
                    "--fine-grained-upstream must match one of --upstream-db-table values",
                    file=sys.stderr,
                )
                return 2
            upstream_urn_for_fields = builder.make_dataset_urn_with_platform_instance(
                platform="hive",
                name=u_tbl,
                platform_instance=args.platform_instance,
                env=args.env,
            )
        else:
            if len(args.upstreams) != 1:
                print(
                    "--fine-grained-same-name needs exactly one --upstream-db-table "
                    "or use --fine-grained-upstream",
                    file=sys.stderr,
                )
                return 2
            upstream_urn_for_fields = upstreams[0].dataset

        cols = _parse_columns(args.columns) or _default_columns_for_pair(
            args.downstream_db_table, args.upstreams
        )
        if not cols:
            print(
                "No columns: pass --columns a,b,c for this table pair",
                file=sys.stderr,
            )
            return 2
        fine_list = _field_lineage_same_name(
            downstream_urn, str(upstream_urn_for_fields), cols
        )

    aspect = UpstreamLineageClass(upstreams=upstreams, fineGrainedLineages=fine_list)
    mcp = MetadataChangeProposalWrapper(entityUrn=downstream_urn, aspect=aspect)
    emitter = DatahubRestEmitter(args.datahub_gms, token=args.token)
    emitter.emit_mcp(mcp)
    print("Emitted upstreamLineage for", downstream_urn)
    for u in upstreams:
        print("  upstream:", u.dataset)
    if fine_list is not None:
        print("  fineGrainedLineages:", len(fine_list), "column mappings")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - CLI
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
