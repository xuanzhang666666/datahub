"""hive_jobs_table_ingest：表名解析与删后 ingest 流程（mock）。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from job_info_sync_datahub import hive_jobs_table_ingest
from job_info_sync_datahub.hive_jobs_table_ingest import (
    parse_table_line,
    parse_table_list_text,
    resolve_existing_dataset_action,
    sync_hive_tables_delete_then_ingest,
)
from job_info_sync_datahub.hive_single_table_ingest import build_multi_table_recipe
from job_info_sync_datahub.models import TableRef


class TestParseTableList(unittest.TestCase):
    def test_db_table(self) -> None:
        r = parse_table_line("data_sec_dw.dim_store_info")
        assert r is not None
        self.assertEqual(r.full_name, "data_sec_dw.dim_store_info")

    def test_three_part_db(self) -> None:
        r = parse_table_line("cat.db.table_x")
        assert r is not None
        self.assertEqual(r.db, "cat.db")
        self.assertEqual(r.table, "table_x")

    def test_implicit_db(self) -> None:
        r = parse_table_line("dim_store_info", implicit_database="data_sec_dw")
        assert r is not None
        self.assertEqual(r.full_name, "data_sec_dw.dim_store_info")

    def test_skip_comment_and_dedupe(self) -> None:
        text = "default.a\n# comment\n\ndefault.a\ndefault.b\n"
        refs = parse_table_list_text(text)
        self.assertEqual([r.full_name for r in refs], ["default.a", "default.b"])

    def test_static_prefix_allowed(self) -> None:
        r = parse_table_line("default.static_mid_store_info_hd")
        assert r is not None
        self.assertTrue(r.table.startswith("static_"))


class TestMultiTableRecipe(unittest.TestCase):
    def test_no_deny_by_default(self) -> None:
        r = build_multi_table_recipe([("default", "static_foo")])
        self.assertEqual(r["source"]["config"]["table_pattern"]["deny"], [])


class TestSyncFlow(unittest.TestCase):
    def test_resolve_existing_dataset_action(self) -> None:
        self.assertEqual(resolve_existing_dataset_action("skip", False), "skip")
        self.assertEqual(resolve_existing_dataset_action("update", False), "update")
        self.assertEqual(resolve_existing_dataset_action("delete", False), "delete")
        self.assertEqual(resolve_existing_dataset_action(None, True), "delete")

    def test_default_skips_existing_and_ingests_missing_only(self) -> None:
        refs = [TableRef("default", "t1"), TableRef("ods", "t2")]
        with patch(
            "job_info_sync_datahub.hive_jobs_table_ingest.dataset_entity_exists",
            side_effect=[True, False],
        ) as exists_mock:
            with patch(
                "job_info_sync_datahub.hive_jobs_table_ingest.delete_dataset_entity",
            ) as del_mock:
                with patch(
                    "job_info_sync_datahub.hive_jobs_table_ingest.ingest_hive_table_list",
                ) as ingest_mock:
                    rep = sync_hive_tables_delete_then_ingest(
                        refs,
                        gms_url="http://127.0.0.1:8080",
                        dry_run=False,
                    )
        self.assertEqual(exists_mock.call_count, 2)
        del_mock.assert_not_called()
        ingest_mock.assert_called_once()
        ingest_refs = ingest_mock.call_args.args[0]
        self.assertEqual([r.full_name for r in ingest_refs], ["ods.t2"])
        self.assertEqual(rep.deleted, [])
        self.assertEqual(rep.existing_skipped, ["default.t1"])
        self.assertEqual(rep.ingested, ["ods.t2"])
        self.assertEqual(rep.ingest_count, 1)

    def test_update_existing_dataset_ingests_without_delete(self) -> None:
        refs = [TableRef("default", "t1"), TableRef("ods", "t2")]
        with patch(
            "job_info_sync_datahub.hive_jobs_table_ingest.dataset_entity_exists",
            side_effect=[True, False],
        ) as exists_mock:
            with patch(
                "job_info_sync_datahub.hive_jobs_table_ingest.delete_dataset_entity",
            ) as del_mock:
                with patch(
                    "job_info_sync_datahub.hive_jobs_table_ingest.ingest_hive_table_list",
                ) as ingest_mock:
                    rep = sync_hive_tables_delete_then_ingest(
                        refs,
                        gms_url="http://127.0.0.1:8080",
                        dry_run=False,
                        existing_dataset_action="update",
                    )
        self.assertEqual(exists_mock.call_count, 2)
        del_mock.assert_not_called()
        ingest_mock.assert_called_once()
        ingest_refs = ingest_mock.call_args.args[0]
        self.assertEqual([r.full_name for r in ingest_refs], ["default.t1", "ods.t2"])
        self.assertEqual(rep.updated_existing, ["default.t1"])
        self.assertEqual(rep.existing_skipped, [])
        self.assertEqual(rep.ingested, ["default.t1", "ods.t2"])
        self.assertEqual(rep.ingest_count, 2)

    def test_delete_existing_dataset_allows_delete_then_ingest(self) -> None:
        refs = [TableRef("default", "t1"), TableRef("ods", "t2")]
        with patch(
            "job_info_sync_datahub.hive_jobs_table_ingest.dataset_entity_exists",
            side_effect=[True, False],
        ) as exists_mock:
            with patch(
                "job_info_sync_datahub.hive_jobs_table_ingest.delete_dataset_entity",
                return_value=True,
            ) as del_mock:
                with patch(
                    "job_info_sync_datahub.hive_jobs_table_ingest.ingest_hive_table_list",
                ) as ingest_mock:
                    rep = sync_hive_tables_delete_then_ingest(
                        refs,
                        gms_url="http://127.0.0.1:8080",
                        dry_run=False,
                        existing_dataset_action="delete",
                    )
        self.assertEqual(exists_mock.call_count, 2)
        del_mock.assert_called_once()
        ingest_mock.assert_called_once()
        ingest_refs = ingest_mock.call_args.args[0]
        self.assertEqual([r.full_name for r in ingest_refs], ["default.t1", "ods.t2"])
        self.assertEqual(rep.deleted, ["default.t1"])
        self.assertEqual(rep.existing_skipped, [])
        self.assertEqual(rep.ingested, ["default.t1", "ods.t2"])
        self.assertEqual(rep.ingest_count, 2)

    def test_minimal_mode_passed_through(self) -> None:
        refs = [TableRef("default", "static_x")]
        with patch(
            "job_info_sync_datahub.hive_jobs_table_ingest.dataset_entity_exists",
            return_value=False,
        ):
            with patch(
                "job_info_sync_datahub.hive_jobs_table_ingest.ingest_hive_table_list",
            ) as ingest_mock:
                sync_hive_tables_delete_then_ingest(
                    refs,
                    gms_url="http://127.0.0.1:8080",
                    ingest_mode="minimal",
                )
        ingest_mock.assert_called_once()
        self.assertEqual(ingest_mock.call_args.kwargs.get("ingest_mode"), "minimal")

    def test_main_passes_delete_existing_dataset_flag(self) -> None:
        with patch.dict("os.environ", {"TABLE_NAMES": "default.t1\n"}, clear=False):
            with patch(
                "sys.argv",
                [
                    "hive_jobs_table_ingest.py",
                    "--dry-run",
                    "--ingest-mode",
                    "minimal",
                    "--delete-existing-dataset",
                ],
            ):
                with patch(
                    "job_info_sync_datahub.hive_jobs_table_ingest.sync_hive_tables_delete_then_ingest",
                ) as sync_mock:
                    sync_mock.return_value.tables = ["default.t1"]
                    sync_mock.return_value.deleted = []
                    sync_mock.return_value.existing_skipped = []
                    sync_mock.return_value.updated_existing = []
                    sync_mock.return_value.ingested = ["default.t1"]
                    sync_mock.return_value.ingest_count = 1

                    code = hive_jobs_table_ingest.main()

        self.assertEqual(code, 0)
        self.assertEqual(sync_mock.call_args.kwargs["existing_dataset_action"], "delete")

    def test_main_passes_update_existing_dataset_action(self) -> None:
        with patch.dict(
            "os.environ",
            {"TABLE_NAMES": "default.t1\n", "EXISTING_DATASET_ACTION": "update"},
            clear=False,
        ):
            with patch(
                "sys.argv",
                ["hive_jobs_table_ingest.py", "--dry-run", "--ingest-mode", "full"],
            ):
                with patch(
                    "job_info_sync_datahub.hive_jobs_table_ingest.sync_hive_tables_delete_then_ingest",
                ) as sync_mock:
                    sync_mock.return_value.tables = ["default.t1"]
                    sync_mock.return_value.deleted = []
                    sync_mock.return_value.existing_skipped = []
                    sync_mock.return_value.updated_existing = ["default.t1"]
                    sync_mock.return_value.ingested = ["default.t1"]
                    sync_mock.return_value.ingest_count = 1

                    code = hive_jobs_table_ingest.main()

        self.assertEqual(code, 0)
        self.assertEqual(sync_mock.call_args.kwargs["existing_dataset_action"], "update")

    def test_main_reads_table_names_env(self) -> None:
        with patch.dict("os.environ", {"TABLE_NAMES": "default.t1\nods.t2\n"}, clear=False):
            with patch(
                "sys.argv",
                ["hive_jobs_table_ingest.py", "--dry-run", "--ingest-mode", "minimal"],
            ):
                with patch(
                    "job_info_sync_datahub.hive_jobs_table_ingest.sync_hive_tables_delete_then_ingest",
                ) as sync_mock:
                    sync_mock.return_value.tables = ["default.t1", "ods.t2"]
                    sync_mock.return_value.deleted = []
                    sync_mock.return_value.existing_skipped = []
                    sync_mock.return_value.updated_existing = []
                    sync_mock.return_value.ingested = ["default.t1", "ods.t2"]
                    sync_mock.return_value.ingest_count = 2

                    code = hive_jobs_table_ingest.main()

        self.assertEqual(code, 0)
        refs = sync_mock.call_args.args[0]
        self.assertEqual([r.full_name for r in refs], ["default.t1", "ods.t2"])


if __name__ == "__main__":
    unittest.main()
