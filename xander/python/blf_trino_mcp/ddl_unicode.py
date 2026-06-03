"""Decode Trino SHOW CREATE TABLE COMMENT U&'\\XXXX...' output."""

from __future__ import annotations

import re

_U_ESCAPE = re.compile(r"\\([0-9A-Fa-f]{4})")
_COMMENT_U_AMP = re.compile(
    r"COMMENT\s+U&'((?:[^'\\]|\\.)*?)'",
    re.IGNORECASE | re.DOTALL,
)


def decode_trino_u_string(body: str) -> str:
    """Replace \\XXXX sequences inside a U&'...' body with Unicode characters."""

    def repl(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 16))

    return _U_ESCAPE.sub(repl, body)


def translate_ddl_unicode_comments(ddl: str) -> str:
    """Replace each COMMENT U&'...' with COMMENT 'decoded...'."""

    def sub_comment(match: re.Match[str]) -> str:
        decoded = decode_trino_u_string(match.group(1))
        return "COMMENT '" + decoded.replace("'", "''") + "'"

    return _COMMENT_U_AMP.sub(sub_comment, ddl)


def maybe_decode_show_create_ddl(text: str) -> str:
    """If Trino DDL uses COMMENT U&'...', rewrite to readable Chinese comments."""
    if "COMMENT U&'" not in text and "comment u&'" not in text:
        return text
    return translate_ddl_unicode_comments(text)
