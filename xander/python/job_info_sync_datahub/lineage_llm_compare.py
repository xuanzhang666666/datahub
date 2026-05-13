"""lineage_llm_compare — DeepSeek + sqlglot 表血缘对比。

从环境变量或 .env 读取密钥（不写入代码）：

  DEEPSEEK_OPENAI_BASE_URL=https://api.deepseek.com/v1  # 可选，有默认值
  DEEPSEEK_API_KEY=...
  DEEPSEEK_MODEL=deepseek-v4-pro                        # 可选

可选：BLF_LINEAGE_ENV_FILE 指向 .env；CLI --env-file 可多次指定。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "job_info_sync_datahub"

from .lineage_parser import build_lineage_summary, parse_block_lineage
from .logging_utils import get_logger, setup_logging
from .sql_extractor import extract_sql_blocks

logger = get_logger("lineage_llm_compare")

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.I)


def load_env_file(path: Path, override: bool = False) -> None:
    """解析 KEY=VAL 写入 os.environ（默认不覆盖已存在变量）。"""
    if not path.is_file():
        logger.debug("env 文件不存在，跳过: %s", path)
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1].replace('\\"', '"')
        elif val.startswith("'") and val.endswith("'"):
            val = val[1:-1]
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = val
    logger.info("已加载 env 文件: %s (override=%s)", path, override)


def _llm_ssl_context() -> Optional[ssl.SSLContext]:
    """返回 LLM 请求所用的 SSL context。
    BLF_LLM_SSL_VERIFY=0 时跳过证书验证（应对企业内网中间人证书问题）。
    """
    val = os.environ.get("BLF_LLM_SSL_VERIFY", "").strip().lower()
    if val in ("0", "false", "no"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None


def _normalize_openai_v1_base(url: str) -> str:
    u = url.strip().rstrip("/")
    if not u.endswith("/v1"):
        u = f"{u}/v1"
    return u


def _deepseek_config() -> Tuple[str, str, str]:
    base = os.environ.get("DEEPSEEK_OPENAI_BASE_URL", "https://api.deepseek.com")
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
    base = _normalize_openai_v1_base(base)
    if not key:
        raise RuntimeError("缺少 DeepSeek API Key：请设置 DEEPSEEK_API_KEY")
    return base, key, model


SYSTEM_PROMPT = """你是数据平台工程师，擅长阅读 Hive/Spark SQL、shell 与 Python 中的 SQL 片段。
你必须只输出一个 JSON 对象（不要 markdown），schema 如下：
{
  "lineage": [
    {
      "target": {"db": "库名，省略时用 default", "table": "表名"},
      "upstreams": [ {"db": "...", "table": "..."} ]
    }
  ],
  "notes": "简短说明不确定处、动态表名、仅 shell 无 SQL 等"
}
规则：
- lineage 数组：每个元素对应一条写入语句（INSERT INTO/OVERWRITE、CREATE TABLE AS 等）及其读取的上游表。
- target：该写入语句的物理目标表，db 省略时用 default，全部小写。
- upstreams：该目标表对应 SQL 中 FROM/JOIN/子查询读取的物理表；排除 WITH/CTE 别名；排除明显临时变量占位；全部小写。
- 若脚本写入多个目标表，每个目标表单独列一条 lineage 条目，各自只列与该 SQL 语句相关的上游表。
- 若同一目标表被多条 SQL 写入，合并为一条，upstreams 取并集。"""


def _build_user_message(etl_script: str, max_chars: int = 120_000) -> str:
    body = etl_script if len(etl_script) <= max_chars else etl_script[:max_chars] + "\n... [truncated]"
    return "以下为 ETL 脚本全文，请按 lineage 数组格式提取每个目标表及其对应的上游表：\n\n" + body


def _openai_chat_json(
    base_v1: str,
    api_key: str,
    model: str,
    user_message: str,
    timeout_sec: int = 90,
) -> Dict[str, Any]:
    url = base_v1.rstrip("/") + "/chat/completions"
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec, context=_llm_ssl_context()) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(f"HTTP {e.code} {url}: {err_body}") from e

    outer = json.loads(raw)
    try:
        content = outer["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Unexpected API response: {raw[:1500]}") from e

    if isinstance(content, str):
        return parse_llm_json_object(content)
    if isinstance(content, dict):
        return content  # type: ignore[return-value]
    raise RuntimeError(f"Unexpected message content type: {type(content)}")


def parse_llm_json_object(text: str) -> Dict[str, Any]:
    """从模型输出中解析 JSON 对象（支持裸 JSON 或 ```json 围栏）。"""
    text = text.strip()
    m = _JSON_FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    return json.loads(text)


def _openai_chat_json_with_fallback(
    base_v1: str,
    api_key: str,
    model: str,
    user_message: str,
    timeout_sec: int = 90,
) -> Dict[str, Any]:
    try:
        return _openai_chat_json(base_v1, api_key, model, user_message, timeout_sec)
    except (RuntimeError, json.JSONDecodeError, urllib.error.URLError) as first:
        logger.warning("首次调用（含 response_format）失败，重试无 json_object: %s", first)
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + "\n只输出 JSON，不要用 markdown。"},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.1,
    }
    url = base_v1.rstrip("/") + "/chat/completions"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_sec, context=_llm_ssl_context()) as resp:
        raw = resp.read().decode("utf-8")
    outer = json.loads(raw)
    content = outer["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise RuntimeError("unexpected content")
    return parse_llm_json_object(content)


def tables_from_llm_payload(payload: Dict[str, Any]) -> Tuple[Set[str], Set[str]]:
    """从 LLM JSON 得到 full_name 集合（委托给 lineage_write_policy）。"""
    from .lineage_write_policy import tables_from_llm_payload as _impl

    return _impl(payload)


def call_llm_extract(etl_script: str, timeout_sec: int = 90) -> Dict[str, Any]:
    """调用 DeepSeek 提取目标/上游表，返回 LLM 原始 JSON payload。失败时抛出异常。"""
    db, dk, dm = _deepseek_config()
    user_msg = _build_user_message(etl_script)
    return _openai_chat_json_with_fallback(db, dk, dm, user_msg, timeout_sec)


def tables_from_sqlglot(etl_content: str, job_file_name: str, date_str: Optional[str]) -> Tuple[Set[str], Set[str]]:
    blocks = extract_sql_blocks(etl_content, job_file_name, date_str=date_str)
    for b in blocks:
        parse_block_lineage(b, parent_logger=logger)
    t_lineages, _ = build_lineage_summary(blocks)
    targets: Set[str] = set()
    ups: Set[str] = set()
    for tl in t_lineages:
        targets.add(tl.target.full_name.lower())
        for u in tl.upstreams:
            ups.add(u.full_name.lower())
    return targets, ups


@dataclass
class CompareReport:
    sqlglot_parse_ok: bool = False
    sqlglot_targets: List[str] = field(default_factory=list)
    sqlglot_upstream: List[str] = field(default_factory=list)
    deepseek_raw: Optional[Dict[str, Any]] = None
    deepseek_error: Optional[str] = None
    verdict: str = ""
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sqlglot_parse_ok": self.sqlglot_parse_ok,
            "sqlglot_targets": self.sqlglot_targets,
            "sqlglot_upstream": self.sqlglot_upstream,
            "deepseek_raw": self.deepseek_raw,
            "deepseek_error": self.deepseek_error,
            "verdict": self.verdict,
            "errors": self.errors,
        }


def run_compare(
    etl_script: str,
    job_file_name: str,
    date_str: Optional[str],
    timeout_sec: int = 90,
) -> CompareReport:
    rep = CompareReport()
    try:
        st, su = tables_from_sqlglot(etl_script, job_file_name, date_str)
    except Exception as e:
        rep.errors.append(f"sqlglot pipeline: {e}")
        st, su = set(), set()

    rep.sqlglot_targets = sorted(st)
    rep.sqlglot_upstream = sorted(su)
    rep.sqlglot_parse_ok = bool(st or su)

    user_msg = _build_user_message(etl_script)

    try:
        db, dk, dm = _deepseek_config()
        rep.deepseek_raw = _openai_chat_json_with_fallback(db, dk, dm, user_msg, timeout_sec)
    except Exception as e:
        err_msg = str(e)
        rep.errors.append(f"deepseek: {err_msg}")
        rep.deepseek_error = err_msg
        rep.deepseek_raw = None

    # 简单 verdict：仅用于日志可读性，不参与策略决策
    if rep.deepseek_raw:
        dt, du = tables_from_llm_payload(rep.deepseek_raw)
        t_match = {x.lower() for x in st} == {x.lower() for x in dt}
        rep.verdict = "CONSISTENT" if t_match else "DISAGREE"
    elif rep.errors:
        rep.verdict = "LLM_ERROR"
    else:
        rep.verdict = "LLM_UNAVAILABLE"

    return rep


def _default_env_paths() -> List[Path]:
    paths: List[Path] = []
    extra = os.environ.get("BLF_LINEAGE_ENV_FILE", "").strip()
    if extra:
        paths.append(Path(extra))
    cwd = Path.cwd()
    paths.extend(
        [
            cwd / ".env",
            Path(__file__).resolve().parent / ".env",
            Path(__file__).resolve().parent.parent / ".env",
        ]
    )
    return paths


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="DeepSeek 与 sqlglot 表血缘对比")
    parser.add_argument("--etl-file", type=Path, help="ETL 脚本文本文件路径")
    parser.add_argument("--etl-text", type=str, default="", help="直接传入脚本内容（小脚本调试）")
    parser.add_argument("--job-file-name", default="inline.job", help="用于 sql 提取的文件名提示，如 xxx.job / x.py")
    parser.add_argument("--dt", default="", help="调度分区日期 YYYYMMDD，用于变量展开")
    parser.add_argument("--env-file", type=Path, action="append", default=[], help="可多次指定 .env 路径（先于默认路径加载）")
    parser.add_argument("--out-json", type=Path, default=None, help="写入完整 JSON 报告")
    parser.add_argument("--timeout", type=int, default=90, help="DeepSeek HTTP 超时秒数")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)

    setup_logging(args.log_level)
    for ef in args.env_file:
        load_env_file(ef, override=False)
    for ef in _default_env_paths():
        load_env_file(ef, override=False)

    if args.etl_file:
        etl = args.etl_file.read_text(encoding="utf-8", errors="replace")
    elif args.etl_text:
        etl = args.etl_text
    else:
        parser.error("请指定 --etl-file 或 --etl-text")

    rep = run_compare(etl, args.job_file_name, args.dt or None, timeout_sec=args.timeout)
    d = rep.to_dict()
    print(json.dumps(d, ensure_ascii=False, indent=2))
    if args.out_json:
        args.out_json.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("已写入: %s", args.out_json)

    return 0 if not rep.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
