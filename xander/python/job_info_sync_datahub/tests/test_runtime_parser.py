"""runtime_parser 单元测试。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

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


def test_extract_job_path_python_stops_at_shell_var() -> None:
    """runner 行末尾 ``$DATABASE`` 等变量不参与路径，避免拼成错误的 .py 文件名。"""
    shell = (
        "/home/w/thrall/bin/w-run-task.sh python "
        "dw_ordering/financial_calculation/base_index_fluc_di $DATABASE"
    )
    path, kind = extract_job_path_and_type(shell)
    assert path == "dw_ordering/financial_calculation/base_index_fluc_di"
    assert kind == "python"
    assert job_file_name(path, kind) == (
        "dw_ordering_financial_calculation_base_index_fluc_di.py"
    )


def test_extract_job_path_job_stops_at_shell_var() -> None:
    shell = "/home/w/thrall/bin/w-run-task.sh dw_ordering/financial_calculation/base_index_fluc_di $DATABASE"
    path, kind = extract_job_path_and_type(shell)
    assert path == "dw_ordering/financial_calculation/base_index_fluc_di"
    assert kind == "job"


def test_extract_job_path_job_multiline_two_spaces_before_dollar() -> None:
    """DMP 常见：路径与 ``$DATABASE`` 间多个空格，下一行 ``# echo``；须仍能解析 .job 基名。"""
    shell = (
        "/home/w/thrall/bin/w-run-task.sh dw_ordering/financial_calculation/base_index_data_da  $DATABASE\n"
        "# echo 1 \n"
    )
    path, kind = extract_job_path_and_type(shell)
    assert path == "dw_ordering/financial_calculation/base_index_data_da"
    assert kind == "job"
    assert job_file_name(path, kind) == "dw_ordering_financial_calculation_base_index_data_da.job"


def test_extract_job_path_job_glued_dollar_var() -> None:
    """路径与变量粘连（无空白）时仍截断。"""
    shell = "/home/w/thrall/bin/w-run-task.sh dw_ordering/financial_calculation/base_index_data_da$DATABASE"
    path, kind = extract_job_path_and_type(shell)
    assert path == "dw_ordering/financial_calculation/base_index_data_da"
    assert kind == "job"


def test_extract_job_path_ignores_inline_comment_after_path() -> None:
    shell = "/home/w/thrall/bin/w-run-task.sh dw_ordering/nostore_spu_abc_di # $WHICH_DATA"
    path, kind = extract_job_path_and_type(shell)
    assert path == "dw_ordering/nostore_spu_abc_di"
    assert kind == "job"
    assert job_file_name(path, kind) == "dw_ordering_nostore_spu_abc_di.job"


def test_extract_job_path_job_nbsp_before_dollar() -> None:
    """非常规空白（NBSP）分隔时，``split()`` 拆不出 ``$`` token，须靠正则截断。"""
    nbsp = "\u00a0"
    shell = f"/home/w/thrall/bin/w-run-task.sh dw_ordering/financial_calculation/base_index_data_da{nbsp}$DATABASE"
    path, kind = extract_job_path_and_type(shell)
    assert path == "dw_ordering/financial_calculation/base_index_data_da"
    assert kind == "job"


def test_extract_job_path_python_stops_at_first_shell_var() -> None:
    """多个 ``$VAR`` 时截断在第一个变量前。"""
    shell = (
        "/home/w/thrall/bin/w-run-task.sh python "
        "dw_ordering/financial_calculation/base_index_fluc_di $DATE $DATABASE"
    )
    path, kind = extract_job_path_and_type(shell)
    assert path == "dw_ordering/financial_calculation/base_index_fluc_di"
    assert kind == "python"


def test_extract_job_path_python_stops_at_braced_var() -> None:
    shell = "/home/w/thrall/bin/w-run-task.sh python foo/bar_task ${DATE}"
    path, kind = extract_job_path_and_type(shell)
    assert path == "foo/bar_task"
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


def test_resolve_unknown_gitlab_name_when_local_dir_exists_returns_empty() -> None:
    """未映射的 gitlab_name：若 BLF_ETL_LOCAL_ROOT/<name> 为目录，则返回空 project_path（仅 local）。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "data_dev").mkdir()
        with patch.dict(os.environ, {"BLF_ETL_LOCAL_ROOT": str(tmp), "BLF_ETL_LOCAL_DISABLE": ""}):
            assert resolve_project_path("data_dev") == ""


def test_resolve_unknown_gitlab_name_raises_when_no_local_mirror() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(os.environ, {"BLF_ETL_LOCAL_ROOT": str(tmp), "BLF_ETL_LOCAL_DISABLE": ""}):
            try:
                resolve_project_path("data_shop")
                raise AssertionError("expected RuntimeError")
            except RuntimeError as exc:
                assert "data_shop" in str(exc)


def test_candidate_paths_dedup() -> None:
    paths = candidate_gitlab_paths("pdw_opc_flag/pdw_opc_flag_contact", "pdw_opc_flag_pdw_opc_flag_contact.job")
    assert len(paths) == len(set(paths))
    assert paths[0].startswith("jobs/pdw_opc_flag/")
