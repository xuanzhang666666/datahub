"""Fallback ETL source loading from existing DataHub structured properties."""

from __future__ import annotations

from job_info_sync_datahub import etl_source_fallback as fallback


def test_looks_like_inline_etl_detects_sql_but_not_plain_shell() -> None:
    assert fallback.looks_like_inline_etl("$HIVE -e 'insert overwrite table t select * from s'")
    assert fallback.looks_like_inline_etl("select a from data_smartorder.t")
    assert not fallback.looks_like_inline_etl("echo hello")


def test_load_existing_structured_etl_source_uses_execute_shell_when_etl_empty(monkeypatch) -> None:
    urn = "urn:li:dataset:(urn:li:dataPlatform:hive,blf-prod-hive.data_smartorder.some_job,PROD)"

    monkeypatch.setattr(fallback, "_candidate_dataset_urns", lambda *args, **kwargs: [urn])
    monkeypatch.setattr(fallback, "fetch_structured_properties", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        fallback,
        "extract_property_texts",
        lambda payload: ("", "function calculate { select * from data_smartorder.ods_source; }"),
    )

    source = fallback.load_existing_structured_etl_source(
        "http://gms",
        None,
        "some_job",
    )

    assert source is not None
    assert source.table_name == "data_smartorder.some_job"
    assert source.property_label == "Execute Shell"
    assert source.source_name == "datahub_structured_execute_shell"
    assert "ods_source" in source.content
