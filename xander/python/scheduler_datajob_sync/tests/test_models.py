import base64
from datetime import datetime

from scheduler_datajob_sync.models import SchedulerJobMetadata, decode_maybe_base64


def test_scheduler_job_metadata_maps_mysql_row_and_normalizes_shell_command() -> None:
    row = {
        "id": 123,
        "job_name": "pdw_example_job",
        "job_display_name": "PDW_Example_Job",
        "job_owner_name": "alice",
        "job_proxy_user": "warehouse",
        "line_business_code": "pdw",
        "last_build_start_time": datetime(2026, 6, 10, 8, 30, 0),
        "created_time": datetime(2026, 1, 1, 0, 0, 0),
        "updated_time": datetime(2026, 6, 10, 9, 0, 0),
        "contacts_name": "alice,bob",
        "assigned_node": "node-a",
        "job_disable": "false",
        "job_priority": "5",
        "build_keep_days": "30",
        "build_keep_num": "100",
        "shell_commond": base64.b64encode(b"echo run job").decode("ascii"),
        "content": base64.b64encode(b"<xml>job</xml>").decode("ascii"),
        "upstream_jobs": '["upstream_a", "upstream_b"]',
        "upstream_jobs_conditions": "success",
        "delay_config": "0",
        "ivr_notify": "false",
        "sms_notify": "true",
        "im_notify": "true",
        "job_size": 2048,
        "job_count": 7,
        "build_update_time": datetime(2026, 6, 10, 8, 45, 0),
        "batch_exec_time": datetime(2026, 6, 10, 9, 10, 0),
    }

    metadata = SchedulerJobMetadata.from_mysql_row(row)

    assert metadata.id == 123
    assert metadata.job_display_name == "PDW_Example_Job"
    assert metadata.shell_command == "echo run job"
    assert metadata.upstream_jobs == ["upstream_a", "upstream_b"]
    assert metadata.last_build_start_time == datetime(2026, 6, 10, 8, 30, 0)
    assert metadata.content == "<xml>job</xml>"


def test_scheduler_job_metadata_converts_none_values_to_safe_defaults() -> None:
    metadata = SchedulerJobMetadata.from_mysql_row(
        {
            "id": None,
            "job_display_name": "job_without_optional_fields",
            "shell_commond": None,
            "upstream_jobs": "UNKNOWN_UPSTREAM_JOBS",
        }
    )

    assert metadata.id == 0
    assert metadata.job_name == ""
    assert metadata.job_display_name == "job_without_optional_fields"
    assert metadata.shell_command == ""
    assert metadata.upstream_jobs == []


def test_scheduler_job_metadata_treats_mysql_zero_datetime_as_empty() -> None:
    metadata = SchedulerJobMetadata.from_mysql_row(
        {
            "job_display_name": "job_with_zero_datetime",
            "last_build_start_time": "0000-00-00 00:00:00",
            "created_time": "0000-00-00 00:00:00",
            "updated_time": "0000-00-00 00:00:00",
            "build_update_time": "0000-00-00 00:00:00",
            "batch_exec_time": "0000-00-00 00:00:00",
        }
    )

    assert metadata.last_build_start_time is None
    assert metadata.created_time is None
    assert metadata.updated_time is None
    assert metadata.build_update_time is None
    assert metadata.batch_exec_time is None


def test_decode_maybe_base64_decodes_utf8_payload() -> None:
    encoded = base64.b64encode("echo hello".encode("utf-8")).decode("ascii")
    assert decode_maybe_base64(encoded) == "echo hello"


def test_decode_maybe_base64_returns_plain_text_when_not_base64() -> None:
    assert decode_maybe_base64("echo plain") == "echo plain"
