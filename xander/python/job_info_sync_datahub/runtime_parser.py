"""runtime_parser — 从 shell_command 解析 GitLab 仓库信息和运行时参数。

复用并扩展了 pilot_pdw_job_etl_script_to_structured_properties.py 中已验证的解析逻辑。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .logging_utils import get_logger
from .models import RuntimeContext

logger = get_logger("runtime_parser")

# gitlab_name → GitLab 项目路径映射表，遇到新仓库在此补充一行即可
GITLAB_NAME_TO_PROJECT_PATH: Dict[str, str] = {
    "analysis-jobs": "data/analysis-jobs",
    "thrall": "smart-order/thrall",
    "mddf-job": "aladdin/mddf-job",
    "strategy-platform": "aladdin/strategy-job",
    "dayu": "logistics-data/dayu",
    "gis_hammurabi_jobs": "gis/gis-hammurabi-jobs",
    "confidentiality-jobs": "data/confidentiality-data-jobs",
    "data_finance": "data/data_dw_data_finance",
    "data_takeaway": "data/data_dw_data_takeaway",
    "data_drink": "data/data_drink",
    "data-autobox-jobs": "opc/data-autobox-jobs",
    "data_userresearch": "data/data_userresearch",
    "data_tech": "data/data_tech",
    "data_equipment": "data/data_equipment",
    "data_or_jobs": "mlg/data_or_jobs",
    "data_support": "data/data_support",
    "supplychain-jobs": "bach/supplychain-jobs",
    "data_analysis_etc_jobs": "data/analysis-etc-jobs",
    "analysis-jobs-test": "data/analysis-jobs-test",
    "other_campus_internal_control": "data/other-campus-internal-control",
}

# 匹配 --key=value 或 --key value 形式的运行参数
_PARAM_RE = re.compile(r"--([A-Za-z0-9_\-]+)(?:=(\S+))?")

# runner 脚本名 → 对应的 GitLab 中 jobs 目录前缀
_RUNNER_JOB_DIR: Dict[str, str] = {
    "w-run-task.sh": "jobs",
    "bike-run-task.sh": "bike-jobs",
    "yummy-run-task.sh": "yummy-jobs",
}

_RUNNER_RE = re.compile(r"/bin/((?:w|bike|yummy)-run-task\.sh)")


def _real_shell_lines(shell: str) -> List[str]:
    """返回 shell_command 中的非注释可执行行（去掉所有 # 开头的行）。"""
    out: List[str] = []
    for line in shell.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def has_real_w_run_task(shell: str) -> bool:
    """判断 shell_command 中是否有非注释的 runner 调用（需要去 GitLab 拉文件）。

    支持 w-run-task.sh、bike-run-task.sh、yummy-run-task.sh。
    """
    for line in shell.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and _RUNNER_RE.search(s):
            return True
    return False


def get_job_dir_name(shell: str) -> str:
    """从 shell_command 中提取 runner 对应的 GitLab jobs 目录前缀（jobs / bike-jobs / yummy-jobs）。"""
    line = _get_runner_line(shell)
    if not line:
        return "jobs"
    m = _RUNNER_RE.search(line)
    if not m:
        return "jobs"
    script_name = m.group(1)
    return _RUNNER_JOB_DIR.get(script_name, "jobs")


def _get_runner_line(shell: str) -> Optional[str]:
    """返回最后一条 runner 行，优先非注释行，次选 shebang 式注释行。"""
    real = [ln for ln in _real_shell_lines(shell) if _RUNNER_RE.search(ln)]
    if real:
        return real[-1].strip()
    # 降级：shebang 式注释行（#/home/.../w-run-task.sh）
    for line in shell.splitlines():
        s = line.strip()
        if s.startswith("#") and _RUNNER_RE.search(s):
            return s.lstrip("#").strip()
    return None


def _last_runner_line(shell: str) -> str:
    line = _get_runner_line(shell)
    if not line:
        raise RuntimeError("shell_command 中未找到 w-run-task.sh 行")
    return line


def extract_gitlab_name(shell: str) -> str:
    """从 …/<gitlab_name>/bin/<runner>.sh 提取 gitlab_name。"""
    line = _last_runner_line(shell)
    m = _RUNNER_RE.search(line)
    if not m:
        raise RuntimeError("shell_command 中无 /bin/*-run-task.sh，无法确定 gitlab_name")
    idx = m.start()
    prefix = line[:idx].rstrip()
    seg = prefix.split("/")[-1]
    if not seg:
        raise RuntimeError("/bin/*-run-task.sh 前的路径段为空，无法确定 gitlab_name")
    return seg


def _trim_runner_line_for_job_path(line: str) -> str:
    """去掉 runner 行上的 ``||`` 降级与 shell 管道 ``|`` 右侧，避免污染 job_path。"""
    line = re.split(r"\s*\|\|", line)[0].strip()
    if "|" in line:
        line = line.split("|", 1)[0].strip()
    return line


def _is_runtime_tail_token(tok: str) -> bool:
    """runner 参数中 job_path 之后的调度/环境 token。"""
    if tok in ("prod", "before", "after"):
        return True
    if tok.isdigit():
        return True
    if tok.startswith("--"):
        return True
    # shell 变量（如 ``$DATABASE``），调度在执行前展开，不参与 job 路径与 .py/.job 文件名
    if tok.startswith("$"):
        return True
    if re.match(r"^date\b", tok):
        return True
    if len(tok) == 1 and tok.isalpha():
        return True
    return False


def _cut_rest_at_runtime_tokens(rest: str) -> str:
    cut = len(rest)
    for tok in rest.split():
        if _is_runtime_tail_token(tok):
            p = rest.find(tok)
            if 0 <= p < cut:
                cut = p
    return rest[:cut].strip()


def extract_job_path_and_type(shell: str) -> Tuple[str, str]:
    """返回 (job_path, kind)，kind 为 'job' 或 'python'。"""
    line = _trim_runner_line_for_job_path(_last_runner_line(shell))

    m = _RUNNER_RE.search(line)
    if not m:
        raise RuntimeError("无法从 shell_command 解析 runner 后的参数")
    tail = line[m.end():].strip()

    if not tail:
        raise RuntimeError("无法从 shell_command 解析 w-run-task.sh 后的参数")

    env_markers = (" prod", " before", " after")

    if tail.lower().startswith("python "):
        rest = tail[7:].strip()
        cut = len(rest)
        for em in env_markers:
            p = rest.find(em)
            if 0 <= p < cut:
                cut = p
        return _cut_rest_at_runtime_tokens(rest[:cut].strip()), "python"

    rest = tail
    # 截断反引号命令（如 `date -d "-0 day"`）及 $(...) 展开；裸 ``$VAR`` 由 _cut_rest_at_runtime_tokens 处理
    backtick_pos = rest.find("`")
    dollar_pos = rest.find("$(")
    for pos in (backtick_pos, dollar_pos):
        if pos > 0:
            rest = rest[:pos]
    rest = rest.strip()

    return _cut_rest_at_runtime_tokens(rest), "job"


def extract_runtime_params(shell: str) -> Dict[str, str]:
    """从 shell_command 中提取 --key=value / --key value 形式的运行参数。"""
    line = _last_runner_line(shell)
    params: Dict[str, str] = {}
    for m in _PARAM_RE.finditer(line):
        key = m.group(1)
        val = m.group(2) or ""
        params[key] = val
    return params


def job_file_name(job_path: str, kind: str) -> str:
    base = job_path.replace("/", "_")
    return f"{base}.py" if kind == "python" else f"{base}.job"


def resolve_project_path(gitlab_name: str) -> str:
    """返回 GitLab ``group/project``；未映射时若 localfolder 下存在同名目录则返回空串（仅走本地源）。

    空串表示 ``resolve_etl_file`` 不调用 GitLab API，仅在 ``BLF_ETL_LOCAL_ROOT/<gitlab_name>/`` 下解析文件。
    """
    if gitlab_name in GITLAB_NAME_TO_PROJECT_PATH:
        return GITLAB_NAME_TO_PROJECT_PATH[gitlab_name]
    from .etl_file_resolver import get_local_root

    root = get_local_root()
    if root is not None and (root / gitlab_name).is_dir():
        logger.info(
            "gitlab_name=%s 未在 GITLAB_NAME_TO_PROJECT_PATH 中配置；"
            "在 %s 下发现同名目录，仅使用 localfolder 拉取 ETL",
            gitlab_name,
            root,
        )
        return ""
    hint = (
        f"未知 gitlab_name={gitlab_name!r}；请在 runtime_parser.GITLAB_NAME_TO_PROJECT_PATH 中补充映射，"
        f"或在 {root}/{gitlab_name} 部署 localfolder 镜像目录（并确保 BLF_ETL_LOCAL_ROOT 有效）。"
        if root is not None
        else f"未知 gitlab_name={gitlab_name!r}；请补充 GITLAB_NAME_TO_PROJECT_PATH 映射，"
        "或启用 BLF_ETL_LOCAL_ROOT 且同步对应目录。"
    )
    raise RuntimeError(hint)


def parse_runtime_context(
    job_display_name: str,
    shell_command: str,
    etl_content: str,
    gitlab_file_path: str,
) -> RuntimeContext:
    """将 shell_command 解析为完整 RuntimeContext。

    etl_content 和 gitlab_file_path 由 gitlab_client 获取后传入。
    """
    try:
        gitlab_name = extract_gitlab_name(shell_command)
        project_path = resolve_project_path(gitlab_name)
    except RuntimeError:
        gitlab_name = ""
        project_path = ""
    job_path, kind = extract_job_path_and_type(shell_command)
    jfn = job_file_name(job_path, kind)
    runtime_params = extract_runtime_params(shell_command)

    logger.debug(
        "RuntimeContext 解析完成: gitlab_name=%s project=%s job_path=%s kind=%s file=%s",
        gitlab_name,
        project_path,
        job_path,
        kind,
        jfn,
    )

    return RuntimeContext(
        job_display_name=job_display_name,
        gitlab_name=gitlab_name,
        project_path=project_path,
        job_path=job_path,
        job_type=kind,
        job_file_name=jfn,
        gitlab_file_path=gitlab_file_path,
        etl_content=etl_content,
        runtime_params=runtime_params,
    )


def candidate_gitlab_paths(job_path: str, jfn: str, job_dir: str = "jobs") -> List[str]:
    """生成 GitLab 文件路径候选列表，按优先顺序排列。

    job_dir: 仓库内的作业目录前缀（jobs / bike-jobs / yummy-jobs）。
    """
    first_seg = job_path.split("/")[0]
    jp = job_path.strip().strip("/")
    return list(
        dict.fromkeys(
            [
                f"{job_dir}/{first_seg}/{jfn}",
                f"{job_dir}/{jp}.job",
                f"{job_dir}/{jp}.py",
                f"{job_dir}/{jp}.yml",
                f"{job_dir}/{jp}/{jfn}",
            ]
        )
    )
