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
- bars_held = 出场 bar 序号 − 入场 bar 序号（成交 tick 所在 bar 区间），
  与 A 层口径一致。保护性出场后同向信号是否再入场沿用 A 层"目标仓位"
  语义（下一根 bar 收盘后重新下单；ExitRules.reentry 若落地由接线层处理）。
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
- 本引擎是正确性优先的参考实现（逐 tick Python 循环）；向量化/事件折叠
  等优化留待 T10 对账通过之后。
"""
from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import asdict, fields
from typing import Callable, Protocol

import pandas as pd

from mt5data.store import TIMEFRAME_SECONDS
from mt5quant.strategies.base import ExitRules
# 复用 A 层的公开类型与校验器（单一语义来源；私有校验器若改名由集成测试兜住）
from mt5quant.backtest.engine_a import (
    BacktestResult,
    Trade,
    atr_series,
    _validate_bars,
    _validate_contract_size,
    _validate_rules,
    _validate_signals,
    _week_close_bars,
)

ENGINE_VERSION_B = "B-0.2"   # 2026-10-26 T20：SessionPolicy 禁入窗推迟开仓
                             # （订单保留到第一个允许时刻的 tick；平仓不受限）


class TickCoverageError(RuntimeError):
    """tick 覆盖不足（需要的 bar 区间无 tick / 订单无法成交）——拒绝静默跳过。"""


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
    """逐月窗口拉取并产出 (msc, bid, ask, Timestamp)；记录已加载覆盖范围。"""

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
            for i in range(len(msc_arr)):
                m = int(msc_arr[i])
                b, a = float(bid_arr[i]), float(ask_arr[i])
                if self._last_msc is not None and m < self._last_msc:
                    raise ValueError(
                        f"{self.symbol} tick time_msc 倒退"
                        f"（{self._last_msc} → {m}），历史损坏，拒绝回测")
                if not (math.isfinite(b) and math.isfinite(a) and b > 0
                        and a >= b):
                    raise ValueError(
                        f"{self.symbol} tick 报价非法（bid={b!r}, ask={a!r}，"
                        "要求有限为正且 ask ≥ bid），拒绝回测")
                self._last_msc = m
                if self.first_msc is None:
                    self.first_msc = m
                self.last_msc = m
                self.loaded_rows += 1
                yield (m, b, a, pd.Timestamp(m, unit="ms"))


# ------------------------------------------------ 主入口

def run_backtest_ticks(strategy, bars: pd.DataFrame, ticks_loader, costs, *,
                       symbol: str, params: dict,
                       contract_size: float,
                       volume_lots: float = 1.0,
                       initial_capital: float = 10_000.0,
                       timeframe: str | None = None,
                       latency_ms: int | float = 0,
                       slippage_price: float = 0.0,
                       session_policy=None) -> BacktestResult:
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
    """
    cs = _validate_contract_size(contract_size)
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
        week_close_idx = _week_close_bars(times, tf_seconds)

    trades: list[Trade] = []
    state: dict | None = None      # 持仓；None = 空仓
    pending: dict | None = None    # {"desired": -1|0|1, "ready_ms": int}
    prev: tuple | None = None      # 已处理的最后一笔 tick (msc, bid, ask, ts)
    last_closed = -1               # 最后已收盘 bar 的下标
    interval_need = False          # 当前 bar 区间内曾有持仓或待成交订单
    realized = 0.0
    equity_vals = [0.0] * n
    trade_field_names = {f.name for f in fields(Trade)}

    def _bar_of(m: int) -> int:
        return bisect_right(t_ms, m) - 1

    def _r_multiple(net: float, st: dict) -> float:
        atr_v = st["entry_atr"]
        if math.isnan(atr_v) or atr_v <= 0:
            return float("nan")
        k = st["sl_k"] if st["sl_k"] is not None else 1.0
        return net / (k * atr_v * st["lots"] * cs)

    def _open_at(tk: tuple, desired: int) -> None:
        """按成交 tick 开仓：多头 Ask 买入 / 空头 Bid 卖出，另加不利滑点。"""
        nonlocal state
        m, bid, ask, ts_ = tk
        if desired == 1:
            entry_price = ask + slippage_price
            entry_spread = ask - bid          # 多头：价差拆分挂入场腿
        else:
            entry_price = bid - slippage_price
            entry_spread = None               # 空头：挂出场腿（_close_at 补记）
        sig_bar = last_closed                 # SL/TP 用信号 bar 的 ATR（已收盘）
        entry_atr = float(atr_arr[sig_bar]) if sig_bar >= 0 else float("nan")
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
            "lots": volume_lots, "entry_price": entry_price, "entry_time": ts_,
            "entry_bar": _bar_of(m), "entry_slip": slippage_price,
            "entry_spread": entry_spread, "entry_atr": entry_atr,
            "sl_k": rules.stop_loss_atr, "sl": sl, "tp": tp,
        }

    def _close_at(tk: tuple, reason: str) -> None:
        """按成交 tick 平仓：多头 Bid 卖出 / 空头 Ask 买入，另加不利滑点。"""
        nonlocal state, realized, pending
        assert state is not None
        m, bid, ask, ts_ = tk
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
        state = None
        # 持仓已被保护性出场平掉时，同向的"平仓单"已成空单，立即撤销
        if pending is not None and pending["desired"] == 0:
            pending = None

    def _protective(tk: tuple) -> None:
        """逐 tick SL/TP：多头按 Bid、空头按 Ask 触发（MT5 口径），止损优先。"""
        _, bid, ask, _ = tk
        sl, tp = state["sl"], state["tp"]
        if sl is None and tp is None:
            return
        if state["dir"] == 1:
            if sl is not None and bid <= sl:
                _close_at(tk, "stop")
            elif tp is not None and bid >= tp:
                _close_at(tk, "take_profit")
        else:
            if sl is not None and ask >= sl:
                _close_at(tk, "stop")
            elif tp is not None and ask <= tp:
                _close_at(tk, "take_profit")

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
        bar 级出场 → 收盘市值 → 由 sig[b] 生成/替换目标仓位订单。"""
        nonlocal state, pending, interval_need, prev
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
                _close_at(prev, reason)
        # 3) 收盘市值：多头按 Bid、空头按 Ask（该 bar 收盘前最后一笔 tick）
        unreal = 0.0
        if state is not None:
            mark = prev[1] if state["dir"] == 1 else prev[2]
            unreal = (mark - state["entry_price"]) * state["dir"] * state["lots"] * cs
        equity_vals[b] = initial_capital + realized + unreal
        # 4) bar b 收盘 → 目标仓位订单（最后一根 bar 的信号永不执行，同 A 层）
        if b < n - 1:
            desired = int(sig_arr[b])
            cur = state["dir"] if state is not None else 0
            pending = ({"desired": desired, "ready_ms": close_ms[b] + int(latency_ms)}
                       if desired != cur else None)
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
    for tk in stream:
        # bar 收盘边界（含两端）：先处理完边界再消费该 tick
        while last_closed < n - 1 and close_ms[last_closed + 1] <= tk[0]:
            last_closed += 1
            _close_bar(last_closed, stream)
        # 订单成交：就绪时刻含边界。开仓订单遇禁入时段 → 保留待下一笔
        # 允许时刻的 tick（T20）；平仓单（desired=0）不受限
        entered = False
        if pending is not None and tk[0] >= pending["ready_ms"]:
            desired = pending["desired"]
            if state is not None and state["dir"] != desired:
                _close_at(tk, "signal")
            if desired != 0:
                if session_policy is None or session_policy.entry_allowed(
                        symbol, tk[3]):
                    pending = None
                    _open_at(tk, desired)
                    entered = True
            else:
                pending = None
        # SL/TP 逐 tick（入场 tick 自身不触发自己的止损）
        if state is not None and not entered:
            _protective(tk)
        prev = tk
        if state is not None or pending is not None:
            interval_need = True
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
                          contract_size=cs, volume_lots=float(volume_lots),
                          initial_capital=float(initial_capital))
