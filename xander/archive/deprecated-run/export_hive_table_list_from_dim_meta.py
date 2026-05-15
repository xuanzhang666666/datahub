#!/usr/bin/env python3
"""从 default.dim_dc_hive_meta_info_di 拉取 Hive 表全名，写入每行 db.table 的文本文件。

与 ingest_hive_table_list_to_datahub.sh 衔接：先导出列表，再设置
HIVE_INGEST_TABLE_LIST_FILE 跑 ingest。

依赖: pip install trino（与 job_info_sync_datahub 调度侧一致）
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--out",
        type=Path,
        required=True,
        help="输出文件路径，每行一个 db.table",
    )
    p.add_argument(
        "--dt",
        default=os.getenv("HIVE_META_DT", "").strip(),
        help="分区 dt，如 20260512；默认读环境变量 HIVE_META_DT",
    )
    p.add_argument(
        "--catalog",
        default=os.getenv("TRINO_CATALOG", "hive"),
        help="Trino catalog，默认 hive",
    )
    p.add_argument(
        "--schema",
        default=os.getenv("TRINO_SCHEMA", "default"),
        help="维表所在 schema，默认 default",
    )
    p.add_argument(
        "--table",
        default="dim_dc_hive_meta_info_di",
        help="元信息表名，默认 dim_dc_hive_meta_info_di",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.dt:
        print("ERROR: 请传 --dt 或设置环境变量 HIVE_META_DT（分区日期）", file=sys.stderr)
        return 2

    try:
        import trino.dbapi
    except ImportError as e:
        print("ERROR: 请安装 trino: pip install trino", file=sys.stderr)
        raise SystemExit(2) from e

    host = os.getenv("TRINO_HOST", "10.253.7.167")
    port = int(os.getenv("TRINO_PORT", "8081"))
    user = os.getenv("TRINO_USER", "xuan.zhang")
    if not re.fullmatch(r"\d{8}", args.dt):
        print("ERROR: --dt / HIVE_META_DT 须为 8 位数字分区，如 20260512", file=sys.stderr)
        return 2

    # FROM 使用 schema.table；catalog 由连接参数 TRINO_CATALOG（默认 hive）解析。
    fq = f"{args.schema}.{args.table}"
    # Trino 的 concat() 为二元函数，不能用 concat(a, '.', b)；用 || 拼接三段。
    sql = f"""
SELECT DISTINCT
    trim(cast(db_name AS varchar)) || '.' || trim(cast(table_name AS varchar)) AS fqtn
FROM {fq}
WHERE dt = '{args.dt}'
  AND is_hive_table = '1'
  AND trim(cast(db_name AS varchar)) <> ''
  AND trim(cast(table_name AS varchar)) <> ''
ORDER BY 1
""".strip()

    conn = trino.dbapi.connect(
        host=host,
        port=port,
        user=user,
        catalog=args.catalog,
        schema=args.schema,
    )
    cur = conn.cursor()
    cur.execute(sql)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    lines: list[str] = []
    seen: set[str] = set()
    for (cell,) in rows:
        s = str(cell).strip() if cell is not None else ""
        if not s or "." not in s:
            continue
        low = s.lower()
        if low in seen:
            continue
        seen.add(low)
        lines.append(s)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    print(
        f"[export_hive_table_list_from_dim_meta] dt={args.dt} "
        f"source={fq} rows={len(rows)} unique_lines={len(lines)} -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
