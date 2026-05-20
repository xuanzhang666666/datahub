"""Fallback ETL source loading from existing DataHub structured properties."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .field_lineage_datahub_reader import (
    extract_property_texts,
    fetch_structured_properties,
)
from .logging_utils import get_logger
from .query_upstream_lineage import urn_to_table_name

logger = get_logger("etl_source_fallback")

_INLINE_ETL_RE = re.compile(
    r"\b(insert\s+(?:overwrite|into)|create\s+table|select\s+.+\s+from|from\s+[A-Za-z_])\b|\$HIVE\b",
    re.IGNORECASE | re.DOTALL,
)

_SCHEDULING_SUFFIXES = ("_merge", "_test")


@dataclass(frozen=True)
class StructuredPropertyEtlSource:
    dataset_urn: str
    table_name: str
    content: str
    property_label: str

    @property
    def source_name(self) -> str:
        return f"datahub_structured_{self.property_label.lower().replace(' ', '_')}"

    @property
    def resolved_path(self) -> str:
        return f"datahub_structured_property:{self.table_name}:{self.property_label}"


def looks_like_inline_etl(shell_command: str) -> bool:
    """Return true when shell_command itself appears to contain SQL/ETL content."""
    return bool(_INLINE_ETL_RE.search(shell_command or ""))


def table_name_candidates(table_or_job_name: str) -> list[str]:
    """Return exact table-name candidates derived from a job display name."""
    name = table_or_job_name.strip().lower()
    if not name:
        return []
    out = [name]
    for suffix in _SCHEDULING_SUFFIXES:
        if name.endswith(suffix):
            out.append(name[: -len(suffix)])
    return list(dict.fromkeys(out))


def _make_graph(gms_url: str, token: Optional[str]):
    try:
        from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
    except ImportError as exc:
        raise RuntimeError("需要安装 acryl-datahub: pip install acryl-datahub") from exc

    return DataHubGraph(DatahubClientConfig(server=gms_url.rstrip("/"), token=token))


def _candidate_dataset_urns(
    gms_url: str,
    token: Optional[str],
    table_or_job_name: str,
    *,
    platform_instance: str,
    env: str,
) -> list[str]:
    name = table_or_job_name.strip().lower()
    if not name:
        return []
    graph = _make_graph(gms_url, token)
    matches: list[str] = []
    for candidate in table_name_candidates(name):
        for urn in graph.get_urns_by_filter(
            entity_types=["dataset"],
            platform="hive",
            platform_instance=platform_instance,
            env=env,
            query=candidate,
            batch_size=1000,
        ):
            if not isinstance(urn, str) or not urn:
                continue
            table_name = urn_to_table_name(urn, platform_instance).lower()
            if table_name == candidate or table_name.rsplit(".", 1)[-1] == candidate:
                matches.append(urn)
    return sorted(set(matches))


def load_existing_structured_etl_source(
    gms_url: str,
    token: Optional[str],
    table_or_job_name: str,
    *,
    platform_instance: str = "blf-prod-hive",
    env: str = "PROD",
) -> Optional[StructuredPropertyEtlSource]:
    """Find a same-named Hive dataset and reuse its Etl Script / Execute Shell content.

    This is a compatibility fallback for historical jobs whose DMP shell_command no
    longer contains a runner path, but DataHub already has the job script stored in
    structured properties.
    """
    candidates = _candidate_dataset_urns(
        gms_url,
        token,
        table_or_job_name,
        platform_instance=platform_instance,
        env=env,
    )
    if not candidates:
        logger.info(
            "未找到同名 DataHub Hive dataset，无法从 structuredProperties 兜底: %s",
            table_or_job_name,
        )
        return None

    for urn in candidates:
        table_name = urn_to_table_name(urn, platform_instance).lower()
        try:
            payload = fetch_structured_properties(gms_url, urn, token=token)
        except Exception as exc:
            logger.warning("读取 structuredProperties 失败: table=%s err=%s", table_name, exc)
            continue
        etl_script, execute_shell = extract_property_texts(payload)
        if etl_script.strip():
            return StructuredPropertyEtlSource(
                dataset_urn=urn,
                table_name=table_name,
                content=etl_script,
                property_label="Etl Script",
            )
        if execute_shell.strip():
            return StructuredPropertyEtlSource(
                dataset_urn=urn,
                table_name=table_name,
                content=execute_shell,
                property_label="Execute Shell",
            )

    logger.info(
        "同名 DataHub Hive dataset 未保存 Etl Script / Execute Shell 内容: %s candidates=%s",
        table_or_job_name,
        [urn_to_table_name(u, platform_instance) for u in candidates],
    )
    return None
