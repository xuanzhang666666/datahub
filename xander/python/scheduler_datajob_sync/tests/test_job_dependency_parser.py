from scheduler_datajob_sync.job_dependency_parser import (
    ParsedJobDependency,
    parse_job_dependencies_from_content,
)
from scheduler_datajob_sync.models import SchedulerJobMetadata

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

WORMPEX_XML = """<?xml version='1.0' encoding='UTF-8'?>
<project>
  <triggers>
    <com.wormpex.dp.trigger.JobDependencyBuildTrigger plugin="job-dependency-plugin@1.1">
      <upstreamProjects>job_a,job_b,job_c</upstreamProjects>
      <jobProperties>
        <com.wormpex.dp.pojo.JobDependencyProperty>
          <upstreamJobName>job_a</upstreamJobName>
          <triggerCondition>d = @$</triggerCondition>
          <threshold>SUCCESS</threshold>
        </com.wormpex.dp.pojo.JobDependencyProperty>
        <com.wormpex.dp.pojo.JobDependencyProperty>
          <upstreamJobName>job_b</upstreamJobName>
          <triggerCondition>h = 1</triggerCondition>
          <threshold>SUCCESS</threshold>
        </com.wormpex.dp.pojo.JobDependencyProperty>
        <com.wormpex.dp.pojo.JobDependencyProperty>
          <upstreamJobName>job_c</upstreamJobName>
          <triggerCondition>d = @$</triggerCondition>
          <threshold>UNSTABLE</threshold>
        </com.wormpex.dp.pojo.JobDependencyProperty>
      </jobProperties>
    </com.wormpex.dp.trigger.JobDependencyBuildTrigger>
  </triggers>
</project>
"""

TRUNCATED_MYSQL_UPSTREAM = (
    "job_a,job_b,dwd_l"
)


def test_parse_job_dependencies_from_content_reads_job_dependency_properties() -> None:
    deps = parse_job_dependencies_from_content(WORMPEX_XML)

    assert deps == [
        ParsedJobDependency("job_a", "d = @$", "SUCCESS"),
        ParsedJobDependency("job_b", "h = 1", "SUCCESS"),
        ParsedJobDependency("job_c", "d = @$", "UNSTABLE"),
    ]


def test_parse_job_dependencies_from_content_reads_jobs_and_conditions_format() -> None:
    deps = parse_job_dependencies_from_content(JOB_DEPENDENCY_XML)

    assert deps == [ParsedJobDependency("ods_assassin_creed_alarm_content_di", "d=@$")]


def test_parse_job_dependencies_from_content_falls_back_to_upstream_projects() -> None:
    xml = """<project><triggers>
    <com.wormpex.dp.trigger.JobDependencyBuildTrigger>
      <upstreamProjects>only_a,only_b</upstreamProjects>
    </com.wormpex.dp.trigger.JobDependencyBuildTrigger>
    </triggers></project>"""

    deps = parse_job_dependencies_from_content(xml)

    assert deps == [
        ParsedJobDependency("only_a"),
        ParsedJobDependency("only_b"),
    ]


def test_parse_job_dependencies_from_content_returns_none_without_trigger() -> None:
    assert parse_job_dependencies_from_content("<project></project>") is None
    assert parse_job_dependencies_from_content("") is None


def test_parse_job_dependencies_from_content_returns_empty_list_for_trigger_without_upstreams() -> (
    None
):
    xml = """<project><triggers>
    <com.wormpex.dp.trigger.JobDependencyBuildTrigger>
      <upstreamProjects></upstreamProjects>
    </com.wormpex.dp.trigger.JobDependencyBuildTrigger>
    </triggers></project>"""

    assert parse_job_dependencies_from_content(xml) == []


def test_scheduler_job_metadata_prefers_content_xml_over_truncated_mysql() -> None:
    metadata = SchedulerJobMetadata.from_mysql_row(
        {
            "job_display_name": "dm_app_logistics_work_piece_di_v2",
            "content": WORMPEX_XML,
            "upstream_jobs": TRUNCATED_MYSQL_UPSTREAM,
            "upstream_jobs_conditions": "d = @$,d = @$,d",
        }
    )

    assert metadata.upstream_jobs == ["job_a", "job_b", "job_c"]
    assert metadata.dependency_pair_mismatch_count == 0
    assert len(metadata.job_dependencies) == 3
    assert metadata.job_dependencies[0].condition == "d = @$"
    assert metadata.writes_job_lineage_edges() is True


def test_scheduler_job_metadata_falls_back_to_mysql_when_content_has_no_dependency_trigger() -> (
    None
):
    metadata = SchedulerJobMetadata.from_mysql_row(
        {
            "job_display_name": "mysql_only_job",
            "content": "<project><triggers></triggers></project>",
            "upstream_jobs": '["upstream_a", "upstream_b"]',
            "upstream_jobs_conditions": '["d=@$", "h=1"]',
        }
    )

    assert metadata.upstream_jobs == ["upstream_a", "upstream_b"]
    assert metadata.dependency_pair_mismatch_count == 0
    assert len(metadata.job_dependencies) == 2
