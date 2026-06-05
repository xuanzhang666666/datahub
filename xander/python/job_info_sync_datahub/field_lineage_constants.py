"""Helpers for target fields derived from constants rather than source fields."""

from __future__ import annotations

import re

_NULL_LITERAL_RE = re.compile(r"^(?:null|cast\s*\(\s*null\s+as\s+[\w<>,\s]+\))$", re.I)
_STRING_LITERAL_RE = re.compile(r"^'(?:[^']|'')*'$")
_NUMBER_LITERAL_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
_BACKTICK_TEXT_RE = re.compile(r"`([^`]+)`")
_RUNTIME_CONSTANT_RE = re.compile(
    r"\b(?:current_date|current_timestamp|current_time|date_format|unix_timestamp|from_unixtime)\s*\(",
    re.I,
)


def is_constant_transform_expression(expression: str) -> bool:
    """Return True for expressions that need no upstream source field."""
    cleaned = (expression or "").strip().rstrip(";").strip()
    if not cleaned:
        return False
    lowered = cleaned.lower()
    if _NULL_LITERAL_RE.match(cleaned):
        return True
    if lowered in {"true", "false"}:
        return True
    if _STRING_LITERAL_RE.match(cleaned):
        return True
    if _NUMBER_LITERAL_RE.match(cleaned):
        return True
    return _RUNTIME_CONSTANT_RE.search(cleaned) is not None


def constant_expression_from_select_item(select_item: str, target_field: str) -> str:
    """Extract a constant expression from a SELECT item like ``NULL AS vendor_code``."""
    cleaned = (select_item or "").strip().rstrip(";").strip()
    field = (target_field or "").strip().strip("`").lower()
    if not cleaned or not field:
        return ""
    alias_re = re.compile(
        rf"^(?P<expr>.+?)\s+(?:as\s+)?`?{re.escape(field)}`?$",
        re.I,
    )
    match = alias_re.match(cleaned)
    expression = match.group("expr").strip() if match else cleaned
    if is_constant_transform_expression(expression):
        return expression.upper() if expression.lower() == "null" else expression
    return ""


def constant_expression_from_reason(reason: str, target_field: str) -> tuple[str, str]:
    """Return ``(expression, evidence_sql)`` when unresolved text describes a constant."""
    for item in _BACKTICK_TEXT_RE.findall(reason or ""):
        expression = constant_expression_from_select_item(item, target_field)
        if expression:
            return expression, item.strip()
    return "", ""
