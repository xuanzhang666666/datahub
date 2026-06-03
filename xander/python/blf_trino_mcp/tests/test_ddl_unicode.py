from __future__ import annotations

from blf_trino_mcp.ddl_unicode import maybe_decode_show_create_ddl


def test_maybe_decode_show_create_ddl_decodes_comment_u_amp() -> None:
    ddl = "CREATE TABLE x (id bigint COMMENT U&'\\8BA2\\5355')"

    assert maybe_decode_show_create_ddl(ddl) == "CREATE TABLE x (id bigint COMMENT '订单')"
