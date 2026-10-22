"""MCP 数据工具：有界查询 + 服务端指标现算。

工具都返回"进 agent 上下文也安全"的小结果——上限 MAX_BARS_LIMIT，
杜绝把全量历史灌进模型上下文。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from mt5mcp import indicators
from mt5data.store import TIMEFRAME_SECONDS, read_watermark, series_dir

MAX_BARS_LIMIT = 500
MAX_INDICATOR_POINTS = 500

_BAR_COLUMNS = ["time", "open", "high", "low", "close",
                "tick_volume", "spread", "real_volume"]


def _load(root, symbol: str, timeframe: str) -> pd.DataFrame:
    tf = timeframe.upper()
    if tf not in TIMEFRAME_SECONDS:
        raise ValueError(f"未知周期 {timeframe}，可用：{sorted(TIMEFRAME_SECONDS)}")
    p = Path(series_dir(root, symbol, tf)) / "data.parquet"
    if not p.exists():
        raise ValueError(f"库中没有 {symbol.upper()} {tf}（先运行 download_bars.py）")
    return pd.read_parquet(p)


def _iso(ts) -> str:
    return pd.Timestamp(ts).isoformat()


def list_series(root) -> list[dict]:
    """库里有哪些品种×周期，各多少行、时间范围。"""
    root = Path(root)
    out = []
    bars_dir = root / "bars"
    if not bars_dir.exists():
        return out
    for wm_file in sorted(bars_dir.glob("*/*/meta.json")):
        wm = read_watermark(root, wm_file.parent.parent.name, wm_file.parent.name)
        if wm:
            out.append({
                "symbol": wm["symbol"], "timeframe": wm["timeframe"],
                "rows": wm["rows"],
                "first_time": wm["first_time"], "last_time": wm["last_closed_time"],
                "updated_at": wm["updated_at"],
            })
    return out


def get_bars(root, symbol: str, timeframe: str, *, limit: int = 100,
             since: datetime | None = None, until: datetime | None = None) -> dict:
    """取 K 线（最多 MAX_BARS_LIMIT 根，取最新一段；可用 since/until 圈窗口）。"""
    limit = max(1, min(int(limit), MAX_BARS_LIMIT))
    df = _load(root, symbol, timeframe)
    if since is not None:
        df = df[df["time"] >= pd.Timestamp(since)]
    if until is not None:
        df = df[df["time"] <= pd.Timestamp(until)]
    total = int(len(df))
    truncated = total > limit
    df = df.tail(limit)
    return {
        "symbol": symbol.upper(), "timeframe": timeframe.upper(),
        "rows_total_before_limit": total,
        "truncated": truncated,
        "rows_returned": int(len(df)),
        "bars": [
            {**{c: (float(b[c]) if c not in ("tick_volume", "spread", "real_volume")
                  else int(b[c])) for c in _BAR_COLUMNS if c != "time"},
             "time": _iso(b["time"])}
            for b in df.to_dict("records")
        ],
    }


def describe_series(root, symbol: str, timeframe: str) -> dict:
    """序列概况：行数、范围、应有步长、缺失根数（周末休市会显著放大，属正常）。"""
    df = _load(root, symbol, timeframe)
    tf = timeframe.upper()
    step = pd.Timedelta(seconds=TIMEFRAME_SECONDS[tf])
    span = df["time"].iloc[-1] - df["time"].iloc[0]
    expected = int(span / step) + 1
    return {
        "symbol": symbol.upper(), "timeframe": tf,
        "rows": int(len(df)),
        "first_time": _iso(df["time"].iloc[0]),
        "last_time": _iso(df["time"].iloc[-1]),
        "expected_step_minutes": step.total_seconds() / 60,
        "expected_bars_if_no_gaps": expected,
        "missing_bars": max(0, expected - len(df)),
    }


_INDICATORS = {
    "sma": lambda df, p: indicators.sma(df["close"], p.get("period", 20)),
    "ema": lambda df, p: indicators.ema(df["close"], p.get("period", 20)),
    "rsi": lambda df, p: indicators.rsi(df["close"], p.get("period", 14)),
    "atr": lambda df, p: indicators.atr(
        df["high"], df["low"], df["close"], p.get("period", 14)),
}


def compute_indicator(root, symbol: str, timeframe: str, indicator: str,
                      params: dict | None = None, limit: int = 50) -> dict:
    """服务端现算指标，只回最后 limit 个点（指标不落库，参数随意改）。"""
    if indicator not in _INDICATORS:
        raise ValueError(f"未知指标 {indicator}，可用：{sorted(_INDICATORS)}")
    limit = max(1, min(int(limit), MAX_INDICATOR_POINTS))
    params = params or {}
    df = _load(root, symbol, timeframe)
    values = _INDICATORS[indicator](df, params)
    tail = pd.DataFrame({"time": df["time"], "value": values}).dropna().tail(limit)
    return {
        "symbol": symbol.upper(), "timeframe": timeframe.upper(),
        "indicator": indicator, "params": params,
        "values": [{"time": _iso(r["time"]), "value": round(float(r["value"]), 8)}
                   for _, r in tail.iterrows()],
    }
