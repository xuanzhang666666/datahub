"""Jenkins JOBS 参数驱动的 Hive 表清单 → DataHub（存在则先删后 ingest）。

**JOBS**：Multi-line 环境变量，每行 ``库.表``（或仅表名 + ``HIVE_INGEST_IMPLICIT_DATABASE``）。
不做 fqtn 层级前缀等合法性校验，按用户输入生成 ingest recipe。

**流程**：对每张表若 DataHub 已有则 hard delete → 写入 DataHub。

**速度**（``BLF_HIVE_INGEST_MODE``）：

- ``minimal``（推荐单表/补血缘节点）：MCP 轻量注册，秒级，无 HMS 全库扫描。
- ``full``（默认）：``datahub ingest``；HMS 会对 ``default`` 等库 ``get_all_tables`` 并逐表拉元数据，
  即使 JOBS 只有一张表也可能很慢。
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
    ingest_count: int = 0
    dry_run: bool = False

    def to_dict(self) -> dict:
        return {
            "tables": self.tables,
            "deleted": self.deleted,
            "delete_skipped": self.delete_skipped,
            "ingest_count": self.ingest_count,
            "dry_run": self.dry_run,
        }


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
) -> SyncReport:
    """对名单内每张表：存在则删除，最后批量 ingest。"""
    if not refs:
        raise ValueError("表列表为空")

    report = SyncReport(
        tables=[r.full_name for r in refs],
        dry_run=dry_run,
    )

    for ref in refs:
        try:
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
        except Exception as exc:
            raise RuntimeError(f"删除失败 {ref.full_name}: {exc}") from exc

    ingest_hive_table_list(
        refs,
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
    report.ingest_count = len(refs)
    return report


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--table-list-file",
        help="表名列表文件（每行 库.表）；与 --jobs-text 二选一",
    )
    p.add_argument(
        "--jobs-text",
        default=os.environ.get("JOBS", ""),
        help="Multi-line 表名（默认读环境变量 JOBS）",
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
        help="minimal=轻量 MCP；full=完整 HMS ingest（默认，大库慢）",
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
    elif args.jobs_text and args.jobs_text.strip():
        refs = parse_table_list_text(args.jobs_text, implicit_database=args.implicit_database)
    else:
        logger.error("请设置 JOBS 环境变量、--jobs-text 或 --table-list-file")
        return 2

    if not refs:
        logger.error("未解析到任何表名")
        return 2

    py_exec = args.python or sys.executable
    mode = resolve_hive_ingest_mode(args.ingest_mode)
    logger.info(
        "同步 %d 张表 -> DataHub GMS=%s platform_instance=%s ingest_mode=%s dry_run=%s",
        len(refs),
        args.datahub_gms,
        args.platform_instance,
        mode,
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
        )
    except Exception as exc:
        logger.error("同步失败: %s", exc)
        return 1

    print(
        f"[DONE] tables={len(report.tables)} deleted={len(report.deleted)} "
        f"skipped_delete={len(report.delete_skipped)} ingested={report.ingest_count}"
    )
    if report.deleted:
        print("deleted:", ", ".join(report.deleted))
    return 0


if __name__ == "__main__":
    sys.exit(main())
