from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from job_info_sync_datahub.field_lineage_datahub_reader import make_hive_dataset_urn
from job_info_sync_datahub.structured_properties import URN_ETL_SCRIPT, URN_EXECUTE_SHELL
from job_info_sync_datahub.table_documentation_from_dataset_props import (
    AUTO_DOC_END,
    AUTO_DOC_START,
    build_llm_user_message,
    decode_trino_ddl_unicode_comments,
    expand_generated_markdown_variables,
    expand_prompt_variables,
    merge_documentation,
    prune_python_script_to_entrypoint,
    strip_commented_logic_for_prompt,
    sync_one_table_documentation,
    with_updated_editable_description,
)


def _structured_properties_payload(etl_script: str = "", execute_shell: str = "") -> dict:
    return {
        "structuredProperties": {
            "value": {
                "properties": [
                    {
                        "propertyUrn": URN_ETL_SCRIPT,
                        "values": [{"string": etl_script}],
                    },
                    {
                        "propertyUrn": URN_EXECUTE_SHELL,
                        "values": [{"string": execute_shell}],
                    },
                ]
            }
        }
    }


def test_merge_documentation_append_preserves_existing_text() -> None:
    merged = merge_documentation("人工说明", "## 表加工逻辑说明\nAI", action="append")

    assert merged.startswith("人工说明")
    assert AUTO_DOC_START in merged
    assert "## 表加工逻辑说明" in merged
    assert AUTO_DOC_END in merged


def test_merge_documentation_append_replaces_existing_auto_block_only() -> None:
    existing = f"人工说明\n\n{AUTO_DOC_START}\n旧内容\n{AUTO_DOC_END}\n\n人工结尾"

    merged = merge_documentation(existing, "新内容", action="append")

    assert "人工说明" in merged
    assert "人工结尾" in merged
    assert "新内容" in merged
    assert "旧内容" not in merged
    assert merged.count(AUTO_DOC_START) == 1


def test_merge_documentation_overwrite_replaces_all_content() -> None:
    merged = merge_documentation("人工说明", "新内容", action="overwrite")

    assert merged == "新内容"


def test_with_updated_editable_description_preserves_other_fields() -> None:
    class EditableAspect:
        def __init__(self, description: str = "", name: str = "") -> None:
            self.description = description
            self.name = name

    existing = EditableAspect(description="旧说明", name="人工名称")

    updated = with_updated_editable_description(existing, "新说明", EditableAspect)

    assert updated is existing
    assert updated.description == "新说明"
    assert updated.name == "人工名称"


def test_prune_python_script_to_entrypoint_omits_unreachable_functions() -> None:
    script = """
def unused_backup():
    return "backup"

def helper():
    return "real"

def run_job():
    return helper()
"""

    pruned = prune_python_script_to_entrypoint(script, "python job.py --entry run_job")

    assert "def run_job" in pruned
    assert "def helper" in pruned
    assert "unused_backup" not in pruned


def test_build_llm_user_message_contains_ddl_and_upstream_field_table_contract() -> None:
    msg = build_llm_user_message(
        table_name="dw.target",
        dataset_urn=make_hive_dataset_urn("dw.target"),
        execute_shell="sh run.sh",
        etl_script="insert overwrite table dw.target select id from ods.source",
        ddl="CREATE TABLE dw.target (id bigint)",
    )

    assert "CREATE TABLE dw.target" in msg
    assert "Execute Shell" in msg
    assert "Etl Script" in msg
    assert "完整 DDL" in msg
    assert "### 4. 数据来源" in msg
    assert "| 上游表 | 用途 |" in msg
    assert "| --- | --- |" in msg
    assert "SQL 中出现过的所有来源表都必须列出" in msg
    assert "脚本中定义但最终写入未使用" in msg
    assert "not_verified_" in msg
    assert "不要放入 4. 数据来源" in msg
    assert "### 5. 使用到的上游表字段" in msg
    assert "| 上游表 | 字段 | 在本表加工中的用途 | 相关逻辑/表达式 |" in msg
    assert "无法确认字段时填“未明确”" in msg


def test_expand_prompt_variables_replaces_simple_shell_assignments() -> None:
    script = """
DATABASE=data_smartorder
TARGET_TABLE="${DATABASE}.dw_ordering_report_store_status_monitor_cost_di"

insert overwrite table ${TARGET_TABLE}
select *
from ${DATABASE}.dw_ordering_report_store_status_monitor_half_hour_info_di
"""

    expanded = expand_prompt_variables(script)

    assert "data_smartorder.dw_ordering_report_store_status_monitor_half_hour_info_di" in expanded
    assert "data_smartorder.dw_ordering_report_store_status_monitor_cost_di" in expanded
    assert "${DATABASE}.dw_ordering_report_store_status_monitor_half_hour_info_di" not in expanded


def test_expand_prompt_variables_normalizes_dev_database_assignments() -> None:
    script = """
if [ "$1" == "gray" ] ; then
    DATABASE="data_smartorder_dev"
else
    DATABASE="data_smartorder"
fi
DEV_ONLY_DB=data_smartorder_dev

select *
from ${DATABASE}.dw_ordering_report_store_status_monitor_half_hour_info_di
join ${DEV_ONLY_DB}.dim_ordering_city_info
"""

    expanded = expand_prompt_variables(script)

    assert "data_smartorder.dw_ordering_report_store_status_monitor_half_hour_info_di" in expanded
    assert "data_smartorder.dim_ordering_city_info" in expanded
    assert "data_smartorder_dev." not in expanded


def test_expand_generated_markdown_variables_uses_etl_assignments() -> None:
    etl_script = """
DATABASE="data_smartorder"
BEST_TABLE="${DATABASE}.dw_ordering_report_store_status_monitor_best_status_di"
"""
    markdown = """
| `${DATABASE}.dw_ordering_report_store_status_monitor_history_status_di` | 历史状态 |
| `${BEST_TABLE}` | 最佳状态 |
"""

    expanded = expand_generated_markdown_variables(markdown, etl_script)

    assert "`data_smartorder.dw_ordering_report_store_status_monitor_history_status_di`" in expanded
    assert "`data_smartorder.dw_ordering_report_store_status_monitor_best_status_di`" in expanded
    assert "${DATABASE}" not in expanded
    assert "${BEST_TABLE}" not in expanded


def test_strip_commented_logic_for_prompt_removes_commented_sql_sources() -> None:
    script = """
function run_job {
  $HIVE <<EOF
    select *
    from data_smartorder.real_source_di
    -- left join data_smartorder.commented_line_source_di c on a.id = c.id
    # left join data_smartorder.hash_commented_source_di h on a.id = h.id
    /*
    left join data_smartorder.block_commented_source_di b on a.id = b.id
    */
    left join data_smartorder.real_dim_di d on a.id = d.id -- keep inline note
  EOF
}
"""

    cleaned = strip_commented_logic_for_prompt(script)

    assert "data_smartorder.real_source_di" in cleaned
    assert "data_smartorder.real_dim_di" in cleaned
    assert "commented_line_source_di" not in cleaned
    assert "hash_commented_source_di" not in cleaned
    assert "block_commented_source_di" not in cleaned
    assert "keep inline note" not in cleaned


def test_decode_trino_ddl_unicode_comments_to_readable_utf8() -> None:
    ddl = (
        "CREATE TABLE default.t (\n"
        "  id bigint COMMENT U&'\\8BA2\\5355ID',\n"
        "  name string COMMENT U&'\\5546\\54C1\\540D\\79F0'\n"
        ") COMMENT U&'\\8868\\6CE8\\91CA'"
    )

    decoded = decode_trino_ddl_unicode_comments(ddl)

    assert "COMMENT '订单ID'" in decoded
    assert "COMMENT '商品名称'" in decoded
    assert "COMMENT '表注释'" in decoded
    assert "U&'" not in decoded


def test_sync_one_table_documentation_dry_run_exports_markdown_and_does_not_write(tmp_path: Path) -> None:
    payload = _structured_properties_payload(
        etl_script="```sql\ninsert overwrite table dw.target select id from ods.source\n```",
        execute_shell="```shell\nsh run.sh\n```",
    )
    llm_raw = {
        "content": "## 表加工逻辑说明\n\n### 2. 表结构 DDL\n```sql\nCREATE TABLE dw.target (id bigint)\n```",
        "raw_response": {"id": "test"},
    }

    with (
        patch(
            "job_info_sync_datahub.table_documentation_from_dataset_props.fetch_structured_properties",
            return_value=payload,
        ),
        patch(
            "job_info_sync_datahub.table_documentation_from_dataset_props.is_view_dataset",
            return_value=False,
        ),
        patch(
            "job_info_sync_datahub.table_documentation_from_dataset_props.fetch_table_ddl",
            return_value="CREATE TABLE dw.target (id bigint)",
        ),
        patch(
            "job_info_sync_datahub.table_documentation_from_dataset_props.call_llm_generate_documentation",
            return_value=llm_raw,
        ),
        patch(
            "job_info_sync_datahub.table_documentation_from_dataset_props.fetch_existing_editable_description",
            return_value="人工说明",
        ),
        patch(
            "job_info_sync_datahub.table_documentation_from_dataset_props.write_editable_description",
        ) as write_desc,
    ):
        result = sync_one_table_documentation(
            "dw.target",
            gms_url="http://localhost:8080",
            token=None,
            platform_instance="blf-prod-hive",
            env="PROD",
            dry_run=True,
            action="append",
            llm_timeout_sec=1,
            output_dir=str(tmp_path),
        )

    assert result["status"] == "OK"
    assert result["write_documentation"] is False
    assert result["ddl_export_path"]
    assert result["markdown_export_path"]
    assert result["llm_raw_export_path"]
    assert "CREATE TABLE dw.target" in Path(result["ddl_export_path"]).read_text(encoding="utf-8")
    assert AUTO_DOC_START in Path(result["markdown_export_path"]).read_text(encoding="utf-8")
    assert json.loads(Path(result["llm_raw_export_path"]).read_text(encoding="utf-8")) == llm_raw
    write_desc.assert_not_called()


def test_sync_one_table_documentation_skips_when_no_source_and_no_ddl() -> None:
    with (
        patch(
            "job_info_sync_datahub.table_documentation_from_dataset_props.fetch_structured_properties",
            return_value=_structured_properties_payload(),
        ),
        patch(
            "job_info_sync_datahub.table_documentation_from_dataset_props.is_view_dataset",
            return_value=False,
        ),
        patch(
            "job_info_sync_datahub.table_documentation_from_dataset_props.fetch_table_ddl",
            side_effect=RuntimeError("DDL failed"),
        ),
    ):
        result = sync_one_table_documentation(
            "dw.target",
            gms_url="http://localhost:8080",
            token=None,
            platform_instance="blf-prod-hive",
            env="PROD",
            dry_run=True,
            action="append",
            llm_timeout_sec=1,
            output_dir=None,
        )

    assert result["status"] == "SKIP"
    assert result["documentation_status"] == "SKIP_NO_DOC_SOURCE"
