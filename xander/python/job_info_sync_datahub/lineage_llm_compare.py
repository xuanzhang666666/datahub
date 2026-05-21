"""lineage_llm_compare — DeepSeek 表血缘提取。

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
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "job_info_sync_datahub"

from .logging_utils import get_logger, setup_logging

logger = get_logger("lineage_llm_compare")

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.I)
_SHELL_ASSIGNMENT_RE = re.compile(
    r"""^\s*([A-Za-z_][A-Za-z0-9_]*)=(?:"([^"]*)"|'([^']*)'|([^\s#]+))"""
)
_SHELL_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_SHELL_FUNCTION_START_RE = re.compile(
    r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(\s*\))?\s*\{"
)
_SHELL_SPLIT_COMMAND_RE = re.compile(r"\s*(?:;|&&|\|\|)\s*")

_SHELL_NON_CALL_WORDS = {
    "if",
    "then",
    "else",
    "elif",
    "fi",
    "for",
    "do",
    "done",
    "while",
    "case",
    "esac",
    "function",
    "local",
    "export",
    "return",
    "echo",
}


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
- 若同一目标表被多条 SQL 写入，合并为一条，upstreams 取并集。
- 脚本可能已按 '-- SQL 段 N --' 标注分段，每段对应一条写入语句，请按段分别提取各自的 target 和 upstreams。
- 多段 SQL（先 CREATE/INSERT 临时表、再写正式分层表）时：以**最终持久化落表**为 target（如 mid_*、dw_*、pdw_* 等）；以 tmp_ 开头或明显作业内临时表（含 tmp_mid_*）的写入目标**不得**作为 lineage[].target。
- upstreams 中不要输出**本脚本内创建/删除的作业内临时表**。若后段仅从这类临时表读取再写入最终表，须结合**同脚本更早 SQL 段**追溯该临时表在 FROM/JOIN 中实际读取的持久化表，将其并入最终 target 的 upstreams（跨段折叠、去重），不要把作业内临时表本身列入 upstreams。
- 有明确库名且不是本脚本内创建的 tmp_* 表，应视为外部物理上游表并保留，例如 data_smartorder.tmp_xxx、data_smartorder.tmp_${TABLE_NAME}_xxx 这类跨作业产物可以出现在 upstreams 中；不要因为表名包含 tmp_ 就一刀切删除。
- 前段写临时表、后段写最终表时，lineage 中**至少一条** target 为最终表，其 upstreams =（后段直接读取的物理表）∪（前段构建临时表所读取的物理表），合并去重；除仅有临时表且无最终落表、须在 notes 说明的情况外，不要仅为临时表单独留一条 lineage。
- target 表名须为数据分层可接受的前缀（如 dm/ods/pdw/pdim/app/dw/mid/ai/dwa/dwd/dim 等），**禁止**把 tmp_* 写入 target；upstreams 允许保留有明确库名的外部物理 tmp_* 上游。
- not_verified_* 不是 tmp_*。在 BLF 作业里，NOT_VERIFIED_TABLE_NAME、not_verified_${TABLE_NAME}、not_verified_<真实表名> 是数据校验用落表别名；遇到 INSERT/OVERWRITE 写入 not_verified_<真实表名> 时，lineage[].target 必须填写去掉 not_verified_ 前缀后的真实表名，不得因为它带 not_verified_ 就丢弃目标表。
- 若脚本定义 TABLE_NAME="pdim_xxx" 且 NOT_VERIFIED_TABLE_NAME="not_verified_${TABLE_NAME}"，则写入 $NOT_VERIFIED_TABLE_NAME 等价于写入目标表 pdim_xxx；必须提取该 lineage，并把 FROM/JOIN 中的物理表作为 upstreams。
- 动态表名、${var} 等无法确定处写在 notes；拿不准的物理表宁可少写也不要编造库表名。"""


def _extract_shell_assignments(etl_script: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw in etl_script.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _SHELL_ASSIGNMENT_RE.match(line)
        if not m:
            continue
        value = next(v for v in m.groups()[1:] if v is not None)
        out[m.group(1)] = value.strip()
    return out


def _expand_shell_vars(value: str, assignments: Dict[str, str]) -> str:
    expanded = value
    for _ in range(5):
        new_value = _SHELL_VAR_RE.sub(
            lambda m: assignments.get(m.group(1) or m.group(2), m.group(0)),
            expanded,
        )
        if new_value == expanded:
            break
        expanded = new_value
    return expanded


def _not_verified_alias_prompt(etl_script: str) -> str:
    assignments = _extract_shell_assignments(etl_script)
    if not assignments:
        return ""
    not_verified_name = _expand_shell_vars(
        assignments.get("NOT_VERIFIED_TABLE_NAME", ""),
        assignments,
    ).strip()
    table_name = _expand_shell_vars(assignments.get("TABLE_NAME", ""), assignments).strip()
    if not not_verified_name.startswith("not_verified_"):
        return ""
    real_target = not_verified_name[len("not_verified_") :].strip() or table_name
    if not real_target:
        return ""
    return (
        "检测到 BLF 校验落表变量：\n"
        f"- TABLE_NAME={table_name or '-'}\n"
        f"- NOT_VERIFIED_TABLE_NAME={assignments.get('NOT_VERIFIED_TABLE_NAME', '-')}\n"
        f"- 展开后 not_verified 表名={not_verified_name}\n"
        f"请将写入 $NOT_VERIFIED_TABLE_NAME / {not_verified_name} 的 SQL 视为写入真实 target={real_target}；"
        "不要因为目标表带 not_verified_ 前缀而返回空 lineage。\n\n"
    )


def _strip_shell_strings_for_braces(line: str) -> str:
    line = re.sub(r"\$\{[^}]*\}", "", line)
    line = re.sub(r"'[^']*'", "''", line)
    line = re.sub(r'"(?:[^"\\]|\\.)*"', '""', line)
    line = re.sub(r"`[^`]*`", "``", line)
    return line


def _shell_brace_delta(line: str) -> int:
    stripped = _strip_shell_strings_for_braces(line)
    return stripped.count("{") - stripped.count("}")


def _extract_shell_functions(script: str) -> Dict[str, Tuple[int, int, str]]:
    lines = script.splitlines()
    functions: Dict[str, Tuple[int, int, str]] = {}
    i = 0
    while i < len(lines):
        m = _SHELL_FUNCTION_START_RE.match(lines[i])
        if not m:
            i += 1
            continue
        name = m.group(1)
        depth = _shell_brace_delta(lines[i])
        end = i
        while end + 1 < len(lines) and depth > 0:
            end += 1
            depth += _shell_brace_delta(lines[end])
        functions[name] = (i, end, "\n".join(lines[i : end + 1]).strip())
        i = end + 1
    return functions


def _command_first_word(command: str) -> str:
    command = command.strip()
    command = re.sub(r"^(?:time|command|builtin|source|\.)\s+", "", command)
    m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\b", command)
    return m.group(1) if m else ""


def _called_shell_functions(body: str, function_names: Set[str]) -> Set[str]:
    calls: Set[str] = set()
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for part in _SHELL_SPLIT_COMMAND_RE.split(line):
            word = _command_first_word(part)
            if word in function_names and word not in _SHELL_NON_CALL_WORDS:
                calls.add(word)
    return calls


def _entrypoint_names_for_job(job_file_name: str) -> List[str]:
    base = Path(job_file_name).name
    if "." in base:
        base = base.rsplit(".", 1)[0]
    return [f"{base}_run", f"{base}run"]


def _prune_shell_job_to_entrypoint(etl_script: str, job_file_name: str) -> str:
    functions = _extract_shell_functions(etl_script)
    if not functions:
        return etl_script

    entrypoint = next((name for name in _entrypoint_names_for_job(job_file_name) if name in functions), "")
    if not entrypoint:
        return etl_script

    reachable: Set[str] = set()
    queue = [entrypoint]
    function_names = set(functions)
    while queue:
        name = queue.pop(0)
        if name in reachable:
            continue
        reachable.add(name)
        _, _, body = functions[name]
        for called in sorted(_called_shell_functions(body, function_names)):
            if called not in reachable:
                queue.append(called)

    function_ranges = [(start, end) for start, end, _body in functions.values()]
    preamble_lines: List[str] = []
    for idx, line in enumerate(etl_script.splitlines()):
        if any(start <= idx <= end for start, end in function_ranges):
            continue
        if line.strip():
            preamble_lines.append(line)

    kept_functions = [
        body
        for name, (_start, _end, body) in functions.items()
        if name in reachable
    ]
    if not kept_functions:
        return etl_script

    header = (
        f"# DataHub lineage parser: only functions reachable from entrypoint {entrypoint} are included.\n"
        "# Unreachable shell functions are omitted to avoid parsing backup/dead code."
    )
    parts = [header]
    if preamble_lines:
        parts.append("\n".join(preamble_lines).strip())
    parts.extend(kept_functions)
    return "\n\n".join(part for part in parts if part.strip())


def _build_user_message(etl_script: str, max_chars: int = 120_000, job_file_name: str = "") -> str:
    etl_for_prompt = (
        _prune_shell_job_to_entrypoint(etl_script, job_file_name)
        if job_file_name.endswith(".job")
        else etl_script
    )
    if job_file_name.endswith(".job"):
        segments = [s.strip() for s in etl_for_prompt.split(";") if s.strip()]
        if segments:
            parts = [f"-- SQL 段 {i + 1} --\n{seg}" for i, seg in enumerate(segments)]
            body = "\n\n".join(parts)
        else:
            body = etl_for_prompt
    else:
        body = etl_for_prompt
    if len(body) > max_chars:
        body = body[:max_chars] + "\n... [truncated]"
    alias_prompt = _not_verified_alias_prompt(etl_for_prompt)
    return (
        "以下为 ETL 脚本全文，请按 lineage 数组格式提取每个目标表及其对应的上游表：\n\n"
        + alias_prompt
        + body
    )


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


def call_llm_extract(etl_script: str, timeout_sec: int = 90, job_file_name: str = "") -> Dict[str, Any]:
    """调用 DeepSeek 提取目标/上游表，返回 LLM 原始 JSON payload。失败时抛出异常。"""
    db, dk, dm = _deepseek_config()
    user_msg = _build_user_message(etl_script, job_file_name=job_file_name)
    return _openai_chat_json_with_fallback(db, dk, dm, user_msg, timeout_sec)


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
    parser = argparse.ArgumentParser(description="DeepSeek 表血缘提取")
    parser.add_argument("--etl-file", type=Path, help="ETL 脚本文本文件路径")
    parser.add_argument("--etl-text", type=str, default="", help="直接传入脚本内容（小脚本调试）")
    parser.add_argument("--job-file-name", default="inline.job", help="用于脚本分段提示，如 xxx.job / x.py")
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

    try:
        d = call_llm_extract(etl, timeout_sec=args.timeout, job_file_name=args.job_file_name)
    except Exception as exc:
        d = {"deepseek_error": str(exc)}
    print(json.dumps(d, ensure_ascii=False, indent=2))
    if args.out_json:
        args.out_json.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("已写入: %s", args.out_json)

    return 0 if "deepseek_error" not in d else 1


if __name__ == "__main__":
    raise SystemExit(main())
