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
}

# 匹配 --key=value 或 --key value 形式的运行参数
_PARAM_RE = re.compile(r"--([A-Za-z0-9_\-]+)(?:=(\S+))?")


def _real_shell_lines(shell: str) -> List[str]:
    """返回 shell_command 中的非注释可执行行（去掉所有 # 开头的行）。"""
    out: List[str] = []
    for line in shell.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def _shell_lines_for_parse(shell: str) -> List[str]:
    """返回可执行行；若无非注释 w-run-task.sh 行，则兼容 shebang 式注释行（去掉 #）。

    区分两种情况：
    - w-run-task.sh 在非注释行：真正通过 w-run-task.sh 执行，去 GitLab 拉文件。
    - w-run-task.sh 只在注释行（如 #/home/.../w-run-task.sh）：注释引用，ETL 内联在 shell_command。
    """
    # 优先收集非注释行
    real_lines: List[str] = []
    comment_w_lines: List[str] = []  # shebang 式注释中含 w-run-task.sh 的行

    for line in shell.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#") and "w-run-task.sh" in s:
            comment_w_lines.append(s.lstrip("#").strip())
            continue
        if s.startswith("#"):
            continue
        real_lines.append(s)

    return real_lines


def has_real_w_run_task(shell: str) -> bool:
    """判断 shell_command 中是否有非注释的 w-run-task.sh 调用（需要去 GitLab 拉文件）。"""
    for line in shell.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "w-run-task.sh" in s:
            return True
    return False


def _get_w_run_task_line(shell: str) -> Optional[str]:
    """返回最后一条 w-run-task.sh 行，优先非注释行，次选 shebang 式注释行。"""
    real = [ln for ln in _real_shell_lines(shell) if "w-run-task.sh" in ln]
    if real:
        return real[-1].strip()
    # 降级：shebang 式注释行（仅用于提取 gitlab_name / job_path，不代表真正调用）
    for line in shell.splitlines():
        s = line.strip()
        if s.startswith("#") and "w-run-task.sh" in s:
            return s.lstrip("#").strip()
    return None


def _last_w_run_task_line(shell: str) -> str:
    line = _get_w_run_task_line(shell)
    if not line:
        raise RuntimeError("shell_command 中未找到 w-run-task.sh 行")
    return line


def extract_gitlab_name(shell: str) -> str:
    """从 …/<gitlab_name>/bin/w-run-task.sh 提取 gitlab_name。"""
    line = _last_w_run_task_line(shell)
    idx = line.find("/bin/w-run-task.sh")
    if idx < 0:
        raise RuntimeError("shell_command 中无 /bin/w-run-task.sh，无法确定 gitlab_name")
    prefix = line[:idx].rstrip()
    seg = prefix.split("/")[-1]
    if not seg:
        raise RuntimeError("/bin/w-run-task.sh 前的路径段为空，无法确定 gitlab_name")
    return seg


def extract_job_path_and_type(shell: str) -> Tuple[str, str]:
    """返回 (job_path, kind)，kind 为 'job' 或 'python'。"""
    line = _last_w_run_task_line(shell)
    m = re.search(r"/bin/w-run-task\.sh\s+(.*)$", line)
    if not m:
        raise RuntimeError("无法从 shell_command 解析 w-run-task.sh 后的参数")
    tail = m.group(1).strip()

    env_markers = (" prod", " before", " after")

    if tail.lower().startswith("python "):
        rest = tail[7:].strip()
        cut = len(rest)
        for em in env_markers:
            p = rest.find(em)
            if 0 <= p < cut:
                cut = p
        for tok in rest.split():
            if tok.startswith("--"):
                p = rest.find(tok)
                if 0 <= p < cut:
                    cut = p
        return rest[:cut].strip(), "python"

    rest = tail
    cut = len(rest)
    for tok in rest.split():
        if tok in ("prod", "before", "after") or tok.isdigit() or tok.startswith("--"):
            p = rest.find(tok)
            if 0 <= p < cut:
                cut = p
    return rest[:cut].strip(), "job"


def extract_runtime_params(shell: str) -> Dict[str, str]:
    """从 shell_command 中提取 --key=value / --key value 形式的运行参数。"""
    line = _last_w_run_task_line(shell)
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
    if gitlab_name not in GITLAB_NAME_TO_PROJECT_PATH:
        raise RuntimeError(
            f"未知 gitlab_name={gitlab_name!r}；"
            f"请在 runtime_parser.GITLAB_NAME_TO_PROJECT_PATH 中补充映射"
        )
    return GITLAB_NAME_TO_PROJECT_PATH[gitlab_name]


def parse_runtime_context(
    job_display_name: str,
    shell_command: str,
    etl_content: str,
    gitlab_file_path: str,
) -> RuntimeContext:
    """将 shell_command 解析为完整 RuntimeContext。

    etl_content 和 gitlab_file_path 由 gitlab_client 获取后传入。
    """
    gitlab_name = extract_gitlab_name(shell_command)
    project_path = resolve_project_path(gitlab_name)
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


def candidate_gitlab_paths(job_path: str, jfn: str) -> List[str]:
    """生成 GitLab 文件路径候选列表，按优先顺序排列。"""
    first_seg = job_path.split("/")[0]
    jp = job_path.strip().strip("/")
    return list(
        dict.fromkeys(
            [
                f"jobs/{first_seg}/{jfn}",
                f"jobs/{jp}.job",
                f"jobs/{jp}.py",
                f"jobs/{jp}/{jfn}",
            ]
        )
    )
