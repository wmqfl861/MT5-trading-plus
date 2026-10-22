"""MCP server 组装：工具注册到 MCPServer，stdio/HTTP 双传输。

stdio 给本机 agent；HTTP 配 Bearer 令牌给远程服务器（见 config.py）。
工具本体在 tools.py（纯函数，可独立测试），这里只做闭包包装。
"""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from mt5mcp import tools as t
from mt5mcp.auth import bearer_auth_middleware

# I14：单进程并发计算上限，防止并发大请求压垮同机运行的 MT5 终端
_SEMAPHORE = threading.Semaphore(2)


def _parse_dt(value: str | None) -> datetime | None:
    if value is None or value == "":
        return None
    dt = datetime.fromisoformat(value)  # 3.11+ 可解析 "Z"，得到 tz-aware
    if dt.tzinfo is not None:
        raise ValueError(
            "时间参数请用不带时区的服务器时间（naive），例如 2026-10-01T00:00:00；"
            "数据列本身是 naive 的，混用会比较失败"
        )
    return dt


def build_server(data_root: str | Path) -> MCPServer:
    root = str(data_root)
    server = MCPServer("mt5-kline-db")

    @server.tool()
    def list_series() -> list[dict]:
        """列出 K 线库里所有 品种×周期 序列：行数、起止时间、最近更新时间。先调它了解库里有什么。"""
        with _SEMAPHORE:
            return t.list_series(root)

    @server.tool()
    def get_bars(symbol: str, timeframe: str, limit: int = 100,
                 since: str = "", until: str = "") -> dict:
        """取某个品种某周期的 K 线（最多 500 根，返回最新一段）。
        symbol 如 "XAUUSD"；timeframe 如 "M1"/"M15"/"H1"/"D1"；
        since/until 为可选 ISO 时间窗（如 "2026-10-01T00:00:00"，不带时区）。"""
        with _SEMAPHORE:
            return t.get_bars(root, symbol, timeframe, limit=limit,
                              since=_parse_dt(since), until=_parse_dt(until))

    @server.tool()
    def describe_series(symbol: str, timeframe: str) -> dict:
        """序列概况：行数、时间范围、应有步长与最大相邻间隔（周末休市的大间隔属正常）。"""
        with _SEMAPHORE:
            return t.describe_series(root, symbol, timeframe)

    @server.tool()
    def compute_indicator(symbol: str, timeframe: str, indicator: str,
                          limit: int = 50, period: int = 0, fast: int = 0,
                          slow: int = 0, signal: int = 0, std_mult: float = 0) -> dict:
        """服务端现算技术指标并返回最后 limit 个点（不落库，参数随意改）。
        indicator 可选 "sma"/"ema"/"rsi"/"atr"/"macd"/"bbands"；
        period 用于 sma/ema/rsi/atr/bbands（如 RSI 默认 14），
        fast/slow/signal 用于 macd（默认 12/26/9），std_mult 用于 bbands（默认 2）。
        口径：RSI/ATR 为 Wilder 平滑（种子=前 period 均值）；Bollinger 用总体
        标准差；MACD 信号线为 EMA（注意 MT5 内置 MACD 信号线用 SMA）。"""
        params: dict = {}
        if period:
            params["period"] = period
        if fast:
            params["fast"] = fast
        if slow:
            params["slow"] = slow
        if signal:
            params["signal"] = signal
        if std_mult:
            params["std_mult"] = std_mult
        with _SEMAPHORE:
            return t.compute_indicator(root, symbol, timeframe, indicator,
                                       params, limit=limit)

    return server


def run_stdio(data_root: str | Path) -> None:
    build_server(data_root).run(transport="stdio")


def run_http(cfg, data_root: str | Path) -> None:
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    app = build_server(data_root).streamable_http_app(
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=cfg.dns_rebinding_protection,
            allowed_hosts=list(cfg.allowed_hosts),
        ),
    )
    guarded = bearer_auth_middleware(app, cfg.token)
    uvicorn.run(guarded, host=cfg.host, port=cfg.port, log_level="info")
