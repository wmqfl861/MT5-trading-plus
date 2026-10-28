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
from mt5gw.trading import sanitize_comment
from mt5live.commands import (
    ALERT_COMMAND_UNREADABLE,
    CommandUnreadableError,
    read_ack,
    read_flatten,
    write_ack,
)
from mt5live.fileio import append_jsonl, write_json_atomic
from mt5live.gate import ORDER_LOG_MAGIC_FMT, LiveSizer, RiskGate, alert_reject
from mt5live.monitor import append_alert, iso_utc, naive_utc, parse_iso
from mt5live.reconcile import (
    DEFAULT_VOLUME_TOLERANCE,
    INTENT_FILE_FMT,
    STATUS_CONFIRMED,
    STATUS_PENDING,
    Intent,
    write_intent,
)
from mt5live.runner import Decision
from mt5quant.strategies.base import ExitRules

_EPS = 1e-9


def decision_comment(decision_id: str) -> str:
    """decision_id → MT5 安全订单 comment（压缩形态，仅辅助定位）。

    canonical decision_id = "{symbol}|{TF}|{bar_close_iso}"（runner 与风控
    进程的接口约定）。'|' 与 ISO 冒号触发 MT5 拒单（F 阶段首战事故，
    last_error=(-2, 'Invalid "comment" argument')），压缩为
    "{symbol}_{TF}_{MMDD}_{HHMM}"（白名单字符、≤31）。非 canonical 形态
    退回 sanitize_comment 兜底安全化。完整 decision_id 恒在本地留痕
    （expected-<magic>.json 的 cause 与 orders-<magic>.jsonl 行），
    注释不承担还原职责。
    """
    parts = decision_id.split("|")
    if len(parts) == 3:
        symbol, timeframe, iso = parts
        try:
            close_time = datetime.fromisoformat(iso)
            stamp = f"{close_time:%m%d}_{close_time:%H%M}"
        except ValueError:                       # 非 ISO（测试简写等）→ 原样安全化
            stamp = iso
        return sanitize_comment(f"{symbol}_{timeframe}_{stamp}")
    return sanitize_comment(decision_id)


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

    def pending_orders(self) -> list:
        """本 magic 在本品种的全部挂单（LiveExecutor 全平完成判据用）。"""
        return [o for o in self._account.pending_orders(self._symbol)
                if o.magic == self._magic]

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

    def flatten_all(self) -> list[OrderOutcome]:
        """全平指令的完整执行腿（C1/R6）：撤本 magic 全部挂单 + 市价平掉
        全部持仓。**完成与否不凭本返回值判定**——由 LiveExecutor 重查
        持仓与挂单均清零后写完成 ack（部分成交残仓 → 下一轮继续）。"""
        outcomes = self._cancel_stale_pendings()
        outcomes.extend(self._close_all(self._my_positions()))
        return outcomes

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
            deviation=self._deviation,
            comment=decision_comment(decision.decision_id),  # 安全压缩（F 停修）
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
# expected 为"意图"语义；T31 定手数；T32 全平指令编号协议；R6 补 C1/C2）：
# - 每轮先查 data/live/commands/flatten.json（mt5live.commands 编号协议，
#   T32/D21）：本 magic 的 ack 已对应当前指令编号 → 返回 flatten_acked，
#   不再重复平仓、不下新仓；否则撤掉本 magic 全部挂单、市价平掉全部持仓、
#   **重查确认持仓与挂单均清零**才写引用编号的 flatten.ack-<magic> 并把
#   意图改 confirmed(0)（C1/R6：部分成交残仓 → 不写 ack、意图如实=残仓、
#   写告警，下一轮继续消费同一条指令直到清零；归档因此天然等价"全员真
#   完成"）；指令文件读不出 → 按"有指令"处理（照样全平）并写告警
#   （COMMAND_UNREADABLE）；
# - 每轮开头识别外部出场（T30/C1；C2/R6 状态机化）：上一轮已知持仓本轮
#   消失且非执行器平仓 → 按 position_id 查成交，reason ∈ {SL,TP,SO} →
#   已解释，意图改为实际净持仓（cause=external_exit:<deal>）；其他原因
#   （人工/未知/查无成交/查询失败）→ **持久化为 state-<magic>.json 的
#   pending_external_exits**（ticket+最后已知量+时间），每轮开头重查这些
#   ticket 的成交历史（历史延迟可见时持续重试，事件不丢失），不改意图、
#   写告警（ALERT_EXTERNAL_EXIT），交给对账进程；
# - 存在未解释外部出场（pending 非空）时**本 magic 停止开新仓**（平仓
#   照常；action=blocked_external_exit），直到解释成功或 live_ack_external
#   人工确认——这是闸门 read_pause 之外的第二道防线（C2/R6：不依赖对账
#   进程稍后写 pause，先于对账运行也生效）。**N-C1/R7 起确认必须绑定
#   事件**：live_ack_external 在意图文件附 confirmed_events（被确认的
#   ticket 集合+确认时刻+确认时实际净持仓快照），执行器只清空绑定覆盖
#   且确认后世界未变的事件——裸 cause=ack_external 不再永久放行后续
#   新出场（旧确认放行新事件即原缺陷）；
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
# - 本地下单日志 data/live/orders-<magic>.jsonl（每笔市价单一行；T33/M8
#   起每策略各写各的、经 fileio.append_jsonl 追加——RiskGate 读全部
#   orders-*.jsonl 作为频率滑窗数据源，兼容旧 orders.jsonl）。
# - 成交证据流 data/live/deals-<magic>.jsonl（R10/T58 契约 C-2，同目录）：
#   主动成交（fill_open/fill_close）与外部/保护性出场解释
#   （external_exit，reason∈SL/TP/SO）各一行——price=成交价、
#   decision_bar=当轮决策 bar（外部出场=检测当轮 bar 或 null）；写入
#   失败记告警不阻断交易（fail-open 仅限此证据流）。

STATE_FILE_FMT = "state-{magic}.json"
# 成交证据流（R10/T58 契约 C-2）：deals-<magic>.jsonl 与 orders-<magic>.jsonl
# 同目录并存——orders 仍是**请求**日志（风控频率滑窗数据源），deals 记
# **成交**证据（主动成交 fill_open/fill_close + 外部/保护性出场解释
# external_exit，reason/price 如实），供验收（demo_check）读取。
DEALS_LOG_FMT = "deals-{magic}.jsonl"
# 全平指令/确认文件（flatten.json、flatten.ack-<magic>）的读写一律经
# mt5live.commands（T32/D21 编号协议），本层不再自带路径拼装。

# 告警类别（T30/T31/T32；monitor 未定义的新类别，常量放本层）
ALERT_PARTIAL_FILL = "PARTIAL_FILL"        # 部分成交：意图=成交量，如实记录
ALERT_EXTERNAL_EXIT = "EXTERNAL_EXIT"      # 外部出场无法解释：不改意图，交给对账
ALERT_UNEXPECTED_RESIZE = "UNEXPECTED_RESIZE"   # 持仓期间出现加/减仓腿（T31/D22：
#   正常轮次不会出现——固定手数配置被人为改动、或仓位被外部改动后差分出
#   加/减腿时兜底告警，须人工核对）
ALERT_FLATTEN_RESIDUAL = "FLATTEN_RESIDUAL"     # C1/R6：全平部分成交残仓——
#   不写完成 ack，下一轮继续平仓直到清零，归档在此之前不发生
ALERT_DEALS_LOG_ERROR = "DEALS_LOG_ERROR"       # R10/T58（C-2）：deals 证据
#   写入失败——fail-open 记告警不阻断交易（唯一允许 fail-open 的证据流）

# 已解释的外部出场（服务器端保护性出场/强平）：意图可改为实际净持仓
_EXPLAINED_REASONS = frozenset({DEAL_REASON_SL, DEAL_REASON_TP, DEAL_REASON_SO})
# deal.reason 原值 → deals 证据行的 reason 字符串（契约 C-2："SL|TP|SO|null"）
_DEAL_REASON_NAMES = {DEAL_REASON_SL: "SL", DEAL_REASON_TP: "TP",
                      DEAL_REASON_SO: "SO"}
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
                                    # blocked_reentry/blocked_external_exit/
                                    # skipped_zero_lots/risk_rejected
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
        self._order_log_path = os.path.join(
            live_dir, ORDER_LOG_MAGIC_FMT.format(magic=magic))
        self._deals_log_path = os.path.join(
            live_dir, DEALS_LOG_FMT.format(magic=magic))
        self._decision_bar: str | None = None   # 本轮决策 bar（deals 行用）
        self._state = self._load_state()

    # ---- 主入口 ----

    def poll(self, decision, now: datetime | None = None) -> RoundReport:
        """一轮：先消费 flatten 指令（每轮必查），再按决策差分下单。"""
        n = naive_utc(now if now is not None
                      else datetime.now(timezone.utc).replace(tzinfo=None))
        # R10/T58（C-2）：deals 证据行的 decision_bar——当轮决策的信号 bar
        # 收盘时刻（与 decision_id 后缀同源）；无决策轮（休市轮询/flatten）
        # 为 None（external_exit 语义："检测当轮 bar 或 null"）
        self._decision_bar = (iso_utc(decision.bar_close_time)
                              if decision is not None else None)

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
        #    人工/未知 → 持久化为未决出场，交给对账进程；C2/R6）
        blocked = self._sync_external_exits(n)

        # 2b) 人工确认（C2/R6 引入；N-C1/R7 改为**事件绑定**消费）：
        #     live_ack_external 落盘的确认附带 confirmed_events（被确认的
        #     ticket 集合+确认时刻+确认时实际净持仓快照）——只有绑定
        #     覆盖的未决出场才被清空；确认之后新出现的消失回到 pending，
        #     不得被旧确认放行（裸 cause=ack_external 永久有效是原缺陷）
        if self._state["pending_external_exits"]:
            self._consume_external_ack()

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
            # C2/R6 第二道防线：存在未解释的外部出场（pending 非空）→
            # 本 magic 停止开新仓（平仓照常），直到成交历史解释为
            # SL/TP/SO 或 live_ack_external 人工确认——不依赖对账进程
            # 稍后写 pause（先于对账运行也生效）。
            pending_exits = self._state.get("pending_external_exits") or {}
            if pending_exits:
                reason = ("external_exit_pending: 存在未解释的外部出场"
                          f"（tickets {sorted(pending_exits)}）——本 magic "
                          "暂停开新仓（平仓不受影响），直到解释为 SL/TP/SO"
                          " 或 scripts/live_ack_external.py 人工确认")
                append_alert(
                    self._live_dir, ALERT_EXTERNAL_EXIT,
                    f"magic {self._magic} {decision.decision_id}：{reason}", n)
                if net * target < 0:
                    # 反向：平仓腿照常（不受限），开仓腿被挡——先平后不开
                    close_only = replace(decision, signal=0,
                                         target_position=0,
                                         volume_lots=0.0)
                    self._write_pending(0.0, decision.decision_id, n)
                    try:
                        report = self._adapter.execute(close_only)
                    except Exception:
                        self._write_confirmed(decision.decision_id, n)
                        raise
                    return RoundReport(
                        action="blocked_external_exit",
                        decision_id=decision.decision_id, net_before=net,
                        net_after=self._adapter.net_volume(),
                        orders=tuple(report.orders), error=reason)
                return RoundReport(
                    action="blocked_external_exit",
                    decision_id=decision.decision_id, net_before=net,
                    net_after=self._adapter.net_volume(), error=reason)
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
        """全平指令的执行腿（T32/D21 编号协议；C1/R6 完成判据强化）。

        本 magic 的 ack 已对应当前指令编号 → **不再重复平仓**（返回
        flatten_acked，不下新仓——指令归档由风控进程核对编号后完成）；
        否则撤掉本 magic 全部挂单、市价平掉全部持仓，**重查确认本 magic
        持仓与挂单均清零**才写完成 ack 并把意图改 confirmed(0)。部分成交
        留下残仓 → 不写 ack（归档天然失败、指令原地保留）、意图如实记
        残仓净额并写告警（ALERT_FLATTEN_RESIDUAL）——下一轮继续消费同
        一条指令，直到清零。指令读不出（unreadable=True，告警已在上层
        写）→ 按"有指令"处理：照常全平，ack 的 id 记空串（无法引用；
        该文件修复/清理前每轮重复全平告警）。
        """
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
        positions = self._adapter.positions()
        outcomes = self._adapter.flatten_all()
        # C1/R6+R7：close_all 返回 ≠ 全平完成——重查**持仓明细与挂单**，
        # 均清零才写完成 ack。净额为零不等于空仓（C1 残留）：多空各残留
        # 0.08 手时净额恰为 0，按净额判完成会把残仓当全平、归档随之发生；
        # 净零但明细非空 → 视为残仓，与部分成交同路径继续消费
        leftover_positions = self._adapter.positions()
        leftover_pendings = self._adapter.pending_orders()
        if not leftover_positions and not leftover_pendings:
            write_ack(self._live_dir, self._magic, cmd_id,
                      closed=len(positions), now=n)
            self._state["blocked_dir"] = 0   # 全平后禁入状态清零（无持仓可禁）
            self._write_confirmed(cause, n)
        else:
            # 残仓（部分成交/对冲残仓）/ 撤单失败：不写完成 ack——下一轮
            # 继续消费同一条指令直到明细清零；意图如实=当前残仓净额
            residual = self._adapter.net_volume()
            self._write_confirmed(cause, n)
            append_alert(
                self._live_dir, ALERT_FLATTEN_RESIDUAL,
                f"magic {self._magic} 全平指令 {cmd_id or '<读不出>'} 执行后"
                f"仍有持仓 {len(leftover_positions)} 张（净 {residual:+.2f}"
                f" 手）、挂单 {len(leftover_pendings)} 张（部分成交/撤单失败）"
                "——不写完成 ack，下一轮继续平仓直到清零；归档在此之前"
                "不会发生", n)
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

    @staticmethod
    def _parse_known(v) -> dict:
        """known_positions 单项：{"dir": ±1, "volume": 手数}（C2/R6 起记
        最后一次已知量）；兼容旧格式（纯方向整数，volume 缺省 0）。"""
        if isinstance(v, dict):
            return {"dir": int(v.get("dir", 0)),
                    "volume": float(v.get("volume") or 0.0)}
        return {"dir": int(v), "volume": 0.0}

    @staticmethod
    def _parse_pending(data) -> dict:
        """pending_external_exits（C2/R6）：{ticket: {"dir","volume","ts"}}。
        单项损坏时保留空壳条目（ticket 仍在、防线不撤——fail-closed）；
        整体不是对象 → 空（等价旧状态文件）。"""
        out: dict[int, dict] = {}
        if isinstance(data, dict):
            for k, v in data.items():
                try:
                    ticket = int(k)
                except (TypeError, ValueError):
                    continue
                try:
                    out[ticket] = {"dir": int(v["dir"]),
                                   "volume": float(v.get("volume") or 0.0),
                                   "ts": str(v.get("ts", ""))}
                except (KeyError, TypeError, ValueError):
                    out[ticket] = {"dir": 0, "volume": 0.0, "ts": ""}
        return out

    def _load_state(self) -> dict:
        if os.path.exists(self._state_path):
            try:
                with open(self._state_path, encoding="utf-8") as f:
                    data = json.load(f)
                return {"blocked_dir": int(data.get("blocked_dir", 0)),
                        "known_positions": {
                            int(k): self._parse_known(v)
                            for k, v in data.get("known_positions",
                                                 {}).items()},
                        "pending_external_exits": self._parse_pending(
                            data.get("pending_external_exits")),
                        "last_decision_id": data.get("last_decision_id")}
            except (OSError, ValueError, TypeError):
                pass    # 状态文件损坏：从零开始（保守：无禁入、无已知持仓）
        return {"blocked_dir": 0, "known_positions": {},
                "pending_external_exits": {}, "last_decision_id": None}

    def _save_state(self) -> None:
        payload = {
            "magic": self._magic,
            "updated_at_utc": iso_utc(datetime.now(timezone.utc)),
            "blocked_dir": int(self._state["blocked_dir"]),
            "known_positions": {
                str(k): {"dir": int(v["dir"]), "volume": float(v["volume"])}
                for k, v in self._state["known_positions"].items()},
            "pending_external_exits": {
                str(k): dict(v) for k, v in
                self._state["pending_external_exits"].items()},
            "last_decision_id": self._state.get("last_decision_id"),
        }
        write_json_atomic(self._state_path, payload)   # T33：经 fileio（重试）

    def _current_positions(self) -> dict[int, dict]:
        """{ticket: {"dir": ±1, "volume": 手数}}（buy=1/sell=-1；C2/R6 起
        记最后一次已知量，供未决出场持久化）。"""
        return {p.ticket: {"dir": 1 if p.side == "buy" else -1,
                           "volume": float(p.volume)}
                for p in self._adapter.positions()}

    def _explain_exit(self, ticket: int):
        """按持仓 id 查全部成交，找保护性出场（reason ∈ {SL,TP,SO}）。

        返回 (deal, None)（已解释）或 (None, 描述)——查无平仓成交 /
        查询失败 / reason 不在已解释集合（人工 CLIENT、未知）都算未解释。
        """
        try:
            deals = self._adapter.deals_for_position(ticket)
        except Exception as exc:   # 查询失败：按未解释处理（fail-closed）
            return None, f"成交查询失败 {type(exc).__name__}: {exc}"
        closing = [d for d in deals
                   if d.entry in _CLOSING_ENTRIES and d.volume > 0]
        hit = next((d for d in closing if d.reason in _EXPLAINED_REASONS), None)
        if hit is not None:
            return hit, None
        if closing:
            return None, ("平仓成交 reason=" + ",".join(
                str(d.reason) for d in closing))
        return None, "查不到平仓成交"

    def _read_external_ack(self):
        """读 live_ack_external 的确认绑定（N-C1/R7）：意图 confirmed 且
        cause=ack_external，且附 confirmed_events——{"tickets": 被确认的
        ticket 集合, "confirmed_at": 确认时刻, "net_volume": 确认时实际
        净持仓快照}。

        旧格式（无 confirmed_events）→ None：裸 cause 字符串不构成对任何
        事件的授权（fail-closed）。返回 None 或
        (tickets: set[int], confirmed_at: datetime, net_volume: float)。
        """
        try:
            with open(self._intent_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        if not (isinstance(data, dict)
                and data.get("status") == STATUS_CONFIRMED
                and data.get("cause") == "ack_external"):
            return None
        ev = data.get("confirmed_events")
        if not isinstance(ev, dict):
            return None                  # 无绑定：不放行任何未决事件
        try:
            tickets = {int(t) for t in ev.get("tickets") or ()}
            confirmed_at = parse_iso(str(ev["confirmed_at"]))
            net_volume = float(ev["net_volume"])
        except (KeyError, TypeError, ValueError):
            return None
        return tickets, confirmed_at, net_volume

    def _consume_external_ack(self) -> None:
        """消费人工确认绑定（N-C1/R7）——仅清空**同时**满足以下条件的
        未决出场项：

        - ticket ∈ confirmed_events.tickets（人工核对过这个出场）；
        - 事件登记时刻 ≤ 确认时刻（确认之后新出现的消失不被旧确认放行）；
        - 当前实际净持仓 == 确认时刻快照（确认以来世界没再变——变了就
          要重新人工核对，这份确认不再描述当前世界）。

        任一条件不满足 → 该项保留在 pending（开仓防线继续生效）。
        查询失败/字段损坏 → 不清（fail-closed）。
        """
        ack = self._read_external_ack()
        if ack is None:
            return
        tickets, confirmed_at, net_at_ack = ack
        pending = self._state["pending_external_exits"]
        if not pending:
            return
        try:
            net_now = self._adapter.net_volume()
        except Exception:
            return                      # 持仓查询失败：不凭旧快照放行
        if abs(net_now - net_at_ack) > DEFAULT_VOLUME_TOLERANCE:
            return                      # 确认后持仓又变：需重新确认
        for ticket in sorted(pending):
            if ticket not in tickets:
                continue
            try:
                event_ts = parse_iso(str(pending[ticket].get("ts", "")))
            except (TypeError, ValueError):
                continue                # 事件时间不可判：不凭旧确认放行
            if event_ts <= confirmed_at:
                del pending[ticket]

    def _sync_external_exits(self, n: datetime) -> int:
        """每轮开头：外部出场状态机（T30/C1 + C2/R6）。

        - 未决出场（pending_external_exits）每轮开头**重查**成交历史
          （deals_for_position）——历史延迟可见时持续重试直到解释，事件
          不因 known_positions 被现值覆盖而丢失（复现 A）；
        - 本轮消失的已知持仓：reason ∈ {SL,TP,SO} → 已解释；其他（人工
          CLIENT / 未知 / 查无成交 / 查询失败）→ **持久化为未决出场**
          （ticket + 最后已知量 + 时间）并写告警，交给对账进程；
        - 意图改写（confirmed，cause=external_exit:<deals>）只在"本轮有
          消失或解释成功、且未决队列已清空"时发生——多张同时消失须全部
          解释，否则整体交给对账（不改意图，防洗白）；
        - 未决非空 → _plan_and_execute 的开仓防线（C2/R6）挡开仓腿。
        返回当前 blocked_dir。
        """
        known = dict(self._state["known_positions"])
        current = self._current_positions()
        pending = dict(self._state["pending_external_exits"])
        blocked = int(self._state["blocked_dir"])

        explained_deals: list[str] = []      # 本轮解释成功的 deal ticket
        # 1) 未决出场重查（成交历史延迟可见 → 持续重查直到解释）
        for ticket in sorted(pending):
            hit, _ = self._explain_exit(ticket)
            if hit is not None:
                del pending[ticket]
                explained_deals.append(str(hit.ticket))
                self._log_external_exit_deal(hit, n)   # R10/T58 C-2
        # 2) 本轮新消失的持仓
        gone = sorted(set(known) - set(current))
        for ticket in gone:
            hit, why = self._explain_exit(ticket)
            if hit is not None:
                explained_deals.append(str(hit.ticket))
                self._log_external_exit_deal(hit, n)   # R10/T58 C-2
                continue
            pending[ticket] = {
                "dir": int(known[ticket]["dir"]),
                "volume": float(known[ticket]["volume"]),
                "ts": iso_utc(n)}
            append_alert(
                self._live_dir, ALERT_EXTERNAL_EXIT,
                f"magic {self._magic} 持仓 {ticket} 消失且无法解释"
                f"（{why}，须 ∈ SL/TP/SO）——已记入未决出场（暂停开新仓），"
                "不改意图，交给对账进程（人工核对后 "
                "scripts/live_ack_external.py）", n)
        # 3) 意图：本轮有解释成功且未决已清空 → 意图=实际净持仓
        if explained_deals and not pending:
            self._write_confirmed(
                "external_exit:" + ",".join(sorted(explained_deals)), n)
        if gone and self._rules.reentry == "after_signal_change":
            for ticket in gone:
                blocked = known[ticket]["dir"]  # 非执行器平仓 → 禁同向再入（照旧）
        self._state["known_positions"] = current
        self._state["pending_external_exits"] = pending
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
        """本地下单日志（orders-<magic>.jsonl，T33/M8 每策略各写各的）：
        每笔市价开/平一行（频率滑窗数据源），经 fileio.append_jsonl
        （O_APPEND 单次 write，多运行器并发不交错）。"""
        for o in orders:
            if o.kind not in ("open", "close"):
                continue
            append_jsonl(self._order_log_path, {
                "ts": iso_utc(n), "magic": self._magic,
                "symbol": self._symbol, "kind": o.kind,
                "side": o.side or "", "lots": (o.filled_volume
                                               if o.filled_volume is not None
                                               else o.requested_volume),
                "decision_id": decision_id or "-",
            })
            # R10/T58（C-2）：主动成交证据行（price=成交价 res.deal_price、
            # volume=成交量、decision_bar=当轮决策 bar）——与请求日志并存
            self._log_deal(
                n, ticket=o.ticket,
                event="fill_open" if o.kind == "open" else "fill_close",
                reason=None, price=o.price,
                volume=(o.filled_volume if o.filled_volume is not None
                        else o.requested_volume),
                side=o.side or "")

    def _log_external_exit_deal(self, deal, n: datetime) -> None:
        """R10/T58（C-2）：外部/保护性出场解释的成交证据行——_explain_exit
        的 deal 带 reason/price/volume/side，如实入 deals-<magic>.jsonl
        （decision_bar=检测当轮 bar 或 null）。"""
        self._log_deal(
            n, ticket=deal.ticket, event="external_exit",
            reason=_DEAL_REASON_NAMES.get(deal.reason), price=deal.price,
            volume=deal.volume, side=deal.side)

    def _log_deal(self, n: datetime, *, ticket, event: str, reason,
                  price, volume, side) -> None:
        """deals-<magic>.jsonl 追加一行成交证据（契约 C-2 字段逐字）。

        写入一律经 fileio.append_jsonl（data/live 跨进程文件唯一写入口）。
        失败 → 记告警（ALERT_DEALS_LOG_ERROR）后继续：fail-open 仅限此
        证据流，交易路径不变（证据缺失可由告警发现，交易中断不可接受）。
        """
        try:
            append_jsonl(self._deals_log_path, {
                "ts": iso_utc(n), "magic": self._magic, "ticket": ticket,
                "event": event, "reason": reason, "price": price,
                "volume": volume, "side": side,
                "decision_bar": self._decision_bar})
        except Exception as exc:
            try:
                append_alert(
                    self._live_dir, ALERT_DEALS_LOG_ERROR,
                    f"magic {self._magic} deals 成交证据写入失败"
                    f"（{type(exc).__name__}: {exc}）——fail-open 不阻断交易，"
                    "证据缺失需人工核对", n)
            except Exception:
                pass    # 告警通道同坏：证据流失败绝不波及交易路径
