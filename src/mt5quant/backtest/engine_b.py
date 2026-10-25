"""B 层回测引擎（逐 tick）：信号仍是已收盘 bar，成交与风控逐笔 tick 真实报价。

与 A 层（engine_a）的关系——同一信号源，不同执行模型：
- 信号：完全相同（同一个 strategy.generate_signals，只用已收盘 bar；
  bar i 收盘产生目标仓位信号，绝不看 bar i 之后的 K 线）。
- 成交：bar i 收盘时刻 + latency_ms 之后的第一笔 tick（就绪时刻含边界）。
  多头按该 tick Ask 买入、Bid 卖出；空头相反（Bid 卖出开仓、Ask 买入平仓）。
  点差来自真实 tick（不调用 costs.spread_p90 / costs.slippage）；
  slippage_price 为每腿额外不利滑点（多头买入加/卖出减，空头对称）。
- SL/TP：逐 tick 判断——多头 Bid ≤ SL / Bid ≥ TP，空头 Ask ≥ SL / Ask ≤ TP，
  按触发那笔 tick 的价格 ± slippage_price 成交。跳空穿过价位直接按触发
  tick 价成交（跳空止损更差、跳空止盈更好，都是真实可达价），因此 B 层
  没有 stop_gap / take_profit_gap 之分，exit_reason 只有
  signal | stop | take_profit | time_stop | week_close | end_of_data
  （检查顺序仍止损优先；单笔 tick 只有一个 bid/ask，本就不可能双触）。
  SL/TP 价位 = 入场成交价 ± 系数 × ATR14（ATR 取信号 bar，与 A 层一致）；
  入场 tick 自身不触发自己的 SL/TP（从下一笔 tick 起监控）。
- time_stop / week_close / end_of_data：在该 bar 收盘前最后一笔 tick
  （msc < 收盘时刻）成交，多头 Bid、空头 Ask ± slippage_price。
- 时间字段：entry_time / exit_time 就是成交 tick 的时刻（毫秒精度）。
  A 层是 bar 开盘标签；对账时"信号类成交"两层时间完全一致（bar 收盘后的
  第一笔 tick 通常即下一根 bar 的开盘 tick），收盘价类出场相差不足一根
  bar——这是 T10 对账容差的对象，不是缺陷。
- 禁入窗（T20 / B-0.2，SessionPolicy）：开仓订单在禁入时段就绪时保留，
  等第一个允许时刻的 tick 成交；平仓不受限；禁入窗未开到数据末尾 → 订单
  作废（不视为覆盖不足）。与 A 层、实盘风控共用 config/sessions.toml。
- 定手数（T23 / B-0.4，M2）：`sizing: RiskSizing` 与 `volume_lots` 二选一
  （同给/同缺都拒），语义与 A 层 A-1.5 完全一致（同一函数
  mt5quant.sizing.lots_for_risk）：按成交 tick 时点的资金（basis="initial"
  初始资金 / "equity" = 初始资金 + 已实现净利）× risk_fraction 与止损距离
  （策略 SL 距离或 fallback_stop_atr×信号 bar ATR；ATR 不可得 → 0 手）定
  手数；手数为 0 → 本笔订单作废并计数 skipped_zero_lots（写进 summary），
  信号持续时下一次 bar 收盘重新下单重试；Trade.volume_lots 如实记录。
- bars_held = 出场 bar 序号 − 入场 bar 序号（成交 tick 所在 bar 区间），
  与 A 层口径一致。
- 再入场（T25 / B-0.5，R7 对齐 A-1.3）：ExitRules.reentry="immediate"（默认）
  ＝目标仓位语义——保护性出场后信号仍指向该方向时下一根 bar 收盘重新下单；
  "after_signal_change"＝保护性出场（stop / take_profit / time_stop /
  week_close；signal 出场不算，与 A 层 PROTECTIVE_REASONS 同源——B 层没有
  stop_gap/take_profit_gap）后记 blocked_dir，信号离开该方向（变 0 或反向）
  之前禁止同向开仓。解禁判定时点＝bar 收盘的下单决策（用刚收盘 bar 的
  信号），与 A 层"下一根 bar 开盘用上一根收盘信号"同口径；期间反向信号的
  反手单照常下单（平仓腿执行、开仓腿在成交时被跳过，同 A 层"先平后不开"）。
- 爆仓标记（T25 / B-0.5，R16 对齐）：逐 bar 收盘权益 ≤ 0 时引擎停止开新仓
  （已有持仓的各类出场照常执行），BacktestResult.ruined=True 且 ruined_at
  记首次发生 bar 的时间（标记粘滞，权益回升不清除）；equity 序列如实记录
  负值。判定时点＝bar 收盘（与 A 层逐 bar 收盘市值同口径）。
- week_close_exit 只允许 ≤ H1 的周期（T25 / B-0.5，R8 对齐）：与 A 层
  同文案拒绝（D1/H4 会提前一整根平仓）。
- 金额口径与 A 层（C1 修复后）完全一致：contract_size 必填；
  gross = 价格差 × 手数 × contract_size（账户货币）；
  commission = 每手来回佣金 × 手数；swap = costs.overnight_fee(
  symbol, side, entry_time, exit_time) × 手数；net = gross − commission + swap。
  spread_cost 拆分 = 非Bid 腿那笔 tick 的 (ask − bid) × 手数 × contract_size
  （多头入场腿、空头出场腿，对应 A 层"价差挂非Bid腿"的拆分口径）；
  slippage_cost = 两腿 slippage_price × 手数 × contract_size。
  两字段仅作报告拆分，计算 net 时不再重复扣减（已含在成交价内）。
- equity：逐 bar 收盘市值 = 初始资金 + 已实现净利 + 持仓浮盈，按该 bar
  收盘前最后一笔 tick 估值（多头 Bid、空头 Ask），与各自平仓口径一致。
- tick 覆盖（fail-fast，绝不静默跳过）：任一 bar 区间 [t_i, t_i+周期)
  内曾有持仓或待成交订单却一笔 tick 都没有 → TickCoverageError，报出
  该区间与已加载 tick 的覆盖范围；数据末尾仍未成交的订单同样报错。
  bar 之间的天然缺口（周末/休市，无 bar 便无区间）不构成覆盖不足。
- 读取：ticks_loader(start_msc=..., end_msc=...) 按月窗口流式拉取
  （生产接线 functools.partial(mt5data.ticks.load_ticks, root, symbol)，
  契约即 load_ticks 的区间参数），一次只在内存驻留一个月；
  tick 帧需含 time_msc/bid/ask，time_msc 单调不倒退、bid/ask 有限为正
  且 ask ≥ bid，违反抛 ValueError。
- 性能（T28/M8，B-0.3）：tick 校验按月块向量化（不再逐笔 Python 判定）；
  不再为每笔 tick 构造 pd.Timestamp——成交时刻在 _open_at/_close_at 按需
  构造；空仓且无待成交订单的区间用 numpy searchsorted 直接跳到下一个
  bar 边界后的首笔 tick（被跳过段的末笔按"逐笔消费"语义更新 prev，
  覆盖检查/收盘估值结果与逐笔循环逐位一致）；逐笔段按 64k 小批把三列
  转 Python 标量迭代（tolist 批转换，免去逐笔 numpy 标量提取），下一个
  bar 收盘时刻与持仓方向/SL/TP 提升为循环内局部变量。
- 本引擎是正确性优先的参考实现（持仓区间仍逐 tick Python 循环）；
  持仓区间的向量化留待 T10 对账通过之后。
"""
from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import asdict, fields
from typing import Callable, Protocol

import numpy as np
import pandas as pd

from mt5data.store import TIMEFRAME_SECONDS
from mt5quant.sizing import RiskSizing, lots_for_risk, validate_volume_bounds
from mt5quant.strategies.base import ExitRules
# 复用 A 层的公开类型与校验器（单一语义来源；私有校验器若改名由集成测试兜住）
from mt5quant.backtest.engine_a import (
    BacktestResult,
    PROTECTIVE_REASONS,
    Trade,
    atr_series,
    _validate_bars,
    _validate_contract_size,
    _validate_rules,
    _validate_signals,
    _week_close_bars,
)

ENGINE_VERSION_B = "B-0.5"   # 2026-10-26 T25：执行语义对齐 A 层（M1）——
                             # reentry="after_signal_change"（blocked_dir 状态机）、
                             # ruined/ruined_at 爆仓标记、week_close_exit ≤ H1
                             # 校验（与 A 层同文案）。B-0.4 = T23 RiskSizing（M2）；
                             # B-0.3 = T28/M8 性能改写；更早见 git 历史


class TickCoverageError(RuntimeError):
    """tick 覆盖不足（需要的 bar 区间无 tick / 订单无法成交）——拒绝静默跳过。"""


_TICK_BATCH = 1 << 16   # T28：逐笔段的小批大小（tolist 批转换，约 5 MB 标量）


class CostsTick(Protocol):
    """B 层消费的成本方法（签名与 mt5quant.costs 聚合一致）。

    点差/滑点模型不参与 B 层成交——点差来自真实 tick，滑点用 slippage_price。
    """

    def commission_per_lot(self, symbol: str) -> float:
        """每手来回佣金（账户货币）。"""
        ...

    def overnight_fee(self, symbol: str, side: str,
                      from_ts: pd.Timestamp, to_ts: pd.Timestamp) -> float:
        """持仓 [from_ts, to_ts] 的隔夜费（每手，账户货币，恒 ≤0）。"""
        ...


# ------------------------------------------------ 按月流式 tick 拉取

def _month_windows(start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    """把毫秒区间 [start_ms, end_ms]（含两端）切成按月窗口，月边界向内钳制。"""
    if start_ms > end_ms:
        return []
    out: list[tuple[int, int]] = []
    first = pd.Timestamp(start_ms, unit="ms")
    m_start = pd.Timestamp(first.year, first.month, 1)
    while True:
        s = max(int(m_start.value // 1_000_000), start_ms)
        nxt = m_start + pd.offsets.MonthBegin(1)
        nxt_ms = int(nxt.value // 1_000_000)
        e = min(nxt_ms - 1, end_ms)
        out.append((s, e))
        if nxt_ms > end_ms:
            return out
        m_start = nxt


class _TickStream:
    """逐月窗口拉取并产出整块经验证的 (msc, bid, ask) numpy 数组；记录覆盖范围。

    T28/M8：校验（time_msc 不倒退、报价有限为正且 ask ≥ bid）按块向量化，
    与旧版逐笔判定逐位同判（同一谓词、同一首个坏样本上报）；跨月接续点
    仍逐块比末笔。不再为每笔 tick 构造 pd.Timestamp（成交时刻由引擎按需
    构造）。
    """

    def __init__(self, symbol: str, loader: Callable, start_ms: int, end_ms: int):
        self.symbol = symbol
        self._loader = loader
        self._windows = _month_windows(start_ms, end_ms)
        self._last_msc: int | None = None
        self.first_msc: int | None = None
        self.last_msc: int | None = None
        self.requested = (start_ms, end_ms)
        self.loaded_rows = 0

    def __iter__(self):
        for ws, we in self._windows:
            df = self._loader(start_msc=int(ws), end_msc=int(we))
            if df is None or len(df) == 0:
                continue
            missing = {"time_msc", "bid", "ask"} - set(df.columns)
            if missing:
                raise ValueError(
                    f"{self.symbol} tick 帧缺列：{sorted(missing)}"
                    f"（需 time_msc/bid/ask，参见 mt5data.ticks.TICK_COLUMNS）")
            msc_arr = df["time_msc"].to_numpy(dtype="int64")
            bid_arr = df["bid"].to_numpy(dtype="float64")
            ask_arr = df["ask"].to_numpy(dtype="float64")
            if msc_arr.size >= 2:
                bad = np.flatnonzero(np.diff(msc_arr) < 0)
                if bad.size:
                    i = int(bad[0])
                    raise ValueError(
                        f"{self.symbol} tick time_msc 倒退"
                        f"（{msc_arr[i]} → {msc_arr[i + 1]}），历史损坏，拒绝回测")
            if self._last_msc is not None and int(msc_arr[0]) < self._last_msc:
                raise ValueError(
                    f"{self.symbol} tick time_msc 倒退"
                    f"（{self._last_msc} → {int(msc_arr[0])}），历史损坏，拒绝回测")
            ok = (np.isfinite(bid_arr) & np.isfinite(ask_arr)
                  & (bid_arr > 0) & (ask_arr >= bid_arr))
            if not bool(ok.all()):
                i = int(np.flatnonzero(~ok)[0])
                raise ValueError(
                    f"{self.symbol} tick 报价非法（bid={bid_arr[i]!r}, "
                    f"ask={ask_arr[i]!r}，要求有限为正且 ask ≥ bid），拒绝回测")
            self._last_msc = int(msc_arr[-1])
            if self.first_msc is None:
                self.first_msc = int(msc_arr[0])
            self.last_msc = int(msc_arr[-1])
            self.loaded_rows += int(msc_arr.size)
            yield msc_arr, bid_arr, ask_arr


# ------------------------------------------------ 主入口

def run_backtest_ticks(strategy, bars: pd.DataFrame, ticks_loader, costs, *,
                       symbol: str, params: dict,
                       contract_size: float,
                       volume_lots: float | None = None,
                       initial_capital: float = 10_000.0,
                       timeframe: str | None = None,
                       latency_ms: int | float = 0,
                       slippage_price: float = 0.0,
                       session_policy=None,
                       sizing: RiskSizing | None = None,
                       vol_min: float | None = None,
                       vol_step: float | None = None,
                       vol_max: float | None = None) -> BacktestResult:
    """跑一次 B 层（tick 级）回测，语义详见模块 docstring。

    ticks_loader 契约：ticks_loader(start_msc=毫秒, end_msc=毫秒) →
    DataFrame（time_msc/bid/ask...，区间含两端）或 None（无库），
    生产接线 functools.partial(mt5data.ticks.load_ticks, root, symbol)。
    contract_size 必填（口径同 A 层）；latency_ms 为订单就绪延迟（毫秒，
    含边界）；slippage_price 为每腿额外不利滑点（价格单位）。
    timeframe 缺省取 strategy.required_data.timeframe（B 层必须有周期秒数）。
    session_policy（mt5data.sessions.SessionPolicy，缺省 None=不限制）：换日
    禁入窗（T20/B-0.2）。就绪订单若在禁入时段到达允许成交的 tick → 订单保留，
    等到第一个允许时刻的 tick 成交（等价 A 层"推迟到下一个允许的 bar 开盘"，
    粒度更细）；**平仓不受限**。禁入窗一直未开到数据末尾 → 订单作废（不算
    tick 覆盖问题，不抛 TickCoverageError）。与实盘风控共用 config/sessions.toml。
    定手数（T23/B-0.4）：volume_lots 与 sizing 二选一（同给/同缺都拒）；
    sizing 模式必须另给 vol_min/vol_step/vol_max（口径同 A 层 A-1.5）。
    执行语义（T25/B-0.5，与 A 层 A-1.3/A-1.5 对齐）：ExitRules.reentry=
    "after_signal_change" 时保护性出场后信号离开原方向前禁同向开仓；
    逐 bar 收盘权益 ≤ 0 → BacktestResult.ruined/ruined_at（粘滞）且不再
    开新仓；week_close_exit 用于 > H1 周期直接 ValueError（同 A 层文案）。
    """
    cs = _validate_contract_size(contract_size)
    if sizing is not None and volume_lots is not None:
        raise ValueError(
            "sizing 与 volume_lots 二选一：两者同时给出（"
            f"sizing={sizing!r}, volume_lots={volume_lots!r}）")
    if sizing is None and volume_lots is None:
        raise ValueError(
            "必须给出 volume_lots（固定手数）或 sizing（RiskSizing 按风险"
            " 定手数）之一——不提供默认手数：静默的 1 手黄金在 1 万本金下"
            "首次止损即爆仓（M2）")
    if sizing is not None:
        if vol_min is None or vol_step is None or vol_max is None:
            raise ValueError(
                "sizing 模式需要 vol_min/vol_step/vol_max（品种快照"
                " volume_min/step/max，scripts 侧用 mt5quant.sizing."
                "load_volume_limits 读取），缺 "
                f"(min={vol_min!r}, step={vol_step!r}, max={vol_max!r})")
        vol_bounds = validate_volume_bounds(vol_min, vol_step, vol_max)
    else:
        vol_bounds = None
        if not (isinstance(volume_lots, (int, float)) and not isinstance(volume_lots, bool)
                and math.isfinite(volume_lots) and volume_lots > 0):
            raise ValueError(f"volume_lots 必须是有限正数，收到 {volume_lots!r}")
    if not (isinstance(initial_capital, (int, float))
            and not isinstance(initial_capital, bool)
            and math.isfinite(initial_capital) and initial_capital > 0):
        raise ValueError(f"initial_capital 必须是有限正数，收到 {initial_capital!r}")
    if not (isinstance(latency_ms, (int, float)) and not isinstance(latency_ms, bool)
            and math.isfinite(latency_ms) and latency_ms >= 0):
        raise ValueError(f"latency_ms 必须是非负有限数，收到 {latency_ms!r}")
    if not (isinstance(slippage_price, (int, float))
            and not isinstance(slippage_price, bool)
            and math.isfinite(slippage_price) and slippage_price >= 0):
        raise ValueError(f"slippage_price 必须是非负有限数，收到 {slippage_price!r}")
    if timeframe is None:
        timeframe = strategy.required_data.timeframe
    tf_key = (timeframe or "").upper()
    if tf_key not in TIMEFRAME_SECONDS:
        raise ValueError(
            f"B 层需要有效 timeframe（TIMEFRAME_SECONDS 之一），得到 {timeframe!r}")
    tf_seconds = TIMEFRAME_SECONDS[tf_key]
    params = strategy.validate_params(dict(params))
    _validate_bars(bars)
    rules: ExitRules = strategy.exit_rules or ExitRules()
    _validate_rules(rules)
    sig = _validate_signals(strategy.generate_signals(bars, params), bars)
    sig_arr = sig.to_numpy()
    atr_arr = atr_series(bars).to_numpy()

    n = len(bars)
    times = [pd.Timestamp(t) for t in bars["time"]]
    t_ms = [int(t.value // 1_000_000) for t in times]          # bar 开盘（ms）
    close_ms = [t_ms[i] + tf_seconds * 1000 for i in range(n)]  # bar 收盘（ms）

    week_close_idx: frozenset = frozenset()
    if rules.week_close_exit:
        # T25（R8 对齐）：与 A 层同文案拒绝 > H1 的周期
        if tf_seconds > TIMEFRAME_SECONDS["H1"]:
            raise ValueError(
                f"week_close_exit 只允许 ≤ H1 的周期：按 bar 收盘时刻判定周五"
                f"收盘，D1/H4 会提前一整根平仓（D1 在周四收盘强平），得到 "
                f"{timeframe!r}")
        week_close_idx = _week_close_bars(times, tf_seconds)

    trades: list[Trade] = []
    state: dict | None = None      # 持仓；None = 空仓
    pending: dict | None = None    # {"desired": -1|0|1, "ready_ms": int}
    prev: tuple | None = None      # 已处理的最后一笔 tick (msc, bid, ask)
    last_closed = -1               # 最后已收盘 bar 的下标
    interval_need = False          # 当前 bar 区间内曾有持仓或待成交订单
    realized = 0.0
    skipped_zero_lots = 0          # T23：手数为 0 而作废的订单（含 ATR 缺）
    equity_vals = [0.0] * n
    trade_field_names = {f.name for f in fields(Trade)}
    # T25（R7 对齐）：after_signal_change 模式下保护性出场后的禁入方向
    # （±1；0=无禁入）。immediate 模式不设禁入（保持目标仓位语义）。
    blocked_dir = 0
    # T25（R16 对齐）：权益 ≤ 0 后不再开新仓；ruined 粘滞，ruined_at 记首次 bar
    ruined = False
    ruined_at: pd.Timestamp | None = None
    # T28：state 的热字段镜像（逐笔 SL/TP 判定免字典查找；只在
    # _open_at/_close_at 两个唯一变更点随 state 同步）
    pos_dir = 0
    pos_sl: float | None = None
    pos_tp: float | None = None

    def _bar_of(m: int) -> int:
        return bisect_right(t_ms, m) - 1

    def _r_multiple(net: float, st: dict) -> float:
        atr_v = st["entry_atr"]
        if math.isnan(atr_v) or atr_v <= 0:
            return float("nan")
        k = st["sl_k"] if st["sl_k"] is not None else 1.0
        return net / (k * atr_v * st["lots"] * cs)

    def _open_at(m: int, bid: float, ask: float, desired: int) -> None:
        """按成交 tick 开仓：多头 Ask 买入 / 空头 Bid 卖出，另加不利滑点。

        T28：pd.Timestamp 只在成交时构造（不再逐笔构造）。
        T23：sizing 模式按成交 tick 时点定手数（与 A 层同一函数）；手数为 0
        → 本笔订单作废并计数（调用方已清 pending，信号持续时下一次 bar
        收盘重新下单，与 A 层"逐 bar 重试"节奏一致）。
        """
        nonlocal state, pos_dir, pos_sl, pos_tp, skipped_zero_lots
        sig_bar = last_closed                 # SL/TP 用信号 bar 的 ATR（已收盘）
        entry_atr = float(atr_arr[sig_bar]) if sig_bar >= 0 else float("nan")
        if sizing is not None:
            if math.isnan(entry_atr):
                lots = 0.0                    # 无风险距离（ATR 回看不足）
            else:
                stop_k = (rules.stop_loss_atr if rules.stop_loss_atr is not None
                          else sizing.fallback_stop_atr)
                capital = (initial_capital if sizing.basis == "initial"
                           else initial_capital + realized)  # 成交时点不持仓
                lots = lots_for_risk(capital, sizing.risk_fraction,
                                     stop_k * entry_atr, cs,
                                     vol_bounds[0], vol_bounds[1], vol_bounds[2])
            if lots <= 0.0:
                skipped_zero_lots += 1
                return                        # 不开仓（订单作废已由调用方清掉）
        else:
            lots = volume_lots
        ts_ = pd.Timestamp(m, unit="ms")
        if desired == 1:
            entry_price = ask + slippage_price
            entry_spread = ask - bid          # 多头：价差拆分挂入场腿
        else:
            entry_price = bid - slippage_price
            entry_spread = None               # 空头：挂出场腿（_close_at 补记）
        sl = tp = None
        if not math.isnan(entry_atr):
            if rules.stop_loss_atr is not None:
                sl = (entry_price - rules.stop_loss_atr * entry_atr if desired == 1
                      else entry_price + rules.stop_loss_atr * entry_atr)
            if rules.take_profit_atr is not None:
                tp = (entry_price + rules.take_profit_atr * entry_atr if desired == 1
                      else entry_price - rules.take_profit_atr * entry_atr)
        state = {
            "dir": desired, "side": "long" if desired == 1 else "short",
            "lots": lots, "entry_price": entry_price, "entry_time": ts_,
            "entry_bar": _bar_of(m), "entry_slip": slippage_price,
            "entry_spread": entry_spread, "entry_atr": entry_atr,
            "sl_k": rules.stop_loss_atr, "sl": sl, "tp": tp,
        }
        pos_dir, pos_sl, pos_tp = desired, sl, tp

    def _close_at(m: int, bid: float, ask: float, reason: str) -> None:
        """按成交 tick 平仓：多头 Bid 卖出 / 空头 Ask 买入，另加不利滑点。"""
        nonlocal state, realized, pending, pos_dir, pos_sl, pos_tp, blocked_dir
        assert state is not None
        ts_ = pd.Timestamp(m, unit="ms")
        if state["dir"] == 1:
            exit_price = bid - slippage_price
            spread_charged = state["entry_spread"]
        else:
            exit_price = ask + slippage_price
            spread_charged = ask - bid        # 空头：价差拆分挂出场腿
        lots = state["lots"]
        gross = (exit_price - state["entry_price"]) * state["dir"] * lots * cs
        commission = costs.commission_per_lot(symbol) * lots
        # 隔夜费口径与 A 层一致：整段 (entry, exit] 交给模型按计费时刻数夜，
        # 每手口径必须 × 手数
        swap = costs.overnight_fee(
            symbol, state["side"], state["entry_time"], ts_) * lots
        net = gross - commission + swap
        kwargs = dict(
            entry_time=state["entry_time"], exit_time=ts_,
            side=state["side"], volume_lots=lots,
            entry_price=state["entry_price"], exit_price=exit_price,
            gross=gross, commission=commission,
            slippage_cost=(state["entry_slip"] + slippage_price) * lots * cs,
            spread_cost=spread_charged * lots * cs, swap=swap, net=net,
            r_multiple=_r_multiple(net, state),
            bars_held=_bar_of(m) - state["entry_bar"], exit_reason=reason)
        if "exit_fill_time" in trade_field_names:
            kwargs["exit_fill_time"] = ts_    # T7 字段：B 层即成交 tick 时刻
        trades.append(Trade(**kwargs))
        realized += net
        if (rules.reentry == "after_signal_change"
                and reason in PROTECTIVE_REASONS):
            # T25（R7，同 A 层判定）：保护性出场（B 层不会出现 stop_gap/
            # take_profit_gap）→ 禁同向再入场；immediate 模式不设禁入
            blocked_dir = state["dir"]
        state = None
        pos_dir, pos_sl, pos_tp = 0, None, None
        # 持仓已被保护性出场平掉时，同向的"平仓单"已成空单，立即撤销
        if pending is not None and pending["desired"] == 0:
            pending = None

    def _coverage_desc(stream: _TickStream) -> str:
        if stream.first_msc is None:
            a, b = stream.requested
            return ("无（未加载到任何 tick；请求区间 ["
                    f"{pd.Timestamp(a, unit='ms')} … "
                    f"{pd.Timestamp(b, unit='ms')}]）")
        return (f"{pd.Timestamp(stream.first_msc, unit='ms')} … "
                f"{pd.Timestamp(stream.last_msc, unit='ms')}"
                f"（共 {stream.loaded_rows} 笔）")

    def _close_bar(b: int, stream: _TickStream) -> None:
        """bar b 收盘（时刻 = t_b + 周期）：覆盖检查 → 收盘前最后一笔 tick 的
        bar 级出场 → 收盘市值（含爆仓判定）→ 由 sig[b] 生成/替换目标仓位订单。"""
        nonlocal state, pending, interval_need, prev, blocked_dir, ruined, ruined_at
        # 1) 覆盖检查：本 bar 区间曾有持仓/待成交订单却一笔 tick 都没有
        if interval_need and (prev is None or prev[0] < t_ms[b]):
            raise TickCoverageError(
                f"{symbol} tick 覆盖不足：bar 区间 [{times[b]}, "
                f"{times[b] + pd.Timedelta(seconds=tf_seconds)}) 内有持仓或"
                f"待成交订单但无任何 tick；已加载 tick 覆盖："
                f"{_coverage_desc(stream)}。请检查 tick 月分区数据"
                f"（scripts/download_ticks.py / migrate_ticks_layout.py）。")
        # 2) 收盘前最后一笔 tick：time_stop / week_close / 末根强平
        if state is not None and prev is not None:
            reason = None
            if (rules.time_stop_bars is not None
                    and b - state["entry_bar"] >= rules.time_stop_bars):
                reason = "time_stop"
            elif b in week_close_idx:
                reason = "week_close"
            if reason is None and b == n - 1:
                reason = "end_of_data"
            if reason is not None:
                _close_at(prev[0], prev[1], prev[2], reason)
        # 3) 收盘市值：多头按 Bid、空头按 Ask（该 bar 收盘前最后一笔 tick）
        unreal = 0.0
        if state is not None:
            mark = prev[1] if state["dir"] == 1 else prev[2]
            unreal = (mark - state["entry_price"]) * state["dir"] * state["lots"] * cs
        equity_vals[b] = initial_capital + realized + unreal
        if not ruined and equity_vals[b] <= 0:   # T25（R16）：爆仓（粘滞标记）
            ruined = True
            ruined_at = times[b]
        # 4) bar b 收盘 → 目标仓位订单（最后一根 bar 的信号永不执行，同 A 层）。
        # T25（R7/R16 对齐 A 层）：信号已离开禁入方向（变 0 或反向）→ 解禁；
        # 开仓单被同向禁入 / 爆仓拦下时不产生订单（平仓/反手单照常——平仓腿
        # 必须执行，反手单的开仓腿在成交时再裁决，同 A 层"先平后不开"）。
        if b < n - 1:
            desired = int(sig_arr[b])
            if blocked_dir and desired != blocked_dir:
                blocked_dir = 0
            cur = state["dir"] if state is not None else 0
            if desired == cur:
                pending = None
            elif (desired == 0 or cur != 0
                    or (not ruined and desired != blocked_dir)):
                pending = {"desired": desired,
                           "ready_ms": close_ms[b] + int(latency_ms)}
            else:
                pending = None    # 开仓被禁：不下单（空仓区间无引擎事件）
        else:
            if pending is not None and not (
                    session_policy is not None and stream.last_msc is not None
                    and pending["ready_ms"] <= stream.last_msc):
                # 就绪晚于末笔 tick（真覆盖不足）→ 报错；
                # 就绪早于末笔 tick 却未成交 → 只能是禁入窗未放开（T20）→ 作废
                raise TickCoverageError(
                    f"{symbol} tick 覆盖不足：订单（目标仓位 {pending['desired']}）"
                    f"就绪时刻 {pd.Timestamp(pending['ready_ms'], unit='ms')} "
                    f"晚于数据末尾可用 tick，无法成交；已加载 tick 覆盖："
                    f"{_coverage_desc(stream)}。")
            pending = None
        interval_need = (state is not None) or (pending is not None)

    stream = _TickStream(symbol, ticks_loader, close_ms[0], close_ms[n - 1] - 1)
    # T28/M8：按月块迭代。空仓且无待成交订单（interval_need=False）的区间
    # 没有任何引擎事件——searchsorted 直接跳到下一个 bar 边界后的首笔 tick，
    # 被跳过段的末笔照"逐笔消费"语义更新 prev（覆盖检查/收盘估值与逐笔
    # 循环逐位一致）；持仓/待成交区间逐笔处理——64k 小批 tolist 转 Python
    # 标量迭代（免去逐笔 numpy 标量提取），下一收盘时刻/方向/SL/TP 为
    # 循环局部变量（镜像见 pos_* 注释）。
    close_next = close_ms[0]   # 待处理的下一次 bar 收盘（ms）；None=全部收盘
    for msc_arr, bid_arr, ask_arr in stream:
        ntk = msc_arr.size
        i = 0
        while i < ntk:
            if state is None and pending is None and not interval_need:
                # 空闲：下一个事件只能是下一次 bar 收盘产生的新订单
                if close_next is None:
                    break                      # 全部 bar 已收盘：剩余 tick 无关
                j = int(np.searchsorted(msc_arr, close_next, side="left"))
                if j >= ntk:
                    if j > i:                  # 模拟逐笔消费到本块末笔
                        prev = (int(msc_arr[-1]), bid_arr[-1], ask_arr[-1])
                    i = ntk
                    break
                if j > i:
                    prev = (int(msc_arr[j - 1]), bid_arr[j - 1], ask_arr[j - 1])
                i = j
            end = min(i + _TICK_BATCH, ntk)
            for m, bid, ask in zip(msc_arr[i:end].tolist(),
                                   bid_arr[i:end].tolist(),
                                   ask_arr[i:end].tolist()):
                # bar 收盘边界（含两端）：先处理完边界再消费该 tick
                while close_next is not None and close_next <= m:
                    last_closed += 1
                    _close_bar(last_closed, stream)
                    close_next = (close_ms[last_closed + 1]
                                  if last_closed < n - 1 else None)
                # 订单成交：就绪时刻含边界。开仓订单遇禁入时段 → 保留待下一笔
                # 允许时刻的 tick（T20）；平仓单（desired=0）不受限。
                # T25（R7/R16 对齐）：同向禁入 / 爆仓时开仓腿跳过、订单作废
                #（反手平仓已在上面照常执行；信号持续时下一根 bar 收盘按新
                # 信号重新裁决——与 A 层逐 bar 的判定节奏一致）。
                entered = False
                if pending is not None and m >= pending["ready_ms"]:
                    desired = pending["desired"]
                    if state is not None and state["dir"] != desired:
                        _close_at(m, bid, ask, "signal")
                    if desired != 0:
                        if session_policy is None or session_policy.entry_allowed(
                                symbol, pd.Timestamp(m, unit="ms")):
                            if not ruined and desired != blocked_dir:
                                pending = None
                                _open_at(m, bid, ask, desired)
                                entered = True
                            else:
                                pending = None
                    else:
                        pending = None
                # SL/TP 逐 tick（入场 tick 自身不触发自己的止损；内联省一次
                # 逐笔函数调用与字典查找——T28）：多头按 Bid、空头按 Ask，
                # 止损优先
                if state is not None and not entered \
                        and (pos_sl is not None or pos_tp is not None):
                    if pos_dir == 1:
                        if pos_sl is not None and bid <= pos_sl:
                            _close_at(m, bid, ask, "stop")
                        elif pos_tp is not None and bid >= pos_tp:
                            _close_at(m, bid, ask, "take_profit")
                    else:
                        if pos_sl is not None and ask >= pos_sl:
                            _close_at(m, bid, ask, "stop")
                        elif pos_tp is not None and ask <= pos_tp:
                            _close_at(m, bid, ask, "take_profit")
                prev = (m, bid, ask)
                if state is not None or pending is not None:
                    interval_need = True
                i += 1
    # tick 耗尽：剩余 bar 依次收盘（空区间在此触发覆盖检查）
    while last_closed < n - 1:
        last_closed += 1
        _close_bar(last_closed, stream)
    assert state is None, "末根收盘必须强平（end_of_data）"

    trades_df = pd.DataFrame(
        [asdict(t) for t in trades],
        columns=[f.name for f in fields(Trade)],
    )
    equity = pd.Series(equity_vals, index=bars.index, name="equity")
    return BacktestResult(trades=trades_df, equity=equity, bars=bars,
                          params=params, symbol=symbol, timeframe=timeframe,
                          contract_size=cs,
                          volume_lots=(None if sizing is not None
                                       else float(volume_lots)),
                          initial_capital=float(initial_capital),
                          ruined=ruined, ruined_at=ruined_at,
                          skipped_zero_lots=skipped_zero_lots)
