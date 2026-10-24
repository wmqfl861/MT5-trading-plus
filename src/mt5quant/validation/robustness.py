"""G2 稳健性关卡：一切"换个说法还成立吗"的变体重测（P4/T12）。

子检查（门槛数值全部来自 config/gates.toml，即蓝图附录 B）：
- cost_scaling：成本 ×1.5 仍正期望（×2 只记录备查，不计入通过）；
- param_plateau：参数邻域（每个参数 ±1 档，其余不动）≥70% 净盈利；
- yearly_segments：≥70% 年份盈利，且单一年份贡献 ≤ 总利润 50%；
- cross_symbol：通用逻辑 ≥2 个品种净利为正（可用品种不足 2 → 不适用）；
- random_entry_benchmark：随机入场蒙特卡洛（同交易数、同持仓时长分布、
  同出场规则——随机信号喂给**同一个引擎**，保护性出场照常触发），
  策略夏普须高于其 95 分位；
- buy_hold_benchmark：纯多头策略夏普须高于同期买入持有（否则不适用）；
- drop_best_5pct：去掉净利最好的 5% 交易后仍盈利；
- delay_1_bar：全部信号延迟一根 bar 执行（更保守的成交时刻）仍盈利。

蒙特卡洛种子：np.random.default_rng(seed)，seed 由调用方传
stats.rng_seed(card_id)，同一卡可复现。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from mt5quant.backtest.engine_a import run_backtest
from mt5quant.backtest.report import bars_per_year, observed_bars_per_year
from mt5quant.strategies import BaseStrategy, BuyHold
from mt5quant.strategies.base import ExitRules


@dataclass(frozen=True)
class BacktestSpec:
    """关卡内反复回测的装配参数（engine_a.run_backtest 的具名集合）。"""

    strategy: object
    symbol: str
    timeframe: str
    contract_size: float
    volume_lots: float = 1.0
    initial_capital: float = 10_000.0


def run_engine(spec: BacktestSpec, bars: pd.DataFrame, costs, params: dict, *,
               strategy=None):
    """按 spec 跑一次 A 层（strategy 可覆盖，如 FixedSignals / BuyHold）。"""
    return run_backtest(
        strategy or spec.strategy, bars, costs, symbol=spec.symbol,
        params=dict(params), contract_size=spec.contract_size,
        volume_lots=spec.volume_lots, initial_capital=spec.initial_capital,
        timeframe=spec.timeframe)


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
                       initial_capital, cfg) -> dict:
    """同一逻辑在 ≥ cross_symbol_min 个品种上净利为正。"""
    symbols = sorted(bars_by_symbol)
    if len(symbols) < cfg.cross_symbol_min:
        return {"passed": True, "applicable": False,
                "positive_symbols": 0, "evaluated": len(symbols),
                "note": f"仅 {len(symbols)} 个品种可用，跨品种检查不适用"}
    nets = {}
    for sym in symbols:
        spec = BacktestSpec(strategy=strategy, symbol=sym, timeframe=timeframe,
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


def _random_signal_series(n: int, n_trades: int, durations: np.ndarray,
                          rng) -> np.ndarray:
    """随机 ±1 入场、持仓时长从经验分布抽取、互不重叠（中间隔至少 1 根 0）。"""
    sig = np.zeros(n, dtype=np.int64)
    starts = rng.permutation(np.arange(1, n))
    placed = 0
    for start in starts:
        if placed >= n_trades:
            break
        d = max(1, int(durations[int(rng.integers(0, len(durations)))]))
        end = start + d
        if end + 1 > n:
            continue
        if sig[max(0, start - 1):end + 1].any():
            continue
        sig[start:end] = 1 if rng.integers(0, 2) else -1
        placed += 1
    return sig


def random_entry_check(spec, bars, costs, base_trades, base_sharpe, *,
                       sims: int, percentile: float, seed: int) -> dict:
    """随机入场蒙特卡洛：同交易数、同持仓时长分布、同出场规则（随机信号
    喂给同一个 A 层引擎，保护性出场照常触发）；策略夏普须高于分位阈值。"""
    if not len(base_trades) or base_sharpe is None:
        return {"passed": False, "valid_sims": 0, "p95": None,
                "reason": "无交易或夏普无定义，无法对比随机入场基准"}
    durations = base_trades["bars_held"].to_numpy()
    n_trades = len(base_trades)
    exit_rules = spec.strategy.exit_rules
    rng = np.random.default_rng(seed)
    sharpes: list[float] = []
    for _ in range(int(sims)):
        sig = _random_signal_series(len(bars), n_trades, durations, rng)
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
        # 原始分布（G5 的区间检查用）；写入台账前应 pop 掉（凭 seed 可复现）
        "sharpes": [float(x) for x in sharpes],
    }


# ---------------------------------------------------------------- 组合

def run_g2(spec, bars, costs, params, *, base_result, base_summary, grid,
           bars_by_symbol, costs_by_symbol, sizes_by_symbol, cfg,
           seed: int) -> dict:
    """G2 全部子检查（顺序执行，互不短路；details 全部落台账）。"""
    checks: dict[str, dict] = {}
    checks["cost_scaling"] = cost_scaling_check(spec, bars, costs, params, cfg)
    checks["param_plateau"] = param_plateau(spec, bars, costs, params, grid, cfg)
    checks["yearly_segments"] = yearly_segments(base_result.trades, cfg)
    checks["cross_symbol"] = cross_symbol_check(
        spec.strategy, bars_by_symbol, costs_by_symbol, sizes_by_symbol,
        params, spec.timeframe, spec.volume_lots, spec.initial_capital, cfg)
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
