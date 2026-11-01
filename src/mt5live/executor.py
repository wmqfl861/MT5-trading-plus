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
  返回 duplicate。H2-01（阶段 1A，2026-11-01）起升级为 **execution
  journal 持久幂等**：发送前登记 pending（decision_id/request_uuid/腿），
  订单返回记 sent，结论记 confirmed/rejected/unknown——同 decision_id
  的重投在 poll 入口被终态幂等拒绝（confirmed 不重发；rejected **不再
  可重投**，重发须先过对账/仲裁——语义变更，与审计"任何重发先过对账"
  一致）；回报未知（异常）→ 意图保持 pending + journal 记 unknown，
  重启由 _run_journal_recovery 核对券商裁决（R2/R3/R5）。
- retcode 分类复用 mt5gw 异常：TradeRejectedError / OrderResultUnknownError /
  LiveTradingBlockedError 原样上抛；执行器层唯一例外是 **Requote(10004)**
  ——确定态拒绝（订单未成交，重发安全），网关内部有界重试耗尽后仍上抛时
  执行器再重试一次（T89/R14-L5，2026-10-29 交易日 2 平仓腿 requote 崩溃
  事故）；其余被拒零重试；Unknown 必须先对账再决定，绝不盲目重发。
- 部分成交（DONE_PARTIAL）如实回报（filled_volume < requested_volume），
  不追单——差额留给下个决策点或人工/风控处置。
- 绝不 import MetaTrader5 直连；实盘保险丝语义（mt5gw.config）不在本层。
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from mt5gw.errors import RETCode, TradeRejectedError
from mt5gw.models import DEAL_REASON_SL, DEAL_REASON_SO, DEAL_REASON_TP
from mt5gw.trading import sanitize_comment
from mt5live.commands import (
    ALERT_COMMAND_UNREADABLE,
    CommandUnreadableError,
    read_ack,
    read_flatten,
    write_ack,
)
from mt5live.fileio import append_jsonl, append_line, write_json_atomic
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

logger = logging.getLogger(__name__)


def decision_comment(decision_id: str) -> str:
    """decision_id → MT5 安全订单 comment（压缩形态，仅辅助定位）。

    canonical decision_id = "{symbol}|{TF}|{bar_close_iso}"（runner 与风控
    进程的接口约定）。首战事故（F 阶段）原始 comment 30 字符被 MT5 Python
    库以 -2 拒绝——**根因是长度**（T89/R14-L5 实测：≥30 拒、29 过、'|'
    单独不致拒），压缩为 "{symbol}_{TF}_{MMDD}_{HHMM}"（白名单字符、
    ≤20，远低于 25 上限）。非 canonical 形态退回 sanitize_comment 兜底
    安全化。完整 decision_id 恒在本地留痕（expected-<magic>.json 的 cause
    与 orders-<magic>.jsonl 行），注释不承担还原职责。
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
    action: str                    # duplicate/hold/open/add/reduce/reverse/
                                    # reverse_pending/close
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

        # H-04：hold 只在"净额达标且无持仓明细"时成立——signal=0 且存在
        # 持仓（哪怕净额恰为 0 的多空对锁）必须走清仓（按 ticket 判断，
        # 净额为零不能掩盖对冲残仓）
        if abs(target - net) <= _EPS and not (target == 0 and positions):
            action = "hold"
        elif target == 0:
            action = "close"                    # 归零（含净零对锁）→ 逐仓全平
            outcomes.extend(self._close_all(positions))
        elif net * target < 0:                  # 反向：先平后开（target 带符号定方向）
            action = "reverse"
            outcomes.extend(self._close_all(positions))
            # H-04：平仓腿未确认清零前禁止开反向仓——部分成交残仓（或期间
            # 外部新仓）时开反向仓会形成多空对锁；只平不开（reverse_pending），
            # 差额留给下个决策点（新 decision_id）续做反向
            if not self._my_positions():
                outcomes.extend(self._open(decision, target))
            else:
                action = "reverse_pending"
        elif net == 0:
            action = "open"
            outcomes.extend(self._open(decision, target))
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

    def history_deals(self, since: datetime, to: datetime) -> list:
        """[since, to] 窗口内本 magic 本品种的成交历史（H2-01 重启核对用；
        AccountService.deals 透传 + magic/symbol 双重过滤）。"""
        return [d for d in self._account.deals(since, to)
                if d.magic == self._magic and d.symbol == self._symbol]

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

    def _requote_retry_once(self, send):
        """Requote(10004) 确定态拒绝（订单未成交，重发安全）→ 额外重试一次。

        T89/R14-L5（2026-10-29 交易日 2 事故）：网关内部 3 次重取参考价重试
        耗尽后 TradeRejectedError(REQUOTE) 上抛，runner 未捕获崩溃退出、
        风控停机。requote 不同于 Unknown（状态未知须先对账）：服务器给了
        新价、原单确定未成交——执行器作为第二道防线再重试一次（send 内部
        会重取参考价）；仍 requote 则原样上抛（持续行情背离交给风控停机
        路径，绝不无限重试）。其余异常（含其他 retcode 的被拒）不在此列。
        """
        try:
            return send()
        except TradeRejectedError as exc:
            if exc.retcode != RETCode.REQUOTE:
                raise
            logger.warning(
                "市价单 Requote(10004) 网关重试耗尽仍被拒，执行器重试一次"
                "（确定态未成交，重发安全）：%s", exc)
            return send()

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
        res = self._requote_retry_once(lambda: self._trading.market_order(
            self._symbol, side, lots, sl=sl, tp=tp,
            deviation=self._deviation,
            comment=decision_comment(decision.decision_id),  # 安全压缩（F 停修）
            magic=self._magic))
        outcome = OrderOutcome(
            kind="open", ticket=res.order_ticket, side=side,
            requested_volume=res.requested_volume or lots,
            filled_volume=(res.volume if res.filled else None),
            price=res.deal_price,
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
            res = self._requote_retry_once(lambda: self._trading.close_position(
                pos.ticket, vol, deviation=self._deviation, magic=self._magic))
            outcomes.append(OrderOutcome(
                kind="close", ticket=pos.ticket,
                side="sell" if pos.side == "buy" else "buy",
                requested_volume=vol,
                filled_volume=(res.volume if res.filled else None),
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
# H2-01（阶段 1A，计划 2026-11-01-hline-foundation）：execution journal——
# 决策级订单意图与结局的持久流水（发送前 pending → sent → 终态
# confirmed/rejected/unknown），同 decision_id 的重发由它幂等拒绝，重启
# 恢复（1B）以它为准核对券商。经 fileio.append_jsonl（O_APPEND 单次
# write + fsync），与 orders/deals 同目录。
JOURNAL_FILE_FMT = "execution-journal-{magic}.jsonl"
# R3：判 rejected 需要"成交历史窗口覆盖发送时刻"的最小经过时间（秒）——
# 成交归档延迟可见时窗口实际未覆盖，过早判 rejected 会让重放重复开仓。
JOURNAL_HISTORY_COVER_MARGIN_S = 60.0
# H2-02（阶段 2A，计划 2026-11-01-hline-foundation）：consumed deal 水位——
# 每笔已用于解释持仓变化的成交（ticket/volume/time/reason/ts_consumed）。
# _explain_exit 的候选成交排除已消费票号：旧部分 SL deal 不得复活解释
# 新消失（审计探针根因）。R5：文件丢失/损坏 → fail-closed（解释停摆）。
CONSUMED_FILE_FMT = "consumed-deals-{magic}.jsonl"
# H2-02（2B，R4）：出场解释的事件区间 ``(最后已知存在时刻, 检测时刻]``
# 两侧各加的裕量（秒）——券商成交时刻与本地墙钟的偏差容忍；区间外的
# 更古老 deal 一律不构成"本次事件"（非固定 ±60s 时间点过滤）。
EXIT_EVENT_MARGIN_S = 30.0
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
ALERT_STATE_CORRUPT = "STATE_CORRUPT"           # H-05：状态文件损坏/不可读
#   ——保守恢复（暂停开仓+保留证据），不清空 blocked_dir/
#   pending_external_exits 的保护，人工核对前不放开
ALERT_EXECUTION_UNKNOWN = "EXECUTION_UNKNOWN"   # H2-01（1A/1B）：execution
#   journal 存在未决决策（发送后崩溃/回报未知/行撕裂）——暂停开新仓
#   （平仓不受影响），直到重启核对裁决或 live_ack_execution 仲裁
ALERT_BROKER_PENDING_ORDER = "BROKER_PENDING_ORDER"  # H2-01：未关联到
#   当前 journal 的本 magic 挂单仍可能成交——暂停开新仓，平仓不受影响
ALERT_EXECUTION_RECOVERED = "EXECUTION_RECOVERED"   # H2-01（1B）：重启核对
#   裁决落定（confirmed/rejected）——expected 已恢复为实际净持仓
ALERT_BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE" # H2-02（2C）：券商持仓查询
#   失败/超时——本轮不开新仓、不清空 pending exit（fail-closed），不 crash


def _event_dt(value) -> datetime | None:
    """事件区间端点的宽松解析：datetime / ISO 字符串 → naive UTC；解析
    失败或空值 → None（= 区间不可界定，fail-closed）。"""
    if isinstance(value, datetime):
        return naive_utc(value)
    try:
        return parse_iso(str(value)) if value else None
    except (TypeError, ValueError):
        return None

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
                                    # open/add/reduce/reverse/reverse_pending/
                                    # close/blocked_reentry/blocked_external_exit/
                                    # blocked_state_corrupt/blocked_execution_
                                    # unknown/blocked_broker_unavailable/
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
        self._journal_path = os.path.join(
            live_dir, JOURNAL_FILE_FMT.format(magic=magic))
        self._consumed_path = os.path.join(
            live_dir, CONSUMED_FILE_FMT.format(magic=magic))
        self._decision_bar: str | None = None   # 本轮决策 bar（deals 行用）
        # H2-02（2A）：consumed 水位——{deal_ticket: 记录}；None = 水位不可用
        #（R5：文件丢失/损坏 → fail-closed，一切出场解释停摆）。
        self._consumed: dict[int, dict] | None = None
        self._consumed_size = -1
        self._consumed_alerted = False
        # H2-01（1A/1B）：journal 终态表（decision_id → confirmed/rejected，
        # poll 入口幂等跳过）与未决表（pending/sent/unknown/torn）——由
        # _journal_refresh 从文件重建（崩溃/仲裁工具追写都能看到）。
        self._journal_final: dict[str, str] = {}
        self._undecided: dict[str, dict] = {}
        self._journal_size = -1                 # stat 变化才重读
        self._journal_active: str | None = None  # 本轮已 journal pending 的决策
        self._torn_alerted: set[str] = set()
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

        # 0) H2-01（1A/1B）：execution journal 刷新 + 未决项核对券商——先于
        #    一切下单路径；未决非空时每轮重查（延迟可见时下轮确认，R3）
        self._run_journal_recovery(n)

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
        #    H2-02（2C）：券商持仓查询失败/超时 → fail-closed：本轮不开
        #    新仓、不清空 pending、known/意图原样保留，写告警返回（不
        #    crash——旧实现异常上抛直接崩掉运行器）。
        try:
            blocked = self._sync_external_exits(n)
        except Exception as exc:
            append_alert(
                self._live_dir, ALERT_BROKER_UNAVAILABLE,
                f"magic {self._magic} 券商持仓查询失败（{type(exc).__name__}:"
                f" {exc}）——fail-closed：本轮不开新仓、不清空未决出场，"
                "状态原样保留", n)
            try:
                net_snapshot = self._adapter.net_volume()
            except Exception:
                net_snapshot = 0.0
            return RoundReport(
                action="blocked_broker_unavailable",
                decision_id=(decision.decision_id if decision is not None
                             else None),
                net_before=net_snapshot, net_after=net_snapshot,
                error=f"broker_unavailable: {type(exc).__name__}: {exc}")

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

        # 3a) H2-01（1A）：journal 终态幂等——confirmed（已成交不重发）/
        #     rejected（拒绝后不可重投——语义变更：被拒决策不再可重投，
        #     重发须先过对账/仲裁；与审计"任何重发先过对账"一致）的决策
        #     重投一律跳过，零新增请求
        final = self._journal_final.get(decision.decision_id)
        if final is not None:
            self._finish(n, decision.decision_id, ())
            net_now = self._adapter.net_volume()
            reason = (
                "journal 幂等跳过：该决策已有终态 confirmed"
                "（已成交的决策不重发）" if final == "confirmed" else
                "journal 幂等跳过：该决策已有终态 rejected"
                "（拒绝后不可重投——重发须先过对账/仲裁）")
            return RoundReport(
                action="duplicate", decision_id=decision.decision_id,
                net_before=net_now, net_after=net_now, error=reason)

        # 3b) H-05：同 bar 决策去重（跨重启）——last_decision_id 已随状态
        #     持久化，重启后同一 decision_id 的重投递一律 duplicate、零新增
        #     请求（adapter._seen 只在内存，重启即失效是原缺陷；世界在重启
        #     窗口已变时按过期决策继续下单等于重复下单）
        if decision.decision_id == self._state.get("last_decision_id"):
            self._finish(n, decision.decision_id, ())
            return RoundReport(
                action="duplicate", decision_id=decision.decision_id,
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
        report = self._after_orders(decision, report, n)
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

    def _close_only_leg(self, decision, n: datetime) -> list[OrderOutcome]:
        """把反向决策降级为"只平不开"执行（H-04：平仓不受限）。

        用于反向决策的开仓腿被挡的场合（未决外部出场 / 状态损坏保守恢复 /
        风控拒绝 / 风控取数异常）：平仓腿照常先执行，开仓腿留待防线解除后
        的下个决策点。意图走 pending(0)→confirmed(实际) 与正常下单同契约。
        """
        close_only = replace(decision, signal=0, target_position=0,
                             volume_lots=0.0)
        self._write_pending(0.0, decision.decision_id, n)
        try:
            report = self._adapter.execute(close_only)
        except Exception:
            self._write_confirmed(decision.decision_id, n)
            raise
        return list(report.orders)

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

        # H-04：needs_open_leg 只在目标非零时成立——target=0 是纯平仓
        #（含净零对锁清仓），任何开仓防线都不得拦它（平仓不受限）
        needs_open_leg = (
            target != 0
            and (net == 0 or net * target < 0 or abs(target) > abs(net)))
        if needs_open_leg:
            # 开仓腿的四道挡板：状态损坏保守恢复（H-05）→ 未决外部出场
            #（C2/R6 第二道防线）→ journal 未决决策（H2-01）→ 风控闸门（C1）。
            # 前三道挡开仓腿时平仓腿照常（_close_only_leg），风控拒绝/异常
            # 同路径。
            block_action = block_alert = block_reason = None
            if self._state.get("state_corrupt"):
                block_action = "blocked_state_corrupt"
                block_alert = ALERT_STATE_CORRUPT
                block_reason = (
                    "state_corrupt: 状态文件损坏（证据已保留 "
                    "state-*.corrupt-*）——暂停开新仓（平仓不受影响），"
                    "人工核对券商实际持仓并修复/删除 state-<magic>.json "
                    "后重启")
            elif self._state.get("pending_external_exits"):
                # C2/R6：存在未解释的外部出场（pending 非空）→ 本 magic
                # 停止开新仓（平仓照常），直到成交历史解释为 SL/TP/SO
                # 或 live_ack_external 人工确认——不依赖对账进程稍后写
                # pause（先于对账运行也生效）。
                pending_exits = self._state["pending_external_exits"]
                block_action = "blocked_external_exit"
                block_alert = ALERT_EXTERNAL_EXIT
                block_reason = (
                    "external_exit_pending: 存在未解释的外部出场"
                    f"（tickets {sorted(pending_exits)}）——本 magic "
                    "暂停开新仓（平仓不受影响），直到解释为 SL/TP/SO"
                    " 或 scripts/live_ack_external.py 人工确认")
            elif self._undecided:
                # H2-01（1A/1B）：execution journal 存在未决决策（发送后
                # 崩溃/回报未知/行撕裂）——该决策可能已成交，任何新开仓都
                # 可能在重复开仓；暂停开新仓（平仓不受影响），直到重启
                # 核对裁决（confirmed/rejected）或 live_ack_execution 仲裁。
                block_action = "blocked_execution_unknown"
                block_alert = ALERT_EXECUTION_UNKNOWN
                block_reason = (
                    "execution_unknown_pending: execution journal 存在未决"
                    f"决策 {sorted(self._undecided)}（发送后崩溃/回报未知，"
                    "重启核对尚未裁决）——暂停开新仓（平仓不受影响），直到"
                    "核对裁决或 scripts/live_ack_execution.py 仲裁")
            else:
                # H2-01 P0：旧版本/人工/其他执行路径遗留的挂单可能在未来
                # 成交。即使 journal 没有未决项，也不能在未决挂单仍存在时
                # 再开新仓；否则旧挂单 + 新市价单会叠加风险。
                try:
                    broker_pendings = self._adapter.pending_orders()
                except Exception as exc:
                    block_action = "blocked_broker_unavailable"
                    block_alert = ALERT_BROKER_UNAVAILABLE
                    block_reason = (
                        "broker_pending_query_failed: 无法确认本 magic 未决挂单"
                        f"（{type(exc).__name__}: {exc}）——fail-closed，暂停"
                        "开新仓（平仓不受影响）")
                else:
                    if broker_pendings:
                        block_action = "blocked_broker_pending"
                        block_alert = ALERT_BROKER_PENDING_ORDER
                        block_reason = (
                            "broker_pending_order: 本 magic 仍有未完成挂单 "
                            f"{[o.ticket for o in broker_pendings]}——该订单可能"
                            "稍后成交，暂停开新仓（平仓不受影响）；先核对并"
                            "撤销/确认挂单终态后再开仓")
            if block_action is not None:
                append_alert(
                    self._live_dir, block_alert,
                    f"magic {self._magic} {decision.decision_id}："
                    f"{block_reason}", n)
                if net * target < 0:
                    # 反向：平仓腿照常（不受限），开仓腿被挡——先平后不开
                    orders = self._close_only_leg(decision, n)
                    return RoundReport(
                        action=block_action,
                        decision_id=decision.decision_id, net_before=net,
                        net_after=self._adapter.net_volume(),
                        orders=tuple(orders), error=block_reason)
                return RoundReport(
                    action=block_action,
                    decision_id=decision.decision_id, net_before=net,
                    net_after=self._adapter.net_volume(), error=block_reason)
            side = "buy" if target > 0 else "sell"
            leg_lots = (abs(target) if net * target <= 0
                        else abs(target - net))
            if self._gate is not None:
                try:
                    verdict = self._gate.check_open(
                        self._symbol, side, leg_lots, self._magic, now=n)
                except Exception as exc:
                    # 闸门数据故障：fail-closed 拒开仓——但平仓不受限（H-04）：
                    # 反向决策照常执行平仓腿（与风控拒绝同路径，先平后不开），
                    # 绝不因风控取数异常把减仓/平仓也挂起
                    if net * target < 0:
                        orders = self._close_only_leg(decision, n)
                        return _rejected(
                            f"risk_gate_error: {type(exc).__name__}: {exc}",
                            orders=orders)
                    return _rejected(
                        f"risk_gate_error: {type(exc).__name__}: {exc}")
                if not verdict.allowed:
                    if net * target < 0:
                        # 反向：平仓腿照常（不受限），开仓腿被拒——先平后不开
                        orders = self._close_only_leg(decision, n)
                        return _rejected(verdict.reason, action="risk_rejected",
                                         orders=orders)
                    return _rejected(verdict.reason)
        # 下单前（闸门已过）：意图 = 目标持仓（pending；订单未回，对账给宽限）
        self._write_pending(target, decision.decision_id, n)
        # H2-01（1A）：发送前 journal 登记决策与唯一请求身份——这行写不进去
        # 就不发送（append 抛错 → 本轮中断，零请求），幂等保证从这行开始；
        # 其后的任何崩溃都留下 pending/sent 状态，由重启核对（1B）裁决。
        # 纯平仓的 _close_only_leg 不入 journal（重放安全：目标 0，已平则
        # hold——journal 防的是重复开仓，不拦风险降低操作）。
        self._journal_begin(decision, target, net, n)
        try:
            report = self._adapter.execute(decision)
        except TradeRejectedError as exc:
            # 确定态拒绝（服务器明确拒单，订单确定未成交）：journal 记
            # rejected；意图如实 confirmed(实际净持仓)——确定没有成交，
            # 实际即真相（与 Unknown 分开记录：审计要求"分别记录确定
            # 拒绝、已成交、未知待核对"）。
            self._journal_finish(decision.decision_id, "rejected", n=n,
                                 detail=f"retcode={exc.retcode}: {exc}",
                                 retcode=exc.retcode)
            self._write_confirmed(decision.decision_id, n)
            raise
        except Exception as exc:
            # R2/H2-01：回报未知（IPC 中断等）——旧实现把未知也洗白成
            # confirmed(当前可见持仓)，无法证明该请求最终没有成交。改为：
            # 意图保持 pending（对账宽限后 pause，双防线），journal 记
            # unknown；重启由 _run_journal_recovery 核对券商裁决。
            self._journal_finish(decision.decision_id, "unknown", n=n,
                                 detail=f"exception: {type(exc).__name__}:"
                                        f" {exc}")
            raise
        # 发送已回：journal "sent"（此后到终态前的崩溃由重启核对裁决）
        self._journal_sent(decision.decision_id, report, n)
        return self._from_execution(report)

    def _after_orders(self, decision, report: RoundReport,
                      n: datetime) -> RoundReport:
        """订单返回后区分真实成交与仅被券商接受的请求。

        PLACED/无成交量不等于 fill：意图保持 pending；journal 保持
        unknown，直到后续成交、挂单终态或人工仲裁。若反向操作已有平仓
        成交但开仓腿仍 pending，expected 按已发生的真实净持仓更新，
        journal 仍保持 unknown，防止下一决策重复增加风险。
        """
        legs = [o for o in report.orders if o.kind in ("open", "close")]
        if not legs:
            return report
        filled = [o for o in legs
                  if o.filled_volume is not None and o.filled_volume > _EPS]
        unfilled = [o for o in legs
                    if o.filled_volume is None or o.filled_volume <= _EPS]
        if unfilled:
            # 有真实成交的腿，expected 可按当前券商净持仓如实落盘；
            # 若完全没有成交，保留发送前 pending 意图，不得洗成 confirmed。
            if filled:
                self._write_confirmed(decision.decision_id, n)
            if self._journal_active == decision.decision_id:
                self._journal_finish(
                    decision.decision_id, "unknown", n=n,
                    detail=("broker accepted/request returned without "
                            "confirmed fill; pending legs="
                            f"{[(o.kind, o.ticket, o.retcode) for o in unfilled]}"))
            append_alert(
                self._live_dir, ALERT_EXECUTION_UNKNOWN,
                f"magic {self._magic} {decision.decision_id} 存在已返回但未确认"
                f"成交的订单腿 {[(o.kind, o.ticket, o.retcode) for o in unfilled]}；"
                "不写成交台账、不解除执行未知门禁，等待券商成交/撤单终态并对账",
                n)
            return replace(
                report, action="execution_pending",
                error="broker_order_pending: request accepted but fill not confirmed")

        # 所有开/平腿均有真实成交量，意图按实际净持仓确认。
        self._write_confirmed(decision.decision_id, n)
        if self._journal_active == decision.decision_id:
            self._journal_finish(decision.decision_id, "confirmed", n=n,
                                 detail=f"action={report.action}")
        if report.action in ("add", "reduce"):
            append_alert(
                self._live_dir, ALERT_UNEXPECTED_RESIZE,
                f"magic {self._magic} {decision.decision_id} 出现 {report.action}"
                f" 腿（净 {report.net_before:+.2f} → 目标差异）——D22 持仓期间"
                "不调仓，正常轮次不应加减仓（固定手数配置被人为改动？仓位被"
                "外部改动？），须人工核对", n)
        for o in filled:
            if o.partial:
                append_alert(
                    self._live_dir, ALERT_PARTIAL_FILL,
                    f"magic {self._magic} {decision.decision_id} {o.kind} "
                    f"部分成交：请求 {o.requested_volume} 手只成 "
                    f"{o.filled_volume} 手——意图按成交量登记；差额不再自动"
                    "补足（D22 持仓期间不调仓），反手/全平时了结或人工处置",
                    n)
        return report

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

    # ---- H2-01（阶段 1A/1B）：execution journal ----
    #
    # 决策级订单流水：发送前 pending（decision_id/request_uuid/腿计划）→
    # sent（发送已回）→ 终态 confirmed/rejected/unknown。作用：
    # - 同 decision_id 幂等：终态决策重投一律跳过（poll 入口 3a）——
    #   "券商已成交但本地崩溃后重启，同一决策不重复开仓"；
    # - 重启恢复（1B）：pending/sent/unknown 的最近决策逐项核对券商
    #   （当前持仓 + 挂单 + 成交历史），裁决落终态并恢复 expected；
    # - R5：尾部撕裂行（写一半崩溃）按 unknown 处理（合成未决项挡开仓，
    #   仲裁工具解除），绝不 crash。

    def _journal_append(self, record: dict) -> None:
        """journal 追加一行（append_line：O_APPEND 单次 write + fsync）。

        尾部此前若有撕裂行（无换行结尾），先补一个换行把残片封闭——
        新行不与残片拼接成两败俱伤（R5）。"""
        prefix = ""
        try:
            with open(self._journal_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                if size:
                    f.seek(size - 1)
                    if f.read(1) != b"\n":
                        prefix = "\n"
        except OSError:
            pass                    # 不存在/读不了：直接追加（append 自建）
        append_line(self._journal_path,
                    prefix + json.dumps(record, ensure_ascii=False) + "\n")

    def _journal_begin(self, decision, target: float, net: float,
                       n: datetime) -> None:
        """发送前登记（必须在 order_send 之前落盘；抛错则本轮不发送）。"""
        action = ("close" if target == 0 else
                  "reverse" if net * target < 0 else
                  "open" if net == 0 else
                  "add" if abs(target) > abs(net) else "reduce")
        self._journal_active = decision.decision_id
        self._journal_append({
            "ts": iso_utc(n), "decision_id": decision.decision_id,
            "request_uuid": uuid.uuid4().hex, "action": action,
            "symbol": self._symbol,
            "side": "buy" if target >= 0 else "sell",
            "volume": round(abs(target), 8), "status": "pending"})

    def _journal_sent(self, decision_id: str, report, n: datetime) -> None:
        """发送已回（首个开/平腿的 retcode/ticket 如实入行）。"""
        first = next((o for o in report.orders
                      if o.kind in ("open", "close")), None)
        self._journal_append({
            "ts": iso_utc(n), "decision_id": decision_id, "status": "sent",
            "retcode": first.retcode if first else None,
            "order_ticket": first.ticket if first else None})

    def _journal_finish(self, decision_id: str, status: str, *, n: datetime,
                        detail: str | None = None,
                        retcode: int | None = None) -> None:
        """落终态（confirmed/rejected）或中间态（unknown）。"""
        record = {"ts": iso_utc(n), "decision_id": decision_id,
                  "status": status}
        if detail is not None:
            record["detail"] = detail
        if retcode is not None:
            record["retcode"] = retcode
        self._journal_append(record)
        if status in ("confirmed", "rejected"):
            self._journal_final[decision_id] = status
            self._undecided.pop(decision_id, None)
        elif decision_id in self._undecided:
            self._undecided[decision_id]["status"] = status
        if self._journal_active == decision_id:
            self._journal_active = None

    @staticmethod
    def _journal_entry_ts(value) -> datetime | None:
        try:
            return parse_iso(str(value)) if value else None
        except (TypeError, ValueError):
            return None

    def _journal_refresh(self, n: datetime) -> None:
        """journal 有新内容时全文重读，重建终态表/未决表（R5 撕裂不 crash）。

        文件 append-only：行号稳定，撕裂行登记为合成未决项 torn@<行号>
        （跨重启稳定，仲裁工具可按此 id 解除）。他进程（仲裁工具）落了
        终态、或重启前崩在"终态已写、意图未恢复"的窗口 → 补写意图。"""
        try:
            size = os.path.getsize(self._journal_path)
        except OSError:
            size = -1
        if size == self._journal_size:
            return
        self._journal_size = size
        prev_undecided = set(self._undecided)
        finals: dict[str, str] = {}
        undecided: dict[str, dict] = {}
        torn: set[str] = set()

        def _synthetic(key: str) -> None:
            torn.add(key)
            undecided[key] = {"decision_id": key, "status": "torn",
                              "send_ts": None, "synthetic": True}

        lines: list[str] = []
        if size >= 0:
            try:
                with open(self._journal_path, encoding="utf-8",
                          errors="replace") as f:
                    lines = f.read().split("\n")
            except OSError:
                _synthetic("torn@unreadable")
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                if not isinstance(rec, dict):
                    raise ValueError("非对象行")
                did = str(rec.get("decision_id") or "")
                status = str(rec.get("status") or "")
                if not did or not status:
                    raise ValueError("缺 decision_id/status")
            except ValueError:
                _synthetic(f"torn@{i}")     # R5：撕裂/损坏行按 unknown 处理
                continue
            if status == "pending" and did not in finals:
                undecided[did] = {"decision_id": did, "status": "pending",
                                  "send_ts": self._journal_entry_ts(
                                      rec.get("ts"))}
            elif status in ("sent", "unknown") and did in undecided:
                undecided[did]["status"] = status
            elif status in ("confirmed", "rejected"):
                finals[did] = status
                undecided.pop(did, None)
        self._journal_final = finals
        self._undecided = undecided
        for key in sorted(torn - self._torn_alerted):
            self._torn_alerted.add(key)
            append_alert(
                self._live_dir, ALERT_EXECUTION_UNKNOWN,
                f"magic {self._magic} execution journal 存在撕裂/损坏行"
                f"（{key}，R5 按 unknown 处理）——无法归属到具体决策，暂停"
                "开新仓（平仓不受影响）；人工核对券商后台后用 "
                "scripts/live_ack_execution.py --decision-id <该合成 id> "
                "仲裁解除", n)
        # 未决→终态（本进程核对落终态之外，还有仲裁工具/重启窗口的补写）：
        # 崩溃留下的 pending 意图在此恢复为实际净持仓
        for did in sorted(prev_undecided & set(finals) - set(undecided)):
            self._fix_intent_after_final(did, n)

    def _fix_intent_after_final(self, decision_id: str, n: datetime) -> None:
        """终态落定后：若意图文件仍停在该决策的 pending（崩溃窗口），按
        实际净持仓恢复为 confirmed（journal_recovery:<id>）。"""
        try:
            with open(self._intent_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if (isinstance(data, dict) and data.get("status") == STATUS_PENDING
                and data.get("cause") == decision_id):
            try:
                self._write_confirmed(f"journal_recovery:{decision_id}", n)
            except Exception:       # 持仓查询/写失败：下一轮再试（fail-closed）
                pass

    def _journal_verdict(self, entry: dict, n: datetime) -> tuple[str, str]:
        """核对券商裁决一项未决决策：(verdict, 详情)。

        - confirmed 证据：发送时刻之后本 magic 的任意成交（请求已真实到达
          并执行），或 state 未知的新持仓 ticket（成交归档延迟时的第二
          证据）；
        - 空成交/空持仓/空挂单不是拒单的正面证据：历史归档可能延迟，
          且订单可能已成交后又平仓；此情形保持 unknown，只能由明确拒单
          retcode 或 live_ack_execution 人工仲裁落 rejected。
        - 查询失败或仍有挂单同样保持 unknown（fail-closed）。
        """
        send_ts = entry.get("send_ts")
        try:
            positions = self._adapter.positions()
            pendings = self._adapter.pending_orders()
            deals = (self._adapter.history_deals(
                send_ts - timedelta(seconds=1), n)
                if send_ts is not None else [])
        except Exception as exc:
            return "", f"券商核对查询失败 {type(exc).__name__}: {exc}"
        # P0 audit fix: any still-open pending order for this magic/symbol
        # keeps the execution outcome unresolved. Check this before accepting
        # broad post-send deal/position evidence, which may otherwise belong to
        # a different event while the original request can still fill.
        if pendings:
            return "", (f"仍有本 magic 未决挂单 "
                        f"{[o.ticket for o in pendings]}——保持 unknown，"
                        "等待挂单终态并重新对账")
        if deals:
            return "confirmed", (f"发送后本 magic 成交 {len(deals)} 笔"
                                 f"（tickets {[d.ticket for d in deals]}）")
        known = set(self._state.get("known_positions") or {})
        fresh = [p.ticket for p in positions if p.ticket not in known]
        if fresh:
            return "confirmed", f"出现 state 未知的新持仓 {fresh}"
        # Absence of a deal after an arbitrary time margin is not positive
        # evidence that the broker rejected the request: history publication
        # can be delayed, and an accepted order may have filled and closed.
        # Only an explicit TradeRejectedError or operator arbitration may
        # resolve this negative case. Keep the gate closed indefinitely.
        return "", (
            f"未发现可归属成交或新持仓（持仓 {len(positions)}、挂单 "
            f"{len(pendings)}），但空历史不是拒单证明——保持 unknown，"
            "须等待券商订单/成交证据或 live_ack_execution 人工仲裁")

    def _run_journal_recovery(self, n: datetime) -> None:
        """每轮开头：journal 刷新 + 未决项核对券商（H2-01/1B，重启或崩溃后）。

        未决（pending/sent/unknown/torn）非空时逐项核对：能确定已成交 →
        confirmed + expected 恢复；能确定拒绝 → rejected + expected 恢复；
        不确定 → 保持 unknown（开仓腿被 _plan_and_execute 第四道挡板拦住，
        平仓不受影响），下轮重查（R3 延迟可见时不误判）。"""
        self._journal_refresh(n)
        if not self._undecided:
            return
        for key in sorted(self._undecided):
            entry = self._undecided[key]
            if entry.get("synthetic"):
                continue      # 撕裂/不可读：无真实 decision_id，仅仲裁可解除
            verdict, detail = self._journal_verdict(entry, n)
            if not verdict:
                # 保持 unknown：下轮重查（R3）。首个失败轮记一次告警
                #（开仓腿被挡的轮次另有 block 告警，避免每轮刷屏）。
                if not entry.get("alerted"):
                    entry["alerted"] = True
                    append_alert(
                        self._live_dir, ALERT_EXECUTION_UNKNOWN,
                        f"magic {self._magic} journal 未决决策 "
                        f"{entry['decision_id']} 本轮无法裁决（{detail}）——"
                        "保持 unknown 并暂停开新仓（平仓不受影响），下轮"
                        "重查；长期未决用 scripts/live_ack_execution.py "
                        "仲裁", n)
                continue
            decision_id = entry["decision_id"]
            self._journal_finish(decision_id, verdict, n=n, detail=detail)
            try:
                self._write_confirmed(f"journal_recovery:{decision_id}", n)
            except Exception:
                pass          # 意图恢复失败：终态已落，下一轮 refresh 补写
            append_alert(
                self._live_dir, ALERT_EXECUTION_RECOVERED,
                f"magic {self._magic} journal 未决核对裁决：决策 "
                f"{decision_id} → {verdict}（{detail}）——expected 已恢复为"
                "实际净持仓", n)

    # ---- 外部出场检测与状态持久化 ----

    @staticmethod
    def _parse_known(v) -> dict:
        """known_positions 单项：{"dir": ±1, "volume": 手数, "ts": 最后
        观察到该持仓的时刻}（C2/R6 起记最后一次已知量；H2-02 起记最后已知
        存在时刻——出场解释事件区间的下界）；兼容旧格式（纯方向整数、
        无 ts——ts 为空串时区间不可界定，按未解释处理，fail-closed）。"""
        if isinstance(v, dict):
            return {"dir": int(v.get("dir", 0)),
                    "volume": float(v.get("volume") or 0.0),
                    "ts": str(v.get("ts") or "")}
        return {"dir": int(v), "volume": 0.0, "ts": ""}

    @staticmethod
    def _parse_pending(data) -> dict:
        """pending_external_exits（C2/R6 + H2-02）：{ticket: {"dir","volume",
        "ts"(检测时刻), "last_seen"(最后已知存在时刻)}}。单项损坏时保留
        空壳条目（ticket 仍在、防线不撤——fail-closed）；整体不是对象 →
        空（等价旧状态文件）。"""
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
                                   "ts": str(v.get("ts", "")),
                                   "last_seen": str(v.get("last_seen") or "")}
                except (KeyError, TypeError, ValueError):
                    out[ticket] = {"dir": 0, "volume": 0.0, "ts": "",
                                   "last_seen": ""}
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
                        "last_decision_id": data.get("last_decision_id"),
                        "state_corrupt": bool(data.get("state_corrupt",
                                                       False)),
                        "consumed_written": bool(
                            data.get("consumed_written", False))}
            except (OSError, ValueError, TypeError) as exc:
                # H-05：状态文件损坏/不可读 ≠ 首次启动——保守恢复：不清空
                # blocked_dir/pending_external_exits 的保护（原文已不可信，
                # 就按"保护仍在"处理：state_corrupt 挡开仓腿，平仓不受影响），
                # 损坏原文抄送 *.corrupt-<时间戳> 保留证据（_save_state 稍后
                # 会以合法内容覆盖原路径），人工核对券商实际持仓并修复/删除
                # state 文件前不放开；封锁随新状态文件持久化、跨重启持续
                evidence = self._preserve_corrupt_state()
                append_alert(
                    self._live_dir, ALERT_STATE_CORRUPT,
                    f"magic {self._magic} 状态文件 {self._state_path} 损坏/"
                    f"不可读（{type(exc).__name__}: {exc}）——保守恢复：暂停"
                    "开新仓（平仓不受影响），不清空 blocked_dir/"
                    "pending_external_exits 的保护"
                    + (f"；损坏原文已抄送 {evidence} 保留证据"
                       if evidence else
                       "（证据抄送失败——请立即人工保全该文件）")
                    + "。人工核对券商实际持仓后，修复/删除该状态文件并重启",
                    None)
                return {"blocked_dir": 0, "known_positions": {},
                        "pending_external_exits": {},
                        "last_decision_id": None, "state_corrupt": True,
                        "consumed_written": False}
        return {"blocked_dir": 0, "known_positions": {},
                "pending_external_exits": {}, "last_decision_id": None,
                "state_corrupt": False, "consumed_written": False}

    def _preserve_corrupt_state(self) -> str | None:
        """损坏的状态原文抄送到 state-<magic>.json.corrupt-<时间戳>（H-05
        保留证据）；失败返回 None（告警由调用方写）。"""
        try:
            with open(self._state_path, "rb") as f:
                raw = f.read()
        except OSError:
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        evidence = f"{self._state_path}.corrupt-{stamp}"
        try:
            with open(evidence, "wb") as f:
                f.write(raw)
            return evidence
        except OSError:
            return None

    def _save_state(self) -> None:
        payload = {
            "magic": self._magic,
            "updated_at_utc": iso_utc(datetime.now(timezone.utc)),
            "blocked_dir": int(self._state["blocked_dir"]),
            "known_positions": {
                str(k): {"dir": int(v["dir"]), "volume": float(v["volume"]),
                         "ts": str(v.get("ts") or "")}
                for k, v in self._state["known_positions"].items()},
            "pending_external_exits": {
                str(k): dict(v) for k, v in
                self._state["pending_external_exits"].items()},
            "last_decision_id": self._state.get("last_decision_id"),
            "state_corrupt": bool(self._state.get("state_corrupt")),
            # H2-02（R5）：曾写过 consumed 水位——文件却缺失时按丢失处理
            "consumed_written": bool(self._state.get("consumed_written")),
        }
        write_json_atomic(self._state_path, payload)   # T33：经 fileio（重试）

    def _current_positions(self, n: datetime) -> dict[int, dict]:
        """{ticket: {"dir": ±1, "volume": 手数, "ts": 观察时刻}}（buy=1/
        sell=-1；C2/R6 起记最后一次已知量；H2-02 起记最后已知存在时刻——
        出场解释事件区间的下界）。"""
        return {p.ticket: {"dir": 1 if p.side == "buy" else -1,
                           "volume": float(p.volume),
                           "ts": iso_utc(n)}
                for p in self._adapter.positions()}

    def _explain_exit(self, ticket: int, *,
                      expected_volume: float | None = None,
                      lower_ts=None, upper_ts=None):
        """按持仓 id 查全部成交，**绑定本次事件**找保护性出场。

        H-05 绑定 + H2-02（2A/2B，R4/R5）双水位：
        - 候选成交排除 **consumed 水位**中的票号——已用于解释先前事件的
          旧 deal（如部分 SL）不得复活解释新消失（审计探针根因：旧部分
          SL 量恰等于余量时"解释"掉了实际是人工的出场）；
        - 事件**区间** ``(lower_ts, upper_ts]`` ± EXIT_EVENT_MARGIN_S：
          lower=最后已知该仓位存在的时刻、upper=检测到消失/量变的时刻。
          区间外的更古老 deal 一律不构成"本次事件"（非固定 ±60s 时间点
          过滤——事件区间随最后已知量逐轮推进）；端点缺失/不可解析 →
          区间不可界定，按未解释处理（fail-closed）；
        - consumed 水位不可用（R5：文件丢失/损坏）→ 一律未解释；
        - expected_volume（本次事件的量：消失=最后已知量、量变=减少量）：
          区间内候选按成交时间**从最新往回累计到该量为止**；
          累计不足 → 成交历史不完整/延迟可见，按未解释处理；
        - 本次事件**全部**成交 reason ∈ {SL,TP,SO} 才算已解释——混入
          CLIENT/未知即人工参与，交给对账/人工。

        返回 (event_deals: list, None)（已解释——全部事件成交，供消费与
        证据流）或 (None, 描述)——查无成交 / 查询失败 / 水位不可用 /
        区间外 / reason 不在已解释集合都算未解释。
        """
        consumed = self._consumed_tickets()
        if consumed is None:
            return None, "consumed 水位不可用（R5 fail-closed）"
        lower = _event_dt(lower_ts)
        upper = _event_dt(upper_ts)
        if lower is None or upper is None:
            return None, ("事件区间不可界定（最后已知存在时刻/检测时刻"
                          "缺失——旧格式状态？）须人工确认")
        if expected_volume is not None and expected_volume <= \
                DEFAULT_VOLUME_TOLERANCE:
            return None, "本次事件量为 0，无从绑定成交"
        try:
            deals = self._adapter.deals_for_position(ticket)
        except Exception as exc:   # 查询失败：按未解释处理（fail-closed）
            return None, f"成交查询失败 {type(exc).__name__}: {exc}"
        lo = lower - timedelta(seconds=EXIT_EVENT_MARGIN_S)
        hi = upper + timedelta(seconds=EXIT_EVENT_MARGIN_S)
        closing = sorted(
            (d for d in deals
             if d.entry in _CLOSING_ENTRIES and d.volume > 0
             and d.ticket not in consumed
             and lo < naive_utc(d.time) <= hi),
            key=lambda d: naive_utc(d.time))
        event = closing
        if expected_volume is not None and event:
            picked: list = []
            acc = 0.0
            for d in reversed(event):
                picked.append(d)
                acc = round(acc + d.volume, 8)
                if acc >= expected_volume - DEFAULT_VOLUME_TOLERANCE:
                    break
            if acc < expected_volume - DEFAULT_VOLUME_TOLERANCE:
                return None, (f"事件区间内未消费的平仓成交累计 {acc:.2f} 手，"
                              f"不足本次事件量 {expected_volume:.2f} 手（成交"
                              "历史不完整/延迟可见？）——按未解释处理")
            event = picked[::-1]
        if not event:
            return None, "事件区间内查不到未消费的平仓成交"
        if any(d.reason not in _EXPLAINED_REASONS for d in event):
            return None, ("本次事件平仓成交 reason=" + ",".join(
                str(d.reason) for d in event))
        return event, None       # 全部为保护性出场 → 已解释（全事件成交）

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
        """每轮开头：外部出场状态机（T30/C1 + C2/R6 + H2-02 R4/R5）。

        - 未决出场（pending_external_exits）每轮开头**重查**成交历史
          （deals_for_position）——历史延迟可见时持续重试直到解释，事件
          不因 known_positions 被现值覆盖而丢失（复现 A）；重查绑定**登记
          时的事件区间**（last_seen→检测时刻），解释成功即把覆盖 deal 记入
          consumed 水位（2A）；
        - **部分减量（R4）**：本轮仍在但 volume 减少的已知持仓 → 在事件
          区间内找新 deal 解释（SL/TP/SO → 已解释+消费，仓位继续持有）；
          找不到/混入人工 → 按未决出场登记（volume=减少量）+ 告警——
          旧实现只看 ticket 消失，部分 SL deal 从未被消费（审计根因）；
        - 本轮消失的已知持仓：reason ∈ {SL,TP,SO} → 已解释+消费；其他
          （人工 CLIENT / 未知 / 查无成交 / 查询失败）→ **持久化为未决
          出场**（ticket + 最后已知量 + 区间端点）并写告警，交给对账进程；
        - consumed 水位不可用（R5）→ 一切解释停摆（全部按未决处理）；
        - 意图改写（confirmed，cause=external_exit:<deals>）只在"本轮有
          消失或解释成功、且未决队列已清空"时发生——多张同时消失须全部
          解释，否则整体交给对账（不改意图，防洗白）；
        - 未决非空 → _plan_and_execute 的开仓防线（C2/R6）挡开仓腿。
        返回当前 blocked_dir。
        """
        known = dict(self._state["known_positions"])
        current = self._current_positions(n)
        pending = dict(self._state["pending_external_exits"])
        blocked = int(self._state["blocked_dir"])

        explained_deals: list[str] = []      # 本轮解释成功的 deal ticket
        consumed_now: list = []              # 本轮待记入水位的 deal（2A）
        # 1) 未决出场重查（成交历史延迟可见 → 持续重查直到解释）。解释绑定
        #    登记时的事件区间 + consumed 水位排除（H2-02：迟到的旧 deal
        #    不得冒充，延迟可见的新 deal 到位后自然解释）
        for ticket in sorted(pending):
            event, _ = self._explain_exit(
                ticket,
                expected_volume=float(pending[ticket].get("volume") or 0.0),
                lower_ts=pending[ticket].get("last_seen"),
                upper_ts=pending[ticket].get("ts"))
            if event:
                del pending[ticket]
                for d in event:
                    explained_deals.append(str(d.ticket))
                    self._log_external_exit_deal(d, n)    # R10/T58 C-2
                consumed_now.extend(event)
        # 1b) 部分减量（R4）：同 ticket 持仓量减少（非整仓消失）→ 事件区间
        #     内的新 deal 解释+消费；解释不了 → 未决出场（volume=减少量）
        for ticket in sorted(set(known) & set(current)):
            drop = round(float(known[ticket]["volume"])
                         - float(current[ticket]["volume"]), 8)
            if drop <= DEFAULT_VOLUME_TOLERANCE:
                continue
            event, why = self._explain_exit(
                ticket, expected_volume=drop,
                lower_ts=known[ticket].get("ts"), upper_ts=n)
            if event:
                for d in event:
                    explained_deals.append(str(d.ticket))
                    self._log_external_exit_deal(d, n)    # R10/T58 C-2
                consumed_now.extend(event)
                continue
            # 覆盖同一 ticket 的新事件登记（后到事件覆盖先前未决——防线
            # 只增不减：该 ticket 存在未解释的外部改动）
            pending[ticket] = {
                "dir": int(known[ticket]["dir"]),
                "volume": drop,
                "ts": iso_utc(n),
                "last_seen": known[ticket].get("ts") or iso_utc(n)}
            append_alert(
                self._live_dir, ALERT_EXTERNAL_EXIT,
                f"magic {self._magic} 持仓 {ticket} 部分减量 {drop:.2f} 手"
                f"且无法解释（{why}，须 ∈ SL/TP/SO）——已记入未决出场"
                "（暂停开新仓），不改意图，交给对账进程（人工核对后 "
                "scripts/live_ack_external.py）", n)
        # 2) 本轮新消失的持仓：解释绑定本次事件（H-05 量绑定 + H2-02
        #    区间/水位）——区间内未消费的成交从最新往回累计到最后已知量；
        #    解释成功即消费（旧 deal 永不复活）
        gone = sorted(set(known) - set(current))
        for ticket in gone:
            event, why = self._explain_exit(
                ticket,
                expected_volume=float(known[ticket].get("volume") or 0.0),
                lower_ts=known[ticket].get("ts"), upper_ts=n)
            if event:
                for d in event:
                    explained_deals.append(str(d.ticket))
                    self._log_external_exit_deal(d, n)    # R10/T58 C-2
                consumed_now.extend(event)
                continue
            pending[ticket] = {
                "dir": int(known[ticket]["dir"]),
                "volume": float(known[ticket]["volume"]),
                "ts": iso_utc(n),
                "last_seen": known[ticket].get("ts") or iso_utc(n)}
            append_alert(
                self._live_dir, ALERT_EXTERNAL_EXIT,
                f"magic {self._magic} 持仓 {ticket} 消失且无法解释"
                f"（{why}，须 ∈ SL/TP/SO）——已记入未决出场（暂停开新仓），"
                "不改意图，交给对账进程（人工核对后 "
                "scripts/live_ack_external.py）", n)
        # 3) 解释成功的 deal 记入 consumed 水位（先于状态落盘——宁可下一
        #    轮重复解释一次，不可让旧 deal 复活解释新事件）
        if consumed_now:
            self._consume_deals(consumed_now, n)
        # 4) 意图：本轮有解释成功且未决已清空 → 意图=实际净持仓
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

    # ---- H2-02（2A）：consumed deal 水位 ----

    def _consumed_refresh(self) -> None:
        """consumed 水位加载/按尺寸增量刷新（R5 fail-closed）。

        - 文件缺失且 state 显示曾写过（consumed_written）→ 按**丢失**处理：
          水位不可信（_consumed=None，解释停摆）+ STATE_CORRUPT 告警；
        - 文件存在但任何一行解析失败 → **损坏**：同上；
        - 损坏/丢失后每轮重试解析：人工从备份恢复文件后自动解除停摆。
        """
        try:
            size = os.path.getsize(self._consumed_path)
        except OSError:
            if self._state.get("consumed_written"):
                self._consumed_broken("丢失")
            else:
                self._consumed = {}
            return
        if size == self._consumed_size and self._consumed is not None:
            return                      # 无变化且可用：跳过重读
        records: dict[int, dict] = {}
        try:
            with open(self._consumed_path, encoding="utf-8") as f:
                for line in f.read().split("\n"):
                    if not line.strip():
                        continue
                    rec = json.loads(line)     # 任何一行坏 → 整体不可信
                    records[int(rec["ticket"])] = {
                        "ticket": int(rec["ticket"]),
                        "volume": float(rec["volume"]),
                        "time": str(rec.get("time", "")),
                        "reason": rec.get("reason"),
                        "ts_consumed": str(rec.get("ts_consumed", ""))}
        except (OSError, ValueError, TypeError, KeyError):
            self._consumed_broken("损坏")
            return
        recovered = self._consumed is None
        self._consumed = records
        self._consumed_size = size
        if recovered:
            self._consumed_alerted = False    # 恢复可用：告警节流复位

    def _consumed_broken(self, how: str) -> None:
        """水位不可用（R5）：解释停摆（_consumed=None）+ 一次性告警。"""
        self._consumed = None
        self._consumed_size = -1        # 每轮重试解析（恢复后自动解除）
        if not self._consumed_alerted:
            self._consumed_alerted = True
            append_alert(
                self._live_dir, ALERT_STATE_CORRUPT,
                f"magic {self._magic} consumed 水位文件{how}"
                f"（{self._consumed_path}）——已消费成交票号不可信，外部"
                "出场解释全部停摆（新消失按未决处理、既有未决视为仍在，"
                "等同 state_corrupt 的保守恢复）；人工从备份恢复该文件"
                "（恢复后自动解除）或核对券商后台后处置", None)

    def _consumed_tickets(self) -> set[int] | None:
        """已消费 deal 票号集合；None = 水位不可用（调用方 fail-closed）。"""
        self._consumed_refresh()
        return None if self._consumed is None else set(self._consumed)

    def _consume_deals(self, deals, n: datetime) -> None:
        """把已用于解释的成交记入 consumed 水位（先于状态落盘——宁可下轮
        重复解释一次 pending，不可让旧 deal 复活解释新事件）。"""
        if self._consumed is None:
            return                      # 水位不可用：解释路径本已停摆
        for d in deals:
            if d.ticket in self._consumed:
                continue                # 水位单调：不重复消费
            append_jsonl(self._consumed_path, {
                "ticket": d.ticket, "volume": d.volume,
                "time": iso_utc(naive_utc(d.time)), "reason": d.reason,
                "ts_consumed": iso_utc(n)})
            self._consumed[d.ticket] = {"ticket": d.ticket}
            self._consumed_size = -1    # 尺寸已变：下轮强制重读
        self._state["consumed_written"] = True

    def _finish(self, n: datetime, decision_id, orders) -> None:
        """每轮收尾：known_positions 同步、状态落盘、意图首次登记、下单日志。

        意图只在**本文件尚不存在**（首次登记，让对账进程开始管理本 magic）
        时兜底写 confirmed(实际净持仓)；下单轮的 pending→confirmed 由
        _plan_and_execute/_after_orders/_consume_flatten 显式写。被拒/跳过/
        无动作的轮次绝不改写意图——目标持仓没变，改写反而会把外部漂移
        洗白（对账就失去意义了）。
        """
        self._state["known_positions"] = self._current_positions(n)
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
            # R10/T58（C-2）：成交证据只记录真实 fill。PLACED/accepted
            # 请求没有 filled_volume，必须只留在 orders 请求日志，不能伪造
            # fill_open/fill_close（否则台账会把未成交挂单当成交）。
            if (o.filled_volume is not None and o.filled_volume > _EPS
                    and o.price is not None):
                self._log_deal(
                    n, ticket=o.ticket,
                    event="fill_open" if o.kind == "open" else "fill_close",
                    reason=None, price=o.price, volume=o.filled_volume,
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
