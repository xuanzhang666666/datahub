"""Shared OpenAI-compatible LLM client configuration and request helpers."""

from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .logging_utils import get_logger

logger = get_logger("llm_client")

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.I)
DEFAULT_BLF_LLM_BASE_URL = "http://token-pool.vip.blibee.com/v1"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_ACTIVE_LLM = "deepseek"

# OpenAI-compatible 模型 context 预算（可通过环境变量与 BLF_LLM_MODEL 对齐）
DEFAULT_LLM_CONTEXT_TOKENS = 128_000
DEFAULT_LLM_RESERVED_OUTPUT_TOKENS = 4_096
DEFAULT_LLM_RESERVED_SYSTEM_TOKENS = 2_500
DEFAULT_LLM_CHARS_PER_TOKEN = 3
MIN_USER_MESSAGE_CHARS = 10_000


@dataclass(frozen=True)
class LlmConfig:
    base_v1: str
    api_key: str
    model: str
    provider: str


def normalize_openai_v1_base(url: str) -> str:
    base = url.strip().rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base


def _read_positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if raw.isdigit():
        return max(1, int(raw))
    return default


def llm_user_message_max_chars(
    *,
    system_prompt_chars: int = 0,
    explicit_max_chars: Optional[int] = None,
    legacy_env_name: str = "BLF_LINEAGE_LLM_PROMPT_MAX_CHARS",
) -> int:
    """按模型 context 推算 user message 字符上限（保守估计 token）。

    优先级：explicit_max_chars > legacy_env_name（如 BLF_LINEAGE_LLM_PROMPT_MAX_CHARS）
    > BLF_LLM_CONTEXT_TOKENS - 输出预留 - system 预留。

    环境变量：
      BLF_LLM_CONTEXT_TOKENS（默认 128000，与 gpt-5.5 等 128k 窗口对齐）
      BLF_LLM_RESERVED_OUTPUT_TOKENS（默认 4096）
      BLF_LLM_RESERVED_SYSTEM_TOKENS（未传 system_prompt_chars 时使用，默认 2500）
      BLF_LLM_CHARS_PER_TOKEN（默认 3，SQL/中文混合偏保守）
    """
    if explicit_max_chars is not None and explicit_max_chars > 0:
        return explicit_max_chars
    legacy = os.environ.get(legacy_env_name, "").strip()
    if legacy.isdigit():
        return max(MIN_USER_MESSAGE_CHARS, int(legacy))

    context_tokens = _read_positive_int_env("BLF_LLM_CONTEXT_TOKENS", DEFAULT_LLM_CONTEXT_TOKENS)
    output_tokens = _read_positive_int_env(
        "BLF_LLM_RESERVED_OUTPUT_TOKENS", DEFAULT_LLM_RESERVED_OUTPUT_TOKENS
    )
    chars_per_token = _read_positive_int_env("BLF_LLM_CHARS_PER_TOKEN", DEFAULT_LLM_CHARS_PER_TOKEN)
    if system_prompt_chars > 0:
        system_tokens = (system_prompt_chars + chars_per_token - 1) // chars_per_token
    else:
        system_tokens = _read_positive_int_env(
            "BLF_LLM_RESERVED_SYSTEM_TOKENS", DEFAULT_LLM_RESERVED_SYSTEM_TOKENS
        )
    user_tokens = max(
        MIN_USER_MESSAGE_CHARS // chars_per_token,
        context_tokens - output_tokens - system_tokens,
    )
    return user_tokens * chars_per_token


def get_llm_config() -> LlmConfig:
    """Resolve LLM config using BLF_ACTIVE_LLM, defaulting to deepseek."""
    active = os.environ.get("BLF_ACTIVE_LLM", DEFAULT_ACTIVE_LLM).strip().lower()
    if active in ("blf", "token-pool", "token_pool"):
        blf_base = os.environ.get("BLF_LLM_BASE_URL", "").strip()
        blf_key = os.environ.get("BLF_LLM_API_KEY", "").strip()
        blf_model = os.environ.get("BLF_LLM_MODEL", "").strip()
        if not blf_key:
            raise RuntimeError("缺少 LLM API Key：当前 BLF_ACTIVE_LLM=blf，请设置 BLF_LLM_API_KEY")
        return LlmConfig(
            base_v1=normalize_openai_v1_base(blf_base or DEFAULT_BLF_LLM_BASE_URL),
            api_key=blf_key,
            model=blf_model or "gpt-5.5",
            provider="blf",
        )
    if active != "deepseek":
        raise RuntimeError(
            f"不支持的 BLF_ACTIVE_LLM={active!r}；当前支持 deepseek、blf"
        )

    deepseek_base = os.environ.get("DEEPSEEK_OPENAI_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL)
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    deepseek_model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
    if not deepseek_key:
        raise RuntimeError(
            "缺少 LLM API Key：当前 BLF_ACTIVE_LLM=deepseek 或未设置；"
            "请设置 DEEPSEEK_API_KEY，或设置 BLF_ACTIVE_LLM=blf 并配置 BLF_LLM_API_KEY"
        )
    return LlmConfig(
        base_v1=normalize_openai_v1_base(deepseek_base),
        api_key=deepseek_key,
        model=deepseek_model,
        provider="deepseek",
    )


def llm_ssl_context() -> Optional[ssl.SSLContext]:
    verify = os.environ.get("BLF_LLM_SSL_VERIFY", "").strip().lower()
    if verify in ("0", "false", "no"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None


def parse_llm_json_object(text: str) -> Dict[str, Any]:
    cleaned = text.strip()
    match = _JSON_FENCE_RE.search(cleaned)
    if match:
        cleaned = match.group(1).strip()
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise RuntimeError("LLM 输出不是 JSON object")
    return parsed


def _decode_chat_response(raw: str) -> Dict[str, Any]:
    content = decode_chat_response_content(raw)
    if isinstance(content, str):
        return parse_llm_json_object(content)
    if isinstance(content, dict):
        return content
    raise RuntimeError(f"Unexpected message content type: {type(content)}")


def decode_chat_response_content(raw: str) -> Any:
    outer = json.loads(raw)
    try:
        content = outer["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected API response: {raw[:1500]}") from exc
    return content


def _post_chat_completion_text(
    config: LlmConfig,
    payload: Dict[str, Any],
    timeout_sec: int,
) -> Dict[str, Any]:
    url = config.base_v1.rstrip("/") + "/chat/completions"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec, context=llm_ssl_context()) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(f"LLM 调用失败 HTTP {exc.code} {url}: {detail}") from exc

    content = decode_chat_response_content(raw)
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    return {
        "content": content,
        "raw_response": json.loads(raw),
    }


def _post_chat_completion(
    config: LlmConfig,
    payload: Dict[str, Any],
    timeout_sec: int,
) -> Dict[str, Any]:
    url = config.base_v1.rstrip("/") + "/chat/completions"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec, context=llm_ssl_context()) as resp:
            return _decode_chat_response(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(f"LLM 调用失败 HTTP {exc.code} {url}: {detail}") from exc


def call_openai_compatible_chat_json(
    config: LlmConfig,
    *,
    system_prompt: str,
    user_message: str,
    timeout_sec: int,
    response_format_json: bool = True,
    temperature: float = 0.1,
    fallback_system_suffix: str = "\n只输出 JSON，不要用 markdown。",
) -> Dict[str, Any]:
    logger.info(
        "LLM 请求配置: provider=%s base=%s model=%s timeout_sec=%s response_format_json=%s",
        config.provider,
        config.base_v1,
        config.model,
        timeout_sec,
        response_format_json,
    )
    payload: Dict[str, Any] = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": temperature,
    }
    if response_format_json:
        payload["response_format"] = {"type": "json_object"}

    if not response_format_json:
        return _post_chat_completion(config, payload, timeout_sec)

    try:
        return _post_chat_completion(config, payload, timeout_sec)
    except (RuntimeError, json.JSONDecodeError, urllib.error.URLError) as first:
        logger.warning("首次 LLM 调用（含 response_format）失败，重试无 json_object: %s", first)

    fallback_payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system_prompt + fallback_system_suffix},
            {"role": "user", "content": user_message},
        ],
        "temperature": temperature,
    }
    return _post_chat_completion(config, fallback_payload, timeout_sec)


def call_openai_compatible_chat_text(
    config: LlmConfig,
    *,
    system_prompt: str,
    user_message: str,
    timeout_sec: int,
    temperature: float = 0.1,
) -> Dict[str, Any]:
    logger.info(
        "LLM 请求配置: provider=%s base=%s model=%s timeout_sec=%s response_format_json=%s",
        config.provider,
        config.base_v1,
        config.model,
        timeout_sec,
        False,
    )
    payload: Dict[str, Any] = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": temperature,
    }
    return _post_chat_completion_text(config, payload, timeout_sec)
