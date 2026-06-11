from __future__ import annotations

import urllib.parse
from typing import Any

import pytest

from blf_datahub_mcp.field_lineage import trace_hive_field_lineage
from blf_datahub_mcp.hive import make_hive_dataset_urn
from blf_datahub_mcp.summarizers import URN_DATA_AVAILABILITY_FLAG


def _field_urn(table: str, field: str) -> str:
    return (
        "urn:li:schemaField:("
        f"{make_hive_dataset_urn(table)},{urllib.parse.quote(field, safe='')})"
    )


def _schema(fields: list[str], partitions: list[str] | None = None) -> dict[str, Any]:
    partition_set = set(partitions or [])
    return {
        "schemaMetadata": {
            "value": {
                "fields": [
                    {
                        "fieldPath": field,
                        "description": f"{field} description",
                        "isPartitioningKey": field in partition_set,
                    }
                    for field in fields
                ]
            }
        }
    }


def _lineage(
    *,
    upstreams: list[str] | None = None,
    fine_grained: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "upstreamLineage": {
            "value": {
                "upstreams": [
                    {"dataset": make_hive_dataset_urn(table)}
                    for table in (upstreams or [])
                ],
                "fineGrainedLineages": fine_grained or [],
            }
        }
    }


def _flags(*values: str) -> dict[str, Any]:
    return {
        "structuredProperties": {
            "value": {
                "properties": [
                    {
                        "propertyUrn": URN_DATA_AVAILABILITY_FLAG,
                        "values": [{"string": value} for value in values],
                    }
                ]
            }
        }
    }


class FakeDataHubClient:
    def __init__(self, payloads: dict[str, dict[str, dict[str, Any]]]) -> None:
        self.payloads = payloads

    def get_schema_metadata(self, dataset_urn: str) -> dict[str, Any]:
        return self._payload(dataset_urn, "schemaMetadata")

    def get_upstream_lineage(self, dataset_urn: str) -> dict[str, Any]:
        return self._payload(dataset_urn, "upstreamLineage")

    def get_structured_properties(self, dataset_urn: str) -> dict[str, Any]:
        return self._payload(dataset_urn, "structuredProperties")

    def _payload(self, dataset_urn: str, aspect: str) -> dict[str, Any]:
        table = dataset_urn.split("blf-prod-hive.", 1)[1].split(",PROD", 1)[0]
        return self.payloads[table][aspect]


def test_trace_hive_field_lineage_stops_at_ods_and_pdw_tables() -> None:
    client = FakeDataHubClient(
        {
            "dw.target": {
                "schemaMetadata": _schema(["sku_id", "store_id", "dt"], ["dt"]),
                "structuredProperties": _flags("字段血缘"),
                "upstreamLineage": _lineage(
                    upstreams=["dim.sku", "dim.store"],
                    fine_grained=[
                        {
                            "upstreams": [_field_urn("dim.sku", "id")],
                            "downstreams": [_field_urn("dw.target", "sku_id")],
                            "transformOperation": "cast(id as string)",
                        },
                        {
                            "upstreams": [_field_urn("dim.store", "id")],
                            "downstreams": [_field_urn("dw.target", "store_id")],
                            "transformOperation": "store id",
                        },
                    ],
                ),
            },
            "dim.sku": {
                "schemaMetadata": _schema(["id"]),
                "structuredProperties": _flags("字段血缘"),
                "upstreamLineage": _lineage(
                    upstreams=["ods_sku"],
                    fine_grained=[
                        {
                            "upstreams": [_field_urn("default.ods_sku", "id")],
                            "downstreams": [_field_urn("dim.sku", "id")],
                            "transformOperation": "id",
                        }
                    ],
                ),
            },
            "dim.store": {
                "schemaMetadata": _schema(["id"]),
                "structuredProperties": _flags("字段血缘"),
                "upstreamLineage": _lineage(
                    upstreams=["pdw_store"],
                    fine_grained=[
                        {
                            "upstreams": [_field_urn("default.pdw_store", "id")],
                            "downstreams": [_field_urn("dim.store", "id")],
                            "transformOperation": "id",
                        }
                    ],
                ),
            },
            "default.ods_sku": {
                "schemaMetadata": _schema(["id"]),
                "structuredProperties": _flags("字段血缘"),
                "upstreamLineage": _lineage(upstreams=["raw.sku"]),
            },
            "default.pdw_store": {
                "schemaMetadata": _schema(["id"]),
                "structuredProperties": _flags("字段血缘"),
                "upstreamLineage": _lineage(upstreams=["raw.store"]),
            },
        }
    )

    result = trace_hive_field_lineage(client, table="dw.target")

    assert result["field_count"] == 2
    assert result["path_count"] == 2
    assert result["stop_reasons"] == {"SOURCE_LAYER_REACHED": 2}
    paths_by_field = {path["target_field"]: path for path in result["paths"]}
    assert paths_by_field["sku_id"]["nodes"] == [
        "dw.target.sku_id",
        "dim.sku.id",
        "default.ods_sku.id",
    ]
    assert paths_by_field["sku_id"]["source_layer"] == "ods"
    assert paths_by_field["store_id"]["nodes"][-1] == "default.pdw_store.id"
    assert paths_by_field["store_id"]["source_layer"] == "pdw"


def test_trace_hive_field_lineage_reports_missing_field_lineage_before_source_layer() -> None:
    client = FakeDataHubClient(
        {
            "dw.target": {
                "schemaMetadata": _schema(["amount"]),
                "structuredProperties": _flags("字段血缘"),
                "upstreamLineage": _lineage(upstreams=["dwd.order"]),
            }
        }
    )

    result = trace_hive_field_lineage(client, table="dw.target")

    assert result["stop_reasons"] == {"MISSING_FIELD_LINEAGE": 1}
    assert result["paths"][0]["stop_reason"] == "MISSING_FIELD_LINEAGE"
    assert "dw.target.amount" in result["open_questions"][0]


def test_trace_hive_field_lineage_parses_openapi_v2_field_paths() -> None:
    client = FakeDataHubClient(
        {
            "data_sec_dw.dim_store_info": {
                "schemaMetadata": {
                    "value": {
                        "fields": [
                            {
                                "fieldPath": "[version=2.0].[type=string].store_code",
                                "description": "门店code",
                            }
                        ]
                    }
                },
                "structuredProperties": _flags("字段血缘"),
                "upstreamLineage": _lineage(
                    upstreams=["default.ods_store_info"],
                    fine_grained=[
                        {
                            "upstreams": [_field_urn("default.ods_store_info", "store_code")],
                            "downstreams": [
                                _field_urn("data_sec_dw.dim_store_info", "store_code")
                            ],
                        }
                    ],
                ),
            },
            "default.ods_store_info": {
                "schemaMetadata": _schema(["store_code"]),
                "structuredProperties": _flags("字段血缘"),
                "upstreamLineage": _lineage(),
            },
        }
    )

    result = trace_hive_field_lineage(
        client,
        table="data_sec_dw.dim_store_info",
        fields=["store_code"],
    )

    assert result["fields"][0]["target_field"] == "store_code"
    assert result["paths"][0]["nodes"][-1].endswith(".store_code")


def test_trace_hive_field_lineage_filters_requested_fields_and_rejects_unknown() -> None:
    client = FakeDataHubClient(
        {
            "dw.target": {
                "schemaMetadata": _schema(["id", "name"]),
                "structuredProperties": _flags("字段血缘"),
                "upstreamLineage": _lineage(
                    upstreams=["ods_user"],
                    fine_grained=[
                        {
                            "upstreams": [_field_urn("default.ods_user", "id")],
                            "downstreams": [_field_urn("dw.target", "id")],
                        }
                    ],
                ),
            },
            "default.ods_user": {
                "schemaMetadata": _schema(["id"]),
                "structuredProperties": _flags("字段血缘"),
                "upstreamLineage": _lineage(),
            },
        }
    )

    result = trace_hive_field_lineage(client, table="dw.target", fields=["id"])

    assert [field["target_field"] for field in result["fields"]] == ["id"]
    with pytest.raises(ValueError, match="unknown fields"):
        trace_hive_field_lineage(client, table="dw.target", fields=["missing"])
