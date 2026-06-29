from __future__ import annotations

from job_info_sync_datahub.audit_schedule_retry_config import (
    FIXED_DELAY_CLASS,
    JOB_CONTENT_XML_URN,
    _write_csv,
    build_downstream_counts,
    classify_datajob,
)


def _entity(xml: str | None) -> dict:
    properties = []
    if xml is not None:
        properties.append(
            {
                "structuredProperty": {"urn": JOB_CONTENT_XML_URN},
                "values": [{"stringValue": xml}],
            }
        )
    return {
        "urn": (
            "urn:li:dataJob:(urn:li:dataFlow:"
            "(blf-schedule,blf-schedule,PROD),dw_order_v1_archive_warm)"
        ),
        "jobId": "dw_order_v1_archive_warm",
        "properties": {
            "name": "dw_order_v1_archive_warm",
            "customProperties": [],
        },
        "structuredProperties": {"properties": properties},
    }


def test_classify_datajob_detects_fixed_delay() -> None:
    result = classify_datajob(
        _entity(f'<delay class="{FIXED_DELAY_CLASS}"><delay>2</delay></delay>')
    )

    assert result.status == "configured"
    assert result.has_fixed_delay is True


def test_classify_datajob_marks_missing_fixed_delay() -> None:
    result = classify_datajob(_entity("<project><publishers /></project>"))

    assert result.status == "not_configured"
    assert result.has_fixed_delay is False


def test_classify_datajob_marks_missing_xml_as_unknown() -> None:
    result = classify_datajob(_entity(None))

    assert result.status == "missing_job_xml"
    assert result.has_fixed_delay is False


def test_classify_datajob_adds_prefix_and_downstream_count() -> None:
    result = classify_datajob(
        _entity("<project><publishers /></project>"),
        downstream_job_count=3,
    )

    assert result.job_name_prefix == "dw"
    assert result.downstream_job_count == 3


def test_build_downstream_counts_includes_direct_and_indirect_jobs() -> None:
    root = _entity("<project />")
    direct = _entity("<project />")
    direct["jobId"] = "dwd_order"
    direct["properties"] = {
        "name": "dwd_order",
        "customProperties": [
            {"key": "upstream_jobs", "value": "dw_order_v1_archive_warm"}
        ],
    }
    indirect = _entity("<project />")
    indirect["jobId"] = "ads_order"
    indirect["properties"] = {
        "name": "ads_order",
        "customProperties": [{"key": "upstream_jobs", "value": "dwd_order"}],
    }

    counts = build_downstream_counts([root, direct, indirect])

    assert counts["dw_order_v1_archive_warm"] == 2
    assert counts["dwd_order"] == 1


def test_write_csv_supports_empty_result(tmp_path) -> None:
    output = tmp_path / "empty.csv"

    _write_csv(output, [])

    assert output.read_text(encoding="utf-8-sig").startswith("job_display_name,")
