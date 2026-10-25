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

import json
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from mt5live.gate import ORDER_LOG_FILE, LiveSizer, RiskGate, alert_reject
from mt5live.monitor import iso_utc, naive_utc
from mt5live.reconcile import ExpectedPosition, write_expected
from mt5live.runner import Decision
from mt5quant.strategies.base import ExitRules

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

    # T24：LiveExecutor 需要的公开视图/动作（平仓不受风控闸门限制）
    def positions(self) -> list:
        """本 magic 在本品种的全部持仓（LiveExecutor 轮询外部出场用）。"""
        return self._my_positions()

    def net_volume(self) -> float:
        """当前净头寸（有符号手数；buy 正 / sell 负）。"""
        return self._net_volume()

    def close_all(self) -> list[OrderOutcome]:
        """市价平掉本 magic 全部持仓（flatten 指令的执行腿，不过闸门）。"""
        return self._close_all(self._my_positions())

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


# ================================================================ T24 实盘接线
#
# LiveExecutor：执行器一侧的全部文件契约与风控接线（计划 T24 逐条）：
# - 每轮先查 data/live/commands/flatten.json：存在 → 平掉本 magic 全部持仓、
#   写 commands/flatten.ack-<magic>（风控进程在所有登记 magic 确认后归档）；
# - 开仓/加仓/反手的开仓腿先过 RiskGate.check_open（C1）；拒绝 → 不下单，
#   原因写心跳 last_error（调用方）与 alerts.log（ALERT_RISK_REJECT）；
#   平仓不受限；
# - 每次执行后写 expected-<magic>.json（对账进程读全部 expected-*.json）；
# - reentry="after_signal_change"（T25 同语义）：保护性出场（SL/TP/人工等
#   非执行器平仓）后记 blocked_dir，持久化 state-<magic>.json，重启恢复；
#   信号离开该方向（0 或反向）解禁；immediate 模式不设禁入；
# - 手数：decision.volume_lots 为 None 时 LiveSizer 按风险定手数（T23 同一
#   函数），0 手跳过并记录；
# - 启动校验（M1）：time_stop_bars / week_close_exit 未实现，策略用到即
#   拒绝启动（validate_live_strategy，dry-run 同样校验）。
# - 本地下单日志 data/live/orders.jsonl（每笔市价单一行，RiskGate 的频率
#   滑窗数据源）。

STATE_FILE_FMT = "state-{magic}.json"
EXPECTED_FILE_FMT = "expected-{magic}.json"
FLATTEN_ACK_FMT = "flatten.ack-{magic}"
FLATTEN_FILE = "flatten.json"
COMMANDS_SUBDIR = "commands"

# 真正执行了订单的 action（这些轮次才把"目标持仓"刷新进 expected 文件；
# 被拒/跳过/无动作的轮次绝不改写 expected——否则会把外部擅动的仓位漂移
# 悄悄"洗白"成目标状态，对账就失去意义了）
_EXECUTING_ACTIONS = frozenset({
    "open", "add", "reduce", "reverse", "close", "flatten"})


# 启动校验文案（M1：实盘未实现的出场语义，用到就拒绝启动）
_UNSUPPORTED_RULES = (
    ("time_stop_bars", "time_stop_bars 实盘未实现：策略声明了时间止损，"
     "实盘执行器不会替你平仓——拒绝启动（T24/M1）"),
    ("week_close_exit", "week_close_exit 实盘未实现：策略声明了周五收盘"
     "强平，实盘执行器不会替你平仓——拒绝启动（T24/M1）"),
)


def validate_live_strategy(strategy) -> ExitRules:
    """实盘启动校验（M1）：未实现的出场规则直接拒绝（返回策略的 ExitRules）。"""
    rules = strategy.exit_rules or ExitRules()
    for attr, message in _UNSUPPORTED_RULES:
        if getattr(rules, attr):
            raise ValueError(message)
    return rules


@dataclass(frozen=True)
class RoundReport:
    """LiveExecutor.poll 一轮的结果（action 语义见 LiveExecutor.poll）。"""

    action: str                     # none/flatten/hold/duplicate/open/add/
                                    # reduce/reverse/close/blocked_reentry/
                                    # skipped_zero_lots/risk_rejected
    decision_id: str | None
    net_before: float
    net_after: float
    orders: tuple[OrderOutcome, ...] = ()
    error: str | None = None        # 拒绝/跳过原因（→ 心跳 last_error + alerts）


class LiveExecutor:
    """执行器轮询体：flatten 指令 → 外部出场检测 → 风控闸门 → 差分下单
    → expected/下单日志/状态持久化。每轮调 poll(decision, now)。"""

    def __init__(self, adapter: ExecutionAdapter, *, symbol: str, magic: int,
                 live_dir: str, rules: ExitRules,
                 gate: RiskGate | None = None, sizer: LiveSizer | None = None):
        rules = rules or ExitRules()
        for attr, message in _UNSUPPORTED_RULES:
            if getattr(rules, attr):
                raise ValueError(message)
        self._adapter = adapter
        self._symbol = symbol
        self._magic = magic
        self._live_dir = live_dir
        self._rules = rules
        self._gate = gate
        self._sizer = sizer
        self._commands_dir = os.path.join(live_dir, COMMANDS_SUBDIR)
        self._state_path = os.path.join(
            live_dir, STATE_FILE_FMT.format(magic=magic))
        self._expected_path = os.path.join(
            live_dir, EXPECTED_FILE_FMT.format(magic=magic))
        self._order_log_path = os.path.join(live_dir, ORDER_LOG_FILE)
        self._state = self._load_state()

    # ---- 主入口 ----

    def poll(self, decision, now: datetime | None = None) -> RoundReport:
        """一轮：先消费 flatten 指令（每轮必查），再按决策差分下单。"""
        n = naive_utc(now if now is not None
                      else datetime.now(timezone.utc).replace(tzinfo=None))

        # 1) flatten 指令：平掉本 magic 全部持仓并确认（不下新仓）
        if os.path.exists(os.path.join(self._commands_dir, FLATTEN_FILE)):
            return self._consume_flatten(n)

        # 2) 外部出场检测（SL/TP/人工：上一轮已知持仓本轮消失且非我方平仓）
        blocked = self._sync_external_exits()

        if decision is not None:
            # 3) after_signal_change：信号离开禁入方向 → 解禁
            if (self._rules.reentry == "after_signal_change"
                    and blocked != 0 and decision.signal != blocked):
                blocked = 0
            self._state["blocked_dir"] = blocked
        else:
            self._state["blocked_dir"] = blocked

        if decision is None:
            self._finish(n, decision_id=None, orders=())
            return RoundReport(
                action="none", decision_id=None,
                net_before=self._adapter.net_volume(),
                net_after=self._adapter.net_volume())

        # 4) 禁入方向：不重入（immediate 模式 blocked 恒 0，不会走到这）
        if (self._rules.reentry == "after_signal_change"
                and self._state["blocked_dir"] != 0
                and decision.signal == self._state["blocked_dir"]):
            self._finish(n, decision.decision_id, ())
            return RoundReport(
                action="blocked_reentry", decision_id=decision.decision_id,
                net_before=self._adapter.net_volume(),
                net_after=self._adapter.net_volume(),
                error=None)

        # 5) 手数：配置钉死或按风险定（0 手 → 跳过并记录）
        target = self._target_volume(decision)
        if decision.signal != 0 and target == 0.0:
            self._finish(n, decision.decision_id, ())
            reason = ("skipped_zero_lots: 按风险定手数为 0（ATR 缺失或风险额"
                      "不足最小手数），跳过开仓")
            return RoundReport(
                action="skipped_zero_lots", decision_id=decision.decision_id,
                net_before=self._adapter.net_volume(),
                net_after=self._adapter.net_volume(), error=reason)
        if decision.signal != 0 and decision.volume_lots is None:
            # 定手数结果回填决策（ExecutionAdapter 按 volume_lots 差分下单）
            decision = replace(decision, volume_lots=round(abs(target), 8))

        # 6) 差分计划 + 开仓腿过闸门（平仓腿不受限）
        net = self._adapter.net_volume()
        report = self._plan_and_execute(decision, target, net, n)
        self._finish(n, decision.decision_id, report.orders,
                     update_expected=report.action in _EXECUTING_ACTIONS)
        return report

    # ---- 各环节 ----

    def _target_volume(self, decision) -> float:
        if decision.signal == 0:
            return 0.0
        if decision.volume_lots is not None:
            return float(decision.signal * decision.volume_lots)
        if self._sizer is None:
            raise ValueError(
                f"决策 {decision.decision_id} 无手数且未配置定手数器"
                "（LiveSizer）——拒绝开仓")
        lots = self._sizer.lots_for(decision)
        return float(decision.signal * lots) if lots > 0 else 0.0

    def _plan_and_execute(self, decision, target: float, net: float,
                          n: datetime) -> RoundReport:
        eps = _EPS

        def _rejected(reason: str, action: str = "risk_rejected",
                      orders=()) -> RoundReport:
            alert_reject(self._live_dir, self._magic, decision.decision_id,
                         reason, n)
            return RoundReport(
                action=action, decision_id=decision.decision_id,
                net_before=net, net_after=self._adapter.net_volume(),
                orders=tuple(orders), error=f"{action}: {reason}")

        if abs(target - net) <= eps:
            report = self._adapter.execute(decision)     # hold / duplicate
            return self._from_execution(report)

        needs_open_leg = (
            net == 0 or net * target < 0 or abs(target) > abs(net))
        if needs_open_leg:
            side = "buy" if target > 0 else "sell"
            leg_lots = (abs(target) if net * target <= 0
                        else abs(target - net))
            if self._gate is not None:
                try:
                    verdict = self._gate.check_open(
                        self._symbol, side, leg_lots, self._magic, now=n)
                except Exception as exc:   # 闸门数据故障：fail-closed 拒开仓
                    return _rejected(
                        f"risk_gate_error: {type(exc).__name__}: {exc}")
                if not verdict.allowed:
                    if net * target < 0:
                        # 反向：平仓腿照常（不受限），开仓腿被拒——先平后不开
                        close_only = replace(decision, signal=0,
                                             target_position=0,
                                             volume_lots=0.0)
                        report = self._adapter.execute(close_only)
                        return _rejected(
                            verdict.reason, action="risk_rejected",
                            orders=report.orders)
                    return _rejected(verdict.reason)
        report = self._adapter.execute(decision)
        return self._from_execution(report)

    def _from_execution(self, report) -> RoundReport:
        return RoundReport(
            action=report.action, decision_id=report.decision_id,
            net_before=report.net_before, net_after=report.net_after,
            orders=report.orders, error=None)

    def _consume_flatten(self, n: datetime) -> RoundReport:
        positions = self._adapter.positions()
        net_before = self._adapter.net_volume()
        outcomes = self._adapter.close_all()
        ack_path = os.path.join(
            self._commands_dir, FLATTEN_ACK_FMT.format(magic=self._magic))
        os.makedirs(self._commands_dir, exist_ok=True)
        with open(ack_path + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"magic": self._magic, "closed": len(positions),
                        "ts": iso_utc(n)}, f, ensure_ascii=False)
        os.replace(ack_path + ".tmp", ack_path)
        self._state["blocked_dir"] = 0      # 全平后禁入状态清零（无持仓可禁）
        self._finish(n, decision_id=None, orders=outcomes, update_expected=True)
        return RoundReport(
            action="flatten", decision_id=None, net_before=net_before,
            net_after=self._adapter.net_volume(), orders=tuple(outcomes))

    # ---- 外部出场检测与状态持久化 ----

    def _load_state(self) -> dict:
        if os.path.exists(self._state_path):
            try:
                with open(self._state_path, encoding="utf-8") as f:
                    data = json.load(f)
                return {"blocked_dir": int(data.get("blocked_dir", 0)),
                        "known_positions": {
                            int(k): int(v)
                            for k, v in data.get("known_positions",
                                                 {}).items()},
                        "last_decision_id": data.get("last_decision_id")}
            except (OSError, ValueError, TypeError):
                pass    # 状态文件损坏：从零开始（保守：无禁入、无已知持仓）
        return {"blocked_dir": 0, "known_positions": {},
                "last_decision_id": None}

    def _save_state(self) -> None:
        payload = {
            "magic": self._magic,
            "updated_at_utc": iso_utc(datetime.now(timezone.utc)),
            "blocked_dir": int(self._state["blocked_dir"]),
            "known_positions": {
                str(k): v for k, v in self._state["known_positions"].items()},
            "last_decision_id": self._state.get("last_decision_id"),
        }
        tmp = self._state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._state_path)

    def _current_positions(self) -> dict[int, int]:
        return {p.ticket: (1 if p.side == "buy" else -1)
                for p in self._adapter.positions()}

    def _sync_external_exits(self) -> int:
        """检测外部出场（SL/TP/人工）：上一轮已知持仓本轮消失。

        执行器自己的平仓发生在 poll 内部、结束时同步 known_positions，
        因此两轮之间消失的持仓必然不是执行器平的 → 保护性出场；
        after_signal_change 模式下记 blocked_dir（同向禁入），immediate
        模式只更新视图。返回当前 blocked_dir。
        """
        known = dict(self._state["known_positions"])
        current = self._current_positions()
        gone = set(known) - set(current)
        blocked = int(self._state["blocked_dir"])
        if gone and self._rules.reentry == "after_signal_change":
            for ticket in sorted(gone):
                blocked = known[ticket]     # 非执行器平仓 → 禁同向再入
        self._state["known_positions"] = current
        self._state["blocked_dir"] = blocked
        return blocked

    def _finish(self, n: datetime, decision_id, orders, *,
                update_expected: bool = False) -> None:
        """每轮收尾：known_positions 同步、状态落盘、（按需）expected、下单日志。

        expected 只在真正执行了订单（update_expected）或文件尚不存在（首次
        登记，让对账进程开始管理本 magic）时写入；被拒/跳过轮不写——目标
        持仓没变，改写反而会把外部漂移洗白（见 _EXECUTING_ACTIONS 注释）。
        """
        self._state["known_positions"] = self._current_positions()
        if decision_id is not None:
            self._state["last_decision_id"] = decision_id
        self._save_state()
        if update_expected or not os.path.exists(self._expected_path):
            net = self._adapter.net_volume()
            side = "buy" if net >= 0 else "sell"
            write_expected(self._expected_path, n, (
                ExpectedPosition(symbol=self._symbol, magic=self._magic,
                                 side=side, volume=abs(net)),))
        self._log_orders(orders, decision_id, n)

    def _log_orders(self, orders, decision_id, n: datetime) -> None:
        """本地下单日志（orders.jsonl）：每笔市价开/平一行（频率滑窗数据源）。"""
        entries = []
        for o in orders:
            if o.kind not in ("open", "close"):
                continue
            entries.append({
                "ts": iso_utc(n), "magic": self._magic,
                "symbol": self._symbol, "kind": o.kind,
                "side": o.side or "", "lots": (o.filled_volume
                                               if o.filled_volume is not None
                                               else o.requested_volume),
                "decision_id": decision_id or "-",
            })
        if not entries:
            return
        os.makedirs(self._live_dir, exist_ok=True)
        with open(self._order_log_path, "a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
