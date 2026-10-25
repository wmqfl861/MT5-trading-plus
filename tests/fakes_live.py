"""mt5live 测试的离线替身（全部内存态，绝不触 MT5 终端、不下真实单）。

FakeBroker 实现 mt5live 依赖的 mt5gw 服务层子集，签名与语义贴真包：
- market_order / close_position / modify_position / cancel_order 与
  mt5gw.trading.TradingService 逐参一致（含 keyword-only sl/tp/deviation/
  comment/magic）；
- positions / pending_orders 与 mt5gw.account.AccountService 一致
  （返回模型对象列表；查询失败形态由真包裁决，替身只造成功形态）；
- quote 与 mt5gw.market.MarketData.quote 一致（Quote 模型）。

市价单按"每单新 ticket"记账（对冲式）；mt5live 执行器保证反向先平后开，
不会出现同品种双向持仓，因此替身不需要实现净额合并。
另含 runner 测试用的 toy 策略与 K 线构造器。
"""
from __future__ import annotations

import dataclasses
from datetime import datetime

import pandas as pd

from mt5gw.models import OrderResult, PendingOrder, Position, Quote
from mt5quant.strategies.base import BaseStrategy, ExitRules, RequiredData

_T0 = datetime(2026, 10, 20, 18, 0)   # 造数基准时刻（naive，贴 UTC 标签）


class FakeBroker:
    """内存撮合：一个对象同时充当 trading / account / market 三个角色。"""

    def __init__(self, symbol: str = "XAUUSD", *, bid: float = 100.0,
                 ask: float = 100.2, fill_price: float | None = None,
                 partial_volume: float | None = None,
                 error_on_market_order=None):
        self.symbol = symbol
        self.bid = bid
        self.ask = ask
        self.fill_price = fill_price        # None → buy 按 ask、sell 按 bid 成交
        self.partial_volume = partial_volume        # 非 None → DONE_PARTIAL
        self.error_on_market_order = error_on_market_order  # 非 None → 抛该异常
        self._next_ticket = 5001
        self._positions: list[Position] = []
        self._pending: list[PendingOrder] = []
        self.actions: list[tuple] = []      # 全局时序（断言"先平后开"等）
        self.market_orders: list[dict] = []
        self.close_calls: list[tuple] = []
        self.modify_calls: list[tuple] = []
        self.cancel_calls: list[int] = []

    # ---- 造数 ----

    def set_quote(self, bid: float, ask: float) -> None:
        self.bid, self.ask = bid, ask

    def seed_position(self, *, side: str = "buy", volume: float = 0.01,
                      magic: int, ticket: int | None = None,
                      sl: float = 0.0, tp: float = 0.0) -> Position:
        pos = Position(
            ticket=ticket or self._next_ticket, symbol=self.symbol, side=side,
            volume=volume, price_open=100.0, price_current=100.0, sl=sl, tp=tp,
            swap=0.0, profit=0.0, time=_T0, comment="seed", magic=magic)
        self._next_ticket = max(self._next_ticket + 1, pos.ticket + 1)
        self._positions.append(pos)
        return pos

    def seed_pending(self, *, magic: int, kind: str = "buy_limit",
                     volume: float = 0.01, ticket: int | None = None) -> PendingOrder:
        order = PendingOrder(
            ticket=ticket or self._next_ticket, symbol=self.symbol, kind=kind,
            volume=volume, price_open=99.0, price_current=100.0, sl=0.0, tp=0.0,
            time_setup=_T0, comment="seed", magic=magic, type_time=0,
            stoplimit=0.0, expiration=None)
        self._next_ticket = max(self._next_ticket + 1, order.ticket + 1)
        self._pending.append(order)
        return order

    def _take_position(self, ticket: int) -> Position:
        for p in self._positions:
            if p.ticket == ticket:
                return p
        raise ValueError(f"持仓 {ticket} 不存在（可能已平仓）")

    def _replace_position(self, pos: Position) -> None:
        self._positions = [
            pos if p.ticket == pos.ticket else p for p in self._positions]

    # ---- MarketData ----

    def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, time=_T0, bid=self.bid, ask=self.ask,
                     last=0.0, volume=0)

    # ---- AccountService ----

    def positions(self, symbol: str | None = None) -> list[Position]:
        return [p for p in self._positions if symbol in (None, p.symbol)]

    def pending_orders(self, symbol: str | None = None) -> list[PendingOrder]:
        return [o for o in self._pending if symbol in (None, o.symbol)]

    # ---- TradingService ----

    def market_order(self, symbol: str, side: str, volume: float, *,
                     sl: float | None = None, tp: float | None = None,
                     deviation: int = 20, comment: str = "",
                     magic: int | None = None) -> OrderResult:
        rec = dict(symbol=symbol, side=side, volume=volume, sl=sl, tp=tp,
                   deviation=deviation, comment=comment, magic=magic)
        self.market_orders.append(rec)
        self.actions.append(("market", rec))
        if self.error_on_market_order is not None:
            raise self.error_on_market_order
        fill = (self.fill_price if self.fill_price is not None
                else (self.ask if side == "buy" else self.bid))
        partial = self.partial_volume is not None
        filled_vol = self.partial_volume if partial else volume
        ticket = self._next_ticket
        self._next_ticket += 1
        self._positions.append(Position(
            ticket=ticket, symbol=symbol, side=side, volume=filled_vol,
            price_open=fill, price_current=fill, sl=sl or 0.0, tp=tp or 0.0,
            swap=0.0, profit=0.0, time=_T0, comment=comment, magic=magic or 0))
        return OrderResult(
            ok=True, filled=True, partial=partial,
            retcode=10010 if partial else 10009, comment="done",
            order_ticket=ticket, deal_ticket=ticket, deal_price=fill,
            requested_volume=volume, volume=filled_vol)

    def close_position(self, ticket: int, volume: float | None = None, *,
                       deviation: int = 20, magic: int | None = None) -> OrderResult:
        pos = self._take_position(ticket)
        vol = pos.volume if volume is None else volume
        if vol > pos.volume + 1e-9:
            raise ValueError(f"请求平仓 {vol} 手超出持仓余量 {pos.volume} 手")
        self.close_calls.append((ticket, vol))
        self.actions.append(("close", ticket, vol))
        remaining = round(pos.volume - vol, 8)
        if remaining > 1e-9:
            self._replace_position(dataclasses.replace(pos, volume=remaining))
        else:
            self._positions = [p for p in self._positions if p.ticket != ticket]
        exit_price = self.bid if pos.side == "buy" else self.ask
        return OrderResult(
            ok=True, filled=True, partial=False, retcode=10009, comment="close",
            order_ticket=ticket, deal_ticket=ticket, deal_price=exit_price,
            requested_volume=vol, volume=vol)

    def modify_position(self, ticket: int, *, sl: float | None = None,
                        tp: float | None = None) -> OrderResult:
        pos = self._take_position(ticket)
        self._replace_position(dataclasses.replace(
            pos, sl=sl if sl is not None else pos.sl,
            tp=tp if tp is not None else pos.tp))
        self.modify_calls.append((ticket, sl, tp))
        self.actions.append(("modify", ticket, sl, tp))
        return OrderResult(
            ok=True, filled=True, partial=False, retcode=10009, comment="done",
            order_ticket=ticket, deal_ticket=None, deal_price=None,
            requested_volume=None, volume=None)

    def cancel_order(self, ticket: int) -> OrderResult:
        self._take_pending(ticket)
        self._pending = [o for o in self._pending if o.ticket != ticket]
        self.cancel_calls.append(ticket)
        self.actions.append(("cancel", ticket))
        return OrderResult(
            ok=True, filled=False, partial=False, retcode=10009, comment="cancel",
            order_ticket=ticket, deal_ticket=None, deal_price=None,
            requested_volume=None, volume=None)

    def _take_pending(self, ticket: int) -> PendingOrder:
        for o in self._pending:
            if o.ticket == ticket:
                return o
        raise ValueError(f"挂单 {ticket} 不存在（可能已成交/已撤销）")


# ---- toy 策略（runner 测试用；记录每次调用收到的窗口）----

class LastBarDirection(BaseStrategy):
    """信号 = 末根 bar 方向（只依赖末根 → 天然窗口不变）。"""

    required_data = RequiredData(symbols=(), timeframe="", lookback_bars=2)
    exit_rules = ExitRules(stop_loss_atr=1.5, take_profit_atr=2.5)

    def __init__(self):
        self.calls: list[dict] = []

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        self.calls.append(dict(n=len(bars),
                               first_time=bars["time"].iloc[0],
                               last_time=bars["time"].iloc[-1]))
        up = float(bars["close"].iloc[-1]) > float(bars["open"].iloc[-1])
        return pd.Series(1 if up else -1, index=bars.index, dtype="int64")


class ConstantSignal(BaseStrategy):
    """恒定信号（构造参数给定），exit_rules 可逐实例覆盖。"""

    required_data = RequiredData(symbols=(), timeframe="", lookback_bars=1)

    def __init__(self, signal: int = 1, exit_rules: ExitRules | None = None):
        self.signal = signal
        if exit_rules is not None:
            self.exit_rules = exit_rules
        self.calls: list[dict] = []

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        self.calls.append(dict(n=len(bars)))
        return pd.Series(self.signal, index=bars.index, dtype="int64")


# ---- K 线构造器 ----

def mk_bars(rows) -> pd.DataFrame:
    """rows: [(time, open, high, low, close), ...] → 标准八列 DataFrame。"""
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])
    df["time"] = pd.to_datetime(df["time"])
    for c in ("tick_volume", "spread", "real_volume"):
        df[c] = 0
    return df


def const_range_bars(n: int, tf_seconds: int, start: str = "2026-10-20 08:00",
                     *, close: float = 100.0, tr: float = 2.0,
                     last_up: bool | None = None) -> pd.DataFrame:
    """n 根等幅 bar（时间从 start 起按 tf 递增）。

    常规根：open=close、high=close+tr/2、low=close−tr/2（TR≈tr，ATR 收敛到 tr）。
    末根方向由 last_up 指定：True=阳线、False=阴线、None=平的。
    """
    t0 = pd.Timestamp(start)
    rows = []
    for i in range(n):
        t = t0 + pd.Timedelta(seconds=tf_seconds * i)
        o = c = close
        if i == n - 1 and last_up is True:
            o = close - 1.0
        elif i == n - 1 and last_up is False:
            o = close + 1.0
        rows.append((t, o, close + tr / 2, close - tr / 2, c))
    return mk_bars(rows)


def mk_decision(decision_id: str = "XAUUSD|M15|2026-10-20T18:00:00",
                signal: int = 1, volume_lots: float = 0.01,
                atr: float | None = 2.0, sl_atr: float | None = 1.5,
                tp_atr: float | None = 2.5, **kw):
    """按缺省值构造 Decision（mt5live.runner），测试只覆盖关心的字段。"""
    from mt5live.runner import Decision  # 延迟导入：fakes 不应在 import 期拖入实现

    base = dict(
        decision_id=decision_id,
        decided_at=datetime(2026, 10, 20, 18, 0, 7),
        bar_time=datetime(2026, 10, 20, 17, 45),
        bar_close_time=datetime(2026, 10, 20, 18, 0),
        signal=signal, target_position=signal, volume_lots=volume_lots,
        quote_bid=100.0, quote_ask=100.2, atr=atr, sl_atr=sl_atr, tp_atr=tp_atr,
        sl=None, tp=None, bars_used=30)
    base.update(kw)
    return Decision(**base)
