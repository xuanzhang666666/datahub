from __future__ import annotations

from blf_datahub_mcp.datahub_client import DataHubClient
from blf_datahub_mcp.server import BlfMcpApplication


def test_authorized_requires_bearer_token() -> None:
    app = BlfMcpApplication(
        datahub_client=DataHubClient("http://gms"),
        public_base_url="http://ui",
        mcp_token="secret",
    )

    assert app.authorized("Bearer secret")
    assert not app.authorized("Bearer wrong")
    assert not app.authorized(None)


def test_tools_list_contains_blf_tools() -> None:
    app = BlfMcpApplication(
        datahub_client=DataHubClient("http://gms"),
        public_base_url="http://ui",
        mcp_token=None,
    )

    response = app.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    names = {tool["name"] for tool in response["result"]["tools"]}
    assert "blf_get_hive_table_profile" in names
    assert "blf_get_hive_etl_context" in names
    assert "blf_get_hive_structured_properties" in names
    assert "blf_get_hive_structured_property" in names
    assert "blf_get_hive_etl_script" in names
    assert "blf_get_hive_execute_shell" in names
    assert "blf_get_hive_schedule_url" in names
    assert "blf_get_hive_data_availability_flag" in names
    assert "blf_get_hive_other_remark" in names
    assert "blf_get_hive_lineage" in names
    assert "blf_search_hive_assets" in names
    assert "blf_audit_hive_table" in names
    assert "blf_explain_hive_table_context" in names
