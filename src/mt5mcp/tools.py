"""MCP 数据工具：有界查询 + 服务端指标现算。

安全与资源（I12/I14 修复后）：
- symbol 白名单正则校验——杜绝路径穿越与 UNC 注入（SMB 凭据泄露）；
- 读取一律经 DuckDB 下推过滤 + LIMIT，绝不整文件载入（XAUUSD M1 91MB）；
- 指标只读"limit + 预热期"所需的尾部数据。
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

from mt5mcp import indicators
from mt5data.store import TIMEFRAME_SECONDS, read_watermark, series_dir

MAX_BARS_LIMIT = 500
MAX_INDICATOR_POINTS = 500

# 品种名白名单：MT5 品种只含字母数字与 . _ # + -（如 XAUUSD.m、BTCUSD#)
# 用 fullmatch（$ 可被尾随换行绕过）
_SYMBOL_RE = re.compile(r"[A-Za-z0-9._#+-]{1,32}")

MAX_PERIOD = 500  # m4：period 上限，防超大 period 让 LIMIT 覆盖全表


def _capped_period(value: int, name: str) -> int:
    v = int(value)
    if v < 1 or v > MAX_PERIOD:
        raise ValueError(f"{name} 必须在 1..{MAX_PERIOD} 之间，收到 {value}")
    return v


def _warmup_for(indicator: str, params: dict) -> int:
    """预热根数：保证首个返回点的种子残余 <1e-4。

    Wilder 类（rsi/atr）残余按 (1-1/n)^k 衰减，需约 10.5n；
    EMA 类 5n 即 e^-10 量级；macd 由 slow 与 signal 中较大者决定。
    """
    n = int(params.get("period", 0) or 0)
    if indicator in ("rsi", "atr"):
        n = n or (14 if indicator == "rsi" else 14)
        return max(260, int(10.5 * n) + n)
    if indicator == "macd":
        slow = int(params.get("slow", 0) or 26)
        signal = int(params.get("signal", 0) or 9)
        return max(260, 5 * max(slow, signal) + 50)
    n = n or 20
    return max(260, 5 * n + 50)


def _validate_symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol):
        raise ValueError(f"非法品种名 {symbol!r}（只接受 MT5 品名字符集）")
    if symbol.strip("._") == "":  # 拒绝 "."、".." 等纯点号路径组件
        raise ValueError(f"非法品种名 {symbol!r}")
    return symbol.upper()


def _parquet_path(root, symbol: str, timeframe: str) -> Path:
    tf = timeframe.upper()
    if tf not in TIMEFRAME_SECONDS:
        raise ValueError(f"未知周期 {timeframe}，可用：{sorted(TIMEFRAME_SECONDS)}")
    p = Path(series_dir(root, _validate_symbol(symbol), tf)) / "data.parquet"
    if not p.exists():
        raise ValueError(f"库中没有 {symbol.upper()} {tf}（先运行 download_bars.py）")
    return p


def _sql_literal(s: str) -> str:
    return s.replace("'", "''")


def _iso(ts) -> str:
    return pd.Timestamp(ts).isoformat()


def list_series(root) -> list[dict]:
    """库里有哪些品种×周期，各多少行、时间范围。"""
    bars_dir = Path(root) / "bars"
    if not bars_dir.exists():
        # 不回显服务器绝对路径
        raise ValueError("数据目录不存在：请检查服务的 --data-dir 与启动目录")
    out = []
    for wm_file in sorted(bars_dir.glob("*/*/meta.json")):
        wm = read_watermark(root, wm_file.parent.parent.name, wm_file.parent.name)
        if wm:
            out.append({
                "symbol": wm["symbol"], "timeframe": wm["timeframe"],
                "rows": wm["rows"],
                "first_time": wm["first_time"], "last_time": wm["last_closed_time"],
                "updated_at_utc": wm.get("updated_at_utc"),
                "server": wm.get("server"), "login": wm.get("login"),
            })
    return out


def get_bars(root, symbol: str, timeframe: str, *, limit: int = 100,
             since: datetime | None = None, until: datetime | None = None) -> dict:
    """取 K 线（最多 MAX_BARS_LIMIT 根，最新一段；since/until 圈窗口）。"""
    limit = max(1, min(int(limit), MAX_BARS_LIMIT))
    path = _parquet_path(root, symbol, timeframe)
    con = duckdb.connect()
    try:
        cond = []
        if since is not None:
            cond.append(f"time >= TIMESTAMP '{since.isoformat()}'")
        if until is not None:
            cond.append(f"time <= TIMESTAMP '{until.isoformat()}'")
        where = ("WHERE " + " AND ".join(cond)) if cond else ""
        table = f"read_parquet('{_sql_literal(str(path))}')"
        total = con.execute(f"SELECT count(*) FROM {table} {where}").fetchone()[0]
        df = con.execute(
            f"SELECT * FROM {table} {where} ORDER BY time DESC LIMIT {limit}"
        ).fetchdf()
    finally:
        con.close()
    df = df.sort_values("time").reset_index(drop=True)
    return {
        "symbol": symbol.upper(), "timeframe": timeframe.upper(),
        "rows_total_before_limit": int(total),
        "truncated": bool(total > limit),
        "rows_returned": int(len(df)),
        "bars": [
            {"time": _iso(b["time"]),
             "open": float(b["open"]), "high": float(b["high"]),
             "low": float(b["low"]), "close": float(b["close"]),
             "tick_volume": int(b["tick_volume"]), "spread": int(b["spread"]),
             "real_volume": int(b["real_volume"])}
            for b in df.to_dict("records")
        ],
    }


def describe_series(root, symbol: str, timeframe: str) -> dict:
    """序列概况：行数、范围、步长、最大相邻间隔（周末休市导致的大间隔属正常）。"""
    path = _parquet_path(root, symbol, timeframe)
    con = duckdb.connect()
    try:
        table = f"read_parquet('{_sql_literal(str(path))}')"
        row = con.execute(f"""
            SELECT count(*) AS n, min(time) AS f, max(time) AS l, max(gap) AS max_gap
            FROM (SELECT time, datediff('second', lag(time) OVER (ORDER BY time), time) AS gap
                  FROM {table})
        """).fetchone()
    finally:
        con.close()
    n, first, last, max_gap = row
    tf = timeframe.upper()
    return {
        "symbol": symbol.upper(), "timeframe": tf,
        "rows": int(n),
        "first_time": _iso(first),
        "last_time": _iso(last),
        "expected_step_minutes": TIMEFRAME_SECONDS[tf] / 60,
        "max_gap_seconds": int(max_gap) if max_gap is not None else 0,
        "note": "max_gap 大多为周末/假日休市；远超两倍步长且不是周末时段才值得排查",
    }


def _indicator_columns(name: str, df: pd.DataFrame, params: dict) -> dict[str, pd.Series]:
    p = params
    if name == "sma":
        return {"value": indicators.sma(df["close"], p.get("period", 20))}
    if name == "ema":
        return {"value": indicators.ema(df["close"], p.get("period", 20))}
    if name == "rsi":
        return {"value": indicators.rsi(df["close"], p.get("period", 14))}
    if name == "atr":
        return {"value": indicators.atr(df["high"], df["low"], df["close"], p.get("period", 14))}
    if name == "macd":
        line, sig, hist = indicators.macd(
            df["close"], p.get("fast", 12), p.get("slow", 26), p.get("signal", 9))
        return {"macd": line, "signal": sig, "hist": hist}
    if name == "bbands":
        mid, upper, lower = indicators.bbands(
            df["close"], p.get("period", 20), p.get("std_mult", 2.0))
        return {"mid": mid, "upper": upper, "lower": lower}
    raise ValueError(f"未知指标 {name}，可用：sma, ema, rsi, atr, macd, bbands")


def compute_indicator(root, symbol: str, timeframe: str, indicator: str,
                      params: dict | None = None, limit: int = 50) -> dict:
    """服务端现算指标，只回最后 limit 个点（只读尾部 limit+预热期，不碰全量）。"""
    limit = max(1, min(int(limit), MAX_INDICATOR_POINTS))
    params = params or {}
    for key in ("period", "fast", "slow", "signal"):
        if key in params and params[key]:
            params[key] = _capped_period(params[key], key)
    warmup = _warmup_for(indicator, params)
    path = _parquet_path(root, symbol, timeframe)
    con = duckdb.connect()
    try:
        df = con.execute(
            f"SELECT * FROM read_parquet('{_sql_literal(str(path))}') "
            f"ORDER BY time DESC LIMIT {limit + warmup}"
        ).fetchdf()
    finally:
        con.close()
    df = df.sort_values("time").reset_index(drop=True)
    cols = _indicator_columns(indicator, df, params)
    frame = pd.DataFrame({"time": df["time"], **cols})
    tail = frame.dropna().tail(limit)
    values = [
        {"time": _iso(r["time"]),
         **{k: round(float(r[k]), 8) for k in cols}}
        for _, r in tail.iterrows()
    ]
    return {
        "symbol": symbol.upper(), "timeframe": timeframe.upper(),
        "indicator": indicator, "params": params,
        "values": values,
    }
