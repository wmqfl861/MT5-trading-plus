"""Tick 历史采集与存储：Parquet + 毫秒级水位。

布局：{root}/ticks/{SYMBOL}/data.parquet + meta.json
（rows/first_msc/last_msc/updated_at_utc/server/login）。
约定与 bars 一致：时间为服务器时间贴 UTC 标签的 naive datetime；tick 是
已发生的事实，没有"未收盘"裁剪。写入一律原子（临时文件 + os.replace
带重试）+ 跨进程序列锁，复用 mt5data.store 的原语（经公共别名）。

数据保护铁律（对齐 mt5data.downloader）：
- 回补按时间窗从 end 往回分块（copy_ticks_range + COPY_TICKS_ALL）；
  连续 3 个空窗视为历史尽头正常收尾，不算错误；
- 块内 time_msc 必须单调不倒退——真包同一毫秒可有多条 tick（bid/ask 更新），
  相邻相等是正常数据；只有出现倒退（后一条更早）才判历史损坏，拒绝写盘；
- 单块返回 None 按 last_error 如实上抛，绝不吞错落残缺数据；
- 刷新用"重叠证明"验证接续：抓回页必须含旧末条 time_msc 那一条；
  空抓回且静默不足 7 天视为周末/休市，返回 0。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from mt5gw.errors import DataUnavailableError
from mt5data.store import atomic_replace, fsync_file, series_lock

logger = logging.getLogger(__name__)

# 贴真包 MetaTrader5.copy_ticks_range 结构化数组的字段（名字与顺序一致；
# 存储位宽：time_msc 毫秒 int64，bid/ask/last/volume float64，flags int64）
TICK_COLUMNS = ["time_msc", "bid", "ask", "last", "volume", "flags"]

_TICK_DTYPES = {
    "time_msc": "int64",
    "bid": "float64",
    "ask": "float64",
    "last": "float64",
    "volume": "float64",
    "flags": "int64",
}

_EMPTY_WINDOW_LIMIT = 3  # 连续空窗达到该块数 → 视为历史尽头正常收尾
_QUIET_DAYS = 7          # refresh 空抓回且静默不足该天数 → 视为休市，返回 0
_TO_MARGIN_S = 60        # refresh 上界放宽 60 秒，容下恰好落在 now 的 tick

_EPOCH = datetime(1970, 1, 1)


class DataIntegrityError(DataUnavailableError):
    """tick 历史完整性异常（接续无法证明/块内乱序）——拒绝写盘，如实上抛。"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _epoch_s(dt: datetime) -> int:
    # 与 mt5gw.models._epoch_seconds 同一约定：只接受贴 UTC 标签的 naive
    if dt.tzinfo is not None:
        raise ValueError(
            "只接受贴 UTC 标签的 naive datetime（服务器时间），"
            f"收到 tzinfo={dt.tzinfo}"
        )
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def ticks_dir(root, symbol: str) -> str:
    # 品种名保留原大小写（XAUUSDm 等带后缀品种大小写敏感）
    return os.path.join(root, "ticks", symbol)


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """统一列序与 dtype（真包 flags 为 int32 等，落盘一律按本库规范）。"""
    out = df.copy()
    for c in TICK_COLUMNS:
        out[c] = out[c].astype(_TICK_DTYPES[c])
    return out[TICK_COLUMNS]


def merge_ticks(old: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame:
    """合并去重：整行去重（同毫秒的多条 tick 是正常数据，绝不按 time_msc 去重）。

    排序按 time_msc 稳定排序（同毫秒内保持到达顺序），列序化返回。
    """
    if new is None or len(new) == 0:
        return old if old is not None else _normalize(new)
    if old is None or len(old) == 0:
        merged = new
    else:
        merged = pd.concat([old, new], ignore_index=True)
    merged = (
        merged.drop_duplicates(keep="last")
        .sort_values("time_msc", kind="stable")
        .reset_index(drop=True)
    )
    return merged[TICK_COLUMNS]


def _fetch_window(conn, mt5_module, symbol: str, from_s: int, to_s: int) -> pd.DataFrame:
    """取一个时间窗（epoch 秒，含两端）的 tick。

    None → 按 last_error 上抛；空窗 → 空表；返回前做块内不倒退校验。
    真包返回 numpy 结构化数组（多余字段如 volume_real 被列序化裁掉）。
    """
    rows = conn.call(
        mt5_module.copy_ticks_range, symbol, from_s, to_s, mt5_module.COPY_TICKS_ALL
    )
    if rows is None:
        raise DataUnavailableError(
            f"{symbol} tick 窗口 [{from_s}, {to_s}] 不可用：{conn.last_error()}"
        )
    if len(rows) == 0:
        return pd.DataFrame(columns=TICK_COLUMNS)
    arr = np.asarray(rows)
    df = pd.DataFrame(arr.tolist(), columns=list(arr.dtype.names))
    t = df["time_msc"].to_numpy(dtype="int64")
    bad = np.flatnonzero(np.diff(t) < 0)  # 相邻相等=同毫秒多条，合法；倒退才损坏
    if bad.size:
        i = int(bad[0])
        raise DataIntegrityError(
            f"{symbol} tick 窗口 [{from_s}, {to_s}] 第 {i} 行起 time_msc 倒退"
            f"（{t[i]} → {t[i + 1]}），历史损坏，拒绝写入"
        )
    return _normalize(df)


def backfill_ticks(conn, mt5_module, root, symbol: str, *, start=None, end=None,
                   chunk_days: float = 7.0, on_progress=None,
                   meta_info: dict | None = None) -> int:
    """分块回补 tick 历史，返回本次新增行数（合并后 − 旧库）。

    从 end（缺省 = 墙钟 UTC）往回按 chunk_days 取窗，直到 start；连续
    _EMPTY_WINDOW_LIMIT 个空窗视为历史尽头正常收尾（服务器 tick 史比请求
    的 start 更短不是错误）。on_progress(win_start, win_end, window_rows)
    每窗回调一次。start 必填（增量刷新用 refresh_ticks）。
    """
    if start is None:
        raise ValueError(
            "backfill_ticks 需要 start（贴 UTC 标签的 naive datetime）；"
            "增量刷新用 refresh_ticks"
        )
    if start.tzinfo is not None or (end is not None and end.tzinfo is not None):
        raise ValueError("start/end 只接受贴 UTC 标签的 naive datetime")
    if end is None:
        end = _utcnow()
    if end <= start:
        raise ValueError(f"end {end} 必须晚于 start {start}")

    step = timedelta(days=float(chunk_days))
    old = load_ticks(root, symbol)
    fetched: pd.DataFrame | None = None
    empty_run = 0
    win_end = end
    while win_end > start:
        win_start = max(win_end - step, start)
        page = _fetch_window(conn, mt5_module, symbol,
                             _epoch_s(win_start), _epoch_s(win_end))
        if len(page) == 0:
            empty_run += 1
            if empty_run >= _EMPTY_WINDOW_LIMIT:
                logger.info("%s tick 连续 %d 个空窗，视为历史尽头，正常收尾",
                            symbol, empty_run)
                break
        else:
            empty_run = 0
            fetched = (page if fetched is None
                       else pd.concat([fetched, page], ignore_index=True))
        if on_progress is not None:
            on_progress(win_start, win_end, len(page))
        win_end = win_start

    if fetched is None or len(fetched) == 0:
        if old is not None and len(old) > 0:
            return 0  # 一窗数据都没抓到但旧库健在：无事发生
        raise DataUnavailableError(
            f"{symbol} tick 回补未取到任何数据（终端未同步该品种/无 tick 历史？）"
        )
    merged = merge_ticks(old, fetched)
    save_ticks(root, symbol, merged, **(meta_info or {}))
    return int(len(merged) - (0 if old is None else len(old)))


def refresh_ticks(conn, mt5_module, root, symbol: str, *, now_server,
                  meta_info: dict | None = None) -> int:
    """增量刷新：从旧末条 last_msc（含）取到 now_server，返回新增行数。

    接续证明（对齐 bars 的 refresh 语义）：抓回页必须含 time_msc == last_msc
    的记录，否则抛 DataIntegrityError——接续点之后的缺口属于市场自身序列，
    不做算术推断。空抓回且 now − last_msc < 7 天 → 0（周末/无行情）。
    """
    if getattr(now_server, "tzinfo", None) is not None:
        raise ValueError("now_server 只接受贴 UTC 标签的 naive datetime")
    meta = read_ticks_watermark(root, symbol)
    old = load_ticks(root, symbol)
    if meta is None or old is None or len(old) == 0:
        raise DataUnavailableError(
            f"{symbol} 无历史 tick（{ticks_dir(root, symbol)} 缺 data.parquet/"
            "meta.json），先跑回补（--mode backfill）"
        )
    last_msc = int(meta["last_msc"])
    last_dt = _EPOCH + timedelta(milliseconds=last_msc)
    from_s = last_msc // 1000  # 含边界（秒粒度向下取整，宁可多抓几条）
    to_s = _epoch_s(now_server) + _TO_MARGIN_S

    page = _fetch_window(conn, mt5_module, symbol, from_s, to_s)
    if len(page) == 0:
        quiet = now_server - last_dt
        if quiet < timedelta(days=_QUIET_DAYS):
            return 0  # 周末/无行情的正常静默
        raise DataIntegrityError(
            f"{symbol} tick 自 {last_dt} 起静默 {quiet} 且抓回为空，无法证明接续"
            "（停更过久或服务端缺段）——请重跑回补（backfill）。"
        )
    if not (page["time_msc"] == last_msc).any():
        raise DataIntegrityError(
            f"{symbol} tick 抓回页不含旧末条 time_msc={last_msc}，无法证明接续"
            "（停更过久或服务端缺段）——请重跑回补（backfill）。"
        )
    merged = merge_ticks(old, page)
    save_ticks(root, symbol, merged, **(meta_info or {}))
    return int(len(merged) - len(old))


def save_ticks(root, symbol: str, df: pd.DataFrame,
               *, server: str | None = None, login: int | None = None) -> None:
    """原子写入 data.parquet 与 meta.json（先数据后水位，跨进程序列锁）。"""
    d = ticks_dir(root, symbol)
    os.makedirs(d, exist_ok=True)
    with series_lock(d):
        _save_ticks_locked(d, symbol, df, server=server, login=login)


def _save_ticks_locked(d, symbol, df, *, server=None, login=None):
    if df is None or len(df) == 0:
        raise ValueError("拒绝写入空 tick 表（先查抓取结果再落盘）")
    # meta 的 server/login 未提供时继承旧值（别用 null 覆盖掉数据来源）
    old_meta_path = os.path.join(d, "meta.json")
    if (server is None or login is None) and os.path.exists(old_meta_path):
        try:
            with open(old_meta_path, encoding="utf-8") as _f:
                old = json.load(_f)
            server = old.get("server") if server is None else server
            login = old.get("login") if login is None else login
        except (OSError, ValueError):
            pass
    out = _normalize(df)

    parquet_tmp = os.path.join(d, "data.parquet.tmp")
    out.to_parquet(parquet_tmp, index=False)
    fsync_file(parquet_tmp)
    atomic_replace(parquet_tmp, os.path.join(d, "data.parquet"))

    meta = {
        "symbol": symbol.upper(),
        "rows": int(len(out)),
        "first_msc": int(out["time_msc"].iloc[0]),
        "last_msc": int(out["time_msc"].iloc[-1]),
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "server": server,
        "login": login,
    }
    meta_tmp = os.path.join(d, "meta.json.tmp")
    with open(meta_tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    atomic_replace(meta_tmp, os.path.join(d, "meta.json"))


def load_ticks(root, symbol: str) -> pd.DataFrame | None:
    p = os.path.join(ticks_dir(root, symbol), "data.parquet")
    if not os.path.exists(p):
        return None
    return pd.read_parquet(p)


def read_ticks_watermark(root, symbol: str) -> dict | None:
    p = os.path.join(ticks_dir(root, symbol), "meta.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)
