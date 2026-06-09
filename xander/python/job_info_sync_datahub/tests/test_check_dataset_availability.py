"""Dataset availability flag checks."""

from __future__ import annotations

import json

import pytest

from job_info_sync_datahub import check_dataset_availability as mod


def test_field_lineage_coverage_deduplicates_and_excludes_all_partition_fields() -> None:
    result = mod.AvailabilityResult(
        table_name="dw.target",
        dataset_urn="urn:dataset:dw.target",
        dataset_type="table",
    )
    schema_payload = {
        "schemaMetadata": {
            "value": {
                "fields": [
                    {"fieldPath": "id"},
                    {"fieldPath": "name"},
                    {"fieldPath": "version", "nativeDataType": "Partition Key"},
                    {"fieldPath": "biz_hour", "isPartitioningKey": True},
                ]
            }
        }
    }
    upstream_lineage_payload = {
        "upstreamLineage": {
            "value": {
                "fineGrainedLineages": [
                    {
                        "downstreams": [
                            "urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:hive,dw.target,PROD),id)"
                        ]
                    },
                    {
                        "downstreams": [
                            "urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:hive,dw.target,PROD),id)",
                            "urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:hive,dw.target,PROD),version)",
                            "urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:hive,dw.target,PROD),unexpected)",
                        ]
                    },
                ]
            }
        }
    }

    mod.apply_field_lineage_coverage(result, schema_payload, upstream_lineage_payload)

    assert result.ddl_non_partition_field_count == 2
    assert result.field_lineage_covered_field_count == 1
    assert result.field_lineage_coverage_percent == 50.0
    assert result.field_lineage_missing_fields == ["name"]
    assert result.partition_fields == ["biz_hour", "version"]


def test_format_field_lineage_coverage_summary_uses_non_partition_counts() -> None:
    assert mod.format_field_lineage_coverage_summary(
        [
            {
                "field_lineage_covered_field_count": 2,
                "ddl_non_partition_field_count": 4,
            },
            {
                "field_lineage_covered_field_count": 1,
                "ddl_non_partition_field_count": 2,
            },
        ]
    ) == "field_lineage_coverage=50.00% (3/6, 已过滤分区字段)"


def test_table_basic_check_requires_structured_properties_and_schema_fields() -> None:
    result = mod.evaluate_dataset_availability(
        table_name="dw.target",
        dataset_urn="urn:dataset:dw.target",
        is_view=False,
        structured_values={
            mod.URN_ETL_SCRIPT: "select * from ods.source",
            mod.URN_SCHEDULE_URL: "https://schedule/job/dw.target",
            mod.URN_EXECUTE_SHELL: "sh run.sh",
        },
        schema_field_count=2,
        view_logic="",
        documentation="",
        upstreams=set(),
        existing_flags=set(),
    )

    assert result.passed_flags == {"DDL"}
    assert "DDL: PASS" in result.reason


def test_table_basic_check_rejects_empty_structured_property() -> None:
    result = mod.evaluate_dataset_availability(
        table_name="dw.target",
        dataset_urn="urn:dataset:dw.target",
        is_view=False,
        structured_values={
            mod.URN_ETL_SCRIPT: "null",
            mod.URN_SCHEDULE_URL: "https://schedule/job/dw.target",
            mod.URN_EXECUTE_SHELL: "sh run.sh",
        },
        schema_field_count=2,
        view_logic="",
        documentation="",
        upstreams=set(),
        existing_flags=set(),
    )

    assert "DDL" not in result.passed_flags
    assert "DDL-STRUCTURED-PROPERTIES" in result.reason
    ddl_issues = [issue for issue in result.issues if issue.check == "DDL"]
    assert len(ddl_issues) == 1
    assert ddl_issues[0].property_label == "Etl Script"
    assert "占位符" in ddl_issues[0].message
    log_text = "\n".join(mod.format_result_log_lines(result))
    assert "[WARN]" in log_text
    assert "Etl Script" in log_text


def test_failure_reasons_include_rule_and_aspect_locations() -> None:
    result = mod.evaluate_dataset_availability(
        table_name="dw.target",
        dataset_urn="urn:dataset:dw.target",
        is_view=False,
        structured_values={},
        schema_field_count=0,
        view_logic="",
        documentation="",
        upstreams=set(),
        existing_flags=set(),
    )

    assert "DDL-STRUCTURED-PROPERTIES" in result.reason
    assert "structuredProperties: Etl Script" in result.reason
    assert "structuredProperties: Schedule URL" in result.reason
    assert "structuredProperties: Execute Shell" in result.reason
    assert "LINEAGE-DOC-SECTION" in result.reason
    assert "aspect=editableDatasetProperties field=description" in result.reason


def test_table_lineage_check_matches_documented_sources_with_unqualified_names() -> None:
    result = mod.evaluate_dataset_availability(
        table_name="dw.target",
        dataset_urn="urn:dataset:dw.target",
        is_view=False,
        structured_values={},
        schema_field_count=0,
        view_logic="",
        documentation="""
### 4. 数据来源

| 上游表 | 用途 |
| --- | --- |
| `ods_order_source_di` | 来源 |
| `dim.dim_city_info_df` | 维表 |

### 5. 使用到的上游表字段
""",
        upstreams={"default.ods_order_source_di", "dim.dim_city_info_df"},
        existing_flags=set(),
    )

    assert "表血缘" in result.passed_flags
    assert result.lineage_missing_upstreams == []
    assert result.lineage_extra_upstreams == []


def test_table_lineage_parser_ignores_inline_field_names_in_data_source_descriptions() -> None:
    result = mod.evaluate_dataset_availability(
        table_name="default.mid_sku_info_bach_1",
        dataset_urn="urn:dataset:default.mid_sku_info_bach_1",
        is_view=False,
        structured_values={},
        schema_field_count=0,
        view_logic="",
        documentation="""
### 4. 数据来源

主要上游表如下：

1. `pdw_bach_baseinfo_product_product_sku`
   商品 SKU 主数据。

2. `pdw_bach_baseinfo_product_product_spec`
   商品规格数据，按 `sku_code` 聚合生成销售规格。

3. `pdw_bach_baseinfo_product_default_value_rule`
   默认规则来源表。实际写入目标时仅使用 `sku_default_term`，特殊规则 CTE `sku_special_term` 未参与最终结果关联。
""",
        upstreams={
            "default.pdw_bach_baseinfo_product_product_sku",
            "default.pdw_bach_baseinfo_product_product_spec",
            "default.pdw_bach_baseinfo_product_default_value_rule",
        },
        existing_flags=set(),
    )

    assert "表血缘" in result.passed_flags
    assert result.lineage_missing_upstreams == []
    assert result.lineage_extra_upstreams == []


def test_table_lineage_parser_ignores_api_names_and_urls_in_data_sources() -> None:
    result = mod.evaluate_dataset_availability(
        table_name="data_takeaway.pdw_takeaway_tp_night_aftersale_order_process_detail_di",
        dataset_urn="urn:dataset:data_takeaway.pdw_takeaway_tp_night_aftersale_order_process_detail_di",
        is_view=False,
        structured_values={},
        schema_field_count=0,
        view_logic="",
        documentation="""
### 4. 数据来源

主要数据来源包括：

1. `data_takeaway.pdw_takeaway_store_operating_state_info_di`
   - 用于获取当前可运营且美团或饿了么售卖状态有效的门店清单。

2. 饿了么开放接口
   - `order.reverse.unprocessedlist`
   - `order.reverse.process`

3. 美团开放接口
   - `https://waimaiopen.meituan.com/api/v1/ecommerce/order/getAfterSaleOrders`
   - `https://waimaiopen.meituan.com/api/v1/order/refund/reject`

### 5. 使用到的上游表字段
""",
        upstreams={"data_takeaway.pdw_takeaway_store_operating_state_info_di"},
        existing_flags=set(),
    )

    assert "表血缘" in result.passed_flags
    assert result.lineage_documented_upstreams == ["data_takeaway.pdw_takeaway_store_operating_state_info_di"]
    assert result.lineage_missing_upstreams == []
    assert result.lineage_extra_upstreams == []


def test_parse_documented_upstreams_unescapes_markdown_table_cells() -> None:
    _, upstreams = mod.parse_documented_upstreams(
        """
### 4\\. 数据来源

| 上游表 | 用途 | 是否 Hive 表 |
| --- | --- | --- |
| data\_md.dm\_md\_dim\_base\_sku\_info\_base\_sku\_v1 | 商品维表 | 是 |
| <span style="font-size:13px">data\_md.dm\_md\_features\_12\_class\_sku\_tag\_info\_sku\_v1</span> | 标签 | 是 |
"""
    )

    assert upstreams == {
        "data_md.dm_md_dim_base_sku_info_base_sku_v1",
        "data_md.dm_md_features_12_class_sku_tag_info_sku_v1",
    }


def test_parse_documented_upstreams_defaults_db_and_rejects_invalid_table_names() -> None:
    _, upstreams = mod.parse_documented_upstreams(
        """
### 4. 数据来源

1. `pdw_order_detail_di`
2. `ods.ods_order_detail_di`
3. `foo_order_detail_di`
4. `pdw-order-detail-di`
5. `_pdw_order_detail_di`
6. `1pdw_order_detail_di`
7. `pdw_order`
8. `order.reverse.process`
9. `https://example.com/order`

### 5. 使用到的上游表字段
"""
    )

    assert upstreams == {"default.pdw_order_detail_di", "ods.ods_order_detail_di"}


def test_table_lineage_normalizes_not_verified_tables_and_ignores_target_table() -> None:
    result = mod.evaluate_dataset_availability(
        table_name="default.dim_sku_info",
        dataset_urn="urn:dataset:default.dim_sku_info",
        is_view=False,
        structured_values={},
        schema_field_count=0,
        view_logic="",
        documentation="""
### 4. 数据来源

主要上游表如下：

1. `mid_sku_info_hd`
   - 用于提供历史商品 SKU 基准数据。

2. `mid_sku_info_bach_1`
   - 用于提供每日新版商品信息。

3. `default.dim_sku_info`
   - 在 `do_check` 中读取上一日分区。
   - 用于校验当日待验证分区数据量。

4. `not_verified_dim_sku_info`
   - 核心 SQL 的写入表。
   - 在 `do_check` 中作为待校验数据源读取当日分区。

### 5. 使用到的上游表字段
""",
        upstreams={"default.mid_sku_info_hd", "default.mid_sku_info_bach_1"},
        existing_flags=set(),
    )

    assert "表血缘" in result.passed_flags
    assert result.lineage_documented_upstreams == [
        "default.mid_sku_info_bach_1",
        "default.mid_sku_info_hd",
    ]
    assert result.lineage_missing_upstreams == []
    assert result.lineage_extra_upstreams == []


def test_table_lineage_ignores_target_table_from_existing_upstreams() -> None:
    result = mod.evaluate_dataset_availability(
        table_name="default.pdw_bach_valeera_book_particulars_v1",
        dataset_urn="urn:dataset:default.pdw_bach_valeera_book_particulars_v1",
        is_view=False,
        structured_values={},
        schema_field_count=0,
        view_logic="",
        documentation="""
### 4. 数据来源

主要上游表如下：

1. `ods_bach_valeera_book_particulars_v1`
   - 读取当前调度日分区。

2. `pdw_bach_valeera_book_particulars_v1`
   - 从前一日历史分区中读取更早 booking_date 数据。

### 5. 使用到的上游表字段
""",
        upstreams={
            "default.ods_bach_valeera_book_particulars_v1",
            "default.pdw_bach_valeera_book_particulars_v1",
        },
        existing_flags=set(),
    )

    assert "表血缘" in result.passed_flags
    assert result.lineage_documented_upstreams == ["default.ods_bach_valeera_book_particulars_v1"]
    assert result.lineage_existing_upstreams == ["default.ods_bach_valeera_book_particulars_v1"]
    assert result.lineage_extra_upstreams == []


def test_table_lineage_check_reports_documentation_diff() -> None:
    result = mod.evaluate_dataset_availability(
        table_name="dw.target",
        dataset_urn="urn:dataset:dw.target",
        is_view=False,
        structured_values={},
        schema_field_count=0,
        view_logic="",
        documentation="""
### 4. 数据来源

| 上游表 | 用途 |
| --- | --- |
| `ods.ods_expected_order_di` | 来源 |
""",
        upstreams={"ods.ods_actual_order_di"},
        existing_flags=set(),
    )

    assert "表血缘" not in result.passed_flags
    assert result.lineage_missing_upstreams == ["ods.ods_expected_order_di"]
    assert result.lineage_extra_upstreams == ["ods.ods_actual_order_di"]


def test_view_checks_use_view_logic_and_any_upstream() -> None:
    result = mod.evaluate_dataset_availability(
        table_name="dw.some_view",
        dataset_urn="urn:dataset:dw.some_view",
        is_view=True,
        structured_values={},
        schema_field_count=0,
        view_logic="select * from ods.source",
        documentation="",
        upstreams={"ods.source"},
        existing_flags=set(),
    )

    assert result.passed_flags == {"DDL", "表血缘"}


def test_deprecated_dataset_is_marked_available_without_content_checks() -> None:
    result = mod.evaluate_dataset_availability(
        table_name="dw.deprecated_table",
        dataset_urn="urn:dataset:dw.deprecated_table",
        is_view=False,
        is_deprecated=True,
        structured_values={},
        schema_field_count=0,
        view_logic="",
        documentation="",
        upstreams=set(),
        existing_flags=set(),
    )

    assert result.passed_flags == {"DDL", "表血缘"}
    assert result.final_flags == ["DDL", "表血缘"]
    assert result.reason == "Dataset 已废弃，跳过可用性检查"


def test_check_one_dataset_skips_content_aspects_for_deprecated_dataset(monkeypatch) -> None:
    def fake_fetch_structured_properties_or_empty(gms_url, dataset_urn, token=None):
        return {"structuredProperties": {"value": {"properties": []}}}

    def fake_fetch_aspect_payload(gms_url, dataset_urn, aspect_name, token=None, timeout_sec=60):
        if aspect_name == "deprecation":
            return {"deprecation": {"value": {"deprecated": True}}}
        raise AssertionError(f"unexpected content aspect fetch: {aspect_name}")

    monkeypatch.setattr(mod, "fetch_structured_properties_or_empty", fake_fetch_structured_properties_or_empty)
    monkeypatch.setattr(mod, "fetch_aspect_payload", fake_fetch_aspect_payload)

    result = mod.check_one_dataset(
        "dw.deprecated_table",
        gms_url="http://gms",
        token=None,
        platform_instance="blf-prod-hive",
        env="PROD",
        dry_run=True,
    )

    assert result.passed_flags == {"DDL", "表血缘"}
    assert result.write_status == "DRY_RUN"


def test_discover_table_names_by_prefix_from_mysql_matches_table_segment_only(monkeypatch) -> None:
    output = "\n".join(
        [
            "urn:li:dataset:(urn:li:dataPlatform:hive,blf-prod-hive.ods.pdw_target,PROD)",
            "urn:li:dataset:(urn:li:dataPlatform:hive,blf-prod-hive.pdw.dim_store,PROD)",
            "urn:li:dataset:(urn:li:dataPlatform:hive,blf-prod-hive.default.PDW_OTHER,PROD)",
            "urn:li:dataset:(urn:li:dataPlatform:hive,other.default.pdw_skip,PROD)",
            "urn:li:dataset:(urn:li:dataPlatform:hive,blf-prod-hive.default.pdw_dev,DEV)",
        ]
    )

    monkeypatch.setattr(mod, "_run_mysql_query", lambda sql: output)

    assert mod.discover_table_names_by_prefix_from_mysql(
        "pdw",
        platform_instance="blf-prod-hive",
        env="PROD",
    ) == ["default.pdw_other", "ods.pdw_target"]


def test_main_uses_table_prefix_when_explicit_table_names_are_empty(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    monkeypatch.delenv("TABLE_NAMES", raising=False)
    monkeypatch.setattr(
        mod,
        "discover_table_names_by_prefix_from_mysql",
        lambda table_prefix, *, platform_instance, env: ["default.pdw_target"],
    )

    def fake_run(table_names, **kwargs):
        captured["table_names"] = table_names
        captured["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(mod, "run", fake_run)

    exit_code = mod.main(
        [
            "--table-prefix",
            "pdw",
            "--jsonl",
            str(tmp_path / "report.jsonl"),
            "--xlsx",
            str(tmp_path / "report.xlsx"),
        ]
    )

    assert exit_code == 0
    assert captured["table_names"] == ["default.pdw_target"]


def test_set_available_flags_one_dataset_writes_exact_ddl_and_table_lineage(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        mod,
        "fetch_structured_properties_or_empty",
        lambda gms_url, dataset_urn, token=None: {
            "structuredProperties": {
                "value": {
                    "properties": [
                        {
                            "propertyUrn": mod.URN_DATA_AVAILABILITY_FLAG,
                            "values": [{"string": "字段血缘"}],
                        }
                    ]
                }
            }
        },
    )

    def fake_patch(gms_url, dataset_urn, flags, token=None):
        captured["dataset_urn"] = dataset_urn
        captured["flags"] = flags

    monkeypatch.setattr(mod, "patch_data_availability_flags", fake_patch)
    monkeypatch.setattr(mod, "fetch_aspect_payload", lambda *args, **kwargs: {})

    result = mod.set_available_flags_one_dataset(
        "dw.target",
        gms_url="http://gms",
        token=None,
        platform_instance="blf-prod-hive",
        env="PROD",
        dry_run=False,
    )

    assert captured["dataset_urn"] == "urn:li:dataset:(urn:li:dataPlatform:hive,blf-prod-hive.dw.target,PROD)"
    assert captured["flags"] == ["DDL", "表血缘"]
    assert result.existing_flags == {"字段血缘"}
    assert result.final_flags == ["DDL", "表血缘"]
    assert result.write_status == "UPDATED"


def test_set_available_flags_one_dataset_uses_requested_flags(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        mod,
        "fetch_structured_properties_or_empty",
        lambda gms_url, dataset_urn, token=None: {
            "structuredProperties": {
                "value": {
                    "properties": [
                        {
                            "propertyUrn": mod.URN_DATA_AVAILABILITY_FLAG,
                            "values": [{"string": "表血缘"}],
                        }
                    ]
                }
            }
        },
    )

    def fake_patch(gms_url, dataset_urn, flags, token=None):
        captured["flags"] = flags

    monkeypatch.setattr(mod, "patch_data_availability_flags", fake_patch)
    monkeypatch.setattr(mod, "fetch_aspect_payload", lambda *args, **kwargs: {})

    result = mod.set_available_flags_one_dataset(
        "dw.target",
        gms_url="http://gms",
        token=None,
        platform_instance="blf-prod-hive",
        env="PROD",
        dry_run=False,
        target_flags=["字段血缘", "DDL"],
    )

    assert captured["flags"] == ["DDL", "字段血缘"]
    assert result.final_flags == ["DDL", "字段血缘"]
    assert result.reason == '手工设置 Data Availability Flag = ["DDL", "字段血缘"]'


def test_parse_requested_available_flags_normalizes_and_validates() -> None:
    assert mod.parse_requested_available_flags("字段血缘, DDL") == ["DDL", "字段血缘"]
    assert mod.parse_requested_available_flags("DDL\n表血缘\n字段血缘") == [
        "DDL",
        "表血缘",
        "字段血缘",
    ]


def test_parse_requested_available_flags_rejects_unknown_flag() -> None:
    with pytest.raises(ValueError, match="不支持的 Data Availability Flag"):
        mod.parse_requested_available_flags("DDL,未知")


def test_main_set_available_flags_requires_explicit_table_names(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("TABLE_NAMES", raising=False)
    monkeypatch.setattr(
        mod,
        "discover_table_names_by_prefix_from_mysql",
        lambda table_prefix, *, platform_instance, env: ["default.pdw_target"],
    )

    exit_code = mod.main(
        [
            "--set-available-flags",
            "--table-prefix",
            "pdw",
            "--jsonl",
            str(tmp_path / "report.jsonl"),
            "--xlsx",
            str(tmp_path / "report.xlsx"),
        ]
    )

    assert exit_code == 2


def test_merge_availability_flags_preserves_existing_field_lineage() -> None:
    assert mod.merge_availability_flags({"字段血缘"}, {"DDL", "表血缘"}) == [
        "DDL",
        "表血缘",
        "字段血缘",
    ]


def test_patch_data_availability_flags_writes_multiple_values(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        status = 200

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    def fake_urlopen(req, timeout):
        captured["timeout"] = timeout
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["method"] = req.get_method()
        return FakeResponse()

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)

    mod.patch_data_availability_flags(
        "http://gms",
        "urn:li:dataset:(urn:li:dataPlatform:hive,blf-prod-hive.dw.target,PROD)",
        ["DDL", "表血缘", "字段血缘"],
        token=None,
    )

    assert captured["method"] == "PATCH"
    assert captured["body"] == {
        "patch": [
            {
                "op": "add",
                "path": f"/properties/{mod.URN_DATA_AVAILABILITY_FLAG}",
                "value": {
                    "propertyUrn": mod.URN_DATA_AVAILABILITY_FLAG,
                    "values": [
                        {"string": "DDL"},
                        {"string": "表血缘"},
                        {"string": "字段血缘"},
                    ],
                },
            }
        ],
        "arrayPrimaryKeys": {"properties": ["propertyUrn"]},
    }
