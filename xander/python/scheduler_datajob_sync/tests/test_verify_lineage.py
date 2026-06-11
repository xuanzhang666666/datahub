from scheduler_datajob_sync.datajob_writer import make_scheduler_datajob_urn
from scheduler_datajob_sync.models import SchedulerJobDependency, SchedulerJobMetadata
from scheduler_datajob_sync.trigger_parser import TRIGGER_JOB_DEPENDENCY
from scheduler_datajob_sync.verify_lineage import verify_jobs


def test_verify_jobs_reports_resolved_and_missing_edges() -> None:
    metadata = SchedulerJobMetadata(
        job_display_name="downstream",
        trigger_types=[TRIGGER_JOB_DEPENDENCY],
        job_dependencies=[
            SchedulerJobDependency("upstream_ok", "d=@$"),
            SchedulerJobDependency("upstream_missing", "d=@$"),
        ],
    )
    downstream_urn = make_scheduler_datajob_urn("downstream")
    upstream_ok_urn = make_scheduler_datajob_urn("upstream_ok")

    def _fetch(urn: str) -> dict[str, object]:
        assert urn == downstream_urn
        return {
            "aspects": {
                "dataJobInputOutput": {
                    "value": {"inputDatajobs": [upstream_ok_urn]},
                }
            }
        }

    report = verify_jobs([metadata], gms_url="http://example", entity_fetcher=_fetch)

    assert report.sampled_jobs == 1
    assert report.jobs_with_expected_edges == 1
    assert report.resolved_edges == 1
    assert report.missing_edges == 1
    assert report.samples[0]["missing_upstreams"] == [
        make_scheduler_datajob_urn("upstream_missing")
    ]
