"""Unit tests for job-dependency-plugin logic port.

These tests verify that the Python port of the Java plugin's dependency logic
matches the documented semantics:

  - parse_trigger_condition   → Java TriggerConditionParser.parse
  - validate_trigger_condition → Java TriggerConditionParser.checkBeforeParse/checkAfterParse
  - parse_upstream_job_params → Java UpstreamJobParams.getJobParams
  - format_time_hour_token    → Java getOnlyBuildOnceFormatStringContext
  - _build_expected_tokens    → Java AbstractTriggerDownstream.checkHourCondition 等
  - _match_run_against_expected → Java matchTokenFromBuildHistory
  - _threshold_met            → Java ResultCondition.isMet
  - check_upstream_time_hour_match / diagnose_dependency_trigger
                              → Java JobDependencyBuildTrigger.shouldTriggerBuild
  - is_user_triggered_build   → Java BuildTriggerUtils.isTriggerByUser
"""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime
from unittest.mock import patch

import pytest

from blf_schedule_mcp import tools
from blf_schedule_mcp.jenkins_client import JenkinsClient
from blf_schedule_mcp.tools import (
    TriggerConditionInfo,
    _build_expected_tokens,
    _check_date_range,
    _find_logic_symbol,
    _format_expected_day,
    _format_expected_hour,
    _format_expected_month,
    _format_expected_week,
    _match_run_against_expected,
    _parse_date_value,
    _parse_time_hour_token,
    _strip_date_marker,
    _threshold_met,
    check_upstream_time_hour_match,
    diagnose_dependency_trigger,
    format_time_hour_token,
    format_time_hour_token_tool,
    is_user_triggered_build,
    parse_trigger_condition,
    parse_trigger_condition_tool,
    parse_upstream_job_params,
    parse_upstream_job_params_tool,
    validate_trigger_condition,
)


# ---------------------------------------------------------------------------
# 1) TriggerConditionParser 端口
# ---------------------------------------------------------------------------


class TestFindLogicSymbol:
    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("1 & 2", "AND"),
            ("01&02", "AND"),
            ("1 | 2", "OR"),
            ("01|02", "OR"),
            ("1", "NO"),
            ("@$", "NO"),
            ("*$ - 1", "NO"),
        ],
    )
    def test_find_logic_symbol(self, expr: str, expected: str) -> None:
        assert _find_logic_symbol(expr) == expected


class TestParseDateValue:
    def test_plain_number(self) -> None:
        assert _parse_date_value("1") == (1, False, False)

    def test_self_marker(self) -> None:
        assert _parse_date_value("%$ - 1") == (-1, True, False)

    def test_context_marker(self) -> None:
        assert _parse_date_value("*$ + 2") == (2, False, True)

    def test_strip_markers(self) -> None:
        assert _strip_date_marker("01") == "01"
        assert _strip_date_marker("*$ - 1") == "-1"
        assert _strip_date_marker("%$ + 5") == "+5"
        assert _strip_date_marker("@$") == ""


class TestValidateTriggerCondition:
    @pytest.mark.parametrize(
        "condition",
        [
            "h = 1",          # 单值 (NOTE: Java 正则要求两位数,这里会被拒绝 — 与 Java 行为一致)
            "h = 01",
            "h = 01 & 02",
            "h = 01 | 02",
            "d = 01 & #$",    # #$ 是月末
            "d = @$",
            "h = *$ - 1",
            "h = %$ + 1",
            "w = 1",
            "m = 12",
        ],
    )
    def test_valid_examples(self, condition: str) -> None:
        result = validate_trigger_condition(condition)
        assert result["valid"], f"expected valid but got errors: {result['errors']}"

    @pytest.mark.parametrize(
        "condition",
        [
            "",                          # 空
            "x = 1",                     # 不支持的类型字符
            "h = abc",                   # 非数字
            "h = 25",                    # 超出范围
            "h = 01 & 25",               # 部分超出
            "h = 01 | & 02",             # 空值
            "d = 32",                    # 天超出
            "m = 13",                    # 月超出
        ],
    )
    def test_invalid_examples(self, condition: str) -> None:
        result = validate_trigger_condition(condition)
        assert not result["valid"], f"expected invalid but {condition!r} passed"

    def test_self_marker_must_be_no_logic(self) -> None:
        # Java: 包含 %$ 或 *$ 时,不能使用 & 或 |
        result = validate_trigger_condition("h = %$ - 1 & 02")
        assert not result["valid"]


class TestParseTriggerCondition:
    def test_hour_and(self) -> None:
        info = parse_trigger_condition("h = 01 & 02")
        assert info.date_type == "HOUR"
        assert info.logic_symbol == "AND"
        assert info.date_list == ["01", "02"]

    def test_day_or(self) -> None:
        info = parse_trigger_condition("d = 01 | 02")
        assert info.date_type == "DAY"
        assert info.logic_symbol == "OR"
        assert info.date_list == ["01", "02"]

    def test_any_day(self) -> None:
        info = parse_trigger_condition("d = @$")
        assert info.date_type == "DAY"
        assert info.logic_symbol == "NO"
        assert info.date_list == ["@$"]

    def test_self_dependency(self) -> None:
        info = parse_trigger_condition("h = %$ - 1")
        assert info.date_type == "HOUR"
        assert info.logic_symbol == "NO"
        assert info.date_list == ["%$ - 1"]

    def test_context_offset(self) -> None:
        info = parse_trigger_condition("h = *$ - 1")
        assert info.date_type == "HOUR"
        assert info.logic_symbol == "NO"
        assert info.date_list == ["*$ - 1"]

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_trigger_condition("h = 25")


class TestParseTriggerConditionTool:
    def test_tool_success(self) -> None:
        result = parse_trigger_condition_tool(condition_str="h = 01 & 02")
        assert result["success"] is True
        assert result["summary"]["date_type"] == "HOUR"
        assert result["summary"]["logic_symbol"] == "AND"
        assert result["summary"]["date_list"] == ["01", "02"]

    def test_tool_failure(self) -> None:
        result = parse_trigger_condition_tool(condition_str="h = abc")
        assert result["success"] is False
        assert result["error_type"] == "invalid_input"

    def test_tool_empty(self) -> None:
        result = parse_trigger_condition_tool(condition_str="")
        assert result["success"] is False


# ---------------------------------------------------------------------------
# 2) UpstreamJobParams 端口
# ---------------------------------------------------------------------------


class TestParseUpstreamJobParams:
    def test_empty(self) -> None:
        assert parse_upstream_job_params("") == []
        assert parse_upstream_job_params("   ") == []

    def test_plain(self) -> None:
        result = parse_upstream_job_params("A,B")
        assert result == [
            {"type": "plain", "raw": "A", "value": "A"},
            {"type": "plain", "raw": "B", "value": "B"},
        ]

    def test_job_param(self) -> None:
        result = parse_upstream_job_params("A.company")
        assert result == [
            {"type": "JobParam", "raw": "A.company", "job_name": "A", "param": "company"}
        ]

    def test_job_date_param(self) -> None:
        result = parse_upstream_job_params("A.3.company,B.date,B.param")
        assert result == [
            {"type": "JobDateParam", "raw": "A.3.company", "job_name": "A", "date_type": "3", "param": "company"},
            {"type": "JobParam", "raw": "B.date", "job_name": "B", "param": "date"},
            {"type": "JobParam", "raw": "B.param", "job_name": "B", "param": "param"},
        ]

    def test_mixed(self) -> None:
        result = parse_upstream_job_params("A,B.param,C.3.company")
        assert [r["type"] for r in result] == ["plain", "JobParam", "JobDateParam"]

    def test_too_many_dots(self) -> None:
        with pytest.raises(ValueError):
            parse_upstream_job_params("A.B.C.D")


class TestParseUpstreamJobParamsTool:
    def test_tool(self) -> None:
        result = parse_upstream_job_params_tool(params_str="A.param,B.3.company")
        assert result["success"] is True
        assert result["summary"]["count"] == 2


# ---------------------------------------------------------------------------
# 3) DateUtils / getOnlyBuildOnceFormatStringContext 端口
# ---------------------------------------------------------------------------


class TestParseTimeHourToken:
    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            ("2026/06/23/14", datetime(2026, 6, 23, 14)),
            ("2025/01/01/00", datetime(2025, 1, 1, 0)),
        ],
    )
    def test_parse(self, token: str, expected: datetime) -> None:
        assert _parse_time_hour_token(token) == expected

    def test_invalid(self) -> None:
        with pytest.raises(ValueError):
            _parse_time_hour_token("not-a-date")

    def test_empty(self) -> None:
        with pytest.raises(ValueError):
            _parse_time_hour_token("")


class TestFormatTimeHourToken:
    def test_hour_unchanged(self) -> None:
        result = format_time_hour_token("2026/06/23/14", "HOUR")
        assert result["output"] == "2026/06/23/14"
        assert result["parsed"] is True

    def test_day(self) -> None:
        result = format_time_hour_token("2026/06/23/14", "DAY")
        assert result["output"] == "2026/06/23"

    def test_week(self) -> None:
        # 2026/06/23 是 Tuesday → 周一是 2026/06/22
        result = format_time_hour_token("2026/06/23/14", "WEEK")
        assert result["output"] == "2026/06/22"

    def test_month(self) -> None:
        result = format_time_hour_token("2026/06/23/14", "MONTH")
        assert result["output"] == "2026/06"

    def test_unsupported_date_type(self) -> None:
        result = format_time_hour_token("2026/06/23/14", "GARBAGE")
        assert result["parsed"] is False
        assert "error" in result

    def test_empty_token(self) -> None:
        result = format_time_hour_token("", "HOUR")
        assert result["output"] == ""


class TestFormatTimeHourTokenTool:
    def test_tool(self) -> None:
        result = format_time_hour_token_tool(token="2026/06/23/14", date_type="DAY")
        assert result["success"] is True
        assert result["summary"]["output"] == "2026/06/23"


# ---------------------------------------------------------------------------
# 4) AbstractTriggerDownstream 时间计算端口
# ---------------------------------------------------------------------------


class TestFormatExpectedHour:
    def test_absolute(self) -> None:
        token = datetime(2026, 6, 23, 14)
        # 绝对 hour: 同一天的小时
        assert _format_expected_hour(token, 1, absolute=True) == "2026/06/23/01"
        assert _format_expected_hour(token, 23, absolute=True) == "2026/06/23/23"

    def test_relative(self) -> None:
        token = datetime(2026, 6, 23, 14)
        # 相对 hour: 在 token 当前时间上偏移
        assert _format_expected_hour(token, -1, absolute=False) == "2026/06/23/13"
        assert _format_expected_hour(token, 1, absolute=False) == "2026/06/23/15"


class TestFormatExpectedDay:
    def test_absolute(self) -> None:
        token = datetime(2026, 6, 23, 14)
        assert _format_expected_day(token, 1, absolute=True) == "2026/06/01"
        assert _format_expected_day(token, 15, absolute=True) == "2026/06/15"

    def test_relative(self) -> None:
        token = datetime(2026, 6, 23, 14)
        assert _format_expected_day(token, 1, absolute=False) == "2026/06/24"
        assert _format_expected_day(token, -1, absolute=False) == "2026/06/22"


class TestFormatExpectedWeek:
    def test_week(self) -> None:
        # 2026/06/23 是 Tuesday (weekday=1), Monday 是 2026/06/22
        token = datetime(2026, 6, 23, 14)
        assert _format_expected_week(token, 1) == "2026/06/22"  # Monday
        assert _format_expected_week(token, 7) == "2026/06/28"  # next Sunday


class TestFormatExpectedMonth:
    def test_absolute(self) -> None:
        token = datetime(2026, 6, 23, 14)
        assert _format_expected_month(token, 1, absolute=True) == "2026/01"
        assert _format_expected_month(token, 6, absolute=True) == "2026/06"


class TestCheckDateRange:
    def test_hour_in_range(self) -> None:
        assert _check_date_range("01", "HOUR") == []
        assert _check_date_range("23", "HOUR") == []

    def test_hour_out_of_range(self) -> None:
        errors = _check_date_range("25", "HOUR")
        assert errors and "超出" in errors[0]

    def test_day_in_range(self) -> None:
        assert _check_date_range("31", "DAY") == []

    def test_day_out_of_range(self) -> None:
        errors = _check_date_range("32", "DAY")
        assert errors


# ---------------------------------------------------------------------------
# 5) matchTokenFromBuildHistory 端口
# ---------------------------------------------------------------------------


class TestMatchRunAgainstExpected:
    def test_hour_match(self) -> None:
        token = datetime(2026, 6, 23, 14)
        info = TriggerConditionInfo("HOUR", "AND", ["01", "02"])
        expected = _build_expected_tokens(info, token)
        # 上游构建在 2026/06/23/01 → 命中
        result = _match_run_against_expected("2026/06/23/01", expected)
        assert result is not None
        assert result["raw_date"] == "01"

    def test_hour_no_match(self) -> None:
        token = datetime(2026, 6, 23, 14)
        info = TriggerConditionInfo("HOUR", "AND", ["01", "02"])
        expected = _build_expected_tokens(info, token)
        # 上游构建在 2026/06/23/05 → 不命中
        assert _match_run_against_expected("2026/06/23/05", expected) is None

    def test_empty_time_hour(self) -> None:
        assert _match_run_against_expected(None, [{"expected": "2026/06/23/01"}]) is None
        assert _match_run_against_expected("", [{"expected": "2026/06/23/01"}]) is None

    def test_invalid_time_hour(self) -> None:
        assert _match_run_against_expected("not-a-date", [{"expected": "2026/06/23/01"}]) is None

    def test_day_match(self) -> None:
        token = datetime(2026, 6, 23, 14)
        info = TriggerConditionInfo("DAY", "NO", ["@$"])
        expected = _build_expected_tokens(info, token)
        # 上游构建在 2026/06/23/14 → 命中 @$ (任意一天)
        result = _match_run_against_expected("2026/06/23/14", expected)
        assert result is not None

    def test_self_dependency(self) -> None:
        token = datetime(2026, 6, 23, 14)
        info = TriggerConditionInfo("HOUR", "NO", ["%$ - 1"])
        expected = _build_expected_tokens(info, token)
        # self: relative offset → 2026/06/23/13
        result = _match_run_against_expected("2026/06/23/13", expected)
        assert result is not None
        assert result["is_self"] is True


# ---------------------------------------------------------------------------
# 6) ResultCondition 端口
# ---------------------------------------------------------------------------


class TestThresholdMet:
    @pytest.mark.parametrize(
        ("threshold", "result", "expected"),
        [
            ("SUCCESS", "SUCCESS", True),
            ("SUCCESS", "UNSTABLE", True),    # Java: SUCCESS → SUCCESS/UNSTABLE 都算
            ("SUCCESS", "FAILURE", False),
            ("UNSTABLE", "UNSTABLE", True),
            ("UNSTABLE", "FAILURE", True),
            ("UNSTABLE", "SUCCESS", False),
            ("FAILED", "FAILURE", True),
            ("FAILED", "SUCCESS", False),
            ("SUCCESS", None, False),
        ],
    )
    def test_threshold(self, threshold: str, result: str | None, expected: bool) -> None:
        assert _threshold_met(threshold, result) is expected


# ---------------------------------------------------------------------------
# 7) BuildTriggerUtils.isTriggerByUser 端口
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, data: bytes, headers: dict[str, str] | None = None) -> None:
        self.data = data
        self.headers = headers or {}

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.data if size < 0 else self.data[:size]


def _fake_urlopen_factory(responses: list[bytes]):
    """按顺序消费 responses 的 urlopen 替身."""
    iterator = iter(responses)

    def fake_urlopen(request, timeout):  # noqa: ANN001, ANN202
        data = next(iterator)
        return _FakeResponse(data)

    return fake_urlopen


def _build_info_payload(causes: list[dict[str, str]], result: str = "SUCCESS") -> dict:
    return {
        "number": 100,
        "result": result,
        "actions": [
            {
                "_class": "hudson.model.CauseAction",
                "causes": causes,
            }
        ],
    }


class TestIsUserTriggeredBuild:
    def test_user_triggered_directly(self) -> None:
        client = JenkinsClient(base_url="https://j.example", username="u", token="t")
        build_payload = _build_info_payload(
            [{"_class": "hudson.model.Cause$UserIdCause", "shortDescription": "Started by user admin"}]
        )
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen_factory([json.dumps(build_payload).encode()])):
            result = is_user_triggered_build(client, job_display_name="A", build_ref=100)
        assert result["success"] is True
        assert result["summary"]["is_triggered_by_user"] is True
        assert result["summary"]["cause_chain"][0]["user_id_found_at_this_level"] is True

    def test_upstream_chain_ends_in_user(self) -> None:
        client = JenkinsClient(base_url="https://j.example", username="u", token="t")
        downstream_payload = _build_info_payload(
            [{"_class": "hudson.model.Cause$UpstreamCause", "shortDescription": 'Started by upstream project "A" build number 50'}]
        )
        upstream_payload = _build_info_payload(
            [{"_class": "hudson.model.Cause$UserIdCause", "shortDescription": "Started by user alice"}]
        )
        with patch(
            "urllib.request.urlopen",
            side_effect=_fake_urlopen_factory(
                [json.dumps(downstream_payload).encode(), json.dumps(upstream_payload).encode()]
            ),
        ):
            result = is_user_triggered_build(client, job_display_name="B", build_ref=200)
        assert result["summary"]["is_triggered_by_user"] is True
        assert len(result["summary"]["cause_chain"]) == 2

    def test_not_user_triggered(self) -> None:
        client = JenkinsClient(base_url="https://j.example", username="u", token="t")
        # 没有任何 CauseAction
        build_payload = {"number": 100, "result": "SUCCESS", "actions": []}
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen_factory([json.dumps(build_payload).encode()])):
            result = is_user_triggered_build(client, job_display_name="A", build_ref=100)
        assert result["summary"]["is_triggered_by_user"] is False


# ---------------------------------------------------------------------------
# 8) JobDependencyBuildTrigger.shouldTriggerBuild 三道闸门 端口
# ---------------------------------------------------------------------------


class TestDiagnoseDependencyTrigger:
    def _build_history_payload(self, builds: list[dict[str, Any]]) -> bytes:
        return json.dumps({"builds": builds}).encode("utf-8")

    def _build_info_payload_with_actions(self, result: str, time_hour: str | None = None, build_number: int = 100) -> bytes:
        actions: list[dict[str, Any]] = []
        if time_hour is not None:
            actions.append(
                {
                    "_class": "hudson.model.ParametersAction",
                    "parameters": [{"name": "time_hour", "value": time_hour}],
                }
            )
        payload = {"number": build_number, "result": result, "actions": actions}
        return json.dumps(payload).encode("utf-8")

    def _queue_payload(self, items: list[dict[str, Any]]) -> bytes:
        return json.dumps({"items": items}).encode("utf-8")

    def test_all_gates_pass(self) -> None:
        """happy path: 上游 SUCCESS + 队列无重复 + condition 匹配 → 三道闸门都过."""
        client = JenkinsClient(base_url="https://j.example", username="u", token="t")
        # 上游 lastBuild SUCCESS,time_hour=2026/06/23/01
        upstream_info = self._build_info_payload_with_actions("SUCCESS", "2026/06/23/01", build_number=100)
        # 下游队列为空
        queue = self._queue_payload([])
        # 上游历史:上一次构建在 2026/06/23/01 SUCCESS (供 condition 匹配)
        history = self._build_history_payload(
            [
                {"number": 99, "result": "SUCCESS", "timestamp": 1, "duration": 1, "url": "x", "building": False},
                {"number": 98, "result": "SUCCESS", "timestamp": 1, "duration": 1, "url": "x", "building": False},
            ]
        )
        # 上游 99 号构建的参数
        upstream_99_info = self._build_info_payload_with_actions("SUCCESS", "2026/06/23/01", build_number=99)
        upstream_98_info = self._build_info_payload_with_actions("SUCCESS", "2026/06/22/23", build_number=98)

        with patch(
            "urllib.request.urlopen",
            side_effect=_fake_urlopen_factory(
                [
                    upstream_info,  # /job/A/lastBuild/api/json
                    queue,           # /queue/api/json
                    history,         # /job/A/api/json?tree=builds
                    upstream_99_info,  # /job/A/99/api/json?tree=actions
                    upstream_98_info,  # /job/A/98/api/json?tree=actions
                ]
            ),
        ):
            result = diagnose_dependency_trigger(
                client,
                downstream_job="B",
                time_hour="2026/06/23/14",
                upstream_job="A",
                trigger_condition="h = 01",
            )
        assert result["success"] is True
        gates = result["summary"]["gates"]
        assert gates[0]["name"] == "result_threshold"
        assert gates[0]["passed"] is True
        assert gates[1]["name"] == "queue_dedup"
        assert gates[1]["passed"] is True
        assert gates[2]["name"] == "upstream_condition_match"
        assert result["summary"]["overall_passed"] is True
        assert result["summary"]["recommendations"] == []

    def test_gate1_failed_upstream_failed(self) -> None:
        client = JenkinsClient(base_url="https://j.example", username="u", token="t")
        upstream_info = self._build_info_payload_with_actions("FAILURE", None, build_number=100)
        queue = self._queue_payload([])
        empty_history = json.dumps({"builds": []}).encode()
        with patch(
            "urllib.request.urlopen",
            side_effect=_fake_urlopen_factory([upstream_info, queue, empty_history]),
        ):
            result = diagnose_dependency_trigger(
                client,
                downstream_job="B",
                time_hour="2026/06/23/14",
                upstream_job="A",
                trigger_condition="",
            )
        assert result["success"] is True
        assert result["summary"]["gates"][0]["passed"] is False
        assert result["summary"]["overall_passed"] is False
        assert "闸门 1 失败" in result["summary"]["recommendations"][0]

    def test_gate2_failed_queue_already_has_same_time_hour(self) -> None:
        client = JenkinsClient(base_url="https://j.example", username="u", token="t")
        upstream_info = self._build_info_payload_with_actions("SUCCESS", None, build_number=100)
        queue = self._queue_payload(
            [
                {
                    "id": 501,
                    "task": {"name": "B", "url": "https://j.example/job/B/"},
                    "blocked": False,
                    "buildable": True,
                    "why": "x",
                    "inQueueSince": 0,
                }
            ]
        )
        queue_item_params = json.dumps(
            {
                "actions": [
                    {
                        "_class": "hudson.model.ParametersAction",
                        "parameters": [{"name": "time_hour", "value": "2026/06/23/14"}],
                    }
                ]
            }
        ).encode()
        empty_history = json.dumps({"builds": []}).encode()
        with patch(
            "urllib.request.urlopen",
            side_effect=_fake_urlopen_factory([upstream_info, queue, queue_item_params, empty_history]),
        ):
            result = diagnose_dependency_trigger(
                client,
                downstream_job="B",
                time_hour="2026/06/23/14",
                upstream_job="A",
                trigger_condition="",
            )
        assert result["success"] is True
        assert result["summary"]["gates"][0]["passed"] is True
        assert result["summary"]["gates"][1]["passed"] is False
        assert "闸门 2 失败" in result["summary"]["recommendations"][0]

    def test_invalid_trigger_condition(self) -> None:
        client = JenkinsClient(base_url="https://j.example", username="u", token="t")
        # 注意: 即便 triggerCondition 解析失败,函数也会先调用 Jenkins
        # (get_build_info + get_queue_items),所以需要 mock 这两步的响应.
        upstream_info = self._build_info_payload_with_actions("SUCCESS", None, build_number=100)
        empty_queue = self._queue_payload([])
        with patch(
            "urllib.request.urlopen",
            side_effect=_fake_urlopen_factory([upstream_info, empty_queue]),
        ):
            result = diagnose_dependency_trigger(
                client,
                downstream_job="B",
                time_hour="2026/06/23/14",
                upstream_job="A",
                trigger_condition="h = 25",  # 越界
            )
        assert result["success"] is False
        assert result["error_type"] == "invalid_input"

    def test_upstream_build_not_found(self) -> None:
        client = JenkinsClient(base_url="https://j.example", username="u", token="t")
        with patch(
            "urllib.request.urlopen",
            side_effect=_fake_urlopen_factory([b""]),
        ):
            result = diagnose_dependency_trigger(
                client,
                downstream_job="B",
                time_hour="2026/06/23/14",
                upstream_job="A",
                trigger_condition="",
            )
        assert result["success"] is False
        assert result["error_type"] == "not_found"


# ---------------------------------------------------------------------------
# 9) check_upstream_time_hour_match 端到端
# ---------------------------------------------------------------------------


class TestCheckUpstreamTimeHourMatch:
    def test_no_condition_lists_all(self) -> None:
        client = JenkinsClient(base_url="https://j.example", username="u", token="t")
        history = json.dumps(
            {
                "builds": [
                    {"number": 100, "result": "SUCCESS", "timestamp": 1, "duration": 1, "url": "x", "building": False},
                    {"number": 99, "result": "FAILURE", "timestamp": 1, "duration": 1, "url": "x", "building": False},
                ]
            }
        ).encode()
        b100 = json.dumps(
            {
                "actions": [
                    {
                        "_class": "hudson.model.ParametersAction",
                        "parameters": [{"name": "time_hour", "value": "2026/06/23/14"}],
                    }
                ]
            }
        ).encode()
        b99 = json.dumps(
            {
                "actions": [
                    {
                        "_class": "hudson.model.ParametersAction",
                        "parameters": [{"name": "time_hour", "value": "2026/06/22/13"}],
                    }
                ]
            }
        ).encode()
        with patch(
            "urllib.request.urlopen",
            side_effect=_fake_urlopen_factory([history, b100, b99]),
        ):
            result = check_upstream_time_hour_match(
                client,
                upstream_job="A",
                time_hour="2026/06/23/14",
                trigger_condition="",
            )
        # 没有 trigger_condition 时,expected_list 为空,所以不会做匹配
        assert result["success"] is True
        assert result["summary"]["match_count"] == 0
        assert len(result["summary"]["evaluated_builds"]) == 2

    def test_with_condition(self) -> None:
        client = JenkinsClient(base_url="https://j.example", username="u", token="t")
        history = json.dumps(
            {"builds": [{"number": 100, "result": "SUCCESS", "timestamp": 1, "duration": 1, "url": "x", "building": False}]}
        ).encode()
        b100 = json.dumps(
            {
                "actions": [
                    {
                        "_class": "hudson.model.ParametersAction",
                        "parameters": [{"name": "time_hour", "value": "2026/06/23/01"}],
                    }
                ]
            }
        ).encode()
        with patch(
            "urllib.request.urlopen",
            side_effect=_fake_urlopen_factory([history, b100]),
        ):
            result = check_upstream_time_hour_match(
                client,
                upstream_job="A",
                time_hour="2026/06/23/14",
                trigger_condition="h = 01",
            )
        assert result["success"] is True
        assert result["summary"]["match_count"] == 1
        assert result["summary"]["evaluated_builds"][0]["matched_expected"] == "01"

    def test_invalid_condition_in_tool(self) -> None:
        client = JenkinsClient(base_url="https://j.example", username="u", token="t")
        # trigger_condition 解析失败时,函数会在调用 Jenkins 之前 return,
        # 所以 urlopen 不应被调用,但为了保险起见给一个空 history 响应.
        empty_history = json.dumps({"builds": []}).encode()
        with patch(
            "urllib.request.urlopen",
            side_effect=_fake_urlopen_factory([empty_history]),
        ):
            result = check_upstream_time_hour_match(
                client,
                upstream_job="A",
                time_hour="2026/06/23/14",
                trigger_condition="h = 99",
            )
        assert result["success"] is False
        assert result["error_type"] == "invalid_input"
