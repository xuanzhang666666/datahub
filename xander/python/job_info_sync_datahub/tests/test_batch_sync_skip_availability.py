"""batch_sync skips DataHub writes when target already has Data Availability Flag."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from job_info_sync_datahub import batch_sync as mod
from job_info_sync_datahub.models import JobMetadata, TableLineage, TableRef


def test_sync_one_skips_datahub_write_when_target_has_availability_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    metadata = JobMetadata(
        job_display_name="dm_cigarette_base_actual_sku_info_di",
        job_name="dm_cigarette_base_actual_sku_info_di",
        shell_command="/home/w/mddf-job/bin/w-run-task.sh dm_cigarette/base/actual_sku_info_di",
    )
    target = TableRef("dm_cigarette", "actual_sku_info_di")
    upstream = TableRef("ods", "sku")
    decision = SimpleNamespace(
        write_upstream_lineage=True,
        reason="ok",
        status="OK",
        selected_targets={target.full_name},
        selected_upstreams={upstream.full_name},
        trust_score=1,
    )

    monkeypatch.setattr(mod, "fetch_job_metadata", lambda _job: metadata)
    monkeypatch.setattr(mod, "download_etl_file", lambda **_kwargs: ("gitlab:jobs/dm/job.job", "select 1", "gitlab"))

    import job_info_sync_datahub.lineage_write_policy as policy

    monkeypatch.setattr(
        policy,
        "evaluate_llm_only",
        lambda *_args, **_kwargs: ([TableLineage(target=target, upstreams=[upstream])], decision, {}),
    )
    monkeypatch.setattr(policy, "append_lineage_audit_jsonl", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        mod,
        "read_target_data_availability_flags",
        lambda *_args, **_kwargs: {target.full_name: "DDL"},
        raising=False,
    )

    class FailingWriter:
        def __init__(self, *_args, **_kwargs) -> None:
            raise AssertionError("DataHub writer must not be constructed when Data Availability Flag exists")

    monkeypatch.setattr(mod, "DatahubWriter", FailingWriter)

    result = mod.sync_one(
        "dm_cigarette_base_actual_sku_info_di",
        gms_url="http://datahub-gms:8080",
        gms_token=None,
        gitlab_token=None,
        platform_instance="blf-prod-hive",
        env="PROD",
        dry_run=False,
        batch_output_dir=str(tmp_path),
    )

    assert result["status"] == "SKIP"
    assert result["target_table"] == target.full_name
    assert result["data_availability_flags"] == {target.full_name: "DDL"}
    assert "Data Availability Flag" in result["error"]
