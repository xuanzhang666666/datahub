"""batch_sync conservative handling for Sqoop MySQL -> Hive jobs."""

from __future__ import annotations

from pathlib import Path

import pytest

from job_info_sync_datahub import batch_sync as mod
from job_info_sync_datahub.models import JobMetadata


SQOOP_JOB = """
DATABASE_NAME="default"
TABLE_NAME="ods_bach_baseinfo_shop_company"
SOURCE_TABLE_NAME="company"
TABLE_COLUMNS="id,code,name"
SOURCE_TABLE_JDBC_STR="${BACH_BASEINFO_SHOP_STR}"

function ods_bach_baseinfo_shop_company_run {
    calculate
}

function calculate {
    ${SQOOP} import \\
    --connect ${SOURCE_TABLE_JDBC_STR} \\
    --table ${SOURCE_TABLE_NAME} \\
    --columns ${TABLE_COLUMNS} \\
    --hcatalog-database ${DATABASE_NAME} \\
    --hcatalog-table ${TABLE_NAME}
}
"""


def test_sync_one_documents_sqoop_without_llm_or_upstream_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    metadata = JobMetadata(
        job_display_name="ods_bach_baseinfo_shop_company",
        job_name="ods_bach_baseinfo_shop_company",
        shell_command="/home/w/analysis-jobs/bin/w-run-task.sh ods_bach_baseinfo/shop/company",
    )
    written_docs: list[tuple[str, str]] = []
    structured_writes: list[tuple[str, list[str]]] = []

    monkeypatch.setattr(mod, "fetch_job_metadata", lambda _job: metadata)
    monkeypatch.setattr(
        mod,
        "download_etl_file",
        lambda **_kwargs: (
            "gitlab:jobs/ods_bach_baseinfo/ods_bach_baseinfo_shop_company.job",
            SQOOP_JOB,
            "gitlab",
        ),
    )

    import job_info_sync_datahub.lineage_write_policy as policy

    monkeypatch.setattr(
        policy,
        "evaluate_llm_only",
        lambda *_args, **_kwargs: pytest.fail("Sqoop jobs should not call LLM extraction"),
    )

    class CapturingWriter:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def write_structured_properties(self, urn, props, *_args, **_kwargs):
            structured_writes.append((urn, [p.property_urn for p in props]))
            return True

        def write_all(self, *_args, **_kwargs):
            raise AssertionError("Sqoop conservative path must not call write_all")

    monkeypatch.setattr(mod, "DatahubWriter", CapturingWriter)
    monkeypatch.setattr(mod, "fetch_existing_editable_description", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        mod,
        "write_editable_description",
        lambda _gms, urn, doc, _token: written_docs.append((urn, doc)),
    )

    result = mod.sync_one(
        "ods_bach_baseinfo_shop_company",
        gms_url="http://datahub-gms:8080",
        gms_token=None,
        gitlab_token=None,
        platform_instance="blf-prod-hive",
        env="PROD",
        dry_run=False,
        batch_output_dir=str(tmp_path),
    )

    assert result["status"] == "OK"
    assert result["target_table"] == "default.ods_bach_baseinfo_shop_company"
    assert result["upstream_count"] == 0
    assert result["lineage_status"] == "SQOOP_DOCUMENTED"
    assert result["write_upstream_lineage"] is False
    assert result["lineage_targets_chosen"] == ["default.ods_bach_baseinfo_shop_company"]
    assert result["lineage_sources_chosen"] == ["${BACH_BASEINFO_SHOP_STR}.company"]
    assert written_docs
    assert written_docs[0][0] == (
        "urn:li:dataset:(urn:li:dataPlatform:hive,"
        "blf-prod-hive.default.ods_bach_baseinfo_shop_company,PROD)"
    )
    assert "不写 DataHub upstreamLineage" in written_docs[0][1]
    assert structured_writes == [
        (
            written_docs[0][0],
            [
                "urn:li:structuredProperty:blf.data.warehouse.etl_script",
                "urn:li:structuredProperty:blf.data.schedule.schedule_url",
                "urn:li:structuredProperty:blf.data.schedule.execute_shell",
            ],
        )
    ]
