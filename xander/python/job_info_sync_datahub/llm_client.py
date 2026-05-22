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
