"""BLF DataHub Hive MCP tool implementations."""

from __future__ import annotations

from typing import Any, Literal

from .datahub_client import DataHubClient, DataHubClientError
from .hive import make_datahub_dataset_url, make_hive_dataset_urn, normalize_hive_table
from .summarizers import (
    aspect_value,
    extract_structured_properties,
    extract_table_refs,
    governance_gaps,
    summarize_schema_fields,
    truncate_text,
)

PROFILE_ASPECTS = [
    "datasetProperties",
    "editableDatasetProperties",
    "schemaMetadata",
    "editableSchemaMetadata",
    "ownership",
    "institutionalMemory",
    "globalTags",
    "glossaryTerms",
    "domains",
    "structuredProperties",
    "status",
]

DATASET_PROFILE_QUERY = """
query DatasetProfile($urn: String!) {
  dataset(urn: $urn) {
    urn
    name
    exists
    properties { name description qualifiedName customProperties { key value } }
    editableProperties { description }
    ownership { owners { type owner { urn } } }
    tags { tags { tag { urn name } } }
    glossaryTerms { terms { term { urn name } } }
    domain { domain { urn properties { name description } } }
    schemaMetadata {
      name
      version
      fields { fieldPath nativeDataType type description nullable }
    }
  }
}
"""

LINEAGE_QUERY = """
query SearchLineage($input: SearchAcrossLineageInput!) {
  searchAcrossLineage(input: $input) {
    total
    searchResults {
      degree
      entity {
        urn
        type
        ... on Dataset {
          name
          properties { name description qualifiedName }
          platform { name }
        }
        ... on DataJob {
          properties { name description }
        }
        ... on DataFlow {
          properties { name description }
        }
      }
    }
  }
}
"""

SEARCH_QUERY = """
query SearchHiveDatasets($input: SearchAcrossEntitiesInput!) {
  searchAcrossEntities(input: $input) {
    total
    searchResults {
      entity {
        urn
        type
        ... on Dataset {
          name
          properties { name description qualifiedName }
          schemaMetadata { fields { fieldPath description nativeDataType } }
        }
      }
    }
  }
}
"""


def _base(table: str, public_base_url: str) -> dict[str, str]:
    normalized = normalize_hive_table(table)
    urn = make_hive_dataset_urn(normalized)
    return {
        "table": normalized,
        "dataset_urn": urn,
        "datahub_url": make_datahub_dataset_url(urn, public_base_url),
    }


def _error_response(error: Exception, **extra: Any) -> dict[str, Any]:
    error_type = "datahub_error"
    if isinstance(error, DataHubClientError):
        if error.status_code == 401:
            error_type = "unauthorized"
        elif error.status_code == 403:
            error_type = "forbidden"
        elif error.status_code == 404:
            error_type = "not_found"
    elif isinstance(error, ValueError):
        error_type = "invalid_input"
    return {
        "success": False,
        "error_type": error_type,
        "message": str(error),
        **extra,
    }


def get_hive_table_profile(
    client: DataHubClient,
    *,
    public_base_url: str,
    table: str,
    include_fields: bool = True,
    field_limit: int = 80,
) -> dict[str, Any]:
    """Return a compact DataHub profile for one BLF Hive table."""
    try:
        base = _base(table, public_base_url)
        aspects = client.get_dataset_aspects(base["dataset_urn"], PROFILE_ASPECTS)
        graph_profile = _fetch_dataset_graphql_profile(client, base["dataset_urn"])
        structured = extract_structured_properties(
            aspect_value(aspects, "structuredProperties")
        )
        schema = graph_profile.get("schemaMetadata") or aspect_value(
            aspects, "schemaMetadata"
        )
        dataset_props = graph_profile.get("properties") or aspect_value(
            aspects, "datasetProperties"
        )
        editable_props = graph_profile.get("editableProperties") or aspect_value(
            aspects, "editableDatasetProperties"
        )
        description = (
            editable_props.get("description")
            or dataset_props.get("description")
            or ""
        )
        owners = (graph_profile.get("ownership") or {}).get("owners") or []
        tags = (graph_profile.get("tags") or {}).get("tags") or []
        terms = (graph_profile.get("glossaryTerms") or {}).get("terms") or []
        domain = (graph_profile.get("domain") or {}).get("domain")
        ownership_aspect = aspect_value(aspects, "ownership")
        global_tags_aspect = aspect_value(aspects, "globalTags")
        glossary_aspect = aspect_value(aspects, "glossaryTerms")
        domains_aspect = aspect_value(aspects, "domains")
        if not owners and isinstance(ownership_aspect.get("owners"), list):
            owners = ownership_aspect["owners"]
        if not tags and isinstance(global_tags_aspect.get("tags"), list):
            tags = global_tags_aspect["tags"]
        if not terms and isinstance(glossary_aspect.get("terms"), list):
            terms = glossary_aspect["terms"]
        if domain is None and isinstance(domains_aspect.get("domains"), list):
            domain = domains_aspect["domains"][0] if domains_aspect["domains"] else None
        fields = summarize_schema_fields(schema, field_limit) if include_fields else None
        gaps = governance_gaps(
            has_schema=bool(schema.get("fields")),
            has_documentation=bool(description),
            has_etl_script=bool(structured["etl_script"]),
            has_execute_shell=bool(structured["execute_shell"]),
            has_owner=bool(owners),
            has_tags=bool(tags or terms or domain),
            availability_flags=structured["data_availability_flags"],
        )
        return {
            "success": True,
            **base,
            "summary": {
                "name": graph_profile.get("name") or dataset_props.get("name"),
                "description": truncate_text(description, 1200),
                "availability_flags": structured["data_availability_flags"],
                "owner_count": len(owners),
                "tag_count": len(tags),
                "glossary_term_count": len(terms),
                "domain": domain,
                "schedule_url": structured["schedule_url"],
            },
            "fields": fields,
            "structured_properties": {
                "available_property_urns": structured["raw_property_urns"],
                "has_etl_script": bool(structured["etl_script"]),
                "has_execute_shell": bool(structured["execute_shell"]),
            },
            "risks": gaps,
            "evidence": {
                "interfaces": ["GraphQL dataset", "OpenAPI dataset aspects"],
                "aspects": PROFILE_ASPECTS,
            },
        }
    except Exception as exc:
        try:
            base = _base(table, public_base_url)
        except Exception:
            base = {"table": table}
        return _error_response(exc, **base)


def get_hive_etl_context(
    client: DataHubClient,
    *,
    public_base_url: str,
    table: str,
    max_script_chars: int = 8000,
) -> dict[str, Any]:
    """Return ETL structured property context for one Hive table."""
    try:
        base = _base(table, public_base_url)
        payload = client.get_structured_properties(base["dataset_urn"])
        structured = extract_structured_properties(payload)
        combined_text = "\n".join(
            [structured["execute_shell"], structured["etl_script"]]
        )
        refs = extract_table_refs(combined_text)
        risks = []
        if not structured["execute_shell"]:
            risks.append("缺少 Execute Shell structured property")
        if not structured["etl_script"]:
            risks.append("缺少 Etl Script structured property")
        if "${" in combined_text or "$" in combined_text:
            risks.append("脚本包含动态变量，MCP 未展开运行时取值")
        return {
            "success": True,
            **base,
            "summary": {
                "execute_shell": truncate_text(
                    structured["execute_shell"], max_script_chars
                ),
                "etl_script": truncate_text(structured["etl_script"], max_script_chars),
                "visible_table_refs": refs,
                "schedule_url": structured["schedule_url"],
            },
            "risks": risks,
            "evidence": {
                "interface": "OpenAPI structuredProperties",
                "property_urns": [
                    "urn:li:structuredProperty:blf.data.schedule.execute_shell",
                    "urn:li:structuredProperty:blf.data.warehouse.etl_script",
                ],
            },
        }
    except Exception as exc:
        try:
            base = _base(table, public_base_url)
        except Exception:
            base = {"table": table}
        return _error_response(exc, **base)


def get_hive_lineage(
    client: DataHubClient,
    *,
    public_base_url: str,
    table: str,
    direction: Literal["upstream", "downstream", "both"] = "both",
    max_hops: int = 1,
    max_results: int = 50,
) -> dict[str, Any]:
    """Return table-level DataHub lineage for one Hive table."""
    try:
        base = _base(table, public_base_url)
        if direction not in {"upstream", "downstream", "both"}:
            raise ValueError("direction must be upstream, downstream, or both")
        max_hops = min(max(int(max_hops), 1), 3)
        max_results = min(max(int(max_results), 1), 100)
        result: dict[str, Any] = {}
        totals: dict[str, int] = {}
        for label, gql_direction in _lineage_directions(direction):
            payload = _fetch_lineage(
                client,
                base["dataset_urn"],
                gql_direction,
                max_hops,
                max_results,
            )
            entries = _summarize_lineage_results(payload, public_base_url)
            result[label] = entries
            totals[label] = int(payload.get("total") or 0)
        has_any = any(totals.values())
        return {
            "success": True,
            **base,
            "summary": {
                "direction": direction,
                "max_hops": max_hops,
                "max_results": max_results,
                "totals": totals,
                "lineage": result,
            },
            "risks": [] if has_any else ["DataHub 表级血缘为空或未摄入"],
            "evidence": {
                "interface": "GraphQL searchAcrossLineage",
                "note": "这是表级血缘，不代表字段级影响面",
            },
        }
    except Exception as exc:
        try:
            base = _base(table, public_base_url)
        except Exception:
            base = {"table": table}
        return _error_response(exc, **base)


def search_hive_assets(
    client: DataHubClient,
    *,
    public_base_url: str,
    query: str,
    only_available: bool = True,
    limit: int = 10,
) -> dict[str, Any]:
    """Search Hive datasets and return compact recommendations."""
    try:
        limit = min(max(int(limit), 1), 30)
        gql_query = query if query.startswith("/q") else f"/q {query}"
        variables = {
            "input": {
                "query": gql_query,
                "start": 0,
                "count": limit,
                "types": ["DATASET"],
                "orFilters": [
                    {
                        "and": [
                            {
                                "field": "platform",
                                "condition": "EQUAL",
                                "values": ["urn:li:dataPlatform:hive"],
                            }
                        ]
                    }
                ],
            }
        }
        data = client.graphql(SEARCH_QUERY, variables)
        response = data.get("searchAcrossEntities") or {}
        candidates = []
        for item in response.get("searchResults") or []:
            entity = item.get("entity") or {}
            urn = entity.get("urn") or ""
            flags: list[str] = []
            if only_available and urn:
                try:
                    structured = extract_structured_properties(
                        client.get_structured_properties(urn)
                    )
                    flags = structured["data_availability_flags"]
                    if not flags:
                        continue
                except Exception:
                    continue
            candidates.append(
                {
                    "urn": urn,
                    "name": entity.get("name"),
                    "description": truncate_text(
                        ((entity.get("properties") or {}).get("description") or ""),
                        500,
                    ),
                    "availability_flags": flags,
                    "datahub_url": make_datahub_dataset_url(urn, public_base_url)
                    if urn
                    else "",
                }
            )
        return {
            "success": True,
            "query": query,
            "summary": {
                "total": response.get("total"),
                "returned": len(candidates),
                "only_available": only_available,
                "candidates": candidates,
            },
            "risks": []
            if candidates
            else ["未找到候选 Hive 表，或 only_available 过滤后无结果"],
            "evidence": {"interface": "GraphQL searchAcrossEntities"},
        }
    except Exception as exc:
        return _error_response(exc, query=query)


def audit_hive_table(
    client: DataHubClient,
    *,
    public_base_url: str,
    table: str,
) -> dict[str, Any]:
    """Audit metadata completeness for one Hive table."""
    profile = get_hive_table_profile(
        client,
        public_base_url=public_base_url,
        table=table,
        include_fields=False,
    )
    lineage = get_hive_lineage(
        client,
        public_base_url=public_base_url,
        table=table,
        direction="both",
        max_hops=1,
        max_results=1,
    )
    if not profile.get("success"):
        return profile
    totals = ((lineage.get("summary") or {}).get("totals") or {}) if lineage else {}
    risks = list(profile.get("risks") or [])
    if not any(totals.values()):
        risks.append("上下游血缘均为空")
    return {
        "success": True,
        "table": profile["table"],
        "dataset_urn": profile["dataset_urn"],
        "datahub_url": profile["datahub_url"],
        "summary": {
            "checks": {
                "schema": not any("schemaMetadata" in risk for risk in risks),
                "documentation": not any("表文档" in risk for risk in risks),
                "etl_script": profile["structured_properties"]["has_etl_script"],
                "execute_shell": profile["structured_properties"]["has_execute_shell"],
                "lineage": any(totals.values()),
                "owner": not any("owner" in risk for risk in risks),
                "governance_tags": not any("治理标记" in risk for risk in risks),
                "availability_flags": profile["summary"]["availability_flags"],
            }
        },
        "risks": risks,
        "evidence": {"tools": ["blf_get_hive_table_profile", "blf_get_hive_lineage"]},
    }


def explain_hive_table_context(
    client: DataHubClient,
    *,
    public_base_url: str,
    table: str,
) -> dict[str, Any]:
    """Return one compact context bundle for an agent answer."""
    profile = get_hive_table_profile(
        client,
        public_base_url=public_base_url,
        table=table,
        include_fields=True,
        field_limit=30,
    )
    etl = get_hive_etl_context(
        client,
        public_base_url=public_base_url,
        table=table,
        max_script_chars=3000,
    )
    lineage = get_hive_lineage(
        client,
        public_base_url=public_base_url,
        table=table,
        direction="both",
        max_hops=1,
        max_results=20,
    )
    if not profile.get("success"):
        return profile
    risks = []
    for payload in (profile, etl, lineage):
        risks.extend(payload.get("risks") or [])
    return {
        "success": True,
        "table": profile["table"],
        "dataset_urn": profile["dataset_urn"],
        "datahub_url": profile["datahub_url"],
        "summary": {
            "profile": profile.get("summary"),
            "fields": profile.get("fields"),
            "etl": etl.get("summary") if etl.get("success") else None,
            "lineage": lineage.get("summary") if lineage.get("success") else None,
        },
        "risks": sorted(set(risks)),
        "evidence": {
            "tools": [
                "blf_get_hive_table_profile",
                "blf_get_hive_etl_context",
                "blf_get_hive_lineage",
            ]
        },
    }


def _fetch_dataset_graphql_profile(
    client: DataHubClient,
    dataset_urn: str,
) -> dict[str, Any]:
    try:
        data = client.graphql(DATASET_PROFILE_QUERY, {"urn": dataset_urn})
    except Exception:
        return {}
    dataset = data.get("dataset")
    return dataset if isinstance(dataset, dict) else {}


def _lineage_directions(direction: str) -> list[tuple[str, str]]:
    if direction == "upstream":
        return [("upstreams", "UPSTREAM")]
    if direction == "downstream":
        return [("downstreams", "DOWNSTREAM")]
    return [("upstreams", "UPSTREAM"), ("downstreams", "DOWNSTREAM")]


def _degree_values(max_hops: int) -> list[str]:
    if max_hops <= 1:
        return ["1"]
    if max_hops == 2:
        return ["1", "2"]
    return ["1", "2", "3+"]


def _fetch_lineage(
    client: DataHubClient,
    urn: str,
    direction: str,
    max_hops: int,
    max_results: int,
) -> dict[str, Any]:
    variables = {
        "input": {
            "urn": urn,
            "direction": direction,
            "query": "*",
            "start": 0,
            "count": max_results,
            "types": ["DATASET", "DATA_JOB", "DATA_FLOW"],
            "orFilters": [
                {
                    "and": [
                        {
                            "field": "degree",
                            "condition": "EQUAL",
                            "values": _degree_values(max_hops),
                        }
                    ]
                }
            ],
            "searchFlags": {"skipHighlighting": True, "maxAggValues": 3},
        }
    }
    data = client.graphql(LINEAGE_QUERY, variables)
    response = data.get("searchAcrossLineage")
    return response if isinstance(response, dict) else {}


def _summarize_lineage_results(
    payload: dict[str, Any],
    public_base_url: str,
) -> list[dict[str, Any]]:
    entries = []
    for item in payload.get("searchResults") or []:
        entity = item.get("entity") or {}
        urn = entity.get("urn") or ""
        properties = entity.get("properties") or {}
        entries.append(
            {
                "degree": item.get("degree"),
                "urn": urn,
                "type": entity.get("type"),
                "name": properties.get("name") or entity.get("name"),
                "description": truncate_text(properties.get("description") or "", 300),
                "datahub_url": make_datahub_dataset_url(urn, public_base_url)
                if urn.startswith("urn:li:dataset:")
                else "",
            }
        )
    return entries
