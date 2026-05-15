"""etl_file_resolver — 从 neo4j2 localfolder 与 GitLab 双源解析 ETL 脚本。

合并规则（两侧均指同一相对路径，如 jobs/foo/bar.job）：
  - 仅 GitLab 有 → GitLab
  - 仅 local 有 → localfolder
  - 两侧都有且内容相同 → GitLab
  - 两侧都有且内容不同 → localfolder

环境变量：
  BLF_ETL_LOCAL_ROOT       默认 /localfolder（neo4j2 项目镜像根）
  BLF_ETL_LOCAL_DISABLE=1  关闭本地源，仅 GitLab
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .logging_utils import get_logger

logger = get_logger("etl_file_resolver")

ReadGitFn = Callable[[str], Optional[str]]
ReadLocalFn = Callable[[str], Optional[str]]


def _env_local_disabled() -> bool:
    v = os.environ.get("BLF_ETL_LOCAL_DISABLE", "").strip().lower()
    return v in ("1", "true", "yes", "on")


def get_local_root() -> Optional[Path]:
    """返回本地镜像根目录；未启用或不存在时返回 None。"""
    if _env_local_disabled():
        return None
    raw = os.environ.get("BLF_ETL_LOCAL_ROOT", "/localfolder").strip()
    if not raw:
        return None
    root = Path(raw)
    if not root.is_dir():
        logger.debug("BLF_ETL_LOCAL_ROOT 不是目录，跳过本地源: %s", root)
        return None
    return root


def read_local_candidate(
    gitlab_name: str,
    rel_path: str,
    *,
    local_root: Optional[Path] = None,
) -> Optional[str]:
    """读 {local_root}/{gitlab_name}/{rel_path}，不存在返回 None。"""
    root = local_root if local_root is not None else get_local_root()
    if root is None:
        return None
    fp = root / gitlab_name / rel_path.lstrip("/")
    if not fp.is_file():
        return None
    return fp.read_text(encoding="utf-8", errors="replace")


def normalize_content_for_compare(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip()


def content_equal(a: str, b: str) -> bool:
    return normalize_content_for_compare(a) == normalize_content_for_compare(b)


def merge_contents(
    git_content: Optional[str],
    local_content: Optional[str],
    *,
    rel_path: str,
    gitlab_name: str,
) -> Tuple[str, str, str]:
    """返回 (content, source_tag, display_path)。"""
    if git_content is not None and local_content is None:
        return git_content, "gitlab", f"gitlab:{rel_path}"
    if git_content is None and local_content is not None:
        return local_content, "local", f"localfolder:{gitlab_name}/{rel_path}"
    if git_content is None and local_content is None:
        raise ValueError("merge_contents 需要至少一侧有内容")

    if content_equal(git_content, local_content):
        logger.debug(
            "ETL 双源一致，选用 GitLab: %s/%s",
            gitlab_name,
            rel_path,
        )
        return git_content, "gitlab_preferred_same", f"gitlab:{rel_path}"

    logger.warning(
        "ETL 双源内容不一致，选用 localfolder: gitlab_name=%s rel=%s "
        "gitlab_len=%d local_len=%d",
        gitlab_name,
        rel_path,
        len(git_content),
        len(local_content),
    )
    return local_content, "local_differs", f"localfolder:{gitlab_name}/{rel_path}"


def search_local_paths_by_filename(
    gitlab_name: str,
    job_file_name: str,
    *,
    local_root: Optional[Path] = None,
) -> List[str]:
    """在 {root}/{gitlab_name} 下按 basename 查找，返回仓库内相对路径列表。"""
    root = local_root if local_root is not None else get_local_root()
    if root is None:
        return []
    base = root / gitlab_name
    if not base.is_dir():
        return []
    found: List[str] = []
    for dirpath, _dirnames, filenames in os.walk(base):
        if job_file_name not in filenames:
            continue
        full = Path(dirpath) / job_file_name
        try:
            rel = full.relative_to(base).as_posix()
        except ValueError:
            continue
        found.append(rel)
    return list(dict.fromkeys(found))


def _try_merge_at_path(
    rel_path: str,
    gitlab_name: str,
    read_git: ReadGitFn,
    read_local: ReadLocalFn,
) -> Optional[Tuple[str, str, str]]:
    git_c = read_git(rel_path)
    local_c = read_local(rel_path)
    if git_c is None and local_c is None:
        return None
    content, source_tag, display = merge_contents(
        git_c, local_c, rel_path=rel_path, gitlab_name=gitlab_name
    )
    logger.info(
        "ETL 文件解析成功: %s source=%s",
        display,
        source_tag,
    )
    return display, content, source_tag


def resolve_etl_file(
    gitlab_name: str,
    project_path: str,
    candidate_paths: List[str],
    job_file_name: str,
    ref: str = "master",
    explicit_path: Optional[str] = None,
    token: Optional[str] = None,
    *,
    local_root: Optional[Path] = None,
    read_gitlab_at_path: Optional[ReadGitFn] = None,
    read_local_at_path: Optional[ReadLocalFn] = None,
) -> Tuple[str, str, str]:
    """双源解析 ETL 文件，返回 (display_path, content, source_tag)。"""
    from .gitlab_client import (
        _get_token,
        get_project_id,
        search_blob_paths,
        try_gitlab_file_at_path,
    )

    if token is None:
        token = _get_token()

    root = local_root if local_root is not None else get_local_root()
    local_enabled = root is not None

    pid: Optional[int] = None
    try:
        pid = get_project_id(project_path, token)
    except RuntimeError as exc:
        if not local_enabled:
            raise
        logger.warning(
            "GitLab 项目不可用，仅尝试 localfolder: project=%s err=%s",
            project_path,
            exc,
        )

    def _read_git(rel: str) -> Optional[str]:
        if pid is None:
            return None
        if read_gitlab_at_path is not None:
            return read_gitlab_at_path(rel)
        return try_gitlab_file_at_path(pid, rel, ref, token)

    def _read_local(rel: str) -> Optional[str]:
        if read_local_at_path is not None:
            return read_local_at_path(rel)
        return read_local_candidate(gitlab_name, rel, local_root=root)

    paths_to_try: List[str] = []
    if explicit_path:
        paths_to_try.append(explicit_path)
    for p in candidate_paths:
        if p and p not in paths_to_try:
            paths_to_try.append(p)

    logger.debug(
        "双源解析 ETL: gitlab_name=%s project=%s candidates=%d local=%s",
        gitlab_name,
        project_path,
        len(paths_to_try),
        local_enabled,
    )

    for rel in paths_to_try:
        merged = _try_merge_at_path(rel, gitlab_name, _read_git, _read_local)
        if merged is not None:
            return merged

    search_rels: List[str] = []
    if pid is not None:
        logger.warning("候选路径未命中，尝试 GitLab 全文搜索: file=%s", job_file_name)
        search_rels.extend(search_blob_paths(pid, job_file_name, ref, token))
    if local_enabled:
        local_found = search_local_paths_by_filename(
            gitlab_name, job_file_name, local_root=root
        )
        if local_found:
            logger.warning(
                "候选路径未命中，localfolder 按文件名找到 %d 条: file=%s",
                len(local_found),
                job_file_name,
            )
        search_rels.extend(local_found)
    search_rels = list(dict.fromkeys(search_rels))

    for rel in search_rels:
        merged = _try_merge_at_path(rel, gitlab_name, _read_git, _read_local)
        if merged is not None:
            return merged

    raise RuntimeError("所有候选路径均未找到文件（GitLab + localfolder）")
