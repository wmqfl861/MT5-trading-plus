"""K 线存储：Parquet 文件 + 水位。

布局：{root}/bars/{SYMBOL}/{TF}/data.parquet + meta.json（水位）。
约定（与 mt5gw 一致）：time 为服务器时间贴 UTC 标签的 naive datetime。
写入一律原子（临时文件 + os.replace 带重试），读者不会看到半截文件。
"""
from __future__ import annotations

import json
import os
import time
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


def series_dir(root, symbol: str, timeframe: str) -> str:
    # 品种名保留原大小写（XAUUSDm 等带后缀品种大小写敏感）；周期用规范大写
    return os.path.join(root, "bars", symbol, timeframe.upper())


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


def drop_unclosed(df: pd.DataFrame, timeframe: str, now_server: datetime) -> pd.DataFrame:
    """剔除未收盘的 K 线。

    规则：非末行的 bar 必已收盘（其后已有新 bar）；末行收盘当且仅当
    time+周期 <= now_server。now_server = 墙钟 UTC + 自校准券商时差
    （downloader.server_now，超限回退 offset=0 属墙钟近似而非完全解耦，
    方向见 README 时区约定）；无任何宽容秒数。
    """
    if df is None or len(df) == 0:
        return df
    tf = TIMEFRAME_SECONDS[timeframe.upper()]
    head = df.iloc[:-1]
    last = df.iloc[[-1]]
    last_closed = last["time"] + pd.to_timedelta(tf, unit="s") <= pd.Timestamp(now_server)
    return pd.concat([head, last[last_closed]]).reset_index(drop=True)


def _atomic_replace(tmp_path: str, final_path: str) -> None:
    for attempt in range(10):
        try:
            os.replace(tmp_path, final_path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.3)  # Windows：读者占着句柄时稍候重试（总预算约 3 秒）


def _fsync_file(path: str) -> None:
    fd = os.open(path, os.O_RDWR)  # Windows：fsync 需写句柄
    try:
        os.fsync(fd)  # 落盘后再替换，掉电不至半截
    finally:
        os.close(fd)


class _SeriesLock:
    """同序列的跨进程写锁（定时刷新与手动回填并发时互相保护）。

    锁文件带时间戳，超过 stale_after 视为残锁接管（进程崩溃遗留）。
    """

    def __init__(self, directory: str, stale_after: float = 600.0):
        self._path = os.path.join(directory, ".lock")
        self._stale = stale_after

    def __enter__(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        for _ in range(50):
            try:
                fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(time.time()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(self._path)
                    if age > self._stale:
                        os.remove(self._path)  # 残锁接管
                        continue
                except OSError:
                    pass
                time.sleep(0.2)
        raise TimeoutError(f"等待序列写锁超时：{self._path}（另一进程在写？）")

    def __exit__(self, *exc):
        try:
            os.remove(self._path)
        except OSError:
            pass


def save_bars(root, symbol: str, timeframe: str, df: pd.DataFrame,
              *, server: str | None = None, login: int | None = None) -> None:
    """原子写入 data.parquet 与 meta.json（先数据后水位，跨进程序列锁）。"""
    d = series_dir(root, symbol, timeframe)
    os.makedirs(d, exist_ok=True)
    with _SeriesLock(d):
        _save_bars_locked(d, symbol, timeframe, df, server=server, login=login)


def _save_bars_locked(d, symbol, timeframe, df, *, server=None, login=None):
    # meta 的 server/login 未提供时继承旧值（别用 null 覆盖掉数据来源）
    old_meta_path = os.path.join(d, "meta.json")
    if (server is None or login is None) and os.path.exists(old_meta_path):
        try:
            old = json.load(open(old_meta_path, encoding="utf-8"))
            server = old.get("server") if server is None else server
            login = old.get("login") if login is None else login
        except (OSError, ValueError):
            pass
    out = df.copy()
    out["time"] = pd.to_datetime(out["time"]).astype("datetime64[ns]")
    for c in ("open", "high", "low", "close"):
        out[c] = out[c].astype("float64")
    for c in ("tick_volume", "spread", "real_volume"):
        out[c] = out[c].astype("int64")
    out = out[_COLUMNS]

    parquet_tmp = os.path.join(d, "data.parquet.tmp")
    out.to_parquet(parquet_tmp, index=False)
    _fsync_file(parquet_tmp)
    _atomic_replace(parquet_tmp, os.path.join(d, "data.parquet"))

    wm = {
        "symbol": symbol.upper(),
        "timeframe": timeframe.upper(),
        "rows": int(len(out)),
        "first_time": out["time"].iloc[0].isoformat(),
        "last_closed_time": out["time"].iloc[-1].isoformat(),
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "server": server,
        "login": login,
    }
    meta_tmp = os.path.join(d, "meta.json.tmp")
    with open(meta_tmp, "w", encoding="utf-8") as f:
        json.dump(wm, f, ensure_ascii=False, indent=2)
    _atomic_replace(meta_tmp, os.path.join(d, "meta.json"))


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
