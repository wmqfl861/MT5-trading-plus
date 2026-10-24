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
from mt5data import lockbox
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
    return symbol  # 保留原始大小写（R13）：目录名是事实来源，Windows 不分大小写能读、Linux 会按原名找


def _parquet_path(root, symbol: str, timeframe: str) -> Path:
    tf = timeframe.upper()
    if tf not in TIMEFRAME_SECONDS:
        raise ValueError(f"未知周期 {timeframe}，可用：{sorted(TIMEFRAME_SECONDS)}")
    p = Path(series_dir(root, _validate_symbol(symbol), tf)) / "data.parquet"
    if not p.exists():
        raise ValueError(f"库中没有 {symbol} {tf}（先运行 download_bars.py）")
    return p


def _sql_literal(s: str) -> str:
    return s.replace("'", "''")


def _iso(ts) -> str:
    return pd.Timestamp(ts).isoformat()


def list_series(root) -> list[dict]:
    """库里有哪些品种×周期，各多少行、时间范围。

    锁箱约束（R10）：序列配置了 cutoff（v1 序列键或 v2 全局）时，
    rows 与 last_time 只统计开放段（time < cutoff）——锁箱段的行数与
    末时刻本身就是元数据泄露面；first_time 属开放段前缀，不受影响。
    无 cutoff 的序列行为与引入钳制前完全一致。
    """
    bars_dir = Path(root) / "bars"
    if not bars_dir.exists():
        # 不回显服务器绝对路径
        raise ValueError("数据目录不存在：请检查服务的 --data-dir 与启动目录")
    out = []
    for wm_file in sorted(bars_dir.glob("*/*/meta.json")):
        wm = read_watermark(root, wm_file.parent.parent.name, wm_file.parent.name)
        if not wm:
            continue
        entry = {
            "symbol": wm["symbol"], "timeframe": wm["timeframe"],
            "rows": wm["rows"],
            "first_time": wm["first_time"], "last_time": wm["last_closed_time"],
            "updated_at_utc": wm.get("updated_at_utc"),
            "server": wm.get("server"), "login": wm.get("login"),
        }
        cutoff = _lockbox_cutoff(root, wm["symbol"], wm["timeframe"])
        parquet = wm_file.parent / "data.parquet"
        if cutoff is not None and parquet.exists():
            con = duckdb.connect()
            try:
                table = f"read_parquet('{_sql_literal(str(parquet))}')"
                n, last = con.execute(
                    f"SELECT count(*), max(time) FROM {table} "
                    f"WHERE time < TIMESTAMP '{cutoff.isoformat()}'").fetchone()
            finally:
                con.close()
            entry["rows"] = int(n)
            entry["last_time"] = _iso(last) if last is not None else None
            entry["lockbox_cutoff"] = cutoff.isoformat()
        out.append(entry)
    return out


def _lockbox_cutoff(root, symbol: str, tf: str) -> datetime | None:
    """该序列的锁箱 cutoff（data/lockbox.json，键 SYMBOL/TF）；无配置 → None。"""
    lb = lockbox.load_config(root)
    return lb.get(lockbox.series_key(symbol, tf)) if lb else None


def get_bars(root, symbol: str, timeframe: str, *, limit: int = 100,
             since: datetime | None = None, until: datetime | None = None) -> dict:
    """取 K 线（最多 MAX_BARS_LIMIT 根，最新一段；since/until 圈窗口）。

    锁箱约束（远程研究 agent 与本地 research_bars 同一约束）：
    该序列配置了 cutoff 时——since/until 越过 cutoff → 报错（绝不静默截断）；
    未传 until → 钳到 cutoff（time < cutoff），响应带 lockbox_cutoff 字段。
    无配置 → 行为与引入锁箱前完全一致。
    """
    limit = max(1, min(int(limit), MAX_BARS_LIMIT))
    path = _parquet_path(root, symbol, timeframe)
    tf = timeframe.upper()
    cutoff = _lockbox_cutoff(root, symbol, tf)
    con = duckdb.connect()
    try:
        cond = []
        clamped = False
        if cutoff is not None:
            if since is not None and since >= cutoff:
                raise ValueError(
                    f"锁箱：{symbol} {tf} 研究数据截止（cutoff）为 "
                    f"{cutoff.isoformat()}，since={since.isoformat()} "
                    "已越过锁箱边界，锁箱段不外发——请收窄时间窗"
                )
            if until is not None and until > cutoff:
                raise ValueError(
                    f"锁箱：{symbol} {tf} 研究数据截止（cutoff）为 "
                    f"{cutoff.isoformat()}，until={until.isoformat()} "
                    "已越过锁箱边界，锁箱段不外发——请收窄时间窗"
                )
            if until is None or until == cutoff:
                # 未显式传 until（或恰等于 cutoff）→ 钳到 cutoff，
                # 左闭右开：cutoff 时刻的 bar 属锁箱段，不外发
                cond.append(f"time < TIMESTAMP '{cutoff.isoformat()}'")
                clamped = True
        if since is not None:
            cond.append(f"time >= TIMESTAMP '{since.isoformat()}'")
        if until is not None and until != cutoff:
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
    out = {
        "symbol": _validate_symbol(symbol), "timeframe": timeframe.upper(),
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
    if clamped:
        out["lockbox_cutoff"] = cutoff.isoformat()
    return out


def describe_series(root, symbol: str, timeframe: str) -> dict:
    """序列概况：行数、范围、步长、最大相邻间隔（周末休市导致的大间隔属正常）。

    锁箱约束：该序列配置了 cutoff 时只统计开放段（time < cutoff），
    last_time 绝不越过 cutoff——否则等于泄露锁箱段的末时刻。
    """
    path = _parquet_path(root, symbol, timeframe)
    tf = timeframe.upper()
    cutoff = _lockbox_cutoff(root, symbol, tf)
    cond = f"WHERE time < TIMESTAMP '{cutoff.isoformat()}' " if cutoff else ""
    con = duckdb.connect()
    try:
        table = f"read_parquet('{_sql_literal(str(path))}')"
        row = con.execute(f"""
            SELECT count(*) AS n, min(time) AS f, max(time) AS l, max(gap) AS max_gap
            FROM (SELECT time, datediff('second', lag(time) OVER (ORDER BY time), time) AS gap
                  FROM {table} {cond})
        """).fetchone()
    finally:
        con.close()
    n, first, last, max_gap = row
    out = {
        "symbol": _validate_symbol(symbol), "timeframe": tf,
        "rows": int(n),
        "first_time": _iso(first),
        "last_time": _iso(last),
        "expected_step_minutes": TIMEFRAME_SECONDS[tf] / 60,
        "max_gap_seconds": int(max_gap) if max_gap is not None else 0,
        "note": "max_gap 大多为周末/假日休市；远超两倍步长且不是周末时段才值得排查",
    }
    if cutoff:
        out["lockbox_cutoff"] = cutoff.isoformat()
    return out


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
    """服务端现算指标，只回最后 limit 个点（只读尾部 limit+预热期，不碰全量）。

    锁箱约束：配置了 cutoff 时只读开放段（time < cutoff）的尾部——指标
    全部由开放段数据计算，锁箱段一根都不进（period=1 的 sma 即收盘价原值，
    不钳制等于整段外发锁箱收盘价）。
    """
    limit = max(1, min(int(limit), MAX_INDICATOR_POINTS))
    params = params or {}
    for key in ("period", "fast", "slow", "signal"):
        if key in params and params[key]:
            params[key] = _capped_period(params[key], key)
    warmup = _warmup_for(indicator, params)
    path = _parquet_path(root, symbol, timeframe)
    cutoff = _lockbox_cutoff(root, symbol, timeframe.upper())
    cond = f"WHERE time < TIMESTAMP '{cutoff.isoformat()}' " if cutoff else ""
    con = duckdb.connect()
    try:
        df = con.execute(
            f"SELECT * FROM read_parquet('{_sql_literal(str(path))}') {cond}"
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
    out = {
        "symbol": _validate_symbol(symbol), "timeframe": timeframe.upper(),
        "indicator": indicator, "params": params,
        "values": values,
    }
    if cutoff:
        out["lockbox_cutoff"] = cutoff.isoformat()
    return out
