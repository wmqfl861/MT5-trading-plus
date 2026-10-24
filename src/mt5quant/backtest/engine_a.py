"""A 层回测引擎（逐 bar 循环）：信号 bar 收盘 → 下一根 bar 开盘调仓，bid 基准成本模型。

货币口径（2026-10-25 修复，C1）：
- 价格单位与账户货币严格区分。contract_size = 每手每 1.0 价格变动对应的
  账户货币盈亏（报价货币 = 账户货币时即合约大小：XAUUSD 100、EURUSD
  100000、BTCUSD/USTEC 以经纪商规格为准，见 data/specs/latest.json）。
- gross / spread_cost / slippage_cost = 价格差 × 手数 × contract_size（账户货币）；
  commission = 每手来回佣金 × 手数；swap = 每手隔夜费 × 手数（账户货币）；
  net = gross − commission + swap；equity 与初始资金同为账户货币。

成交与成本模型（全部钉死，测试逐项手算对账）：
- bar 价格即 bid；ask = bid + 点差。costs.spread_p90 即点差全幅（p90），
  每个来回恰计一次，挂在"非 bid 一侧"的那条腿上（多头入场/空头出场）。
- 执行价（p 为 bid 口径的成交基准价，h 为执行 bar 开盘时刻的小时，
  spr=costs.spread_p90(symbol,h)、slip=costs.slippage(symbol,h)）：
      多头 entry = p + spr + slip    多头 exit = p − slip
      空头 entry = p − slip           空头 exit = p + spr + slip
  → 价差与滑点已含在成交价内；Trade.slippage_cost/spread_cost 仅作报告
    拆分，计算 net 时不再重复扣减。
- 成交基准价 p：信号调仓 = 下一根 bar 的 open；time_stop / week_close /
  end_of_data = 该 bar 的 close；SL/TP 见下。
- SL/TP 价位 = 入场成交价 ± 系数 × ATR14。ATR 取**信号 bar**（决策时点
  已收盘）的值；入场 bar 自身的 ATR 未收盘不可知。ATR 为 NaN（回看不足）
  → 不设 SL/TP，r_multiple 记 NaN。
- SL/TP 触发口径与 MT5 一致（H2 修复）：多头按 Bid 触发（bar 的 low/high），
  空头按 Ask 触发（bar 的 high/low + spr）。成交：多头 exit = 价位 − slip；
  空头 exit = 价位 + slip（价位本身就是 Ask 口径，不再另加点差）。
- 跳空穿价（H1 修复）：入场之后的 bar 若**开盘**已越过 SL（多头 open ≤ SL；
  空头 open+spr ≥ SL），按开盘价成交（exit_reason="stop_gap"，比止损价更差）；
  开盘已越过 TP 时按 TP 价成交（exit_reason="take_profit_gap"，保守不计跳空收益）。
- 同一根 bar 盘中同时触及 SL 与 TP → 止损优先，按止损价成交。
- bar 内处理顺序：开盘调仓（同 bar 反向先平后开）→ 开盘跳空检查 → SL → TP →
  time_stop（到期 bar 收盘价出）→ week_close（周五收盘价出）；
  入场当根 bar 同样适用 SL/TP 盘中检查（入场即开盘价，不存在跳空）。
- 保护性出场（SL/TP/time_stop/week_close）之后若信号仍指向同一方向，
  下一根 bar 开盘会重新入场——这是"目标仓位"语义；需要冷却的策略自行在
  信号里归 0（P5 计划提供 ExitRules 选项）。
- 隔夜费：整段 (entry_time, exit_time] 交给 overnight_fee 按计费时刻数夜
  （三倍日由成本模型内部计算），结果 × 手数。
- equity = 逐 bar 收盘市值：初始资金 + 已实现净利 + 持仓浮盈；多头按收盘
  Bid 估值，空头按收盘 Ask（close + spr）估值，与各自的平仓口径一致；
  未实现隔夜费不计，出场时一次入账。
- 数据末尾仍持仓 → 按末根 bar 收盘价强平，exit_reason="end_of_data"。
- 时间口径：bar.time 是经纪商服务器时间（库内约定贴 UTC 标签）；
  Exness 服务器为 GMT+0，故与 UTC 一致。换用非 GMT+0 经纪商前必须先
  处理时区（见 docs/blueprint.md「时间约定」）。
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
ENGINE_VERSION = "A-1.1"     # 2026-10-25：货币口径/跳空止损/空头 Ask 触发


class Costs(Protocol):
    """成本模型结构化接口——四个方法名与 mt5quant.costs 的产出逐字一致。"""

    def spread_p90(self, symbol: str, hour_utc: int) -> float:
        """该小时的点差全幅 p90（价格单位，每个来回恰计一次）。"""
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
    """单笔往返。价格字段为价格单位；金额字段（gross 起）一律为账户货币。"""

    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    side: str            # "long" | "short"
    volume_lots: float
    entry_price: float   # 实际成交价（已含点差/滑点）
    exit_price: float
    gross: float         # (exit−entry)×方向×手数×contract_size——已含价差与滑点损耗
    commission: float    # 来回佣金（每手来回 × 手数）
    slippage_cost: float  # 双边滑点金额（拆分用，已含在 gross 内）
    spread_cost: float   # 该来回的价差成本金额（拆分用，已含在 gross 内）
    swap: float          # 隔夜费（每手 × 手数，≤0）
    net: float           # gross − commission + swap
    r_multiple: float    # net / 初始风险金额（SL 距离；无 SL 时按 1×ATR）
    bars_held: int
    exit_reason: str     # signal|stop|stop_gap|take_profit|take_profit_gap|
                         # time_stop|week_close|end_of_data


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.Series
    bars: pd.DataFrame
    params: dict
    symbol: str
    timeframe: str
    contract_size: float | None = None   # 金额换算口径（写进 manifest）
    volume_lots: float | None = None
    initial_capital: float | None = None


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


def _validate_contract_size(contract_size) -> float:
    if isinstance(contract_size, bool) or not isinstance(contract_size, (int, float)):
        raise ValueError(f"contract_size 必须是正数，收到 {contract_size!r}")
    cs = float(contract_size)
    if not math.isfinite(cs) or cs <= 0:
        raise ValueError(f"contract_size 必须是有限正数，收到 {contract_size!r}")
    return cs


def run_backtest(strategy, bars: pd.DataFrame, costs, *, symbol: str, params: dict,
                 contract_size: float,
                 volume_lots: float = 1.0, initial_capital: float = 10_000.0,
                 timeframe: str | None = None) -> BacktestResult:
    """跑一次 A 层回测。

    contract_size 必填（无默认值，防止静默按 1 折算）：每手每 1.0 价格变动的
    账户货币盈亏；报价货币 = 账户货币时即品种合约大小
    （data/specs/latest.json 的 contract_size）。
    timeframe 未显式给出时取 strategy.required_data.timeframe；仅
    week_close_exit=True 时必须能对应 TIMEFRAME_SECONDS 的周期。
    """
    cs = _validate_contract_size(contract_size)
    if not (isinstance(volume_lots, (int, float)) and not isinstance(volume_lots, bool)
            and math.isfinite(volume_lots) and volume_lots > 0):
        raise ValueError(f"volume_lots 必须是有限正数，收到 {volume_lots!r}")
    if not (isinstance(initial_capital, (int, float)) and math.isfinite(initial_capital)
            and initial_capital > 0):
        raise ValueError(f"initial_capital 必须是有限正数，收到 {initial_capital!r}")
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
        """平仓。base_price 为 bid 口径基准价（空头出场另加 spr 变成 Ask）。"""
        nonlocal state, realized
        assert state is not None
        spr = costs.spread_p90(symbol, times[j].hour)
        slip = costs.slippage(symbol, times[j].hour)
        if state["dir"] == 1:
            exit_price = base_price - slip
        else:
            exit_price = base_price + spr + slip
        lots = state["lots"]
        gross = (exit_price - state["entry_price"]) * state["dir"] * lots * cs
        commission = costs.commission_per_lot(symbol) * lots
        # 整段 (entry, exit] 交给模型按计费时刻数夜：同日跨 charge_hour 也计，
        # 不再用"跨 UTC 日界"作代理（那会漏掉日内跨计费时刻的持仓）；
        # overnight_fee 是"每手"口径，必须 × 手数
        swap = costs.overnight_fee(
            symbol, state["side"], state["entry_time"], times[j]) * lots
        net = gross - commission + swap
        spread_charged = state["spread_charged"]
        if spread_charged is None:        # 空头：价差计在出场腿
            spread_charged = spr
        trades.append(Trade(
            entry_time=state["entry_time"], exit_time=times[j],
            side=state["side"], volume_lots=lots,
            entry_price=state["entry_price"], exit_price=exit_price,
            gross=gross, commission=commission,
            slippage_cost=(state["entry_slip"] + slip) * lots * cs,
            spread_cost=spread_charged * lots * cs, swap=swap, net=net,
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
        return net / (k * atr_v * st["lots"] * cs)   # 初始风险金额（账户货币）

    def _protective_exit(j: int) -> None:
        """开盘跳空 → 盘中 SL → 盘中 TP。多头按 Bid、空头按 Ask 触发（MT5 口径）。"""
        sl, tp = state["sl"], state["tp"]
        if sl is None and tp is None:
            return
        gap_check = j > state["entry_bar"]   # 入场当根以开盘价入场，不存在跳空
        if state["dir"] == 1:
            if gap_check and sl is not None and opens[j] <= sl:
                _close(j, opens[j], "stop_gap")          # 跳空穿止损：按开盘价（更差）
            elif gap_check and tp is not None and opens[j] >= tp:
                _close(j, tp, "take_profit_gap")         # 保守：按 TP 价，不计跳空收益
            elif sl is not None and lows[j] <= sl:       # 同 bar 双触：止损优先
                _close(j, sl, "stop")
            elif tp is not None and highs[j] >= tp:
                _close(j, tp, "take_profit")
            return
        spr = costs.spread_p90(symbol, times[j].hour)   # 空头按 Ask = Bid + spr 触发
        if gap_check and sl is not None and opens[j] + spr >= sl:
            _close(j, opens[j], "stop_gap")              # exit = open + spr + slip
        elif gap_check and tp is not None and opens[j] + spr <= tp:
            _close(j, tp - spr, "take_profit_gap")       # exit = tp + slip
        elif sl is not None and highs[j] + spr >= sl:
            _close(j, sl - spr, "stop")                  # exit = sl + slip
        elif tp is not None and lows[j] + spr <= tp:
            _close(j, tp - spr, "take_profit")           # exit = tp + slip

    for j in range(n):
        # 1) 开盘调仓：信号来自 bar j-1 的收盘
        if j > 0 and int(sig_arr[j - 1]) != (state["dir"] if state else 0):
            desired = int(sig_arr[j - 1])
            if state is not None:
                _close(j, opens[j], "signal")
            if desired != 0:
                _open(j, desired)
        # 2) 持仓检查（入场当根同样适用盘中 SL/TP）
        if state is not None:
            _protective_exit(j)
        if state is not None:
            if (rules.time_stop_bars is not None
                    and j - state["entry_bar"] >= rules.time_stop_bars):
                _close(j, closes[j], "time_stop")
            elif j in week_close_idx:
                _close(j, closes[j], "week_close")
        # 3) 收盘市值：多头按 Bid、空头按 Ask（与平仓口径一致）
        unreal = 0.0
        if state is not None:
            mark = closes[j]
            if state["dir"] == -1:
                mark = closes[j] + costs.spread_p90(symbol, times[j].hour)
            unreal = (mark - state["entry_price"]) * state["dir"] * state["lots"] * cs
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
                          params=params, symbol=symbol, timeframe=timeframe,
                          contract_size=cs, volume_lots=float(volume_lots),
                          initial_capital=float(initial_capital))
