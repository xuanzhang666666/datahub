from __future__ import annotations

from blf_datahub_mcp.summarizers import (
    URN_DATA_AVAILABILITY_FLAG,
    URN_ETL_SCRIPT,
    URN_EXECUTE_SHELL,
    URN_OTHER_REMARK,
    URN_SCHEDULE_URL,
    extract_all_structured_properties,
    extract_structured_properties,
    extract_table_refs,
    governance_gaps,
    normalize_structured_property_name,
    strip_markdown_code_fence,
)


def test_strip_markdown_code_fence() -> None:
    assert strip_markdown_code_fence("```sql\nselect 1\n```") == "select 1"


def test_extract_structured_properties() -> None:
    payload = {
        "structuredProperties": {
            "value": {
                "properties": [
                    {
                        "propertyUrn": URN_ETL_SCRIPT,
                        "values": [{"string": "```sql\nselect * from ods.t\n```"}],
                    },
                    {
                        "propertyUrn": URN_EXECUTE_SHELL,
                        "values": [{"string": "```shell\nsh run.sh\n```"}],
                    },
                    {
                        "propertyUrn": URN_DATA_AVAILABILITY_FLAG,
                        "values": [{"string": "表血缘"}, {"string": "DDL"}],
                    },
                    {
                        "propertyUrn": URN_SCHEDULE_URL,
                        "values": [{"string": "https://schedule/job/a"}],
                    },
                    {
                        "propertyUrn": URN_OTHER_REMARK,
                        "values": [{"string": "owner remark"}],
                    },
                ]
            }
        }
    }

    result = extract_structured_properties(payload)

    assert result["etl_script"] == "select * from ods.t"
    assert result["execute_shell"] == "sh run.sh"
    assert result["data_availability_flags"] == ["DDL", "表血缘"]
    assert result["schedule_url"] == "https://schedule/job/a"
    assert result["other_remark"] == "owner remark"


def test_extract_all_structured_properties_keeps_known_and_unknown() -> None:
    payload = {
        "structuredProperties": {
            "value": {
                "properties": [
                    {
                        "propertyUrn": URN_ETL_SCRIPT,
                        "values": [{"string": "```sql\nselect 1\n```"}],
                    },
                    {
                        "propertyUrn": "urn:li:structuredProperty:custom.extra",
                        "values": [{"string": "x"}],
                    },
                ]
            }
        }
    }

    result = extract_all_structured_properties(payload)

    assert result["known"]["etl_script"]["first_value"] == "select 1"
    assert result["known"]["execute_shell"]["exists"] is False
    assert result["unknown"] == {"urn:li:structuredProperty:custom.extra": ["x"]}


def test_normalize_structured_property_name_accepts_aliases_and_urns() -> None:
    assert normalize_structured_property_name("shell") == "execute_shell"
    assert normalize_structured_property_name(URN_OTHER_REMARK) == "other_remark"


def test_extract_table_refs() -> None:
    refs = extract_table_refs(
        "insert overwrite table dw.target select * from ods.a join dim.b on a.id=b.id"
    )

    assert refs == ["dw.target", "ods.a", "dim.b"]


def test_governance_gaps_reports_missing_availability() -> None:
    gaps = governance_gaps(
        has_schema=True,
        has_documentation=True,
        has_etl_script=True,
        has_execute_shell=True,
        availability_flags=["DDL"],
    )

    assert "availability flag 缺少 表血缘" in gaps
    assert "availability flag 缺少 字段血缘" in gaps
