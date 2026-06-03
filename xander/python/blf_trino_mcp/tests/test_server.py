from __future__ import annotations

from blf_trino_mcp.server import BlfTrinoMcpApplication
from blf_trino_mcp.trino_client import TrinoClient, TrinoConfig


def test_authorized_requires_bearer_token() -> None:
    app = BlfTrinoMcpApplication(
        trino_client=TrinoClient(TrinoConfig()),
        mcp_token="secret",
    )

    assert app.authorized("Bearer secret")
    assert not app.authorized("Bearer wrong")
    assert not app.authorized(None)


def test_tools_list_contains_trino_tools_with_chinese_descriptions() -> None:
    app = BlfTrinoMcpApplication(
        trino_client=TrinoClient(TrinoConfig()),
        mcp_token=None,
    )

    response = app.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    tools = response["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert "blf_trino_get_hive_table_ddl" in names
    assert "blf_trino_get_hive_table_partitions" in names
    assert "blf_trino_query_hive_sql" in names
    assert "blf_trino_query_hive_sql_fragment" in names
    assert "blf_trino_query_hive_by_natural_language" in names
    ddl_tool = next(tool for tool in tools if tool["name"] == "blf_trino_get_hive_table_ddl")
    assert "通过 Trino 查询 Hive 表 DDL" in ddl_tool["description"]
    assert "Hive 表名" in ddl_tool["inputSchema"]["properties"]["table"]["description"]
