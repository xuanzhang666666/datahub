"""resolve_hive_ingest_mode 与 minimal 批量注册。"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from job_info_sync_datahub.hive_single_table_ingest import (
    ingest_hive_table_list,
    resolve_hive_ingest_mode,
)
from job_info_sync_datahub.models import TableRef


class TestResolveHiveIngestMode(unittest.TestCase):
    def test_explicit_arg(self) -> None:
        self.assertEqual(resolve_hive_ingest_mode("minimal"), "minimal")

    def test_env_mode(self) -> None:
        with patch.dict(os.environ, {"BLF_HIVE_INGEST_MODE": "minimal"}, clear=False):
            self.assertEqual(resolve_hive_ingest_mode(), "minimal")

    def test_default_full(self) -> None:
        env = os.environ.copy()
        for k in (
            "BLF_HIVE_INGEST_MODE",
            "BLF_HIVE_INGEST_MINIMAL",
            "BLF_HIVE_INGEST_FULL",
        ):
            env.pop(k, None)
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(resolve_hive_ingest_mode(), "full")


class TestIngestHiveTableListMinimal(unittest.TestCase):
    def test_minimal_skips_datahub_cli(self) -> None:
        ref = TableRef("default", "t1")
        with patch(
            "job_info_sync_datahub.hive_single_table_ingest.register_minimal_hive_dataset",
        ) as reg_mock:
            with patch(
                "job_info_sync_datahub.hive_single_table_ingest._run_datahub_ingest_recipe",
            ) as cli_mock:
                ingest_hive_table_list(
                    [ref],
                    gms_url="http://127.0.0.1:8080",
                    ingest_mode="minimal",
                )
        reg_mock.assert_called_once()
        cli_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
