"""Filter Hive table list to datasets missing from DataHub."""

from __future__ import annotations

from job_info_sync_datahub import filter_existing_hive_table_list as mod
from job_info_sync_datahub.models import TableRef


def test_load_table_refs_filters_database_and_dedupes(tmp_path) -> None:
    table_list = tmp_path / "tables.txt"
    table_list.write_text(
        "default.t1\nods.t2\n# comment\nods.t2\nonly_table\n",
        encoding="utf-8",
    )

    refs = mod.load_table_refs(
        table_list,
        implicit_database="default",
        database="default",
    )

    assert [ref.full_name for ref in refs] == ["default.t1", "default.only_table"]


def test_split_existing_and_missing(monkeypatch) -> None:
    refs = [TableRef("default", "exists"), TableRef("default", "missing")]

    def fake_exists(gms_url, ref, platform_instance, env, token=None):
        return ref.table == "exists"

    monkeypatch.setattr(mod, "dataset_entity_exists", fake_exists)

    existing, missing = mod.split_existing_and_missing(
        refs,
        gms_url="http://localhost:8080",
        token=None,
        platform_instance="blf-prod-hive",
        env="PROD",
    )

    assert [ref.full_name for ref in existing] == ["default.exists"]
    assert [ref.full_name for ref in missing] == ["default.missing"]


def test_write_table_list(tmp_path) -> None:
    out = tmp_path / "missing.txt"

    mod.write_table_list(out, [TableRef("default", "t1"), TableRef("ods", "t2")])

    assert out.read_text(encoding="utf-8") == "default.t1\nods.t2\n"
