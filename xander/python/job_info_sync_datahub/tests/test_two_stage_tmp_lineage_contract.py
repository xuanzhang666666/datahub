"""两段 SQL + tmp_* 中间表：LLM 输出契约与 fqtn 过滤的回归用例（不调用 DeepSeek）。"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from job_info_sync_datahub.lineage_llm_compare import SYSTEM_PROMPT
from job_info_sync_datahub.lineage_write_policy import (
    _parse_lineage_array,
    apply_lineage_filters_from_parsed,
)


class TestTwoStageTmpLineageContract(unittest.TestCase):
    def test_system_prompt_documents_tmp_fold_rules(self) -> None:
        for fragment in (
            "tmp_",
            "不得",
            "跨段",
            "最终",
        ):
            self.assertIn(fragment, SYSTEM_PROMPT)

    def test_ideal_folded_llm_json_passes_filters_with_hive_skipped(self) -> None:
        """模拟「先 tmp、再 mid」折叠后的理想 JSON：无 tmp_*，应过 fqtn 且可写入（跳过 Hive 存在性）。"""
        raw: dict = {
            "lineage": [
                {
                    "target": {"db": "default", "table": "mid_store_sku_info_bach_v2"},
                    "upstreams": [
                        {"db": "default", "table": "pdw_bach_baseinfo_product_sku"},
                        {"db": "default", "table": "dim_logistics_sku_supplier"},
                    ],
                }
            ],
            "notes": "example: folded tmp_mid_* per two-stage contract",
        }
        with patch.dict(os.environ, {"BLF_LINEAGE_SKIP_HIVE_EXISTENCE_CHECK": "1"}):
            parsed = _parse_lineage_array(raw)
            kept, decision = apply_lineage_filters_from_parsed(parsed, raw)
        self.assertTrue(decision.write_upstream_lineage)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].target.full_name, "default.mid_store_sku_info_bach_v2")
        up = {u.full_name for u in kept[0].upstreams}
        self.assertEqual(
            up,
            {
                "default.pdw_bach_baseinfo_product_sku",
                "default.dim_logistics_sku_supplier",
            },
        )

    def test_tmp_only_target_yields_skip_invalid_fqtn(self) -> None:
        """若模型仍把 tmp 当 target，过滤后为空 → SKIP_INVALID_FQTN（与 hive_fqtn_validation 一致）。"""
        raw: dict = {
            "lineage": [
                {
                    "target": {"db": "default", "table": "tmp_mid_store_sku_info_bach_v2_main"},
                    "upstreams": [
                        {"db": "default", "table": "pdw_bach_baseinfo_product_sku"},
                    ],
                }
            ],
            "notes": "bad: tmp as target",
        }
        with patch.dict(os.environ, {"BLF_LINEAGE_SKIP_HIVE_EXISTENCE_CHECK": "1"}):
            parsed = _parse_lineage_array(raw)
            _, decision = apply_lineage_filters_from_parsed(parsed, raw)
        self.assertFalse(decision.write_upstream_lineage)
        self.assertEqual(decision.status, "SKIP_INVALID_FQTN")


if __name__ == "__main__":
    unittest.main()
