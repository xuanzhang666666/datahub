"""Render DataHub MySQL ingestion recipes."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

DEFAULT_SYSTEM_DATABASE_DENY = [
    "^information_schema$",
    "^mysql$",
    "^performance_schema$",
    "^sys$",
]


def split_patterns(raw: str | None) -> list[str]:
    """Parse newline/comma separated regex patterns, preserving order."""
    if not raw:
        return []
    out: list[str] = []
    for line in raw.splitlines():
        for token in line.split(","):
            value = token.strip()
            if value and value not in out:
                out.append(value)
    return out


def build_mysql_ingest_recipe(
    *,
    host_port: str,
    database_allow: Sequence[str],
    table_allow: Sequence[str],
    gms_url: str,
    username: str = "${MYSQL_USERNAME}",
    password: str = "${MYSQL_PASSWORD}",
    token: str = "${DATAHUB_GMS_TOKEN}",
    include_tables: bool = True,
    include_views: bool = True,
    profiling_enabled: bool = False,
    platform_instance: str = "",
    database_deny: Sequence[str] = DEFAULT_SYSTEM_DATABASE_DENY,
    env: str = "PROD",
) -> str:
    """Return a JSON-formatted DataHub recipe.

    JSON is valid YAML for DataHub's recipe loader and avoids shell quoting bugs
    when regex patterns contain backslashes.
    """
    source_config: dict[str, object] = {
        "host_port": host_port,
        "username": username,
        "password": password,
        "include_tables": include_tables,
        "include_views": include_views,
        "profiling": {"enabled": profiling_enabled},
    }
    if database_allow:
        source_config["database_pattern"] = {"allow": list(database_allow)}
    if database_deny:
        database_pattern = dict(source_config.get("database_pattern", {}))
        database_pattern["deny"] = list(database_deny)
        source_config["database_pattern"] = database_pattern
    if table_allow:
        source_config["table_pattern"] = {"allow": list(table_allow)}
    if platform_instance.strip():
        source_config["platform_instance"] = platform_instance.strip()
    if env.strip():
        source_config["env"] = env.strip()

    recipe = {
        "source": {
            "type": "mysql",
            "config": source_config,
        },
        "sink": {
            "type": "datahub-rest",
            "config": {
                "server": gms_url,
                "token": token,
            },
        },
    }
    return json.dumps(recipe, ensure_ascii=False, indent=2) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-port", required=True)
    parser.add_argument("--database-allow", default=os.getenv("MYSQL_DATABASE_ALLOW", ""))
    parser.add_argument(
        "--database-deny",
        default=os.getenv("MYSQL_DATABASE_DENY", ",".join(DEFAULT_SYSTEM_DATABASE_DENY)),
    )
    parser.add_argument("--table-allow", default=os.getenv("MYSQL_TABLE_ALLOW", ""))
    parser.add_argument("--gms-url", default=os.getenv("DATAHUB_GMS_URL", "http://localhost:8080"))
    parser.add_argument("--platform-instance", default=os.getenv("MYSQL_PLATFORM_INSTANCE", ""))
    parser.add_argument("--env", default=os.getenv("DATAHUB_ENV", "PROD"))
    parser.add_argument("--out", required=True)
    parser.add_argument("--include-views", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--include-tables", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--profiling-enabled", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rendered = build_mysql_ingest_recipe(
        host_port=args.host_port,
        database_allow=split_patterns(args.database_allow),
        database_deny=split_patterns(args.database_deny),
        table_allow=split_patterns(args.table_allow),
        gms_url=args.gms_url,
        include_tables=args.include_tables,
        include_views=args.include_views,
        profiling_enabled=args.profiling_enabled,
        platform_instance=args.platform_instance,
        env=args.env,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
