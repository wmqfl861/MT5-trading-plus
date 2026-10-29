"""K 线下载器：分块回填全量历史 + 增量刷新，只落库已收盘 bar。

数据保护铁律：
- 回填中途出错必须上抛，绝不把残缺数据写盘；
- 与既有文件一律取并集（force 也只是放弃"收缩即拒"的检查，不丢行）；
- 彻底替换需显式 --replace，且替换前自动备份旧文件；
- 刷新用"重叠证明"验证接续：翻页结果必须包含旧末根那一根——
  接续点之后的缺口属于终端/市场自身的序列（休市），不做算术推断；
- 一根都没取到、或翻页取尽仍无法与旧数据接续，都是失败。
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from mt5gw.errors import DataUnavailableError
from mt5gw.market import MarketData
from mt5data.store import (
    TIMEFRAME_SECONDS, drop_unclosed, load_bars_checked, merge_bars, update_bars,
)

logger = logging.getLogger(__name__)

ALL_TIMEFRAMES = ["M1", "M2", "M3", "M4", "M5", "M6", "M10", "M12", "M15",
                  "M20", "M30", "H1", "H2", "H3", "H4", "H6", "H8", "H12",
                  "D1", "W1", "MN1"]

_OFFSET_LIMIT = timedelta(hours=6)  # 券商时差合理上限；超出视为时钟异常


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def server_now(market: MarketData, symbol: str) -> datetime:
    """估算服务器"现在" = 墙钟 UTC + 券商时差（由最新 tick 自校准，限幅 ±6h）。

    注意：超限回退 offset=0 是"墙钟近似"，并非与 tick 完全解耦——周末冻结时
    tick 滞后数日、offset 回退 0，方向保守（晚判收盘），不会把未收盘 bar 提前
    放行；GMT 负偏移的券商在极端情况下可能高估 now，见 README 时区约定。
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


def _to_df(bars) -> pd.DataFrame:
    return pd.DataFrame([{
        "time": b.time, "open": b.open, "high": b.high, "low": b.low,
        "close": b.close, "tick_volume": b.tick_volume,
        "spread": b.spread, "real_volume": b.real_volume,
    } for b in bars])


def _fetch_pages(market: MarketData, symbol: str, timeframe: str, chunk: int,
                 max_chunks: int, stop_before):
    """从最新往回分页；返回 (合并后的 DataFrame, chunks)。错误一律上抛。

    每页请求 chunk+1 根：整除边界时下一页必然拿不满，杜绝"恰好整数倍
    导致越界请求"的歧义。翻到与 stop_before 重叠即停。
    """
    merged = None
    pos = 0
    chunks = 0
    want = chunk + 1
    while chunks < max_chunks:
        batch = market.bars(symbol, timeframe, want, end_pos=pos)  # 错误上抛
        chunks += 1
        if not batch:
            break  # 取尽（服务器无更多历史）
        merged = merge_bars(merged, _to_df(batch))
        pos += len(batch)
        if stop_before is not None and merged["time"].iloc[0] <= pd.Timestamp(stop_before):
            break  # 已与旧数据重叠
        if len(batch) < want:
            break  # 不满页 = 取尽
    else:
        raise DataUnavailableError(
            f"{symbol} {timeframe} 翻页达 max_chunks={max_chunks} 上限仍未取尽，"
            "结果不完整，拒绝写入（调大 chunk/max_chunks 后重试）。"
        )
    return merged, chunks


def _lineage_guard(old_meta, meta_info, symbol, tf):
    """数据血缘：新旧 server/login 不一致说明换了券商/账号，拒绝混写。"""
    if not meta_info or not old_meta:
        return
    old_key = (old_meta.get("server"), old_meta.get("login"))
    new_key = (meta_info.get("server"), meta_info.get("login"))
    if old_key != (None, None) and new_key != (None, None) and old_key != new_key:
        raise DataUnavailableError(
            f"{symbol} {tf} 数据来源变更：库内 {old_key} vs 当前 {new_key}，"
            "拒绝把不同券商/账号的数据混入同一序列（如确要更换，用 --replace）。"
        )


def backfill_series(root, market: MarketData, symbol: str, timeframe: str,
                    chunk: int = 500_000, max_chunks: int = 2000, *,
                    force: bool = False, replace: bool = False,
                    meta_info: dict | None = None) -> dict:
    """全量回填：取尽历史 → 剔除未收盘 → 并集合并（收缩需 force）→ 原子落库。

    H-11：抓取在锁外；"读最新表→合并→校验→发布"整段经 update_bars 在
    序列写锁内完成——并发写者新增的段不会被本次的旧快照覆盖丢。
    """
    now = server_now(market, symbol)
    old = load_bars_checked(root, symbol, timeframe)
    _lineage_guard(_read_meta(root, symbol, timeframe), meta_info, symbol, timeframe)
    merged, chunks = _fetch_pages(market, symbol, timeframe, chunk, max_chunks,
                                  stop_before=None)
    if merged is None or len(merged) == 0:
        raise DataUnavailableError(
            f"{symbol} {timeframe} 未取到任何数据（终端未同步该品种/周期？）"
        )
    closed = drop_unclosed(merged, timeframe, now)
    if closed is None or len(closed) == 0:
        raise DataUnavailableError(f"{symbol} {timeframe} 取到的全是未收盘 bar，异常")

    def _merge(latest, fetched):
        # 锁内复检：并发写者可能已推进水位/血缘
        _lineage_guard(_read_meta(root, symbol, timeframe), meta_info,
                       symbol, timeframe)
        if replace and latest is not None and len(latest):
            _backup(root, symbol, timeframe)  # 锁内备份：备的正是将被替换的那份
            return fetched
        return _union_guard(fetched, latest, symbol, timeframe, force)

    target = update_bars(root, symbol, timeframe, closed, merge=_merge,
                         **(meta_info or {}))
    return {
        "symbol": symbol, "timeframe": timeframe,
        "rows": int(len(target)), "chunks_fetched": chunks,
        "first_time": target["time"].iloc[0].isoformat(),
        "last_time": target["time"].iloc[-1].isoformat(),
    }


def refresh_series(root, market: MarketData, symbol: str, timeframe: str,
                   lookback: int = 5000, *, meta_info: dict | None = None) -> dict:
    """增量刷新：翻页回取至与旧末根**重叠**（重叠即接续证明）。

    接续点之后的缺口（周末/假日休市、日内休市）属于源数据自身的序列，
    不做算术推断、不会误报；翻页取尽仍够不到旧末根才是失败。
    旧文件不存在时直接转全量回填。
    H-11：抓取在锁外；"读最新表→合并→校验→发布"整段经 update_bars 在
    序列写锁内完成（锁内重读最新表，两个并发刷新不互覆）。
    """
    old = load_bars_checked(root, symbol, timeframe)
    if old is None or len(old) == 0:
        logger.info("%s %s 无旧文件，刷新转为全量回填", symbol, timeframe)
        return backfill_series(root, market, symbol, timeframe,
                               meta_info=meta_info)

    _lineage_guard(_read_meta(root, symbol, timeframe), meta_info, symbol, timeframe)
    now = server_now(market, symbol)
    tf_seconds = TIMEFRAME_SECONDS[timeframe.upper()]
    old_last = old["time"].iloc[-1]
    gap_bars = (now - old_last).total_seconds() / tf_seconds
    need = max(lookback, int(gap_bars * 1.2) + 100)

    merged, chunks = _fetch_pages(market, symbol, timeframe, need,
                                  max_chunks=2000, stop_before=old_last)
    if merged is None or len(merged) == 0:
        raise DataUnavailableError(f"{symbol} {timeframe} 刷新未取到任何数据")
    if not (merged["time"] == old_last).any():
        raise DataUnavailableError(
            f"{symbol} {timeframe} 翻页取尽仍未覆盖旧末根 {old_last}，无法证明与"
            "旧数据接续（停更过久或服务端缺段）——请重跑全量回填（backfill）。"
        )

    def _merge(latest, fetched):
        # 锁内复检：并发写者可能已推进水位/血缘
        _lineage_guard(_read_meta(root, symbol, timeframe), meta_info,
                       symbol, timeframe)
        base = latest if (latest is not None and len(latest)) else None
        if base is not None and not fetched["time"].isin(base["time"]).any():
            # 抓取时的接续证明对的是当时的旧末根；锁内最新表已与抓取结果
            # 无任何重叠（并发 --replace 收缩等）→ 证明失效，拒绝发布
            raise DataUnavailableError(
                f"{symbol} {timeframe} 抓取结果与库内最新数据无重叠（并发写者"
                "已改写该序列？），无法证明接续——请重试或重跑全量回填"
                "（backfill）。"
            )
        combined = merge_bars(base, fetched)
        closed = drop_unclosed(combined, timeframe, now)
        if base is not None and len(closed) < len(base):
            # 时钟异常的防御分支：绝不因刷新删旧数据
            logger.warning("%s %s 收盘裁剪反而变短（时钟异常？），保留并集",
                           symbol, timeframe)
            closed = combined
        return closed

    closed = update_bars(root, symbol, timeframe, merged, merge=_merge,
                         **(meta_info or {}))
    return {
        "symbol": symbol, "timeframe": timeframe,
        "rows": int(len(closed)),
        "new_bars": int(len(closed) - len(old)),
        "first_time": closed["time"].iloc[0].isoformat(),
        "last_time": closed["time"].iloc[-1].isoformat(),
    }


def _union_guard(new_df: pd.DataFrame, old, symbol, tf, force: bool):
    """结果与旧文件取并集；比旧历史更短/更晚起步时，force 之外一律拒绝。"""
    if old is None or len(old) == 0:
        return new_df
    shrunk = len(new_df) < len(old) or new_df["time"].iloc[0] > old["time"].iloc[0]
    if shrunk and not force:
        raise DataUnavailableError(
            f"{symbol} {tf}：新取数据比现存更短或更晚起步"
            f"（新 {len(new_df):,} 根起 {new_df['time'].iloc[0]}，"
            f"旧 {len(old):,} 根起 {old['time'].iloc[0]}），疑似截断——常见原因是"
            "终端'图表最大柱数'被重置为默认 10 万（重装/换机后），先检查该设置。"
            "确要以新数据为准请用 --replace（旧文件自动备份）。"
        )
    return merge_bars(old, new_df)  # 并集：force 也不丢行


def _read_meta(root, symbol, tf):
    from mt5data.store import read_watermark
    return read_watermark(root, symbol, tf)


def _backup(root, symbol, tf) -> str:
    from mt5data.store import series_dir
    d = series_dir(root, symbol, tf)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bak = os.path.join(d, f"data.parquet.bak-{stamp}")
    shutil.copy2(os.path.join(d, "data.parquet"), bak)
    return bak
