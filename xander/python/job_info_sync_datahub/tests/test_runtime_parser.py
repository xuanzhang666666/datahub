"""runtime_parser 单元测试。"""

from __future__ import annotations

from job_info_sync_datahub.runtime_parser import (
    candidate_gitlab_paths,
    extract_gitlab_name,
    extract_job_path_and_type,
    job_file_name,
    resolve_project_path,
)


def test_extract_gitlab_name_standard() -> None:
    shell = "/home/data/analysis-jobs/bin/w-run-task.sh pdw_opc_flag/pdw_opc_flag_contact prod"
    assert extract_gitlab_name(shell) == "analysis-jobs"


def test_extract_gitlab_name_shebang_style() -> None:
    shell = "#/home/data/thrall/bin/w-run-task.sh some/job prod"
    assert extract_gitlab_name(shell) == "thrall"


def test_extract_gitlab_name_multiline() -> None:
    shell = "# some comment\n/home/data/mddf-job/bin/w-run-task.sh path/to/job prod"
    assert extract_gitlab_name(shell) == "mddf-job"


def test_extract_job_path_job_type() -> None:
    shell = "/home/data/analysis-jobs/bin/w-run-task.sh pdw_opc_flag/pdw_opc_flag_contact prod"
    path, kind = extract_job_path_and_type(shell)
    assert path == "pdw_opc_flag/pdw_opc_flag_contact"
    assert kind == "job"


def test_extract_job_path_python_type() -> None:
    shell = "/home/data/analysis-jobs/bin/w-run-task.sh python some/py_job prod"
    path, kind = extract_job_path_and_type(shell)
    assert path == "some/py_job"
    assert kind == "python"


def test_extract_job_path_stops_at_flag() -> None:
    shell = "/home/data/analysis-jobs/bin/w-run-task.sh some/job --hive_table_name=foo prod"
    path, kind = extract_job_path_and_type(shell)
    assert path == "some/job"
    assert kind == "job"


def test_extract_job_path_stops_at_partition_letter_d() -> None:
    shell = "/home/w/thrall/bin/w-run-task.sh dim_store/date_tag_di D"
    path, kind = extract_job_path_and_type(shell)
    assert path == "dim_store/date_tag_di"
    assert kind == "job"
    assert job_file_name(path, kind) == "dim_store_date_tag_di.job"


def test_extract_job_path_partition_d_with_comment_line() -> None:
    shell = "/home/w/thrall/bin/w-run-task.sh dim_store/date_tag_di D\n# echo 1"
    path, kind = extract_job_path_and_type(shell)
    assert path == "dim_store/date_tag_di"
    assert job_file_name(path, kind) == "dim_store_date_tag_di.job"


def test_extract_job_path_stops_at_shell_pipe() -> None:
    shell = (
        "/path/to/analysis-jobs/bin/w-run-task.sh "
        "pdw/pdw_bach_affair_baseinfo_sku_convert_particular_di | echo ignore check"
    )
    path, kind = extract_job_path_and_type(shell)
    assert path == "pdw/pdw_bach_affair_baseinfo_sku_convert_particular_di"
    assert kind == "job"
    assert job_file_name(path, kind) == "pdw_pdw_bach_affair_baseinfo_sku_convert_particular_di.job"
    assert "|" not in job_file_name(path, kind)


def test_job_file_name_job() -> None:
    assert job_file_name("pdw_opc_flag/pdw_opc_flag_contact", "job") == "pdw_opc_flag_pdw_opc_flag_contact.job"


def test_job_file_name_python() -> None:
    assert job_file_name("some/py_job", "python") == "some_py_job.py"


def test_resolve_known_gitlab_name() -> None:
    assert resolve_project_path("analysis-jobs") == "data/analysis-jobs"


def test_candidate_paths_dedup() -> None:
    paths = candidate_gitlab_paths("pdw_opc_flag/pdw_opc_flag_contact", "pdw_opc_flag_pdw_opc_flag_contact.job")
    assert len(paths) == len(set(paths))
    assert paths[0].startswith("jobs/pdw_opc_flag/")
