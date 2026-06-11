from __future__ import annotations

import urllib.parse
from typing import Any

from blf_datahub_mcp.summarizers import (
    URN_DATA_AVAILABILITY_FLAG,
    URN_ETL_SCRIPT,
    URN_EXECUTE_SHELL,
    URN_OTHER_REMARK,
    URN_SCHEDULE_URL,
)
from blf_datahub_mcp.hive import make_hive_dataset_urn
from blf_datahub_mcp.tools import (
    explain_hive_field_lineage,
    get_hive_data_availability_flag,
    get_hive_execute_shell,
    get_hive_structured_properties,
    get_hive_structured_property,
    search_hive_assets,
)


def _field_urn(table: str, field: str) -> str:
    return (
        "urn:li:schemaField:("
        f"{make_hive_dataset_urn(table)},{urllib.parse.quote(field, safe='')})"
    )


class FakeDataHubClient:
    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return {
            "searchAcrossEntities": {
                "total": 1,
                "searchResults": [
                    {
                        "entity": {
                            "urn": "urn:li:dataset:(urn:li:dataPlatform:hive,blf-prod-hive.default.dw_order_v1,PROD)",
                            "type": "DATASET",
                            "name": "dw_order_v1",
                            "properties": {"description": "订单表"},
                        }
                    }
                ],
            }
        }

    def get_schema_metadata(self, dataset_urn: str) -> dict[str, Any]:
        table = self._table_from_urn(dataset_urn)
        fields = {
            "dw_order_v1": ["sku_id", "dt"],
            "dim_sku": ["id"],
            "ods_sku": ["id"],
        }[table]
        return {
            "schemaMetadata": {
                "value": {
                    "fields": [
                        {
                            "fieldPath": field,
                            "isPartitioningKey": field == "dt",
                        }
                        for field in fields
                    ]
                }
            }
        }

    def get_upstream_lineage(self, dataset_urn: str) -> dict[str, Any]:
        table = self._table_from_urn(dataset_urn)
        payloads = {
            "dw_order_v1": {
                "upstreams": [{"dataset": make_hive_dataset_urn("default.dim_sku")}],
                "fineGrainedLineages": [
                    {
                        "upstreams": [_field_urn("default.dim_sku", "id")],
                        "downstreams": [_field_urn("default.dw_order_v1", "sku_id")],
                        "transformOperation": "cast(id as string)",
                    }
                ],
            },
            "dim_sku": {
                "upstreams": [{"dataset": make_hive_dataset_urn("default.ods_sku")}],
                "fineGrainedLineages": [
                    {
                        "upstreams": [_field_urn("default.ods_sku", "id")],
                        "downstreams": [_field_urn("default.dim_sku", "id")],
                    }
                ],
            },
            "ods_sku": {"upstreams": [], "fineGrainedLineages": []},
        }
        return {"upstreamLineage": {"value": payloads[table]}}

    def get_structured_properties(self, dataset_urn: str) -> dict[str, Any]:
        return {
            "structuredProperties": {
                "value": {
                    "properties": [
                        {
                            "propertyUrn": URN_ETL_SCRIPT,
                            "values": [{"string": "```sql\nselect 1\n```"}],
                        },
                        {
                            "propertyUrn": URN_EXECUTE_SHELL,
                            "values": [{"string": "```shell\nsh run.sh\n```"}],
                        },
                        {
                            "propertyUrn": URN_SCHEDULE_URL,
                            "values": [{"string": "https://schedule/job/a"}],
                        },
                        {
                            "propertyUrn": URN_DATA_AVAILABILITY_FLAG,
                            "values": [
                                {"string": "DDL"},
                                {"string": "表血缘"},
                                {"string": "字段血缘"},
                            ],
                        },
                        {
                            "propertyUrn": URN_OTHER_REMARK,
                            "values": [{"string": "remark"}],
                        },
                    ]
                }
            }
        }

    @staticmethod
    def _table_from_urn(dataset_urn: str) -> str:
        return dataset_urn.split("blf-prod-hive.default.", 1)[1].split(",PROD", 1)[0]


def test_get_hive_structured_properties_returns_all_known_properties() -> None:
    result = get_hive_structured_properties(
        FakeDataHubClient(),
        public_base_url="http://datahub",
        table="dw_order_v1",
    )

    assert result["success"] is True
    assert result["table"] == "default.dw_order_v1"
    properties = result["summary"]["properties"]
    assert properties["etl_script"]["first_value"]["text"] == "select 1"
    assert properties["execute_shell"]["first_value"]["text"] == "sh run.sh"
    assert properties["schedule_url"]["exists"] is True
    assert properties["data_availability_flag"]["value_count"] == 3
    assert properties["other_remark"]["first_value"]["text"] == "remark"


def test_get_hive_structured_property_accepts_specific_property_name() -> None:
    result = get_hive_structured_property(
        FakeDataHubClient(),
        public_base_url="http://datahub",
        table="default.dw_order_v1",
        property_name="schedule_url",
    )

    assert result["success"] is True
    assert result["summary"]["property_name"] == "schedule_url"
    assert result["summary"]["first_value"]["text"] == "https://schedule/job/a"


def test_dedicated_structured_property_tools_delegate_to_generic_getter() -> None:
    shell = get_hive_execute_shell(
        FakeDataHubClient(),
        public_base_url="http://datahub",
        table="dw_order_v1",
    )
    flag = get_hive_data_availability_flag(
        FakeDataHubClient(),
        public_base_url="http://datahub",
        table="dw_order_v1",
    )

    assert shell["summary"]["property_name"] == "execute_shell"
    assert shell["summary"]["first_value"]["text"] == "sh run.sh"
    assert flag["summary"]["property_name"] == "data_availability_flag"
    assert flag["summary"]["value_count"] == 3


def test_search_hive_assets_fetches_availability_flags_without_filtering() -> None:
    result = search_hive_assets(
        FakeDataHubClient(),
        public_base_url="http://datahub",
        query="订单",
        only_available=False,
        limit=10,
    )

    assert result["success"] is True
    candidate = result["summary"]["candidates"][0]
    assert candidate["name"] == "dw_order_v1"
    assert candidate["availability_flags"] == ["DDL", "表血缘", "字段血缘"]


def test_explain_hive_field_lineage_returns_structured_trace() -> None:
    result = explain_hive_field_lineage(
        FakeDataHubClient(),
        public_base_url="http://datahub",
        table="dw_order_v1",
        fields=["sku_id"],
    )

    assert result["success"] is True
    assert result["table"] == "default.dw_order_v1"
    summary = result["summary"]
    assert summary["max_depth"] == 30
    assert summary["max_paths"] == 1000
    assert summary["stop_reasons"] == {"SOURCE_LAYER_REACHED": 1}
    assert summary["paths"][0]["nodes"] == [
        "default.dw_order_v1.sku_id",
        "default.dim_sku.id",
        "default.ods_sku.id",
    ]
    assert summary["paths"][0]["source_layer"] == "ods"
    assert result["risks"] == []
