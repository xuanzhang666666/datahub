"""测试 runtime_parser 核心函数。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from job_info_sync_datahub.runtime_parser import (
    candidate_gitlab_paths,
    extract_gitlab_name,
    extract_job_path_and_type,
    job_file_name,
    resolve_project_path,
)


# ---------------------------------------------------------------------------
# extract_gitlab_name
# ---------------------------------------------------------------------------


def test_extract_gitlab_name_standard():
    shell = "/home/data/analysis-jobs/bin/w-run-task.sh pdw_opc_flag/pdw_opc_flag_contact prod"
    assert extract_gitlab_name(shell) == "analysis-jobs"


def test_extract_gitlab_name_shebang_style():
    shell = "#/home/data/thrall/bin/w-run-task.sh some/job prod"
    assert extract_gitlab_name(shell) == "thrall"


def test_extract_gitlab_name_multiline():
    shell = "# some comment\n/home/data/mddf-job/bin/w-run-task.sh path/to/job prod"
    assert extract_gitlab_name(shell) == "mddf-job"


# ---------------------------------------------------------------------------
# extract_job_path_and_type
# ---------------------------------------------------------------------------


def test_extract_job_path_job_type():
    shell = "/home/data/analysis-jobs/bin/w-run-task.sh pdw_opc_flag/pdw_opc_flag_contact prod"
    path, kind = extract_job_path_and_type(shell)
    assert path == "pdw_opc_flag/pdw_opc_flag_contact"
    assert kind == "job"


def test_extract_job_path_python_type():
    shell = "/home/data/analysis-jobs/bin/w-run-task.sh python some/py_job prod"
    path, kind = extract_job_path_and_type(shell)
    assert path == "some/py_job"
    assert kind == "python"


def test_extract_job_path_stops_at_flag():
    shell = "/home/data/analysis-jobs/bin/w-run-task.sh some/job --hive_table_name=foo prod"
    path, kind = extract_job_path_and_type(shell)
    assert path == "some/job"
    assert kind == "job"


# ---------------------------------------------------------------------------
# job_file_name
# ---------------------------------------------------------------------------


def test_job_file_name_job():
    assert job_file_name("pdw_opc_flag/pdw_opc_flag_contact", "job") == "pdw_opc_flag_pdw_opc_flag_contact.job"


def test_job_file_name_python():
    assert job_file_name("some/py_job", "python") == "some_py_job.py"


# ---------------------------------------------------------------------------
# resolve_project_path
# ---------------------------------------------------------------------------


def test_resolve_known_gitlab_name():
    assert resolve_project_path("analysis-jobs") == "data/analysis-jobs"


def test_resolve_unknown_gitlab_name():
    try:
        resolve_project_path("unknown-repo-xyz")
        assert False, "应该抛出 RuntimeError"
    except RuntimeError as e:
        assert "unknown-repo-xyz" in str(e)


# ---------------------------------------------------------------------------
# candidate_gitlab_paths
# ---------------------------------------------------------------------------


def test_candidate_paths_dedup():
    paths = candidate_gitlab_paths("pdw_opc_flag/pdw_opc_flag_contact", "pdw_opc_flag_pdw_opc_flag_contact.job")
    # 不应有重复
    assert len(paths) == len(set(paths))
    # 第一候选应包含 first_seg
    assert paths[0].startswith("jobs/pdw_opc_flag/")


if __name__ == "__main__":
    test_extract_gitlab_name_standard()
    test_extract_gitlab_name_shebang_style()
    test_extract_gitlab_name_multiline()
    test_extract_job_path_job_type()
    test_extract_job_path_python_type()
    test_extract_job_path_stops_at_flag()
    test_job_file_name_job()
    test_job_file_name_python()
    test_resolve_known_gitlab_name()
    test_resolve_unknown_gitlab_name()
    test_candidate_paths_dedup()
    print("所有 runtime_parser 测试通过")
