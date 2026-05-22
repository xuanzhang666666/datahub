"""Jenkins TABLE_NAMES 参数驱动的 Hive 表清单 → DataHub。

**TABLE_NAMES**：Multi-line 环境变量，每行 ``库.表``（或仅表名 + ``HIVE_INGEST_IMPLICIT_DATABASE``）。
不做 fqtn 层级前缀等合法性校验，按用户输入生成 ingest recipe。

**流程**：默认只 ingest DataHub 中不存在的 Dataset；已存在的 Dataset 跳过不删除。
可设置 ``--existing-dataset-action update`` 直接刷新已存在 Dataset，或设置
``--existing-dataset-action delete`` / ``--delete-existing-dataset`` 先 hard delete 后重新 ingest。

**速度**（``BLF_HIVE_INGEST_MODE``）：

- ``minimal``（Jenkins 入口默认，推荐单表/补血缘节点）：MCP 轻量注册，秒级，无 HMS 全库扫描。
- ``full``：``datahub ingest``；HMS 会对 ``default`` 等库 ``get_all_tables`` 并逐表拉元数据，
  即使 TABLE_NAMES 只有一张表也可能很慢。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

from .hive_single_table_ingest import (
    delete_dataset_entity,
    dataset_entity_exists,
    ingest_hive_table_list,
    resolve_hive_ingest_mode,
)
from .models import TableRef

logger = logging.getLogger(__name__)


def _load_lineage_env_early() -> None:
    raw = os.environ.get("BLF_LINEAGE_ENV_FILE", "").strip()
    if not raw:
        return
    try:
        from .lineage_llm_compare import load_env_file

        load_env_file(Path(raw), override=False)
    except Exception:
        pass


def parse_table_line(line: str, implicit_database: Optional[str] = None) -> Optional[TableRef]:
    """解析一行表名；不做业务合法性校验，仅 strip / 跳过空行与 # 注释。"""
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    if "." not in s:
        db = (implicit_database or "default").strip()
        if not db:
            return TableRef(db="default", table=s)
        return TableRef(db=db, table=s)
    parts = s.split(".")
    if len(parts) == 2:
        return TableRef(db=parts[0].strip(), table=parts[1].strip())
    return TableRef(db=".".join(p.strip() for p in parts[:-1]), table=parts[-1].strip())


def parse_table_list_text(
    text: str,
    *,
    implicit_database: Optional[str] = None,
) -> List[TableRef]:
    """从多行文本解析表列表，去重保序。"""
    out: List[TableRef] = []
    seen: set[str] = set()
    for line in text.splitlines():
        ref = parse_table_line(line, implicit_database)
        if ref is None:
            continue
        key = ref.full_name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
    return out


def load_table_list_file(path: Path, implicit_database: Optional[str] = None) -> List[TableRef]:
    return parse_table_list_text(
        path.read_text(encoding="utf-8"),
        implicit_database=implicit_database,
    )


@dataclass
class SyncReport:
    tables: List[str] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)
    delete_skipped: List[str] = field(default_factory=list)
    existing_skipped: List[str] = field(default_factory=list)
    updated_existing: List[str] = field(default_factory=list)
    ingested: List[str] = field(default_factory=list)
    ingest_count: int = 0
    dry_run: bool = False

    def to_dict(self) -> dict:
        return {
            "tables": self.tables,
            "deleted": self.deleted,
            "delete_skipped": self.delete_skipped,
            "existing_skipped": self.existing_skipped,
            "updated_existing": self.updated_existing,
            "ingested": self.ingested,
            "ingest_count": self.ingest_count,
            "dry_run": self.dry_run,
        }


def resolve_existing_dataset_action(
    action: Optional[str],
    delete_existing_dataset: bool = False,
) -> str:
    """Resolve how to handle datasets that already exist in DataHub."""
    if delete_existing_dataset:
        return "delete"
    normalized = (action or "skip").strip().lower()
    if normalized not in {"skip", "update", "delete"}:
        raise ValueError("existing_dataset_action 必须是 skip / update / delete")
    return normalized


def sync_hive_tables_delete_then_ingest(
    refs: Sequence[TableRef],
    *,
    gms_url: str,
    token: Optional[str] = None,
    platform_instance: str = "blf-prod-hive",
    env: str = "PROD",
    python_executable: Optional[str] = None,
    dry_run: bool = False,
    include_view_lineage: bool = False,
    chunk_size: int = 600,
    ingest_mode: Optional[str] = None,
    delete_existing_dataset: bool = False,
    existing_dataset_action: Optional[str] = None,
) -> SyncReport:
    """对名单内每张表按 skip/update/delete 策略处理已存在 Dataset。"""
    if not refs:
        raise ValueError("表列表为空")
    existing_action = resolve_existing_dataset_action(existing_dataset_action, delete_existing_dataset)

    report = SyncReport(
        tables=[r.full_name for r in refs],
        dry_run=dry_run,
    )
    refs_to_ingest: List[TableRef] = []

    for ref in refs:
        try:
            exists = dataset_entity_exists(
                gms_url,
                ref,
                platform_instance,
                env,
                token,
            )
            if exists and existing_action == "skip":
                logger.info("DataHub 中已存在，默认跳过不删除不重建: %s", ref.full_name)
                report.existing_skipped.append(ref.full_name)
                continue
            if exists and existing_action == "update":
                logger.info("DataHub 中已存在，将直接 ingest 更新 Dataset: %s", ref.full_name)
                report.updated_existing.append(ref.full_name)
            elif exists and existing_action == "delete":
                removed = delete_dataset_entity(
                    ref,
                    gms_url=gms_url,
                    token=token,
                    platform_instance=platform_instance,
                    env=env,
                    dry_run=dry_run,
                )
                if removed:
                    report.deleted.append(ref.full_name)
                else:
                    report.delete_skipped.append(ref.full_name)
            else:
                logger.info("DataHub 中不存在，将执行 ingest: %s", ref.full_name)
            refs_to_ingest.append(ref)
            report.ingested.append(ref.full_name)
        except Exception as exc:
            raise RuntimeError(f"处理失败 {ref.full_name}: {exc}") from exc

    if refs_to_ingest:
        ingest_hive_table_list(
            refs_to_ingest,
            gms_url=gms_url,
            token=token,
            platform_instance=platform_instance,
            env=env,
            python_executable=python_executable,
            dry_run=dry_run,
            include_view_lineage=include_view_lineage,
            chunk_size=chunk_size,
            ingest_mode=ingest_mode,
        )
    else:
        logger.info("没有需要 ingest 的表；所有目标 Dataset 已存在且 existing_dataset_action=skip")
    report.ingest_count = len(refs_to_ingest)
    return report


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--table-list-file",
        help="表名列表文件（每行 库.表）；与 --table-names-text 二选一",
    )
    p.add_argument(
        "--table-names-text",
        dest="table_names_text",
        default=os.environ.get("TABLE_NAMES", ""),
        help="Multi-line 表名（默认读环境变量 TABLE_NAMES）",
    )
    p.add_argument(
        "--implicit-database",
        default=os.environ.get("HIVE_INGEST_IMPLICIT_DATABASE"),
        help="行内无库名时使用的 Hive 库，默认 default",
    )
    p.add_argument("--datahub-gms", default=os.getenv("DATAHUB_GMS_URL", "http://127.0.0.1:8080"))
    p.add_argument("--token", default=os.getenv("DATAHUB_GMS_TOKEN"))
    p.add_argument(
        "--platform-instance",
        default=os.getenv("BLF_DATAHUB_PLATFORM_INSTANCE", "blf-prod-hive"),
    )
    p.add_argument("--env", default=os.getenv("DATAHUB_ENV", "PROD"))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--include-view-lineage",
        action="store_true",
        help="开启视图 SQL 解析（慢）",
    )
    p.add_argument("--chunk-size", type=int, default=int(os.getenv("HIVE_INGEST_CHUNK_SIZE", "600")))
    p.add_argument(
        "--python",
        default=os.environ.get("LINEAGE_PYTHON") or os.environ.get("HIVE_INGEST_PYTHON"),
    )
    p.add_argument(
        "--ingest-mode",
        choices=["minimal", "full"],
        default=None,
        help="minimal=轻量 MCP；full=完整 HMS ingest（大库慢）；Jenkins 入口默认传 minimal",
    )
    p.add_argument(
        "--delete-existing-dataset",
        action="store_true",
        help="兼容旧参数：等同 --existing-dataset-action delete",
    )
    p.add_argument(
        "--existing-dataset-action",
        choices=["skip", "update", "delete"],
        default=os.getenv("EXISTING_DATASET_ACTION", "skip"),
        help="已存在 Dataset 的处理方式：skip=跳过；update=不删除直接 ingest；delete=hard delete 后 ingest",
    )
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


def main() -> int:
    _load_lineage_env_early()
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s %(message)s",
    )

    if args.table_list_file:
        refs = load_table_list_file(Path(args.table_list_file), args.implicit_database)
    elif args.table_names_text and args.table_names_text.strip():
        refs = parse_table_list_text(args.table_names_text, implicit_database=args.implicit_database)
    else:
        logger.error("请设置 TABLE_NAMES 环境变量、--table-names-text 或 --table-list-file")
        return 2

    if not refs:
        logger.error("未解析到任何表名")
        return 2

    py_exec = args.python or sys.executable
    mode = resolve_hive_ingest_mode(args.ingest_mode)
    existing_action = resolve_existing_dataset_action(
        args.existing_dataset_action,
        args.delete_existing_dataset,
    )
    logger.info(
        "同步 %d 张表 -> DataHub GMS=%s platform_instance=%s ingest_mode=%s existing_dataset_action=%s dry_run=%s",
        len(refs),
        args.datahub_gms,
        args.platform_instance,
        mode,
        existing_action,
        args.dry_run,
    )

    try:
        report = sync_hive_tables_delete_then_ingest(
            refs,
            gms_url=args.datahub_gms,
            token=args.token,
            platform_instance=args.platform_instance,
            env=args.env,
            python_executable=py_exec,
            dry_run=args.dry_run,
            include_view_lineage=args.include_view_lineage,
            chunk_size=args.chunk_size,
            ingest_mode=args.ingest_mode,
            existing_dataset_action=existing_action,
        )
    except Exception as exc:
        logger.error("同步失败: %s", exc)
        return 1

    print(
        f"[DONE] tables={len(report.tables)} deleted={len(report.deleted)} "
        f"existing_skipped={len(report.existing_skipped)} "
        f"updated_existing={len(report.updated_existing)} ingested={report.ingest_count}"
    )
    if report.deleted:
        print("deleted:", ", ".join(report.deleted))
    if report.existing_skipped:
        print("existing_skipped:", ", ".join(report.existing_skipped))
    if report.updated_existing:
        print("updated_existing:", ", ".join(report.updated_existing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
