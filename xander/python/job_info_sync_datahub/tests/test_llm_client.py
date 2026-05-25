from __future__ import annotations

import json
import logging
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from job_info_sync_datahub.field_lineage_llm import call_llm_extract_field_lineage
from job_info_sync_datahub.field_lineage_models import FieldLineageInput
from job_info_sync_datahub.llm_client import (
    LlmConfig,
    call_openai_compatible_chat_json,
    get_llm_config,
    llm_user_message_max_chars,
    normalize_openai_v1_base,
)


def _clear_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "BLF_LLM_BASE_URL",
        "BLF_LLM_API_KEY",
        "BLF_LLM_MODEL",
        "BLF_ACTIVE_LLM",
        "BLF_LLM_CONTEXT_TOKENS",
        "BLF_LINEAGE_LLM_PROMPT_MAX_CHARS",
        "DEEPSEEK_OPENAI_BASE_URL",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)


def test_user_message_max_chars_aligns_with_default_128k_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_llm_env(monkeypatch)
    budget = llm_user_message_max_chars(system_prompt_chars=2500)
    assert budget >= 360_000


def test_user_message_max_chars_honors_explicit_char_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_llm_env(monkeypatch)
    assert llm_user_message_max_chars(explicit_max_chars=200_000) == 200_000


def test_get_llm_config_uses_active_blf_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("BLF_ACTIVE_LLM", "blf")
    monkeypatch.setenv("BLF_LLM_BASE_URL", "http://token-pool.vip.blibee.com/v1")
    monkeypatch.setenv("BLF_LLM_API_KEY", "sk-new")
    monkeypatch.setenv("BLF_LLM_MODEL", "gpt-5.5")
    monkeypatch.setenv("DEEPSEEK_OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-old")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-pro")

    cfg = get_llm_config()

    assert cfg == LlmConfig(
        base_v1="http://token-pool.vip.blibee.com/v1",
        api_key="sk-new",
        model="gpt-5.5",
        provider="blf",
    )


def test_get_llm_config_defaults_to_deepseek_even_when_blf_variables_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("BLF_LLM_BASE_URL", "http://token-pool.vip.blibee.com/v1")
    monkeypatch.setenv("BLF_LLM_API_KEY", "sk-new")
    monkeypatch.setenv("BLF_LLM_MODEL", "gpt-5.5")
    monkeypatch.setenv("DEEPSEEK_OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-old")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-pro")

    cfg = get_llm_config()

    assert cfg == LlmConfig(
        base_v1="https://api.deepseek.com/v1",
        api_key="sk-old",
        model="deepseek-v4-pro",
        provider="deepseek",
    )


def test_get_llm_config_falls_back_to_deepseek(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_OPENAI_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-old")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-pro")

    cfg = get_llm_config()

    assert cfg == LlmConfig(
        base_v1="https://api.deepseek.com/v1",
        api_key="sk-old",
        model="deepseek-v4-pro",
        provider="deepseek",
    )


def test_get_llm_config_uses_production_token_pool_default_for_blf(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("BLF_ACTIVE_LLM", "blf")
    monkeypatch.setenv("BLF_LLM_API_KEY", "sk-new")

    cfg = get_llm_config()

    assert cfg.base_v1 == "http://token-pool.vip.blibee.com/v1"
    assert cfg.model == "gpt-5.5"
    assert cfg.provider == "blf"


def test_normalize_openai_v1_base_appends_v1_once() -> None:
    assert normalize_openai_v1_base("http://host:8317") == "http://host:8317/v1"
    assert normalize_openai_v1_base("http://host:8317/v1") == "http://host:8317/v1"
    assert normalize_openai_v1_base("http://host:8317/v1/") == "http://host:8317/v1"


def test_get_llm_config_missing_blf_key_mentions_active_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("BLF_ACTIVE_LLM", "blf")

    with pytest.raises(RuntimeError) as exc:
        get_llm_config()

    msg = str(exc.value)
    assert "BLF_ACTIVE_LLM=blf" in msg
    assert "BLF_LLM_API_KEY" in msg


def test_get_llm_config_missing_default_deepseek_key_mentions_switch_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_llm_env(monkeypatch)

    with pytest.raises(RuntimeError) as exc:
        get_llm_config()

    msg = str(exc.value)
    assert "BLF_ACTIVE_LLM=deepseek" in msg
    assert "DEEPSEEK_API_KEY" in msg
    assert "BLF_ACTIVE_LLM=blf" in msg


def test_call_openai_compatible_chat_json_posts_expected_payload() -> None:
    captured = {}
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(
        {"choices": [{"message": {"content": "{\"ok\": true}"}}]}
    ).encode("utf-8")

    def fake_urlopen(req, timeout=None, context=None):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["auth"] = req.headers.get("Authorization")
        return response

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = call_openai_compatible_chat_json(
            LlmConfig(
                base_v1="http://token-pool.vip.blibee.com/v1",
                api_key="sk-test",
                model="gpt-5.5",
                provider="blf",
            ),
            system_prompt="system",
            user_message="user",
            timeout_sec=12,
            response_format_json=True,
        )

    assert result == {"ok": True}
    assert captured["url"] == "http://token-pool.vip.blibee.com/v1/chat/completions"
    assert captured["timeout"] == 12
    assert captured["auth"] == "Bearer sk-test"
    assert captured["body"] == {
        "model": "gpt-5.5",
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "user"},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }


def test_call_openai_compatible_chat_json_logs_provider_and_model(caplog: pytest.LogCaptureFixture) -> None:
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(
        {"choices": [{"message": {"content": "{\"ok\": true}"}}]}
    ).encode("utf-8")

    with patch("urllib.request.urlopen", return_value=response), caplog.at_level(
        logging.INFO,
        logger="job_info_sync_datahub.llm_client",
    ):
        call_openai_compatible_chat_json(
            LlmConfig(
                base_v1="http://token-pool.vip.blibee.com/v1",
                api_key="sk-test",
                model="gpt-5.5",
                provider="blf",
            ),
            system_prompt="system",
            user_message="user",
            timeout_sec=12,
        )

    assert "provider=blf" in caplog.text
    assert "model=gpt-5.5" in caplog.text
    assert "base=http://token-pool.vip.blibee.com/v1" in caplog.text


def test_call_openai_compatible_chat_json_falls_back_without_response_format() -> None:
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(
        {"choices": [{"message": {"content": "{\"ok\": true}"}}]}
    ).encode("utf-8")
    calls = []

    def fake_urlopen(req, timeout=None, context=None):
        calls.append(json.loads(req.data.decode("utf-8")))
        if len(calls) == 1:
            raise urllib.error.URLError("unsupported response_format")
        return response

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = call_openai_compatible_chat_json(
            LlmConfig(
                base_v1="http://token-pool.vip.blibee.com/v1",
                api_key="sk-test",
                model="gpt-5.5",
                provider="blf",
            ),
            system_prompt="system",
            user_message="user",
            timeout_sec=12,
            response_format_json=True,
            fallback_system_suffix="\nonly json",
        )

    assert result == {"ok": True}
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in calls[1]
    assert calls[1]["messages"][0]["content"] == "system\nonly json"


def test_table_lineage_llm_uses_blf_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from job_info_sync_datahub.lineage_llm_compare import call_llm_extract

    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("BLF_ACTIVE_LLM", "blf")
    monkeypatch.setenv("BLF_LLM_BASE_URL", "http://token-pool.vip.blibee.com/v1")
    monkeypatch.setenv("BLF_LLM_API_KEY", "sk-new")
    monkeypatch.setenv("BLF_LLM_MODEL", "gpt-5.5")
    captured = {}
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "lineage": [
                                    {
                                        "target": {"db": "default", "table": "dw_target"},
                                        "upstreams": [{"db": "default", "table": "ods_source"}],
                                    }
                                ],
                                "notes": "",
                            }
                        )
                    }
                }
            ]
        }
    ).encode("utf-8")

    def fake_urlopen(req, timeout=None, context=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return response

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = call_llm_extract(
            "insert overwrite table default.dw_target select * from default.ods_source",
            timeout_sec=10,
            job_file_name="dw_target.job",
        )

    assert result["lineage"][0]["target"] == {"db": "default", "table": "dw_target"}
    assert captured["url"] == "http://token-pool.vip.blibee.com/v1/chat/completions"
    assert captured["body"]["model"] == "gpt-5.5"
    assert captured["body"]["response_format"] == {"type": "json_object"}


def test_field_lineage_llm_uses_blf_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("BLF_ACTIVE_LLM", "blf")
    monkeypatch.setenv("BLF_LLM_BASE_URL", "http://token-pool.vip.blibee.com")
    monkeypatch.setenv("BLF_LLM_API_KEY", "sk-new")
    monkeypatch.setenv("BLF_LLM_MODEL", "gpt-5.4-mini")
    captured = {}
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "target_table": "default.dw_target",
                                "mappings": [],
                                "unresolved_fields": [],
                            }
                        )
                    }
                }
            ]
        }
    ).encode("utf-8")

    def fake_urlopen(req, timeout=None, context=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return response

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        parsed, model = call_llm_extract_field_lineage(
            FieldLineageInput(
                dataset_urn="urn:li:dataset:(urn:li:dataPlatform:hive,blf-prod-hive.default.dw_target,PROD)",
                table_name="default.dw_target",
                etl_script="insert overwrite table default.dw_target select 1",
                execute_shell="sh run.sh",
            ),
            timeout_sec=10,
        )

    assert parsed.target_table == "default.dw_target"
    assert model == "gpt-5.4-mini"
    assert captured["url"] == "http://token-pool.vip.blibee.com/v1/chat/completions"
    assert captured["body"]["model"] == "gpt-5.4-mini"
    assert captured["body"]["response_format"] == {"type": "json_object"}
