"""hive_single_table_ingest：单表存在性检查与 ensure 逻辑（mock，不连 HMS/GMS）。"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from job_info_sync_datahub.hive_single_table_ingest import (
    build_single_table_recipe,
    dataset_entity_exists,
    ensure_upstream_dataset_in_datahub,
)
from job_info_sync_datahub.models import TableRef


class TestBuildSingleTableRecipe(unittest.TestCase):
    def test_allow_pattern_matches_one_table(self) -> None:
        r = build_single_table_recipe("default", "static_mid_store_info_hd")
        allow = r["source"]["config"]["table_pattern"]["allow"]
        self.assertEqual(len(allow), 1)
        self.assertIn("static_mid_store_info_hd", allow[0])


class TestEnsureUpstreamDataset(unittest.TestCase):
    def test_skip_when_already_exists(self) -> None:
        ref = TableRef("default", "dw_order_v1")
        with patch(
            "job_info_sync_datahub.hive_single_table_ingest.dataset_entity_exists",
            return_value=True,
        ):
            ingested, msg = ensure_upstream_dataset_in_datahub(
                ref,
                gms_url="http://127.0.0.1:8080",
                dry_run=False,
            )
        self.assertFalse(ingested)
        self.assertIn("已在", msg)

    def test_minimal_register_when_missing(self) -> None:
        ref = TableRef("default", "static_mid_store_info_hd")
        with patch(
            "job_info_sync_datahub.hive_single_table_ingest.dataset_entity_exists",
            side_effect=[False, True],
        ):
            with patch(
                "job_info_sync_datahub.hive_single_table_ingest.register_minimal_hive_dataset",
            ) as reg_mock:
                ingested, msg = ensure_upstream_dataset_in_datahub(
                    ref,
                    gms_url="http://127.0.0.1:8080",
                    dry_run=False,
                )
        self.assertTrue(ingested)
        reg_mock.assert_called_once()
        self.assertIn("轻量注册", msg)

    def test_skip_ingest_env(self) -> None:
        ref = TableRef("default", "x")
        with patch.dict("os.environ", {"BLF_LINEAGE_SKIP_UPSTREAM_INGEST": "1"}):
            with patch(
                "job_info_sync_datahub.hive_single_table_ingest.dataset_entity_exists",
                return_value=False,
            ):
                ingested, _ = ensure_upstream_dataset_in_datahub(
                    ref,
                    gms_url="http://127.0.0.1:8080",
                    skip_ingest=True,
                )
        self.assertFalse(ingested)


class TestDatasetEntityExists(unittest.TestCase):
    def test_404_returns_false(self) -> None:
        import urllib.error

        def fake_urlopen(req, timeout=30):
            raise urllib.error.HTTPError(req.full_url, 404, "nope", None, None)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ok = dataset_entity_exists(
                "http://gms",
                TableRef("default", "missing"),
                "blf-prod-hive",
                "PROD",
            )
        self.assertFalse(ok)

    def test_200_returns_true(self) -> None:
        body = MagicMock()
        body.read.return_value = b"{}"
        ctx = MagicMock()
        ctx.__enter__.return_value = body

        with patch("urllib.request.urlopen", return_value=ctx):
            ok = dataset_entity_exists(
                "http://gms",
                TableRef("default", "t"),
                "blf-prod-hive",
                "PROD",
            )
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
