from scheduler_datajob_sync.trigger_parser import (
    TRIGGER_JOB_DEPENDENCY,
    TRIGGER_TIMER,
    parse_job_triggers,
)

TIMER_XML = """<?xml version='1.0' encoding='UTF-8'?>
<project>
  <triggers>
    <hudson.triggers.TimerTrigger>
      <spec>05 13 * * *</spec>
    </hudson.triggers.TimerTrigger>
  </triggers>
</project>
"""

JOB_DEPENDENCY_XML = """<?xml version='1.0' encoding='UTF-8'?>
<project>
  <triggers>
    <com.example.JobDependencyTrigger plugin="job-dependency@1.0">
      <upstreamParams>time_hour</upstreamParams>
      <jobs>
        <string>ods_assassin_creed_alarm_content_di</string>
      </jobs>
      <conditions>
        <string>d=@$</string>
      </conditions>
    </com.example.JobDependencyTrigger>
  </triggers>
</project>
"""

COMBINED_XML = TIMER_XML.replace("</project>", "") + JOB_DEPENDENCY_XML.split("<project>")[1]


def test_parse_job_triggers_detects_timer_and_cron() -> None:
    parsed = parse_job_triggers(TIMER_XML)

    assert TRIGGER_TIMER in parsed.trigger_types
    assert TRIGGER_JOB_DEPENDENCY not in parsed.trigger_types
    assert parsed.cron_schedule == "05 13 * * *"
    assert parsed.time_hour_param == ""


def test_parse_job_triggers_detects_job_dependency_and_time_hour_param() -> None:
    parsed = parse_job_triggers(JOB_DEPENDENCY_XML)

    assert TRIGGER_JOB_DEPENDENCY in parsed.trigger_types
    assert TRIGGER_TIMER not in parsed.trigger_types
    assert parsed.cron_schedule == ""
    assert parsed.time_hour_param == "time_hour"


def test_parse_job_triggers_supports_both_timer_and_job_dependency() -> None:
    parsed = parse_job_triggers(COMBINED_XML)

    assert TRIGGER_TIMER in parsed.trigger_types
    assert TRIGGER_JOB_DEPENDENCY in parsed.trigger_types
    assert parsed.cron_schedule == "05 13 * * *"
    assert parsed.time_hour_param == "time_hour"


def test_parse_job_triggers_returns_empty_for_blank_xml() -> None:
    parsed = parse_job_triggers("")

    assert parsed.trigger_types == []
    assert parsed.cron_schedule == ""
    assert parsed.time_hour_param == ""
