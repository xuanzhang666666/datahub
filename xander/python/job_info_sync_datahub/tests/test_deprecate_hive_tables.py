"""Batch deprecate Hive tables from Jenkins TABLE_NAMES."""

from __future__ import annotations

from job_info_sync_datahub.deprecate_hive_tables import (
    DEFAULT_DEPRECATION_NOTE,
    build_deprecated_structured_properties,
    dataset_urn_for_table_name,
    parse_table_names,
    update_tables,
)


def test_parse_table_names_accepts_multiline_and_commas() -> None:
    names = parse_table_names(
        """
        default.table_a
        # comment
        table_b, default.table_c

        """
    )

    assert names == ["default.table_a", "table_b", "default.table_c"]


def test_dataset_urn_for_table_name_defaults_to_default_db() -> None:
    assert dataset_urn_for_table_name("table_b") == (
        "urn:li:dataset:(urn:li:dataPlatform:hive,"
        "blf-prod-hive.default.table_b,PROD)"
    )
    assert dataset_urn_for_table_name("default.table_a") == (
        "urn:li:dataset:(urn:li:dataPlatform:hive,"
        "blf-prod-hive.default.table_a,PROD)"
    )


def test_build_deprecated_structured_properties_defaults() -> None:
    props = build_deprecated_structured_properties()

    assert {p.property_urn: p.string_value for p in props} == {
        "urn:li:structuredProperty:blf.data.schedule.schedule_url": "无",
        "urn:li:structuredProperty:blf.data.warehouse.etl_script": "无",
        "urn:li:structuredProperty:blf.data.warehouse.other_remark": DEFAULT_DEPRECATION_NOTE,
        "urn:li:structuredProperty:blf.data.schedule.execute_shell": "无",
    }


def test_update_tables_calls_deprecation_and_structured_property_writers() -> None:
    calls: list[tuple[str, str, object]] = []

    def fake_deprecate(gms_url: str, dataset_urn: str, note: str, actor: str, token: str | None) -> None:
        calls.append(("deprecate", dataset_urn, (gms_url, note, actor, token)))

    def fake_patch(gms_url: str, dataset_urn: str, props: object, token: str | None) -> None:
        calls.append(("patch", dataset_urn, props))

    result = update_tables(
        ["default.table_a"],
        gms_url="http://localhost:8080",
        token=None,
        actor="urn:li:corpuser:xuan.zhang",
        mark_deprecated_fn=fake_deprecate,
        patch_structured_properties_fn=fake_patch,
    )

    expected_urn = (
        "urn:li:dataset:(urn:li:dataPlatform:hive,"
        "blf-prod-hive.default.table_a,PROD)"
    )
    assert result == [{"table": "default.table_a", "urn": expected_urn, "status": "OK", "error": ""}]
    assert calls[0] == (
        "deprecate",
        expected_urn,
        ("http://localhost:8080", DEFAULT_DEPRECATION_NOTE, "urn:li:corpuser:xuan.zhang", None),
    )
    assert calls[1][0] == "patch"
    assert calls[1][1] == expected_urn
