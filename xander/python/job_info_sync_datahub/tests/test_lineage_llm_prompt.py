"""LLM table-lineage prompt behavior."""

from __future__ import annotations

from job_info_sync_datahub.lineage_llm_compare import _build_user_message


def test_prompt_explains_not_verified_table_alias_mapping() -> None:
    msg = _build_user_message(
        """
TABLE_NAME="pdim_tag_info_tag_details_v1"
NOT_VERIFIED_TABLE_NAME="not_verified_${TABLE_NAME}"
insert overwrite table $NOT_VERIFIED_TABLE_NAME
select * from ods_uploads_tag_info_tag_details_v1;
""",
        job_file_name="pdim_tag_info_tag_details_v1.job",
    )

    assert "NOT_VERIFIED_TABLE_NAME" in msg
    assert "not_verified_${TABLE_NAME}" in msg
    assert "not_verified_pdim_tag_info_tag_details_v1" in msg
    assert "target" in msg
    assert "pdim_tag_info_tag_details_v1" in msg


def test_job_prompt_keeps_segments_after_30_so_final_insert_is_visible() -> None:
    segments = [f"select {i} as c" for i in range(35)]
    segments.append(
        "insert overwrite table $DB_NAME.$NOT_VERIFIED_TABLE_NAME "
        "select * from data_smartorder.dw_ordering_tad14_store_di"
    )
    script = ";\n".join(segments) + ";"

    msg = _build_user_message(script, job_file_name="large.job")

    assert "-- SQL 段 36 --" in msg
    assert "insert overwrite table $DB_NAME.$NOT_VERIFIED_TABLE_NAME" in msg
    assert "data_smartorder.dw_ordering_tad14_store_di" in msg


def test_system_prompt_allows_physical_fully_qualified_tmp_upstreams() -> None:
    from job_info_sync_datahub.lineage_llm_compare import SYSTEM_PROMPT

    assert "有明确库名且不是本脚本内创建的 tmp_* 表" in SYSTEM_PROMPT
    assert "data_smartorder.tmp_xxx" in SYSTEM_PROMPT
