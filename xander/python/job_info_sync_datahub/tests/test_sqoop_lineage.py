"""Sqoop import jobs are documented without DataHub upstreamLineage writes."""

from __future__ import annotations

from job_info_sync_datahub.sqoop_lineage import (
    build_sqoop_documentation,
    detect_sqoop_import,
)


SQOOP_JOB = """
DATABASE_NAME="default"
TABLE_NAME="ods_bach_baseinfo_shop_company"
SOURCE_TABLE_NAME="company"
TABLE_COLUMNS="id,code,name,main_code,state,operator,created_time,updated_time"
SOURCE_TABLE_JDBC_STR="${BACH_BASEINFO_SHOP_STR}"

function ods_bach_baseinfo_shop_company_run {
    rebuild_hdfs_dir ${HDFS_DIR} && calculate && do_check
}

function calculate {
    ${SQOOP} import -D org.apache.sqoop.splitter.allow_text_splitter=true  \\
    --connect ${SOURCE_TABLE_JDBC_STR} \\
    --username ${SOURCE_TABLE_CON_USERNAME} \\
    --password ${SOURCE_TABLE_CON_PASSWORD} \\
    --table ${SOURCE_TABLE_NAME} -m 1 \\
    --columns ${TABLE_COLUMNS} \\
    --hcatalog-database ${DATABASE_NAME} \\
    --hcatalog-table ${TABLE_NAME} \\
    --hcatalog-partition-keys  "dt" \\
    --hcatalog-partition-values "${DATE}"
}
"""


def test_detect_sqoop_import_extracts_target_and_mysql_source() -> None:
    info = detect_sqoop_import(SQOOP_JOB)

    assert info is not None
    assert info.target.full_name == "default.ods_bach_baseinfo_shop_company"
    assert info.source_table == "company"
    assert info.source_connect == "${BACH_BASEINFO_SHOP_STR}"
    assert info.source_display == "${BACH_BASEINFO_SHOP_STR}.company"
    assert info.columns == [
        "id",
        "code",
        "name",
        "main_code",
        "state",
        "operator",
        "created_time",
        "updated_time",
    ]


def test_detect_sqoop_import_ignores_hive_sql() -> None:
    assert detect_sqoop_import("insert overwrite table default.dw_a select * from default.ods_b") is None


def test_build_sqoop_documentation_records_conservative_policy() -> None:
    info = detect_sqoop_import(SQOOP_JOB)
    assert info is not None

    markdown = build_sqoop_documentation(info)

    assert "`default.ods_bach_baseinfo_shop_company`" in markdown
    assert "`${BACH_BASEINFO_SHOP_STR}`" in markdown
    assert "`company`" in markdown
    assert "不写 DataHub upstreamLineage" in markdown
