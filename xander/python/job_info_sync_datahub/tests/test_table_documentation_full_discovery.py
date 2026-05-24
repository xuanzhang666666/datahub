from __future__ import annotations

import json
import os
from unittest import mock

from job_info_sync_datahub.structured_properties import URN_ETL_SCRIPT, URN_EXECUTE_SHELL
from job_info_sync_datahub.table_documentation_full_discovery import (
    DatasetDocCandidate,
    _mysql_candidate_cmds,
    _mysql_host_base_cmd,
    discover_candidates_from_rows,
    filter_candidates_by_table_prefix,
    filter_table_names_by_prefix,
    filter_table_names_file,
    is_meaningful_doc_source_text,
    parse_hive_dataset_urn,
    structured_doc_sources,
    table_name_matches_prefix,
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


def test_mysql_base_cmd_uses_direct_client_when_host_is_set() -> None:
    env = {
        "DATAHUB_MYSQL_HOST": "127.0.0.1",
        "DATAHUB_MYSQL_PORT": "3307",
        "DATAHUB_MYSQL_USER": "root",
        "DATAHUB_MYSQL_PASSWORD": "secret",
        "DATAHUB_MYSQL_DATABASE": "datahub",
        "DATAHUB_MYSQL_CONTAINER": "should-not-use",
        "DATAHUB_MYSQL_CLIENT": "mysql",
    }
    with mock.patch.dict(os.environ, env, clear=False):
        cmd = _mysql_host_base_cmd()
    assert cmd[:6] == ["mysql", "-h127.0.0.1", "-P3307", "-uroot", "-psecret", "-D"]
    assert "docker" not in cmd


def test_mysql_candidate_cmds_prefers_host_and_keeps_docker_fallback() -> None:
    env = {
        "DATAHUB_MYSQL_USER": "root",
        "DATAHUB_MYSQL_PASSWORD": "datahub",
        "DATAHUB_MYSQL_DATABASE": "datahub",
        "DATAHUB_MYSQL_CONTAINER": "datahub-mysql-1",
        "DATAHUB_MYSQL_CLIENT": "mysql",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        cmds = _mysql_candidate_cmds()
    assert cmds[0][:3] == ["mysql", "-h127.0.0.1", "-P3306"]
    assert cmds[1][:4] == ["docker", "exec", "datahub-mysql-1", "mysql"]


def test_table_name_matches_prefix_table_segment_only() -> None:
    assert table_name_matches_prefix("ods.pdw_target", "pdw")
    assert table_name_matches_prefix("ODS.PDW_TARGET", "pdw")
    assert not table_name_matches_prefix("pdw.dim_store", "pdw")
    assert not table_name_matches_prefix("data_dw.dw_target", "pdw")
    assert not table_name_matches_prefix("pdw", "pdw")


def test_filter_candidates_by_table_prefix() -> None:
    candidates = [
        DatasetDocCandidate("u1", "pdw.a", ("Etl Script",)),
        DatasetDocCandidate("u2", "ods.pdw_b", ("Execute Shell",)),
        DatasetDocCandidate("u3", "data_dw.c", ("Etl Script",)),
    ]
    filtered = filter_candidates_by_table_prefix(candidates, "pdw")
    assert [c.table_name for c in filtered] == ["ods.pdw_b"]


def test_filter_table_names_by_prefix() -> None:
    assert filter_table_names_by_prefix(["pdw.x", "data_dw.y", "ods.pdw_z"], "pdw") == ["ods.pdw_z"]
    assert filter_table_names_by_prefix(["a.b"], "") == ["a.b"]


def test_filter_table_names_file_writes_filtered_lines(tmp_path) -> None:
    src = tmp_path / "in.txt"
    out = tmp_path / "out.txt"
    src.write_text("pdw.dim_store\nods.pdw_target\ndata_dw.dw\n", encoding="utf-8")
    count = filter_table_names_file(str(src), "pdw", str(out))
    assert count == 1
    assert out.read_text(encoding="utf-8") == "ods.pdw_target\n"


def test_filter_candidates_by_table_prefix_empty_prefix_is_noop() -> None:
    candidates = [
        DatasetDocCandidate("u1", "pdw.a", ("Etl Script",)),
        DatasetDocCandidate("u2", "data_dw.c", ("Etl Script",)),
    ]
    assert filter_candidates_by_table_prefix(candidates, "") == candidates


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
