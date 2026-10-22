"""MCP server 组装：工具注册到 MCPServer，stdio/HTTP 双传输。

stdio 给本机 agent；HTTP 配 Bearer 令牌给远程服务器（见 config.py）。
工具本体在 tools.py（纯函数，可独立测试），这里只做闭包包装。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from mt5mcp import tools as t
from mt5mcp.auth import bearer_auth_middleware


def _parse_dt(value: str | None) -> datetime | None:
    if value is None or value == "":
        return None
    return datetime.fromisoformat(value)


def build_server(data_root: str | Path) -> MCPServer:
    root = str(data_root)
    server = MCPServer("mt5-kline-db")

    @server.tool()
    def list_series() -> list[dict]:
        """列出 K 线库里所有 品种×周期 序列：行数、起止时间、最近更新时间。先调它了解库里有什么。"""
        return t.list_series(root)

    @server.tool()
    def get_bars(symbol: str, timeframe: str, limit: int = 100,
                 since: str = "", until: str = "") -> dict:
        """取某个品种某周期的 K 线（最多 500 根，返回最新一段）。
        symbol 如 "XAUUSD"；timeframe 如 "M1"/"M15"/"H1"/"D1"；
        since/until 为可选 ISO 时间窗（如 "2026-10-01T00:00:00"）。"""
        return t.get_bars(root, symbol, timeframe, limit=limit,
                          since=_parse_dt(since), until=_parse_dt(until))

    @server.tool()
    def describe_series(symbol: str, timeframe: str) -> dict:
        """序列概况：行数、时间范围、应有步长与缺失根数（周末休市导致的缺失属正常）。"""
        return t.describe_series(root, symbol, timeframe)

    @server.tool()
    def compute_indicator(symbol: str, timeframe: str, indicator: str,
                          limit: int = 50, period: int = 0,
                          fast: int = 0, slow: int = 0) -> dict:
        """服务端现算技术指标并返回最后 limit 个点（不落库，参数随意改）。
        indicator 可选 "sma"/"ema"/"rsi"/"atr"；period 为周期参数（如 RSI 默认 14）。"""
        params: dict = {}
        if period:
            params["period"] = period
        if fast:
            params["fast"] = fast
        if slow:
            params["slow"] = slow
        return t.compute_indicator(root, symbol, timeframe, indicator,
                                   params, limit=limit)

    return server


def run_stdio(data_root: str | Path) -> None:
    build_server(data_root).run(transport="stdio")


def run_http(cfg, data_root: str | Path) -> None:
    import uvicorn

    app = build_server(data_root).streamable_http_app()
    guarded = bearer_auth_middleware(app, cfg.token)
    uvicorn.run(guarded, host=cfg.host, port=cfg.port, log_level="info")
