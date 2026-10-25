"""执行适配器：把目标仓位差分成 MT5 订单，全部经 mt5gw TradingService。

设计（T14 / blueprint §10"实盘"）：
- 目标仓位语义：以 (symbol, magic) 过滤持仓与挂单算净头寸（buy 为正），
  与 Decision.target_position × volume_lots 差分：
    无仓→市价开（附 SL/TP）；同向→加/减仓；反向→先平后开；归零→全平；
    已达目标→不动。差分本身幂等（重复执行同一目标不会再下单）。
- SL/TP 口径与 engine_a 相同：**实际成交价 ± k × ATR（信号 bar）**。
  市价单先随单提交按报价参考价（买 Ask / 卖 Bid）算的服务器端止损，
  成交后若实际成交价偏离参考价，立即 modify_position 修正为成交价口径
  ——持仓自第一毫秒起就有止损，最终价位与引擎逐字同式。
- 幂等：成功执行的 decision_id 记入进程内去重集，同 bar 重复触发直接
  返回 duplicate；被拒/未知的决策不标记（目标仓位语义下个触发点自愈）。
  跨重启的去重属于对账进程（mt5live.reconcile，T14b）职责。
- retcode 分类复用 mt5gw 异常：TradeRejectedError / OrderResultUnknownError /
  LiveTradingBlockedError 原样上抛，执行器层**零重试**（Requote 的有界
  重试在网关内部；Unknown 必须先对账再决定，绝不盲目重发）。
- 部分成交（DONE_PARTIAL）如实回报（filled_volume < requested_volume），
  不追单——差额留给下个决策点或人工/风控处置。
- 绝不 import MetaTrader5 直连；实盘保险丝语义（mt5gw.config）不在本层。
"""
from __future__ import annotations

from dataclasses import dataclass

from mt5live.runner import Decision

_EPS = 1e-9


@dataclass(frozen=True)
class OrderOutcome:
    """单笔请求的结果（含部分成交的如实拆分）。"""

    kind: str                      # open/add/close/cancel/modify/modify_skipped
    ticket: int | None = None
    side: str | None = None
    requested_volume: float | None = None
    filled_volume: float | None = None
    price: float | None = None     # 成交价（close 为出场价；cancel/modify 无）
    partial: bool = False
    retcode: int | None = None
    comment: str = ""


@dataclass(frozen=True)
class ExecutionReport:
    """一次 execute 的完整回报（对账/监控进程 T14b 的输入之一）。"""

    decision_id: str
    action: str                    # duplicate/hold/open/add/reduce/reverse/close
    net_before: float              # 执行前净头寸（有符号手数）
    net_after: float               # 执行后按持仓重查的净头寸
    orders: tuple[OrderOutcome, ...] = ()


class ExecutionAdapter:
    def __init__(self, trading, account, market, *, symbol: str, magic: int,
                 deviation: int = 20, sltp_tolerance: float = 0.0):
        """
        - trading / account / market：mt5gw 的 TradingService / AccountService /
          MarketData（测试注入同签名替身）；
        - sltp_tolerance：成交价与参考价差异超过该值（价格单位）才补
          modify_position 修正 SL/TP；0.0 = 任何差异都修正。
        """
        self._trading = trading
        self._account = account
        self._market = market
        self._symbol = symbol
        self._magic = magic
        self._deviation = deviation
        self._tolerance = sltp_tolerance
        self._seen: set[str] = set()

    # ---- 对外 ----

    def execute(self, decision: Decision) -> ExecutionReport:
        """按目标仓位差分下单；异常原样上抛（本层不吞、不重试）。"""
        if decision.decision_id in self._seen:
            net_now = self._net_volume()
            return ExecutionReport(decision_id=decision.decision_id,
                                   action="duplicate",
                                   net_before=net_now, net_after=net_now)
        if decision.signal != 0 and decision.volume_lots is None:
            raise ValueError(
                f"决策 {decision.decision_id} 信号非零但 volume_lots=None"
                "（风控进程未裁决手数）——拒开仓；平仓类操作不受影响")

        outcomes: list[OrderOutcome] = []
        outcomes.extend(self._cancel_stale_pendings())

        positions = self._my_positions()
        net = self._signed_volume(positions)
        target = decision.signal * (decision.volume_lots or 0.0)

        if abs(target - net) <= _EPS:
            action = "hold"
        elif net * target < 0:                       # 反向：先平后开（target 带符号定方向）
            action = "reverse"
            outcomes.extend(self._close_all(positions))
            outcomes.extend(self._open(decision, target))
        elif net == 0:
            action = "open"
            outcomes.extend(self._open(decision, target))
        elif target == 0:
            action = "close"
            outcomes.extend(self._close_all(positions))
        elif abs(target) > abs(net):                 # 同向加仓
            action = "add"
            outcomes.extend(self._open(decision, target - net))
        else:                                        # 同向减仓
            action = "reduce"
            outcomes.extend(self._close_volume(positions, net - target
                                               if net > 0 else target - net))

        self._seen.add(decision.decision_id)
        return ExecutionReport(decision_id=decision.decision_id, action=action,
                               net_before=net, net_after=self._net_volume(),
                               orders=tuple(outcomes))

    # ---- 头寸视图 ----

    def _my_positions(self) -> list:
        return [p for p in self._account.positions(self._symbol)
                if p.magic == self._magic]

    def _signed_volume(self, positions) -> float:
        total = 0.0
        for p in positions:
            total += p.volume if p.side == "buy" else -p.volume
        return round(total, 8)

    def _net_volume(self) -> float:
        return self._signed_volume(self._my_positions())

    def _cancel_stale_pendings(self) -> list[OrderOutcome]:
        """目标仓位语义只走市价单：本 magic 在本品种的遗留挂单一律撤销。"""
        outcomes = []
        for order in self._account.pending_orders(self._symbol):
            if order.magic != self._magic:
                continue
            res = self._trading.cancel_order(order.ticket)
            outcomes.append(OrderOutcome(kind="cancel", ticket=order.ticket,
                                         retcode=res.retcode,
                                         comment=res.comment))
        return outcomes

    # ---- 开仓 / 加仓 ----

    def _open(self, decision: Decision, volume: float) -> list[OrderOutcome]:
        volume = round(volume, 8)
        if abs(volume) <= _EPS:              # volume 带符号：正=buy、负=sell
            return []
        side = "buy" if volume > 0 else "sell"
        lots = abs(volume)
        ref = self._ref_price(side)
        sl = tp = None
        if ref is not None and decision.atr is not None:
            if decision.sl_atr is not None:
                sl = (ref - decision.sl_atr * decision.atr if side == "buy"
                      else ref + decision.sl_atr * decision.atr)
            if decision.tp_atr is not None:
                tp = (ref + decision.tp_atr * decision.atr if side == "buy"
                      else ref - decision.tp_atr * decision.atr)
        before = {p.ticket for p in self._my_positions()}
        res = self._trading.market_order(
            self._symbol, side, lots, sl=sl, tp=tp,
            deviation=self._deviation, comment=decision.decision_id,
            magic=self._magic)
        outcome = OrderOutcome(
            kind="open", ticket=res.order_ticket, side=side,
            requested_volume=res.requested_volume or lots,
            filled_volume=res.volume, price=res.deal_price,
            partial=res.partial, retcode=res.retcode, comment=res.comment)
        outcomes = [outcome]
        outcomes.extend(self._align_sltp(decision, side, before, res, ref))
        return outcomes

    def _align_sltp(self, decision: Decision, side: str, before: set,
                    result, ref: float | None) -> list[OrderOutcome]:
        """把 SL/TP 修正为"实际成交价 ± k×ATR"（引擎同口径）。

        找单对象：开仓后重查持仓，新 ticket 优先；净额账户加仓可能复用
        原 ticket，退回唯一持仓；都定位不到 → 记 modify_skipped（如实，
        不静默）。无成交价 / 无 ATR / 无 k / 价差在容差内 → 不动。
        """
        if (result.deal_price is None or decision.atr is None
                or (decision.sl_atr is None and decision.tp_atr is None)):
            return []
        if ref is not None and abs(result.deal_price - ref) <= self._tolerance:
            return []
        price = result.deal_price
        sl = tp = None
        if decision.sl_atr is not None:
            sl = (price - decision.sl_atr * decision.atr if side == "buy"
                  else price + decision.sl_atr * decision.atr)
        if decision.tp_atr is not None:
            tp = (price + decision.tp_atr * decision.atr if side == "buy"
                  else price - decision.tp_atr * decision.atr)
        if sl is None and tp is None:
            return []
        after = self._my_positions()
        fresh = [p for p in after if p.ticket not in before]
        target = fresh[0] if fresh else (after[0] if len(after) == 1 else None)
        if target is None:
            return [OrderOutcome(
                kind="modify_skipped", comment=(
                    f"无法定位成交后的持仓以修正 SL/TP（成交价 {price}，"
                    f"应设 sl={sl} tp={tp}）——需人工/对账核对"))]
        res = self._trading.modify_position(target.ticket, sl=sl, tp=tp)
        return [OrderOutcome(kind="modify", ticket=target.ticket,
                             retcode=res.retcode, comment=res.comment)]

    def _ref_price(self, side: str) -> float | None:
        """下单参考价：买用 Ask、卖用 Bid（无行情源时返回 None）。"""
        if self._market is None:
            return None
        try:
            quote = self._market.quote(self._symbol)
        except Exception:   # 行情暂不可用：不带 SL 提交，成交后仍会按成交价补设
            return None
        return quote.ask if side == "buy" else quote.bid

    # ---- 平仓 ----

    def _close_all(self, positions) -> list[OrderOutcome]:
        return self._close_tickets([(p, p.volume) for p in positions])

    def _close_volume(self, positions, volume: float) -> list[OrderOutcome]:
        """按 ticket 顺序平掉指定量（跨多张残仓票据）。"""
        plan, remaining = [], round(volume, 8)
        for p in positions:
            if remaining <= _EPS:
                break
            vol = min(p.volume, remaining)
            plan.append((p, vol))
            remaining = round(remaining - vol, 8)
        if remaining > _EPS:
            raise RuntimeError(
                f"持仓不足以平掉目标差额 {volume}（期间被外部改动？）——"
                "如实中断，等待下个决策点或对账")
        return self._close_tickets(plan)

    def _close_tickets(self, plan) -> list[OrderOutcome]:
        outcomes = []
        for pos, vol in plan:
            res = self._trading.close_position(
                pos.ticket, vol, deviation=self._deviation, magic=self._magic)
            outcomes.append(OrderOutcome(
                kind="close", ticket=pos.ticket,
                side="sell" if pos.side == "buy" else "buy",
                requested_volume=vol, filled_volume=res.volume,
                price=res.deal_price, partial=res.partial,
                retcode=res.retcode, comment=res.comment))
        return outcomes
