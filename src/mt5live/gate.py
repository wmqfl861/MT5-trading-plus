"""mt5live.gate（T24/C1）：风控闸门与定手数——执行器下单前的唯一关卡。

RiskGate.check_open 组装 RiskState（计划 T24 逐字）：
- 账户全部持仓（account.positions，跨 magic——风控保护整个账户）；
- 快照合约大小（data/specs/latest.json，scripts/snapshot_specs.py 产出）；
- 实时报价（market.quote 的中间价，供美元暴露换算；取不到 → missing_data
  拒单，绝不静默放行）；
- 本地下单日志频率（live/orders-<magic>.jsonl，每策略一个、LiveExecutor
  经 fileio.append_jsonl 追加（T33/M8）；闸门读全部 orders-*.jsonl（兼容
  旧 orders.jsonl），坏行按"此刻一笔下单"保守计入并告警、不阻断裁决；
  60 秒滑窗由 RiskProcess.check 的规则 7 裁决）；
- 当日盈亏（UTC 日内 deals 的已实现 + 持仓浮动，daily_pnl 同源）；
- 停机标志（killSwitch / halt.json 由 RiskProcess.check_order 文件级
  裁决；对账暂停 pause-<magic>.json（按策略，T30/D20）与全局 pause.json
  （人工）在本闸门最前检查，任一暂停即拒开仓）
→ RiskProcess.check_order（含 SessionPolicy 的禁入窗检查）。

点差闸门（T24/M10）：开仓前当前点差 > config/sessions.toml 的
spread_gate_multiplier × 点差模型该纽约本地小时的 p90 → 拒单（skip 并
记录）；模型缺该小时统计 → 按 missing_hour_policy="forbid" 禁入。口径随
模型（relative 按比率、absolute 按价格差），与 mt5live.monitor.compare_
spreads 一致。

LiveSizer（T24/M2 实盘侧）：手数 = mt5quant.sizing.lots_for_risk(
账户权益 × risk_fraction, 止损距离, 合约大小, vol 三元组)——与回测/关卡
**同一个函数**（铁律 v3 第 2 条）；风控（max_lots_per_order）只设上限，
不算手数。
"""
from __future__ import annotations

import glob
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from mt5data.sessions import SessionPolicy
from mt5live.monitor import (
    ALERT_RISK_REJECT,
    append_alert,
    naive_utc,
    parse_iso,
)
from mt5live.riskproc import OrderRequest, RiskDecision, RiskProcess, RiskState, daily_pnl
from mt5quant.costs.spread import BUCKET_TZ, SpreadModel
from mt5quant.sizing import RiskSizing, lots_for_risk

ORDER_LOG_FILE = "orders.jsonl"            # 旧版共用下单日志（只兼容读，M8）
ORDER_LOG_MAGIC_FMT = "orders-{magic}.jsonl"   # T33/M8：每策略各写各的
ORDER_LOG_MAGIC_GLOB = "orders-*.jsonl"    # 闸门读全部（频率保护整个账户）

# 告警类别（T33；monitor 未定义的新类别，常量放本层——同 executor 先例）
ALERT_PAUSE_UNREADABLE = "PAUSE_UNREADABLE"      # 暂停文件读不出：按暂停处理
ALERT_ORDER_LOG_BAD_LINE = "ORDER_LOG_BAD_LINE"  # 下单日志坏行：保守计入频率


# ---- 快照读取（合约大小 / vol 三元组；单一来源 data/specs/latest.json） ----

def _load_specs(specs_path: str) -> dict:
    with open(specs_path, encoding="utf-8") as f:
        snap = json.load(f)
    if not isinstance(snap, dict):
        raise ValueError(f"{specs_path} 结构非法（顶层须为对象）")
    return snap


def load_contract_sizes(specs_path: str) -> dict[str, float]:
    """symbol → contract_size（美元暴露换算用；快照缺品种由检查器拒单）。"""
    snap = _load_specs(specs_path)
    out: dict[str, float] = {}
    for sym, entry in snap.items():
        if isinstance(entry, dict) and isinstance(entry.get("contract_size"),
                                                  (int, float)):
            out[sym] = float(entry["contract_size"])
    return out


def load_sizing_inputs(specs_path: str, symbol: str) -> tuple[float, float, float, float]:
    """(contract_size, vol_min, vol_step, vol_max)——定手数四元组。

    缺文件/缺品种/缺字段 → ValueError（指路 snapshot_specs，绝不静默假设）。
    """
    snap = _load_specs(specs_path)
    entry = snap.get(symbol)
    if not isinstance(entry, dict):
        raise ValueError(
            f"{symbol} 不在快照 {specs_path} 中——先跑 scripts/snapshot_specs.py")
    missing = [k for k in ("contract_size", "volume_min", "volume_step",
                           "volume_max")
               if not isinstance(entry.get(k), (int, float))
               or isinstance(entry.get(k), bool)]
    if missing:
        raise ValueError(
            f"{symbol} 快照条目缺 {missing}——重跑 scripts/snapshot_specs.py")
    cs = float(entry["contract_size"])
    if not math.isfinite(cs) or cs <= 0:
        raise ValueError(f"{symbol} contract_size 须为正：{cs!r}")
    return (cs, float(entry["volume_min"]), float(entry["volume_step"]),
            float(entry["volume_max"]))


# ---- 定手数（T23 同一函数；权益 + 止损距离） ----

class LiveSizer:
    """决策 → 手数：账户权益 × risk_fraction ÷ 止损距离（lots_for_risk）。

    止损距离 = （策略 SL 倍数 | sizing.fallback_stop_atr）× 信号 bar ATR；
    ATR 不可得 → 0 手（跳过入场并记录，与引擎口径一致）。basis 固定
    "equity"（实盘没有"初始资金"锚点，RiskSizing 传其他 basis 拒绝构造）。
    """

    def __init__(self, sizing: RiskSizing, *, account, specs_path: str):
        if sizing.basis != "equity":
            raise ValueError(
                f"实盘定手数 basis 必须是 'equity'（账户权益），收到 "
                f"{sizing.basis!r}")
        self.sizing = sizing
        self._account = account
        self._specs_path = specs_path

    def lots_for(self, decision) -> float:
        if decision.signal == 0:
            return 0.0
        if decision.atr is None:
            return 0.0                       # 无风险距离（ATR 回看不足）
        k = (decision.sl_atr if decision.sl_atr is not None
             else self.sizing.fallback_stop_atr)
        equity = float(self._account.state().equity)
        cs, lo, step, hi = load_sizing_inputs(self._specs_path, decision_symbol(decision))
        return lots_for_risk(equity, self.sizing.risk_fraction, k * decision.atr,
                             cs, lo, step, hi)


def decision_symbol(decision) -> str:
    """Decision → symbol（decision_id 的第一段；Decision 未带独立字段）。"""
    return decision.decision_id.split("|", 1)[0]


# ---- 风控闸门 ----

PAUSE_FILE = "pause.json"                # 全局暂停（只留给人工）
PAUSE_MAGIC_FMT = "pause-{magic}.json"   # T30/D20：对账进程按策略写


@dataclass(frozen=True)
class _Paused:
    paused: bool
    reason: str | None
    unreadable: bool = False   # T33/M2：文件存在但读不出（fail-closed 按暂停）


def read_pause(live_dir: str, magic: int | None = None) -> _Paused:
    """读暂停指令（T30/D20：只读；写路径在对账进程/人工）。

    magic 给定 → ``pause-<magic>.json``（对账进程按策略写的暂停）；
    magic=None → 全局 ``pause.json``（只留给人工）。缺失 = 未暂停；
    **存在但读不出（损坏/权限/缺 paused 字段）= 暂停**（T33/M2 反转旧
    语义——停机类信号读不出必须按停机处理，绝不默认未暂停）。此时
    paused=True、reason 以 ``pause_unreadable`` 开头、unreadable=True，
    由调用方（RiskGate.check_open）写告警。
    """
    name = (PAUSE_FILE if magic is None
            else PAUSE_MAGIC_FMT.format(magic=magic))
    path = os.path.join(live_dir, name)
    if not os.path.exists(path):
        return _Paused(False, None)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "paused" not in data:
            raise ValueError(f"顶层须为带 paused 字段的对象，收到 {data!r}")
    except (OSError, ValueError) as e:
        return _Paused(
            True, f"pause_unreadable: {name} 读不出（{e}）——fail-closed "
            "按暂停处理，人工核对后修复或删除", unreadable=True)
    return _Paused(bool(data.get("paused")), data.get("reason"))


def order_log_paths(live_dir: str) -> list[str]:
    """全部下单日志路径：orders-<magic>.jsonl（每策略一个，T33/M8）+ 兼容
    旧版共用 orders.jsonl（存在才列入）。"""
    paths = sorted(glob.glob(os.path.join(live_dir, ORDER_LOG_MAGIC_GLOB)))
    legacy = os.path.join(live_dir, ORDER_LOG_FILE)
    if os.path.exists(legacy):
        paths.append(legacy)
    return paths


def read_recent_order_times(live_dir: str, now: datetime,
                            window_s: float = 3600.0) -> tuple:
    """全部下单日志 → 近 window_s 秒内的下单时刻（频率规则的数据源）。

    行格式（LiveExecutor 经 fileio.append_jsonl 追加）：{"ts": iso, "magic",
    "symbol", "kind", "side", "lots", "decision_id"}。T33/M8 语义：

    - 读**全部** orders-*.jsonl（+旧 orders.jsonl）——频率保护整个账户；
    - 坏行（并发追加交错/半行）不再抛错阻断所有策略：保守按**此刻一笔
      下单**计入频率（时间 = now，必在滑窗内），并写告警
      （ALERT_ORDER_LOG_BAD_LINE）；闸门照常给出裁决。
    """
    n = naive_utc(now)
    out: list[datetime] = []
    bad: list[str] = []
    for path in order_log_paths(live_dir):
        try:
            with open(path, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        rec = json.loads(ln)
                        t = parse_iso(rec["ts"])
                    except (ValueError, KeyError, TypeError):
                        bad.append(os.path.basename(path))
                        out.append(n)     # 保守：坏行按此刻一笔下单计入
                        continue
                    if (n - t).total_seconds() < window_s:
                        out.append(t)
        except OSError:
            bad.append(os.path.basename(path))
            out.append(n)                 # 整个文件读不出：同样保守计 1 笔
    if bad:
        try:
            append_alert(
                live_dir, ALERT_ORDER_LOG_BAD_LINE,
                f"下单日志 {len(bad)} 行读不出（{sorted(set(bad))}）——按"
                "此刻一笔下单保守计入频率（M8），人工核对 orders-*.jsonl", n)
        except OSError:
            pass                          # 告警通道不可用：不让告警打崩闸门
    return tuple(out)


class RiskGate:
    """执行器的开仓闸门：组装 RiskState → RiskProcess.check_order（+点差）。

    平仓不经本闸门（执行器语义）；加仓/开仓/反手的开仓腿都必须过
    check_open，拒绝 → 不下单、原因回传执行器写心跳 last_error 与
    alerts.log（ALERT_RISK_REJECT）。
    """

    def __init__(self, process: RiskProcess, *, account, market,
                 specs_path: str, live_dir: str,
                 spread_model: SpreadModel | None = None,
                 session_policy: SessionPolicy | None = None,
                 clock=None):
        """
        - process：RiskProcess（halt/killSwitch 文件级裁决 + 纯函数规则）；
        - account / market：mt5gw 的 AccountService / MarketData（测试注入
          同签名替身）；
        - specs_path：data/specs/latest.json（合约大小与 vol 三元组）；
        - spread_model：点差模型（None = 不做点差闸门）；
        - session_policy：点差闸门倍数来源（缺省用风控进程已加载的策略）。
        """
        self._process = process
        self._account = account
        self._market = market
        self._specs_path = specs_path
        self._live_dir = live_dir
        self._spread_model = spread_model
        self._session_policy = (session_policy if session_policy is not None
                                else process.session_policy)
        # R6/C4：点差闸门的每小时最少覆盖天数从**会话策略**取（构造时定格，
        # 与回测 entry_policy_for 同一来源 config/sessions.toml）——n_days
        # 不足的小时按 missing_hour_policy="forbid" 禁入，不能只看 stat.p90。
        self._spread_min_days = (self._session_policy.min_days_per_hour
                                 if self._session_policy is not None else 0)
        self._clock = clock or (lambda: datetime.now(timezone.utc)
                                .replace(tzinfo=None))

    def check_open(self, symbol: str, side: str, lots: float, magic: int,
                   now: datetime | None = None) -> RiskDecision:
        n = naive_utc(now if now is not None else self._clock())

        # 0) 对账暂停（T30/D20）：本策略 pause-<magic>.json（对账进程写）与
        #    全局 pause.json（人工）任一暂停即拒开仓；读不出按暂停处理并
        #    告警（T33/M2 fail-closed——停机类信号读不出必须按停机处理）
        paused = read_pause(self._live_dir, magic)
        if paused.paused:
            if paused.unreadable:
                append_alert(self._live_dir, ALERT_PAUSE_UNREADABLE,
                             f"magic {magic}：{paused.reason}", n)
            return RiskDecision(
                False, f"reconcile_paused: 持仓对账不一致（magic {magic}，"
                f"{paused.reason or f'详见 pause-{magic}.json'}），暂停开仓")
        manual = read_pause(self._live_dir)
        if manual.paused:
            if manual.unreadable:
                append_alert(self._live_dir, ALERT_PAUSE_UNREADABLE,
                             f"全局人工暂停文件：{manual.reason}", n)
            return RiskDecision(
                False, f"reconcile_paused: 全局人工暂停（pause.json，"
                f"{manual.reason or '原因见文件'}），暂停开仓")

        # 1) 组装 RiskState（各来源缺数据 → 检查器 missing_data 拒单）。
        #    R7/N-C2 一致快照：账户提供 snapshot()（paper 目录聚合视图
        #    PaperAccountView——live_runner 的 --paper 装配）→ **单次**调用取
        #    同一版本的持仓+当日成交：两次独立查询之间落盘的平仓不会被
        #    同时计为浮亏与已实现；聚合读失败（状态文件存在但读不出）
        #    经异常如实上抛（执行器 risk_gate_error fail-closed）。
        #    其他账户（真终端 mt5gw / 测试替身）退回逐查询（同进程内存
        #    一致，无跨文件双计问题）。
        day_start = n.replace(hour=0, minute=0, second=0, microsecond=0)
        snap_fn = getattr(self._account, "snapshot", None)
        if callable(snap_fn):
            snap = snap_fn()
            positions = snap.positions
            deals = tuple(d for d in snap.deals if day_start <= d.time <= n)
        else:
            positions = tuple(self._account.positions())
            deals = tuple(self._account.deals(day_start, n))
        contract_sizes = load_contract_sizes(self._specs_path)
        quotes: dict[str, tuple[float, float]] = {}
        for sym in {p.symbol for p in positions} | {symbol}:
            q = self._market.quote(sym)          # 取不到 → 异常如实上抛给调用方记录
            quotes[sym] = (float(q.bid), float(q.ask))
        prices = {s: (b + a) / 2.0 for s, (b, a) in quotes.items()}
        recent = read_recent_order_times(self._live_dir, n)
        realized, floating = daily_pnl(deals, positions)
        state = RiskState(
            positions=positions, contract_sizes=contract_sizes, prices=prices,
            recent_order_times=recent, today_realized=realized,
            today_floating=floating, halted=False)

        # 2) 风控进程裁决（killSwitch/halt 文件优先，其后 8 条纯函数规则）
        decision = self._process.check_order(
            OrderRequest(symbol=symbol, side=side, volume_lots=lots,
                         magic=magic),
            state, n)
        if decision.flatten_all:
            # 日亏在开仓检查时被发现：立即落实全平+停机文件（不等风控主循环
            # 下一轮），执行器下一轮消费 flatten。全平指令走与风控主循环
            # **同一编号协议**（T32/D21，mt5live.commands：带 uuid 编号、
            # ack 引用编号、归档核对编号——闸门只写指令，不代执行器 ack）。
            # killSwitch/halt 只有停机语义（killSwitch 不自动平仓，平仓由
            # 人工决定），不写 flatten。
            self._process.trigger_flatten_and_halt("daily_loss_cap", n)
        if not decision.allowed:
            return decision

        # 3) 点差闸门（M10）：当前点差 > 倍数 × 模型该纽约小时 p90 → 跳过
        if self._spread_model is not None:
            spread_decision = self._spread_check(symbol, quotes[symbol], n)
            if spread_decision is not None:
                return spread_decision
        return decision

    # ---- 内部 ----

    def _spread_check(self, symbol: str, quote: tuple[float, float],
                      now: datetime) -> RiskDecision | None:
        """点差闸门（M10 + R6/C4）。

        stat 查询带上覆盖天数判定（阈值 min_days_per_hour 构造时从会话策略
        取）：小时无统计（stat 抛 ValueError）**或 n_days 不足**都按
        missing_hour_policy="forbid" 禁入——此前只取 stat.p90，一天样本的
        小时在配置要求 20 天时仍放行。p90 阈值比较的口径不变（relative 按
        比率、absolute 按价差）。
        """
        bid, ask = quote
        hour_ny = now.replace(tzinfo=timezone.utc).astimezone(
            ZoneInfo(BUCKET_TZ)).hour
        model = self._spread_model
        try:
            stat = model.stat(hour_ny, symbol=symbol)
            if stat.n_days < self._spread_min_days:
                return RiskDecision(
                    False,
                    f"spread_gate: {symbol} 纽约小时 {hour_ny} 点差统计覆盖不足"
                    f"（n_days={stat.n_days} < min_days_per_hour="
                    f"{self._spread_min_days}，missing_hour_policy=forbid，"
                    "该小时禁入；回补 tick 后重建 data/costs/spread_model.json）")
            p90 = stat.p90
        except ValueError:
            return RiskDecision(
                False,
                f"spread_gate: {symbol} 纽约小时 {hour_ny} 无点差统计"
                f"（missing_hour_policy=forbid，该小时禁入；重建 "
                f"data/costs/spread_model.json 或检查 tick 覆盖）")
        if model.mode == "relative":
            mid = (bid + ask) / 2.0
            if not math.isfinite(mid) or mid <= 0:
                return RiskDecision(
                    False, f"spread_gate: {symbol} 报价中间价非法"
                    f"（bid={bid!r}, ask={ask!r}），拒开仓")
            current = (ask - bid) / mid
        else:
            current = ask - bid
        mult = self._session_policy.spread_gate_multiplier
        if current > mult * p90:
            return RiskDecision(
                False,
                f"spread_gate: 当前点差 {current:.6g} > {mult:g}×p90"
                f"（{p90:.6g}，{symbol} 纽约小时 {hour_ny}，口径 {model.mode}）"
                f"——跳过开仓等下一网格")
        return None


def alert_reject(live_dir: str, magic: int, decision_id: str | None,
                 reason: str, now: datetime | None = None) -> None:
    """开仓被拒的即时告警（心跳 last_error 之外的双通道记录）。"""
    append_alert(live_dir, ALERT_RISK_REJECT,
                 f"magic {magic} {decision_id or '-'}：{reason}",
                 naive_utc(now) if now is not None else None)


__all__ = [
    "ORDER_LOG_FILE",
    "ORDER_LOG_MAGIC_FMT",
    "LiveSizer",
    "RiskGate",
    "alert_reject",
    "decision_symbol",
    "load_contract_sizes",
    "load_sizing_inputs",
    "order_log_paths",
    "read_pause",
    "read_recent_order_times",
]
