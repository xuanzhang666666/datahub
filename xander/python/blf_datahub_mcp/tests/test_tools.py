from __future__ import annotations

import urllib.parse
from typing import Any

from blf_datahub_mcp.summarizers import (
    URN_DATA_AVAILABILITY_FLAG,
    URN_ETL_SCRIPT,
    URN_EXECUTE_SHELL,
    URN_JOB_CONTENT_XML,
    URN_JOB_EXECUTE_SHELL,
    URN_OTHER_REMARK,
    URN_SCHEDULE_URL,
)
from blf_datahub_mcp.hive import make_hive_dataset_urn
from blf_datahub_mcp.schedule import make_scheduler_datajob_urn
from blf_datahub_mcp.tools import (
    explain_hive_field_lineage,
    explain_schedule_job_context,
    get_hive_data_availability_flag,
    get_hive_execute_shell,
    get_hive_structured_properties,
    get_hive_structured_property,
    get_schedule_job_execute_shell,
    get_schedule_job_profile,
    search_hive_assets,
    search_schedule_jobs,
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


# ---------------------------------------------------------------------------
# Schedule job tools — fake client + tests
# ---------------------------------------------------------------------------

_JOB_DISPLAY_NAME = "dm_app_logistics_work_piece_di_v2"
_JOB_URN = make_scheduler_datajob_urn(_JOB_DISPLAY_NAME)
_UPSTREAM_JOB = "ods_logistics_work_piece_di"
_UPSTREAM_URN = make_scheduler_datajob_urn(_UPSTREAM_JOB)


class FakeDataHubClientSchedule:
    """Minimal fake for scheduler DataJob tools."""

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return {
            "searchAcrossEntities": {
                "total": 1,
                "searchResults": [
                    {
                        "entity": {
                            "urn": _JOB_URN,
                            "type": "DATA_JOB",
                            "properties": {
                                "name": _JOB_DISPLAY_NAME,
                                "description": "物流工件日增量",
                                "customProperties": [
                                    {"key": "job_display_name", "value": _JOB_DISPLAY_NAME},
                                    {"key": "trigger_type", "value": "timer"},
                                    {"key": "job_owner_name", "value": "张三"},
                                ],
                            },
                        }
                    }
                ],
            },
            "searchAcrossLineage": {
                "total": 1,
                "searchResults": [
                    {
                        "degree": 1,
                        "entity": {
                            "urn": _UPSTREAM_URN,
                            "type": "DATA_JOB",
                            "properties": {"name": _UPSTREAM_JOB, "description": ""},
                        },
                    }
                ],
            },
        }

    def get_datajob_aspects(
        self, datajob_urn: str, aspects: list[str]
    ) -> dict[str, Any]:
        return {
            "dataJobInfo": {
                "value": {
                    "name": _JOB_DISPLAY_NAME,
                    "type": "BLF_SCHEDULE_JOB",
                    "description": "物流工件日增量",
                    "customProperties": [
                        {"key": "job_display_name", "value": _JOB_DISPLAY_NAME},
                        {"key": "trigger_type", "value": "timer"},
                        {"key": "cron_schedule", "value": "0 3 * * *"},
                        {"key": "job_owner_name", "value": "张三"},
                        {"key": "line_business_code", "value": "logistics"},
                        {"key": "job_disable", "value": "false"},
                    ],
                }
            },
            "dataJobInputOutput": {
                "value": {
                    "inputDatajobEdges": [
                        {
                            "destinationUrn": _UPSTREAM_URN,
                            "properties": [
                                {
                                    "key": "blf_schedule_dependency_condition",
                                    "value": "ALL_DONE",
                                },
                                {
                                    "key": "blf_schedule_dependency_status",
                                    "value": "SUCCESS",
                                },
                            ],
                        }
                    ]
                }
            },
        }

    def get_datajob_structured_properties(self, datajob_urn: str) -> dict[str, Any]:
        return {
            "structuredProperties": {
                "value": {
                    "properties": [
                        {
                            "propertyUrn": URN_JOB_EXECUTE_SHELL,
                            "values": [{"string": "sh /data/etl/logistics_work_piece.sh"}],
                        },
                        {
                            "propertyUrn": URN_JOB_CONTENT_XML,
                            "values": [{"string": "<project><builders/></project>"}],
                        },
                    ]
                }
            }
        }

    def get_structured_properties(self, dataset_urn: str) -> dict[str, Any]:
        return {}


def test_get_schedule_job_profile_returns_expected_fields() -> None:
    result = get_schedule_job_profile(
        FakeDataHubClientSchedule(),
        public_base_url="http://datahub",
        job_display_name=_JOB_DISPLAY_NAME,
    )

    assert result["success"] is True
    assert result["job_display_name"] == _JOB_DISPLAY_NAME
    assert "/tasks/" in result["datahub_url"]
    assert result["schedule_url"].endswith(_JOB_DISPLAY_NAME)
    summary = result["summary"]
    assert summary["trigger_type"] == "timer"
    assert summary["cron_schedule"] == "0 3 * * *"
    assert summary["job_owner_name"] == "张三"
    assert summary["line_business_code"] == "logistics"
    assert summary["dependency_count"] == 1
    deps = summary["dependencies"]
    assert deps[0]["upstream_job"] == _UPSTREAM_JOB
    assert deps[0]["condition"] == "ALL_DONE"
    assert deps[0]["status"] == "SUCCESS"
    assert result["risks"] == []


def test_get_schedule_job_execute_shell_returns_shell_content() -> None:
    result = get_schedule_job_execute_shell(
        FakeDataHubClientSchedule(),
        public_base_url="http://datahub",
        job_display_name=_JOB_DISPLAY_NAME,
    )

    assert result["success"] is True
    assert result["summary"]["exists"] is True
    assert "logistics_work_piece.sh" in result["summary"]["first_value"]["text"]
    assert result["risks"] == []


def test_search_schedule_jobs_returns_candidates() -> None:
    result = search_schedule_jobs(
        FakeDataHubClientSchedule(),
        public_base_url="http://datahub",
        query="logistics",
        limit=5,
    )

    assert result["success"] is True
    assert result["summary"]["total"] == 1
    candidates = result["summary"]["candidates"]
    assert len(candidates) == 1
    assert candidates[0]["job_display_name"] == _JOB_DISPLAY_NAME
    assert candidates[0]["trigger_type"] == "timer"


def test_explain_schedule_job_context_bundles_profile_shell_lineage() -> None:
    result = explain_schedule_job_context(
        FakeDataHubClientSchedule(),
        public_base_url="http://datahub",
        job_display_name=_JOB_DISPLAY_NAME,
    )

    assert result["success"] is True
    assert result["job_display_name"] == _JOB_DISPLAY_NAME
    summary = result["summary"]
    assert summary["profile"] is not None
    assert summary["execute_shell"] is not None
    assert summary["lineage"] is not None
