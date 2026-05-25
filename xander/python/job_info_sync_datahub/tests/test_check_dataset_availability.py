"""Dataset availability flag checks."""

from __future__ import annotations

import json

from job_info_sync_datahub import check_dataset_availability as mod


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
            mod.URN_ETL_SCRIPT: "无",
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
    assert "structuredProperties: Etl Script" in result.reason
    assert mod.URN_ETL_SCRIPT in result.reason


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
    assert "editableDatasetProperties.description" in result.reason


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
| `ods_source` | 来源 |
| `dim.dim_city` | 维表 |

### 5. 使用到的上游表字段
""",
        upstreams={"default.ods_source", "dim.dim_city"},
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
| `ods.expected` | 来源 |
""",
        upstreams={"ods.actual"},
        existing_flags=set(),
    )

    assert "表血缘" not in result.passed_flags
    assert result.lineage_missing_upstreams == ["ods.expected"]
    assert result.lineage_extra_upstreams == ["ods.actual"]


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
