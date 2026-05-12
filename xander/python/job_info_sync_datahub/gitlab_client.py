"""gitlab_client — 从 GitLab API 拉取 ETL 脚本文件。

依赖：Python 标准库 urllib（无第三方依赖）
环境变量：
  BLF_GITLAB_PRIVATE_TOKEN  私有仓库必填
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable, List, Optional, Tuple

from .logging_utils import GITLAB_FETCH_FAILED, get_logger, log_phase_error

logger = get_logger("gitlab_client")

GITLAB_API = "https://git.corp.bianlifeng.com/api/v4"


def _get_token() -> Optional[str]:
    return os.getenv("BLF_GITLAB_PRIVATE_TOKEN")


def _build_request(url: str, token: Optional[str]) -> urllib.request.Request:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        req.add_header("PRIVATE-TOKEN", token)
    return req


def get_project_id(project_path: str, token: Optional[str] = None) -> int:
    """根据 GitLab 项目路径获取数字 project id。"""
    enc = urllib.parse.quote(project_path, safe="")
    url = f"{GITLAB_API}/projects/{enc}"
    req = _build_request(url, token)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            raise RuntimeError(
                f"GitLab 项目 HTTP {e.code}: {project_path!r}。"
                "私有仓库需要设置 BLF_GITLAB_PRIVATE_TOKEN。"
            ) from e
        raise
    pid = data.get("id")
    if pid is None:
        raise RuntimeError(f"GitLab 响应中无 project id: {project_path!r}")
    return int(pid)


def get_file_content(
    project_id: int,
    file_path: str,
    ref: str,
    token: Optional[str] = None,
) -> str:
    """拉取单个文件内容（Base64 解码后返回 UTF-8 字符串）。"""
    enc = urllib.parse.quote(file_path, safe="")
    url = (
        f"{GITLAB_API}/projects/{project_id}/repository/files/{enc}"
        f"?ref={urllib.parse.quote(ref)}"
    )
    req = _build_request(url, token)
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    b64 = data.get("content")
    if not b64:
        raise RuntimeError(f"GitLab 文件响应缺少 content 字段: {file_path}")
    return base64.b64decode(b64).decode("utf-8", errors="replace")


def try_file_paths(
    project_id: int,
    paths: Iterable[str],
    ref: str,
    token: Optional[str] = None,
) -> Tuple[str, str]:
    """依次尝试候选路径（主 ref 和备用 ref），返回 (used_path, content)。"""
    alt_ref = "main" if ref == "master" else "master"
    last_err: Optional[BaseException] = None
    for fp in paths:
        for r in (ref, alt_ref):
            try:
                content = get_file_content(project_id, fp, r, token)
                logger.debug("GitLab 文件获取成功: path=%s ref=%s", fp, r)
                return fp, content
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 404:
                    continue
                raise
            except Exception as e:
                last_err = e
                raise
    raise RuntimeError(f"所有候选路径均未找到文件: {last_err}")


def search_blob_paths(
    project_id: int,
    filename: str,
    ref: str,
    token: Optional[str] = None,
) -> List[str]:
    """在项目内全文搜索文件名，返回匹配的路径列表（需要 token）。"""
    if not token:
        return []
    q = urllib.parse.quote(filename, safe="")
    url = (
        f"{GITLAB_API}/projects/{project_id}/search"
        f"?scope=blobs&search={q}&ref={urllib.parse.quote(ref)}"
    )
    req = _build_request(url, token)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError:
        return []
    if not isinstance(data, list):
        return []
    paths: List[str] = []
    for item in data:
        p = item.get("path")
        if isinstance(p, str) and (p.endswith(filename) or p.endswith("/" + filename)):
            paths.append(p)
    return list(dict.fromkeys(paths))


def download_etl_file(
    project_path: str,
    candidate_paths: List[str],
    job_file_name: str,
    ref: str = "master",
    explicit_path: Optional[str] = None,
    token: Optional[str] = None,
) -> Tuple[str, str]:
    """完整的 ETL 文件拉取逻辑，返回 (used_path, content)。

    流程：
    1. 获取 project id
    2. 使用 explicit_path 或 candidate_paths 逐一尝试
    3. 若全部 404，尝试全文搜索兜底
    """
    if token is None:
        token = _get_token()

    logger.info(
        "开始拉取 GitLab 文件: project=%s candidates=%s ref=%s",
        project_path,
        candidate_paths,
        ref,
    )

    pid = get_project_id(project_path, token)
    paths_to_try = [explicit_path] if explicit_path else candidate_paths

    try:
        return try_file_paths(pid, paths_to_try, ref, token)
    except RuntimeError as first_err:
        logger.warning("候选路径未命中，尝试全文搜索: file=%s", job_file_name)
        found = search_blob_paths(pid, job_file_name, ref, token)
        if not found:
            raise first_err
        return try_file_paths(pid, found, ref, token)
