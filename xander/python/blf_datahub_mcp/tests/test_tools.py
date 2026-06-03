from __future__ import annotations

from typing import Any

from blf_datahub_mcp.summarizers import (
    URN_DATA_AVAILABILITY_FLAG,
    URN_ETL_SCRIPT,
    URN_EXECUTE_SHELL,
    URN_OTHER_REMARK,
    URN_SCHEDULE_URL,
)
from blf_datahub_mcp.tools import (
    get_hive_data_availability_flag,
    get_hive_execute_shell,
    get_hive_structured_properties,
    get_hive_structured_property,
    search_hive_assets,
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
                            "values": [{"string": "DDL"}, {"string": "表血缘"}],
                        },
                        {
                            "propertyUrn": URN_OTHER_REMARK,
                            "values": [{"string": "remark"}],
                        },
                    ]
                }
            }
        }


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
    assert properties["data_availability_flag"]["value_count"] == 2
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
    assert flag["summary"]["value_count"] == 2


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
    assert candidate["availability_flags"] == ["DDL", "表血缘"]
