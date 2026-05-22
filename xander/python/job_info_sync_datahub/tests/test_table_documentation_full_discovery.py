from __future__ import annotations

import json

from job_info_sync_datahub.structured_properties import URN_ETL_SCRIPT, URN_EXECUTE_SHELL
from job_info_sync_datahub.table_documentation_full_discovery import (
    discover_candidates_from_rows,
    is_meaningful_doc_source_text,
    parse_hive_dataset_urn,
    structured_doc_sources,
)


def _urn(table: str, env: str = "PROD") -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:hive,blf-prod-hive.{table},{env})"


def _metadata(*props: tuple[str, str]) -> str:
    return json.dumps(
        {
            "properties": [
                {
                    "propertyUrn": urn,
                    "values": [{"string": value}],
                }
                for urn, value in props
            ]
        }
    )


def test_parse_hive_dataset_urn_returns_db_table_for_matching_platform_and_env() -> None:
    assert (
        parse_hive_dataset_urn(
            _urn("data_dw.dw_target"),
            platform_instance="blf-prod-hive",
            env="PROD",
        )
        == "data_dw.dw_target"
    )
    assert parse_hive_dataset_urn(_urn("data_dw.dw_target", env="DEV"), platform_instance="blf-prod-hive", env="PROD") is None
    assert (
        parse_hive_dataset_urn(
            "urn:li:dataset:(urn:li:dataPlatform:hive,other.data_dw.dw_target,PROD)",
            platform_instance="blf-prod-hive",
            env="PROD",
        )
        is None
    )


def test_structured_doc_sources_reads_non_empty_etl_or_shell() -> None:
    assert structured_doc_sources(_metadata((URN_ETL_SCRIPT, "```sql\nselect 1\n```"))) == ("Etl Script",)
    assert structured_doc_sources(_metadata((URN_EXECUTE_SHELL, "sh run.sh"))) == ("Execute Shell",)
    assert structured_doc_sources(_metadata((URN_ETL_SCRIPT, "```sql\n\n```"), (URN_EXECUTE_SHELL, ""))) == ()
    assert structured_doc_sources(_metadata((URN_ETL_SCRIPT, "无"), (URN_EXECUTE_SHELL, "```shell\n无\n```"))) == ()


def test_is_meaningful_doc_source_text_treats_manual_none_as_empty() -> None:
    assert is_meaningful_doc_source_text("select 1")
    assert not is_meaningful_doc_source_text("")
    assert not is_meaningful_doc_source_text("无")
    assert not is_meaningful_doc_source_text("```sql\n无\n```")


def test_discover_candidates_filters_views_and_sorts_tables() -> None:
    rows = [
        (_urn("data_b.z_table"), _metadata((URN_ETL_SCRIPT, "select 1"))),
        (_urn("data_a.a_table"), _metadata((URN_EXECUTE_SHELL, "sh run.sh"))),
        (_urn("data_a.view_table"), _metadata((URN_ETL_SCRIPT, "select 2"))),
        (_urn("data_a.no_source"), _metadata((URN_ETL_SCRIPT, ""))),
    ]
    candidates = discover_candidates_from_rows(
        rows,
        {_urn("data_a.view_table")},
        set(),
        platform_instance="blf-prod-hive",
        env="PROD",
    )

    assert [item.table_name for item in candidates] == ["data_a.a_table", "data_b.z_table"]
    assert candidates[0].source_properties == ("Execute Shell",)
    assert candidates[1].source_properties == ("Etl Script",)


def test_discover_candidates_filters_deprecated_datasets() -> None:
    rows = [
        (_urn("data_a.active_table"), _metadata((URN_ETL_SCRIPT, "select 1"))),
        (_urn("data_a.deprecated_table"), _metadata((URN_EXECUTE_SHELL, "sh run.sh"))),
    ]

    candidates = discover_candidates_from_rows(
        rows,
        set(),
        {_urn("data_a.deprecated_table")},
        platform_instance="blf-prod-hive",
        env="PROD",
    )

    assert [item.table_name for item in candidates] == ["data_a.active_table"]
