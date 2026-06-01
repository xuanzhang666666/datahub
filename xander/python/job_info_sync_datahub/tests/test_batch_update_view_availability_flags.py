"""batch_update_view_availability_flags eligibility and patch helpers."""

from __future__ import annotations

from job_info_sync_datahub import batch_update_view_availability_flags as mod
from job_info_sync_datahub.export_view_datasets_report import ViewDatasetExportRow


def test_filter_eligible_views() -> None:
    rows = [
        ViewDatasetExportRow("a.v1", 1, "DDL, 表血缘", "是"),
        ViewDatasetExportRow("a.v2", 0, "-", "是"),
        ViewDatasetExportRow("a.v3", 2, "-", "否"),
        ViewDatasetExportRow("a.v4", 3, "-", "是"),
    ]
    eligible = mod.filter_eligible_views(rows)
    assert [e.view_name for e in eligible] == ["a.v1", "a.v4"]


def test_update_one_view_flag_patches_three_flags(monkeypatch) -> None:
    patched: list[list[str]] = []

    def fake_patch(_gms: str, _urn: str, flags: list[str], token=None) -> None:  # noqa: ANN001
        patched.append(flags)

    monkeypatch.setattr(mod, "patch_data_availability_flags", fake_patch)
    monkeypatch.setattr(
        mod,
        "fetch_structured_properties_or_empty",
        lambda *args, **kwargs: {"structuredProperties": {"value": {"properties": []}}},
    )

    result = mod.update_one_view_flag(
        "data_build.pdw_opc_flag_flag_user",
        gms_url="http://localhost:8080",
        token=None,
        platform_instance="blf-prod-hive",
        env="PROD",
        dry_run=False,
        target_flags=mod.VIEW_FULL_AVAILABILITY_FLAGS,
    )

    assert result.write_status == "UPDATED"
    assert patched == [["DDL", "表血缘", "字段血缘"]]


def test_update_one_view_flag_no_change_when_already_set(monkeypatch) -> None:
    monkeypatch.setattr(
        mod,
        "fetch_structured_properties_or_empty",
        lambda *args, **kwargs: {
            "structuredProperties": {
                "value": {
                    "properties": [
                        {
                            "propertyUrn": (
                                "urn:li:structuredProperty:"
                                "blf.data.warehouse.data_availability_flag"
                            ),
                            "values": [
                                {"string": "DDL"},
                                {"string": "表血缘"},
                                {"string": "字段血缘"},
                            ],
                        }
                    ]
                }
            }
        },
    )
    monkeypatch.setattr(
        mod,
        "patch_data_availability_flags",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not patch")),
    )

    result = mod.update_one_view_flag(
        "data_build.pdw_opc_flag_flag_user",
        gms_url="http://localhost:8080",
        token=None,
        platform_instance="blf-prod-hive",
        env="PROD",
        dry_run=False,
        target_flags=mod.VIEW_FULL_AVAILABILITY_FLAGS,
    )
    assert result.write_status == "NO_CHANGE"
