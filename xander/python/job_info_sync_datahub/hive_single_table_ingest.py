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
from typing import List, Optional, Sequence, Tuple

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


def _chunked(seq: Sequence[str], size: int) -> List[Sequence[str]]:
    out: List[Sequence[str]] = []
    for i in range(0, len(seq), size):
        out.append(seq[i : i + size])
    return out


def build_multi_table_recipe(
    pairs: Sequence[Tuple[str, str]],
    *,
    platform_instance: str = _DEFAULT_PLATFORM_INSTANCE,
    env: str = _DEFAULT_ENV,
    hms_host_port: str = "${HMS_THRIFT_HOST}:${HMS_THRIFT_PORT}",
    gms_url_placeholder: str = "${DATAHUB_GMS_URL}",
    include_view_lineage: bool = False,
    chunk_size: int = 600,
    table_pattern_deny: Optional[List[str]] = None,
) -> dict:
    """多表 hive-metastore recipe；``table_pattern_deny`` 默认空（不按 tmp_ 等规则拦截用户名单）。"""
    if not pairs:
        raise ValueError("pairs 为空")
    escaped = [f"{re.escape(db.strip())}\\.{re.escape(tbl.strip())}" for db, tbl in pairs]
    table_allow: List[str] = []
    for group in _chunked(escaped, max(chunk_size, 1)):
        table_allow.append(f"^({'|'.join(group)})$")
    dbs = sorted({db.strip() for db, _ in pairs})
    db_allow = [f"^{re.escape(d)}$" for d in dbs]
    deny = table_pattern_deny if table_pattern_deny is not None else []
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
                "table_pattern": {"allow": table_allow, "deny": deny},
            },
        },
        "sink": {
            "type": "datahub-rest",
            "config": {"server": gms_url_placeholder},
        },
    }


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


def _ingest_timeout_sec() -> int:
    return int(os.environ.get("BLF_HIVE_INGEST_TIMEOUT_SEC", "1800"))


def resolve_hive_ingest_mode(explicit: Optional[str] = None) -> str:
    """``minimal``（MCP 秒级）或 ``full``（``datahub ingest`` + HMS，大库很慢）。

    优先级：CLI ``--ingest-mode`` > ``BLF_HIVE_INGEST_MODE`` > ``BLF_HIVE_INGEST_MINIMAL`` /
    ``BLF_HIVE_INGEST_FULL`` > 默认 ``full``。
    """
    if explicit:
        mode = explicit.strip().lower()
        if mode in ("minimal", "full"):
            return mode
        raise ValueError(f"未知 ingest mode: {explicit!r}，应为 minimal 或 full")

    env_mode = os.environ.get("BLF_HIVE_INGEST_MODE", "").strip().lower()
    if env_mode in ("minimal", "full"):
        return env_mode

    minimal_flag = os.environ.get("BLF_HIVE_INGEST_MINIMAL", "").strip().lower()
    if minimal_flag in ("1", "true", "yes", "on"):
        return "minimal"

    full_flag = os.environ.get("BLF_HIVE_INGEST_FULL", "").strip().lower()
    if full_flag in ("1", "true", "yes", "on"):
        return "full"

    return "full"


def _use_full_hive_ingest_for_upstream() -> bool:
    """默认用轻量 MCP 注册（秒级）；``BLF_LINEAGE_FULL_UPSTREAM_INGEST=1`` 时走完整 HMS ingest。"""
    v = os.environ.get("BLF_LINEAGE_FULL_UPSTREAM_INGEST", "").strip().lower()
    return v in ("1", "true", "yes", "on")


def register_minimal_hive_dataset(
    ref: TableRef,
    *,
    gms_url: str,
    token: Optional[str] = None,
    platform_instance: str = _DEFAULT_PLATFORM_INSTANCE,
    env: str = _DEFAULT_ENV,
    dry_run: bool = False,
) -> None:
    """写入最小 ``datasetProperties``，使血缘图可展示该 Dataset（不拉 HMS 全量 schema）。"""
    urn = make_dataset_urn_from_ref(ref, platform_instance, env)
    if dry_run:
        logger.info("[dry-run] 将注册最小 Dataset: %s", urn)
        return
    try:
        from datahub.emitter.mcp import MetadataChangeProposalWrapper
        from datahub.emitter.rest_emitter import DatahubRestEmitter
        from datahub.metadata.schema_classes import DatasetPropertiesClass
    except ImportError as e:
        raise RuntimeError("需要 acryl-datahub SDK 以注册最小 Dataset") from e

    aspect = DatasetPropertiesClass(
        name=ref.table,
        description=f"Hive table {ref.full_name} (minimal register for lineage UI)",
        customProperties={
            "hive.database": ref.db,
            "hive.table": ref.table,
            "blf.register_mode": "minimal_mcp",
        },
    )
    mcp = MetadataChangeProposalWrapper(entityUrn=urn, aspect=aspect)
    emitter = DatahubRestEmitter(gms_url.rstrip("/"), token=token)
    emitter.emit_mcp(mcp)
    logger.info("已注册最小 Dataset（MCP）: %s", ref.full_name)


def delete_dataset_entity(
    ref: TableRef,
    *,
    gms_url: str,
    token: Optional[str] = None,
    platform_instance: str = _DEFAULT_PLATFORM_INSTANCE,
    env: str = _DEFAULT_ENV,
    dry_run: bool = False,
) -> bool:
    """若 DataHub 中已有该 Dataset，则 hard delete；返回是否执行了删除。"""
    if not dataset_entity_exists(gms_url, ref, platform_instance, env, token):
        logger.info("DataHub 中不存在，跳过删除: %s", ref.full_name)
        return False
    urn = make_dataset_urn_from_ref(ref, platform_instance, env)
    if dry_run:
        logger.info("[dry-run] 将删除 Dataset: %s", urn)
        return True
    try:
        from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
    except ImportError as e:
        raise RuntimeError("删除需要 acryl-datahub（DataHubGraph）") from e
    graph = DataHubGraph(DatahubClientConfig(server=gms_url.rstrip("/"), token=token))
    graph.delete_entity(urn=urn, hard=True)
    logger.info("已删除 DataHub Dataset: %s", ref.full_name)
    return True


def _run_datahub_ingest_recipe(
    recipe_path: Path,
    *,
    gms_url: str,
    token: Optional[str],
    python_executable: Optional[str],
    label: str,
) -> None:
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
    timeout = _ingest_timeout_sec()
    logger.info(
        "开始 Hive ingest: %s GMS=%s recipe=%s timeout_sec=%s",
        label,
        gms_url,
        recipe_path,
        timeout,
    )
    try:
        proc = subprocess.run(
            cmd,
            env=env_run,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "")[-1500:] if exc.stdout else ""
        err = (exc.stderr or "")[-1500:] if exc.stderr else ""
        raise RuntimeError(
            f"Hive ingest 超时（{timeout}s）{label}。"
            f"可增大 BLF_HIVE_INGEST_TIMEOUT_SEC，或手工补血缘时用默认轻量注册（勿设 BLF_LINEAGE_FULL_UPSTREAM_INGEST=1）。"
            f" stderr_tail={err!r} stdout_tail={out!r}"
        ) from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-2000:]
        raise RuntimeError(f"Hive ingest 失败 exit={proc.returncode} {label}: {tail}")


def register_hive_table_list_minimal(
    refs: Sequence[TableRef],
    *,
    gms_url: str,
    token: Optional[str] = None,
    platform_instance: str = _DEFAULT_PLATFORM_INSTANCE,
    env: str = _DEFAULT_ENV,
    dry_run: bool = False,
) -> None:
    """对名单内每张表做 MCP 轻量注册（不连 HMS 扫库，秒级）。"""
    if not refs:
        raise ValueError("refs 为空")
    for ref in refs:
        register_minimal_hive_dataset(
            ref,
            gms_url=gms_url,
            token=token,
            platform_instance=platform_instance,
            env=env,
            dry_run=dry_run,
        )
    logger.info("轻量注册完成: %d 张表", len(refs))


def ingest_hive_table_list(
    refs: Sequence[TableRef],
    *,
    gms_url: str,
    token: Optional[str] = None,
    platform_instance: str = _DEFAULT_PLATFORM_INSTANCE,
    env: str = _DEFAULT_ENV,
    python_executable: Optional[str] = None,
    dry_run: bool = False,
    include_view_lineage: bool = False,
    chunk_size: int = 600,
    ingest_mode: Optional[str] = None,
) -> None:
    """将多张表写入 DataHub。

    ``ingest_mode=minimal``：仅 MCP 注册 Dataset 节点（快）。
    ``ingest_mode=full``（默认）：``datahub ingest`` 从 HMS 拉 schema；会对库内 **列举全部表名**
    再逐表 ``get_table``，``default`` 等大库即使只 allow 一张表也会很慢。
    """
    if not refs:
        raise ValueError("refs 为空")

    mode = resolve_hive_ingest_mode(ingest_mode)
    if mode == "minimal":
        if dry_run:
            logger.info(
                "[dry-run] 将轻量注册 %d 张表: %s",
                len(refs),
                ", ".join(r.full_name for r in refs[:20]),
            )
            return
        logger.info(
            "使用轻量 MCP 注册（不扫 HMS 全库）；需完整列/schema 请设 BLF_HIVE_INGEST_MODE=full"
        )
        register_hive_table_list_minimal(
            refs,
            gms_url=gms_url,
            token=token,
            platform_instance=platform_instance,
            env=env,
            dry_run=dry_run,
        )
        return

    if dry_run:
        logger.info(
            "[dry-run] 将完整 HMS ingest %d 张表: %s",
            len(refs),
            ", ".join(r.full_name for r in refs[:20]),
        )
        return

    if len(refs) <= 5:
        dbs = sorted({r.db for r in refs})
        logger.warning(
            "完整 HMS ingest：会对库 %s 执行 get_all_tables 并逐表拉元数据，"
            "recipe 里 allow 几张表不影响 HMS 扫描范围；大库可能需数十分钟。"
            "仅需血缘图节点时可设 BLF_HIVE_INGEST_MODE=minimal",
            dbs,
        )

    pairs = [(r.db, r.table) for r in refs]
    with tempfile.TemporaryDirectory(prefix="dh_hive_ingest_list_") as tmp:
        recipe_path = Path(tmp) / "ingest_table_list.yml"
        recipe = build_multi_table_recipe(
            pairs,
            platform_instance=platform_instance,
            env=env,
            include_view_lineage=include_view_lineage,
            chunk_size=chunk_size,
        )
        _dump_recipe_yaml(recipe, recipe_path)
        _run_datahub_ingest_recipe(
            recipe_path,
            gms_url=gms_url,
            token=token,
            python_executable=python_executable,
            label=f"{len(refs)} tables",
        )
    logger.info("Hive ingest 完成: %d 张表", len(refs))


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

    with tempfile.TemporaryDirectory(prefix="dh_hive_ingest_") as tmp:
        recipe_path = Path(tmp) / f"ingest_{ref.db}_{ref.table}.yml"
        recipe = build_single_table_recipe(
            ref.db,
            ref.table,
            platform_instance=platform_instance,
            env=env,
        )
        _dump_recipe_yaml(recipe, recipe_path)
        _run_datahub_ingest_recipe(
            recipe_path,
            gms_url=gms_url,
            token=token,
            python_executable=python_executable,
            label=ref.full_name,
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

    if _use_full_hive_ingest_for_upstream():
        logger.info(
            "上游表不在 DataHub，将执行完整 HMS ingest（较慢）: %s",
            upstream.full_name,
        )
        ingest_single_hive_table(
            upstream,
            gms_url=gms_url,
            token=token,
            platform_instance=platform_instance,
            env=env,
            python_executable=python_executable,
            dry_run=dry_run,
        )
        return True, f"已从 Hive 完整 ingest 上游表: {upstream.full_name}"

    logger.info(
        "上游表不在 DataHub，将轻量注册 Dataset（供血缘 UI，非 HMS 全量）: %s",
        upstream.full_name,
    )
    register_minimal_hive_dataset(
        upstream,
        gms_url=gms_url,
        token=token,
        platform_instance=platform_instance,
        env=env,
        dry_run=dry_run,
    )
    if not dry_run and not dataset_entity_exists(
        gms_url, upstream, platform_instance, env, token
    ):
        raise RuntimeError(
            f"轻量注册后仍未在 DataHub 找到 Dataset: {upstream.full_name}"
        )
    return True, f"已轻量注册上游表 Dataset: {upstream.full_name}"
