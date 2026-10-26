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

状态持久化（R6/C3：多运行器并行）：**每策略一个状态文件**
``data/live/paper/state-<magic>.json``（``paper_state_path(live_dir, magic)``）
——两个运行器（不同 magic）各写各的文件、互不整份覆盖；票据与成交序号
从 ``magic × 10^9`` 起跳的区间内单调递增，跨运行器天然不撞号。重启
（新 PaperBroker、同一路径）恢复全部记账——序号接着发，绝不与历史碰撞。
``poll_protective`` 每轮（含无成交轮）都把最新浮盈与 equity 快照时间戳
落盘，风控日亏/对账读到的是当前值而不是上一次成交时的旧值。风控进程与
对账进程用 ``--paper-state``（指向 paper 目录或单状态文件）经
PaperAccountView / PaperStateGateway **每查询重读**聚合（持仓合并、余额
求和），**不碰真实账户**。旧版单文件 ``paper/state.json`` 已停用（多写者
互相覆盖，见第六轮审核 C3）。

R7/N-C2（账户级风控统一语义）：
- **一致快照** ``PaperAccountView.snapshot()``：一次调用 glob 一次、每个
  状态文件只读一次解析一次，返回同版本的持仓/成交/账户状态（不可变
  ``PaperSnapshot``）——一次风控计算（RiskGate.check_open /
  risk_process.build_snapshot）不再把 positions() 与 deals() 两次重读之间
  落盘的同一笔亏损同时计为浮亏与已实现；跨文件快照时刻不一致时照常聚合
  但在快照上标 ``inconsistent=True``（不阻断，供日报/告警）；
- **读失败显式不可用**：状态文件**存在但读不出**（损坏/权限/结构非法）→
  该次聚合查询抛 ``DataUnavailableError``（绝不静默按空聚合——那会把该
  策略的持仓与亏损从风控口径变没），闸门经执行器 ``risk_gate_error``
  路径 fail-closed 拒单、风控主循环按轮询失败告警重试；**文件不存在**
  （策略未启动、glob 后被归档移走）不算失败。

绝不 import MetaTrader5、绝不连终端下单：影子券商的全部"成交"都是本地记账。
"""
from __future__ import annotations

import glob
import json
import os
import sys
from dataclasses import dataclass
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
from mt5live.monitor import (
    ALERT_ACCOUNT_POLL_ERROR,
    append_alert,
    iso_utc,
    naive_utc,
    parse_iso,
)

# ---- 状态文件布局（R6/C3：每策略一个文件；聚合侧按 glob 收集） ----
# paper 目录（相对 live 目录）：live_runner 落 <live>/paper/state-<magic>.json
PAPER_STATE_DIR_RELPATH = "paper"
# 聚合视图在 paper 目录下收集的状态文件（旧版单文件 state.json 不在其中）
PAPER_STATE_GLOB = "state-*.json"
# 旧版单文件布局（多写者互相覆盖，已停用；仅保留给 live_runner 检测告警）
PAPER_STATE_RELPATH = os.path.join("paper", "state.json")


def paper_state_path(live_dir: str, magic: int) -> str:
    """每策略影子状态文件：<live_dir>/paper/state-<magic>.json。

    live_runner（每进程一个策略/magic）固定用这个路径——两个运行器并行
    各写各的文件，互不覆盖（R6/C3）。
    """
    return os.path.join(live_dir, PAPER_STATE_DIR_RELPATH,
                        f"state-{int(magic)}.json")


_STATE_VERSION = 2
_EPS = 1e-9

# 起始票据：与 FakeBroker / 真实终端的常见区间都拉开距离，便于从流水一眼
# 分辨"影子成交"；重启后从状态文件接着发。R6/C3：每个 magic 的序号在
# [magic×10^9, (magic+1)×10^9) 区间内单调递增——跨运行器不撞号。
_TICKET_RANGE = 10**9
_FIRST_TICKET = 7_000_001
_FIRST_DEAL_TICKET = 8_000_001


def _first_ticket(magic: int) -> int:
    """该 magic 状态文件的起始票据（区间 [magic×10^9, (magic+1)×10^9)）。"""
    return int(magic) * _TICKET_RANGE + _FIRST_TICKET


def _first_deal_ticket(magic: int) -> int:
    """该 magic 状态文件的起始成交号（与票据同区间、不同偏移）。"""
    return int(magic) * _TICKET_RANGE + _FIRST_DEAL_TICKET


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
                 clock: Callable[[], datetime] | None = None,
                 magic: int = 0):
        """
        - market：行情服务（quote(symbol) -> Quote；影子盘=真实终端
          mt5gw.MarketData，离线干跑=BarQuoteMarket）；
        - state_path：状态文件路径（存在即恢复，balance 参数被文件覆盖；
          生产布局 = paper_state_path(live_dir, magic)——每策略一个文件）；
        - contract_sizes：symbol → 合约大小（快照 data/specs/latest.json）；
        - balance：初始余额（仅无状态文件时生效）；
        - clock：成交时刻来源（缺省墙钟 UTC；测试注入模拟钟）；
        - magic：本策略 magic（R6/C3）——票据/成交序号在 magic×10^9 起跳
          的区间内发号（跨运行器不撞号；缺省 0 = 旧 7000001 区间，兼容
          单文件测试）。按单开仓的 magic 参数仍逐单标记持仓/成交。
        """
        self._market = market
        self._state_path = state_path
        self._contract_sizes = dict(contract_sizes)
        self._clock = clock or _now_utc
        self._magic = int(magic)
        self.balance = float(balance)
        self._positions: list[Position] = []
        self._deals: list[Deal] = []
        self._next_ticket = _first_ticket(self._magic)
        self._next_deal_ticket = _first_deal_ticket(self._magic)
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

        R6/C3：**无论本轮有没有成交，结束时都把最新浮盈与 equity 快照
        时间戳落盘**（save(now=n)）——风控日亏/对账读到的是当前浮亏，
        而不是上一次成交时的旧值。
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
        self.save(now=n)   # 无成交轮也发布浮盈快照（equity_ts_utc = 本轮时刻）
        return closed

    # ---- 状态持久化（fileio 原子写；重启恢复） ----

    def save(self, now: datetime | None = None) -> None:
        """状态落盘：持仓（含最新浮动感）、余额、全部流水、票据序号、
        equity 快照时间戳。

        - now：浮盈/equity 的估值时刻（poll_protective 传本轮轮询时刻）；
          缺省墙钟。文件里 updated_at_utc 恒为落盘墙钟。
        """
        positions = [self._refresh(p) for p in self._positions]
        snap_ts = naive_utc(now) if now is not None else _now_utc()
        payload = {
            "version": _STATE_VERSION,
            "paper": True,
            "magic": self._magic,
            "updated_at_utc": iso_utc(_now_utc()),
            "equity_ts_utc": iso_utc(snap_ts),
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
        self._next_ticket = int(data.get("next_ticket",
                                         _first_ticket(self._magic)))
        self._next_deal_ticket = int(data.get("next_deal_ticket",
                                              _first_deal_ticket(self._magic)))
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
# 只读聚合账户视图：risk_process / live_reconcile 用状态文件替代真实账户。
# R6/C3：持 paper 目录（或单状态文件），**每次查询都重读盘**——常驻风控/
# 对账循环每轮自动看到各运行器的最新成交与浮盈，不再构造时快照一次。
# R7/N-C2：查询内部经 snapshot()（glob 一次、每文件只读一次解析一次）；
# 存在但读不出的状态文件显式 DataUnavailableError（绝不静默按空聚合）。

@dataclass(frozen=True)
class PaperSnapshot:
    """一次 ``PaperAccountView.snapshot()`` 的不可变聚合快照（R7/N-C2）。

    - positions / deals：全部状态文件合并（票号区间按 magic 派生不撞，
      positions 按 ticket、deals 按 (time, ticket) 排序）——**同版本**：
      每个文件只读一次解析一次，单文件内持仓与成交必然一致（同一笔
      亏损不会既在持仓里计浮亏、又在流水里计已实现）；
    - state：聚合账户状态（余额/权益 = 各文件求和）；
    - file_ts：各状态文件 (basename, 快照时刻)——时刻取 equity_ts_utc
      （缺则 updated_at_utc；都缺/解析失败 → None）；
    - inconsistent：跨文件快照时刻不一致（或部分文件缺时刻）→ True，
      **照常聚合不阻断**——只供日报/告警区分"某策略落盘滞后"，一致性
      结论以单文件内持仓/成交同版本为准。
    """

    positions: tuple[Position, ...]
    deals: tuple[Deal, ...]
    state: AccountState
    file_ts: tuple[tuple[str, datetime | None], ...]
    inconsistent: bool


class PaperAccountView:
    """从 paper 状态文件构造的只读聚合账户视图（不连终端、不下单）。

    - state_path 为 **paper 目录**：聚合目录下全部 ``state-<magic>.json``
      （每策略一个运行器各写各的；旧版单文件 state.json **不**聚合——
      多写者布局已停用）；
    - state_path 为 **单个状态文件**：单策略兼容（只读该文件）。

    **每次查询（snapshot/state/positions/deals/deals_for_position）都重新
    读盘**：另一"进程"（运行器）落盘的新仓/平仓/浮盈，下一次查询即可见；
    每次查询内部是一次 snapshot()——glob 一次、每个文件只读一次解析一次，
    单次查询内的持仓与成交同版本（R7/N-C2 一致快照）。

    读失败分级（R7/N-C2，撤销 R6"按空处理"）：
    - 状态文件**存在但读不出**（损坏/权限/结构非法）→ 该次查询抛
      ``DataUnavailableError``（告警照写：stderr + alerts.log）——该策略
      的持仓与亏损绝不静默从风控口径消失；闸门/风控主循环 fail-closed；
    - **文件不存在**（策略未启动；glob 与 open 之间被归档移走）→ 不算
      失败，视同该策略无状态。

    聚合语义：持仓/成交合并（票号区间按 magic 派生，天然不撞）、余额与
    权益求和；``latest_update_utc()`` 取各文件最新快照时刻。
    """

    def __init__(self, state_path: str, *, alert_dir: str | None = None):
        self._state_path = state_path
        self._alert_dir = alert_dir
        if not self._state_files():
            raise DataUnavailableError(
                f"影子状态不可用：{state_path} 下没有 state-*.json（每策略"
                "运行器写 paper/state-<magic>.json；旧版单文件 state.json "
                "已停用不聚合）——先以 --paper 启动运行器；单策略也可直接"
                "传状态文件路径")
        if (os.path.isfile(state_path)
                and os.path.basename(state_path) == "state.json"):
            # 旧版单文件布局：运行器已改写 state-<magic>.json，这份可能是
            # 升级前的陈旧快照——提示操作者，但仍如实读它（单文件本就合法）
            print(f"⚠ 影子状态文件 {state_path} 是旧版单文件布局——运行器"
                  "现写 paper/state-<magic>.json；如非有意指定，请改用 "
                  "--paper-state 指向 paper 目录", file=sys.stderr, flush=True)

    # ---- 文件发现与一致快照（R7/N-C2） ----

    def _state_files(self) -> list[str]:
        """当前可见的状态文件（目录模式每调用重新 glob——新策略的状态
        文件出现后无需重建视图）。"""
        if os.path.isdir(self._state_path):
            return sorted(glob.glob(
                os.path.join(self._state_path, PAPER_STATE_GLOB)))
        if os.path.isfile(self._state_path):
            return [self._state_path]
        return []

    def _read_state_file(self, path: str) -> dict | None:
        """读单个状态文件并校验结构（R7/N-C2 读失败分级）。

        - 存在但读不出（OSError/坏 JSON/缺 balance）→ 告警后抛
          DataUnavailableError：该策略的风控口径（持仓/成交/盈亏）不可
          用，本次聚合查询整体拒判（fail-closed），绝不按空聚合；
        - glob 后文件消失（FileNotFoundError：策略归档/清理瞬间）→
          None（视同该策略未启动，不算失败）。
        """
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or "balance" not in data:
                raise ValueError("顶层须为带 balance 的对象")
        except FileNotFoundError:
            return None                     # glob 后被移走：视同不存在
        except (OSError, ValueError) as e:
            self._warn_unreadable(path, e)
            raise DataUnavailableError(
                f"影子状态文件 {path} 读取失败（{e}）——该策略的持仓/成交/"
                "盈亏不可用，本次聚合查询拒绝（fail-closed：绝不静默按空"
                "聚合把亏损从风控口径变没）；人工修复或删除该文件后自动"
                "恢复") from e
        return data

    def snapshot(self) -> PaperSnapshot:
        """一致快照（R7/N-C2）：一次调用 glob 一次、每个状态文件只读
        一次解析一次，返回同版本的持仓/成交/账户状态。

        跨文件快照时刻（equity_ts_utc）不一致 → ``inconsistent=True``
        照常聚合（不阻断；单文件内持仓与成交仍必然同版本）。风控闸门
        （RiskGate.check_open）与风控主循环（risk_process.build_snapshot）
        用**单次** snapshot 组装一次风控计算的全部账户数据——两次独立
        查询（positions() 后 deals()）之间落盘的平仓不会再被双计。
        """
        entries: list[tuple[str, dict]] = []
        for path in self._state_files():
            data = self._read_state_file(path)
            if data is None:
                continue
            entries.append((os.path.basename(path), data))
        if not entries:
            raise DataUnavailableError(
                f"影子状态不可用：{self._state_path} 下构造时可见的 "
                "state-*.json 此刻全部不存在——账户整体消失按数据不可用"
                "处理（fail-closed），绝不静默当空账户聚合")
        positions = sorted(
            (p for _, d in entries
             for p in (_position_from_dict(x) for x in d.get("positions", []))),
            key=lambda p: p.ticket)
        deals = sorted(
            (d for _, doc in entries
             for d in (_deal_from_dict(x) for x in doc.get("deals", []))),
            key=lambda d: (d.time, d.ticket))
        balance = sum(float(d.get("balance", 0.0)) for _, d in entries)
        equity = sum(float(d.get("equity", d.get("balance", 0.0)))
                     for _, d in entries)
        file_ts = tuple((name, _doc_snapshot_ts(d)) for name, d in entries)
        ts_values = [t for _, t in file_ts if t is not None]
        inconsistent = (len(set(ts_values)) > 1
                        or 0 < len(ts_values) < len(file_ts))
        return PaperSnapshot(
            positions=tuple(positions), deals=tuple(deals),
            state=AccountState(
                login=0, server="paper-shadow", company="PaperBroker (T34)",
                currency="USD", leverage=100, balance=balance, equity=equity,
                margin=0.0, margin_free=equity, margin_mode=2, trade_mode=0),
            file_ts=file_ts, inconsistent=inconsistent)

    def _warn_unreadable(self, path: str, err: Exception) -> None:
        msg = (f"影子状态文件 {path} 读取失败（数据不可用，本次查询拒绝——"
               f"fail-closed）：{err}")
        print(f"⚠ {msg}", file=sys.stderr, flush=True)
        if self._alert_dir:
            try:
                append_alert(self._alert_dir, ALERT_ACCOUNT_POLL_ERROR, msg)
            except OSError:
                pass          # 告警通道不可用：不让告警打崩溃读取方

    # ---- AccountService 子集（聚合；每次查询一次一致快照） ----

    def state(self) -> AccountState:
        return self.snapshot().state

    def positions(self, symbol: str | None = None) -> list[Position]:
        snap = self.snapshot()
        return [p for p in snap.positions
                if symbol is None or p.symbol == symbol]

    def pending_orders(self, symbol: str | None = None) -> list:
        return []

    def deals(self, since: datetime, to: datetime) -> list[Deal]:
        lo, hi = naive_utc(since), naive_utc(to)
        return [d for d in self.snapshot().deals if lo <= d.time <= hi]

    def deals_for_position(self, position_id: int) -> list[Deal]:
        return [d for d in self.snapshot().deals
                if d.position_id == position_id]

    def latest_update_utc(self) -> datetime | None:
        """全部状态文件里最新的快照时刻（equity_ts_utc 优先，缺则
        updated_at_utc；无任何可读文件 → None）。"""
        ts_list = [t for _, t in self.snapshot().file_ts if t is not None]
        return max(ts_list) if ts_list else None


def _doc_snapshot_ts(doc: dict) -> datetime | None:
    """状态文件的快照时刻：equity_ts_utc 优先，缺则 updated_at_utc；
    都缺/解析失败 → None（快照上据此标 inconsistent）。"""
    for key in ("equity_ts_utc", "updated_at_utc"):
        raw = doc.get(key)
        if not raw:
            continue
        try:
            return parse_iso(str(raw))
        except (ValueError, TypeError):
            continue
    return None


class PaperStateGateway:
    """给 scripts 用的最小网关替身：account 指向 PaperAccountView。

    risk_process / live_reconcile 的 ``--paper-state`` 装配：不连真实
    MT5 终端，只读影子券商状态（目录 = 聚合全部 state-<magic>.json）。
    shutdown 无资源可释放。alert_dir 给了则把读失败告警落 alerts.log。
    """

    def __init__(self, state_path: str, *, alert_dir: str | None = None):
        self.account = PaperAccountView(state_path, alert_dir=alert_dir)

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
    "PAPER_STATE_DIR_RELPATH",
    "PAPER_STATE_GLOB",
    "PAPER_STATE_RELPATH",
    "BarQuoteMarket",
    "PaperAccountView",
    "PaperBroker",
    "PaperSnapshot",
    "PaperStateGateway",
    "paper_state_path",
]
