"""MySQL ingest recipe rendering tests."""

from __future__ import annotations

import json

from job_info_sync_datahub.mysql_ingest_recipe import build_mysql_ingest_recipe


def test_build_mysql_ingest_recipe_uses_env_credentials_and_metadata_only_defaults() -> None:
    rendered = build_mysql_ingest_recipe(
        host_port="mysql.example.com:3306",
        database_allow=["^app_db$"],
        table_allow=["^app_db\\..*$"],
        gms_url="http://localhost:8080",
        platform_instance="blf-prod-mysql",
    )

    recipe = json.loads(rendered)

    assert recipe["source"]["type"] == "mysql"
    source_config = recipe["source"]["config"]
    assert source_config["host_port"] == "mysql.example.com:3306"
    assert source_config["username"] == "${MYSQL_USERNAME}"
    assert source_config["password"] == "${MYSQL_PASSWORD}"
    assert source_config["database_pattern"]["allow"] == ["^app_db$"]
    assert source_config["database_pattern"]["deny"] == [
        "^information_schema$",
        "^mysql$",
        "^performance_schema$",
        "^sys$",
    ]
    assert source_config["table_pattern"]["allow"] == ["^app_db\\..*$"]
    assert source_config["include_tables"] is True
    assert source_config["include_views"] is True
    assert source_config["profiling"]["enabled"] is False
    assert source_config["platform_instance"] == "blf-prod-mysql"
    assert source_config["env"] == "PROD"
    assert recipe["sink"]["type"] == "datahub-rest"
    assert recipe["sink"]["config"]["server"] == "http://localhost:8080"
    assert recipe["sink"]["config"]["token"] == "${DATAHUB_GMS_TOKEN}"


def test_build_mysql_ingest_recipe_keeps_system_database_deny_when_allow_is_empty() -> None:
    rendered = build_mysql_ingest_recipe(
        host_port="mysql.example.com:3306",
        database_allow=[],
        table_allow=[],
        gms_url="http://localhost:8080",
        platform_instance="",
    )

    source_config = json.loads(rendered)["source"]["config"]

    assert source_config["database_pattern"] == {
        "deny": [
            "^information_schema$",
            "^mysql$",
            "^performance_schema$",
            "^sys$",
        ]
    }
    assert "table_pattern" not in source_config
    assert "platform_instance" not in source_config


def test_build_mysql_ingest_recipe_can_disable_database_deny() -> None:
    rendered = build_mysql_ingest_recipe(
        host_port="mysql.example.com:3306",
        database_allow=[],
        database_deny=[],
        table_allow=[],
        gms_url="http://localhost:8080",
    )

    source_config = json.loads(rendered)["source"]["config"]

    assert "database_pattern" not in source_config
