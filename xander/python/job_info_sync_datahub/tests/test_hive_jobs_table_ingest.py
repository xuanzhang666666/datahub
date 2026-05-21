"""hive_jobs_table_ingest：表名解析与删后 ingest 流程（mock）。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from job_info_sync_datahub import hive_jobs_table_ingest
from job_info_sync_datahub.hive_jobs_table_ingest import (
    parse_table_line,
    parse_table_list_text,
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
    def test_delete_then_ingest(self) -> None:
        refs = [TableRef("default", "t1"), TableRef("ods", "t2")]
        with patch(
            "job_info_sync_datahub.hive_jobs_table_ingest.delete_dataset_entity",
            side_effect=[True, False],
        ) as del_mock:
            with patch(
                "job_info_sync_datahub.hive_jobs_table_ingest.ingest_hive_table_list",
            ) as ingest_mock:
                rep = sync_hive_tables_delete_then_ingest(
                    refs,
                    gms_url="http://127.0.0.1:8080",
                    dry_run=False,
                )
        self.assertEqual(del_mock.call_count, 2)
        ingest_mock.assert_called_once()
        self.assertEqual(rep.deleted, ["default.t1"])
        self.assertEqual(rep.delete_skipped, ["ods.t2"])
        self.assertEqual(rep.ingest_count, 2)

    def test_minimal_mode_passed_through(self) -> None:
        refs = [TableRef("default", "static_x")]
        with patch(
            "job_info_sync_datahub.hive_jobs_table_ingest.delete_dataset_entity",
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
                    sync_mock.return_value.delete_skipped = []
                    sync_mock.return_value.ingest_count = 2

                    code = hive_jobs_table_ingest.main()

        self.assertEqual(code, 0)
        refs = sync_mock.call_args.args[0]
        self.assertEqual([r.full_name for r in refs], ["default.t1", "ods.t2"])


if __name__ == "__main__":
    unittest.main()
