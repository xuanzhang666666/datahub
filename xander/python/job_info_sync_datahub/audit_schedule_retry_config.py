"""Audit retry-build configuration from scheduler DataJob XML stored in DataHub."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

FIXED_DELAY_CLASS = "com.chikli.hudson.plugin.naginator.FixedDelay"
JOB_CONTENT_XML_URN = (
    "urn:li:structuredProperty:blf.data.schedule.job_content_xml"
)
SCHEDULE_DATAJOB_URN_PREFIX = (
    "urn:li:dataJob:(urn:li:dataFlow:"
    "(blf-schedule,blf-schedule,PROD),"
)
_CSV_FIELDS = [
    "job_display_name",
    "任务名前缀",
    "下游任务数量",
    "datajob_urn",
    "status",
    "has_fixed_delay",
    "job_xml_chars",
]

_ENTITY_FIELDS = """
urn
... on DataJob {
  jobId
  properties {
    name
    customProperties { key value }
  }
  structuredProperties {
    properties {
      structuredProperty { urn }
      values { ... on StringValue { stringValue } }
    }
  }
}
"""

_FIRST_PAGE_QUERY = """
query($count: Int!) {
  scrollAcrossEntities(input: {
    types: [DATA_JOB]
    query: "*"
    count: $count
  }) {
    nextScrollId
    searchResults { entity { %s } }
  }
}
""" % _ENTITY_FIELDS

_NEXT_PAGE_QUERY = """
query($count: Int!, $scrollId: String!) {
  scrollAcrossEntities(input: {
    types: [DATA_JOB]
    query: "*"
    count: $count
    scrollId: $scrollId
  }) {
    nextScrollId
    searchResults { entity { %s } }
  }
}
""" % _ENTITY_FIELDS


@dataclass(frozen=True)
class RetryAuditResult:
    job_display_name: str
    job_name_prefix: str
    downstream_job_count: int
    datajob_urn: str
    status: str
    has_fixed_delay: bool
    job_xml_chars: int


def _job_xml(entity: dict[str, Any]) -> str:
    properties = (entity.get("structuredProperties") or {}).get("properties") or []
    for assignment in properties:
        property_urn = (assignment.get("structuredProperty") or {}).get("urn")
        if property_urn != JOB_CONTENT_XML_URN:
            continue
        for value in assignment.get("values") or []:
            if isinstance(value, dict) and isinstance(value.get("stringValue"), str):
                return value["stringValue"]
            if isinstance(value, str):
                return value
    return ""


def _job_display_name(entity: dict[str, Any]) -> str:
    urn = str(entity.get("urn") or "")
    properties = entity.get("properties") or {}
    return str(
        properties.get("name")
        or entity.get("jobId")
        or urn.removeprefix(SCHEDULE_DATAJOB_URN_PREFIX).removesuffix(")")
    )


def _job_custom_properties(entity: dict[str, Any]) -> dict[str, str]:
    properties = (entity.get("properties") or {}).get("customProperties") or []
    custom: dict[str, str] = {}
    for item in properties:
        key = item.get("key")
        value = item.get("value")
        if isinstance(key, str) and isinstance(value, str):
            custom[key] = value
    return custom


def _job_name_prefix(job_name: str) -> str:
    return job_name.split("_", 1)[0] if job_name else ""


def build_downstream_counts(entities: list[dict[str, Any]]) -> dict[str, int]:
    downstream_of: dict[str, set[str]] = {}
    job_names = {_job_display_name(entity) for entity in entities}
    for entity in entities:
        job_name = _job_display_name(entity)
        upstream_jobs = _job_custom_properties(entity).get("upstream_jobs", "")
        for upstream in upstream_jobs.split(","):
            upstream = upstream.strip()
            if upstream:
                downstream_of.setdefault(upstream, set()).add(job_name)

    counts: dict[str, int] = {}
    for job_name in job_names:
        visited: set[str] = set()
        queue: deque[str] = deque(downstream_of.get(job_name, set()))
        while queue:
            downstream = queue.popleft()
            if downstream in visited:
                continue
            visited.add(downstream)
            queue.extend(downstream_of.get(downstream, set()))
        visited.discard(job_name)
        counts[job_name] = len(visited)
    return counts


def classify_datajob(
    entity: dict[str, Any],
    *,
    downstream_job_count: int = 0,
) -> RetryAuditResult:
    """Classify one scheduler DataJob using its stored Job XML."""
    urn = str(entity.get("urn") or "")
    name = _job_display_name(entity)
    xml = _job_xml(entity)
    if not xml:
        status = "missing_job_xml"
        configured = False
    else:
        configured = FIXED_DELAY_CLASS in xml
        status = "configured" if configured else "not_configured"
    return RetryAuditResult(
        job_display_name=name,
        job_name_prefix=_job_name_prefix(name),
        downstream_job_count=downstream_job_count,
        datajob_urn=urn,
        status=status,
        has_fixed_delay=configured,
        job_xml_chars=len(xml),
    )


def _graphql(
    gms_url: str,
    token: str | None,
    query: str,
    variables: dict[str, Any],
    timeout_sec: int,
) -> dict[str, Any]:
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{gms_url.rstrip('/')}/api/graphql",
        data=body,
        headers=headers,
        method="POST",
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout_sec) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("errors"):
                raise RuntimeError(f"DataHub GraphQL errors: {payload['errors']}")
            return payload
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2**attempt)
    raise RuntimeError("DataHub GraphQL request failed")


def iter_schedule_datajobs(
    gms_url: str,
    token: str | None,
    *,
    page_size: int = 100,
    timeout_sec: int = 60,
) -> Iterator[dict[str, Any]]:
    """Yield all BLF scheduler DataJobs using DataHub scroll pagination."""
    scroll_id: str | None = None
    while True:
        variables: dict[str, Any] = {"count": page_size}
        query = _FIRST_PAGE_QUERY
        if scroll_id:
            query = _NEXT_PAGE_QUERY
            variables["scrollId"] = scroll_id
        payload = _graphql(gms_url, token, query, variables, timeout_sec)
        page = (payload.get("data") or {}).get("scrollAcrossEntities") or {}
        results = page.get("searchResults") or []
        if not results:
            return
        for item in results:
            entity = (item or {}).get("entity") or {}
            urn = str(entity.get("urn") or "")
            if urn.startswith(SCHEDULE_DATAJOB_URN_PREFIX):
                yield entity
        scroll_id = page.get("nextScrollId")
        if not scroll_id:
            return


def _write_csv(path: Path, results: list[RetryAuditResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for result in results:
            row = asdict(result)
            row["任务名前缀"] = row.pop("job_name_prefix")
            row["下游任务数量"] = row.pop("downstream_job_count")
            writer.writerow(row)


def run(
    gms_url: str,
    token: str | None,
    output_dir: Path,
    *,
    page_size: int = 100,
) -> tuple[Path, Path, dict[str, int]]:
    entities: list[dict[str, Any]] = []
    for index, entity in enumerate(
        iter_schedule_datajobs(gms_url, token, page_size=page_size), start=1
    ):
        entities.append(entity)
        if index % 500 == 0:
            print(f"[INFO] 已检查 {index} 个调度任务", flush=True)
    downstream_counts = build_downstream_counts(entities)
    results = [
        classify_datajob(
            entity,
            downstream_job_count=downstream_counts.get(_job_display_name(entity), 0),
        )
        for entity in entities
    ]
    results.sort(key=lambda item: item.job_display_name)
    if not results:
        raise RuntimeError("DataHub 中未找到 blf-schedule DataJob")

    all_path = output_dir / "schedule_retry_audit_all.csv"
    missing_path = output_dir / "schedule_retry_not_configured.csv"
    _write_csv(all_path, results)
    not_configured = [
        result for result in results if result.status == "not_configured"
    ]
    _write_csv(missing_path, not_configured)
    summary = {
        "total": len(results),
        "configured": sum(result.status == "configured" for result in results),
        "not_configured": len(not_configured),
        "missing_job_xml": sum(
            result.status == "missing_job_xml" for result in results
        ),
    }
    return all_path, missing_path, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="检查 DataHub 调度任务 Job XML 是否配置 Naginator FixedDelay。"
    )
    parser.add_argument("--gms-url", required=True)
    parser.add_argument("--token")
    parser.add_argument("--output-dir", type=Path, default=Path("."))
    parser.add_argument("--page-size", type=int, default=100)
    args = parser.parse_args(argv)
    try:
        all_path, missing_path, summary = run(
            args.gms_url,
            args.token,
            args.output_dir,
            page_size=args.page_size,
        )
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"[INFO] 全量结果: {all_path}")
    print(f"[INFO] 未配置结果: {missing_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
