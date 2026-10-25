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

from mt5gw.models import DEAL_REASON_SL, DEAL_REASON_SO, DEAL_REASON_TP
from mt5live.commands import (
    ALERT_COMMAND_UNREADABLE,
    CommandUnreadableError,
    read_ack,
    read_flatten,
    write_ack,
)
from mt5live.gate import ORDER_LOG_FILE, LiveSizer, RiskGate, alert_reject
from mt5live.monitor import append_alert, iso_utc, naive_utc
from mt5live.reconcile import (
    INTENT_FILE_FMT,
    STATUS_CONFIRMED,
    STATUS_PENDING,
    Intent,
    write_intent,
)
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

    def deals_for_position(self, position_id: int) -> list:
        """按持仓 id 查全部成交（LiveExecutor 外部出场识别用；
        AccountService.deals_for_position 透传，含 reason 字段）。"""
        return self._account.deals_for_position(position_id)

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
# LiveExecutor：执行器一侧的全部文件契约与风控接线（计划 T24 逐条；T30 改
# expected 为"意图"语义；T31 定手数；T32 全平指令编号协议）：
# - 每轮先查 data/live/commands/flatten.json（mt5live.commands 编号协议，
#   T32/D21）：本 magic 的 ack 已对应当前指令编号 → 返回 flatten_acked，
#   不再重复平仓、不下新仓；否则平掉本 magic 全部持仓、写引用编号的
#   flatten.ack-<magic>（风控进程核对编号后归档并清理 ack）。指令文件
#   读不出 → 按"有指令"处理（照样全平）并写告警（COMMAND_UNREADABLE）；
# - 每轮开头识别外部出场（T30/C1）：上一轮已知持仓本轮消失且非执行器平仓
#   → 按 position_id 查成交，reason ∈ {SL,TP,SO} → 已解释，意图改为实际
#   净持仓（cause=external_exit:<deal>）；其他原因（人工/未知）→ 不改意图、
#   写告警，交给对账进程；
# - 开仓/反手的开仓腿先过 RiskGate.check_open（C1）；拒绝 → 不下单，
#   原因写心跳 last_error（调用方）与 alerts.log（ALERT_RISK_REJECT）；
#   平仓不受限；
# - 意图（T30/D19，data/live/expected-<magic>.json）：
#   1) 下单前写 Intent(目标, "pending", cause=decision_id)——对账进程给
#      60 秒宽限（"已决定、订单未回"的空档不误报）；
#   2) 订单返回后写 Intent(实际净持仓, "confirmed")；部分成交如实记成交量
#      并写告警（ALERT_PARTIAL_FILL），差额不再自动补足（D22 持仓期间
#      不调仓——反手/全平时了结，或人工处置）；
#   3) 被拒/跳过/无动作的轮次**绝不改写意图**——否则会把外部擅动的仓位漂移
#      悄悄"洗白"成目标状态，对账就失去意义了；
# - reentry="after_signal_change"（T25 同语义）：保护性出场后记 blocked_dir，
#   持久化 state-<magic>.json，重启恢复；信号离开该方向（0 或反向）解禁；
#   immediate 模式不设禁入；
# - 手数（T31/C2/D22）：**只在开仓时定手数，持仓期间不调仓**（与引擎
#   A/B 一致——_open 只在信号改变持仓方向时调用）。同向持仓保持净手数
#   （入场时的手数），不逐 bar 重算；反手、止损/止盈出场后再入场按**当时**
#   的 ATR 与权益重新定手数；固定 volume_lots 同样只在净仓为 0/反向时
#   生效（开仓腿手数=配置值）。加仓/减仓腿在正常轮次不会出现（目标恒为
#   净持仓）——一旦出现（如固定手数配置被人为改动后叠加外部仓位变动）
#   写告警（ALERT_UNEXPECTED_RESIZE），须人工核对；
# - 启动校验（M1）：time_stop_bars / week_close_exit 未实现，策略用到即
#   拒绝启动（validate_live_strategy，dry-run 同样校验）。
# - 本地下单日志 data/live/orders.jsonl（每笔市价单一行，RiskGate 的频率
#   滑窗数据源）。

STATE_FILE_FMT = "state-{magic}.json"
# 全平指令/确认文件（flatten.json、flatten.ack-<magic>）的读写一律经
# mt5live.commands（T32/D21 编号协议），本层不再自带路径拼装。

# 告警类别（T30/T31/T32；monitor 未定义的新类别，常量放本层）
ALERT_PARTIAL_FILL = "PARTIAL_FILL"        # 部分成交：意图=成交量，如实记录
ALERT_EXTERNAL_EXIT = "EXTERNAL_EXIT"      # 外部出场无法解释：不改意图，交给对账
ALERT_UNEXPECTED_RESIZE = "UNEXPECTED_RESIZE"   # 持仓期间出现加/减仓腿（T31/D22：
#   正常轮次不会出现——固定手数配置被人为改动、或仓位被外部改动后差分出
#   加/减腿时兜底告警，须人工核对）

# 已解释的外部出场（服务器端保护性出场/强平）：意图可改为实际净持仓
_EXPLAINED_REASONS = frozenset({DEAL_REASON_SL, DEAL_REASON_TP, DEAL_REASON_SO})
# 平仓腿的 deal.entry（mt5gw.models.Deal；"in" 是开仓腿）
_CLOSING_ENTRIES = frozenset({"out", "inout", "out_by"})


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

    action: str                     # none/flatten/flatten_acked/hold/duplicate/
                                    # open/add/reduce/reverse/close/
                                    # blocked_reentry/skipped_zero_lots/
                                    # risk_rejected
    decision_id: str | None
    net_before: float
    net_after: float
    orders: tuple[OrderOutcome, ...] = ()
    error: str | None = None        # 拒绝/跳过原因（→ 心跳 last_error + alerts）


class LiveExecutor:
    """执行器轮询体：flatten 指令 → 外部出场识别 → 风控闸门 → 差分下单
    → 意图/下单日志/状态持久化。每轮调 poll(decision, now)。"""

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
        self._state_path = os.path.join(
            live_dir, STATE_FILE_FMT.format(magic=magic))
        self._intent_path = os.path.join(
            live_dir, INTENT_FILE_FMT.format(magic=magic))
        self._order_log_path = os.path.join(live_dir, ORDER_LOG_FILE)
        self._state = self._load_state()

    # ---- 主入口 ----

    def poll(self, decision, now: datetime | None = None) -> RoundReport:
        """一轮：先消费 flatten 指令（每轮必查），再按决策差分下单。"""
        n = naive_utc(now if now is not None
                      else datetime.now(timezone.utc).replace(tzinfo=None))

        # 1) flatten 指令（T32/D21 编号协议）：读不出 → 按"有指令"处理并告警
        try:
            cmd = read_flatten(self._live_dir)
            unreadable = False
        except CommandUnreadableError as e:
            append_alert(
                self._live_dir, ALERT_COMMAND_UNREADABLE,
                f"magic {self._magic} 全平指令读不出（{e}）——按有指令处理"
                "（全平）；修复或人工清理 commands/flatten.json", n)
            cmd = None
            unreadable = True
        if cmd is not None or unreadable:
            return self._consume_flatten(cmd, unreadable=unreadable, n=n)

        # 2) 外部出场检测（T30：SL/TP/SO 已解释 → 意图=实际净持仓；
        #    人工/未知 → 不改意图，交给对账进程）
        blocked = self._sync_external_exits(n)

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

        # 5) 手数（T31/C2/D22：只在开仓时定，同向持仓保持净手数不调仓；
        #    0 手 → 跳过并记录）
        net = self._adapter.net_volume()
        target = self._target_volume(decision, net)
        if decision.signal != 0 and target == 0.0:
            self._finish(n, decision.decision_id, ())
            reason = ("skipped_zero_lots: 按风险定手数为 0（ATR 缺失或风险额"
                      "不足最小手数），跳过开仓")
            return RoundReport(
                action="skipped_zero_lots", decision_id=decision.decision_id,
                net_before=self._adapter.net_volume(),
                net_after=self._adapter.net_volume(), error=reason)
        if decision.signal != 0:
            # 目标手数回填决策：ExecutionAdapter 按 volume_lots 差分下单，
            # 必须与本层算出的目标（同向=净手数）一致，否则 adapter 会按
            # 配置手数自行差分出加/减仓腿（T31）
            decision = replace(decision, volume_lots=round(abs(target), 8))

        # 6) 差分计划 + 开仓腿过闸门（平仓腿不受限）+ 意图（pending→confirmed）
        report = self._plan_and_execute(decision, target, net, n)
        self._after_orders(decision, report, n)
        self._finish(n, decision.decision_id, report.orders)
        return report

    # ---- 各环节 ----

    def _target_volume(self, decision, net: float) -> float:
        """目标净手数（有符号；T31/C2/D22——计划伪码逐字）。

        **只在开仓时定手数，持仓期间不调仓**（与引擎 A/B 的 _open 时机
        一致，铁律第 5 条）：
        - 同向持仓 → 保持净手数（= 入场时的手数），不逐 bar 重算，也不查
          定手数器（ATR/权益变化不再触发加减仓）；
        - 净仓为 0（含止损/止盈出场后再入场）或反向 → 按当时的配置手数或
          LiveSizer（当时 ATR 与权益）重新定手数；
        - 固定 volume_lots 只在开仓腿生效（配置行为不变：开仓=配置值）。
        """
        if decision.signal == 0:
            return 0.0
        if net != 0 and (net > 0) == (decision.signal > 0):
            return net                        # 同向持仓：保持入场时的手数，不调仓
        if decision.volume_lots is not None:
            lots = float(decision.volume_lots)
        elif self._sizer is not None:
            lots = self._sizer.lots_for(decision)
        else:
            raise ValueError(
                f"决策 {decision.decision_id} 无手数且未配置定手数器"
                "（LiveSizer）——拒绝开仓")
        return decision.signal * lots if lots > 0 else 0.0

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
                        self._write_pending(0.0, decision.decision_id, n)
                        try:
                            report = self._adapter.execute(close_only)
                        except Exception:
                            self._write_confirmed(decision.decision_id, n)
                            raise
                        return _rejected(
                            verdict.reason, action="risk_rejected",
                            orders=report.orders)
                    return _rejected(verdict.reason)
        # 下单前（闸门已过）：意图 = 目标持仓（pending；订单未回，对账给宽限）
        self._write_pending(target, decision.decision_id, n)
        try:
            report = self._adapter.execute(decision)
        except Exception:
            # 订单异常返回（被拒/未知）：如实把意图记为当前实际净持仓
            # （confirmed）——重查持仓后写，未知结果也算"已回"（对账的职责
            # 才刚开始）；绝不留一个永远等不到回报的 pending。
            self._write_confirmed(decision.decision_id, n)
            raise
        return self._from_execution(report)

    def _after_orders(self, decision, report: RoundReport,
                      n: datetime) -> None:
        """订单返回后（本轮真下了单）：意图 = 实际净持仓（confirmed）；
        部分成交如实记录（意图=成交量）并写告警；出现加/减仓腿（T31 后
        正常轮次不该有）写告警。

        依据 orders 里有无开/平腿判定，而不是 action——反向被拒轮也会
        执行平仓腿（risk_rejected + close orders），意图同样要如实确认。
        无订单的轮次（hold/duplicate/被拒/跳过）绝不改写意图（防洗白）。
        """
        if not any(o.kind in ("open", "close") for o in report.orders):
            return
        self._write_confirmed(decision.decision_id, n)
        if report.action in ("add", "reduce"):
            append_alert(
                self._live_dir, ALERT_UNEXPECTED_RESIZE,
                f"magic {self._magic} {decision.decision_id} 出现 {report.action}"
                f" 腿（净 {report.net_before:+.2f} → 目标差异）——D22 持仓期间"
                "不调仓，正常轮次不应加减仓（固定手数配置被人为改动？仓位被"
                "外部改动？），须人工核对", n)
        for o in report.orders:
            if o.kind in ("open", "close") and o.partial:
                append_alert(
                    self._live_dir, ALERT_PARTIAL_FILL,
                    f"magic {self._magic} {decision.decision_id} {o.kind} "
                    f"部分成交：请求 {o.requested_volume} 手只成 "
                    f"{o.filled_volume} 手——意图按成交量登记；差额不再自动"
                    "补足（D22 持仓期间不调仓），反手/全平时了结或人工处置",
                    n)

    def _from_execution(self, report) -> RoundReport:
        return RoundReport(
            action=report.action, decision_id=report.decision_id,
            net_before=report.net_before, net_after=report.net_after,
            orders=report.orders, error=None)

    def _consume_flatten(self, cmd, *, unreadable: bool,
                         n: datetime) -> RoundReport:
        """全平指令的执行腿（T32/D21 编号协议）。

        本 magic 的 ack 已对应当前指令编号 → **不再重复平仓**（返回
        flatten_acked，不下新仓——指令归档由风控进程核对编号后完成）；
        否则市价平掉本 magic 全部持仓并写引用编号的 ack。指令读不出
        （unreadable=True，告警已在上层写）→ 按"有指令"处理：照常全平，
        ack 的 id 记空串（无法引用；该文件修复/清理前每轮重复全平告警）。
        """
        positions = self._adapter.positions()
        net_before = self._adapter.net_volume()
        cmd_id = cmd.id if cmd is not None else ""
        if not unreadable:
            ack = read_ack(self._live_dir, self._magic)
            if ack is not None and ack.id == cmd_id:
                return RoundReport(
                    action="flatten_acked", decision_id=None,
                    net_before=net_before, net_after=net_before)
        cause = f"flatten:{cmd_id}" if cmd_id else "flatten"
        self._write_pending(0.0, cause, n)    # 意图：全平在途（T30 cause 衔接）
        outcomes = self._adapter.close_all()
        write_ack(self._live_dir, self._magic, cmd_id,
                  closed=len(positions), now=n)
        self._state["blocked_dir"] = 0      # 全平后禁入状态清零（无持仓可禁）
        self._write_confirmed(cause, n)
        self._finish(n, decision_id=None, orders=outcomes)
        return RoundReport(
            action="flatten", decision_id=None, net_before=net_before,
            net_after=self._adapter.net_volume(), orders=tuple(outcomes))

    # ---- 意图（T30/D19：expected-<magic>.json）----

    def _write_pending(self, target: float, cause: str, n: datetime) -> None:
        """下单前：意图 = 目标持仓（pending）。"""
        write_intent(self._intent_path, Intent(
            symbol=self._symbol, magic=self._magic,
            side="buy" if target >= 0 else "sell", volume=abs(target),
            status=STATUS_PENDING, cause=cause, ts=n))

    def _write_confirmed(self, cause: str, n: datetime) -> None:
        """订单返回/外部出场已解释：意图 = 实际净持仓（confirmed）。"""
        net = self._adapter.net_volume()
        write_intent(self._intent_path, Intent(
            symbol=self._symbol, magic=self._magic,
            side="buy" if net >= 0 else "sell", volume=abs(net),
            status=STATUS_CONFIRMED, cause=cause, ts=n))

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

    def _sync_external_exits(self, n: datetime) -> int:
        """每轮开头：识别外部出场（非执行器平仓——执行器自己的平仓当轮
        结束即同步 known_positions，两轮之间消失的持仓必然不是执行器平的）。

        已知持仓消失 → 按 position_id 查该持仓的全部成交（MT5 的
        position= 查询，跨日也查得到）：
        - 平仓成交 reason ∈ {SL, TP, SO} → **已解释**（服务器端保护性出场/
          强平）：意图改为实际净持仓（confirmed，cause=external_exit:<deal>）；
          after_signal_change 的禁入照旧；
        - 其他原因（人工 CLIENT / 未知 / 查无成交 / 查询失败）→ **不改意图**、
          写告警（ALERT_EXTERNAL_EXIT），交给对账进程处理——改了就会把
          未经确认的漂移"洗白"。
        多张持仓同时消失时，须**全部**已解释才改意图，否则整体交给对账。
        返回当前 blocked_dir。
        """
        known = dict(self._state["known_positions"])
        current = self._current_positions()
        gone = sorted(set(known) - set(current))
        blocked = int(self._state["blocked_dir"])
        if gone:
            explained: list = []       # (ticket, deal)
            unexplained: list = []     # (ticket, 描述)
            for ticket in gone:
                try:
                    deals = self._adapter.deals_for_position(ticket)
                except Exception as exc:   # 查询失败：按未解释处理（fail-closed）
                    unexplained.append(
                        (ticket, f"成交查询失败 {type(exc).__name__}: {exc}"))
                    continue
                closing = [d for d in deals
                           if d.entry in _CLOSING_ENTRIES and d.volume > 0]
                hit = next((d for d in closing
                            if d.reason in _EXPLAINED_REASONS), None)
                if hit is not None:
                    explained.append((ticket, hit))
                elif closing:
                    reasons = ",".join(str(d.reason) for d in closing)
                    unexplained.append((ticket, f"平仓成交 reason={reasons}"))
                else:
                    unexplained.append((ticket, "查不到平仓成交"))
            if not unexplained:
                cause = "external_exit:" + ",".join(
                    str(d.ticket) for _, d in explained)
                self._write_confirmed(cause, n)
            else:
                for ticket, why in unexplained:
                    append_alert(
                        self._live_dir, ALERT_EXTERNAL_EXIT,
                        f"magic {self._magic} 持仓 {ticket} 消失且无法解释"
                        f"（{why}，须 ∈ SL/TP/SO）——不改意图，交给对账进程"
                        f"（人工核对后 scripts/live_ack_external.py）", n)
        if gone and self._rules.reentry == "after_signal_change":
            for ticket in gone:
                blocked = known[ticket]     # 非执行器平仓 → 禁同向再入（照旧）
        self._state["known_positions"] = current
        self._state["blocked_dir"] = blocked
        return blocked

    def _finish(self, n: datetime, decision_id, orders) -> None:
        """每轮收尾：known_positions 同步、状态落盘、意图首次登记、下单日志。

        意图只在**本文件尚不存在**（首次登记，让对账进程开始管理本 magic）
        时兜底写 confirmed(实际净持仓)；下单轮的 pending→confirmed 由
        _plan_and_execute/_after_orders/_consume_flatten 显式写。被拒/跳过/
        无动作的轮次绝不改写意图——目标持仓没变，改写反而会把外部漂移
        洗白（对账就失去意义了）。
        """
        self._state["known_positions"] = self._current_positions()
        if decision_id is not None:
            self._state["last_decision_id"] = decision_id
        self._save_state()
        if not os.path.exists(self._intent_path):
            self._write_confirmed(decision_id or "initial", n)
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
