from __future__ import annotations

import pytest

from blf_datahub_mcp.hive import make_hive_dataset_urn, normalize_hive_table


def test_normalize_hive_table_defaults_database() -> None:
    assert normalize_hive_table("dw_order_v1") == "default.dw_order_v1"


def test_make_hive_dataset_urn_uses_blf_defaults() -> None:
    assert make_hive_dataset_urn("dw_order_v1") == (
        "urn:li:dataset:(urn:li:dataPlatform:hive,"
        "blf-prod-hive.default.dw_order_v1,PROD)"
    )


def test_normalize_hive_table_rejects_invalid_input() -> None:
    with pytest.raises(ValueError):
        normalize_hive_table("default.")

