"""export_view_datasets_report parsing helpers."""

from __future__ import annotations

import json

from job_info_sync_datahub import export_view_datasets_report as mod


def test_view_logic_and_upstream_parsing_from_mysql_metadata() -> None:
    view_metadata = json.dumps(
        {
            "viewLogic": "SELECT * FROM default.dim_a",
            "viewLanguage": "SQL",
        }
    )
    lineage_metadata = json.dumps(
        {
            "upstreams": [
                {"dataset": "urn:li:dataset:(urn:li:dataPlatform:hive,blf-prod-hive.default.dim_a,PROD)"}
            ]
        }
    )
    structured_metadata = json.dumps(
        {
            "properties": [
                {
                    "propertyUrn": "urn:li:structuredProperty:blf.data.warehouse.data_availability_flag",
                    "values": [{"string": "DDL"}, {"string": "表血缘"}],
                }
            ]
        }
    )

    assert mod.view_logic_from_view_properties_metadata(view_metadata).startswith("SELECT")
    assert mod.upstream_count_from_lineage_metadata(lineage_metadata) == 1
    assert mod.data_availability_flag_from_structured_metadata(structured_metadata) == "DDL, 表血缘"


def test_empty_view_logic_and_missing_structured() -> None:
    assert mod.view_logic_from_view_properties_metadata("{}") == ""
    assert mod.upstream_count_from_lineage_metadata("{}") == 0
    assert mod.data_availability_flag_from_structured_metadata("{}") == "-"
