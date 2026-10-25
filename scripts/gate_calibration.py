"""gate_calibration：T22 关卡正负对照实验（H7 判别力）。

用法（Git Bash，仓库根）：
    .venv/Scripts/python.exe scripts/gate_calibration.py --phase both \
        --seeds 20 --out data/reports/gates/
    .venv/Scripts/python.exe scripts/gate_calibration.py --phase positive --quick
    .venv/Scripts/python.exe scripts/gate_calibration.py --phase negative \
        --baselines H002,H003 --seeds 5 --max-bars 60000

负对照（测误报率）：block_permute_bars（块长 = 一个 UTC 日，保留日内结构
与时戳、破坏跨日可预测性，见 mt5quant.validation.synthetic）打乱真实研究段
K 线，在其上跑六个基线策略（B1–B6，参数取 gates-calibration-v2 的选中组合，
grid 供 G2 参数邻域与 G3 网格内重选）与随机策略 RandomEntries；每道关卡
（G1–G4 各子检查）的通过率 = 误报率。

正对照（测检出率）：真实 XAUUSD H1 / USTEC M5 研究段上 plant_edge 植入
已知优势（事件 bar 后 k 根 OHLC 一致 +d，PlantedEdgeStrategy 只交易事件），
calibrate_drift 把**扣成本后**年化夏普校准到 ≈ 0.3 / 0.6 / 1.0 / 1.5（先验
公式定起点 + 割线迭代微调），每档多种子（种子改变事件相位与数据起点），
统计每道关卡子检查的检出率。

**决策规则（写进产出报告；只建议，不自动改门槛）**：真实（校准后）夏普
1.0 档某关卡子检查检出率 < 50%，或负对照误报率 > 5% → 报告"门槛调整建议"
一节列出（附数值与口径）；修改 config/gates.toml 须经用户批准，并同一提交
更新 FROZEN_GATES_SHA256（蓝图附录 B"改动须重新冻结"）。结构性通过检查
（G0 / G2.buy_hold_benchmark / G2.cross_symbol / G4.mc_drawdown——检验代码
卫生、适用性或离群而非"有没有优势"，负对照高通过率属预期）不参与标记，
但仍在汇总表中如实呈现。

实验口径与正式关卡的不同处（报告注明）：
- G2 随机入场蒙特卡洛降至 50 次（--g2-random-sims，正式关卡 200 不变）——
  计算量控制，随机抽样的分位数稳定性略有下降；
- G4 的 n_trials = 1（SR₀ = 0，DSR = PSR(0)）：实验不经过 screen/台账的
  网格选择，如实按"单次试验"计，选择效应修正为 0；
- 正对照单品种运行 → G2 cross_symbol 不适用（passed=True，与单品种卡的
  正式行为一致）；负对照按卡加载全部品种；
- 不跑 G5（锁箱一次性，实验不消耗机会）；G0 全程参与（工具策略必须干净）。

计算量（主会话实跑，估计）：负对照全套（6 基线 × 20 种子 × G1–G4，含 G3
日历月窗网格内重选；B5 的 M5 序列 29.5 万根）为多小时级；正对照每档校准
≤6 次基准回测 + 种子数 × 套件。迭代用 --quick（2 种子、只跑 1.0 档、
随机入场 3 次、最多 2 万根）/ --baselines / --max-bars。

产出：--out 目录（缺省 data/reports/gates/）下 gate-power.json +
gate-power.md（每道关卡 × 每档优势的检出率 / 误报率表 + 门槛调整建议）。
"""
from __future__ import annotations

import argparse
import dataclasses
import itertools
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # 复用 run_backtest 装配

from mt5data.store import TIMEFRAME_SECONDS  # noqa: E402
from mt5quant.backtest.engine_a import run_backtest  # noqa: E402
from mt5quant.backtest.report import (  # noqa: E402
    bars_per_year, observed_bars_per_year, summarize,
)
from mt5quant.sizing import RiskSizing  # noqa: E402
from mt5quant.strategies import (  # noqa: E402
    DonchianBreakout,
    IntradayMomentumNy,
    LondonAsianBreakout,
    NyOpeningRangeBreakout,
    TrendFilteredMeanReversion,
    VolTargetLong,
)
from mt5quant.validation import gates as vgate  # noqa: E402
from mt5quant.validation import lookahead as vlook  # noqa: E402
from mt5quant.validation import robustness as vrob  # noqa: E402
from mt5quant.validation import walkforward as vwf  # noqa: E402
from mt5quant.validation.synthetic import (  # noqa: E402
    PlantedEdgeStrategy,
    RandomEntries,
    block_permute_bars,
    event_bars,
    plant_edge,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# 六个基线（gates-calibration-v2-2026-10-25 的选中组合；品种/周期/grid 与
# 策略类 params_schema 一致）——负对照的被测对象
BASELINES: tuple[dict, ...] = (
    {"name": "H002-Donchian", "factory": lambda seed: DonchianBreakout(),
     "params": {"entry_days": 40, "exit_days": 4},
     "grid": {"entry_days": [10, 20, 40], "exit_days": [2, 4, 8]},
     "symbols": ("XAUUSD", "BTCUSD"), "timeframe": "H1"},
    {"name": "H003-OpeningRange",
     "factory": lambda seed: NyOpeningRangeBreakout(),
     "params": {"exit_hour_ny": 15, "min_range_atr": 0.5},
     "grid": {"exit_hour_ny": [14, 15], "min_range_atr": [0.0, 0.5, 1.0]},
     "symbols": ("USTEC",), "timeframe": "M5"},
    {"name": "H004-LondonBreakout",
     "factory": lambda seed: LondonAsianBreakout(),
     "params": {"exit_hour_ldn": 11, "min_range_atr": 0.0},
     "grid": {"exit_hour_ldn": [11, 12], "min_range_atr": [0.0, 0.5, 1.0]},
     "symbols": ("EURUSD", "XAUUSD"), "timeframe": "M15"},
    {"name": "H005-MeanReversion",
     "factory": lambda seed: TrendFilteredMeanReversion(),
     "params": {"z_n": 40, "z_entry": 2.5, "trend_n": 200},
     "grid": {"z_n": [20, 40, 80], "z_entry": [2.0, 2.5, 3.0],
              "trend_n": [50, 100, 200]},
     "symbols": ("XAUUSD",), "timeframe": "M15"},
    {"name": "H006-IntradayMomentum",
     "factory": lambda seed: IntradayMomentumNy(),
     "params": {"min_ret_atr": 1.5},
     "grid": {"min_ret_atr": [0.0, 0.5, 1.0, 1.5]},
     "symbols": ("USTEC",), "timeframe": "M5"},
    {"name": "H007-VolTarget", "factory": lambda seed: VolTargetLong(),
     "params": {"vol_n": 60, "vol_target": 0.002},
     "grid": {"vol_n": [20, 60, 120],
              "vol_target": [0.001, 0.0015, 0.002, 0.003]},
     "symbols": ("XAUUSD",), "timeframe": "H1"},
)

DEFAULT_LEVELS = (0.3, 0.6, 1.0, 1.5)
DEFAULT_SEEDS = 20
EXPERIMENT_G2_RANDOM_SIMS = 50    # 实验降次（正式关卡 200，报告注明）
DECISION_LEVEL = 1.0              # 决策规则盯住的档位
LOW_DETECTION = 0.50              # 检出率低于此 → 门槛调整建议
HIGH_FALSE_POSITIVE = 0.05        # 误报率高于此 → 门槛调整建议
PLANTED_HOLD_BARS = 4             # 植入/策略共用的持有根数（grid 最小档）
PLANTED_GRID = {"hold_bars": [4, 8]}
# 正对照各周期的事件节奏（每档周期一个 (period_days, interval_hours)）：
# H1 每 24h 一个事件 ≈ 260/年（≥ G1 的 150）；M5 每 6h ≈ 4/日（≥ 300）
PLANTED_RHYTHM = {"H1": (1, 24), "M5": (1, 6)}
DEFAULT_POSITIVE_SYMBOLS = ("XAUUSD:H1", "USTEC:M5")


def load_gates_for_run():
    """加载冻结门槛并把 G2 随机入场降次到实验口径（正式 200 不变）。"""
    cfg, _sha = vgate.load_gates()
    return dataclasses.replace(cfg, random_entry_sims=EXPERIMENT_G2_RANDOM_SIMS)


def bars_per_day_of(timeframe: str) -> int:
    """一个交易日的名义 bar 数（G4 块长 / 种子数据起点偏移用）。"""
    return max(1, round(86400.0 / TIMEFRAME_SECONDS[timeframe.upper()]))


def seed_phases(seed: int, interval_hours: int, period_days: int) -> tuple[int, int]:
    """实验种子 → 事件相位（小时相位 / 日相位）：同一种子确定性映射，
    不同种子覆盖不同的相位与（period>1 时）合格日集合。"""
    phase_hours = int(seed) % max(1, int(interval_hours))
    day_phase = (int(seed) // max(1, int(interval_hours))) % max(1, int(period_days))
    return phase_hours, day_phase


# ---------------------------------------------------------------- 关卡套件

def run_gate_suite(strategy, params, *, timeframe, bars_by_symbol,
                   costs_by_symbol, sizes_by_symbol, vols_by_symbol, cfg,
                   seed, grid=None, initial_capital=100_000.0,
                   risk_fraction=0.005, session_policy=None) -> dict:
    """一次基准运行 + G0–G4（与 bt gate 同一装配：引擎 A、按风险定手数、
    成本模型/门槛来自调用方；G2 随机入场次数由 cfg.random_entry_sims 决定）。

    返回 {"G0":…, "G1":…, …, "G4":…, "summary": 摘要}；G4 按 n_trials=1
    （实验不经网格选择，选择效应为 0，报告注明）。grid：参数网格 dict
    （G2 参数邻域与 G3 网格内重选用；缺省 {} = 单组合）。
    """
    symbol = next(iter(bars_by_symbol))
    bars = bars_by_symbol[symbol]
    costs = costs_by_symbol[symbol]
    sizing = RiskSizing(risk_fraction=float(risk_fraction))
    vol = vols_by_symbol[symbol]
    base = run_backtest(
        strategy, bars, costs, symbol=symbol, params=dict(params),
        contract_size=sizes_by_symbol[symbol], volume_lots=None,
        sizing=sizing, vol_min=vol[0], vol_step=vol[1], vol_max=vol[2],
        initial_capital=initial_capital, timeframe=timeframe,
        session_policy=session_policy)
    summary = summarize(base)
    spec = vrob.BacktestSpec(
        strategy=strategy, symbol=symbol, timeframe=timeframe,
        contract_size=sizes_by_symbol[symbol], volume_lots=None,
        initial_capital=initial_capital, sizing=sizing,
        vol_min=vol[0], vol_step=vol[1], vol_max=vol[2])
    grid = dict(grid or {})
    combos = [dict(zip(grid, values))
              for values in itertools.product(*(grid.values() or [()]))] or [{}]

    results: dict[str, dict] = {}
    for gate, fn in (
            ("G0", lambda: vlook.run_g0(strategy, bars, params)),
            ("G1", lambda: vgate.run_g1(base.trades, timeframe=timeframe,
                                        cfg=cfg)),
            ("G2", lambda: vrob.run_g2(
                spec, bars, costs, params, base_result=base,
                base_summary=summary, grid=grid, bars_by_symbol=bars_by_symbol,
                costs_by_symbol=costs_by_symbol, sizes_by_symbol=sizes_by_symbol,
                cfg=cfg, seed=seed, vols_by_symbol=vols_by_symbol)),
            ("G3", lambda: vwf.run_g3(
                spec, bars, costs, grid=combos,
                in_sample_sharpe=summary.get("sharpe"), cfg=cfg)),
            ("G4", lambda: vgate.run_g4(
                base.equity.astype("float64").pct_change().dropna(),
                n_trials=1, trial_sharpes=None,
                actual_max_drawdown=summary.get("max_drawdown", 0.0),
                cfg=cfg, seed=seed,
                block_len=bars_per_day_of(timeframe)))):
        try:
            results[gate] = fn()
        except Exception as e:      # 单关卡炸掉不拖垮整套（与 bt gate 一致）
            results[gate] = {"passed": False, "error": f"{type(e).__name__}: {e}"}
    results["summary"] = {
        "net_profit": summary["net_profit"],
        "sharpe": summary.get("sharpe"),
        "trade_count": summary["trade_count"],
        "profit_factor": summary.get("profit_factor"),
        "max_drawdown": summary.get("max_drawdown"),
        "ruined": bool(summary.get("ruined")),
    }
    return results


def flatten_checks(suite: dict) -> dict[str, bool]:
    """G0–G4 结果 → {"G1.profit_factor": True, "G0": False, …}（报告口径）。"""
    flat: dict[str, bool] = {"G0": bool(suite["G0"]["passed"])}
    for gate in ("G1", "G2", "G4"):
        for name, check in suite[gate].get("checks", {}).items():
            flat[f"{gate}.{name}"] = bool(check.get("passed", False))
    flat["G3.oos_sharpe"] = bool(suite["G3"].get("passed", False))
    return flat


def aggregate_rates(runs: list[dict]) -> dict[str, float]:
    """逐关卡子检查的通过率（误报率/检出率共用；runs 须含 "checks"）。"""
    buckets: dict[str, list[bool]] = {}
    for run in runs:
        for key, value in run["checks"].items():
            buckets.setdefault(key, []).append(bool(value))
    return {key: float(np.mean(values)) for key, values in buckets.items()}


# ---------------------------------------------------------------- 负对照

def negative_control(baselines, *, seeds, cfg, bars_by_symbol, costs_by_symbol,
                     sizes_by_symbol, vols_by_symbol, initial_capital=100_000.0,
                     risk_fraction=0.005, session_policy=None,
                     log=None) -> dict:
    """打乱数据 × baselines × seeds → {"runs": […], "false_positive_rates": …}。

    baselines: (name, strategy_factory, params, grid, timeframe) 五元组；
    strategy_factory(seed) 按种子构造策略（基线忽略种子，RandomEntries 用
    它换随机事件模式）。bars_by_symbol 为该组基线的品种 K 线（同一次调用
    内共享品种/周期）。每个种子对每个品种做块置换（同品种跨基线同种子
    共享同一次打乱——配对设计），然后跑完整关卡套件；通过率即误报率。
    """
    runs: list[dict] = []
    for name, factory, params, grid, timeframe in baselines:
        for seed in seeds:
            t0 = time.monotonic()
            shuffled = {sym: block_permute_bars(bars, seed=seed)
                        for sym, bars in bars_by_symbol.items()}
            suite = run_gate_suite(
                factory(seed), params, timeframe=timeframe,
                bars_by_symbol=shuffled, costs_by_symbol=costs_by_symbol,
                sizes_by_symbol=sizes_by_symbol, vols_by_symbol=vols_by_symbol,
                cfg=cfg, seed=seed, grid=grid,
                initial_capital=initial_capital,
                risk_fraction=risk_fraction, session_policy=session_policy)
            flat = flatten_checks(suite)
            runs.append({
                "name": name, "seed": int(seed),
                "sharpe": suite["summary"]["sharpe"],
                "net_profit": suite["summary"]["net_profit"],
                "trade_count": suite["summary"]["trade_count"],
                "passed_all": bool(all(flat.values())),
                "checks": flat,
            })
            if log:
                log(f"[negative] {name} seed={seed} "
                    f"sharpe={suite['summary']['sharpe']} "
                    f"({time.monotonic() - t0:.1f}s)")
    return {"runs": runs, "false_positive_rates": aggregate_rates(runs)}


# ---------------------------------------------------------------- 正对照

def _planted_sharpe(strategy, planted_bars, params, *, symbol, timeframe,
                    costs, contract_size, vols, initial_capital,
                    risk_fraction, session_policy) -> tuple[float | None, dict]:
    """植入数据上的一次基准回测：(年化夏普, summarize 摘要)。"""
    result = run_backtest(
        strategy, planted_bars, costs, symbol=symbol, params=dict(params),
        contract_size=contract_size, volume_lots=None,
        sizing=RiskSizing(risk_fraction=float(risk_fraction)),
        vol_min=vols[0], vol_step=vols[1], vol_max=vols[2],
        initial_capital=initial_capital, timeframe=timeframe,
        session_policy=session_policy)
    summary = summarize(result)
    return summary.get("sharpe"), summary


def calibrate_drift(symbol, timeframe, *, target_sharpe, period_days=1,
                    interval_hours=24, hold_bars=PLANTED_HOLD_BARS, cfg=None,
                    seed=0, max_iter=6, tol=0.15, initial_capital=100_000.0,
                    risk_fraction=0.005, bars_by_symbol, costs_by_symbol,
                    sizes_by_symbol, vols_by_symbol, session_policy=None,
                    log=None) -> dict:
    """把植入漂移 d 校准到"扣成本后年化夏普 ≈ target"。

    方法（先验公式 + 割线迭代）：
    1. 先测 d=0（原始数据）的净夏普 S0 与逐笔成本（价格单位中位数）；
    2. 先验起点：每笔期望 ≈ d − 成本、每笔噪声 ≈ sqrt(hold)×σ_open2open，
       事件频率 n_yr/年 → 夏普斜率 ≈ sqrt(n_yr) / (sqrt(hold)·σ)，
       d₁ = (target − S0) / 斜率（并入成本项），下限 0.1σ；
    3. 割线迭代（≤ max_iter 次回测）：S(d) 对 d 近似线性（噪声不变、
       每笔毛利线性 +d），用最近两点 (d,S) 外推到 target；|S−target| ≤ tol
       即停；返回历史里 |S−target| 最小的点（best effort，如实记录）。
    cfg 参数保留以统一调用口径（校准只用基准回测，不涉关卡）。
    """
    bars = bars_by_symbol[symbol]
    costs = costs_by_symbol[symbol]
    vols = vols_by_symbol[symbol]
    contract_size = sizes_by_symbol[symbol]
    phase_hours, day_phase = seed_phases(seed, interval_hours, period_days)
    strategy = PlantedEdgeStrategy(period_days=period_days,
                                   interval_hours=interval_hours,
                                   phase_hours=phase_hours,
                                   day_phase=day_phase)
    params = {"hold_bars": int(hold_bars)}

    def plant(drift: float) -> pd.DataFrame:
        return plant_edge(bars, period_days=period_days,
                          interval_hours=interval_hours,
                          hold_bars=hold_bars, drift=drift,
                          phase_hours=phase_hours, day_phase=day_phase)

    def measure(drift: float) -> float:
        sharpe, summary = _planted_sharpe(
            strategy, plant(drift), params, symbol=symbol, timeframe=timeframe,
            costs=costs, contract_size=contract_size, vols=vols,
            initial_capital=initial_capital, risk_fraction=risk_fraction,
            session_policy=session_policy)
        value = float(sharpe) if sharpe is not None else 0.0
        history.append({"drift": float(drift), "sharpe": value,
                        "trade_count": summary["trade_count"],
                        "net_profit": summary["net_profit"]})
        return value

    history: list[dict] = []
    s0 = measure(0.0)
    opens = bars["open"].to_numpy(dtype="float64")
    sigma = float(np.std(np.diff(opens), ddof=1))
    n_events = int(event_bars(bars, period_days=period_days,
                              interval_hours=interval_hours,
                              phase_hours=phase_hours,
                              day_phase=day_phase).sum())
    bpy = observed_bars_per_year(bars["time"]) or bars_per_year(timeframe)
    events_per_year = n_events * float(bpy) / max(1, len(bars))
    # 成本不进先验：d=0 的实测夏普 S0 已含成本基线，割线迭代自动吸收
    slope = (math.sqrt(max(events_per_year, 1.0))
             / (math.sqrt(int(hold_bars)) * max(sigma, 1e-12)))
    drift = max((target_sharpe - s0) / max(slope, 1e-12), 0.1 * sigma)
    prev: tuple[float, float] | None = None
    for _ in range(max_iter):
        s = measure(drift)
        if abs(s - target_sharpe) <= tol:
            break
        if prev is not None and abs(s - prev[1]) > 1e-9:
            d0, s0_prev = prev
            drift = d0 + (target_sharpe - s0_prev) * (drift - d0) / (s - s0_prev)
        else:
            drift = drift * max(target_sharpe, 0.1) / max(abs(s), 0.1)
        drift = min(max(drift, 0.0), 50.0 * max(sigma, 1e-12))
        prev = (history[-1]["drift"], history[-1]["sharpe"])
    best = min(history, key=lambda h: abs(h["sharpe"] - target_sharpe))
    out = {"drift": float(best["drift"]),
           "achieved_sharpe": float(best["sharpe"]),
           "iterations": len(history), "history": history,
           "target_sharpe": float(target_sharpe),
           "prior": {"sigma_open_to_open": sigma,
                     "events_per_year": events_per_year,
                     "bars_per_year": float(bpy),
                     "slope_sharpe_per_drift": slope,
                     "sharpe_at_zero_drift": s0},
           "seed": int(seed), "period_days": int(period_days),
           "interval_hours": int(interval_hours), "hold_bars": int(hold_bars)}
    if log:
        log(f"[calibrate] {symbol} {timeframe} target={target_sharpe} "
            f"-> d={out['drift']:.6g} sharpe={out['achieved_sharpe']:.3f} "
            f"({out['iterations']} runs)")
    return out


def positive_control(setups, *, levels, seeds, cfg, bars_by_symbol,
                     costs_by_symbol, sizes_by_symbol, vols_by_symbol,
                     initial_capital=100_000.0, risk_fraction=0.005,
                     session_policy=None, session_policies=None,
                     log=None) -> dict:
    """植入已知优势 → 各档 × 各种子的关卡套件 → 检出率。

    setups: (symbol, timeframe, level_cal, period_days, interval_hours,
    hold_bars)；level_cal = {level: {"drift": d, "achieved_sharpe": s}}
    （calibrate_drift 的产出；positive_control 只执行不校准）。
    种子 → 事件相位 + 数据起点偏移（(seed%10)×每日 bar 数），同一种子
    逐位可复现。session_policies[symbol] 覆盖 session_policy（单品种运行）。
    """
    runs: list[dict] = []
    calibration: dict[str, dict] = {}
    detection: dict[str, dict[str, dict[str, float]]] = {}
    policies = dict(session_policies or {})
    for symbol, timeframe, level_cal, period_days, interval_hours, hold_bars \
            in setups:
        key = f"{symbol} {timeframe}"
        calibration[key] = {str(level): {
            "drift": info["drift"], "achieved_sharpe": info["achieved_sharpe"]}
            for level, info in level_cal.items()}
        bars = bars_by_symbol[symbol]
        policy = policies.get(symbol, session_policy)
        bpy_shift = bars_per_day_of(timeframe)
        by_level: dict[str, list[dict]] = {str(level): [] for level in levels}
        for level in levels:
            drift = float(level_cal[level]["drift"])
            for seed in seeds:
                t0 = time.monotonic()
                phase_hours, day_phase = seed_phases(seed, interval_hours,
                                                     period_days)
                offset = (int(seed) % 10) * bpy_shift
                segment = bars.iloc[offset:].reset_index(drop=True)
                planted = plant_edge(
                    segment, period_days=period_days,
                    interval_hours=interval_hours, hold_bars=hold_bars,
                    drift=drift, phase_hours=phase_hours,
                    day_phase=day_phase)
                strategy = PlantedEdgeStrategy(
                    period_days=period_days, interval_hours=interval_hours,
                    phase_hours=phase_hours, day_phase=day_phase)
                suite = run_gate_suite(
                    strategy, {"hold_bars": int(hold_bars)},
                    timeframe=timeframe, bars_by_symbol={symbol: planted},
                    costs_by_symbol={symbol: costs_by_symbol[symbol]},
                    sizes_by_symbol={symbol: sizes_by_symbol[symbol]},
                    vols_by_symbol={symbol: vols_by_symbol[symbol]},
                    cfg=cfg, seed=seed, grid=dict(PLANTED_GRID),
                    initial_capital=initial_capital,
                    risk_fraction=risk_fraction, session_policy=policy)
                flat = flatten_checks(suite)
                run = {"setup": key, "level": float(level), "seed": int(seed),
                       "drift": drift,
                       "sharpe": suite["summary"]["sharpe"],
                       "net_profit": suite["summary"]["net_profit"],
                       "trade_count": suite["summary"]["trade_count"],
                       "passed_all": bool(all(flat.values())),
                       "checks": flat}
                runs.append(run)
                by_level[str(level)].append(run)
                if log:
                    log(f"[positive] {key} level={level} seed={seed} "
                        f"sharpe={run['sharpe']} ({time.monotonic() - t0:.1f}s)")
        detection[key] = {level: aggregate_rates(bucket)
                          for level, bucket in by_level.items()}
    return {"runs": runs, "detection_rates": detection,
            "calibration": calibration}


# ---------------------------------------------------------------- 决策规则

# 结构性通过检查：检验代码卫生 / 离群 / 适用性而非"有没有优势"，负对照上
# 高通过率属预期（G0 只看因果性；buy_hold/cross_symbol 对不适用情形按通过
# 记录；mc_drawdown 是回撤离群报警，无优势策略的回撤落在重采样分布内是
# 常态）。这些检查仍出现在误报率表里，但**不参与**决策规则的标记——
# 决策规则针对的是"优势判别器"。
STRUCTURAL_CHECKS = frozenset({
    "G0", "G2.buy_hold_benchmark", "G2.cross_symbol", "G4.mc_drawdown",
})


def collect_flags(detection_at_level: dict[str, float],
                  false_positive_rates: dict[str, float],
                  *, level_for_detection: str = str(DECISION_LEVEL)) -> list[dict]:
    """决策规则（T22）：夏普 1.0 档检出率 < 50% 或负对照误报率 > 5% → 标记。

    只产出建议（报告"门槛调整建议"一节）；改 gates.toml 须用户批准并
    重新冻结。detection_at_level 传多品种时应先取各品种的最小值（最保守）。
    STRUCTURAL_CHECKS（结构性通过，见其注释）不参与标记。
    """
    flags: list[dict] = []
    for check, rate in sorted(detection_at_level.items()):
        if check in STRUCTURAL_CHECKS:
            continue
        if rate < LOW_DETECTION:
            flags.append({
                "check": check, "kind": "low_detection", "value": float(rate),
                "rule": f"真实夏普 {level_for_detection} 档检出率 "
                        f"< {LOW_DETECTION:.0%}"})
    for check, rate in sorted(false_positive_rates.items()):
        if check in STRUCTURAL_CHECKS:
            continue
        if rate > HIGH_FALSE_POSITIVE:
            flags.append({
                "check": check, "kind": "high_false_positive",
                "value": float(rate),
                "rule": f"负对照误报率 > {HIGH_FALSE_POSITIVE:.0%}"})
    return flags


def _fmt_pct(rate: float | None) -> str:
    return "—" if rate is None else f"{rate * 100:.0f}%"


def render_report(results: dict) -> str:
    """Markdown 汇总：每道关卡 × 每档优势的检出率 / 误报率 + 决策规则。"""
    meta = results.get("meta", {})
    levels = meta.get("levels") or list(DEFAULT_LEVELS)
    lines = [
        "# 关卡正负对照实验（gate-power，T22/H7）", "",
        f"- 阶段：{meta.get('phase', '?')}；种子数：{meta.get('seeds', '?')}；"
        f"夏普档位：{levels}",
        f"- G2 随机入场蒙特卡洛：本实验降至 {meta.get('g2_random_entry_sims', '?')}"
        " 次（正式关卡 200 次不变）",
        f"- 生成：{meta.get('generated_utc', '?')}（gate_calibration.py）", "",
        "## 决策规则", "",
        f"真实（校准后）夏普 {DECISION_LEVEL} 档某关卡子检查检出率 "
        f"< {LOW_DETECTION:.0%}，或负对照误报率 > {HIGH_FALSE_POSITIVE:.0%}，"
        "即在下一节给出门槛调整建议；**只建议**——修改 config/gates.toml "
        "须经用户批准，并在同一提交更新 FROZEN_GATES_SHA256 重新冻结。", "",
    ]
    negative = results.get("negative") or {}
    fp = negative.get("false_positive_rates") or {}
    if fp:
        lines += ["## 负对照误报率（打乱数据 × 基线/随机策略 × 种子）", "",
                  "| 关卡子检查 | 误报率 |", "| --- | --- |"]
        for check in sorted(fp):
            lines.append(f"| {check} | {_fmt_pct(fp[check])} |")
        lines.append("")
    positive = results.get("positive") or {}
    detection = positive.get("detection_rates") or {}
    for key, by_level in detection.items():
        lines += [f"## 正对照检出率：{key}", "",
                  "| 关卡子检查 | " + " | ".join(
                      f"夏普≈{lv}" for lv in levels) + " |",
                  "| --- " * (len(levels) + 1) + "|"]
        checks = sorted({c for rates in by_level.values() for c in rates})
        for check in checks:
            row = [_fmt_pct(by_level.get(str(lv), {}).get(check))
                   for lv in levels]
            lines.append(f"| {check} | " + " | ".join(row) + " |")
        cal = (positive.get("calibration") or {}).get(key) or {}
        if cal:
            lines += ["", "校准（扣成本后年化夏普 → 漂移 d）："
                      + "；".join(
                          f"≈{lv} → d={info['drift']:.6g}"
                          f"（实测 {info['achieved_sharpe']:.2f}）"
                          for lv, info in sorted(
                              cal.items(), key=lambda kv: float(kv[0])))]
        lines.append("")
    flags = results.get("flags") or []
    lines += ["## 门槛调整建议", ""]
    if flags:
        for flag in flags:
            lines.append(f"- **{flag['check']}**：{flag['rule']}——"
                         f"实测 {flag['value'] * 100:.0f}%。"
                         "建议复核该门槛（调整方案与证据请交用户批准）。")
    else:
        lines.append(f"无——夏普 {DECISION_LEVEL} 档所有关卡子检查检出率 "
                     f"≥ {LOW_DETECTION:.0%}，负对照误报率均 "
                     f"≤ {HIGH_FALSE_POSITIVE:.0%}。")
    lines += ["", "## 口径备注", "",
              "- 负对照：块置换（块长 = 一个 UTC 日）保留日内结构与日收益分布，"
              "破坏跨日可预测性；基线参数取 gates-calibration-v2 选中组合。",
              "- 正对照：事件 bar 后 k 根 OHLC 一致 +d；PlantedEdgeStrategy "
              "只交易事件；d 由先验公式 + 割线迭代校准（详见 JSON 的 "
              "calibration.history）。",
              "- G4 n_trials=1（实验不经网格选择，选择效应为 0）；G2 跨品种"
              "检查在单品种运行中不适用（按通过记录，与单品种卡正式行为一致）。",
              "- 结构性通过检查（G0、G2.buy_hold_benchmark、G2.cross_symbol、"
              "G4.mc_drawdown）检验代码卫生/离群/适用性而非优势判别，负对照"
              "高通过率属预期——仍列于上表，但不参与决策规则标记。",
              "- 本实验不跑 G5（锁箱一次性，不在实验范围）。", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- 装配（真实数据）

def assemble_experiment(data_root, keys, max_bars=None) -> dict:
    """读研究段 K 线 + 实测成本 + 快照规格（键 "SYMBOL TF" → 装配 dict）。

    与 bt gate 同源：load_series（锁箱语义，锁箱段绝不读）、build_costs
    （点差缓存 + 快照隔夜费 + sessions sha）、resolve_contract_size、
    load_volume_limits、SessionPolicy（缺统计小时禁入并入）。max_bars
    只保留最近 N 根（迭代提速；正式实验不截）。
    """
    from mt5data.sessions import load_session_policy
    from mt5quant.sizing import load_volume_limits
    from run_backtest import (
        SESSIONS_PATH, build_costs, entry_policy_for, load_series,
        resolve_contract_size, sessions_sha256,
    )

    sess_policy = load_session_policy(str(SESSIONS_PATH))
    sess_sha = sessions_sha256(str(SESSIONS_PATH))
    out: dict[str, dict] = {}
    for key in keys:
        symbol, timeframe = key.split()
        bars = load_series(data_root, symbol, timeframe, None)
        if max_bars and len(bars) > int(max_bars):
            bars = bars.iloc[-int(max_bars):].reset_index(drop=True)
        costs, _version = build_costs(data_root, symbol, 0.1, 0.0,
                                      sessions_sha=sess_sha)
        out[key] = {
            "bars": bars,
            "costs": costs,
            "sizes": resolve_contract_size(data_root, symbol, None),
            "vols": load_volume_limits(data_root, symbol),
            "timeframe": timeframe,
            "session_policy": entry_policy_for(costs, symbol, sess_policy),
        }
    return out


def _baseline_keys(baseline: dict) -> list[str]:
    return [f"{sym} {baseline['timeframe']}" for sym in baseline["symbols"]]


def _json_default(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return str(value)


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="gate_calibration",
        description="T22 关卡正负对照实验（H7 判别力）：负对照误报率 + "
                    "正对照检出率 → JSON + Markdown")
    ap.add_argument("--phase", choices=("negative", "positive", "both"),
                    default="both")
    ap.add_argument("--seeds", type=int, default=DEFAULT_SEEDS,
                    help="每个条件的种子数（负对照与正对照共用）")
    ap.add_argument("--levels", default=",".join(str(v) for v in DEFAULT_LEVELS),
                    help="正对照夏普档位（逗号分隔，如 0.3,0.6,1.0,1.5）")
    ap.add_argument("--symbols", default=",".join(DEFAULT_POSITIVE_SYMBOLS),
                    help="正对照 SYMBOL:TF 列表（缺省 XAUUSD:H1,USTEC:M5）")
    ap.add_argument("--baselines", default=None,
                    help="负对照基线子集（逗号分隔的名称前缀，如 H002,H003；"
                         "缺省全部六个 + 随机策略）")
    ap.add_argument("--out", default=str(REPO_ROOT / "data" / "reports" / "gates"))
    ap.add_argument("--data-root", default="data")
    ap.add_argument("--initial-capital", type=float, default=100_000.0)
    ap.add_argument("--risk-per-trade", type=float, default=0.005)
    ap.add_argument("--max-bars", type=int, default=None,
                    help="每序列只保留最近 N 根（迭代用；正式实验不截）")
    ap.add_argument("--g2-random-sims", type=int,
                    default=EXPERIMENT_G2_RANDOM_SIMS,
                    help="实验用 G2 随机入场次数（正式关卡 200 不变）")
    ap.add_argument("--quick", action="store_true",
                    help="迭代模式：2 种子、只跑 1.0 档、随机入场 3 次、"
                         "最多 20000 根")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    seeds = list(range(args.seeds))
    levels = [float(x) for x in str(args.levels).split(",") if x.strip()]
    g2_sims = args.g2_random_sims
    max_bars = args.max_bars
    if args.quick:
        seeds = seeds[:2]
        levels = [DECISION_LEVEL]
        g2_sims = min(g2_sims, 3)
        max_bars = min(max_bars or 10**9, 20_000)
    cfg = load_gates_for_run()
    cfg = dataclasses.replace(cfg, random_entry_sims=int(g2_sims))
    log = lambda msg: print(msg, flush=True)      # noqa: E731

    def _suite_inputs(entry: dict) -> dict:
        bars_by_symbol, costs_by_symbol = {}, {}
        sizes_by_symbol, vols_by_symbol, policies = {}, {}, {}
        for key in _baseline_keys(entry):
            pack = inputs_by_key[key]
            sym = key.split()[0]
            bars_by_symbol[sym] = pack["bars"]
            costs_by_symbol[sym] = pack["costs"]
            sizes_by_symbol[sym] = pack["sizes"]
            vols_by_symbol[sym] = pack["vols"]
            policies[key] = pack["session_policy"]
        primary = f"{entry['symbols'][0]} {entry['timeframe']}"
        return dict(bars_by_symbol=bars_by_symbol,
                    costs_by_symbol=costs_by_symbol,
                    sizes_by_symbol=sizes_by_symbol,
                    vols_by_symbol=vols_by_symbol,
                    session_policy=policies[primary])

    wanted = ([p.strip().upper() for p in (args.baselines or "").split(",")
               if p.strip()] or None)
    baselines = [b for b in BASELINES
                 if wanted is None or any(b["name"].startswith(w)
                                          for w in wanted)]
    positive_keys = []
    for item in str(args.symbols).split(","):
        item = item.strip()
        if item:
            symbol, _, tf = item.partition(":")
            positive_keys.append(f"{symbol.upper()} {tf.upper() or 'H1'}")

    needed = sorted({k for b in baselines for k in _baseline_keys(b)}
                    | set(positive_keys))
    print(f"— 装配 {len(needed)} 个序列：{needed}（max_bars={max_bars}）",
          flush=True)
    inputs_by_key = assemble_experiment(args.data_root, needed,
                                        max_bars=max_bars)

    results: dict = {"meta": {
        "phase": args.phase, "seeds": len(seeds), "levels": levels,
        "g2_random_entry_sims": int(g2_sims),
        "initial_capital": args.initial_capital,
        "risk_per_trade": args.risk_per_trade,
        "max_bars": max_bars, "baselines": [b["name"] for b in baselines],
        "positive_symbols": positive_keys,
        "engine_note": "run_gate_suite: n_trials=1（选择效应为 0），"
                       "G2 随机入场实验降次，不跑 G5",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
    }}
    if args.phase in ("negative", "both"):
        runs: list[dict] = []
        for entry in baselines:
            sub = negative_control(
                [(entry["name"], entry["factory"], entry["params"],
                  entry["grid"], entry["timeframe"])],
                seeds=seeds, cfg=cfg, initial_capital=args.initial_capital,
                risk_fraction=args.risk_per_trade, log=log, **_suite_inputs(entry))
            runs.extend(sub["runs"])
        for key in positive_keys:      # 随机策略（同样在打乱数据上）
            symbol, timeframe = key.split()
            pack = inputs_by_key[key]
            sub = negative_control(
                [(f"RND@{key}",
                  lambda seed: RandomEntries(seed=int(seed), hold_bars=4,
                                             event_every_bars=24), {}, {},
                  timeframe)],
                seeds=seeds, cfg=cfg,
                bars_by_symbol={symbol: pack["bars"]},
                costs_by_symbol={symbol: pack["costs"]},
                sizes_by_symbol={symbol: pack["sizes"]},
                vols_by_symbol={symbol: pack["vols"]},
                initial_capital=args.initial_capital,
                risk_fraction=args.risk_per_trade,
                session_policy=pack["session_policy"], log=log)
            runs.extend(sub["runs"])
        results["negative"] = {"runs": runs,
                               "false_positive_rates": aggregate_rates(runs)}

    if args.phase in ("positive", "both"):
        setups = []
        for key in positive_keys:
            symbol, timeframe = key.split()
            pack = inputs_by_key[key]
            period_days, interval_hours = PLANTED_RHYTHM.get(
                timeframe.upper(), (1, 24))
            level_cal = {}
            for level in levels:
                cal = calibrate_drift(
                    symbol, timeframe, target_sharpe=level,
                    period_days=period_days, interval_hours=interval_hours,
                    hold_bars=PLANTED_HOLD_BARS, cfg=cfg, seed=0,
                    initial_capital=args.initial_capital,
                    risk_fraction=args.risk_per_trade,
                    bars_by_symbol={symbol: pack["bars"]},
                    costs_by_symbol={symbol: pack["costs"]},
                    sizes_by_symbol={symbol: pack["sizes"]},
                    vols_by_symbol={symbol: pack["vols"]},
                    session_policy=pack["session_policy"], log=log)
                level_cal[level] = {"drift": cal["drift"],
                                    "achieved_sharpe": cal["achieved_sharpe"]}
            setups.append((symbol, timeframe, level_cal, period_days,
                           interval_hours, PLANTED_HOLD_BARS))
        pos = positive_control(
            setups, levels=levels, seeds=seeds, cfg=cfg,
            initial_capital=args.initial_capital,
            risk_fraction=args.risk_per_trade,
            bars_by_symbol={key.split()[0]: inputs_by_key[key]["bars"]
                            for key in positive_keys},
            costs_by_symbol={key.split()[0]: inputs_by_key[key]["costs"]
                             for key in positive_keys},
            sizes_by_symbol={key.split()[0]: inputs_by_key[key]["sizes"]
                             for key in positive_keys},
            vols_by_symbol={key.split()[0]: inputs_by_key[key]["vols"]
                            for key in positive_keys},
            session_policies={key.split()[0]: inputs_by_key[key]
                              ["session_policy"] for key in positive_keys},
            log=log)
        results["positive"] = pos
        results["meta"]["detection_min_note"] = (
            "flags 按 各品种检出率的最小值 计算（最保守）")

    detection_for_flags = (results.get("positive", {}).get("detection_rates")
                           or {})
    fp_rates = (results.get("negative") or {}).get("false_positive_rates") or {}
    if detection_for_flags:
        level_rates = {}
        for key, by_level in detection_for_flags.items():
            for check, rate in (by_level.get(str(DECISION_LEVEL))
                                or {}).items():
                prev = level_rates.get(check)
                level_rates[check] = rate if prev is None else min(prev, rate)
        results["flags"] = collect_flags(level_rates, fp_rates)
    elif fp_rates:
        results["flags"] = collect_flags({}, fp_rates)
    else:
        results["flags"] = []

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "gate-power.json"
    md_path = out_dir / "gate-power.md"
    json_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2,
                   default=_json_default), encoding="utf-8")
    md_path.write_text(render_report(results), encoding="utf-8")
    print(f"— 产出：{json_path} 与 {md_path}", flush=True)
    flags = results.get("flags") or []
    if flags:
        print(f"⚠ 决策规则触发 {len(flags)} 项（详见报告'门槛调整建议'）："
              f"{[f['check'] for f in flags]}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
