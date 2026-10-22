"""K 线下载器：分块回填全量历史 + 增量刷新，只落库已收盘 bar。

数据保护铁律（C3/I6 修复后）：
- 回填中途出错必须上抛，绝不把残缺数据写盘；
- 与既有文件合并，结果若比现存历史更短/更晚起步则拒绝写入（force=True 除外）；
- 一根都没取到就是失败；
- 刷新若与旧数据接不上（空洞）必须报错，绝不静默留洞。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

from mt5gw.errors import DataUnavailableError
from mt5gw.market import MarketData
from mt5data.store import (
    TIMEFRAME_SECONDS, drop_unclosed, load_bars, merge_bars, read_watermark,
    save_bars,
)

logger = logging.getLogger(__name__)

ALL_TIMEFRAMES = ["M1", "M2", "M3", "M4", "M5", "M6", "M10", "M12", "M15",
                  "M20", "M30", "H1", "H2", "H3", "H4", "H6", "H8", "H12",
                  "D1", "W1", "MN1"]

_OFFSET_LIMIT = timedelta(hours=6)  # 券商时差合理上限；超出视为时钟异常


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def server_now(market: MarketData, symbol: str) -> datetime:
    """估算服务器"现在"= 墙钟 UTC + 券商时差。

    时差由 (最新 tick 时间 - 墙钟) 自校准，限幅 ±6 小时；周末行情冻结时
    tick 滞后数日，超出限幅 → 回退 offset=0（墙钟即服务器时钟的近似），
    保证已收盘的周五 bar 不被误删、未收盘 bar 不会因宽容混入库。
    """
    wall = _utcnow()
    tick_time = market.quote(symbol).time
    offset = tick_time - wall
    if abs(offset) > _OFFSET_LIMIT:
        logger.info(
            "%s tick 时间 %s 与墙钟差 %s 超限，按 offset=0 处理（行情冻结/时钟异常）",
            symbol, tick_time, offset,
        )
        offset = timedelta(0)
    return wall + offset


def _is_weekend_gap(start, end) -> bool:
    """缺口 [start, end) 是否全部落在服务器时间的周六/周日（休市，非数据洞）。

    小时粒度扫描；注意周日傍晚开市后的真实缺口也会被放行——由下一次
    全量回填兜底（节假日整日休市同理，需手动重跑回填治愈）。
    """
    cur = start
    while cur < end:
        if cur.weekday() not in (5, 6):
            return False
        cur += timedelta(hours=1)
    return True


def _to_df(bars) -> pd.DataFrame:
    return pd.DataFrame([{
        "time": b.time, "open": b.open, "high": b.high, "low": b.low,
        "close": b.close, "tick_volume": b.tick_volume,
        "spread": b.spread, "real_volume": b.real_volume,
    } for b in bars])


def _fetch_pages(market: MarketData, symbol: str, timeframe: str, chunk: int,
                 max_chunks: int, stop_before: datetime | None):
    """从最新往回分块翻页；返回 (合并后的 DataFrame, chunks)。错误一律上抛。

    翻到与 stop_before（旧文件末根时间）重叠即停——既省流量，又验证接续。
    """
    merged: pd.DataFrame | None = None
    pos = 0
    chunks = 0
    while chunks < max_chunks:
        batch = market.bars(symbol, timeframe, chunk, end_pos=pos)  # 错误上抛
        chunks += 1
        if not batch:
            break  # 取尽（服务器无更多历史）
        merged = merge_bars(merged, _to_df(batch))
        pos += len(batch)
        oldest = merged["time"].iloc[0]
        if stop_before is not None and oldest <= pd.Timestamp(stop_before):
            break  # 已与旧数据重叠
        if len(batch) < chunk:
            break  # 短块 = 取尽
    return merged, chunks


def _shrink_guard(new_df: pd.DataFrame, old: pd.DataFrame | None, symbol, tf, force: bool):
    """结果不得比现存历史更短或更晚起步（防截断覆盖，C3）。"""
    if old is None or len(old) == 0 or force:
        return new_df
    if len(new_df) < len(old):
        raise DataUnavailableError(
            f"{symbol} {tf}：新取 {len(new_df):,} 根 < 现存 {len(old):,} 根，"
            "疑似截断（终端同步失败？），拒绝写入；确要覆盖请用 force=True 重跑。"
        )
    if new_df["time"].iloc[0] > old["time"].iloc[0]:
        raise DataUnavailableError(
            f"{symbol} {tf}：新数据起点 {new_df['time'].iloc[0]} 晚于现存 "
            f"{old['time'].iloc[0]}，历史被截断，拒绝写入。"
        )
    return merge_bars(old, new_df)


def backfill_series(root, market: MarketData, symbol: str, timeframe: str,
                    chunk: int = 500_000, max_chunks: int = 2000, *,
                    force: bool = False, meta_info: dict | None = None) -> dict:
    """全量回填：取尽历史 → 剔除未收盘 → 收缩保护 → 原子落库。"""
    now = server_now(market, symbol)
    old = load_bars(root, symbol, timeframe)
    merged, chunks = _fetch_pages(market, symbol, timeframe, chunk, max_chunks,
                                  stop_before=None)
    if merged is None or len(merged) == 0:
        raise DataUnavailableError(  # C3：一根都没取到就是失败
            f"{symbol} {timeframe} 未取到任何数据（终端未同步该品种/周期？）"
        )
    closed = drop_unclosed(merged, timeframe, now)
    if closed is None or len(closed) == 0:
        raise DataUnavailableError(f"{symbol} {timeframe} 取到的全是未收盘 bar，异常")
    guarded = _shrink_guard(closed, old, symbol, timeframe, force)
    save_bars(root, symbol, timeframe, guarded, **(meta_info or {}))
    return {
        "symbol": symbol, "timeframe": timeframe,
        "rows": int(len(guarded)), "chunks_fetched": chunks,
        "first_time": guarded["time"].iloc[0].isoformat(),
        "last_time": guarded["time"].iloc[-1].isoformat(),
    }


def refresh_series(root, market: MarketData, symbol: str, timeframe: str,
                   lookback: int = 5000, *, meta_info: dict | None = None) -> dict:
    """增量刷新：翻页回取至与旧数据重叠；接不上（空洞）必须报错。

    旧文件不存在时直接转全量回填（绝不静默只存一小段）。
    """
    old = load_bars(root, symbol, timeframe)
    if old is None or len(old) == 0:
        logger.info("%s %s 无旧文件，刷新转为全量回填", symbol, timeframe)
        return backfill_series(root, market, symbol, timeframe,
                               meta_info=meta_info)

    now = server_now(market, symbol)
    tf_seconds = TIMEFRAME_SECONDS[timeframe.upper()]
    old_last = old["time"].iloc[-1]
    # 旧水位之后应有的根数 + 缓冲，再兜底下限，避免长停后刷新留洞
    gap = (now - old_last).total_seconds() / tf_seconds
    need = max(lookback, int(gap * 1.2) + 100)

    merged, chunks = _fetch_pages(market, symbol, timeframe, need,
                                  max_chunks=2000, stop_before=old_last)
    if merged is None or len(merged) == 0:
        raise DataUnavailableError(f"{symbol} {timeframe} 刷新未取到任何数据")

    new_part = merged[merged["time"] > old_last]
    if len(new_part) > 0:
        oldest_new = new_part["time"].iloc[0]
        expected_next = old_last + pd.to_timedelta(tf_seconds, unit="s")
        if oldest_new > expected_next:  # I6：空洞检测（周末休市放行）
            if _is_weekend_gap(expected_next, oldest_new):
                logger.info(
                    "%s %s 缺口 %s → %s 全部落在周末（休市），按正常处理",
                    symbol, timeframe, expected_next, oldest_new,
                )
            else:
                raise DataUnavailableError(
                    f"{symbol} {timeframe} 刷新存在空洞：旧末根 {old_last} 之后直接跳到 "
                    f"{oldest_new}（缺约 {int((oldest_new - expected_next).total_seconds() / tf_seconds)} 根）。"
                    "请重跑全量回填（backfill）。"
                )
    combined = merge_bars(old, merged)
    closed = drop_unclosed(combined, timeframe, now)
    # 只动新数据：已入库的旧 bar 未经重新裁决，不因本次刷新被删
    if len(closed) < len(old):
        closed = combined  # 理论不发生；防御性保留旧数据
    save_bars(root, symbol, timeframe, closed, **(meta_info or {}))
    return {
        "symbol": symbol, "timeframe": timeframe,
        "rows": int(len(closed)),
        "new_bars": int(len(closed) - len(old)),
        "first_time": closed["time"].iloc[0].isoformat(),
        "last_time": closed["time"].iloc[-1].isoformat(),
    }
