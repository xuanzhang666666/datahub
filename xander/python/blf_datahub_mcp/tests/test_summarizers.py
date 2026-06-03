from __future__ import annotations

from blf_datahub_mcp.summarizers import (
    URN_DATA_AVAILABILITY_FLAG,
    URN_ETL_SCRIPT,
    URN_EXECUTE_SHELL,
    extract_structured_properties,
    extract_table_refs,
    governance_gaps,
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
                ]
            }
        }
    }

    result = extract_structured_properties(payload)

    assert result["etl_script"] == "select * from ods.t"
    assert result["execute_shell"] == "sh run.sh"
    assert result["data_availability_flags"] == ["DDL", "表血缘"]


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

