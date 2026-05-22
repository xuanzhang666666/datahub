"""Scan Hive datasets missing ``datasetProperties`` (Summary title shows full URN path).

Usage::

  cd xander/python
  PYTHONPATH=. python3 -m job_info_sync_datahub.audit_missing_dataset_properties \\
      --gms-url http://neo4j2.dp.data.bj1.wormpex.com:8080

Writes:
  - missing_dataset_properties.jsonl (one row per table)
  - missing_dataset_properties_tables.txt (db.table lines for batch ingest)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

_DEFAULT_PLATFORM_INSTANCE = "blf-prod-hive"
_DEFAULT_ENV = "PROD"
_URN_KEY_RE = re.compile(r"urn:li:dataset:\(urn:li:dataPlatform:hive,([^,]+),")


def _graphql(gms_url: str, query: str, variables: Dict[str, Any], *, timeout: int = 180) -> Dict[str, Any]:
    last_err: Optional[BaseException] = None
    for attempt in range(1, 4):
        try:
            body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
            req = urllib.request.Request(
                f"{gms_url.rstrip('/')}/api/graphql",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if payload.get("errors"):
                raise RuntimeError(json.dumps(payload["errors"], ensure_ascii=False))
            return payload["data"]
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            last_err = exc
            if attempt < 3:
                print(f"[WARN] graphql attempt {attempt} failed: {exc}; retrying", file=sys.stderr)
                continue
            raise
    raise RuntimeError(f"graphql failed: {last_err}")


def _urn_key(urn: str) -> str:
    m = _URN_KEY_RE.search(urn)
    return m.group(1) if m else urn


def _to_fqtn(key: str, platform_instance: str) -> str:
    prefix = f"{platform_instance}."
    if key.startswith(prefix):
        return key[len(prefix) :]
    return key


def scroll_hive_datasets(
    gms_url: str,
    *,
    platform_instance: str,
    scroll_query: str,
    page_size: int,
    max_pages: int,
) -> List[Dict[str, Any]]:
    query = """
    query($input: ScrollAcrossEntitiesInput!) {
      scrollAcrossEntities(input: $input) {
        total
        count
        nextScrollId
        searchResults {
          entity {
            urn
            ... on Dataset {
              name
              properties { name }
            }
          }
        }
      }
    }
    """
    rows: List[Dict[str, Any]] = []
    scroll_id: Optional[str] = None
    page = 0
    while True:
        page += 1
        if max_pages > 0 and page > max_pages:
            break
        variables = {
            "input": {
                "types": ["DATASET"],
                "query": scroll_query,
                "count": page_size,
            }
        }
        if scroll_id:
            variables["input"]["scrollId"] = scroll_id
        data = _graphql(gms_url, query, variables)["scrollAcrossEntities"]
        for hit in data.get("searchResults") or []:
            ent = hit.get("entity") or {}
            urn = ent.get("urn")
            if not urn or "urn:li:dataPlatform:hive," not in urn:
                continue
            key = _urn_key(urn)
            if not key.startswith(f"{platform_instance}."):
                continue
            rows.append(
                {
                    "urn": urn,
                    "urn_key": key,
                    "fqtn": _to_fqtn(key, platform_instance),
                    "graphql_name": ent.get("name"),
                    "properties_name": (ent.get("properties") or {}).get("name"),
                }
            )
        scroll_id = data.get("nextScrollId")
        if page == 1:
            print(f"[INFO] scroll total (reported)={data.get('total')} page_size={page_size}")
        if not scroll_id:
            break
        if page % 20 == 0:
            print(f"[INFO] scrolled pages={page} datasets={len(rows)}", file=sys.stderr)
    return rows


def classify(rows: List[Dict[str, Any]], platform_instance: str) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    missing: List[Dict[str, Any]] = []
    stats = defaultdict(int)
    for row in rows:
        key = row["urn_key"]
        props_name = row.get("properties_name")
        gql_name = row.get("graphql_name")
        if props_name is None:
            stats["missing_datasetProperties"] += 1
            reason = "no_datasetProperties"
        elif props_name == key or (gql_name and gql_name == key):
            stats["bad_display_name_equals_urn_key"] += 1
            reason = "name_equals_full_urn_key"
        else:
            stats["ok"] += 1
            continue
        row = {**row, "reason": reason}
        missing.append(row)
    return missing, dict(stats)


def write_outputs(
    missing: List[Dict[str, Any]],
    *,
    jsonl_path: str,
    tables_path: str,
) -> None:
    by_db: Dict[str, int] = defaultdict(int)
    with open(jsonl_path, "w", encoding="utf-8") as jf:
        for row in sorted(missing, key=lambda r: r["fqtn"]):
            by_db[row["fqtn"].split(".", 1)[0]] += 1
            jf.write(json.dumps(row, ensure_ascii=False) + "\n")
    with open(tables_path, "w", encoding="utf-8") as tf:
        for row in sorted(missing, key=lambda r: r["fqtn"]):
            tf.write(row["fqtn"] + "\n")
    print("[INFO] missing_by_db:")
    for db, cnt in sorted(by_db.items(), key=lambda x: (-x[1], x[0])):
        print(f"  {db}: {cnt}")
    print(f"[DONE] jsonl: {jsonl_path}")
    print(f"[DONE] tables: {tables_path}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gms-url", default=os.getenv("DATAHUB_GMS_URL", "http://localhost:8080"))
    p.add_argument("--platform-instance", default=os.getenv("BLF_DATAHUB_PLATFORM_INSTANCE", _DEFAULT_PLATFORM_INSTANCE))
    p.add_argument("--scroll-query", default="blf-prod-hive", help="ES scroll query (prefix match)")
    p.add_argument("--page-size", type=int, default=100)
    p.add_argument("--max-pages", type=int, default=0, help="0 = no limit")
    p.add_argument("--out-dir", default=".")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    jsonl_path = os.path.join(out_dir, "missing_dataset_properties.jsonl")
    tables_path = os.path.join(out_dir, "missing_dataset_properties_tables.txt")

    try:
        rows = scroll_hive_datasets(
            args.gms_url,
            platform_instance=args.platform_instance,
            scroll_query=args.scroll_query,
            page_size=args.page_size,
            max_pages=args.max_pages,
        )
    except urllib.error.URLError as exc:
        print(f"[ERROR] GMS unreachable: {exc}", file=sys.stderr)
        return 1

    print(f"[INFO] scanned blf-prod-hive hive datasets: {len(rows)}")
    missing, stats = classify(rows, args.platform_instance)
    print(f"[INFO] stats: {json.dumps(stats, ensure_ascii=False)}")
    print(f"[INFO] need_ingest_count={len(missing)}")
    write_outputs(missing, jsonl_path=jsonl_path, tables_path=tables_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
