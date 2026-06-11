from scheduler_datajob_sync.models import (
    SchedulerJobDependency,
    pair_job_dependencies,
    parse_upstream_conditions,
)


def test_parse_upstream_conditions_accepts_json_and_commas() -> None:
    assert parse_upstream_conditions('["d=@$", "h=1"]') == ["d=@$", "h=1"]
    assert parse_upstream_conditions("d=@$, h=1") == ["d=@$", "h=1"]
    assert parse_upstream_conditions("") == []
    assert parse_upstream_conditions(None) == []


def test_pair_job_dependencies_pairs_by_index_case_sensitive() -> None:
    deps, mismatches = pair_job_dependencies(
        ["Upstream_A", "ods_assassin_creed_alarm_content_di"],
        '["d=@$", "h=1 | 2"]',
    )

    assert mismatches == 0
    assert deps == [
        SchedulerJobDependency("Upstream_A", "d=@$"),
        SchedulerJobDependency("ods_assassin_creed_alarm_content_di", "h=1 | 2"),
    ]


def test_pair_job_dependencies_preserves_job_name_case() -> None:
    deps, _ = pair_job_dependencies(["PDW_Example_Job"], "d=@$")

    assert deps[0].upstream_job_display_name == "PDW_Example_Job"


def test_pair_job_dependencies_reports_length_mismatch() -> None:
    deps, mismatches = pair_job_dependencies(
        ["job_a", "job_b"],
        "d=@$",
    )

    assert mismatches == 1
    assert len(deps) == 2
    assert deps[0].condition == "d=@$"
    assert deps[1].condition == ""
