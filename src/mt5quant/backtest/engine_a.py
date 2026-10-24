"""A 层向量化回测引擎：信号 bar 收盘 → 下一根 bar 开盘调仓，bid 基准成本模型。

成交与成本模型（全部钉死，测试逐项手算对账）：
- bar 价格即 bid；ask = bid + 点差。costs.spread_p90 即点差全幅（p90），
  每个来回恰计一次，挂在"非 bid 一侧"的那条腿上（多头入场/空头出场）。
- 执行价（p 为成交基准价，h 为执行 bar 开盘时刻的 UTC 小时，
  spr=costs.spread_p90(symbol,h)、slip=costs.slippage(symbol,h)）：
      多头 entry = p + spr + slip    多头 exit = p − slip
      空头 entry = p − slip           空头 exit = p + spr + slip
  → 价差与滑点已含在成交价内；Trade.slippage_cost/spread_cost 仅作报告
    拆分，计算 net 时不再重复扣减。
- 成交基准价 p：信号调仓 = 下一根 bar 的 open；SL/TP = 止损/止盈价；
  time_stop / week_close / end_of_data = 该 bar 的 close。
- SL/TP 价位 = 入场成交价 ± 系数 × ATR14。ATR 取**信号 bar**（决策时点
  已收盘）的值；入场 bar 自身的 ATR 未收盘不可知。ATR 为 NaN（回看不足）
  → 不设 SL/TP，r_multiple 记 NaN。
- 同一根 bar 同时触及 SL 与 TP → 止损优先，按止损价成交。
- 触发判定直接用 bar（bid）的 high/low 与价位比较，不加价差。
- bar 内处理顺序：开盘调仓（同 bar 反向先平后开）→ SL → TP →
  time_stop（到期 bar 收盘价出）→ week_close（周五收盘价出）；
  入场当根 bar 同样适用 SL/TP 盘中检查。
- 隔夜费：入场与出场跨 UTC 日期时，按 overnight_fee(symbol, side,
  entry_time, exit_time) 记一次（计费夜数/三倍周几由成本模型内部计算，
  引擎不重复数夜）；不跨日不计。
- equity = 逐 bar 收盘市值：初始资金 + 已实现净利 + 持仓浮盈
  （按收盘 bid 与成交 entry 之差；未实现隔夜费不计，出场时一次入账）。
- 数据末尾仍持仓 → 按末根 bar 收盘价强平，exit_reason="end_of_data"。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields
from datetime import datetime, time as dtime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from mt5data.store import TIMEFRAME_SECONDS
from mt5quant.strategies.base import ExitRules

NY_TZ = "America/New_York"   # 周五当地收盘（17:00）换算用的时区
_WEEK_CLOSE_LOCAL = dtime(17, 0)


class Costs(Protocol):
    """成本模型结构化接口——四个方法名与 mt5quant.costs 的产出逐字一致。"""

    def spread_p90(self, symbol: str, hour_utc: int) -> float:
        """该 UTC 小时的半点差（价格单位）。"""
        ...

    def slippage(self, symbol: str, hour_utc: int) -> float:
        """该 UTC 小时的滑点幅值（价格单位，恒为正、不利方向）。"""
        ...

    def commission_per_lot(self, symbol: str) -> float:
        """每手来回佣金（计价货币）。"""
        ...

    def overnight_fee(self, symbol: str, side: str,
                      from_ts: datetime, to_ts: datetime) -> float:
        """持仓 [from_ts, to_ts] 的隔夜费（计价货币/手，恒 ≤0）。"""
        ...


@dataclass(frozen=True)
class Trade:
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    side: str            # "long" | "short"
    volume_lots: float
    entry_price: float   # 实际成交价（已含点差/滑点）
    exit_price: float
    gross: float         # (exit−entry)×方向×手数——已含价差与滑点损耗
    commission: float    # 来回佣金
    slippage_cost: float  # 双边滑点（拆分用，已含在 gross 内）
    spread_cost: float   # 该来回的价差成本全幅（拆分用，已含在 gross 内）
    swap: float          # 隔夜费（成本模型口径，≤0）
    net: float           # gross − commission + swap
    r_multiple: float    # net / 初始风险（SL 距离；无 SL 时按 1×ATR）
    bars_held: int
    exit_reason: str     # signal|stop|take_profit|time_stop|week_close|end_of_data


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.Series
    bars: pd.DataFrame
    params: dict
    symbol: str
    timeframe: str


def _wilder(values: pd.Series, period: int) -> pd.Series:
    """Wilder 平滑：种子=前 period 个有效值的均值，其后按 (n-1)/n 递推。

    与 mt5mcp.indicators._wilder 同法（本地实现，本模块不 import mt5mcp）：
    把种子写入第 period 个有效位置、抹掉其前的原始值，再用
    ewm(alpha=1/period, adjust=False, ignore_na=True) 从种子起递推。
    """
    vals = values.astype("float64").copy()
    valid = vals.dropna()
    if len(valid) < period:
        return vals * float("nan")  # 数据不足，全部 NaN
    seed_idx = valid.index[period - 1]
    seed = valid.iloc[:period].mean()
    for i in range(period):
        vals[valid.index[i]] = float("nan")
    vals[seed_idx] = float(seed)
    return vals.ewm(alpha=1 / period, adjust=False, ignore_na=True).mean()


def atr_series(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR（Wilder）：TR = max(high−low, |high−prev_close|, |low−prev_close|)。

    种子=前 period 个 TR 的均值；与 mt5mcp.indicators.atr 数值一致
    （注意 MT5 内置 iATR 是简单平均，口径不同，见 mt5mcp.indicators 说明）。
    """
    highs = bars["high"].astype("float64")
    lows = bars["low"].astype("float64")
    closes = bars["close"].astype("float64")
    prev_close = closes.shift(1)
    tr = pd.concat(
        [highs - lows, (highs - prev_close).abs(), (lows - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return _wilder(tr, period)


def _validate_bars(bars: pd.DataFrame) -> None:
    missing = {"time", "open", "high", "low", "close"} - set(bars.columns)
    if missing:
        raise ValueError(f"bars 缺列：{sorted(missing)}")
    if len(bars) == 0:
        raise ValueError("bars 为空")
    t = pd.to_datetime(bars["time"])
    if not t.is_monotonic_increasing or bool(t.duplicated().any()):
        raise ValueError("bars.time 必须严格升序且无重复")


def _validate_signals(sig: pd.Series, bars: pd.DataFrame) -> pd.Series:
    if not isinstance(sig, pd.Series):
        raise ValueError("generate_signals 必须返回 pd.Series")
    if len(sig) != len(bars) or not sig.index.equals(bars.index):
        raise ValueError("信号长度或 index 必须与 bars 一致")
    bad = set(pd.unique(sig)) - {-1, 0, 1}
    if bad:
        raise ValueError(f"信号取值必须在 {{-1,0,1}} 内，出现：{sorted(bad, key=str)}")
    return sig


def _validate_rules(rules: ExitRules) -> None:
    if rules.stop_loss_atr is not None and not rules.stop_loss_atr > 0:
        raise ValueError(f"stop_loss_atr 必须为正：{rules.stop_loss_atr!r}")
    if rules.take_profit_atr is not None and not rules.take_profit_atr > 0:
        raise ValueError(f"take_profit_atr 必须为正：{rules.take_profit_atr!r}")
    if rules.time_stop_bars is not None and rules.time_stop_bars < 1:
        raise ValueError(f"time_stop_bars 必须 ≥1：{rules.time_stop_bars!r}")


def _week_close_bars(times: list, tf_seconds: int) -> frozenset:
    """标记"周五当地 17:00 收盘前最后一根已收盘 bar"的位置。

    bar j 入选当且仅当 close_j <= 本周五当地 17:00 < close_{j+1}
    （close = time + 周期秒数；DST 由 zoneinfo 处理）。
    """
    ny, utc = ZoneInfo(NY_TZ), ZoneInfo("UTC")
    closes_ts = [t + pd.Timedelta(seconds=tf_seconds) for t in times]
    out = set()
    for j in range(len(times) - 1):
        c, nxt = closes_ts[j], closes_ts[j + 1]
        local = c.replace(tzinfo=utc).astimezone(ny)
        days = 4 - local.weekday()  # 周一(0)→+4 … 周五(4)→0；周六→−1、周日→−2
        fri_local = datetime.combine(
            local.date() + timedelta(days=days), _WEEK_CLOSE_LOCAL, tzinfo=ny)
        cutoff = fri_local.astimezone(utc).replace(tzinfo=None)
        if c <= cutoff < nxt:
            out.add(j)
    return frozenset(out)


def run_backtest(strategy, bars: pd.DataFrame, costs, *, symbol: str, params: dict,
                 volume_lots: float = 1.0, initial_capital: float = 10_000.0,
                 timeframe: str | None = None) -> BacktestResult:
    """跑一次 A 层回测。

    timeframe 未显式给出时取 strategy.required_data.timeframe；仅
    week_close_exit=True 时必须能对应 TIMEFRAME_SECONDS 的周期。
    （timeframe 关键字是对计划签名的一个附加参数：报告的 Sharpe 年化
    需要周期信息，BacktestResult 也携带它。）
    """
    if timeframe is None:
        timeframe = strategy.required_data.timeframe
    params = strategy.validate_params(dict(params))
    _validate_bars(bars)
    rules: ExitRules = strategy.exit_rules or ExitRules()
    _validate_rules(rules)
    sig = _validate_signals(strategy.generate_signals(bars, params), bars)
    sig_arr = sig.to_numpy()
    atr_arr = atr_series(bars).to_numpy()

    n = len(bars)
    times = [pd.Timestamp(t) for t in bars["time"]]
    opens = bars["open"].to_numpy(dtype="float64")
    highs = bars["high"].to_numpy(dtype="float64")
    lows = bars["low"].to_numpy(dtype="float64")
    closes = bars["close"].to_numpy(dtype="float64")

    week_close_idx: frozenset = frozenset()
    if rules.week_close_exit:
        tf = timeframe.upper() if timeframe else ""
        if tf not in TIMEFRAME_SECONDS:
            raise ValueError(
                f"week_close_exit 需要有效 timeframe（TIMEFRAME_SECONDS），"
                f"得到 {timeframe!r}"
            )
        week_close_idx = _week_close_bars(times, TIMEFRAME_SECONDS[tf])

    trades: list[Trade] = []
    state: dict | None = None   # 持仓状态；None = 空仓
    realized = 0.0
    equity_vals = [0.0] * n

    def _close(j: int, base_price: float, reason: str) -> None:
        nonlocal state, realized
        assert state is not None
        spr = costs.spread_p90(symbol, times[j].hour)
        slip = costs.slippage(symbol, times[j].hour)
        if state["dir"] == 1:
            exit_price = base_price - slip
        else:
            exit_price = base_price + spr + slip
        lots = state["lots"]
        gross = (exit_price - state["entry_price"]) * state["dir"] * lots
        commission = costs.commission_per_lot(symbol) * lots
        # 整段 (entry, exit] 交给模型按计费时刻数夜：同日跨 charge_hour 也计，
        # 不再用"跨 UTC 日界"作代理（那会漏掉日内跨计费时刻的持仓）
        swap = costs.overnight_fee(
            symbol, state["side"], state["entry_time"], times[j])
        net = gross - commission + swap
        spread_charged = state["spread_charged"]
        if spread_charged is None:        # 空头：价差计在出场腿
            spread_charged = spr
        trades.append(Trade(
            entry_time=state["entry_time"], exit_time=times[j],
            side=state["side"], volume_lots=lots,
            entry_price=state["entry_price"], exit_price=exit_price,
            gross=gross, commission=commission,
            slippage_cost=(state["entry_slip"] + slip) * lots,
            spread_cost=spread_charged * lots, swap=swap, net=net,
            r_multiple=_r_multiple(net, state), bars_held=j - state["entry_bar"],
            exit_reason=reason))
        realized += net
        state = None

    def _open(j: int, desired: int) -> None:
        nonlocal state
        spr = costs.spread_p90(symbol, times[j].hour)
        slip = costs.slippage(symbol, times[j].hour)
        base = opens[j]
        if desired == 1:
            entry_price = base + spr + slip
            spread_charged: float | None = spr  # 多头：价差计在入场腿
        else:
            entry_price = base - slip
            spread_charged = None
        sig_bar = j - 1                       # SL/TP 用信号 bar 的 ATR（已收盘）
        entry_atr = float(atr_arr[sig_bar])
        if math.isnan(entry_atr):
            entry_atr = float("nan")
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
            "lots": volume_lots, "entry_price": entry_price, "entry_time": times[j],
            "entry_bar": j, "entry_slip": slip, "spread_charged": spread_charged,
            "entry_atr": entry_atr, "sl_k": rules.stop_loss_atr, "sl": sl, "tp": tp,
        }

    def _r_multiple(net: float, st: dict) -> float:
        atr_v = st["entry_atr"]
        if math.isnan(atr_v) or atr_v <= 0:
            return float("nan")
        k = st["sl_k"] if st["sl_k"] is not None else 1.0
        return net / (k * atr_v * st["lots"])

    for j in range(n):
        # 1) 开盘调仓：信号来自 bar j-1 的收盘
        if j > 0 and int(sig_arr[j - 1]) != (state["dir"] if state else 0):
            desired = int(sig_arr[j - 1])
            if state is not None:
                _close(j, opens[j], "signal")
            if desired != 0:
                _open(j, desired)
        # 2) 持仓盘内检查（入场当根同样适用）
        if state is not None:
            d = state["dir"]
            if d == 1:
                hit_sl = state["sl"] is not None and lows[j] <= state["sl"]
                hit_tp = state["tp"] is not None and highs[j] >= state["tp"]
            else:
                hit_sl = state["sl"] is not None and highs[j] >= state["sl"]
                hit_tp = state["tp"] is not None and lows[j] <= state["tp"]
            if hit_sl:  # 同 bar 双触：止损优先
                _close(j, state["sl"], "stop")
            elif hit_tp:
                _close(j, state["tp"], "take_profit")
            elif (rules.time_stop_bars is not None
                    and j - state["entry_bar"] >= rules.time_stop_bars):
                _close(j, closes[j], "time_stop")
            elif j in week_close_idx:
                _close(j, closes[j], "week_close")
        # 3) 收盘市值
        unreal = 0.0
        if state is not None:
            unreal = (closes[j] - state["entry_price"]) * state["dir"] * state["lots"]
        equity_vals[j] = initial_capital + realized + unreal

    if state is not None:  # 数据末尾强平
        _close(n - 1, closes[n - 1], "end_of_data")
        equity_vals[n - 1] = initial_capital + realized

    trades_df = pd.DataFrame(
        [asdict(t) for t in trades],
        columns=[f.name for f in fields(Trade)],
    )
    equity = pd.Series(equity_vals, index=bars.index, name="equity")
    return BacktestResult(trades=trades_df, equity=equity, bars=bars,
                          params=params, symbol=symbol, timeframe=timeframe)
