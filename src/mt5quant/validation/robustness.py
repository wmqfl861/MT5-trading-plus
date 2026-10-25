"""G2 稳健性关卡：一切"换个说法还成立吗"的变体重测（P4/T12）。

子检查（门槛数值全部来自 config/gates.toml，即蓝图附录 B）：
- cost_scaling：成本 ×1.5 仍正期望（×2 只记录备查，不计入通过）；
- param_plateau：参数邻域（每个参数 ±1 档，其余不动）≥70% 净盈利；
- yearly_segments：≥70% 年份盈利，且单一年份贡献 ≤ 总利润 50%；
- cross_symbol：通用逻辑 ≥2 个品种净利为正（可用品种不足 2 → 不适用）；
- random_entry_benchmark：随机入场蒙特卡洛（同交易数、同持仓时长分布、
  同出场规则——随机信号喂给**同一个引擎**，保护性出场照常触发），
  策略夏普须高于其 95 分位；T21e/M7 起入场时刻按策略实际入场的
  **纽约本地小时分布**抽样、多空比例与策略一致（旧实现全场随机 +
  五五开，基准被高成本小时压低，策略更容易胜出）；
- buy_hold_benchmark：纯多头策略夏普须高于同期买入持有（否则不适用）；
- drop_best_5pct：去掉净利最好的 5% 交易后仍盈利；
- delay_1_bar：全部信号延迟一根 bar 执行（更保守的成交时刻）仍盈利。

蒙特卡洛种子：np.random.default_rng(seed)，seed 由调用方传
stats.rng_seed(card_id)，同一卡可复现。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from mt5quant.backtest.engine_a import run_backtest
from mt5quant.backtest.report import bars_per_year, observed_bars_per_year
from mt5quant.sizing import RiskSizing, validate_volume_bounds
from mt5quant.strategies import BaseStrategy, BuyHold
from mt5quant.strategies.base import ExitRules

NY_TZ = ZoneInfo("America/New_York")   # 与点差分桶/换日窗同一时区（T20）


@dataclass(frozen=True)
class BacktestSpec:
    """关卡内反复回测的装配参数（engine_a.run_backtest 的具名集合）。

    T23：定手数口径二选一——volume_lots（固定手数）或 sizing（RiskSizing
    按风险定手数）+ vol_min/vol_step/vol_max 三元组；两者同给/同缺都拒
    （与引擎同一互斥规则，构造时即校验，绝不带错口径跑关卡）。sizing 模式
    下 G2 的成本放大、参数邻域、随机入场等一切复算自动沿用同一口径
    （run_engine 透传）。
    """

    strategy: object
    symbol: str
    timeframe: str
    contract_size: float
    volume_lots: float | None = 1.0
    initial_capital: float = 10_000.0
    sizing: RiskSizing | None = None
    vol_min: float | None = None
    vol_step: float | None = None
    vol_max: float | None = None

    def __post_init__(self):
        if self.sizing is not None and self.volume_lots is not None:
            raise ValueError(
                f"BacktestSpec：sizing 与 volume_lots 二选一（sizing="
                f"{self.sizing!r}, volume_lots={self.volume_lots!r}）——"
                "关卡复算必须与基准运行同一口径")
        if self.sizing is None and self.volume_lots is None:
            raise ValueError(
                "BacktestSpec：必须给出 volume_lots（固定手数）或 sizing"
                "（RiskSizing）之一")
        if self.sizing is not None:
            if (self.vol_min is None or self.vol_step is None
                    or self.vol_max is None):
                raise ValueError(
                    "BacktestSpec：sizing 模式需要 vol_min/vol_step/vol_max"
                    f"（缺 min={self.vol_min!r}, step={self.vol_step!r},"
                    f" max={self.vol_max!r}）")
            validate_volume_bounds(self.vol_min, self.vol_step, self.vol_max)


def run_engine(spec: BacktestSpec, bars: pd.DataFrame, costs, params: dict, *,
               strategy=None):
    """按 spec 跑一次 A 层（strategy 可覆盖，如 FixedSignals / BuyHold）。"""
    kwargs = dict(symbol=spec.symbol, params=dict(params),
                  contract_size=spec.contract_size,
                  initial_capital=spec.initial_capital, timeframe=spec.timeframe)
    if spec.sizing is not None:          # T23：sizing 口径透传（引擎内互斥校验）
        kwargs.update(sizing=spec.sizing, vol_min=spec.vol_min,
                      vol_step=spec.vol_step, vol_max=spec.vol_max)
    else:
        kwargs["volume_lots"] = spec.volume_lots
    return run_backtest(strategy or spec.strategy, bars, costs, **kwargs)


class ScaledCosts:
    """成本模型等比缩放包装：四个成本分量一律 ×factor（G2 成本放大）。"""

    def __init__(self, base, factor: float):
        if not factor > 0:
            raise ValueError(f"factor 必须为正，收到 {factor!r}")
        self._base, self._factor = base, float(factor)

    def spread_p90(self, symbol, hour_utc, *, price=None):
        return self._base.spread_p90(symbol, hour_utc, price=price) * self._factor

    def slippage(self, symbol, hour_utc, *, price=None):
        return self._base.slippage(symbol, hour_utc, price=price) * self._factor

    def commission_per_lot(self, symbol):
        return self._base.commission_per_lot(symbol) * self._factor

    def overnight_fee(self, symbol, side, from_ts, to_ts):
        return self._base.overnight_fee(symbol, side, from_ts, to_ts) * self._factor


class FixedSignals(BaseStrategy):
    """预生成信号替身：返回构造时固定的信号序列，复用给定出场规则。

    随机入场蒙特卡洛与"延迟一根 bar"检查都靠它把任意信号喂给引擎。
    """

    def __init__(self, values, *, exit_rules: ExitRules | None = None):
        self._values = np.asarray(values, dtype="int64")
        self.exit_rules = exit_rules or ExitRules()

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        if len(bars) != len(self._values):
            raise ValueError(
                f"信号长度 {len(self._values)} 与 bars 长度 {len(bars)} 不符"
                "——FixedSignals 只能配构造时的同一 bars")
        return pd.Series(self._values, index=bars.index, dtype="int64")


def equity_sharpe(equity, times, timeframe: str) -> float | None:
    """权益曲线的年化 Sharpe（与 report.summarize 同口径：实测每年 bar 数，
    跨度不足退回日历口径）；无方差/样本不足 → None。"""
    returns = pd.Series(equity).astype("float64").pct_change().dropna()
    if len(returns) < 2:
        return None
    bpy = observed_bars_per_year(times) or bars_per_year(timeframe)
    std = float(returns.std(ddof=1))
    if not std > 0:
        return None
    return float(returns.mean()) / std * math.sqrt(bpy)


def _net_sum(result) -> float:
    return float(result.trades["net"].sum()) if len(result.trades) else 0.0


def _expectancy(result) -> float | None:
    return float(result.trades["net"].mean()) if len(result.trades) else None


# ---------------------------------------------------------------- 子检查

def cost_scaling_check(spec, bars, costs, params, cfg) -> dict:
    """成本 ×1.5（门槛倍数）须仍为正期望；×2（备查倍数）只记录。"""
    r15 = run_engine(spec, bars, ScaledCosts(costs, cfg.cost_scale_pass), params)
    r2 = run_engine(spec, bars, ScaledCosts(costs, cfg.cost_scale_record), params)
    e15 = _expectancy(r15)
    return {
        "passed": bool(e15 is not None and e15 > 0),
        "scale_pass": float(cfg.cost_scale_pass),
        "expectancy_1_5x": e15,
        "scale_record": float(cfg.cost_scale_record),
        "net_2x": _net_sum(r2),
        "record_only": True,                      # ×2 不计入通过判定
    }


def param_plateau(spec, bars, costs, params, grid, cfg) -> dict:
    """参数邻域：每个参数沿卡网格移动 ±1 档（其余不动），统计净盈利占比。"""
    neighbors: list[dict] = []
    for name, values in grid.items():
        current = params.get(name)
        if current not in values:
            return {
                "passed": False, "n_neighbors": 0, "fraction": None,
                "neighbors": [],
                "reason": (f"参数 {name}={current!r} 不在卡网格 "
                           f"{list(values)} 内，邻域无从谈起"),
            }
        i = list(values).index(current)
        for j in (i - 1, i + 1):
            if 0 <= j < len(values):
                neighbors.append({**params, name: values[j]})
    if not neighbors:
        return {"passed": True, "n_neighbors": 0, "fraction": None,
                "neighbors": [], "note": "网格每维只有 1 档，无邻近组合"}
    nets = []
    for combo in neighbors:
        nets.append((combo, _net_sum(run_engine(spec, bars, costs, combo))))
    fraction = float(sum(1 for _, net in nets if net > 0) / len(nets))
    return {
        "passed": bool(fraction >= cfg.plateau_min_fraction),
        "n_neighbors": len(nets),
        "fraction": fraction,
        "min_fraction": float(cfg.plateau_min_fraction),
        "neighbors": [{"params": c, "net": float(n)} for c, n in nets],
    }


def yearly_segments(trades: pd.DataFrame, cfg) -> dict:
    """按（出场）年份分段：盈利年份占比 + 单年集中度。"""
    if not len(trades):
        return {"passed": False, "by_year": {}, "reason": "无交易"}
    years = pd.to_datetime(trades["exit_time"]).dt.year
    by_year = {str(y): float(g["net"].sum())
               for y, g in trades.groupby(years)}
    total = float(sum(by_year.values()))
    if total <= 0:
        return {"passed": False, "by_year": by_year,
                "reason": f"总净利 {total:.2f} ≤ 0"}
    profitable = [v for v in by_year.values() if v > 0]
    fraction = float(len(profitable) / len(by_year))
    concentration = float(max(by_year.values()) / total)
    return {
        "passed": bool(fraction >= cfg.yearly_min_fraction
                       and concentration <= cfg.yearly_max_concentration),
        "by_year": by_year,
        "profitable_fraction": fraction,
        "min_fraction": float(cfg.yearly_min_fraction),
        "max_concentration": concentration,
        "max_concentration_limit": float(cfg.yearly_max_concentration),
    }


def drop_best_check(trades: pd.DataFrame, cfg) -> dict:
    """去掉净利最好的 ⌈5%⌉（至少 1 笔）交易后总净利仍 > 0。"""
    n = len(trades)
    k = max(1, int(math.ceil(cfg.drop_best_fraction * n))) if n else 0
    if n < 2 or k >= n:
        return {"passed": False, "dropped": 0,
                "reason": f"交易数 {n} 不足以做去最好 {cfg.drop_best_fraction:.0%} 检查"}
    nets = trades["net"].sort_values(ascending=False).tolist()
    remaining = float(sum(nets[k:]))
    return {"passed": bool(remaining > 0), "dropped": k, "trade_count": n,
            "total_net": float(sum(nets)), "remaining_net": remaining}


def delay_check(spec, bars, costs, params, cfg) -> dict:
    """全部信号延迟一根 bar 执行（d[i]=s[i−1]）：仍须净盈利。"""
    strategy = spec.strategy
    sig = strategy.generate_signals(bars, params)
    values = sig.to_numpy()
    delayed = np.concatenate(([0], values[:-1])).astype("int64")
    fixed = FixedSignals(delayed, exit_rules=strategy.exit_rules)
    net_delayed = _net_sum(run_engine(spec, bars, costs, {}, strategy=fixed))
    net_original = _net_sum(run_engine(spec, bars, costs, params))
    return {"passed": bool(net_delayed > 0),
            "net_original": net_original, "net_delayed": net_delayed}


def buy_hold_check(spec, bars, costs, trades, base_sharpe, cfg) -> dict:
    """纯多头策略（全部交易 side==long）的夏普须高于同期买入持有。"""
    if not len(trades) or not bool((trades["side"] == "long").all()):
        return {"passed": True, "applicable": False,
                "note": "非纯多头策略，买入持有基准不适用"}
    bh = run_engine(spec, bars, costs, {}, strategy=BuyHold())
    bh_sharpe = equity_sharpe(bh.equity, bars["time"], spec.timeframe)
    passed = bool(base_sharpe is not None and bh_sharpe is not None
                  and base_sharpe > bh_sharpe)
    return {"passed": passed, "applicable": True,
            "base_sharpe": (float(base_sharpe) if base_sharpe is not None
                            else None),
            "buy_hold_sharpe": bh_sharpe}


def cross_symbol_check(strategy, bars_by_symbol, costs_by_symbol,
                       sizes_by_symbol, params, timeframe, volume_lots,
                       initial_capital, cfg, *, sizing=None,
                       vols_by_symbol=None) -> dict:
    """同一逻辑在 ≥ cross_symbol_min 个品种上净利为正。

    T23：sizing 模式下各品种用自己的 vol 三元组（vols_by_symbol[sym] =
    (vol_min, vol_step, vol_max)，scripts 侧从快照读取）；缺品种即拒——
    绝不借用别的品种的手数上下限。固定手数模式沿用 volume_lots。
    """
    symbols = sorted(bars_by_symbol)
    if len(symbols) < cfg.cross_symbol_min:
        return {"passed": True, "applicable": False,
                "positive_symbols": 0, "evaluated": len(symbols),
                "note": f"仅 {len(symbols)} 个品种可用，跨品种检查不适用"}
    nets = {}
    for sym in symbols:
        if sizing is not None:
            vols = (vols_by_symbol or {}).get(sym)
            if vols is None:
                raise ValueError(
                    f"跨品种检查缺 {sym} 的 vol 三元组（vol_min/step/max）——"
                    "sizing 模式下每个品种必须有自己的快照手数上下限")
            spec = BacktestSpec(strategy=strategy, symbol=sym,
                                timeframe=timeframe,
                                contract_size=sizes_by_symbol[sym],
                                volume_lots=None,
                                initial_capital=initial_capital,
                                sizing=sizing, vol_min=vols[0],
                                vol_step=vols[1], vol_max=vols[2])
        else:
            spec = BacktestSpec(strategy=strategy, symbol=sym,
                                timeframe=timeframe,
                                contract_size=sizes_by_symbol[sym],
                                volume_lots=volume_lots,
                                initial_capital=initial_capital)
        nets[sym] = _net_sum(run_engine(spec, bars_by_symbol[sym],
                                        costs_by_symbol[sym], params))
    positive = sum(1 for v in nets.values() if v > 0)
    return {"passed": bool(positive >= cfg.cross_symbol_min),
            "applicable": True, "evaluated": len(symbols),
            "positive_symbols": int(positive),
            "min_positive": int(cfg.cross_symbol_min),
            "net_by_symbol": nets}


def ny_local_hours(times) -> np.ndarray:
    """时间序列（naive UTC 或 aware）→ 纽约本地小时数组（M7 抽样口径，
    与 T20 点差分桶同时区）。"""
    s = pd.to_datetime(pd.Series(times))
    if s.dt.tz is None:
        s = s.dt.tz_localize("UTC")
    return s.dt.tz_convert(NY_TZ).dt.hour.to_numpy(dtype="int64")


def _random_signal_series(n: int, n_trades: int, durations: np.ndarray,
                          rng, *, positions_by_hour: dict[int, np.ndarray],
                          hour_probs: np.ndarray, long_prob: float):
    """随机 ±1 入场（T21e/M7 口径）：

    - 入场 bar 先按策略实际入场的纽约本地小时分布（hour_probs）抽小时，
      再在该小时的候选 bar（positions_by_hour）里均匀抽一根；
    - 方向按 long_prob（策略的多头占比）抽，不再五五开；
    - 持仓时长仍从策略的经验分布抽取；互不重叠（中间隔至少 1 根 0）；
    - 某小时候选耗尽/重叠放不下 → 放弃该次尝试重抽（保持小时口径，
      不回退到全场随机）；重试上限内放不满则如实少放（placed < n_trades）。

    返回 (sig, 入场小时列表, 方向列表)——诊断统计用。
    """
    sig = np.zeros(n, dtype=np.int64)
    placed = 0
    hours_out: list[int] = []
    sides_out: list[int] = []
    max_attempts = 60 * n_trades + 200
    attempts = 0
    while placed < n_trades and attempts < max_attempts:
        attempts += 1
        d = max(1, int(durations[int(rng.integers(0, len(durations)))]))
        h = int(rng.choice(len(hour_probs), p=hour_probs))
        pool = positions_by_hour.get(h)
        if pool is None or len(pool) == 0:
            continue
        start = int(pool[int(rng.integers(0, len(pool)))])
        end = start + d
        if end + 1 > n:
            continue
        if sig[max(0, start - 1):end + 1].any():
            continue
        side = 1 if rng.random() < long_prob else -1
        sig[start:end] = side
        placed += 1
        hours_out.append(h)
        sides_out.append(side)
    return sig, hours_out, sides_out


def random_entry_check(spec, bars, costs, base_trades, base_sharpe, *,
                       sims: int, percentile: float, seed: int) -> dict:
    """随机入场蒙特卡洛：同交易数、同持仓时长分布、同出场规则（随机信号
    喂给同一个 A 层引擎，保护性出场照常触发）；策略夏普须高于分位阈值。

    T21e/M7：随机入场不再是"任意时刻 + 五五开"——入场时刻按策略实际
    入场的纽约本地小时分布抽样、多空比例与策略一致，基准与策略在同一
    时段结构与方向结构上比较（不被高成本小时或方向错配压低）。
    """
    if not len(base_trades) or base_sharpe is None:
        return {"passed": False, "valid_sims": 0, "p95": None,
                "reason": "无交易或夏普无定义，无法对比随机入场基准"}
    durations = base_trades["bars_held"].to_numpy()
    n_trades = len(base_trades)
    exit_rules = spec.strategy.exit_rules
    rng = np.random.default_rng(seed)

    # 策略实际入场的纽约本地小时分布 + 多空比例（M7 抽样口径）
    base_hours = ny_local_hours(base_trades["entry_time"])
    hour_counts = np.bincount(base_hours, minlength=24).astype("float64")
    hour_probs = hour_counts / hour_counts.sum()
    bar_hours = ny_local_hours(bars["time"])
    positions_by_hour: dict[int, np.ndarray] = {}
    for h in np.flatnonzero(hour_counts > 0):
        positions_by_hour[int(h)] = np.flatnonzero(bar_hours == h)
    long_prob = float((base_trades["side"] == "long").mean())

    sharpes: list[float] = []
    sampled_hour_counts = np.zeros(24, dtype="int64")
    sampled_longs = 0
    sampled_total = 0
    for _ in range(int(sims)):
        sig, hours_out, sides_out = _random_signal_series(
            len(bars), n_trades, durations, rng,
            positions_by_hour=positions_by_hour, hour_probs=hour_probs,
            long_prob=long_prob)
        for h, s in zip(hours_out, sides_out):
            sampled_hour_counts[h] += 1
            sampled_longs += 1 if s == 1 else 0
        sampled_total += len(sides_out)
        fixed = FixedSignals(sig, exit_rules=exit_rules)
        r = run_engine(spec, bars, costs, {}, strategy=fixed)
        s = equity_sharpe(r.equity, bars["time"], spec.timeframe)
        if s is not None:
            sharpes.append(s)
    if len(sharpes) < max(1, sims // 2):
        return {"passed": False, "valid_sims": len(sharpes), "p95": None,
                "sims": int(sims),
                "reason": "蒙特卡洛有效样本不足（随机入场大多无方差）"}
    arr = np.asarray(sharpes)
    threshold = float(np.percentile(arr, percentile))
    return {
        "passed": bool(base_sharpe > threshold),
        "base_sharpe": float(base_sharpe),
        "threshold_percentile": float(percentile),
        "p95": float(np.percentile(arr, 95)),
        "p5": float(np.percentile(arr, 5)),
        "mean": float(arr.mean()),
        "valid_sims": len(sharpes),
        "sims": int(sims),
        "target_trades": int(n_trades),
        "seed": int(seed),
        # M7 口径：抽样分布与策略实际入场的对账证据（写进关卡 details）
        "base_entry_hours_ny": [int(h) for h in np.flatnonzero(hour_counts)],
        "base_long_fraction": long_prob,
        "sampled_hours_ny": {str(h): int(sampled_hour_counts[h])
                             for h in np.flatnonzero(sampled_hour_counts)},
        "sampled_long_fraction": (float(sampled_longs / sampled_total)
                                  if sampled_total else None),
        "sampled_trades": int(sampled_total),
        # 原始分布（G5 的区间检查用）；写入台账前应 pop 掉（凭 seed 可复现）
        "sharpes": [float(x) for x in sharpes],
    }


# ---------------------------------------------------------------- 组合

def run_g2(spec, bars, costs, params, *, base_result, base_summary, grid,
           bars_by_symbol, costs_by_symbol, sizes_by_symbol, cfg,
           seed: int, vols_by_symbol: dict | None = None) -> dict:
    """G2 全部子检查（顺序执行，互不短路；details 全部落台账）。

    T23：spec 带 sizing 时全部复算（成本放大、参数邻域、随机入场、延迟、
    买入持有）经 run_engine 自动沿用同一按风险定手数口径；跨品种子检查
    需要 vols_by_symbol（各品种 vol 三元组，bt.py 装配时从快照读取）。
    """
    checks: dict[str, dict] = {}
    checks["cost_scaling"] = cost_scaling_check(spec, bars, costs, params, cfg)
    checks["param_plateau"] = param_plateau(spec, bars, costs, params, grid, cfg)
    checks["yearly_segments"] = yearly_segments(base_result.trades, cfg)
    checks["cross_symbol"] = cross_symbol_check(
        spec.strategy, bars_by_symbol, costs_by_symbol, sizes_by_symbol,
        params, spec.timeframe, spec.volume_lots, spec.initial_capital, cfg,
        sizing=spec.sizing, vols_by_symbol=vols_by_symbol)
    base_sharpe = base_summary.get("sharpe")
    checks["random_entry_benchmark"] = random_entry_check(
        spec, bars, costs, base_result.trades, base_sharpe,
        sims=cfg.random_entry_sims, percentile=cfg.random_entry_percentile,
        seed=seed)
    checks["random_entry_benchmark"].pop("sharpes", None)
    checks["buy_hold_benchmark"] = buy_hold_check(
        spec, bars, costs, base_result.trades, base_sharpe, cfg)
    checks["drop_best_5pct"] = drop_best_check(base_result.trades, cfg)
    checks["delay_1_bar"] = delay_check(spec, bars, costs, params, cfg)
    return {"passed": bool(all(c["passed"] for c in checks.values())),
            "checks": checks}
