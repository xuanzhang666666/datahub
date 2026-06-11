from datetime import datetime

from scheduler_datajob_sync.models import SchedulerJobMetadata
from scheduler_datajob_sync.sync_datajobs import run_sync


class FakeReader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def fetch_job(self, job_display_name: str) -> SchedulerJobMetadata:
        self.calls.append(("job", job_display_name))
        return SchedulerJobMetadata(job_display_name=job_display_name)

    def fetch_jobs_by_prefix(self, prefix: str) -> list[SchedulerJobMetadata]:
        self.calls.append(("prefix", prefix))
        return [
            SchedulerJobMetadata(job_display_name=f"{prefix}A"),
            SchedulerJobMetadata(job_display_name=f"{prefix}B"),
        ]

    def fetch_jobs_updated_since(self, since: datetime) -> list[SchedulerJobMetadata]:
        self.calls.append(("updated_since", since))
        return [SchedulerJobMetadata(job_display_name="updated_job")]


class FakeWriter:
    def __init__(self) -> None:
        self.written: list[str] = []
        self.lineage_written: list[str] = []

    def write_job(self, metadata: SchedulerJobMetadata) -> None:
        self.written.append(metadata.job_display_name)

    def write_job_lineage(self, metadata: SchedulerJobMetadata) -> None:
        self.lineage_written.append(metadata.job_display_name)


def test_run_sync_fetches_single_job_and_writes_it() -> None:
    reader = FakeReader()
    writer = FakeWriter()

    result = run_sync(["--job", "PDW_Example_Job"], reader=reader, writer=writer)

    assert reader.calls == [("job", "PDW_Example_Job")]
    assert writer.written == ["PDW_Example_Job"]
    assert result.total == 1
    assert result.succeeded == 1
    assert result.failed == 0


def test_run_sync_fetches_prefix_jobs_and_writes_each() -> None:
    reader = FakeReader()
    writer = FakeWriter()

    result = run_sync(["--prefix", "PDW_"], reader=reader, writer=writer)

    assert reader.calls == [("prefix", "PDW_")]
    assert writer.written == ["PDW_A", "PDW_B"]
    assert result.total == 2
    assert result.succeeded == 2


def test_run_sync_fetches_incremental_jobs_by_updated_since() -> None:
    reader = FakeReader()
    writer = FakeWriter()

    result = run_sync(
        ["--updated-since", "2026-06-10T00:00:00"],
        reader=reader,
        writer=writer,
    )

    assert reader.calls == [("updated_since", datetime(2026, 6, 10, 0, 0, 0))]
    assert writer.written == ["updated_job"]
    assert result.total == 1


def test_run_sync_counts_writer_failures_without_stopping_batch() -> None:
    class FailingOnceWriter(FakeWriter):
        def write_job(self, metadata: SchedulerJobMetadata) -> None:
            if metadata.job_display_name == "PDW_A":
                raise RuntimeError("boom")
            super().write_job(metadata)

    reader = FakeReader()
    writer = FailingOnceWriter()

    result = run_sync(["--prefix", "PDW_"], reader=reader, writer=writer)

    assert writer.written == ["PDW_B"]
    assert result.total == 2
    assert result.succeeded == 1
    assert result.failed == 1
    assert result.errors == {"PDW_A": "boom"}


def test_run_sync_lineage_only_writes_lineage_without_full_sync() -> None:
    reader = FakeReader()
    writer = FakeWriter()

    result = run_sync(
        ["--job", "PDW_Example_Job", "--lineage-only"],
        reader=reader,
        writer=writer,
    )

    assert writer.written == []
    assert writer.lineage_written == ["PDW_Example_Job"]
    assert result.succeeded == 1
