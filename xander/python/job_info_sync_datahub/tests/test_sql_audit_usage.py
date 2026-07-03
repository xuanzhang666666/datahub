"""Tests for SQL audit usage parsing and aggregation."""

from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from job_info_sync_datahub.sql_audit_usage import (
    AuditSqlRecord,
    FileOperationCheckpoint,
    OperationEvent,
    build_query_candidates,
    dataset_usage_delete_query,
    aggregate_usage,
    filter_new_operations,
    fingerprint_sql,
    merge_usage_statistics,
    normalize_sql_sample,
    operation_key,
    parse_audit_record,
    split_sql_statements,
    usage_bucket_window,
    _fetch_trino_records,
)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class FakeCursor:
    def __init__(self) -> None:
        self.sql: str | None = None
        self.params: tuple[object, ...] | None = None

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.sql = sql
        self.params = params

    def fetchall(self) -> list[dict[str, object]]:
        return []


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = FakeCursor()

    def cursor(self) -> FakeCursor:
        return self.cursor_instance


class SqlAuditUsageTest(unittest.TestCase):
    def test_hive_multistatement_filters_noise_and_keeps_effective_sql(self) -> None:
        record = AuditSqlRecord(
            engine="hive",
            user="wstats",
            source="EXEC",
            query_id=None,
            executed_at="2026-07-01 20:02:23",
            sql_text=_b64(
                """
                use data_smartorder;
                set hive.execution.engine=tez;
                add jar hdfs://wormpexdata/user/wstats/udf/brickhouse.jar;
                insert overwrite table pdw_target partition(dt='20260701')
                select id, amount from ods_source where dt='20260701';
                select count(*) from pdw_target where dt='20260701';
                """
            ),
            sql_is_base64=True,
        )

        parsed = parse_audit_record(record)

        self.assertEqual([stmt.kind for stmt in parsed.statements], ["insert", "select"])
        self.assertEqual(parsed.statements[0].write_tables, {"default.pdw_target"})
        self.assertEqual(parsed.statements[0].read_tables, {"default.ods_source"})
        self.assertEqual(parsed.statements[1].read_tables, {"default.pdw_target"})
        self.assertEqual(parsed.report.filtered_statements, 3)

    def test_missing_database_defaults_to_default_without_use_statement(self) -> None:
        record = AuditSqlRecord(
            engine="trino",
            user="hive",
            source="trino-jdbc",
            query_id="20260701_1",
            executed_at="2026-07-01 20:00:00",
            sql_text="select store_id from dim_store_info where dt='20260701'",
        )

        parsed = parse_audit_record(record)

        self.assertEqual(parsed.statements[0].read_tables, {"default.dim_store_info"})

    def test_alter_table_concatenate_is_operation_without_generic_regex_lineage(self) -> None:
        record = AuditSqlRecord(
            engine="hive",
            user="wstats",
            source="EXEC",
            query_id=None,
            executed_at="2026-07-01 04:00:00",
            sql_text=(
                "alter table pdw_opc_roster_roster_detail "
                "partition (dt='20260630') concatenate"
            ),
        )

        parsed = parse_audit_record(record)

        self.assertEqual(parsed.statements[0].kind, "alter")
        self.assertEqual(
            parsed.statements[0].write_tables,
            {"default.pdw_opc_roster_roster_detail"},
        )
        self.assertEqual(parsed.statements[0].read_tables, set())

    def test_msck_repair_table_is_operation_without_sqlglot_command_fallback(self) -> None:
        record = AuditSqlRecord(
            engine="hive",
            user="wstats",
            source="EXEC",
            query_id=None,
            executed_at="2026-07-01 04:00:00",
            sql_text="msck repair table data_md.dm_md_features_67_class_sku_tag_info_sku_processed",
        )

        parsed = parse_audit_record(record)

        self.assertEqual(parsed.statements[0].kind, "msck")
        self.assertEqual(
            parsed.statements[0].write_tables,
            {"data_md.dm_md_features_67_class_sku_tag_info_sku_processed"},
        )
        self.assertEqual(parsed.statements[0].read_tables, set())
        self.assertEqual(parsed.report.failed_statements, 0)

    def test_incomplete_msck_repair_table_fails_without_table_regex_fallback(self) -> None:
        record = AuditSqlRecord(
            engine="hive",
            user="wstats",
            source="EXEC",
            query_id=None,
            executed_at="2026-07-01 04:00:00",
            sql_text="msck repair table",
        )

        parsed = parse_audit_record(record)

        self.assertEqual(parsed.statements, [])
        self.assertEqual(parsed.report.failed_statements, 1)

    def test_parse_failure_uses_used_tables_for_table_usage_only(self) -> None:
        record = AuditSqlRecord(
            engine="hive",
            user="wstats",
            source="EXEC",
            query_id=None,
            executed_at="2026-07-01 04:00:00",
            sql_text="select * from where",
            used_tables="dm_order.fact_order,dim_store",
        )

        parsed = parse_audit_record(record)

        self.assertEqual(parsed.report.failed_statements, 1)
        self.assertEqual(parsed.report.used_tables_fallback_statements, 1)
        self.assertEqual(
            {next(iter(stmt.read_tables)) for stmt in parsed.statements},
            {"dm_order.fact_order", "default.dim_store"},
        )
        self.assertEqual([stmt.fields_by_table for stmt in parsed.statements], [{}, {}])

    def test_fingerprint_normalizes_dates_partitions_and_ids(self) -> None:
        first = (
            "insert overwrite table dm.user_tag partition(dt='20260701') "
            "select * from dwd.user_behavior where dt='20260701' and store_id in (123,456)"
        )
        second = (
            "insert overwrite table dm.user_tag partition(dt='20260702') "
            "select * from dwd.user_behavior where dt='20260702' and store_id in (789,456)"
        )

        self.assertEqual(fingerprint_sql(first), fingerprint_sql(second))

    def test_aggregate_usage_separates_reads_operations_and_failed_queries(self) -> None:
        hive = parse_audit_record(
            AuditSqlRecord(
                engine="hive",
                user="wstats",
                source="EXEC",
                query_id=None,
                executed_at="2026-07-01 08:00:00",
                sql_text=_b64(
                    "insert overwrite table dm.user_tag partition(dt='20260701') "
                    "select user_id from dwd.user_behavior where dt='20260701'"
                ),
                sql_is_base64=True,
            )
        )
        trino = parse_audit_record(
            AuditSqlRecord(
                engine="trino",
                user="jingliang.zhang",
                source="trino-jdbc",
                query_id="20260701_abc",
                executed_at="2026-07-01 09:00:00",
                sql_text="select user_id from dwd.user_behavior where dt='20260701'",
                state="FINISHED",
                raw_input_bytes=1024,
                duration_seconds=2.5,
            )
        )
        failed = parse_audit_record(
            AuditSqlRecord(
                engine="trino",
                user="hive",
                source="trino-jdbc",
                query_id="20260701_failed",
                executed_at="2026-07-01 10:00:00",
                sql_text="select * from dwd.user_behavior",
                state="FAILED",
            )
        )

        usage = aggregate_usage([hive, trino, failed])

        self.assertEqual(usage.datasets["dwd.user_behavior"].query_count, 2)
        self.assertEqual(usage.datasets["dwd.user_behavior"].users["wstats"], 1)
        self.assertEqual(usage.datasets["dwd.user_behavior"].users["jingliang.zhang"], 1)
        self.assertEqual(usage.datasets["dwd.user_behavior"].fingerprints.total(), 2)
        self.assertEqual(usage.operations[0].table, "dm.user_tag")
        self.assertEqual(usage.operations[0].operation_type, "INSERT")
        self.assertEqual(usage.failed_queries, 1)

    def test_normalize_sql_sample_redacts_literals_without_hiding_structure(self) -> None:
        sample = normalize_sql_sample(
            "select user_id, phone from dm_order.dw_order_v1 "
            "where dt='20260630' and store_code in ('10001','10002') "
            "and user_id = 123456"
        )

        self.assertIn("dm_order.dw_order_v1", sample)
        self.assertIn("store_code in (?, ?)", sample)
        self.assertIn("user_id = ?", sample)
        self.assertNotIn("20260630", sample)
        self.assertNotIn("10001", sample)
        self.assertNotIn("123456", sample)

    def test_query_candidates_include_high_frequency_and_core_table_queries(self) -> None:
        records = [
            parse_audit_record(
                AuditSqlRecord(
                    engine="hive",
                    user="wstats",
                    source="EXEC",
                    query_id=None,
                    executed_at=f"2026-07-01 0{i}:00:00",
                    sql_text=(
                        "select order_no from dw_order_v1 "
                        f"where dt='2026070{i}' and store_code='{10000 + i}'"
                    ),
                )
            )
            for i in range(1, 4)
        ]
        records.append(
            parse_audit_record(
                AuditSqlRecord(
                    engine="trino",
                    user="report",
                    source="trino-jdbc",
                    query_id="q1",
                    executed_at="2026-07-01 10:00:00",
                    sql_text="select sku_code from dim_sku where dt='20260701'",
                )
            )
        )
        usage = aggregate_usage(records)

        candidates = build_query_candidates(
            usage,
            platform_instance="blf-prod-hive",
            env="PROD",
            top_n=1,
            core_table_top_n=1,
            per_core_table=1,
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].query_count, 3)
        self.assertEqual(candidates[0].subjects, {"default.dw_order_v1"})
        self.assertNotIn("20260701", candidates[0].sample_sql)

    def test_merge_usage_statistics_replaces_same_day_and_keeps_other_days(self) -> None:
        existing = [
            {
                "timestampMillis": 1782691200000,
                "totalSqlQueries": 77,
                "uniqueUserCount": 1,
                "userCounts": [{"user": "urn:li:corpuser:wstats", "count": 77}],
                "fieldCounts": [{"fieldPath": "dt", "count": 11}],
            },
            {
                "timestampMillis": 1782777600000,
                "totalSqlQueries": 10,
                "uniqueUserCount": 1,
                "userCounts": [{"user": "urn:li:corpuser:wstats", "count": 10}],
            },
        ]
        current = {
            "timestampMillis": 1782777600000,
            "totalSqlQueries": 78,
            "uniqueUserCount": 1,
            "userCounts": [{"user": "urn:li:corpuser:wstats", "count": 78}],
            "fieldCounts": [{"fieldPath": "order_no", "count": 9}],
        }

        merged = merge_usage_statistics(existing, current)

        self.assertEqual([item["timestampMillis"] for item in merged], [1782691200000, 1782777600000])
        self.assertEqual([item["totalSqlQueries"] for item in merged], [77, 78])
        self.assertNotIn("fieldCounts", merged[0])
        self.assertNotIn("fieldCounts", merged[1])

    def test_usage_bucket_window_covers_one_utc_day_without_touching_next_day(self) -> None:
        start, end = usage_bucket_window("2026-06-30")

        self.assertEqual(int(start.timestamp() * 1000), 1782777600000)
        self.assertEqual(int(end.timestamp() * 1000), 1782863999999)

    def test_dataset_usage_delete_query_is_limited_to_day_platform_and_env(self) -> None:
        query = dataset_usage_delete_query(
            platform_instance="blf-prod-hive",
            env="PROD",
            start_ms=1782777600000,
            end_ms=1782863999999,
        )

        filters = query["query"]["bool"]["filter"]  # type: ignore[index]
        self.assertIn(
            {"range": {"timestampMillis": {"gte": 1782777600000, "lte": 1782863999999}}},
            filters,
        )
        self.assertIn(
            {
                "wildcard": {
                    "urn": "urn:li:dataset:(urn:li:dataPlatform:hive,blf-prod-hive.*PROD)"
                }
            },
            filters,
        )

    def test_split_sql_statements_ignores_semicolon_inside_string(self) -> None:
        statements = split_sql_statements(
            "select ';' as semi from default.t1; select * from default.t2;"
        )

        self.assertEqual(
            statements,
            [
                "select ';' as semi from default.t1",
                "select * from default.t2",
            ],
        )

    def test_field_usage_ignores_string_like_double_quoted_constants(self) -> None:
        parsed = parse_audit_record(
            AuditSqlRecord(
                engine="trino",
                user="hive",
                source="trino-jdbc",
                query_id="20260701_2",
                executed_at="2026-07-01 20:00:00",
                sql_text=(
                    'select case when pay_type_name="无" then 1 end as flag '
                    "from data_finance.dwa_finance_ar_invoice_rent_detail_da_v1 "
                    'where dt="20260630"'
                ),
            )
        )

        self.assertEqual(
            parsed.statements[0].fields_by_table[
                "data_finance.dwa_finance_ar_invoice_rent_detail_da_v1"
            ],
            {"pay_type_name", "dt"},
        )

    def test_fetch_trino_records_uses_create_time_index_range(self) -> None:
        conn = FakeConnection()

        records = _fetch_trino_records(conn, "2026-07-01", limit=10)

        self.assertEqual(records, [])
        assert conn.cursor_instance.sql is not None
        self.assertIn("FORCE INDEX(idx_create_time)", conn.cursor_instance.sql)
        self.assertIn("WHERE create_time >= %s AND create_time < %s", conn.cursor_instance.sql)
        self.assertNotIn("WHERE dt=%s", conn.cursor_instance.sql)
        self.assertEqual(
            conn.cursor_instance.params,
            ("2026-07-01 00:00:00", "2026-07-02 00:00:00", 10),
        )

    def test_operation_key_is_stable_for_same_event(self) -> None:
        op = OperationEvent(
            table="default.dm_user_tag",
            operation_type="INSERT",
            user="wstats",
            source="EXEC",
            engine="hive",
            executed_at="2026-07-01 08:00:00",
            fingerprint="abc123",
            query_id=None,
        )

        self.assertEqual(operation_key(op), operation_key(op))

    def test_file_checkpoint_filters_previously_written_operations(self) -> None:
        op = OperationEvent(
            table="default.dm_user_tag",
            operation_type="INSERT",
            user="wstats",
            source="EXEC",
            engine="hive",
            executed_at="2026-07-01 08:00:00",
            fingerprint="abc123",
            query_id=None,
        )
        other = OperationEvent(
            table="default.dm_user_tag",
            operation_type="ALTER",
            user="wstats",
            source="EXEC",
            engine="hive",
            executed_at="2026-07-01 09:00:00",
            fingerprint="def456",
            query_id=None,
        )

        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = FileOperationCheckpoint(Path(tmp) / "operation_keys.jsonl")
            checkpoint.mark_written(op)

            self.assertEqual(filter_new_operations([op, other], checkpoint), [other])


if __name__ == "__main__":
    unittest.main()
