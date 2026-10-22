"""K 线下载器：分块回填全量历史 + 增量刷新，只落库已收盘 bar。

用法见 scripts/download_bars.py；存储约定见 store.py。
"""
from __future__ import annotations

import pandas as pd

from mt5gw.errors import DataUnavailableError
from mt5gw.market import MarketData
from mt5data.store import drop_unclosed, load_bars, merge_bars, save_bars

ALL_TIMEFRAMES = ["M1", "M2", "M3", "M4", "M5", "M6", "M10", "M12", "M15",
                  "M20", "M30", "H1", "H2", "H3", "H4", "H6", "H8", "H12",
                  "D1", "W1", "MN1"]


def _to_df(bars) -> pd.DataFrame:
    return pd.DataFrame([{
        "time": b.time, "open": b.open, "high": b.high, "low": b.low,
        "close": b.close, "tick_volume": b.tick_volume,
        "spread": b.spread, "real_volume": b.real_volume,
    } for b in bars])


def _now(market: MarketData, symbol: str):
    """服务器'现在' = 最新 tick 时间（与 K 线同一时钟，无时区偏移）。"""
    return market.quote(symbol).time


def backfill_series(root, market: MarketData, symbol: str, timeframe: str,
                    chunk: int = 50_000, max_chunks: int = 2000) -> dict:
    """从最新往回分块取尽历史，合并去重、剔除未收盘 bar 后落库。"""
    now = _now(market, symbol)
    rows: list = []
    seen: set = set()
    pos = 0
    chunks = 0
    while chunks < max_chunks:
        try:
            batch = market.bars(symbol, timeframe, chunk, end_pos=pos)
        except DataUnavailableError:
            break
        chunks += 1
        if not batch:
            break
        new_times = {b.time for b in batch}
        rows.extend(batch)
        progress = bool(new_times - seen)
        seen |= new_times
        pos += len(batch)
        if len(batch) < chunk or not progress:
            break  # 取尽，或只剩重叠（服务器分页上限等情形）
    merged = merge_bars(None, _to_df(rows)) if rows else None
    closed = drop_unclosed(merged, timeframe, now) if merged is not None else None
    if closed is None or len(closed) == 0:
        return {"symbol": symbol, "timeframe": timeframe, "rows": 0,
                "chunks_fetched": chunks, "first_time": None, "last_time": None}
    save_bars(root, symbol, timeframe, closed)
    return {
        "symbol": symbol, "timeframe": timeframe,
        "rows": int(len(closed)), "chunks_fetched": chunks,
        "first_time": closed["time"].iloc[0].isoformat(),
        "last_time": closed["time"].iloc[-1].isoformat(),
    }


def refresh_series(root, market: MarketData, symbol: str, timeframe: str,
                   lookback: int = 5000) -> dict:
    """增量刷新：取最近 lookback 根合并进已有文件（自愈重刷重叠段）。"""
    now = _now(market, symbol)
    old = load_bars(root, symbol, timeframe)
    old_rows = 0 if old is None else int(len(old))
    try:
        batch = market.bars(symbol, timeframe, lookback)
    except DataUnavailableError:
        batch = []
    merged = merge_bars(old, _to_df(batch) if batch else None)
    closed = drop_unclosed(merged, timeframe, now)
    if closed is None or len(closed) == 0:
        return {"symbol": symbol, "timeframe": timeframe, "rows": old_rows,
                "new_bars": 0, "first_time": None, "last_time": None}
    save_bars(root, symbol, timeframe, closed)
    return {
        "symbol": symbol, "timeframe": timeframe,
        "rows": int(len(closed)),
        "new_bars": max(0, int(len(closed)) - old_rows),
        "first_time": closed["time"].iloc[0].isoformat(),
        "last_time": closed["time"].iloc[-1].isoformat(),
    }
