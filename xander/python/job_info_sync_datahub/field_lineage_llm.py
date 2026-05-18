"""LLM extraction for independent field-level lineage candidates."""

from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .field_lineage_models import (
    FieldLineageCandidate,
    FieldLineageInput,
    FieldLineageParseResult,
    FieldLineageReviewStatus,
    UnresolvedField,
)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.I)

FIELD_LINEAGE_SYSTEM_PROMPT = """你是便利店数据仓库的字段级血缘分析专家。
你会收到一个 Hive 表的 ETL 脚本和执行命令。请只输出 JSON 对象，不要 markdown。

输出 schema:
{
  "target_table": "db.table",
  "mappings": [
    {
      "target_field": "目标字段",
      "source_table": "来源表 db.table",
      "source_field": "来源字段",
      "transform_expression": "字段转换表达式",
      "evidence_sql": "能证明该映射的 SQL 片段",
      "confidence": "HIGH|MEDIUM|LOW",
      "notes": "不确定点或解释"
    }
  ],
  "unresolved_fields": [
    {"target_field": "目标字段", "reason": "无法确认原因"}
  ]
}

规则:
- 只生成候选结果，宁可把不确定字段放到 unresolved_fields，也不要编造来源。
- Python 脚本中的 SQL 字符串、临时表、多段 SQL 可综合判断，但 evidence_sql 必须回指到原始脚本片段。
- **每条 mapping 必须填写 transform_expression**（如 `coalesce(a,b)`、`cast(x as bigint)`、或直接列名），用于 DataHub UI 展示 LOGIC。
- 同一目标字段若有多来源（如 coalesce），可为每个来源各写一条 mapping，transform_expression 填同一完整表达式。
- review_status 不需要输出；系统会统一置为 PENDING。
- target_table 必须是本次输入表。
"""


def _deepseek_config() -> Tuple[str, str, str]:
    base = os.environ.get("DEEPSEEK_OPENAI_BASE_URL", "https://api.deepseek.com").rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
    if not key:
        raise RuntimeError("缺少 DeepSeek API Key：请设置 DEEPSEEK_API_KEY")
    return base, key, model


def _llm_ssl_context() -> Optional[ssl.SSLContext]:
    verify = os.environ.get("BLF_LLM_SSL_VERIFY", "").strip().lower()
    if verify in ("0", "false", "no"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None


def _parse_json_object(text: str) -> Dict[str, Any]:
    cleaned = text.strip()
    match = _JSON_FENCE_RE.search(cleaned)
    if match:
        cleaned = match.group(1).strip()
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise RuntimeError("LLM 输出不是 JSON object")
    return parsed


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def parse_field_lineage_payload(text: str) -> FieldLineageParseResult:
    """Parse the LLM JSON payload into reviewable candidate rows."""
    payload = _parse_json_object(text)
    target_table = _as_str(payload.get("target_table")).strip().lower()
    mappings_raw = payload.get("mappings")
    unresolved_raw = payload.get("unresolved_fields")

    mappings: List[FieldLineageCandidate] = []
    if isinstance(mappings_raw, list):
        for item in mappings_raw:
            if not isinstance(item, dict):
                continue
            mappings.append(
                FieldLineageCandidate(
                    target_table=target_table,
                    target_field=_as_str(item.get("target_field")).strip().lower(),
                    source_table=_as_str(item.get("source_table")).strip().lower(),
                    source_field=_as_str(item.get("source_field")).strip().lower(),
                    transform_expression=_as_str(item.get("transform_expression")).strip(),
                    evidence_sql=_as_str(item.get("evidence_sql")).strip(),
                    confidence=_as_str(item.get("confidence")).strip().upper(),
                    llm_notes=_as_str(item.get("notes")).strip(),
                    review_status=FieldLineageReviewStatus.PENDING,
                )
            )

    unresolved: List[UnresolvedField] = []
    if isinstance(unresolved_raw, list):
        for item in unresolved_raw:
            if not isinstance(item, dict):
                continue
            unresolved.append(
                UnresolvedField(
                    target_field=_as_str(item.get("target_field")).strip().lower(),
                    reason=_as_str(item.get("reason")).strip(),
                )
            )

    return FieldLineageParseResult(
        target_table=target_table,
        mappings=mappings,
        unresolved_fields=unresolved,
    )


def build_field_lineage_user_message(source_input: FieldLineageInput) -> str:
    return (
        f"目标表: {source_input.table_name}\n\n"
        f"执行命令:\n{source_input.execute_shell}\n\n"
        f"ETL 脚本:\n{source_input.etl_script}"
    )


def call_llm_extract_field_lineage(
    source_input: FieldLineageInput,
    timeout_sec: int = 180,
) -> Tuple[FieldLineageParseResult, str]:
    """Call an OpenAI-compatible LLM and parse field-lineage candidates."""
    base, api_key, model = _deepseek_config()
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": FIELD_LINEAGE_SYSTEM_PROMPT},
            {"role": "user", "content": build_field_lineage_user_message(source_input)},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec, context=_llm_ssl_context()) as resp:
            outer = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(f"字段血缘 LLM 调用失败 HTTP {exc.code}: {detail}") from exc

    content = outer["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise RuntimeError("字段血缘 LLM 返回 content 不是字符串")
    return parse_field_lineage_payload(content), model
