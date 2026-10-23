"""行情：同步按需拉取，每次调用直接穿透到终端取其最新值。

不缓存、不轮询；事件驱动需求属于策略段（设计文档目标 2）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from mt5gw.connection import MT5Connection
from mt5gw.errors import DataUnavailableError
from mt5gw.symbols import ensure_symbol_visible
from mt5gw.models import Bar, Quote, Tick, _epoch_seconds, _utc

_LATEST_TICK_WINDOW = timedelta(minutes=5)


def _get(row, name):
    """numpy.void 行用下标，namedtuple 行用属性——两种来源都兼容。"""
    try:
        return row[name]
    except (TypeError, KeyError, IndexError):
        return getattr(row, name)


class MarketData:
    def __init__(self, conn: MT5Connection, mt5_module):
        self._conn = conn
        self._mt5 = mt5_module
        # 从注入模块收集 TIMEFRAME_*，避免在纯逻辑里硬编码数值
        self._timeframes = {
            attr[len("TIMEFRAME_"):]: getattr(mt5_module, attr)
            for attr in dir(mt5_module)
            if attr.startswith("TIMEFRAME_")
        }

    def quote(self, symbol: str) -> Quote:
        self._ensure_visible(symbol)
        t = self._conn.call(self._mt5.symbol_info_tick, symbol)
        if t is None:
            raise DataUnavailableError(f"{symbol} 报价不可用：{self._conn.last_error()}")
        return Quote(  # 包的 tick 对象没有 symbol 字段，用入参
            symbol=symbol,
            time=_utc(t.time),
            bid=t.bid,
            ask=t.ask,
            last=t.last,
            volume=t.volume,
        )

    def bars(self, symbol: str, timeframe: str, count: int, *, end_pos: int = 0) -> list[Bar]:
        """自 end_pos（0=最新一根，向过去数）起取 count 根 K 线；回填时用 end_pos 翻页。"""
        tf = self._timeframe(timeframe)
        self._ensure_visible(symbol)
        rows = self._conn.call(
            self._mt5.copy_rates_from_pos, symbol, tf, end_pos, count
        )
        if rows is None:
            raise DataUnavailableError(
                f"{symbol} {timeframe} K线不可用：{self._conn.last_error()}"
            )
        return [
            Bar(
                time=_utc(_get(r, "time")),
                open=_get(r, "open"), high=_get(r, "high"),
                low=_get(r, "low"), close=_get(r, "close"),
                tick_volume=_get(r, "tick_volume"),
                spread=_get(r, "spread"), real_volume=_get(r, "real_volume"),
            )
            for r in rows
        ]

    def ticks(self, symbol: str, count: int, *, since: datetime | None = None) -> list[Tick]:
        """取窗口内**最新**的 count 个 tick（旧实现取到的是窗口最早的 N 个）。

        做法：copy_ticks_range 取整窗（锚点=since 或最新报价前推 5 分钟，
        上界=最新报价+60s），再裁尾部 count 个——窗口内有缺口/流动性极高时
        也不依赖"从锚点数 N 个"的错误假设。
        """
        self._ensure_visible(symbol)
        latest = self.quote(symbol).time
        anchor = (since if since is not None else latest - _LATEST_TICK_WINDOW)
        rows = self._conn.call(
            self._mt5.copy_ticks_range, symbol,
            _epoch_seconds(anchor), _epoch_seconds(latest + timedelta(seconds=60)),
            self._mt5.COPY_TICKS_ALL,
        )
        if rows is None:
            raise DataUnavailableError(
                f"{symbol} tick 不可用：{self._conn.last_error()}"
            )
        ticks = [
            Tick(
                time=_utc(_get(r, "time_msc") / 1000.0),
                bid=_get(r, "bid"), ask=_get(r, "ask"),
                last=_get(r, "last"), volume=_get(r, "volume"),
            )
            for r in rows
        ]
        return ticks[-count:] if count and len(ticks) > count else ticks

    def _timeframe(self, timeframe: str):
        key = timeframe.upper()
        if key not in self._timeframes:
            raise ValueError(f"未知周期 {timeframe}，可用：{sorted(self._timeframes)}")
        return self._timeframes[key]

    def _ensure_visible(self, symbol: str) -> None:
        ensure_symbol_visible(self._conn, self._mt5, symbol)
