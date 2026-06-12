from __future__ import annotations

import urllib.parse

import pytest

from blf_datahub_mcp.schedule import (
    job_base,
    make_datahub_datajob_url,
    make_scheduler_datajob_urn,
    scheduler_web_url,
)


def test_make_scheduler_datajob_urn_builds_correct_urn() -> None:
    urn = make_scheduler_datajob_urn("dm_app_logistics_work_piece_di_v2")
    assert urn == (
        "urn:li:dataJob:(urn:li:dataFlow:(blf-schedule,blf-schedule,PROD),"
        "dm_app_logistics_work_piece_di_v2)"
    )


def test_make_scheduler_datajob_urn_strips_whitespace() -> None:
    urn = make_scheduler_datajob_urn("  my_job  ")
    assert "my_job" in urn
    assert "  " not in urn


def test_make_scheduler_datajob_urn_rejects_empty() -> None:
    with pytest.raises(ValueError):
        make_scheduler_datajob_urn("")
    with pytest.raises(ValueError):
        make_scheduler_datajob_urn("   ")


def test_make_datahub_datajob_url_uses_tasks_prefix() -> None:
    urn = make_scheduler_datajob_urn("my_job")
    url = make_datahub_datajob_url(urn, "http://datahub:9002")
    assert "/tasks/" in url
    assert "/dataset/" not in url
    encoded = urllib.parse.quote(urn, safe="")
    assert url == f"http://datahub:9002/tasks/{encoded}"


def test_make_datahub_datajob_url_strips_trailing_slash() -> None:
    urn = make_scheduler_datajob_urn("my_job")
    url = make_datahub_datajob_url(urn, "http://datahub:9002/")
    assert not url.startswith("http://datahub:9002//tasks/")


def test_scheduler_web_url_builds_jenkins_url() -> None:
    url = scheduler_web_url("dm_app_logistics_work_piece_di_v2")
    assert url == "https://schedule.corp.bianlifeng.com/job/dm_app_logistics_work_piece_di_v2"


def test_job_base_returns_all_fields() -> None:
    base = job_base("my_job", "http://datahub:9002")
    assert base["job_display_name"] == "my_job"
    assert "urn:li:dataJob:" in base["datajob_urn"]
    assert "/tasks/" in base["datahub_url"]
    assert base["schedule_url"] == "https://schedule.corp.bianlifeng.com/job/my_job"
