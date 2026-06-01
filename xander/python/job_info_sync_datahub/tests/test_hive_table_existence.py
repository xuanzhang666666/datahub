"""Hive information_schema existence filtering for table lineage."""

from __future__ import annotations

from unittest.mock import patch

from job_info_sync_datahub.hive_table_existence import filter_lineages_drop_missing_upstreams
from job_info_sync_datahub.lineage_vote import full_names_to_refs
from job_info_sync_datahub.models import TableLineage


def _lineage(target: str, upstreams: list[str]) -> TableLineage:
    tgt_refs = full_names_to_refs({target})
    assert tgt_refs
    return TableLineage(
        target=tgt_refs[0],
        upstreams=full_names_to_refs(set(upstreams)),
        source_block_indices=[],
    )


def test_filter_lineages_drop_missing_upstreams_strips_absent_tables() -> None:
    lineages = [
        _lineage(
            "data_takeaway.pdw_target_di",
            ["default.ods_exists", "default.ods_missing"],
        )
    ]
    existing = {"data_takeaway.pdw_target_di", "default.ods_exists"}

    with patch(
        "job_info_sync_datahub.hive_table_existence.query_hive_existing_fqtns",
        return_value=existing,
    ):
        kept, meta = filter_lineages_drop_missing_upstreams(lineages)

    assert len(kept) == 1
    assert {u.full_name for u in kept[0].upstreams} == {"default.ods_exists"}
    assert meta["mode"] == "drop_missing_upstream"
    assert meta["stripped_upstreams"][0]["missing_upstreams"] == ["default.ods_missing"]


def test_filter_lineages_drop_missing_upstreams_removes_lineage_when_all_upstreams_missing() -> None:
    lineages = [_lineage("data_takeaway.pdw_target_di", ["default.ods_missing"])]

    with patch(
        "job_info_sync_datahub.hive_table_existence.query_hive_existing_fqtns",
        return_value={"data_takeaway.pdw_target_di"},
    ):
        kept, meta = filter_lineages_drop_missing_upstreams(lineages)

    assert kept == []
    assert meta["removed_lineages"][0]["reason"] == "no_valid_upstream_in_hive"
