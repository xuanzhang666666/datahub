"""structured_properties：Execute Shell 与血缘写入门控。"""

from __future__ import annotations

import unittest

from job_info_sync_datahub.models import JobContext, JobMetadata, RuntimeContext, TableLineage, TableRef
from job_info_sync_datahub.structured_properties import (
    DEFAULT_EXTRACTORS,
    EtlScriptExtractor,
    ExecuteShellExtractor,
    ScheduleUrlExtractor,
    URN_EXECUTE_SHELL,
    _wrap_shell_command,
    collect_structured_properties_for_sync,
    run_all_extractors,
)


def _ctx(
    job: str = "mid_test_job",
    shell: str = "/home/w/dayu/bin/w-run-task.sh mid/foo.job",
    etl: str = "SELECT 1",
) -> JobContext:
    meta = JobMetadata(job_display_name=job, job_name=job, shell_command=shell)
    rt = RuntimeContext(
        job_display_name=job,
        gitlab_name="dayu",
        project_path="logistics-data/dayu",
        job_path="mid/foo",
        job_type="job",
        job_file_name="foo.job",
        gitlab_file_path="jobs/mid/foo.job",
        etl_content=etl,
    )
    return JobContext(metadata=meta, runtime=rt)


class TestWrapShellCommand(unittest.TestCase):
    def test_wrap_shell(self) -> None:
        body = "/home/w/dayu/bin/w-run-task.sh pdw/dt_gis_vi_recruit_candidate_v1_da"
        out = _wrap_shell_command(body)
        self.assertTrue(out.startswith("```shell\n"))
        self.assertIn(body, out)


class TestExecuteShellExtractor(unittest.TestCase):
    def test_extract_full_shell_command(self) -> None:
        shell = "# comment\n/home/w/bin/w-run-task.sh mid/a.job\n"
        vals = ExecuteShellExtractor().extract(_ctx(shell=shell))
        self.assertEqual(len(vals), 1)
        self.assertEqual(vals[0].property_urn, URN_EXECUTE_SHELL)
        self.assertIn(shell.strip(), vals[0].string_value)

    def test_empty_skips(self) -> None:
        self.assertEqual(ExecuteShellExtractor().extract(_ctx(shell="  ")), [])


class TestCollectStructuredPropertiesForSync(unittest.TestCase):
    def test_default_extractors_exclude_execute_shell(self) -> None:
        urns = {e.PROPERTY_URN for e in DEFAULT_EXTRACTORS}
        self.assertNotIn(URN_EXECUTE_SHELL, urns)

    def test_no_lineage_no_execute_shell(self) -> None:
        ctx = _ctx()
        props = collect_structured_properties_for_sync(
            ctx, table_lineages=[], write_upstream_lineage=True
        )
        self.assertNotIn(URN_EXECUTE_SHELL, {p.property_urn for p in props})
        self.assertIn(EtlScriptExtractor.PROPERTY_URN, {p.property_urn for p in props})

    def test_write_upstream_false_no_execute_shell(self) -> None:
        ctx = _ctx()
        tl = [
            TableLineage(
                target=TableRef(db="default", table="mid_test_job"),
                upstreams=[],
                source_block_indices=[],
            )
        ]
        props = collect_structured_properties_for_sync(
            ctx, table_lineages=tl, write_upstream_lineage=False
        )
        self.assertNotIn(URN_EXECUTE_SHELL, {p.property_urn for p in props})

    def test_lineage_ok_includes_execute_shell(self) -> None:
        ctx = _ctx()
        tl = [
            TableLineage(
                target=TableRef(db="default", table="mid_test_job"),
                upstreams=[TableRef(db="default", table="mid_up")],
                source_block_indices=[],
            )
        ]
        props = collect_structured_properties_for_sync(
            ctx, table_lineages=tl, write_upstream_lineage=True
        )
        urns = [p.property_urn for p in props]
        self.assertIn(URN_EXECUTE_SHELL, urns)
        self.assertIn(ScheduleUrlExtractor.PROPERTY_URN, urns)
        self.assertIn(EtlScriptExtractor.PROPERTY_URN, urns)

    def test_run_all_extractors_alone_never_execute_shell(self) -> None:
        props = run_all_extractors(_ctx())
        self.assertNotIn(URN_EXECUTE_SHELL, {p.property_urn for p in props})


if __name__ == "__main__":
    unittest.main()
