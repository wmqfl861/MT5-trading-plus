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

from mt5gw.models import (
    DEAL_REASON_CLIENT,
    DEAL_REASON_EXPERT,
    DEAL_REASON_SL,
    AccountState,
    Bar,
    Deal,
    OrderResult,
    PendingOrder,
    Position,
    Quote,
)
from mt5quant.strategies.base import BaseStrategy, ExitRules, RequiredData

_T0 = datetime(2026, 10, 20, 18, 0)   # 造数基准时刻（naive，贴 UTC 标签）


class FakeGateway:
    """最小网关替身：account/market/trading 三个服务都指向同一个 FakeBroker。"""

    def __init__(self, broker: "FakeBroker"):
        self.account = broker
        self.market = broker
        self.trading = broker

    def shutdown(self):
        """对齐 MT5Gateway 接口（脚本 main 的 finally 会调用；替身无资源）。"""


class FakeBroker:
    """内存撮合：一个对象同时充当 trading / account / market 三个角色。

    T24 扩展：账户状态（state/deals，权益=余额+浮动）、成交流水记账、
    外部平仓 close_external（模拟 SL/TP 触发）、终端 bar 缓存（bars/set_bars，
    供 TerminalBarsSource 按 mt5gw.market.MarketData.bars 签名取数）。

    R6（I8 假件补强，不再为场景特制桩）：
    - partial_fill：0<ratio<1 的成交比例——market_order 与 close_position
      都按比例成交（DONE_PARTIAL，retcode 10010），驱动"全平只平掉一半"
      的残仓场景（C1）；
    - deals_delay_polls：成交历史延迟可见——一笔成交在记录后需再经 N 次
      deals_for_position 查询才进入可见历史，驱动"历史延迟两轮才解释"
      的外部出场状态机场景（C2 复现 A）。
    """

    def __init__(self, symbol: str = "XAUUSD", *, bid: float = 100.0,
                 ask: float = 100.2, fill_price: float | None = None,
                 partial_volume: float | None = None,
                 partial_fill: float | None = None,
                 error_on_market_order=None,
                 balance: float = 10_000.0, contract_size: float = 100.0,
                 deals_delay_polls: int = 0):
        self.symbol = symbol
        self.bid = bid
        self.ask = ask
        self.fill_price = fill_price        # None → buy 按 ask、sell 按 bid 成交
        self.partial_volume = partial_volume        # 非 None → DONE_PARTIAL（绝对量）
        self.partial_fill = partial_fill    # 非 None → 按比例成交（优先于上者）
        self.error_on_market_order = error_on_market_order  # 非 None → 抛该异常
        self.balance = balance                      # 账户余额（USD）
        self.contract_size = contract_size          # 平仓盈亏换算用
        self.deals_delay_polls = deals_delay_polls  # 成交 N 次查询后才可见（I8）
        self.quote_time: datetime | None = None     # None → 报价时刻恒为 _T0
        self._next_ticket = 5001
        self._positions: list[Position] = []
        self._pending: list[PendingOrder] = []
        self._deals: list[Deal] = []
        self._deal_visible_at: list[int] = []   # 与 _deals 对齐：可见的查询序号
        self._deal_polls = 0                    # deals_for_position 累计调用数
        self._bars: dict[tuple[str, str], list[Bar]] = {}
        self.actions: list[tuple] = []      # 全局时序（断言"先平后开"等）
        self.market_orders: list[dict] = []
        self.close_calls: list[tuple] = []
        self.modify_calls: list[tuple] = []
        self.cancel_calls: list[int] = []

    # ---- 造数 ----

    def set_quote(self, bid: float, ask: float) -> None:
        self.bid, self.ask = bid, ask

    # ---- AccountService 补充（T24：权益/成交流水） ----

    def _floating(self) -> float:
        return float(sum(p.profit + p.swap for p in self._positions))

    def state(self) -> AccountState:
        equity = self.balance + self._floating()
        return AccountState(
            login=123456, server="Fake-Server", company="Fake Broker Ltd.",
            currency="USD", leverage=100, balance=self.balance, equity=equity,
            margin=0.0, margin_free=equity, margin_mode=2, trade_mode=0)

    def _next_deal_ticket(self) -> int:
        ticket = 10_000_000 + len(self._deals)
        while any(d.ticket == ticket for d in self._deals):
            ticket += 1
        return ticket

    def _append_deal(self, deal: Deal) -> None:
        """记账一笔成交（I8/R6：含"延迟可见"水位——记录后第 N 次查询可见）。"""
        self._deals.append(deal)
        self._deal_visible_at.append(
            self._deal_polls + self.deals_delay_polls + 1)

    def _record_deal(self, *, entry: str, side: str, volume: float, price: float,
                     profit: float, comment: str, magic: int,
                     position_id: int = 0, reason: int | None = None,
                     time: datetime | None = None) -> None:
        self._append_deal(Deal(
            ticket=self._next_deal_ticket(), order=0, position_id=position_id,
            symbol=self.symbol, side=side, entry=entry, volume=volume,
            price=price, profit=profit, commission=0.0, swap=0.0, fee=0.0,
            time=time or _T0, comment=comment, magic=magic, reason=reason))

    def deals(self, since: datetime, to: datetime) -> list[Deal]:
        return [d for d in self._deals if since <= d.time <= to]

    def deals_for_position(self, position_id: int) -> list[Deal]:
        """按持仓 id 查全部成交（对齐 AccountService.deals_for_position；
        T30 外部出场识别用——不看时间窗，跨"日"也找得到）。

        I8/R6：deals_delay_polls>0 时模拟"成交历史延迟可见"——一笔成交
        记录后需再经 N 次**本查询**（每调用一次计一次）才进入可见历史；
        deals(since, to)（日亏等全量流水）不受延迟影响。
        """
        self._deal_polls += 1
        return [d for d, visible_at in zip(self._deals, self._deal_visible_at)
                if d.position_id == position_id
                and self._deal_polls >= visible_at]

    def seed_deal(self, *, profit: float = 0.0, magic: int,
                  entry: str = "out", time: datetime | None = None,
                  symbol: str | None = None, volume: float = 0.1) -> Deal:
        """手工塞一笔当日流水（模拟历史已实现盈亏，供日亏判定）。"""
        d = Deal(
            ticket=self._next_deal_ticket(), order=0, position_id=0,
            symbol=symbol or self.symbol, side="buy", entry=entry,
            volume=volume, price=100.0, profit=profit, commission=0.0,
            swap=0.0, fee=0.0, time=time or _T0, comment="seed", magic=magic)
        self._append_deal(d)
        return d

    def close_external(self, ticket: int, *, profit: float = 0.0,
                       comment: str = "[sl]",
                       reason: int = DEAL_REASON_SL) -> None:
        """外部平仓（SL/TP 触发或人工）：不走执行器，流水如实记账。

        reason 为 MT5 deal.reason（mt5gw.models.DEAL_REASON_*；真包核对的
        常量）。缺省 SL——既有调用方（含探针 P8）都模拟止损出场。
        """
        pos = self._take_position(ticket)
        self._positions = [p for p in self._positions if p.ticket != ticket]
        self.balance += profit
        self._record_deal(entry="out",
                          side="sell" if pos.side == "buy" else "buy",
                          volume=pos.volume, price=pos.price_current,
                          profit=profit, comment=comment, magic=pos.magic,
                          position_id=ticket, reason=reason)

    def reduce_external(self, ticket: int, volume: float, *, profit: float = 0.0,
                        comment: str = "manual",
                        reason: int = DEAL_REASON_CLIENT) -> None:
        """外部部分减仓（人工）：不走执行器，余量保留同一 ticket，流水如实记账。

        N-C1/R7：驱动"人工减仓（ticket 仍在，不是出场事件）→ 确认 →
        再全平（ticket 消失，才是出场事件）"的复现序列。全平请用
        close_external（volume 须严格小于持仓余量）。
        """
        pos = self._take_position(ticket)
        vol = round(float(volume), 8)
        if vol <= 1e-9 or vol >= pos.volume - 1e-9:
            raise ValueError(
                f"减仓量 {vol} 须在 (0, {pos.volume}) 内（全平用 close_external）")
        remaining = round(pos.volume - vol, 8)
        self._replace_position(dataclasses.replace(pos, volume=remaining))
        self.balance += profit
        exit_price = self.bid if pos.side == "buy" else self.ask
        self._record_deal(entry="out",
                          side="sell" if pos.side == "buy" else "buy",
                          volume=vol, price=exit_price, profit=profit,
                          comment=comment, magic=pos.magic,
                          position_id=ticket, reason=reason)

    def remove_position(self, ticket: int) -> None:
        self._take_position(ticket)
        self._positions = [p for p in self._positions if p.ticket != ticket]

    def set_position_profit(self, ticket: int, profit: float) -> None:
        pos = self._take_position(ticket)
        self._replace_position(dataclasses.replace(pos, profit=profit))

    # ---- MarketData.bars（T24：TerminalBarsSource 的数据源） ----

    def set_bars(self, symbol: str, timeframe: str, bars: list[Bar]) -> None:
        self._bars[(symbol, timeframe.upper())] = list(bars)

    def bars(self, symbol: str, timeframe: str, count: int, *,
             end_pos: int = 0) -> list[Bar]:
        rows = self._bars.get((symbol, timeframe.upper()), [])
        # end_pos=0 是最新一根，向过去数 count 根；返回升序（copy_rates 同序）
        end = len(rows) - end_pos
        start = max(0, end - count)
        return list(rows[start:end])


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
        return Quote(symbol=symbol, time=self.quote_time or _T0,
                     bid=self.bid, ask=self.ask, last=0.0, volume=0)

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
        if self.partial_fill is not None:        # I8/R6：按比例成交
            filled_vol = round(volume * float(self.partial_fill), 8)
            partial = filled_vol < volume - 1e-9
        else:
            partial = self.partial_volume is not None
            filled_vol = self.partial_volume if partial else volume
        ticket = self._next_ticket
        self._next_ticket += 1
        self._positions.append(Position(
            ticket=ticket, symbol=symbol, side=side, volume=filled_vol,
            price_open=fill, price_current=fill, sl=sl or 0.0, tp=tp or 0.0,
            swap=0.0, profit=0.0, time=_T0, comment=comment, magic=magic or 0))
        self._record_deal(entry="in", side=side, volume=filled_vol, price=fill,
                          profit=0.0, comment=comment, magic=magic or 0,
                          position_id=ticket, reason=DEAL_REASON_EXPERT)
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
        if (self.partial_fill is not None
                and vol * float(self.partial_fill) < vol - 1e-9):
            # I8/R6：平仓腿按比例成交（驱动 C1"全平只平掉一半"残仓场景）
            filled = round(vol * float(self.partial_fill), 8)
            partial = True
        else:
            filled, partial = vol, False
        self.close_calls.append((ticket, vol))
        self.actions.append(("close", ticket, vol))
        remaining = round(pos.volume - filled, 8)
        if remaining > 1e-9:
            self._replace_position(dataclasses.replace(pos, volume=remaining))
        else:
            self._positions = [p for p in self._positions if p.ticket != ticket]
        exit_price = self.bid if pos.side == "buy" else self.ask
        direction = 1.0 if pos.side == "buy" else -1.0
        profit = ((exit_price - pos.price_open) * direction * filled
                  * self.contract_size)
        self.balance += profit
        self._record_deal(entry="out",
                          side="sell" if pos.side == "buy" else "buy",
                          volume=filled, price=exit_price, profit=profit,
                          comment="close", magic=pos.magic,
                          position_id=pos.ticket, reason=DEAL_REASON_EXPERT)
        return OrderResult(
            ok=True, filled=True, partial=partial,
            retcode=10010 if partial else 10009, comment="close",
            order_ticket=ticket, deal_ticket=ticket, deal_price=exit_price,
            requested_volume=vol, volume=filled)

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


class QuoteMarket:
    """最小行情源（只提供 quote；PaperBroker / 闸门测试注入用，T34）。

    quote.time 恒为 _T0（无休市语义——陈旧判定不适用）；报价经 set_quote
    逐轮驱动（造"报价序列"断言 SL/TP 触发时刻与成交价）。
    """

    def __init__(self, symbol: str = "XAUUSD", *, bid: float = 100.0,
                 ask: float = 100.2):
        self.symbol = symbol
        self.bid = bid
        self.ask = ask

    def set_quote(self, bid: float, ask: float) -> None:
        self.bid, self.ask = bid, ask

    def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, time=_T0, bid=self.bid, ask=self.ask,
                     last=0.0, volume=0)


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


class ClosingSignal(BaseStrategy):
    """末根收盘方向：阳线 +1 / 阴线 -1 / 平盘 0（exit_rules 可逐实例覆盖）。

    T24 集成测试用：信号方向由测试通过末根 bar 的开收价逐轮控制。
    """

    required_data = RequiredData(symbols=(), timeframe="", lookback_bars=2)

    def __init__(self, exit_rules: ExitRules | None = None):
        if exit_rules is not None:
            self.exit_rules = exit_rules
        self.calls: list[dict] = []

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        o, c = float(bars["open"].iloc[-1]), float(bars["close"].iloc[-1])
        sig = 1 if c > o else (-1 if c < o else 0)
        return pd.Series(sig, index=bars.index, dtype="int64")


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


def grid_bars(last_open, n: int = 40, *, tf_seconds: int = 900,
              close: float = 100.0, tr: float = 2.0,
              last: str = "up") -> pd.DataFrame:
    """n 根等幅 bar，**末根开盘时刻 = last_open**（运行器触发网格期望的
    已收盘 bar；now=last_open+tf+容差 时 decide 应恰好消费这根）。

    last："up"（阳线 +1）/"down"（阴线 -1）/"flat"（平盘 0）——配合
    ClosingSignal 逐轮控制信号方向。TR≈tr，ATR 收敛到 tr。
    """
    base = pd.Timestamp(last_open)
    rows = []
    for i in range(n):
        t = base - pd.Timedelta(seconds=tf_seconds * (n - 1 - i))
        o = c = close
        if i == n - 1 and last == "up":
            o = close - 1.0
        elif i == n - 1 and last == "down":
            o = close + 1.0
        rows.append((t, o, close + tr / 2, close - tr / 2, c))
    return mk_bars(rows)


def boundary_open(now, tf_seconds: int = 900):
    """now 所在触发网格点（= 上一根 bar 的收盘时刻）对应**期望的最新已收盘
    bar 开盘时刻**：floor(now/tf)·tf − tf。"""
    epoch = int(pd.Timestamp(now).timestamp())
    boundary = epoch - epoch % tf_seconds
    return (pd.Timestamp(boundary, unit="s") - pd.Timedelta(seconds=tf_seconds))


def bars_from_df(df: pd.DataFrame) -> list:
    """八列 DataFrame → mt5gw.models.Bar 列表（FakeBroker.set_bars 用）。"""
    from mt5gw.models import Bar as _Bar
    out = []
    for _, r in df.iterrows():
        out.append(_Bar(
            time=r["time"].to_pydatetime(), open=float(r["open"]),
            high=float(r["high"]), low=float(r["low"]), close=float(r["close"]),
            tick_volume=int(r["tick_volume"]), spread=int(r["spread"]),
            real_volume=int(r["real_volume"])))
    return out


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
