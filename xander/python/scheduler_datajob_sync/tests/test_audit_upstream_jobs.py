from scheduler_datajob_sync.audit_upstream_jobs import audit_jobs
from scheduler_datajob_sync.models import SchedulerJobDependency, SchedulerJobMetadata
from scheduler_datajob_sync.trigger_parser import TRIGGER_JOB_DEPENDENCY, TRIGGER_TIMER


def test_audit_jobs_counts_trigger_types_and_dangling_refs() -> None:
    jobs = [
        SchedulerJobMetadata(
            job_display_name="timer_root",
            trigger_types=[TRIGGER_TIMER],
            cron_schedule="05 13 * * *",
        ),
        SchedulerJobMetadata(
            job_display_name="downstream",
            trigger_types=[TRIGGER_JOB_DEPENDENCY],
            upstream_jobs=["timer_root", "missing_job"],
            job_dependencies=[
                SchedulerJobDependency("timer_root", "d=@$"),
                SchedulerJobDependency("missing_job", "d=@$"),
            ],
            dependency_pair_mismatch_count=1,
        ),
    ]

    report = audit_jobs(jobs)

    assert report.total_jobs == 2
    assert report.timer_jobs == 1
    assert report.job_dependency_jobs == 1
    assert report.jobs_with_upstream == 1
    assert report.jobs_with_lineage_edges == 1
    assert len(report.dangling_upstream_refs) == 1
    assert report.dangling_upstream_refs[0]["upstream_job_display_name"] == "missing_job"
    assert len(report.condition_pair_mismatches) == 1
