#!/usr/bin/env python3
"""
列出 DataHub 中没有下游依赖 或 never_execute_job 间接下游 的调度任务，输出为 Excel。

用法:
  python3 list_no_downstream_jobs.py [--out no_downstream_jobs_TIME.xlsx]
"""
import concurrent.futures
import json
import os
import sys
import time
import urllib.request
import urllib.error
from collections import deque

_ENV_FILE = "/data/datahub/scripts/lineage.env"
if os.path.exists(_ENV_FILE):
    with open(_ENV_FILE) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.lstrip("export").strip().partition("=")
                os.environ.setdefault(_k.strip(), _v.strip().strip('"'))

GMS_URL = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
GMS_TOKEN = os.environ.get("DATAHUB_GMS_TOKEN", "")
import datetime as _dt
_NOW = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
OUTPUT = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else f"no_downstream_jobs_{_NOW}.xlsx"

NEVER_EXECUTE_JOB = "never_execute_job"
REMARK_URN = "urn:li:structuredProperty:blf.data.warehouse.other_remark"
SCHEDULE_URL_PROP_URN = "urn:li:structuredProperty:blf.data.schedule.schedule_url"
HIVE_PLATFORM_INSTANCE = os.environ.get("BLF_DATAHUB_PLATFORM_INSTANCE", "blf-prod-hive")
JENKINS_BATCH_CLUSTER_MINUTES = int(os.environ.get("JENKINS_BATCH_CLUSTER_MINUTES", "10"))
JENKINS_DELETED_THRESHOLD_HOURS = int(os.environ.get("JENKINS_DELETED_THRESHOLD_HOURS", "2"))

_PDW_ODS_PREFIXES = ("pdw", "ods")
_JENKINS_EXISTS = "存在"
_JENKINS_DELETED = "疑似已删除"
_JENKINS_UNKNOWN = "未知"

# job 名前缀 → Hive 库名（前缀越长越优先，fallback 为 default）
_JOB_PREFIX_TO_DB = {
    "pdw_gis": "data_gis_h3",
    "ods_gis_h3": "data_gis_h3",
    "ods_gis": "data_gis_h3",
}


def _hive_dataset_urn(job_name: str) -> str:
    db = "default"
    for prefix, database in _JOB_PREFIX_TO_DB.items():
        if job_name.startswith(prefix):
            db = database
            break
    return (
        f"urn:li:dataset:(urn:li:dataPlatform:hive,"
        f"{HIVE_PLATFORM_INSTANCE}.{db}.{job_name},PROD)"
    )


def graphql(query, variables=None):
    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    headers = {"Content-Type": "application/json"}
    if GMS_TOKEN:
        headers["Authorization"] = f"Bearer {GMS_TOKEN}"
    req = urllib.request.Request(f"{GMS_URL}/api/graphql", data=payload, headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.URLError:
            if attempt == 2:
                raise
            time.sleep(2)


_ENTITY_FIELDS = """
urn
... on DataJob {
  jobId
  properties { name customProperties { key value } }
  structuredProperties {
    properties {
      structuredProperty { urn }
      values { ... on StringValue { stringValue } }
    }
  }
  editableProperties { description }
  ownership {
    owners {
      owner { ... on CorpUser { username } }
    }
  }
}
"""

_Q_FIRST = """
{
  scrollAcrossEntities(input: {
    types: [DATA_JOB]
    query: "*"
    count: 500
  }) {
    nextScrollId
    searchResults { entity { """ + _ENTITY_FIELDS + """ } }
  }
}
"""

_Q_NEXT = """
query($scrollId: String!) {
  scrollAcrossEntities(input: {
    types: [DATA_JOB]
    query: "*"
    count: 500
    scrollId: $scrollId
  }) {
    nextScrollId
    searchResults { entity { """ + _ENTITY_FIELDS + """ } }
  }
}
"""

# 通过 Schedule URL 结构化属性找到 Hive dataset 的真实 URN
_Q_FIND_URN_BY_SCHEDULE_URL = """
query($url: String!) {
  searchAcrossEntities(input: {
    types: [DATASET]
    query: "*"
    count: 1
    orFilters: [{ and: [{ field: "structuredProperties.blf.data.schedule.schedule_url" value: $url }] }]
  }) {
    searchResults { entity { urn } }
  }
}
"""

# 一次请求：hive 表类型 + 总下游数 + view 下游数
_Q_HIVE_INFO = """
query($urn: String!) {
  dataset(urn: $urn) {
    subTypes { typeNames }
  }
  allDs: searchAcrossLineage(input: {
    urn: $urn
    direction: DOWNSTREAM
    count: 1
  }) { total }
  viewDs: searchAcrossLineage(input: {
    urn: $urn
    direction: DOWNSTREAM
    count: 1
    orFilters: [{ and: [{ field: "typeNames" value: "view" }] }]
  }) { total }
}
"""


def fetch_all_jobs():
    jobs = []
    scroll_id = None
    print("正在拉取所有调度任务（scroll 模式）...", flush=True)
    while True:
        data = graphql(_Q_FIRST) if scroll_id is None else graphql(_Q_NEXT, {"scrollId": scroll_id})
        results = data.get("data", {}).get("scrollAcrossEntities", {})
        batch = results.get("searchResults", [])
        if not batch:
            break
        jobs.extend(batch)
        scroll_id = results.get("nextScrollId")
        print(f"  已拉取 {len(jobs)} 个...", end="\r", flush=True)
        if not scroll_id:
            break
    print(f"\n共 {len(jobs)} 个任务", flush=True)
    return jobs


def fetch_hive_info(job_name: str, schedule_url: str = "") -> tuple:
    """
    返回 (hive_type, downstream_total, downstream_views)。
    优先通过 Schedule URL 结构化属性定位正确的 dataset URN；
    找不到时 fallback 到前缀映射规则（ods_gis/pdw_gis → data_gis_h3，其余 → default）。
    hive_type: "table"/"view" 等 subType，"DataSet"（无 subType），"-"（表不在 DataHub）
    """
    try:
        dataset_urn = None
        if schedule_url:
            r1 = graphql(_Q_FIND_URN_BY_SCHEDULE_URL, {"url": schedule_url})
            results = (((r1.get("data") or {}).get("searchAcrossEntities") or {})
                       .get("searchResults") or [])
            if results:
                dataset_urn = (results[0].get("entity") or {}).get("urn")

        if not dataset_urn:
            dataset_urn = _hive_dataset_urn(job_name)

        data = graphql(_Q_HIVE_INFO, {"urn": dataset_urn})
        d = data.get("data") or {}

        ds = d.get("dataset")
        if ds is None:
            return ("-", "-", "-")   # 表在 DataHub 中不存在

        types = (ds.get("subTypes") or {}).get("typeNames") or []
        hive_type = types[0] if types else "DataSet"

        total = (d.get("allDs") or {}).get("total", 0)
        view_count = (d.get("viewDs") or {}).get("total", 0)
        non_view = total - view_count
        return (hive_type, str(total), str(non_view))
    except Exception:
        return ("?", "?", "?")


def _parse_remark(entity):
    for prop in (entity.get("structuredProperties") or {}).get("properties") or []:
        if (prop.get("structuredProperty") or {}).get("urn") == REMARK_URN:
            return ", ".join(
                v.get("stringValue", "")
                for v in (prop.get("values") or [])
                if v.get("stringValue")
            )
    return ""


def _parse_documentation(entity):
    return ((entity.get("editableProperties") or {}).get("description") or "").strip()


def _parse_owners(entity):
    owners = (entity.get("ownership") or {}).get("owners") or []
    return ", ".join(
        (o.get("owner") or {}).get("username", "")
        for o in owners
        if (o.get("owner") or {}).get("username")
    )


def bfs_descendants(start_name, downstream_of):
    visited = set()
    queue = deque(downstream_of.get(start_name, []))
    while queue:
        name = queue.popleft()
        if name in visited:
            continue
        visited.add(name)
        queue.extend(downstream_of.get(name, []))
    return visited


def _parse_batch_exec_time(value):
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, _dt.date):
        return _dt.datetime.combine(value, _dt.time.min)
    if isinstance(value, str):
        value = value.strip()
        if not value or value.startswith("0000-00-00"):
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return _dt.datetime.strptime(value, fmt)
            except ValueError:
                pass
    return None


def _format_batch_exec_time(value):
    parsed = _parse_batch_exec_time(value)
    return parsed.strftime("%Y-%m-%d %H:%M:%S") if parsed else ""


def _mysql_connection():
    try:
        from scheduler_datajob_sync.mysql_client import default_connection_factory
        return default_connection_factory()
    except ImportError:
        pass

    try:
        import pymysql
    except ImportError as exc:
        raise RuntimeError("缺少依赖：请先安装 pymysql") from exc
    return pymysql.connect(
        host=os.getenv("SCHEDULER_MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("SCHEDULER_MYSQL_PORT", "3306")),
        user=os.getenv("SCHEDULER_MYSQL_USER", ""),
        password=os.getenv("SCHEDULER_MYSQL_PASSWORD", ""),
        database=os.getenv("SCHEDULER_MYSQL_DATABASE", "data_platform"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def fetch_jenkins_batch_exec_times():
    conn = _mysql_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT job_display_name, batch_exec_time "
            "FROM dmp_schedule_job_basic_info "
            "WHERE job_display_name IS NOT NULL"
        )
        batch_times = {}
        latest_batch_exec_time = None
        for row in cur.fetchall():
            job_name = row.get("job_display_name")
            batch_time = _parse_batch_exec_time(row.get("batch_exec_time"))
            if not job_name:
                continue
            existing = batch_times.get(job_name)
            if existing is None or (batch_time is not None and batch_time > existing):
                batch_times[job_name] = batch_time
            if batch_time is not None and (
                latest_batch_exec_time is None or batch_time > latest_batch_exec_time
            ):
                latest_batch_exec_time = batch_time
        return batch_times, latest_batch_exec_time
    finally:
        cur.close()
        conn.close()


def classify_jenkins_job_status(
    job_name,
    batch_exec_times,
    latest_batch_exec_time,
):
    batch_time = _parse_batch_exec_time(batch_exec_times.get(job_name))
    if batch_time is None or latest_batch_exec_time is None:
        return (_JENKINS_DELETED, "")

    threshold = _dt.timedelta(hours=JENKINS_DELETED_THRESHOLD_HOURS)
    status = (
        _JENKINS_DELETED
        if latest_batch_exec_time - batch_time > threshold
        else _JENKINS_EXISTS
    )
    return (status, _format_batch_exec_time(batch_time))


def main():
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        print("请先安装 openpyxl: pip install openpyxl")
        sys.exit(1)

    all_jobs = fetch_all_jobs()

    job_data = {}
    for item in all_jobs:
        entity = item.get("entity", {})
        urn = entity.get("urn", "")
        if not urn:
            continue
        job_id = entity.get("jobId", "")
        custom = {
            p["key"]: p["value"]
            for p in ((entity.get("properties") or {}).get("customProperties") or [])
        }
        job_name = custom.get("job_display_name", job_id)
        job_data[job_name] = {
            "urn": urn,
            "job_id": job_id,
            "custom": custom,
            "remark": _parse_remark(entity),
            "documentation": _parse_documentation(entity),
            "owners": _parse_owners(entity),
        }

    downstream_of = {}
    has_downstream = set()
    for job_name, info in job_data.items():
        for up in info["custom"].get("upstream_jobs", "").split(","):
            up = up.strip()
            if not up:
                continue
            has_downstream.add(up)
            downstream_of.setdefault(up, set()).add(job_name)

    print(f"有下游依赖的任务数: {len(has_downstream)}", flush=True)

    never_descendants = bfs_descendants(NEVER_EXECUTE_JOB, downstream_of)
    print(f"never_execute_job 的下游任务数（直接+间接）: {len(never_descendants)}", flush=True)

    jenkins_batch_exec_times = {}
    latest_batch_exec_time = None
    jenkins_lookup_failed = False
    try:
        print("正在查询 Jenkins 任务元数据批次时间（MySQL）...", flush=True)
        jenkins_batch_exec_times, latest_batch_exec_time = fetch_jenkins_batch_exec_times()
        latest_text = _format_batch_exec_time(latest_batch_exec_time) or "-"
        print(
            f"Jenkins 元数据任务数: {len(jenkins_batch_exec_times)}  |  "
            f"最新批次时间: {latest_text}  |  批次聚合窗口: {JENKINS_BATCH_CLUSTER_MINUTES} 分钟",
            flush=True,
        )
    except Exception as exc:
        jenkins_lookup_failed = True
        print(f"[warning] 查询 Jenkins 任务元数据批次失败: {exc}", file=sys.stderr, flush=True)

    rows = []
    for job_name, info in job_data.items():
        has_direct_downstream = job_name in has_downstream
        is_never_child = job_name in never_descendants
        direct_downstream_count = len(downstream_of.get(job_name, set()))

        if has_direct_downstream:
            reason = "有下游依赖"
        elif is_never_child:
            reason = "never_execute_job 的下游"
        else:
            reason = "无下游依赖"

        custom = info["custom"]
        job_id = info["job_id"]
        prefix = job_id.split("_")[0] if job_id else ""
        if jenkins_lookup_failed:
            jenkins_status, jenkins_batch_exec_time = (_JENKINS_UNKNOWN, "")
        else:
            jenkins_status, jenkins_batch_exec_time = classify_jenkins_job_status(
                job_name,
                jenkins_batch_exec_times,
                latest_batch_exec_time,
            )

        rows.append({
            "job_name": job_id,
            "prefix": prefix,
            "build_update_time": custom.get("build_update_time", ""),
            "job_disable": custom.get("job_disable", ""),
            "schedule_url": custom.get("schedule_url", ""),
            "direct_downstream_count": direct_downstream_count,
            "reason": reason,
            "remark": info["remark"],
            "documentation": info["documentation"],
            "owners": info["owners"],
            "hive_type": "",
            "hive_downstream": "",
            "hive_view_downstream": "",
            "jenkins_status": jenkins_status,
            "jenkins_batch_exec_time": jenkins_batch_exec_time,
        })

    rows.sort(key=lambda x: (x["reason"], x["prefix"], x["job_name"]))

    no_ds_count = sum(1 for r in rows if r["reason"] == "无下游依赖")
    never_count = sum(1 for r in rows if r["reason"] == "never_execute_job 的下游")
    has_ds_count = sum(1 for r in rows if r["reason"] == "有下游依赖")
    print(f"无下游依赖: {no_ds_count}  |  never_execute_job 下游: {never_count}  |  有下游依赖: {has_ds_count}  |  合计: {len(rows)}", flush=True)

    # ── 并发查询 pdw/ods job 的 hive 表信息（类型 + 下游总数 + view 数）────────
    pdw_ods_indices = [
        i for i, r in enumerate(rows)
        if r["prefix"] in _PDW_ODS_PREFIXES
    ]
    if pdw_ods_indices:
        print(f"\n正在查询 {len(pdw_ods_indices)} 个 pdw/ods job 的 Hive 表信息"
              f"（并发 20 线程）...", flush=True)
        done_count = [0]

        def _query_and_set(idx):
            hive_type, total, views = fetch_hive_info(
                rows[idx]["job_name"],
                schedule_url=rows[idx].get("schedule_url", ""),
            )
            rows[idx]["hive_type"] = hive_type
            rows[idx]["hive_downstream"] = total
            rows[idx]["hive_view_downstream"] = views
            done_count[0] += 1
            n = len(pdw_ods_indices)
            if done_count[0] % 200 == 0 or done_count[0] == n:
                print(f"  已查询 {done_count[0]}/{n}...", end="\r", flush=True)

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            list(executor.map(_query_and_set, pdw_ods_indices))
        print(flush=True)

    # ── Excel ─────────────────────────────────────────────────────────────────
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "结果"

    HEADER_FILL  = PatternFill("solid", fgColor="366092")
    HEADER_FONT  = Font(bold=True, color="FFFFFF")
    ROW_FILL     = PatternFill("solid", fgColor="DCE6F1")
    NEVER_FILL   = PatternFill("solid", fgColor="FCE4D6")
    ZERO_DS_FILL = PatternFill("solid", fgColor="FFC000")   # 下游总数=0 橙黄高亮

    HEADERS = [
        ("job名",              55),
        ("job名前缀",           15),
        ("build_update_time",   22),
        ("job_disable",         12),
        ("直接下游数",           12),
        ("加入原因",             20),
        ("remark",              25),
        ("Documentation",       50),
        ("Owners",              20),
        ("Hive表类型",          12),
        ("Hive下游总数",         12),
        ("Hive下游非VIEW数",      14),
        ("Jenkins状态",          14),
        ("Jenkins元数据批次时间",  22),
    ]
    FIELDS = [
        "job_name", "prefix", "build_update_time", "job_disable",
        "direct_downstream_count", "reason",
        "remark", "documentation", "owners",
        "hive_type", "hive_downstream", "hive_view_downstream",
        "jenkins_status", "jenkins_batch_exec_time",
    ]
    ncols = len(HEADERS)

    for col, (h, w) in enumerate(HEADERS, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center")
        ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = w

    COL_DS    = FIELDS.index("hive_downstream") + 1
    COL_VIEWS = FIELDS.index("hive_view_downstream") + 1

    for r, job in enumerate(rows, 2):
        is_never = job["reason"] == "never_execute_job 的下游"
        row_fill = NEVER_FILL if is_never else (ROW_FILL if r % 2 == 0 else None)

        for c, key in enumerate(FIELDS, 1):
            ws.cell(r, c, job[key])

        if row_fill:
            for c in range(1, ncols + 1):
                ws.cell(r, c).fill = row_fill

        # 下游总数=0 单独橙黄高亮，便于识别可删候选
        if job["hive_downstream"] == "0":
            ws.cell(r, COL_DS).fill = ZERO_DS_FILL

    ws.freeze_panes = "A2"
    last_col = ws.cell(row=1, column=ncols).column_letter
    ws.auto_filter.ref = f"A1:{last_col}{len(rows) + 1}"

    # ── 按前缀统计 ────────────────────────────────────────────────────────────
    ws2 = wb.create_sheet("按前缀统计")
    for c, h in enumerate(["job名前缀", "无下游依赖", "never下游", "合计"], 1):
        ws2.cell(1, c, h).font = Font(bold=True)
    for col in "ABCD":
        ws2.column_dimensions[col].width = 14

    for i, prefix in enumerate(sorted({r["prefix"] for r in rows}), 2):
        prefix_rows = [r for r in rows if r["prefix"] == prefix]
        nd = sum(1 for r in prefix_rows if r["reason"] == "无下游依赖")
        nv = sum(1 for r in prefix_rows if r["reason"] == "never_execute_job 的下游")
        ws2.cell(i, 1, prefix)
        ws2.cell(i, 2, nd)
        ws2.cell(i, 3, nv)
        ws2.cell(i, 4, len(prefix_rows))

    wb.save(OUTPUT)
    print(f"✓ 已生成: {OUTPUT}  ({len(rows)} 条)")


if __name__ == "__main__":
    main()
