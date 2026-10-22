import asyncio

from mt5mcp.server import build_server


def test_server_registers_all_tools(tmp_path):
    server = build_server(tmp_path)
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert names >= {"list_series", "get_bars", "describe_series", "compute_indicator"}
    # 工具描述随注册携带，agent 靠它理解用途
    assert all(t.description for t in tools)
