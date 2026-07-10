"""BLF DataHub Hive MCP tool implementations."""

from __future__ import annotations

from typing import Any, Literal

from .datahub_client import DataHubClient, DataHubClientError
from .field_lineage import trace_hive_field_lineage
from .hive import make_datahub_dataset_url, make_hive_dataset_urn, normalize_hive_table
from .schedule import job_base, make_datahub_datajob_url
from .summarizers import (
    STRUCTURED_PROPERTY_DEFINITIONS,
    aspect_value,
    extract_all_structured_properties,
    extract_datajob_dependencies,
    extract_datajob_info,
    extract_datajob_structured_properties,
    extract_structured_properties,
    extract_table_refs,
    governance_gaps,
    normalize_structured_property_name,
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

SCHEDULE_JOB_SEARCH_QUERY = """
query SearchScheduleJobs($input: SearchAcrossEntitiesInput!) {
  searchAcrossEntities(input: $input) {
    total
    searchResults {
      entity {
        urn
        type
        ... on DataJob {
          properties {
            name
            description
            customProperties { key value }
          }
        }
      }
    }
  }
}
"""

DATAJOB_PROFILE_ASPECTS = ["dataJobInfo", "dataJobInputOutput"]


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
            editable_props.get("description") or dataset_props.get("description") or ""
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
        fields = (
            summarize_schema_fields(schema, field_limit) if include_fields else None
        )
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
                "known_properties": {
                    name: {
                        "property_urn": payload["property_urn"],
                        "display_name": payload["display_name"],
                        "exists": payload["exists"],
                        "value_count": len(payload["values"]),
                    }
                    for name, payload in structured["known_properties"].items()
                },
                "has_etl_script": bool(structured["etl_script"]),
                "has_execute_shell": bool(structured["execute_shell"]),
                "has_schedule_url": bool(structured["schedule_url"]),
                "has_other_remark": bool(structured["other_remark"]),
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


def get_hive_structured_properties(
    client: DataHubClient,
    *,
    public_base_url: str,
    table: str,
    max_value_chars: int = 4000,
) -> dict[str, Any]:
    """Return all known BLF structured properties for one Hive table."""
    try:
        base = _base(table, public_base_url)
        payload = client.get_structured_properties(base["dataset_urn"])
        structured = extract_all_structured_properties(payload)
        properties = {}
        for name, property_payload in structured["known"].items():
            first_value = str(property_payload.get("first_value") or "")
            properties[name] = {
                **property_payload,
                "first_value": truncate_text(first_value, max_value_chars),
                "values": [
                    truncate_text(str(value), max_value_chars)
                    for value in property_payload.get("values", [])
                ],
                "value_count": len(property_payload.get("values", [])),
            }
        missing = [
            item["display_name"]
            for item in properties.values()
            if not item.get("exists")
        ]
        return {
            "success": True,
            **base,
            "summary": {
                "properties": properties,
                "missing_properties": missing,
                "unknown_property_urns": sorted(structured["unknown"]),
            },
            "risks": [f"缺少 {name} structured property" for name in missing],
            "evidence": {
                "interface": "OpenAPI structuredProperties",
                "known_property_urns": {
                    name: definition["urn"]
                    for name, definition in STRUCTURED_PROPERTY_DEFINITIONS.items()
                },
                "raw_property_urns": structured["raw_property_urns"],
            },
        }
    except Exception as exc:
        try:
            base = _base(table, public_base_url)
        except Exception:
            base = {"table": table}
        return _error_response(exc, **base)


def get_hive_structured_property(
    client: DataHubClient,
    *,
    public_base_url: str,
    table: str,
    property_name: str,
    max_value_chars: int = 8000,
) -> dict[str, Any]:
    """Return one BLF structured property for one Hive table."""
    try:
        normalized_property_name = normalize_structured_property_name(property_name)
        base = _base(table, public_base_url)
        payload = client.get_structured_properties(base["dataset_urn"])
        structured = extract_all_structured_properties(payload)
        property_payload = structured["known"][normalized_property_name]
        values = [
            truncate_text(str(value), max_value_chars)
            for value in property_payload.get("values", [])
        ]
        return {
            "success": True,
            **base,
            "summary": {
                "property_name": normalized_property_name,
                "property_urn": property_payload["property_urn"],
                "display_name": property_payload["display_name"],
                "exists": property_payload["exists"],
                "value_count": len(values),
                "first_value": values[0]
                if values
                else truncate_text("", max_value_chars),
                "values": values,
            },
            "risks": []
            if property_payload["exists"]
            else [f"缺少 {property_payload['display_name']} structured property"],
            "evidence": {
                "interface": "OpenAPI structuredProperties",
                "raw_property_urns": structured["raw_property_urns"],
            },
        }
    except Exception as exc:
        try:
            base = _base(table, public_base_url)
        except Exception:
            base = {"table": table}
        return _error_response(exc, **base)


def get_hive_etl_script(
    client: DataHubClient,
    *,
    public_base_url: str,
    table: str,
    max_value_chars: int = 50000,
) -> dict[str, Any]:
    """Return Etl Script structured property for one Hive table."""
    return get_hive_structured_property(
        client,
        public_base_url=public_base_url,
        table=table,
        property_name="etl_script",
        max_value_chars=max_value_chars,
    )


def get_hive_execute_shell(
    client: DataHubClient,
    *,
    public_base_url: str,
    table: str,
    max_value_chars: int = 8000,
) -> dict[str, Any]:
    """Return Execute Shell structured property for one Hive table."""
    return get_hive_structured_property(
        client,
        public_base_url=public_base_url,
        table=table,
        property_name="execute_shell",
        max_value_chars=max_value_chars,
    )


def get_hive_schedule_url(
    client: DataHubClient,
    *,
    public_base_url: str,
    table: str,
    max_value_chars: int = 2000,
) -> dict[str, Any]:
    """Return Schedule URL structured property for one Hive table."""
    return get_hive_structured_property(
        client,
        public_base_url=public_base_url,
        table=table,
        property_name="schedule_url",
        max_value_chars=max_value_chars,
    )


def get_hive_data_availability_flag(
    client: DataHubClient,
    *,
    public_base_url: str,
    table: str,
    max_value_chars: int = 2000,
) -> dict[str, Any]:
    """Return Data Availability Flag structured property for one Hive table."""
    return get_hive_structured_property(
        client,
        public_base_url=public_base_url,
        table=table,
        property_name="data_availability_flag",
        max_value_chars=max_value_chars,
    )


def get_hive_other_remark(
    client: DataHubClient,
    *,
    public_base_url: str,
    table: str,
    max_value_chars: int = 8000,
) -> dict[str, Any]:
    """Return Other Remark structured property for one Hive table."""
    return get_hive_structured_property(
        client,
        public_base_url=public_base_url,
        table=table,
        property_name="other_remark",
        max_value_chars=max_value_chars,
    )


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
        max_hops = max(int(max_hops), 1)
        max_results = max(int(max_results), 1)
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


def explain_hive_field_lineage(
    client: DataHubClient,
    *,
    public_base_url: str,
    table: str,
    fields: list[str] | None = None,
    max_depth: int = 30,
    max_paths: int = 1000,
    max_transform_chars: int = 1200,
) -> dict[str, Any]:
    """Return recursive field-lineage evidence for one Hive table."""
    try:
        base = _base(table, public_base_url)
        summary = trace_hive_field_lineage(
            client,
            table=table,
            fields=fields,
            max_depth=max_depth,
            max_paths=max_paths,
            max_transform_chars=max_transform_chars,
        )
        risks = []
        stop_reasons = summary.get("stop_reasons") or {}
        if stop_reasons.get("MISSING_FIELD_LINEAGE"):
            risks.append("部分字段在到达 ods/pdw 前缺少字段级血缘")
        if stop_reasons.get("DATASET_NOT_FOUND"):
            risks.append("部分上游表缺少 schemaMetadata 或无法读取")
        if stop_reasons.get("INVALID_SCHEMA_FIELD_URN"):
            risks.append("部分字段血缘包含无效 schemaField URN")
        if stop_reasons.get("CYCLE_DETECTED"):
            risks.append("字段血缘中检测到循环")
        if stop_reasons.get("MAX_DEPTH_REACHED"):
            risks.append("部分字段血缘达到 max_depth 后停止")
        if summary.get("truncated"):
            risks.append("字段血缘路径数量超过 max_paths，结果已截断")
        if summary.get("unconfirmed_tables"):
            risks.append("部分表缺少 Data Availability Flag: 字段血缘")
        return {
            "success": True,
            **base,
            "summary": summary,
            "risks": risks,
            "evidence": {
                "interface": "OpenAPI dataset aspects",
                "aspects": [
                    "schemaMetadata",
                    "upstreamLineage",
                    "structuredProperties",
                ],
                "note": "这是字段级血缘递归追溯结果；MCP 仅返回结构化证据，不生成自然语言解释。",
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
            if urn:
                try:
                    structured = extract_structured_properties(
                        client.get_structured_properties(urn)
                    )
                    flags = structured["data_availability_flags"]
                except Exception:
                    flags = []
            if only_available and not flags:
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
                "schedule_url": profile["structured_properties"]["has_schedule_url"],
                "other_remark": profile["structured_properties"]["has_other_remark"],
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


def get_schedule_job_profile(
    client: DataHubClient,
    *,
    public_base_url: str,
    job_display_name: str,
    include_shell: bool = False,
) -> dict[str, Any]:
    """Return a compact DataHub profile for one BLF scheduler DataJob."""
    try:
        base = job_base(job_display_name, public_base_url)
        aspects = client.get_datajob_aspects(
            base["datajob_urn"], DATAJOB_PROFILE_ASPECTS
        )
        sp_payload = client.get_datajob_structured_properties(base["datajob_urn"])
        job_info = extract_datajob_info(aspect_value(aspects, "dataJobInfo"))
        dependencies = extract_datajob_dependencies(
            aspect_value(aspects, "dataJobInputOutput")
        )
        sp = extract_datajob_structured_properties(sp_payload)
        custom = job_info["customProperties"]
        risks = []
        if not custom.get("job_owner_name"):
            risks.append("缺少 job_owner_name")
        if custom.get("job_disable") == "true":
            risks.append("调度作业已禁用 (job_disable=true)")
        summary: dict[str, Any] = {
            "name": job_info["name"],
            "description": job_info["description"],
            "trigger_type": custom.get("trigger_type", ""),
            "cron_schedule": custom.get("cron_schedule", ""),
            "time_hour_param": custom.get("time_hour_param", ""),
            "job_owner_name": custom.get("job_owner_name", ""),
            "job_proxy_user": custom.get("job_proxy_user", ""),
            "line_business_code": custom.get("line_business_code", ""),
            "contacts_name": custom.get("contacts_name", ""),
            "assigned_node": custom.get("assigned_node", ""),
            "job_disable": custom.get("job_disable", ""),
            "job_priority": custom.get("job_priority", ""),
            "last_build_start_time": custom.get("last_build_start_time", ""),
            "dependency_count": len(dependencies),
            "dependencies": dependencies,
            "has_execute_shell": sp["job_execute_shell"]["exists"],
            "has_content_xml": sp["job_content_xml"]["exists"],
        }
        if include_shell:
            summary["execute_shell"] = truncate_text(
                sp["job_execute_shell"]["first_value"], 3000
            )
        return {
            "success": True,
            **base,
            "summary": summary,
            "risks": risks,
            "evidence": {
                "interface": "OpenAPI dataJob aspects",
                "aspects": DATAJOB_PROFILE_ASPECTS,
            },
        }
    except Exception as exc:
        try:
            base = job_base(job_display_name, public_base_url)
        except Exception:
            base = {"job_display_name": job_display_name}
        return _error_response(exc, **base)


def get_schedule_job_execute_shell(
    client: DataHubClient,
    *,
    public_base_url: str,
    job_display_name: str,
    max_value_chars: int = 8000,
) -> dict[str, Any]:
    """Return the job_execute_shell structured property for a scheduler job."""
    try:
        base = job_base(job_display_name, public_base_url)
        sp_payload = client.get_datajob_structured_properties(base["datajob_urn"])
        sp = extract_datajob_structured_properties(sp_payload)
        prop = sp["job_execute_shell"]
        return {
            "success": True,
            **base,
            "summary": {
                "property_name": "job_execute_shell",
                "property_urn": prop["property_urn"],
                "exists": prop["exists"],
                "first_value": truncate_text(prop["first_value"], max_value_chars),
            },
            "risks": []
            if prop["exists"]
            else ["缺少 Job Execute Shell structured property"],
            "evidence": {
                "interface": "OpenAPI dataJob structuredProperties",
                "property_urn": prop["property_urn"],
            },
        }
    except Exception as exc:
        try:
            base = job_base(job_display_name, public_base_url)
        except Exception:
            base = {"job_display_name": job_display_name}
        return _error_response(exc, **base)


def get_schedule_job_content_xml(
    client: DataHubClient,
    *,
    public_base_url: str,
    job_display_name: str,
    max_value_chars: int = 8000,
) -> dict[str, Any]:
    """Return the job_content_xml structured property for a scheduler job."""
    try:
        base = job_base(job_display_name, public_base_url)
        sp_payload = client.get_datajob_structured_properties(base["datajob_urn"])
        sp = extract_datajob_structured_properties(sp_payload)
        prop = sp["job_content_xml"]
        return {
            "success": True,
            **base,
            "summary": {
                "property_name": "job_content_xml",
                "property_urn": prop["property_urn"],
                "exists": prop["exists"],
                "first_value": truncate_text(prop["first_value"], max_value_chars),
            },
            "risks": []
            if prop["exists"]
            else ["缺少 Job Content XML structured property"],
            "evidence": {
                "interface": "OpenAPI dataJob structuredProperties",
                "property_urn": prop["property_urn"],
            },
        }
    except Exception as exc:
        try:
            base = job_base(job_display_name, public_base_url)
        except Exception:
            base = {"job_display_name": job_display_name}
        return _error_response(exc, **base)


def get_schedule_job_lineage(
    client: DataHubClient,
    *,
    public_base_url: str,
    job_display_name: str,
    direction: Literal["upstream", "downstream", "both"] = "both",
    max_hops: int = 1,
    max_results: int = 500,
) -> dict[str, Any]:
    """Return DataHub lineage (scheduling DAG) for one BLF scheduler DataJob."""
    try:
        base = job_base(job_display_name, public_base_url)
        if direction not in {"upstream", "downstream", "both"}:
            raise ValueError("direction must be upstream, downstream, or both")
        max_hops = max(int(max_hops), 1)
        max_results = max(int(max_results), 1)
        result: dict[str, Any] = {}
        totals: dict[str, int] = {}
        for label, gql_direction in _lineage_directions(direction):
            payload = _fetch_lineage(
                client,
                base["datajob_urn"],
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
            "risks": [] if has_any else ["DataHub 调度 DAG 血缘为空或未摄入"],
            "evidence": {
                "interface": "GraphQL searchAcrossLineage",
                "note": "展示调度 DAG 中的 DataJob 上下游连接",
            },
        }
    except Exception as exc:
        try:
            base = job_base(job_display_name, public_base_url)
        except Exception:
            base = {"job_display_name": job_display_name}
        return _error_response(exc, **base)


def search_schedule_jobs(
    client: DataHubClient,
    *,
    public_base_url: str,
    query: str,
    limit: int = 10,
) -> dict[str, Any]:
    """Search BLF scheduler jobs in DataHub by keyword."""
    try:
        limit = min(max(int(limit), 1), 200)
        variables = {
            "input": {
                "query": query,
                "start": 0,
                "count": limit,
                "types": ["DATA_JOB"],
                "orFilters": [
                    {
                        "and": [
                            {
                                "field": "orchestrator",
                                "condition": "EQUAL",
                                "values": ["blf-schedule"],
                            }
                        ]
                    }
                ],
            }
        }
        data = client.graphql(SCHEDULE_JOB_SEARCH_QUERY, variables)
        response = data.get("searchAcrossEntities") or {}
        candidates = []
        for item in response.get("searchResults") or []:
            entity = item.get("entity") or {}
            urn = entity.get("urn") or ""
            props = entity.get("properties") or {}
            name = props.get("name") or entity.get("name") or ""
            custom_props: dict[str, str] = {}
            for kv in props.get("customProperties") or []:
                if isinstance(kv, dict) and kv.get("key"):
                    custom_props[kv["key"]] = kv.get("value") or ""
            job_display_name = custom_props.get("job_display_name") or name
            candidates.append(
                {
                    "urn": urn,
                    "job_display_name": job_display_name,
                    "name": name,
                    "description": truncate_text(props.get("description") or "", 300),
                    "trigger_type": custom_props.get("trigger_type", ""),
                    "job_owner_name": custom_props.get("job_owner_name", ""),
                    "datahub_url": make_datahub_datajob_url(urn, public_base_url)
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
                "candidates": candidates,
            },
            "risks": []
            if candidates
            else ["未找到匹配的调度作业，请检查 job_display_name 是否正确"],
            "evidence": {"interface": "GraphQL searchAcrossEntities"},
        }
    except Exception as exc:
        return _error_response(exc, query=query)


def explain_schedule_job_context(
    client: DataHubClient,
    *,
    public_base_url: str,
    job_display_name: str,
) -> dict[str, Any]:
    """Return one compact context bundle for a scheduler job (profile + lineage + shell)."""
    profile = get_schedule_job_profile(
        client,
        public_base_url=public_base_url,
        job_display_name=job_display_name,
        include_shell=False,
    )
    shell = get_schedule_job_execute_shell(
        client,
        public_base_url=public_base_url,
        job_display_name=job_display_name,
        max_value_chars=4000,
    )
    lineage = get_schedule_job_lineage(
        client,
        public_base_url=public_base_url,
        job_display_name=job_display_name,
        direction="both",
        max_hops=1,
        max_results=100,
    )
    if not profile.get("success"):
        return profile
    risks = []
    for payload in (profile, shell, lineage):
        risks.extend(payload.get("risks") or [])
    base = job_base(job_display_name, public_base_url)
    return {
        "success": True,
        **base,
        "summary": {
            "profile": profile.get("summary"),
            "execute_shell": shell.get("summary") if shell.get("success") else None,
            "lineage": lineage.get("summary") if lineage.get("success") else None,
        },
        "risks": sorted(set(risks)),
        "evidence": {
            "tools": [
                "blf_get_schedule_job_profile",
                "blf_get_schedule_job_execute_shell",
                "blf_get_schedule_job_lineage",
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


# Powers blf_get_hive_table_stats and blf_get_hive_table_queries.
# `operations` is the per-execution record (actor, sql text, timestamp, affected rows)
# produced by the Hive SQL queries ingestion source. `datasetProfiles` provides the
# latest row/column counts; `usageStats` aggregates totalSqlQueries / uniqueUserCount /
# topSqlQueries over a TimeRange; `statsSummary` mirrors the Stats tab "Highlights" card.
DATASET_STATS_QUERIES_QUERY = """
query DatasetStatsAndQueries(
  $urn: String!
  $range: TimeRange
  $profileLimit: Int
  $operationLimit: Int
  $operationStartMillis: Long
  $operationEndMillis: Long
) {
  dataset(urn: $urn) {
    urn
    name
    exists
    statsSummary {
      queryCountLast30Days
      uniqueUserCountLast30Days
      topUsersLast30Days { urn username }
    }
    usageStats(range: $range) {
      aggregations {
        uniqueUserCount
        totalSqlQueries
        users { user { urn username } count }
        fields { fieldName count }
      }
      buckets {
        bucket
        duration
        resource
        metrics {
          uniqueUserCount
          totalSqlQueries
          topSqlQueries
          users { user { urn username } count }
          fields { fieldName count }
        }
      }
    }
    datasetProfiles(limit: $profileLimit) {
      timestampMillis
      rowCount
      columnCount
      sizeInBytes
      fieldProfiles {
        fieldPath
        nullCount
        uniqueCount
        min
        max
      }
    }
    operations(
      startTimeMillis: $operationStartMillis
      endTimeMillis: $operationEndMillis
      limit: $operationLimit
    ) {
      timestampMillis
      lastUpdatedTimestamp
      actor
      operationType
      affectedDatasets
      numAffectedRows
      customProperties { key value }
    }
  }
}
"""


def _summarize_top_users(
    users: list[dict[str, Any]] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Normalize per-user usage counts to a compact {urn, username, count} shape."""
    if not users:
        return []
    out: list[dict[str, Any]] = []
    for entry in users[:limit]:
        if not isinstance(entry, dict):
            continue
        user = entry.get("user") or {}
        out.append(
            {
                "urn": user.get("urn") or "",
                "username": user.get("username") or "",
                "count": entry.get("count"),
            }
        )
    return out


def get_hive_table_stats(
    client: DataHubClient,
    *,
    public_base_url: str,
    table: str,
    range: Literal["DAY", "WEEK", "MONTH"] = "MONTH",
    profile_limit: int = 5,
) -> dict[str, Any]:
    """Return the Stats-tab evidence (Highlights + Latest + Recent profiles) for one Hive table."""
    try:
        base = _base(table, public_base_url)
        if range not in {"DAY", "WEEK", "MONTH"}:
            raise ValueError("range must be DAY, WEEK, or MONTH")
        profile_limit = max(int(profile_limit), 1)
        data = client.graphql(
            DATASET_STATS_QUERIES_QUERY,
            {
                "urn": base["dataset_urn"],
                "range": range,
                "profileLimit": profile_limit,
                "operationLimit": 0,
                "operationStartMillis": None,
                "operationEndMillis": None,
            },
        )
        dataset = data.get("dataset") or {}
        if not dataset.get("exists", True):
            return _error_response(
                ValueError(f"dataset {base['dataset']} does not exist in DataHub"),
                **base,
            )
        stats_summary = dataset.get("statsSummary") or {}
        usage_stats = dataset.get("usageStats") or {}
        aggregations = usage_stats.get("aggregations") or {}
        buckets = usage_stats.get("buckets") or []
        profiles = dataset.get("datasetProfiles") or []
        latest = profiles[0] if profiles else {}
        risks: list[str] = []
        if not stats_summary:
            risks.append("statsSummary 为空：最近 30 天可能尚未采集到 usage")
        if not latest:
            risks.append("datasetProfiles 为空：表画像 ingestion 尚未运行")
        if aggregations.get("totalSqlQueries") in (None, 0) and not stats_summary.get(
            "queryCountLast30Days"
        ):
            risks.append("该表最近 30 天内没有 SQL 查询记录")
        return {
            "success": True,
            **base,
            "summary": {
                "range": range,
                "highlights": {
                    "query_count": stats_summary.get("queryCountLast30Days"),
                    "unique_user_count": stats_summary.get("uniqueUserCountLast30Days"),
                    "top_users_last_30_days": [
                        {
                            "urn": (u or {}).get("urn") or "",
                            "username": (u or {}).get("username") or "",
                        }
                        for u in (stats_summary.get("topUsersLast30Days") or [])[:5]
                        if isinstance(u, dict)
                    ],
                },
                "latest": {
                    "row_count": latest.get("rowCount"),
                    "column_count": latest.get("columnCount"),
                    "size_in_bytes": latest.get("sizeInBytes"),
                    "timestamp_millis": latest.get("timestampMillis"),
                },
                "usage": {
                    "unique_user_count": aggregations.get("uniqueUserCount"),
                    "total_sql_queries": aggregations.get("totalSqlQueries"),
                    "top_users": _summarize_top_users(aggregations.get("users"), 10),
                    "fields": [
                        {
                            "field_name": (f or {}).get("fieldName"),
                            "count": (f or {}).get("count"),
                        }
                        for f in (aggregations.get("fields") or [])
                        if isinstance(f, dict)
                    ],
                },
                "bucket_count": len(buckets),
                "profiles": [
                    {
                        "timestamp_millis": p.get("timestampMillis"),
                        "row_count": p.get("rowCount"),
                        "column_count": p.get("columnCount"),
                        "size_in_bytes": p.get("sizeInBytes"),
                    }
                    for p in profiles
                ],
            },
            "risks": risks,
            "evidence": {
                "interface": "GraphQL dataset.{statsSummary,usageStats,datasetProfiles}",
                "note": "Stats tab 数据来自 usage + dataset profile ingestion，缺失即代表对应 ingestion 未接入。",
            },
        }
    except Exception as exc:
        try:
            base = _base(table, public_base_url)
        except Exception:
            base = {"table": table}
        return _error_response(exc, **base)


def get_hive_table_queries(
    client: DataHubClient,
    *,
    public_base_url: str,
    table: str,
    range: Literal["DAY", "WEEK", "MONTH"] = "MONTH",
    operation_limit: int = 20,
    operation_window_hours: int = 24 * 30,
    top_query_limit: int = 50,
    max_sql_chars: int = 8000,
) -> dict[str, Any]:
    """Return the Queries-tab evidence (top SQL + recent operations) for one Hive table."""
    try:
        base = _base(table, public_base_url)
        if range not in {"DAY", "WEEK", "MONTH"}:
            raise ValueError("range must be DAY, WEEK, or MONTH")
        operation_limit = max(int(operation_limit), 1)
        operation_window_hours = max(int(operation_window_hours), 1)
        top_query_limit = max(int(top_query_limit), 0)
        # End at the wall-clock now; start is a rolling window in the past.
        end_millis: int | None = None
        start_millis: int | None = None
        try:
            import time

            end_millis = int(time.time() * 1000)
            start_millis = end_millis - operation_window_hours * 3600 * 1000
        except Exception:
            start_millis = None
            end_millis = None
        data = client.graphql(
            DATASET_STATS_QUERIES_QUERY,
            {
                "urn": base["dataset_urn"],
                "range": range,
                "profileLimit": 0,
                "operationLimit": operation_limit,
                "operationStartMillis": start_millis,
                "operationEndMillis": end_millis,
            },
        )
        dataset = data.get("dataset") or {}
        if not dataset.get("exists", True):
            return _error_response(
                ValueError(f"dataset {base['table']} does not exist in DataHub"),
                **base,
            )
        usage_stats = dataset.get("usageStats") or {}
        aggregations = usage_stats.get("aggregations") or {}
        buckets = usage_stats.get("buckets") or []
        # Deduplicate topSqlQueries across buckets; order preserved by first appearance.
        seen: set[str] = set()
        top_queries: list[str] = []
        for bucket in buckets:
            metrics = (bucket or {}).get("metrics") or {}
            for sql in metrics.get("topSqlQueries") or []:
                if isinstance(sql, str) and sql not in seen:
                    seen.add(sql)
                    top_queries.append(sql)
        operations = dataset.get("operations") or []
        query_records: list[dict[str, Any]] = []
        for op in operations:
            if not isinstance(op, dict):
                continue
            # operations carry the executed SQL text in customProperties under "query_text"
            sql_text = ""
            for prop in op.get("customProperties") or []:
                if not isinstance(prop, dict):
                    continue
                if prop.get("key") in {"query_text", "query", "sql"}:
                    sql_text = prop.get("value") or ""
                    if sql_text:
                        break
            actor = op.get("actor") or ""
            affected = op.get("affectedDatasets") or []
            query_records.append(
                {
                    "timestamp_millis": op.get("timestampMillis"),
                    "operation_type": op.get("operationType"),
                    "actor_urn": actor if isinstance(actor, str) else "",
                    "affected_datasets": [a for a in affected if isinstance(a, str)],
                    "num_affected_rows": op.get("numAffectedRows"),
                    "sql": truncate_text(sql_text, max_sql_chars),
                }
            )
        risks: list[str] = []
        if not top_queries and not query_records:
            risks.append(
                "usageStats.topSqlQueries 与 operations 均为空：可能未接入 Hive SQL queries ingestion"
            )
        if not aggregations.get("totalSqlQueries"):
            risks.append("该时间窗内 totalSqlQueries 为 0")
        return {
            "success": True,
            **base,
            "summary": {
                "range": range,
                "operation_window_hours": operation_window_hours,
                "totals": {
                    "total_sql_queries": aggregations.get("totalSqlQueries"),
                    "unique_user_count": aggregations.get("uniqueUserCount"),
                    "top_unique_query_count": len(top_queries),
                    "operation_count": len(query_records),
                },
                "top_sql_queries": [
                    truncate_text(q, max_sql_chars)
                    for q in top_queries[:top_query_limit]
                ],
                "queries": query_records,
            },
            "risks": risks,
            "evidence": {
                "interface": "GraphQL dataset.{usageStats,operations}",
                "aspects": ["datasetUsageStatistics", "operation"],
                "note": (
                    "topSqlQueries 来自 usage ingestion 聚合；operations 来自 SQL queries ingestion。"
                    "两者独立来源，单独为空并不代表另一者也为空。"
                ),
            },
        }
    except Exception as exc:
        try:
            base = _base(table, public_base_url)
        except Exception:
            base = {"table": table}
        return _error_response(exc, **base)
