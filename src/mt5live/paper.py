"""mt5live.paper（T34/M3，决策 D26）：影子券商——真实行情、模拟成交。

PaperBroker 实现**执行器与闸门用到的** mt5gw TradingService / AccountService
子集（ExecutionAdapter / RiskGate / LiveSizer / scripts/risk_process /
scripts/live_reconcile 的实际调用面，见各方法 docstring），一个对象同时充当
trading 与 account 两个角色；market 角色（quote / bars）由注入的行情服务承担
——影子盘用真实终端（mt5gw.MarketData），离线干跑用 BarQuoteMarket（合成）。

成交语义：
- 市价开仓按报价成交（买 Ask / 卖 Bid——与执行器参考价同侧），平仓按出场侧
  （多头 Bid / 空头 Ask）；盈亏 = 价差 × 手数 × contract_size（USD）；
- ``poll_protective(now)`` 每轮按实时 bid/ask 检查 SL/TP，**触发口径与引擎
  一致**（engine_a._protective_exit 逐字：多头按 Bid、空头按 Ask；SL 优先），
  触发即平仓，成交带 reason（DEAL_REASON_SL / DEAL_REASON_TP）——执行器的
  外部出场识别（mt5live.executor._sync_external_exits）据 reason 把这类出场
  解释为 external_exit（已解释，不改意图为漂移、对账不暂停）；
- 成交价 = 触发一侧现价：真实券商按市价平仓，报价流下等价于引擎 B 的
  "第一笔穿价 tick"；engine_a 的 stop_gap / 止损越幅是回测侧的成本近似，
  不适用于券商模拟（T36 的越幅加在回测 A 层，不在这里重复加）。

状态持久化：每次成交即经 mt5live.fileio.write_json_atomic 落
``data/live/paper/state.json``（持仓逐字段、余额、全部成交流水、票据与成交
序号），重启（新 PaperBroker、同一路径）恢复全部记账——票据序号接着发，
绝不与历史碰撞。风控进程与对账进程用 ``--paper-state`` 经
PaperAccountView / PaperStateGateway 读这个文件，**不碰真实账户**。

绝不 import MetaTrader5、绝不连终端下单：影子券商的全部"成交"都是本地记账。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Callable

from mt5data.store import load_bars
from mt5gw.errors import DataUnavailableError
from mt5gw.models import (
    DEAL_REASON_EXPERT,
    DEAL_REASON_SL,
    DEAL_REASON_TP,
    AccountState,
    Deal,
    OrderResult,
    Position,
    Quote,
)
from mt5live.fileio import write_json_atomic
from mt5live.monitor import iso_utc, naive_utc, parse_iso

# 影子券商状态文件（相对 live 目录）；live_runner 固定落 <live>/paper/state.json
PAPER_STATE_RELPATH = os.path.join("paper", "state.json")

_STATE_VERSION = 1
_EPS = 1e-9

# 起始票据：与 FakeBroker / 真实终端的常见区间都拉开距离，便于从流水一眼
# 分辨"影子成交"；重启后从状态文件接着发。
_FIRST_TICKET = 7_000_001
_FIRST_DEAL_TICKET = 8_000_001


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _contract_size(contract_sizes: dict[str, float], symbol: str) -> float:
    cs = contract_sizes.get(symbol)
    if cs is None or not isinstance(cs, (int, float)) or cs <= 0:
        raise DataUnavailableError(
            f"影子券商缺 {symbol} 的 contract_size（快照 data/specs/"
            "latest.json 未覆盖该品种？）——盈亏换算无依据，拒绝记账")
    return float(cs)


class PaperBroker:
    """影子券商：trading + account 双角色的内存撮合账本 + 状态文件。

    服务面（按调用方逐一对齐，签名与 mt5gw 同名方法逐参一致）：
    - TradingService 子集（ExecutionAdapter）：
      market_order / close_position / modify_position / cancel_order；
    - AccountService 子集（ExecutionAdapter / RiskGate / LiveSizer /
      scripts/risk_process.build_snapshot）：
      state / positions / pending_orders / deals / deals_for_position；
    - 影子主循环：poll_protective（运行器每轮调用，先于 executor.poll——
      保护性出场先发生，执行器同轮就能按 reason 识别 external_exit）。
    """

    def __init__(self, market, *, state_path: str,
                 contract_sizes: dict[str, float],
                 balance: float = 10_000.0,
                 clock: Callable[[], datetime] | None = None):
        """
        - market：行情服务（quote(symbol) -> Quote；影子盘=真实终端
          mt5gw.MarketData，离线干跑=BarQuoteMarket）；
        - state_path：状态文件路径（存在即恢复，balance 参数被文件覆盖）；
        - contract_sizes：symbol → 合约大小（快照 data/specs/latest.json）；
        - balance：初始余额（仅无状态文件时生效）；
        - clock：成交时刻来源（缺省墙钟 UTC；测试注入模拟钟）。
        """
        self._market = market
        self._state_path = state_path
        self._contract_sizes = dict(contract_sizes)
        self._clock = clock or _now_utc
        self.balance = float(balance)
        self._positions: list[Position] = []
        self._deals: list[Deal] = []
        self._next_ticket = _FIRST_TICKET
        self._next_deal_ticket = _FIRST_DEAL_TICKET
        had_state = os.path.exists(state_path)
        self._load()
        if not had_state:
            self.save()   # 首次启动即落盘：--paper-state 消费者立刻可读

    # ---- TradingService 子集 ----

    def market_order(self, symbol: str, side: str, volume: float, *,
                     sl: float | None = None, tp: float | None = None,
                     deviation: int = 20, comment: str = "",
                     magic: int | None = None) -> OrderResult:
        """市价开仓：买按 Ask、卖按 Bid 成交（执行器参考价同侧）。"""
        if side not in ("buy", "sell"):
            raise ValueError(f"非法方向 {side}，仅接受 buy/sell")
        if not isinstance(volume, (int, float)) or volume <= 0:
            raise ValueError(f"非法交易量 {volume!r}")
        _contract_size(self._contract_sizes, symbol)
        quote = self._market.quote(symbol)
        fill = float(quote.ask if side == "buy" else quote.bid)
        ticket = self._next_ticket
        self._next_ticket += 1
        now = self._clock()
        pos = Position(
            ticket=ticket, symbol=symbol, side=side, volume=float(volume),
            price_open=fill, price_current=fill,
            sl=float(sl) if sl else 0.0, tp=float(tp) if tp else 0.0,
            swap=0.0, profit=0.0, time=now, comment=comment,
            magic=magic if magic is not None else 0)
        self._positions.append(pos)
        self._record(pos, entry="in", side=side, volume=pos.volume,
                     price=fill, profit=0.0, comment=comment,
                     reason=DEAL_REASON_EXPERT, time=now)
        self.save()
        return OrderResult(
            ok=True, filled=True, partial=False, retcode=10009, comment="done",
            order_ticket=ticket, deal_ticket=self._deals[-1].ticket,
            deal_price=fill, requested_volume=float(volume),
            volume=float(volume))

    def close_position(self, ticket: int, volume: float | None = None, *,
                       deviation: int = 20,
                       magic: int | None = None) -> OrderResult:
        """市价平仓：多头按 Bid、空头按 Ask 成交；盈亏入余额。"""
        pos = self._take_position(ticket)
        vol = pos.volume if volume is None else float(volume)
        if vol > pos.volume + _EPS:
            raise DataUnavailableError(
                f"请求平仓 {vol} 手超出持仓余量 {pos.volume} 手（{ticket}）")
        quote = self._market.quote(pos.symbol)
        exit_price = float(quote.bid if pos.side == "buy" else quote.ask)
        deal = self._close_at(pos, vol, exit_price, reason=DEAL_REASON_EXPERT,
                              comment="close", time=self._clock())
        return OrderResult(
            ok=True, filled=True, partial=False, retcode=10009,
            comment="close", order_ticket=ticket, deal_ticket=deal.ticket,
            deal_price=exit_price, requested_volume=vol, volume=vol)

    def modify_position(self, ticket: int, *, sl: float | None = None,
                        tp: float | None = None) -> OrderResult:
        """改持仓 SL/TP：只改一侧、另一侧保持现值（mt5gw 同语义）。"""
        if sl is None and tp is None:
            raise ValueError("modify_position 至少要给 sl 或 tp 之一")
        pos = self._take_position(ticket)
        self._positions = [
            Position(
                ticket=pos.ticket, symbol=pos.symbol, side=pos.side,
                volume=pos.volume, price_open=pos.price_open,
                price_current=pos.price_current,
                sl=float(sl) if sl is not None else pos.sl,
                tp=float(tp) if tp is not None else pos.tp,
                swap=pos.swap, profit=pos.profit, time=pos.time,
                comment=pos.comment, magic=pos.magic)
            if p.ticket == ticket else p
            for p in self._positions]
        self.save()
        return OrderResult(
            ok=True, filled=True, partial=False, retcode=10009, comment="done",
            order_ticket=ticket, deal_ticket=None, deal_price=None,
            requested_volume=None, volume=None)

    def cancel_order(self, ticket: int) -> OrderResult:
        """影子券商只走市价单（无挂单）：撤单如实抛错。"""
        raise DataUnavailableError(
            f"挂单 {ticket} 不存在（影子券商只走市价单，无挂单可撤）")

    # ---- AccountService 子集 ----

    def state(self) -> AccountState:
        """账户状态：余额 + 按现价浮动 = 权益（USD；无保证金概念）。"""
        positions = self.positions()
        floating = sum(p.profit + p.swap for p in positions)
        equity = self.balance + floating
        return AccountState(
            login=0, server="paper-shadow", company="PaperBroker (T34)",
            currency="USD", leverage=100, balance=self.balance, equity=equity,
            margin=0.0, margin_free=equity, margin_mode=2, trade_mode=0)

    def positions(self, symbol: str | None = None) -> list[Position]:
        """持仓（price_current/profit 按注入行情源的现价刷新，尽力而为：
        行情暂不可用时保留上次值——持仓本身不受影响）。"""
        out: list[Position] = []
        for p in self._positions:
            if symbol is not None and p.symbol != symbol:
                continue
            out.append(self._refresh(p))
        return out

    def pending_orders(self, symbol: str | None = None) -> list:
        """影子券商只走市价单：恒无挂单。"""
        return []

    def deals(self, since: datetime, to: datetime) -> list[Deal]:
        return [d for d in self._deals
                if naive_utc(since) <= d.time <= naive_utc(to)]

    def deals_for_position(self, position_id: int) -> list[Deal]:
        """按持仓 id 查全部成交（开+平腿；执行器外部出场识别用）。"""
        return [d for d in self._deals if d.position_id == position_id]

    # ---- 影子主循环：保护性出场 ----

    def poll_protective(self, now: datetime) -> list[Deal]:
        """每轮按实时 bid/ask 检查 SL/TP，触发即平仓（成交带 reason）。

        触发口径与 engine_a._protective_exit 逐字一致：
        - 多头（buy）：SL 当 Bid ≤ SL；TP 当 Bid ≥ TP；
        - 空头（sell）：SL 当 Ask ≥ SL；TP 当 Ask ≤ TP；
        - SL 优先于 TP（引擎"同 bar 双触：止损优先"）。
        成交价 = 触发一侧现价（见模块 docstring：真实券商按市价平仓）。
        返回本轮平仓成交（通常 0 或 1 条；多张持仓可多条）。

        行情取不到的品种本轮跳过（不打崩运行器主循环——真实服务器端
        SL/TP 的保护在影子模式里由下一轮补上），下轮照常。
        """
        n = naive_utc(now if now is not None else self._clock())
        closed: list[Deal] = []
        for pos in list(self._positions):
            try:
                quote = self._market.quote(pos.symbol)
            except Exception:
                continue
            bid, ask = float(quote.bid), float(quote.ask)
            if pos.side == "buy":
                if pos.sl > 0 and bid <= pos.sl:
                    reason = DEAL_REASON_SL
                elif pos.tp > 0 and bid >= pos.tp:
                    reason = DEAL_REASON_TP
                else:
                    continue
                price = bid
            else:
                if pos.sl > 0 and ask >= pos.sl:
                    reason = DEAL_REASON_SL
                elif pos.tp > 0 and ask <= pos.tp:
                    reason = DEAL_REASON_TP
                else:
                    continue
                price = ask
            closed.append(self._close_at(
                pos, pos.volume, price, reason=reason,
                comment="[sl]" if reason == DEAL_REASON_SL else "[tp]",
                time=n))
        return closed

    # ---- 状态持久化（fileio 原子写；重启恢复） ----

    def save(self) -> None:
        """状态落盘：持仓（含最新浮动感）、余额、全部流水、票据序号。"""
        positions = [self._refresh(p) for p in self._positions]
        payload = {
            "version": _STATE_VERSION,
            "paper": True,
            "updated_at_utc": iso_utc(_now_utc()),
            "balance": self.balance,
            "equity": self.balance + sum(p.profit + p.swap for p in positions),
            "next_ticket": self._next_ticket,
            "next_deal_ticket": self._next_deal_ticket,
            "positions": [_position_to_dict(p) for p in positions],
            "deals": [_deal_to_dict(d) for d in self._deals],
        }
        d = os.path.dirname(self._state_path)
        if d:
            os.makedirs(d, exist_ok=True)
        write_json_atomic(self._state_path, payload)

    def _load(self) -> None:
        if not os.path.exists(self._state_path):
            return
        with open(self._state_path, encoding="utf-8") as f:
            data = json.load(f)
        self.balance = float(data["balance"])
        self._next_ticket = int(data.get("next_ticket", _FIRST_TICKET))
        self._next_deal_ticket = int(data.get("next_deal_ticket",
                                              _FIRST_DEAL_TICKET))
        self._positions = [_position_from_dict(p)
                           for p in data.get("positions", [])]
        self._deals = [_deal_from_dict(dd) for dd in data.get("deals", [])]

    # ---- 记账内部 ----

    def _take_position(self, ticket: int) -> Position:
        for p in self._positions:
            if p.ticket == ticket:
                return p
        raise DataUnavailableError(f"持仓 {ticket} 不存在（可能已平仓）")

    def _refresh(self, pos: Position) -> Position:
        """按现价刷新 price_current/profit（best-effort，失败保留原值）。"""
        try:
            quote = self._market.quote(pos.symbol)
        except Exception:
            return pos
        cs = self._contract_sizes.get(pos.symbol)
        if not cs:
            return pos
        price = float(quote.bid if pos.side == "buy" else quote.ask)
        direction = 1.0 if pos.side == "buy" else -1.0
        profit = (price - pos.price_open) * direction * pos.volume * float(cs)
        return Position(
            ticket=pos.ticket, symbol=pos.symbol, side=pos.side,
            volume=pos.volume, price_open=pos.price_open, price_current=price,
            sl=pos.sl, tp=pos.tp, swap=pos.swap, profit=profit, time=pos.time,
            comment=pos.comment, magic=pos.magic)

    def _record(self, pos: Position, *, entry: str, side: str, volume: float,
                price: float, profit: float, comment: str,
                reason: int | None, time: datetime) -> Deal:
        deal = Deal(
            ticket=self._next_deal_ticket, order=0, position_id=pos.ticket,
            symbol=pos.symbol, side=side, entry=entry, volume=volume,
            price=price, profit=profit, commission=0.0, swap=0.0, fee=0.0,
            time=time, comment=comment, magic=pos.magic, reason=reason)
        self._next_deal_ticket += 1
        self._deals.append(deal)
        return deal

    def _close_at(self, pos: Position, volume: float, price: float, *,
                  reason: int | None, comment: str,
                  time: datetime) -> Deal:
        """平仓记账：盈亏入余额、out 成交、持仓了结/减量、状态落盘。"""
        cs = _contract_size(self._contract_sizes, pos.symbol)
        direction = 1.0 if pos.side == "buy" else -1.0
        profit = (price - pos.price_open) * direction * volume * cs
        self.balance += profit
        deal = self._record(
            pos, entry="out",
            side="sell" if pos.side == "buy" else "buy",
            volume=volume, price=price, profit=profit, comment=comment,
            reason=reason, time=time)
        remaining = round(pos.volume - volume, 8)
        if remaining > _EPS:
            self._positions = [
                Position(
                    ticket=pos.ticket, symbol=pos.symbol, side=pos.side,
                    volume=remaining, price_open=pos.price_open,
                    price_current=pos.price_current, sl=pos.sl, tp=pos.tp,
                    swap=pos.swap, profit=pos.profit, time=pos.time,
                    comment=pos.comment, magic=pos.magic)
                if p.ticket == pos.ticket else p
                for p in self._positions]
        else:
            self._positions = [p for p in self._positions
                               if p.ticket != pos.ticket]
        self.save()
        return deal


# ---- 序列化（时间：内存 naive UTC，文件 ISO 带 +00:00——全仓约定） ----

def _position_to_dict(p: Position) -> dict:
    return {
        "ticket": p.ticket, "symbol": p.symbol, "side": p.side,
        "volume": p.volume, "price_open": p.price_open,
        "price_current": p.price_current, "sl": p.sl, "tp": p.tp,
        "swap": p.swap, "profit": p.profit, "time": iso_utc(p.time),
        "comment": p.comment, "magic": p.magic,
    }


def _position_from_dict(d: dict) -> Position:
    return Position(
        ticket=int(d["ticket"]), symbol=d["symbol"], side=d["side"],
        volume=float(d["volume"]), price_open=float(d["price_open"]),
        price_current=float(d["price_current"]), sl=float(d["sl"]),
        tp=float(d["tp"]), swap=float(d.get("swap", 0.0)),
        profit=float(d.get("profit", 0.0)), time=parse_iso(d["time"]),
        comment=d.get("comment", ""), magic=int(d.get("magic", 0)))


def _deal_to_dict(d: Deal) -> dict:
    return {
        "ticket": d.ticket, "order": d.order, "position_id": d.position_id,
        "symbol": d.symbol, "side": d.side, "entry": d.entry,
        "volume": d.volume, "price": d.price, "profit": d.profit,
        "commission": d.commission, "swap": d.swap, "fee": d.fee,
        "time": iso_utc(d.time), "comment": d.comment, "magic": d.magic,
        "reason": d.reason,
    }


def _deal_from_dict(d: dict) -> Deal:
    return Deal(
        ticket=int(d["ticket"]), order=int(d.get("order", 0)),
        position_id=int(d.get("position_id", 0)), symbol=d["symbol"],
        side=d["side"], entry=d.get("entry"), volume=float(d["volume"]),
        price=float(d["price"]), profit=float(d.get("profit", 0.0)),
        commission=float(d.get("commission", 0.0)),
        swap=float(d.get("swap", 0.0)), fee=float(d.get("fee", 0.0)),
        time=parse_iso(d["time"]), comment=d.get("comment", ""),
        magic=int(d.get("magic", 0)),
        reason=(int(d["reason"]) if d.get("reason") is not None else None))


# ================================================================ --paper-state
# 只读账户视图：risk_process / live_reconcile 用状态文件替代真实账户。

class PaperAccountView:
    """从 paper 状态文件构造的只读账户视图（不连终端、不下单）。

    服务面对齐 risk_process.build_snapshot / live_reconcile.run_once 的
    调用：state / positions / deals / deals_for_position（pending_orders
    恒空）。浮动盈亏用落盘时快照（运行器每轮成交/轮询都会刷新落盘）。
    """

    def __init__(self, state_path: str):
        if not os.path.exists(state_path):
            raise DataUnavailableError(
                f"影子券商状态文件不存在：{state_path}——先以 --paper 启动"
                "运行器写出 paper 状态")
        with open(state_path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "balance" not in data:
            raise DataUnavailableError(
                f"{state_path} 不是合法的 paper 状态文件（顶层须为带 "
                "balance 的对象）")
        self._data = data
        self._positions = [_position_from_dict(p)
                           for p in data.get("positions", [])]
        self._deals = [_deal_from_dict(d) for d in data.get("deals", [])]
        self._equity = float(data.get("equity", data["balance"]))
        self._balance = float(data["balance"])

    def state(self) -> AccountState:
        return AccountState(
            login=0, server="paper-shadow", company="PaperBroker (T34)",
            currency="USD", leverage=100, balance=self._balance,
            equity=self._equity, margin=0.0, margin_free=self._equity,
            margin_mode=2, trade_mode=0)

    def positions(self, symbol: str | None = None) -> list[Position]:
        return [p for p in self._positions
                if symbol in (None, p.symbol)]

    def pending_orders(self, symbol: str | None = None) -> list:
        return []

    def deals(self, since: datetime, to: datetime) -> list[Deal]:
        return [d for d in self._deals
                if naive_utc(since) <= d.time <= naive_utc(to)]

    def deals_for_position(self, position_id: int) -> list[Deal]:
        return [d for d in self._deals if d.position_id == position_id]


class PaperStateGateway:
    """给 scripts 用的最小网关替身：account 指向 PaperAccountView。

    risk_process / live_reconcile 的 ``--paper-state`` 装配：不连真实
    MT5 终端，只读影子券商状态文件。shutdown 无资源可释放。
    """

    def __init__(self, state_path: str):
        self.account = PaperAccountView(state_path)

    def shutdown(self):
        pass


# ================================================================ 离线合成行情
#（--paper --dry-run --offline 的行情源；影子盘实跑用真实终端注入，不用这个）

class BarQuoteMarket:
    """离线干跑的合成行情源：bid/ask = parquet 最新 bar 收盘价 ∓ spread/2。

    只服务影子券商在离线冒烟（--once）下的成交与保护性出场模拟——
    quote.time = 该 bar 收盘时刻（最新 bar 的下一根开盘沿）。数据为静态
    parquet（无实时流），每次 quote 现读文件。
    """

    def __init__(self, data_root: str, *, symbol: str, timeframe: str,
                 spread: float = 0.2):
        if spread < 0:
            raise ValueError(f"spread 须为非负，收到 {spread!r}")
        self._root = data_root
        self._symbol = symbol
        self._timeframe = timeframe.upper()
        self._spread = float(spread)

    def quote(self, symbol: str) -> Quote:
        from mt5data.store import TIMEFRAME_SECONDS  # 局部导入避免环

        df = load_bars(self._root, symbol, self._timeframe)
        if df is None or len(df) == 0:
            raise DataUnavailableError(
                f"{symbol} {self._timeframe} 无 parquet 数据"
                f"（{self._root}）——离线影子行情源不可用")
        close = float(df["close"].iloc[-1])
        bar_time = df["time"].iloc[-1]
        tf_s = TIMEFRAME_SECONDS[self._timeframe]
        import pandas as pd

        close_time = pd.Timestamp(bar_time) + pd.Timedelta(seconds=tf_s)
        half = self._spread / 2.0
        return Quote(symbol=symbol, time=close_time.to_pydatetime(),
                     bid=close - half, ask=close + half, last=close, volume=0)


__all__ = [
    "PAPER_STATE_RELPATH",
    "BarQuoteMarket",
    "PaperAccountView",
    "PaperBroker",
    "PaperStateGateway",
]
