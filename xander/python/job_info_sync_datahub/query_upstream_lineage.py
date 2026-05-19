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

退出码::

  0  正常完成，所有上游表结构化属性均已填写
  1  查询 DataHub 出错
  2  参数错误
  3  有上游表缺少 Etl Script 或 Execute Shell
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
from typing import Any, Dict, List, Optional, Set, Tuple

from .field_lineage_datahub_reader import (
    extract_property_texts,
    fetch_structured_properties,
    make_hive_dataset_urn,
)

_DEFAULT_PLATFORM_INSTANCE = "blf-prod-hive"
_DEFAULT_ENV = "PROD"

# 属性显示名（和 DataHub UI 一致）
_LABEL_ETL_SCRIPT = "Etl Script"
_LABEL_EXECUTE_SHELL = "Execute Shell"


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
    try:
        from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
        from datahub.metadata.schema_classes import UpstreamLineageClass
    except ImportError as exc:
        raise RuntimeError("需要安装 acryl-datahub: pip install acryl-datahub") from exc

    graph = DataHubGraph(DatahubClientConfig(server=gms_url.rstrip("/"), token=token))

    all_upstream_urns: Set[str] = set()
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
            continue

        for upstream in lineage.upstreams:
            upstream_urn = getattr(upstream, "dataset", None)
            if not isinstance(upstream_urn, str):
                continue
            # 只收录同平台实例的 Hive 表
            if platform_instance not in upstream_urn:
                continue
            if upstream_urn != start_urn:
                all_upstream_urns.add(upstream_urn)
            if upstream_urn not in visited:
                queue.append(upstream_urn)

    return all_upstream_urns


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
    # 把所有目标表的 URN 也收集进来，BFS 时排除
    target_urns: Set[str] = set()
    for t in table_names:
        target_urns.add(make_hive_dataset_urn(t, platform_instance, env))

    for t in table_names:
        start_urn = make_hive_dataset_urn(t, platform_instance, env)
        print(f"[INFO] 查询 {t} 的上游...")
        try:
            urns = fetch_all_upstream_urns(gms_url, token, start_urn, platform_instance)
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

    print()

    if not all_upstream_urns:
        print("[INFO] 未找到任何上游表（DataHub 中无表级血缘记录）")
        return 0

    # ── 2. 去重、排序、输出 ────────────────────────────────────────────────
    upstream_tables: List[str] = sorted(
        {urn_to_table_name(u, platform_instance) for u in all_upstream_urns}
    )

    print(f"[INFO] 合并去重后共 {len(upstream_tables)} 个上游表:")
    print()
    for t in upstream_tables:
        print(t)

    if skip_check_props:
        return 0

    # ── 3. 校验结构化属性 ──────────────────────────────────────────────────
    print()
    print("[INFO] 开始检查结构化属性...")

    missing_props: Dict[str, List[str]] = {}
    skipped_view_tables: List[str] = []
    for t in upstream_tables:
        urn = make_hive_dataset_urn(t, platform_instance, env)
        has_etl, has_shell = check_structured_properties(gms_url, token, urn)
        missing: List[str] = []
        if not has_etl:
            missing.append(_LABEL_ETL_SCRIPT)
        if not has_shell:
            missing.append(_LABEL_EXECUTE_SHELL)
        if missing:
            if is_view_dataset(gms_url, token, urn):
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
        print(f"[WARN] 以下 {len(missing_props)} 个上游表缺少结构化属性，请补充后再做字段血缘分析:")
        print()
        for t in sorted(missing_props):
            labels = "、".join(missing_props[t])
            print(f"  {t}  ← 缺少: {labels}")
        print()
        return 3

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
    )


if __name__ == "__main__":
    sys.exit(main())
