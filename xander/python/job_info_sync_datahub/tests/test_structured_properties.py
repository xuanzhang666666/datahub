"""测试 structured_properties：extractor 注册机制与 ETL 脚本包装。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from job_info_sync_datahub.models import JobContext, JobMetadata, RuntimeContext
from job_info_sync_datahub.structured_properties import (
    EtlScriptExtractor,
    ScheduleUrlExtractor,
    _wrap_etl_script,
    run_all_extractors,
)


def _make_ctx(job: str = "test_job", file_name: str = "test.job", content: str = "SELECT 1") -> JobContext:
    meta = JobMetadata(job_display_name=job, job_name=job, shell_command="")
    rt = RuntimeContext(
        job_display_name=job,
        gitlab_name="analysis-jobs",
        project_path="data/analysis-jobs",
        job_path="test/test_job",
        job_type="job",
        job_file_name=file_name,
        gitlab_file_path=f"jobs/test/{file_name}",
        etl_content=content,
    )
    return JobContext(metadata=meta, runtime=rt)


# ---------------------------------------------------------------------------
# _wrap_etl_script
# ---------------------------------------------------------------------------


def test_wrap_job_file_uses_sql():
    result = _wrap_etl_script("SELECT 1", "test.job")
    assert result.startswith("```sql")
    assert "SELECT 1" in result


def test_wrap_py_file_uses_python():
    result = _wrap_etl_script("print('hello')", "script.py")
    assert result.startswith("```python")


def test_wrap_unknown_ext_uses_text():
    result = _wrap_etl_script("some content", "file.xyz")
    assert result.startswith("```text")


def test_wrap_hql_uses_sql():
    result = _wrap_etl_script("SELECT 1", "query.hql")
    assert result.startswith("```sql")


# ---------------------------------------------------------------------------
# EtlScriptExtractor
# ---------------------------------------------------------------------------


def test_etl_extractor_produces_value():
    ctx = _make_ctx(file_name="myjob.job", content="INSERT INTO t SELECT 1 FROM s")
    ext = EtlScriptExtractor()
    vals = ext.extract(ctx)
    assert len(vals) == 1
    assert vals[0].property_urn == EtlScriptExtractor.PROPERTY_URN
    assert "```sql" in vals[0].string_value


def test_etl_extractor_empty_content():
    ctx = _make_ctx(content="")
    ext = EtlScriptExtractor()
    vals = ext.extract(ctx)
    assert vals == []


def test_etl_extractor_no_runtime():
    meta = JobMetadata(job_display_name="j", job_name="j", shell_command="")
    ctx = JobContext(metadata=meta)
    ext = EtlScriptExtractor()
    vals = ext.extract(ctx)
    assert vals == []


# ---------------------------------------------------------------------------
# ScheduleUrlExtractor
# ---------------------------------------------------------------------------


def test_schedule_url_format():
    ctx = _make_ctx(job="pdw_opc_flag_contact")
    ext = ScheduleUrlExtractor()
    vals = ext.extract(ctx)
    assert len(vals) == 1
    assert "pdw_opc_flag_contact" in vals[0].string_value
    assert vals[0].string_value.startswith("https://schedule.corp.bianlifeng.com/job/")


# ---------------------------------------------------------------------------
# run_all_extractors
# ---------------------------------------------------------------------------


def test_run_all_extractors_default():
    ctx = _make_ctx(job="test_job", file_name="t.job", content="INSERT INTO x SELECT 1 FROM y")
    results = run_all_extractors(ctx)
    urns = [r.property_urn for r in results]
    assert EtlScriptExtractor.PROPERTY_URN in urns
    assert ScheduleUrlExtractor.PROPERTY_URN in urns


def test_run_all_extractors_error_does_not_crash():
    """单个 extractor 抛异常时，其他 extractor 继续执行。"""
    from job_info_sync_datahub.structured_properties import BaseExtractor
    from job_info_sync_datahub.models import StructuredPropertyValue

    class BadExtractor(BaseExtractor):
        PROPERTY_URN = "urn:li:structuredProperty:test.bad"

        def extract(self, context):
            raise RuntimeError("模拟 extractor 异常")

    ctx = _make_ctx()
    results = run_all_extractors(ctx, extractors=[BadExtractor(), ScheduleUrlExtractor()])
    # BadExtractor 失败，ScheduleUrlExtractor 应仍然产出
    urns = [r.property_urn for r in results]
    assert ScheduleUrlExtractor.PROPERTY_URN in urns
    # 失败情况应写入 warnings
    assert len(ctx.warnings) >= 1


if __name__ == "__main__":
    test_wrap_job_file_uses_sql()
    test_wrap_py_file_uses_python()
    test_wrap_unknown_ext_uses_text()
    test_wrap_hql_uses_sql()
    test_etl_extractor_produces_value()
    test_etl_extractor_empty_content()
    test_etl_extractor_no_runtime()
    test_schedule_url_format()
    test_run_all_extractors_default()
    test_run_all_extractors_error_does_not_crash()
    print("所有 structured_properties 测试通过")
