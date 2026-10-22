"""K 线存储：Parquet 文件 + 水位。

布局：{root}/bars/{SYMBOL}/{TF}/data.parquet + meta.json（水位）。
约定（与 mt5gw 一致）：time 为服务器时间贴 UTC 标签的 naive datetime。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pandas as pd

# 全部 21 个标准周期；MN1 按 31 天保守判定"月线收盘"
TIMEFRAME_SECONDS = {
    "M1": 60, "M2": 120, "M3": 180, "M4": 240, "M5": 300, "M6": 360,
    "M10": 600, "M12": 720, "M15": 900, "M20": 1200, "M30": 1800,
    "H1": 3600, "H2": 7200, "H3": 10800, "H4": 14400, "H6": 21600,
    "H8": 28800, "H12": 43200,
    "D1": 86400, "W1": 7 * 86400, "MN1": 31 * 86400,
}

_COLUMNS = ["time", "open", "high", "low", "close",
            "tick_volume", "spread", "real_volume"]
_CLOSED_SLACK = pd.Timedelta(seconds=5)  # 行情冻结（周末）时收盘时刻==最后tick时刻


def series_dir(root, symbol: str, timeframe: str) -> str:
    return os.path.join(root, "bars", symbol.upper(), timeframe.upper())


def merge_bars(old: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame:
    """按 time 去重（保留 new 中的较新行），升序返回。"""
    if new is None or len(new) == 0:
        return old if old is not None else new
    if old is None or len(old) == 0:
        merged = new
    else:
        merged = pd.concat([old, new], ignore_index=True)
    merged = (
        merged.drop_duplicates(subset="time", keep="last")
        .sort_values("time")
        .reset_index(drop=True)
    )
    return merged[_COLUMNS]


def drop_unclosed(df: pd.DataFrame, timeframe: str, now: datetime) -> pd.DataFrame:
    """剔除未收盘的 K 线：收盘时刻（time+周期）须 <= now+5s。

    5 秒宽容是为了周末行情冻结时不误删最后一根已收盘 bar
    （最后 tick 时刻恰好早于收盘时刻）。
    """
    if df is None or len(df) == 0:
        return df
    tf = TIMEFRAME_SECONDS[timeframe.upper()]
    close_time = df["time"] + pd.to_timedelta(tf, unit="s")
    keep = close_time <= pd.Timestamp(now) + _CLOSED_SLACK
    return df[keep].reset_index(drop=True)


def save_bars(root, symbol: str, timeframe: str, df: pd.DataFrame) -> None:
    d = series_dir(root, symbol, timeframe)
    os.makedirs(d, exist_ok=True)
    out = df.copy()
    out["time"] = pd.to_datetime(out["time"]).astype("datetime64[ns]")
    for c in ("open", "high", "low", "close"):
        out[c] = out[c].astype("float64")
    for c in ("tick_volume", "spread", "real_volume"):
        out[c] = out[c].astype("int64")
    out[_COLUMNS].to_parquet(os.path.join(d, "data.parquet"), index=False)
    wm = {
        "symbol": symbol.upper(),
        "timeframe": timeframe.upper(),
        "rows": int(len(out)),
        "first_time": out["time"].iloc[0].isoformat(),
        "last_closed_time": out["time"].iloc[-1].isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(wm, f, ensure_ascii=False, indent=2)


def load_bars(root, symbol: str, timeframe: str) -> pd.DataFrame | None:
    p = os.path.join(series_dir(root, symbol, timeframe), "data.parquet")
    if not os.path.exists(p):
        return None
    return pd.read_parquet(p)


def read_watermark(root, symbol: str, timeframe: str) -> dict | None:
    p = os.path.join(series_dir(root, symbol, timeframe), "meta.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)
