"""从 DataHub 表级血缘递归查询所有上游表，并校验结构化属性完整性。

支持一次传入多个目标表，各自递归查询后合并去重。

用法（模块）::

  cd xander/python

  # 单表
  PYTHONPATH=. python3 -m job_info_sync_datahub.query_upstream_lineage \\
      --table-name default.dim_store_info

  # 多表（重复 --table-name 或用逗号/换行分隔）
  PYTHONPATH=. python3 -m job_info_sync_datahub.query_upstream_lineage \\
      --table-name default.dim_store_info \\
      --table-name default.dim_city_info

Jenkins 部署::

  sh /data/datahub/scripts/run_query_upstream_lineage.sh

环境变量（可放在 lineage.env）::

  TABLE_NAMES             目标表名列表（多行，一行一个；库.表 或仅表名默认库 default）
  DATAHUB_GMS_URL         GMS 地址，默认 http://localhost:8080
  DATAHUB_GMS_TOKEN       GMS token（无鉴权时可不填）
  BLF_DATAHUB_PLATFORM_INSTANCE  默认 blf-prod-hive
  DATAHUB_ENV             默认 PROD
  UPSTREAM_LINEAGE_XLSX   上游明细 Excel 输出路径

退出码::

  0  正常完成；如有上游表缺少结构化属性，会按缺少项分组打印
  1  查询 DataHub 出错
  2  参数错误
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .field_lineage_datahub_reader import (
    extract_property_texts,
    fetch_structured_properties,
    make_hive_dataset_urn,
)
from .structured_properties import (
    URN_DATA_AVAILABILITY_FLAG,
    URN_ETL_SCRIPT,
    URN_EXECUTE_SHELL,
    URN_SCHEDULE_URL,
)

_DEFAULT_PLATFORM_INSTANCE = "blf-prod-hive"
_DEFAULT_ENV = "PROD"
_AUTO_DOC_START = "<!-- DATAHUB_AUTO_PROCESSING_DOC_START -->"
_AUTO_DOC_END = "<!-- DATAHUB_AUTO_PROCESSING_DOC_END -->"
_GENERATED_DOC_REQUIRED_SECTIONS = (
    "## 表加工逻辑说明",
    "### 1. 表用途概览",
    "### 2. 表结构 DDL",
    "### 4. 数据来源",
    "### 5. 使用到的上游表字段",
)

# 属性显示名（和 DataHub UI 一致）
_LABEL_ETL_SCRIPT = "Etl Script"
_LABEL_EXECUTE_SHELL = "Execute Shell"

_CHECKED_STRUCTURED_PROPERTY_LABELS = (_LABEL_ETL_SCRIPT, _LABEL_EXECUTE_SHELL)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def normalize_table_name(name: str) -> str:
    """保证表名为 `库.表` 格式，无库名时默认 default。"""
    name = name.strip().lower()
    if not name:
        raise ValueError("表名为空")
    if "." not in name:
        return f"default.{name}"
    parts = name.split(".")
    if not parts[0] or not parts[-1]:
        raise ValueError(f"表名格式无效: {name!r}，应为 库.表")
    return name


def urn_to_table_name(urn: str, platform_instance: str = _DEFAULT_PLATFORM_INSTANCE) -> str:
    """从 Hive dataset URN 中提取 `库.表`。

    URN 格式: ``urn:li:dataset:(urn:li:dataPlatform:hive,blf-prod-hive.default.t,PROD)``
    """
    try:
        prefix = "urn:li:dataset:("
        if not urn.startswith(prefix):
            return urn
        inner = urn[len(prefix) :]
        if inner.endswith(")"):
            inner = inner[:-1]
        parts = inner.split(",", 2)
        if len(parts) < 2:
            return urn
        name_part = parts[1].strip()
        pi_prefix = platform_instance + "."
        if name_part.startswith(pi_prefix):
            return name_part[len(pi_prefix) :]
        return name_part
    except Exception:
        return urn


# ---------------------------------------------------------------------------
# 血缘遍历
# ---------------------------------------------------------------------------


def fetch_all_upstream_urns(
    gms_url: str,
    token: Optional[str],
    start_urn: str,
    platform_instance: str = _DEFAULT_PLATFORM_INSTANCE,
) -> Set[str]:
    """BFS 递归查询所有上游表 URN（不含起始表本身）。"""
    return fetch_all_upstream_urns_with_counts(
        gms_url=gms_url,
        token=token,
        start_urn=start_urn,
        platform_instance=platform_instance,
    )[0]


def fetch_all_upstream_urns_with_counts(
    gms_url: str,
    token: Optional[str],
    start_urn: str,
    platform_instance: str = _DEFAULT_PLATFORM_INSTANCE,
) -> Tuple[Set[str], Dict[str, int]]:
    """BFS 递归查询所有上游表 URN，并返回每个已访问 dataset 的直接上游数。"""
    try:
        from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
        from datahub.metadata.schema_classes import UpstreamLineageClass
    except ImportError as exc:
        raise RuntimeError("需要安装 acryl-datahub: pip install acryl-datahub") from exc

    graph = DataHubGraph(DatahubClientConfig(server=gms_url.rstrip("/"), token=token))

    all_upstream_urns: Set[str] = set()
    upstream_counts: Dict[str, int] = {}
    visited: Set[str] = set()
    queue: deque[str] = deque([start_urn])

    while queue:
        urn = queue.popleft()
        if urn in visited:
            continue
        visited.add(urn)

        try:
            lineage = graph.get_aspect(entity_urn=urn, aspect_type=UpstreamLineageClass)
        except Exception as exc:
            print(f"[WARN] 查询 {urn_to_table_name(urn, platform_instance)} 血缘失败: {exc}", file=sys.stderr)
            continue

        if not lineage or not lineage.upstreams:
            upstream_counts[urn] = 0
            continue

        direct_upstreams: Set[str] = set()
        for upstream in lineage.upstreams:
            upstream_urn = getattr(upstream, "dataset", None)
            if not isinstance(upstream_urn, str):
                continue
            # 只收录同平台实例的 Hive 表
            if platform_instance not in upstream_urn:
                continue
            direct_upstreams.add(upstream_urn)
            if upstream_urn != start_urn:
                all_upstream_urns.add(upstream_urn)
            if upstream_urn not in visited:
                queue.append(upstream_urn)
        upstream_counts[urn] = len(direct_upstreams)

    return all_upstream_urns, upstream_counts


# ---------------------------------------------------------------------------
# 结构化属性检查
# ---------------------------------------------------------------------------


def check_structured_properties(
    gms_url: str,
    token: Optional[str],
    dataset_urn: str,
) -> Tuple[bool, bool]:
    """返回 (has_etl_script, has_execute_shell)。

    使用 field_lineage_datahub_reader.extract_property_texts 以兼容多种 payload
    形态（如 values=[{"string": ...}] 或 values=["..."]，以及包装层级差异）。
    """
    try:
        payload = fetch_structured_properties(gms_url, dataset_urn, token=token)
    except Exception:
        return False, False

    etl_script, execute_shell = extract_property_texts(payload)
    return bool(etl_script.strip()), bool(execute_shell.strip())


def _iter_property_assignments(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    current: Any = payload
    for key in ("structuredProperties", "value"):
        if isinstance(current, dict) and key in current:
            current = current[key]
    if not isinstance(current, dict) or not isinstance(current.get("properties"), list):
        return []
    return [item for item in current["properties"] if isinstance(item, dict)]


def _first_string_value(assignment: Dict[str, Any]) -> str:
    values = assignment.get("values")
    if not isinstance(values, list):
        return ""
    for value in values:
        if isinstance(value, dict) and isinstance(value.get("string"), str):
            return value["string"]
        if isinstance(value, str):
            return value
    return ""


def _all_string_values(assignment: Dict[str, Any]) -> List[str]:
    values = assignment.get("values")
    if not isinstance(values, list):
        return []
    results: List[str] = []
    for value in values:
        text = ""
        if isinstance(value, dict) and isinstance(value.get("string"), str):
            text = value["string"]
        elif isinstance(value, str):
            text = value
        text = text.strip()
        if text:
            results.append(text)
    return results


def extract_structured_property_value(payload: Dict[str, Any], property_urn: str) -> str:
    """Read one structured property value from a structuredProperties payload."""
    for assignment in _iter_property_assignments(payload):
        if assignment.get("propertyUrn") == property_urn:
            return _first_string_value(assignment).strip()
    return ""


def extract_data_availability_flag(payload: Dict[str, Any]) -> str:
    """Read all blf.data.warehouse.data_availability_flag values."""
    for assignment in _iter_property_assignments(payload):
        if assignment.get("propertyUrn") == URN_DATA_AVAILABILITY_FLAG:
            return ", ".join(_all_string_values(assignment))
    return ""


def read_upstream_structured_status(
    gms_url: str,
    token: Optional[str],
    dataset_urn: str,
) -> Tuple[bool, bool, bool, str]:
    """Return (has_etl_script, has_schedule_url, has_execute_shell, data_availability_flag)."""
    try:
        payload = fetch_structured_properties(gms_url, dataset_urn, token=token)
    except Exception:
        return False, False, False, ""

    etl_script, execute_shell = extract_property_texts(payload)
    schedule_url = extract_structured_property_value(payload, URN_SCHEDULE_URL)
    return (
        bool(etl_script.strip()),
        bool(schedule_url.strip()),
        bool(execute_shell.strip()),
        extract_data_availability_flag(payload),
    )


def _dataset_aspect_url(gms_url: str, dataset_urn: str, aspect_name: str) -> str:
    encoded = urllib.parse.quote(dataset_urn, safe="")
    return f"{gms_url.rstrip('/')}/openapi/v3/entity/dataset/{encoded}/{aspect_name}"


def _fetch_dataset_aspect(
    gms_url: str,
    dataset_urn: str,
    aspect_name: str,
    token: Optional[str] = None,
    timeout_sec: int = 60,
) -> Dict[str, Any]:
    req = urllib.request.Request(
        _dataset_aspect_url(gms_url, dataset_urn, aspect_name),
        method="GET",
        headers={"Accept": "application/json"},
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        return json.loads(resp.read().decode("utf-8"))


def is_view_dataset(gms_url: str, token: Optional[str], dataset_urn: str) -> bool:
    """Return true when DataHub has a viewProperties aspect for this dataset."""
    try:
        payload = _fetch_dataset_aspect(gms_url, dataset_urn, "viewProperties", token=token)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        print(
            f"[WARN] 查询 {urn_to_table_name(dataset_urn)} viewProperties 失败 HTTP {exc.code}",
            file=sys.stderr,
        )
        return False
    except Exception as exc:
        print(
            f"[WARN] 查询 {urn_to_table_name(dataset_urn)} viewProperties 失败: {exc}",
            file=sys.stderr,
        )
        return False

    current: Any = payload
    for key in ("viewProperties", "value"):
        if isinstance(current, dict) and key in current:
            current = current[key]
    return isinstance(current, dict) and bool(current.get("viewLogic"))


def _has_schema_metadata(gms_url: str, token: Optional[str], dataset_urn: str) -> bool:
    try:
        payload = _fetch_dataset_aspect(gms_url, dataset_urn, "schemaMetadata", token=token)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        print(
            f"[WARN] 查询 {urn_to_table_name(dataset_urn)} schemaMetadata 失败 HTTP {exc.code}",
            file=sys.stderr,
        )
        return False
    except Exception as exc:
        print(
            f"[WARN] 查询 {urn_to_table_name(dataset_urn)} schemaMetadata 失败: {exc}",
            file=sys.stderr,
        )
        return False
    return bool(payload)


def _unwrap_aspect(payload: Dict[str, Any], aspect_name: str) -> Dict[str, Any]:
    current: Any = payload
    for key in (aspect_name, "value"):
        if isinstance(current, dict) and key in current:
            current = current[key]
    return current if isinstance(current, dict) else {}


def read_dataset_type(gms_url: str, token: Optional[str], dataset_urn: str) -> str:
    """Return view/table/dataset for report display."""
    if is_view_dataset(gms_url, token, dataset_urn):
        return "view"
    if _has_schema_metadata(gms_url, token, dataset_urn):
        return "table"
    return "dataset"


def is_deprecated_dataset(gms_url: str, token: Optional[str], dataset_urn: str) -> bool:
    try:
        payload = _fetch_dataset_aspect(gms_url, dataset_urn, "deprecation", token=token)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        print(
            f"[WARN] 查询 {urn_to_table_name(dataset_urn)} deprecation 失败 HTTP {exc.code}",
            file=sys.stderr,
        )
        return False
    except Exception as exc:
        print(
            f"[WARN] 查询 {urn_to_table_name(dataset_urn)} deprecation 失败: {exc}",
            file=sys.stderr,
        )
        return False
    return _unwrap_aspect(payload, "deprecation").get("deprecated") is True


def is_llm_generated_description(description: str) -> bool:
    """判断 editableDatasetProperties.description 是否为批量 Documentation 生成产物。"""
    text = (description or "").strip()
    if not text:
        return False
    if _AUTO_DOC_START in text and _AUTO_DOC_END in text:
        return True
    return all(section in text for section in _GENERATED_DOC_REQUIRED_SECTIONS)


def is_llm_generated_documentation(gms_url: str, token: Optional[str], dataset_urn: str) -> bool:
    try:
        payload = _fetch_dataset_aspect(gms_url, dataset_urn, "editableDatasetProperties", token=token)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        print(
            f"[WARN] 查询 {urn_to_table_name(dataset_urn)} editableDatasetProperties 失败 HTTP {exc.code}",
            file=sys.stderr,
        )
        return False
    except Exception as exc:
        print(
            f"[WARN] 查询 {urn_to_table_name(dataset_urn)} editableDatasetProperties 失败: {exc}",
            file=sys.stderr,
        )
        return False

    description = _unwrap_aspect(payload, "editableDatasetProperties").get("description")
    if not isinstance(description, str):
        return False
    return is_llm_generated_description(description)


def split_table_name(full_table_name: str) -> Tuple[str, str]:
    if "." not in full_table_name:
        return "default", full_table_name
    db_name, table_name = full_table_name.split(".", 1)
    return db_name, table_name


def table_name_prefix(table_name: str) -> str:
    return table_name.split("_", 1)[0] if table_name else ""


def _yes_or_dash(value: bool) -> str:
    return "是" if value else "-"


def write_upstream_detail_xlsx(path: str, rows: List[Dict[str, Any]]) -> None:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError as exc:
        raise RuntimeError("请先安装 openpyxl") from exc

    headers = [
        "库名",
        "表名",
        "表名前辍",
        "完整表表",
        "表类型",
        "标记废弃",
        "Documentation生成",
        "etl_script",
        "schedule_url",
        "execute_shell",
        "data_availability_flag",
        "上游表数量",
    ]
    wb = Workbook()
    ws = wb.active
    ws.title = "upstream_lineage"
    ws.append(headers)
    for row in rows:
        ws.append([row.get(header, "") for header in headers])

    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="D9E1F2")
    for col_idx, _ in enumerate(headers, start=1):
        col_letter = get_column_letter(col_idx)
        max_len = 10
        for cell in ws[col_letter]:
            if cell.value is not None:
                max_len = min(max(max_len, len(str(cell.value))), 80)
        ws.column_dimensions[col_letter].width = max_len + 2

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)


# ---------------------------------------------------------------------------
# 主逻辑
# ---------------------------------------------------------------------------


def parse_table_names(raw: str) -> List[str]:
    """将逗号或换行分隔的表名字符串解析为列表，自动去重并过滤空行。"""
    results: List[str] = []
    seen: Set[str] = set()
    for token in raw.replace(",", "\n").splitlines():
        token = token.strip()
        if not token:
            continue
        try:
            normalized = normalize_table_name(token)
        except ValueError as exc:
            print(f"[WARN] 跳过无效表名 {token!r}: {exc}", file=sys.stderr)
            continue
        if normalized not in seen:
            seen.add(normalized)
            results.append(normalized)
    return results


def run(
    table_names: List[str],
    gms_url: str,
    token: Optional[str] = None,
    platform_instance: str = _DEFAULT_PLATFORM_INSTANCE,
    env: str = _DEFAULT_ENV,
    skip_check_props: bool = False,
    output_xlsx: Optional[str] = None,
) -> int:
    """执行查询并输出结果，返回退出码。"""
    if not table_names:
        print("[ERROR] 未提供任何目标表名", file=sys.stderr)
        return 2

    print(f"[INFO] 目标表数量: {len(table_names)}")
    for t in table_names:
        print(f"[INFO]   {t}")
    print(f"[INFO] GMS: {gms_url}")
    print()

    # ── 1. 对每个目标表递归查询上游，合并结果 ─────────────────────────────
    all_upstream_urns: Set[str] = set()
    upstream_dependency_counts: Dict[str, int] = {}
    # 把所有目标表的 URN 也收集进来，BFS 时排除
    target_urns: Set[str] = set()
    for t in table_names:
        target_urns.add(make_hive_dataset_urn(t, platform_instance, env))

    for t in table_names:
        start_urn = make_hive_dataset_urn(t, platform_instance, env)
        print(f"[INFO] 查询 {t} 的上游...")
        try:
            try:
                urns, dependency_counts = fetch_all_upstream_urns_with_counts(
                    gms_url,
                    token,
                    start_urn,
                    platform_instance,
                )
            except TypeError:
                # Compatibility for tests or callers that monkeypatch the old helper.
                urns = fetch_all_upstream_urns(gms_url, token, start_urn, platform_instance)
                dependency_counts = {}
        except RuntimeError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"[ERROR] 查询 {t} 血缘失败: {exc}", file=sys.stderr)
            return 1
        # 过滤掉其他目标表本身（避免互为上游时出现在结果中）
        urns -= target_urns
        print(f"[INFO]   {t} → 找到 {len(urns)} 个上游 URN")
        all_upstream_urns |= urns
        upstream_dependency_counts.update(dependency_counts)

    print()

    if not all_upstream_urns:
        print("[INFO] 未找到任何上游表（DataHub 中无表级血缘记录）")
        if output_xlsx:
            write_upstream_detail_xlsx(output_xlsx, [])
            print(f"[INFO] 上游明细 Excel 已生成: {output_xlsx}")
        return 0

    # ── 2. 去重、排序、输出 ────────────────────────────────────────────────
    upstream_tables: List[str] = sorted(
        {urn_to_table_name(u, platform_instance) for u in all_upstream_urns}
    )

    print(f"[INFO] 合并去重后共 {len(upstream_tables)} 个上游表")
    upstream_status: Dict[str, Tuple[bool, bool, bool, str]] = {}
    upstream_types: Dict[str, str] = {}
    upstream_deprecated: Dict[str, bool] = {}
    upstream_documentation_generated: Dict[str, bool] = {}
    upstream_report_rows: List[Dict[str, Any]] = []
    for table_name in upstream_tables:
        urn = make_hive_dataset_urn(table_name, platform_instance, env)
        upstream_status[table_name] = read_upstream_structured_status(gms_url, token, urn)
        upstream_types[table_name] = read_dataset_type(gms_url, token, urn)
        upstream_deprecated[table_name] = is_deprecated_dataset(gms_url, token, urn)
        upstream_documentation_generated[table_name] = is_llm_generated_documentation(gms_url, token, urn)
        has_etl, has_schedule_url, has_shell, raw_data_availability_flag = upstream_status[table_name]
        data_availability_flag = raw_data_availability_flag or "-"
        db_name, short_table_name = split_table_name(table_name)
        upstream_report_rows.append(
            {
                "库名": db_name,
                "表名": short_table_name,
                "表名前辍": table_name_prefix(short_table_name),
                "完整表表": table_name,
                "表类型": upstream_types[table_name],
                "标记废弃": _yes_or_dash(upstream_deprecated[table_name]),
                "Documentation生成": _yes_or_dash(upstream_documentation_generated[table_name]),
                "etl_script": _yes_or_dash(has_etl),
                "schedule_url": _yes_or_dash(has_schedule_url),
                "execute_shell": _yes_or_dash(has_shell),
                "data_availability_flag": data_availability_flag,
                "上游表数量": upstream_dependency_counts.get(urn, 0),
            }
        )
        print(f"  {table_name}\tdata_availability_flag={data_availability_flag}")

    if output_xlsx:
        write_upstream_detail_xlsx(output_xlsx, upstream_report_rows)
        print(f"[INFO] 上游明细 Excel 已生成: {output_xlsx}")

    if skip_check_props:
        return 0

    # ── 3. 校验结构化属性 ──────────────────────────────────────────────────
    print()
    print("[INFO] 开始检查结构化属性...")

    missing_props: Dict[str, List[str]] = {}
    skipped_view_tables: List[str] = []
    for t in upstream_tables:
        urn = make_hive_dataset_urn(t, platform_instance, env)
        has_etl, _, has_shell, _ = upstream_status.get(t) or read_upstream_structured_status(gms_url, token, urn)
        missing: List[str] = []
        if not has_etl:
            missing.append(_LABEL_ETL_SCRIPT)
        if not has_shell:
            missing.append(_LABEL_EXECUTE_SHELL)
        if missing:
            if upstream_types.get(t) == "view":
                skipped_view_tables.append(t)
                continue
            missing_props[t] = missing

    print()
    if skipped_view_tables:
        print(
            f"[INFO] 以下 {len(skipped_view_tables)} 个上游表是视图，允许 Etl Script / Execute Shell 为空:"
        )
        for t in sorted(skipped_view_tables):
            print(f"  {t}")
        print()

    if missing_props:
        print(f"[WARN] 以下 {len(missing_props)} 个上游表缺少结构化属性:")
        print()

        missing_by_label: Dict[str, List[str]] = {
            label: [] for label in _CHECKED_STRUCTURED_PROPERTY_LABELS
        }
        for table_name, labels in missing_props.items():
            for label in labels:
                missing_by_label.setdefault(label, []).append(table_name)

        for label in _CHECKED_STRUCTURED_PROPERTY_LABELS:
            tables = sorted(missing_by_label.get(label, []))
            if not tables:
                continue
            print(f"缺少: {label} 的如下：")
            for t in tables:
                print(f"  {t}")
            print()

        print()
        return 0

    print(f"[INFO] 所有 {len(upstream_tables)} 个上游表的结构化属性均已填写")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--table-name",
        action="append",
        dest="table_names",
        metavar="DB.TABLE",
        help="目标表名（可重复多次）。也可通过 TABLE_NAMES 环境变量传多行表名",
    )
    p.add_argument(
        "--gms-url",
        default=os.getenv("DATAHUB_GMS_URL", "http://localhost:8080"),
        help="DataHub GMS 地址（默认 http://localhost:8080 或 DATAHUB_GMS_URL）",
    )
    p.add_argument(
        "--token",
        default=os.getenv("DATAHUB_GMS_TOKEN"),
        help="GMS 访问令牌（无鉴权时可不填，或设 DATAHUB_GMS_TOKEN）",
    )
    p.add_argument(
        "--platform-instance",
        default=os.getenv("BLF_DATAHUB_PLATFORM_INSTANCE", _DEFAULT_PLATFORM_INSTANCE),
    )
    p.add_argument(
        "--env",
        default=os.getenv("DATAHUB_ENV", _DEFAULT_ENV),
    )
    p.add_argument(
        "--no-check-props",
        action="store_true",
        help="跳过结构化属性（Etl Script / Execute Shell）检查",
    )
    p.add_argument(
        "--output-xlsx",
        default=os.getenv("UPSTREAM_LINEAGE_XLSX"),
        help="去重后的上游任务明细 Excel 输出路径（默认读取 UPSTREAM_LINEAGE_XLSX）",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    # 收集表名：CLI 参数优先，否则从 TABLE_NAMES 环境变量读取
    raw_names: List[str] = list(args.table_names or [])
    env_names = os.getenv("TABLE_NAMES", "").strip()
    if not raw_names and env_names:
        raw_names = [env_names]

    if not raw_names:
        print(
            "[ERROR] 必须提供 --table-name 或设置 TABLE_NAMES 环境变量",
            file=sys.stderr,
        )
        return 2

    table_names = parse_table_names("\n".join(raw_names))
    if not table_names:
        print("[ERROR] 未解析到有效表名", file=sys.stderr)
        return 2

    return run(
        table_names=table_names,
        gms_url=args.gms_url,
        token=args.token,
        platform_instance=args.platform_instance,
        env=args.env,
        skip_check_props=args.no_check_props,
        output_xlsx=args.output_xlsx,
    )


if __name__ == "__main__":
    sys.exit(main())
