"""将单张 Hive 表从 HMS 同步到 DataHub（供手工补血缘时补齐上游 Dataset 节点）。"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import List, Optional, Tuple

from .datahub_writer import make_dataset_urn_from_ref
from .models import TableRef

logger = logging.getLogger(__name__)

_DEFAULT_PLATFORM_INSTANCE = "blf-prod-hive"
_DEFAULT_ENV = "PROD"


def _yaml_double_quoted(s: str) -> str:
    esc = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{esc}"'


def _dump_recipe_yaml(recipe: dict, path: Path) -> None:
    lines: List[str] = []

    def emit(obj: object, ind: int) -> None:
        sp = " " * ind
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, dict):
                    lines.append(f"{sp}{k}:")
                    emit(v, ind + 2)
                elif isinstance(v, list):
                    if len(v) == 0:
                        lines.append(f"{sp}{k}: []")
                    else:
                        lines.append(f"{sp}{k}:")
                        emit(v, ind + 2)
                elif isinstance(v, bool):
                    lines.append(f"{sp}{k}: {'true' if v else 'false'}")
                elif isinstance(v, (int, float)):
                    lines.append(f"{sp}{k}: {v}")
                else:
                    lines.append(f"{sp}{k}: {_yaml_double_quoted(str(v))}")
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, (dict, list)):
                    lines.append(f"{sp}-")
                    emit(item, ind + 2)
                else:
                    lines.append(f"{sp}- {_yaml_double_quoted(str(item))}")
        else:
            raise TypeError(type(obj))

    emit(recipe, 0)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_single_table_recipe(
    db: str,
    table: str,
    *,
    platform_instance: str = _DEFAULT_PLATFORM_INSTANCE,
    env: str = _DEFAULT_ENV,
    hms_host_port: str = "${HMS_THRIFT_HOST}:${HMS_THRIFT_PORT}",
    gms_url_placeholder: str = "${DATAHUB_GMS_URL}",
    include_view_lineage: bool = False,
) -> dict:
    """生成只包含一张表的 hive-metastore ingestion recipe 结构。"""
    db_esc = re.escape(db.strip())
    tbl_esc = re.escape(table.strip())
    table_allow = [f"^({db_esc}\\.{tbl_esc})$"]
    db_allow = [f"^{db_esc}$"]
    return {
        "source": {
            "type": "hive-metastore",
            "config": {
                "connection_type": "thrift",
                "host_port": hms_host_port,
                "use_kerberos": False,
                "platform_instance": platform_instance,
                "env": env,
                "emit_storage_lineage": False,
                "hive_storage_lineage_direction": "upstream",
                "include_column_lineage": False,
                "include_view_lineage": include_view_lineage,
                "database_pattern": {"allow": db_allow, "deny": []},
                "table_pattern": {
                    "allow": table_allow,
                    "deny": [
                        r"^[^.]+\.tmp_.*$",
                        r"^[^.]+\.temp_.*$",
                        r"^[^.]+\.bak_tmp.*$",
                        r"^[^.]+\.not_verified_.*$",
                    ],
                },
            },
        },
        "sink": {
            "type": "datahub-rest",
            "config": {"server": gms_url_placeholder},
        },
    }


def dataset_entity_exists(
    gms_url: str,
    ref: TableRef,
    platform_instance: str,
    env: str,
    token: Optional[str] = None,
) -> bool:
    """OpenAPI 查询 datasetProperties；404 视为目录中不存在。"""
    urn = make_dataset_urn_from_ref(ref, platform_instance, env)
    enc = urllib.parse.quote(urn, safe="")
    url = f"{gms_url.rstrip('/')}/openapi/v3/entity/dataset/{enc}?aspects=datasetProperties"
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            json.load(resp)
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"检查 Dataset 是否存在失败 HTTP {e.code}: {body}") from e


def _hive_table_exists_in_metastore(ref: TableRef) -> bool:
    """Trino information_schema 确认表在 Hive 中存在（与 batch 血缘校验一致）。"""
    from .hive_table_existence import query_hive_existing_fqtns

    found = query_hive_existing_fqtns({ref.full_name.lower()})
    return ref.full_name.lower() in found


def ingest_single_hive_table(
    ref: TableRef,
    *,
    gms_url: str,
    token: Optional[str] = None,
    platform_instance: str = _DEFAULT_PLATFORM_INSTANCE,
    env: str = _DEFAULT_ENV,
    python_executable: Optional[str] = None,
    dry_run: bool = False,
) -> None:
    """对单表执行 datahub ingest（HMS -> GMS）。写入前不检查是否已存在。"""
    if dry_run:
        logger.info(
            "[dry-run] 将从 Hive 同步表到 DataHub: %s (platform_instance=%s)",
            ref.full_name,
            platform_instance,
        )
        return

    if not _hive_table_exists_in_metastore(ref):
        raise RuntimeError(
            f"上游表在 Hive information_schema 中不存在，无法 ingest: {ref.full_name}"
        )

    py = python_executable or sys.executable
    try:
        subprocess.run(
            [py, "-c", "import datahub"],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise RuntimeError(
            f"需要 DataHub CLI（{py} -m datahub）。请安装 acryl-datahub[hive-metastore]"
        ) from e

    recipe_path: Path
    with tempfile.TemporaryDirectory(prefix="dh_hive_ingest_") as tmp:
        recipe_path = Path(tmp) / f"ingest_{ref.db}_{ref.table}.yml"
        recipe = build_single_table_recipe(
            ref.db,
            ref.table,
            platform_instance=platform_instance,
            env=env,
        )
        _dump_recipe_yaml(recipe, recipe_path)

        env_run = os.environ.copy()
        env_run["DATAHUB_GMS_URL"] = gms_url.rstrip("/")
        if token:
            env_run["DATAHUB_GMS_TOKEN"] = token
        env_run.setdefault("HMS_THRIFT_HOST", "hiveserver5.dp.data.bj1.wormpex.com")
        env_run.setdefault("HMS_THRIFT_PORT", "9083")
        env_run["PYTHONUNBUFFERED"] = "1"

        cmd = [
            py,
            "-m",
            "datahub",
            "ingest",
            "-c",
            str(recipe_path),
            "--no-progress",
            "--no-spinner",
        ]
        logger.info(
            "开始 Hive ingest: table=%s GMS=%s recipe=%s",
            ref.full_name,
            gms_url,
            recipe_path,
        )
        proc = subprocess.run(
            cmd,
            env=env_run,
            capture_output=True,
            text=True,
            timeout=int(os.environ.get("BLF_HIVE_INGEST_TIMEOUT_SEC", "600")),
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "")[-2000:]
            raise RuntimeError(
                f"Hive ingest 失败 exit={proc.returncode} table={ref.full_name}: {tail}"
            )
        logger.info("Hive ingest 完成: %s", ref.full_name)

    if not dataset_entity_exists(gms_url, ref, platform_instance, env, token):
        raise RuntimeError(
            f"ingest 结束后仍未在 DataHub 找到 Dataset: {ref.full_name}，请检查 GMS 地址与 platform_instance"
        )


def ensure_upstream_dataset_in_datahub(
    upstream: TableRef,
    *,
    gms_url: str,
    token: Optional[str] = None,
    platform_instance: str = _DEFAULT_PLATFORM_INSTANCE,
    env: str = _DEFAULT_ENV,
    python_executable: Optional[str] = None,
    dry_run: bool = False,
    skip_ingest: bool = False,
) -> Tuple[bool, str]:
    """若上游表不在 DataHub 目录中，则从 Hive ingest；返回 (是否执行了 ingest, 说明)。"""
    if skip_ingest:
        return False, "已跳过上游 Hive ingest（BLF_LINEAGE_SKIP_UPSTREAM_INGEST）"

    if dataset_entity_exists(gms_url, upstream, platform_instance, env, token):
        msg = f"上游表已在 DataHub 目录中: {upstream.full_name}"
        logger.info(msg)
        return False, msg

    logger.info("上游表不在 DataHub，将从 Hive 同步: %s", upstream.full_name)
    ingest_single_hive_table(
        upstream,
        gms_url=gms_url,
        token=token,
        platform_instance=platform_instance,
        env=env,
        python_executable=python_executable,
        dry_run=dry_run,
    )
    return True, f"已从 Hive ingest 上游表: {upstream.full_name}"
