#!/usr/bin/env python3
"""从表名列表生成 hive-metastore ingestion 用的临时 recipe YAML。

表名列表支持:
  - 每行一个全名: db.table（推荐）
  - 仅表名 + --implicit-database: 自动拼成 db.table

将全表名按 chunk 聚合成多条正则 (db\\.t1|db\\.t2|...)，写入 table_pattern.allow，
database_pattern 为列表中所有库的去重集合；若指定 --database，则只保留该库的表且
database_pattern 仅含该库。

仅依赖 Python 标准库（无 PyYAML）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set, Tuple

# 与 xander/recipes/hive_ingest_one_database.yml 及血缘侧保持一致
_DEFAULT_PLATFORM_INSTANCE = "blf-prod-hive"
_DEFAULT_ENV = "PROD"


def _split_db_table(line: str, implicit_db: Optional[str]) -> Optional[Tuple[str, str]]:
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    if "." not in s:
        if not implicit_db:
            raise ValueError(
                f"行无库名前缀且未传 --implicit-database: {s!r}"
            )
        return implicit_db.strip(), s
    db, tbl = s.split(".", 1)
    db, tbl = db.strip(), tbl.strip()
    if not db or not tbl:
        return None
    return db, tbl


def _regex_escape_db_table(db: str, tbl: str) -> str:
    return re.escape(db) + r"\." + re.escape(tbl)


def _chunked(seq: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def build_allow_patterns(
    pairs: Sequence[Tuple[str, str]], chunk_size: int
) -> List[str]:
    escaped = [_regex_escape_db_table(db, tbl) for db, tbl in pairs]
    out: List[str] = []
    for group in _chunked(escaped, chunk_size):
        inner = "|".join(group)
        out.append(f"^({inner})$")
    return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--table-list",
        required=True,
        type=Path,
        help="表名列表文件，每行 db.table（或仅表名配合 --implicit-database）",
    )
    p.add_argument(
        "--out",
        required=True,
        type=Path,
        help="输出的 recipe YAML 路径",
    )
    p.add_argument(
        "--chunk-size",
        type=int,
        default=600,
        help="每条 allow 正则内合并的表数（过大可能导致 re 编译慢或超限），默认 600",
    )
    p.add_argument(
        "--implicit-database",
        default=None,
        help="若列表每行只有表名，用此库名拼成 db.table",
    )
    p.add_argument(
        "--database",
        default=None,
        metavar="DB_NAME",
        help="只纳入该库内的表（与列表中库名精确匹配）；用于同一总表文件下分库分批 ingest",
    )
    p.add_argument(
        "--hms-host-port",
        default="${HMS_THRIFT_HOST}:${HMS_THRIFT_PORT}",
        help="recipe 中 host_port 占位，默认走环境变量展开",
    )
    p.add_argument(
        "--platform-instance",
        default=_DEFAULT_PLATFORM_INSTANCE,
        help="与 datahub_writer 一致，默认 blf-prod-hive",
    )
    p.add_argument(
        "--env",
        default=_DEFAULT_ENV,
        help="DataHub env，默认 PROD",
    )
    p.add_argument(
        "--include-view-lineage",
        action="store_true",
        help="开启视图 SQL 解析（7w+ 表时慎用，耗时会显著增加）",
    )
    p.add_argument(
        "--gms-url-placeholder",
        default="${DATAHUB_GMS_URL}",
        help="sink server 占位符",
    )
    return p.parse_args()


def _yaml_double_quoted(s: str) -> str:
    """YAML 双引号转义，供正则等含特殊字符的标量使用。"""
    esc = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{esc}"'


def _dump_recipe_yaml(recipe: dict, path: Path) -> None:
    """仅支持本脚本生成的嵌套 dict/list/str/bool 结构，无 PyYAML 依赖。"""
    lines: List[str] = []

    def emit(obj: object, ind: int) -> None:
        sp = " " * ind
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, dict):
                    lines.append(f"{sp}{k}:")
                    emit(v, ind + 2)
                elif isinstance(v, list):
                    # 空列表必须写成 []；写成「deny:」无子项时 YAML 为 null，Pydantic 会报非 list。
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


def main() -> int:
    args = parse_args()
    raw_lines = args.table_list.read_text(encoding="utf-8").splitlines()
    pairs: List[Tuple[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    for line in raw_lines:
        got = _split_db_table(line, args.implicit_database)
        if not got:
            continue
        if got in seen:
            continue
        seen.add(got)
        pairs.append(got)

    if not pairs:
        print("ERROR: 表列表为空或全部无效", file=sys.stderr)
        return 2

    if args.database is not None:
        db_filter = args.database.strip()
        if not db_filter:
            print("ERROR: --database 不能为空", file=sys.stderr)
            return 2
        pairs = [(db, t) for db, t in pairs if db == db_filter]
        if not pairs:
            print(
                f"ERROR: --database {db_filter!r} 在列表中无匹配表",
                file=sys.stderr,
            )
            return 2

    dbs = sorted({db for db, _ in pairs})
    db_allow = [f"^{re.escape(d)}$" for d in dbs]

    if args.chunk_size < 50:
        print("ERROR: --chunk-size 过小（建议 >= 200）", file=sys.stderr)
        return 2

    table_allow = build_allow_patterns(pairs, args.chunk_size)
    table_deny = [
        r"^[^.]+\.tmp_.*$",
        r"^[^.]+\.temp_.*$",
        r"^[^.]+\.bak_tmp.*$",
        r"^[^.]+\.not_verified_.*$",
    ]

    recipe = {
        "source": {
            "type": "hive-metastore",
            "config": {
                "connection_type": "thrift",
                "host_port": args.hms_host_port,
                "use_kerberos": False,
                "platform_instance": args.platform_instance,
                "env": args.env,
                "emit_storage_lineage": False,
                "hive_storage_lineage_direction": "upstream",
                "include_column_lineage": False,
                "include_view_lineage": bool(args.include_view_lineage),
                "database_pattern": {"allow": db_allow, "deny": []},
                "table_pattern": {"allow": table_allow, "deny": table_deny},
            },
        },
        "sink": {
            "type": "datahub-rest",
            "config": {"server": args.gms_url_placeholder},
        },
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    _dump_recipe_yaml(recipe, args.out)
    db_note = f" database={args.database!r}" if args.database else ""
    print(
        f"[render_hive_ingest_recipe] tables={len(pairs)} unique_dbs={len(dbs)}"
        f"{db_note} allow_regexes={len(table_allow)} chunk_size={args.chunk_size} -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
