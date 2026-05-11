#!/usr/bin/env python3
"""Emit POST /api/graphql JSON body: { \"query\": <one line>, \"variables\": {...} }."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VARS_PATH = ROOT / "gms_variables_dw_order_v1.json"
QUERY_PATH = ROOT / "gms_query_dataset_for_llm.graphql"
OUT_PATH = ROOT / "gms_body_dataset_for_llm_dw_order_v1.json"


def main() -> None:
    query = QUERY_PATH.read_text(encoding="utf-8")
    query_one_line = " ".join(line.strip() for line in query.splitlines() if line.strip())
    variables = json.loads(VARS_PATH.read_text(encoding="utf-8"))
    OUT_PATH.write_text(
        json.dumps({"query": query_one_line, "variables": variables}, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
