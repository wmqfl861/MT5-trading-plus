"""gate_calibration：T22 关卡正负对照实验（H7 判别力）+ T39 收尾
（M4 日内零假设 / M5 G5 判别力 / 合并评估变体）。

用法（Git Bash，仓库根）：
    .venv/Scripts/python.exe scripts/gate_calibration.py --phase both \
        --seeds 20 --null-hypothesis both --out data/reports/gates/
    .venv/Scripts/python.exe scripts/gate_calibration.py --phase positive --quick
    .venv/Scripts/python.exe scripts/gate_calibration.py --phase negative \
        --baselines H002,H003 --null-hypothesis slot --seeds 5 --max-bars 60000

负对照（测误报率）：两种零假设（T39/M4），**按卡的持仓周期选**（--null-
hypothesis auto 缺省；both = 各跑一遍）：
- day（多日持仓卡）：block_permute_bars，块长 = 一个 UTC 日，保留日内
  结构与时戳、破坏跨日可预测性；
- slot（日内卡，当日平仓）：slot_permute_bars，同一纽约本地时刻（bar 槽位）
  的收益在不同日期间置换，保留各时段波动分布、破坏日内先后依赖与跨日
  次序——对当日平仓的策略这才是零假设（M4：日块置换保留每天完整的日内
  形状，测出的"误报率"对日内策略含义不清）。
打乱真实研究段 K 线后跑六个基线策略（B1–B6，参数取 gates-calibration-v2
的选中组合，grid 供 G2 参数邻域与 G3 网格内重选）与随机策略
RandomEntries；每道关卡（G1–G4 各子检查）的通过率 = 误报率。

正对照（测检出率）：真实 XAUUSD H1 / USTEC M5 研究段上 plant_edge 植入
已知优势（事件 bar 后 k 根 OHLC 一致 +d，PlantedEdgeStrategy 只交易事件），
calibrate_drift 把**扣成本后**年化夏普校准到 ≈ 0.3 / 0.6 / 1.0 / 1.5（先验
公式定起点 + 割线迭代微调），每档多种子（种子改变事件相位与数据起点），
统计每道关卡子检查的检出率。

G5 判别力（T39/M5）：与锁箱**等长**的合成窗口（窗口长 = 研究段实测
bars/year × 锁箱已历时长；只读 lockbox.json 的 cutoff 日期，绝不读锁箱
K 线）上镜像 G5 的单侧判定（expectation_positive / trade_count /
sharpe_vs_reference，阈值与冻结的 gates.toml 同源；无台账、无批准、
不消耗一次性机会——正式 G5 仍只在 bt --approve 路径跑）。检出臂 =
植入校准漂移的窗口；误报臂 = 两种零假设打乱后的窗口；参照分布 =
同一策略**全研究段**无植入运行的权益收益（G3 拼接样本外收益的替身，
与真实 G5 的"多年参照 + 独立短窗"同构）。

合并评估变体（蓝图 §6 提议，T39）：多品种卡"交易数按全部品种合计过
G1 门槛 + 同时要求每个品种净利为正"——只出各基线在负对照下的通过率
数据，不改任何门槛。

**决策规则（写进产出报告；只建议，不自动改门槛）**：真实（校准后）夏普
1.0 档某关卡子检查检出率 < 50%，或负对照误报率（任一零假设）> 5% →
报告"门槛调整建议"一节列出（附数值与口径）；修改 config/gates.toml
须经用户批准，并同一提交更新 FROZEN_GATES_SHA256（蓝图附录 B"改动须
重新冻结"）。结构性通过检查（G0 / G2.buy_hold_benchmark / G2.cross_symbol
/ G4.mc_drawdown / G5.trade_count——检验代码卫生、适用性、运维口径或
离群而非"有没有优势"，负对照高通过率属预期）不参与标记，但仍在汇总表
中如实呈现。

实验口径与正式关卡的不同处（报告注明）：
- G2 随机入场蒙特卡洛降至 50 次（--g2-random-sims，正式关卡 200 不变）——
  计算量控制，随机抽样的分位数稳定性略有下降；
- G4 的 n_trials = 1（SR₀ = 0，DSR = PSR(0)）：实验不经过 screen/台账的
  网格选择，如实按"单次试验"计，选择效应修正为 0；
- 正对照单品种运行 → G2 cross_symbol 不适用（passed=True，与单品种卡的
  正式行为一致）；负对照按卡加载全部品种；
- 不跑真实 G5（锁箱一次性，实验不消耗机会）；G0 全程参与（工具策略必须
  干净）；G5 段是**判定逻辑的实验镜像**（evaluate_g5_checks），见上。

计算量（主会话实跑，估计）：负对照全套（6 基线 × 2 零假设 × 20 种子 ×
G1–G4，含 G3 日历月窗网格内重选；B5/B3 的 M5 序列 29.5 万根）为多小时级
（上一轮全量在 M5 序列上最慢）；正对照每档校准 ≤6 次基准回测 + 种子数 ×
套件。迭代用 --quick（2 种子、只跑 1.0 档、随机入场 3 次、最多 2 万根、
G5 自助 200 次）/ --baselines / --max-bars。

产出：--out 目录（缺省 data/reports/gates/）下 gate-power.json +
gate-power.md（每道关卡 × 每档优势的检出率 / 误报率表 + 合并评估变体 +
G5 判别力 + 门槛调整建议）。**本版（T39）起，旧版 gate-power 产物
（第四轮口径：负对照只有日块置换）对日内卡的误报率一律作废**——见
报告开头注明。
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
from mt5quant.validation import stats as vstats  # noqa: E402
from mt5quant.validation import walkforward as vwf  # noqa: E402
from mt5quant.validation.synthetic import (  # noqa: E402
    PlantedEdgeStrategy,
    RandomEntries,
    block_permute_bars,
    event_bars,
    plant_edge,
    slot_permute_bars,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# 六个基线（gates-calibration-v2-2026-10-25 的选中组合；品种/周期/grid 与
# 策略类 params_schema 一致）——负对照的被测对象。null = 按持仓周期选定的
# 缺省零假设（T39/M4）：当日平仓的卡用 slot（日内零假设），会持隔夜/多日
# 的卡用 day（日块置换），null_basis 写明依据（报告引用）。
BASELINES: tuple[dict, ...] = (
    {"name": "H002-Donchian", "null": "day",
     "null_basis": "多日持仓（Donchian 40d 入场 / 4d 出场，H1）",
     "factory": lambda seed: DonchianBreakout(),
     "params": {"entry_days": 40, "exit_days": 4},
     "grid": {"entry_days": [10, 20, 40], "exit_days": [2, 4, 8]},
     "symbols": ("XAUUSD", "BTCUSD"), "timeframe": "H1"},
    {"name": "H003-OpeningRange", "null": "slot",
     "null_basis": "当日平仓（纽约 15/14 点整点收盘离场）",
     "factory": lambda seed: NyOpeningRangeBreakout(),
     "params": {"exit_hour_ny": 15, "min_range_atr": 0.5},
     "grid": {"exit_hour_ny": [14, 15], "min_range_atr": [0.0, 0.5, 1.0]},
     "symbols": ("USTEC",), "timeframe": "M5"},
    {"name": "H004-LondonBreakout", "null": "slot",
     "null_basis": "当日平仓（伦敦 11/12 点整点收盘离场）",
     "factory": lambda seed: LondonAsianBreakout(),
     "params": {"exit_hour_ldn": 11, "min_range_atr": 0.0},
     "grid": {"exit_hour_ldn": [11, 12], "min_range_atr": [0.0, 0.5, 1.0]},
     "symbols": ("EURUSD", "XAUUSD"), "timeframe": "M15"},
    {"name": "H005-MeanReversion", "null": "day",
     "null_basis": "可持隔夜（信号层时间止损 96 根 M15 = 一日，非当日强制平仓）",
     "factory": lambda seed: TrendFilteredMeanReversion(),
     "params": {"z_n": 40, "z_entry": 2.5, "trend_n": 200},
     "grid": {"z_n": [20, 40, 80], "z_entry": [2.0, 2.5, 3.0],
              "trend_n": [50, 100, 200]},
     "symbols": ("XAUUSD",), "timeframe": "M15"},
    {"name": "H006-IntradayMomentum", "null": "slot",
     "null_basis": "当日平仓（开盘头半小时定向 → 尾盘半小时平仓）",
     "factory": lambda seed: IntradayMomentumNy(),
     "params": {"min_ret_atr": 1.5},
     "grid": {"min_ret_atr": [0.0, 0.5, 1.0, 1.5]},
     "symbols": ("USTEC",), "timeframe": "M5"},
    {"name": "H007-VolTarget", "null": "day",
     "null_basis": "多日持仓（波动率目标多头，常驻持仓）",
     "factory": lambda seed: VolTargetLong(),
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

# 两种零假设（T39/M4）：day = 日块置换（多日持仓卡）；slot = 同一纽约
# 本地时刻的 bar 收益跨日期置换（日内卡）。--null-hypothesis 按卡的
# 持仓周期选（BASELINES[].null），both = 各跑一遍。
NULL_SHUFFLERS = {"day": block_permute_bars, "slot": slot_permute_bars}
NULL_ORDER = ("slot", "day")               # both 模式的执行/报告顺序


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

def merged_evaluation(per_symbol: dict, timeframe: str, cfg) -> dict:
    """合并评估变体（蓝图 §6 提议，T39；只出数据，不改门槛）。

    多品种卡按"交易数 = 全部品种合计 ≥ G1 门槛（trade_count_min 按周期
    分档）且每个品种净利为正"判过。per_symbol: {symbol: {"trade_count",
    "net_profit"}}。
    """
    threshold = vgate.trade_count_min(timeframe, cfg)
    total = int(sum(int(v.get("trade_count") or 0) for v in per_symbol.values()))
    each_positive = bool(per_symbol) and all(
        float(v.get("net_profit") or 0.0) > 0 for v in per_symbol.values())
    return {"merged_trade_count": total, "threshold": int(threshold),
            "each_positive": each_positive,
            "merged_pass": bool(total >= threshold and each_positive)}


def negative_control(baselines, *, seeds, cfg, bars_by_symbol, costs_by_symbol,
                     sizes_by_symbol, vols_by_symbol, initial_capital=100_000.0,
                     risk_fraction=0.005, session_policy=None,
                     session_policies=None, null="day",
                     log=None) -> dict:
    """打乱数据 × baselines × seeds → {"runs", "false_positive_rates",
    "merged", "merged_pass_rates"}。

    baselines: (name, strategy_factory, params, grid, timeframe) 五元组；
    strategy_factory(seed) 按种子构造策略（基线忽略种子，RandomEntries 用
    它换随机事件模式）。bars_by_symbol 为该组基线的品种 K 线（同一次调用
    内共享品种/周期）。每个种子对每个品种做一次零假设打乱（null: "day"
    日块置换 | "slot" 槽位置换，T39/M4；同品种跨基线同种子共享同一次
    打乱——配对设计），然后跑完整关卡套件；通过率即误报率。

    多品种基线同时产出合并评估变体（merged_evaluation）：主品种取套件的
    基准运行，其余品种各跑一次基准回测（同一次打乱），合并判据单独记录、
    不进关卡通过率。
    """
    if null not in NULL_SHUFFLERS:
        raise ValueError(f"null 必须是 {sorted(NULL_SHUFFLERS)}，收到 {null!r}")
    shuffle = NULL_SHUFFLERS[null]
    policies = dict(session_policies or {})
    runs: list[dict] = []
    merged: list[dict] = []
    for name, factory, params, grid, timeframe in baselines:
        for seed in seeds:
            t0 = time.monotonic()
            shuffled = {sym: shuffle(bars, seed=seed)
                        for sym, bars in bars_by_symbol.items()}
            primary = next(iter(shuffled))
            suite = run_gate_suite(
                factory(seed), params, timeframe=timeframe,
                bars_by_symbol=shuffled, costs_by_symbol=costs_by_symbol,
                sizes_by_symbol=sizes_by_symbol, vols_by_symbol=vols_by_symbol,
                cfg=cfg, seed=seed, grid=grid,
                initial_capital=initial_capital,
                risk_fraction=risk_fraction, session_policy=session_policy)
            flat = flatten_checks(suite)
            runs.append({
                "name": name, "seed": int(seed), "null": null,
                "sharpe": suite["summary"]["sharpe"],
                "net_profit": suite["summary"]["net_profit"],
                "trade_count": suite["summary"]["trade_count"],
                "passed_all": bool(all(flat.values())),
                "checks": flat,
            })
            if len(shuffled) > 1:            # 合并评估变体（蓝图 §6）
                per_symbol = {primary: {
                    "trade_count": suite["summary"]["trade_count"],
                    "net_profit": suite["summary"]["net_profit"]}}
                for sym, s_bars in shuffled.items():
                    if sym == primary:
                        continue
                    extra = run_backtest(
                        factory(seed), s_bars, costs_by_symbol[sym],
                        symbol=sym, params=dict(params),
                        contract_size=sizes_by_symbol[sym], volume_lots=None,
                        sizing=RiskSizing(risk_fraction=float(risk_fraction)),
                        vol_min=vols_by_symbol[sym][0],
                        vol_step=vols_by_symbol[sym][1],
                        vol_max=vols_by_symbol[sym][2],
                        initial_capital=initial_capital,
                        timeframe=timeframe,
                        session_policy=policies.get(sym, session_policy))
                    s = summarize(extra)
                    per_symbol[sym] = {"trade_count": s["trade_count"],
                                       "net_profit": s["net_profit"]}
                merged.append({
                    "name": name, "seed": int(seed), "null": null,
                    "per_symbol": per_symbol,
                    **merged_evaluation(per_symbol, timeframe, cfg)})
            if log:
                log(f"[negative/{null}] {name} seed={seed} "
                    f"sharpe={suite['summary']['sharpe']} "
                    f"({time.monotonic() - t0:.1f}s)")
    merged_rates: dict[str, float] = {}
    if merged:
        buckets: dict[str, list[bool]] = {}
        for row in merged:
            buckets.setdefault(f"{row['name']}|{row['null']}", []) \
                .append(bool(row["merged_pass"]))
        merged_rates = {k: float(np.mean(v)) for k, v in buckets.items()}
    return {"runs": runs, "false_positive_rates": aggregate_rates(runs),
            "merged": merged, "merged_pass_rates": merged_rates}


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


# ---------------------------------------------------------------- G5 判别力

def load_lockbox_cutoff(data_root) -> pd.Timestamp | None:
    """锁箱 cutoff 日期（窗口长度折算用）：只读 lockbox.json 的
    global_cutoff 这一个**日期**，绝不读锁箱段 K 线（研究隔离，T38/H4）。
    data_root 没有（研究库）时退回主库的 lockbox.json，再没有 → None。"""
    for path in (Path(data_root) / "lockbox.json",
                 REPO_ROOT / "data" / "lockbox.json"):
        if path.is_file():
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
                cutoff = obj.get("global_cutoff")
                if cutoff:
                    return pd.Timestamp(cutoff)
            except (OSError, ValueError):
                continue
    return None


def g5_window_bars(bars: pd.DataFrame, timeframe: str, *, cutoff=None,
                   override=None, now=None) -> int:
    """与锁箱**等长**的合成窗口长度（bar 数，T39/M5）。

    override 显式给定；否则 = 研究段实测 bars/year × 锁箱已历时长
    （cutoff → now；cutoff 缺失按 1.0 年），并夹在 [一个交易日的 bar 数,
    序列长度] 内（序列不够长时如实取整段，报告注明截断）。
    """
    if override:
        return int(override)
    times = bars["time"]
    bpy = observed_bars_per_year(times) or bars_per_year(timeframe.upper())
    years = 1.0
    if cutoff is not None:
        # 统一折算成 tz-naive UTC 再相减（lockbox.json 日期无时区后缀）
        now_ts = pd.Timestamp(now) if now is not None else pd.Timestamp(
            datetime.now(timezone.utc))
        for_name = lambda ts: (ts.tz_convert("UTC").tz_localize(None)
                               if ts.tz is not None else ts)  # noqa: E731
        elapsed = (for_name(now_ts) - for_name(pd.Timestamp(cutoff))).days
        years = max(float(elapsed), 1.0) / 365.25
    want = int(round(float(bpy) * years))
    return max(bars_per_day_of(timeframe), min(want, len(bars)))


def evaluate_g5_checks(lockbox_result: dict, reference_returns,
                       expected_trades, *, cfg, bars_per_day: int,
                       bars_per_year: float, seed: int) -> dict:
    """G5 单侧判定的**实验镜像**（T39/M5；正式判定在 gates.run_g5，冻结
    不改，且只在 bt --approve 路径消耗一次性机会）。

    与 run_g5 同口径：参照分布 = reference_returns 按锁箱长度块自助
    （块长 = 一个交易日的 bar 数）的年化夏普分布；检查 expectation_positive
    （锁箱期望 > 0）/ trade_count（≥ cfg.lockbox_min_trades_ratio × 预期）/
    sharpe_vs_reference（锁箱夏普 ≥ p5，单侧；高于 p95 只记 warning 不判
    失败）。种子为实验种子（正式路径用 rng_seed(卡 id)）。
    """
    boot = vstats.block_bootstrap_sharpes(
        reference_returns, window_len=int(lockbox_result["bar_count"]),
        block_len=int(bars_per_day), sims=int(cfg.lockbox_bootstrap_sims),
        seed=int(seed), bars_per_year=float(bars_per_year))
    p5 = float(np.percentile(boot, cfg.lockbox_sharpe_min_percentile))
    p95 = float(np.percentile(boot, cfg.lockbox_warn_percentile))
    expectancy = lockbox_result.get("expectancy")
    sharpe = lockbox_result.get("sharpe")
    trades = lockbox_result.get("trade_count")
    expected = float(expected_trades) if expected_trades is not None else None
    checks: dict[str, bool] = {
        "expectation_positive": bool(
            expectancy is not None and expectancy > 0),
        "trade_count": bool(
            trades is not None and expected is not None
            and trades >= cfg.lockbox_min_trades_ratio * expected),
        "sharpe_vs_reference": bool(sharpe is not None and sharpe >= p5),
    }
    result = {
        "passed": bool(all(checks.values())),
        "checks": checks,
        "metrics": {k: lockbox_result.get(k) for k in
                    ("expectancy", "sharpe", "trade_count", "bar_count")},
        "reference": {"p5": p5, "p95": p95,
                      "sims": int(cfg.lockbox_bootstrap_sims),
                      "window_len": int(lockbox_result["bar_count"]),
                      "block_len": int(bars_per_day),
                      "bars_per_year": float(bars_per_year)},
        "seed": int(seed),
    }
    if sharpe is not None and sharpe > p95:
        result["warning"] = (f"锁箱夏普 {sharpe:.2f} 高于参照分布 "
                             f"p95（{p95:.2f}）——异常好，复核数据与前视")
    return result


def _lockbox_metrics(result, *, bars_per_year: float) -> dict:
    """回测结果 → run_g5/镜像共用的 lockbox_result（口径同 bt.py 的
    _g5_orchestrate：期望 = 每笔净利均值；夏普 = 实测年化）。"""
    trades = result.trades
    summary = summarize(result)
    sharpe = vrob.equity_sharpe(result.equity.astype("float64"),
                                result.bars["time"], result.timeframe)
    return {
        "expectancy": (float(trades["net"].mean()) if len(trades)
                       else None),
        "sharpe": sharpe,
        "trade_count": int(summary["trade_count"]),
        "bar_count": int(len(result.bars)),
        "annualization_bars_per_year": float(bars_per_year),
        "net_profit": summary["net_profit"],
    }


def g5_power(setups, *, seeds, levels, cfg, bars_by_symbol, costs_by_symbol,
             sizes_by_symbol, vols_by_symbol, session_policy=None,
             session_policies=None, nulls=NULL_ORDER, window_bars=None,
             cutoff=None, initial_capital=100_000.0, risk_fraction=0.005,
             log=None) -> dict:
    """G5 判别力实验（T39/M5）：与锁箱等长的合成窗口 × G5 单侧判定。

    设计（报告"口径备注"同步写明）：
    - 窗口：研究段按种子错开取窗（offset = (seed%10) × 每日 bar 数），长度
      = g5_window_bars（与锁箱等长）；
    - 参照分布（G3 拼接样本外收益的替身）：同一策略在**全研究段**无植入
      （drift=0）运行的权益收益（与真实 G5 同构：参照来自多年研究段、锁箱
      是独立短窗），块自助（阈值/次数与冻结 gates.toml 同源，种子 = 实验
      种子）；expected_trades = 研究段交易频率 × 窗口长（bt.py 同式）；
    - 检出臂：plant_edge 植入该档校准漂移 → 镜像判定 → G5.* 通过率 =
      检出率；误报臂：两种零假设打乱（drift=0）→ 镜像判定 → 通过率 =
      误报率。不读锁箱 K 线、不写台账、不消耗一次性机会。
    """
    policies = dict(session_policies or {})
    runs: list[dict] = []
    detection: dict[str, dict[str, dict[str, float]]] = {}
    fp_buckets: dict[str, list[dict]] = {null: [] for null in nulls}
    windows: dict[str, int] = {}
    for symbol, timeframe, level_cal, period_days, interval_hours, hold_bars \
            in setups:
        key = f"{symbol} {timeframe}"
        bars = bars_by_symbol[symbol]
        costs = costs_by_symbol[symbol]
        vols = vols_by_symbol[symbol]
        sizing = RiskSizing(risk_fraction=float(risk_fraction))
        policy = policies.get(symbol, session_policy)
        length = int(window_bars or g5_window_bars(bars, timeframe,
                                                   cutoff=cutoff))
        windows[key] = length
        bpd = bars_per_day_of(timeframe)
        by_level: dict[str, list[dict]] = {str(level): [] for level in levels}
        for level in levels:
            drift = float(level_cal[level]["drift"])
            for seed in seeds:
                t0 = time.monotonic()
                phase_hours, day_phase = seed_phases(seed, interval_hours,
                                                     period_days)
                offset = (int(seed) % 10) * bpd
                window = bars.iloc[offset: offset + length]
                if len(window) < length:
                    window = bars.iloc[-length:]
                window = window.reset_index(drop=True)
                strat = PlantedEdgeStrategy(
                    period_days=period_days, interval_hours=interval_hours,
                    phase_hours=phase_hours, day_phase=day_phase)
                params = {"hold_bars": int(hold_bars)}
                bpy = (observed_bars_per_year(window["time"])
                       or bars_per_year(timeframe.upper()))
                # 参照：全研究段无植入运行（与真实 G5 的"参照=多年研究段、
                # 锁箱=独立短窗"同构；同窗参照会把 p5 压得结构性地低）
                ref = run_backtest(
                    strat, bars, costs, symbol=symbol, params=dict(params),
                    contract_size=sizes_by_symbol[symbol], volume_lots=None,
                    sizing=sizing, vol_min=vols[0], vol_step=vols[1],
                    vol_max=vols[2], initial_capital=initial_capital,
                    timeframe=timeframe, session_policy=policy)
                reference_returns = ref.equity.astype("float64") \
                    .pct_change().dropna()
                expected_trades = (float(summarize(ref).get("trade_count") or 0)
                                   * length / max(1, len(bars)))
                arms = ([("detection", plant_edge(
                    window, period_days=period_days,
                    interval_hours=interval_hours, hold_bars=hold_bars,
                    drift=drift, phase_hours=phase_hours,
                    day_phase=day_phase))]
                    + [(f"false_positive/{null}",
                        NULL_SHUFFLERS[null](window, seed=seed))
                       for null in nulls])
                for arm, data in arms:
                    res = run_backtest(
                        strat, data, costs, symbol=symbol, params=dict(params),
                        contract_size=sizes_by_symbol[symbol],
                        volume_lots=None, sizing=sizing, vol_min=vols[0],
                        vol_step=vols[1], vol_max=vols[2],
                        initial_capital=initial_capital,
                        timeframe=timeframe, session_policy=policy)
                    metrics = _lockbox_metrics(res, bars_per_year=bpy)
                    verdict = evaluate_g5_checks(
                        metrics, reference_returns, expected_trades,
                        cfg=cfg, bars_per_day=bpd, bars_per_year=bpy,
                        seed=seed)
                    flat = {f"G5.{name}": bool(value)
                            for name, value in verdict["checks"].items()}
                    flat["G5.passed"] = bool(verdict["passed"])
                    runs.append({
                        "setup": key, "arm": arm.split("/")[0],
                        "null": (arm.split("/")[1]
                                 if "/" in arm else None),
                        "level": float(level), "seed": int(seed),
                        "drift": drift if arm == "detection" else 0.0,
                        "sharpe": metrics["sharpe"],
                        "expectancy": metrics["expectancy"],
                        "trade_count": metrics["trade_count"],
                        "p5": verdict["reference"]["p5"],
                        "warned": bool(verdict.get("warning")),
                        "checks": flat,
                    })
                    if arm == "detection":
                        by_level[str(level)].append({"checks": flat})
                    else:
                        fp_buckets[arm.split("/")[1]].append({"checks": flat})
                if log:
                    log(f"[g5] {key} level={level} seed={seed} "
                        f"window={length} ({time.monotonic() - t0:.1f}s)")
        detection[key] = {level: aggregate_rates(bucket)
                          for level, bucket in by_level.items()}
    fp_rates = {null: aggregate_rates(bucket)
                for null, bucket in fp_buckets.items() if bucket}
    return {
        "runs": runs,
        "detection_rates": detection,
        "false_positive_rates": fp_rates,
        "window_bars": windows,
        "bootstrap_sims": int(cfg.lockbox_bootstrap_sims),
        "design": (
            "窗口=研究段子段（长度与锁箱等长：实测 bars/year × cutoff 已历"
            "时长）；参照=全研究段无植入运行的权益收益（G3 拼接样本外收益的"
            "替身，与真实 G5 的多年参照同构），块自助阈值与冻结 gates.toml "
            "同源；检出臂=植入校准漂移，误报臂=零假设打乱（drift=0）；"
            "expected_trades=研究段交易频率×窗口长（bt.py 同式）。"
            "判定为 gates.run_g5 的实验镜像（无台账/批准，不消耗一次性）。"),
    }


# ---------------------------------------------------------------- 决策规则

# 结构性通过检查：检验代码卫生 / 离群 / 适用性而非"有没有优势"，负对照上
# 高通过率属预期（G0 只看因果性；buy_hold/cross_symbol 对不适用情形按通过
# 记录；mc_drawdown 是回撤离群报警，无优势策略的回撤落在重采样分布内是
# 常态；G5.trade_count 是"按预期频率交易"的运维口径检查——事件驱动的
# 策略在打乱数据上照常触发事件，通过属预期）。这些检查仍出现在误报率
# 表里，但**不参与**决策规则的标记——决策规则针对的是"优势判别器"。
STRUCTURAL_CHECKS = frozenset({
    "G0", "G2.buy_hold_benchmark", "G2.cross_symbol", "G4.mc_drawdown",
    "G5.trade_count",
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
    """Markdown 汇总：每道关卡 × 每档优势的检出率 / 误报率（按零假设分列）
    + 合并评估变体 + G5 判别力 + 决策规则。"""
    meta = results.get("meta", {})
    levels = meta.get("levels") or list(DEFAULT_LEVELS)
    null_mode = meta.get("null_hypothesis", "auto")
    lines = [
        "# 关卡正负对照实验（gate-power，T22/H7 + T39 收尾）", "",
        "> **旧版作废**：T39（第五轮审核 M4）判定日块置换对日内策略不是"
        "零假设——此前 gate-power 产物（第四轮口径，负对照只有日块置换）中"
        "日内卡（H003/H004/H006 等）的误报率一律作废，以本报告为准。",
        "",
        f"- 阶段：{meta.get('phase', '?')}；种子数：{meta.get('seeds', '?')}；"
        f"夏普档位：{levels}",
        f"- 零假设：--null-hypothesis {null_mode}"
        + ("（按卡持仓周期选：日内卡 slot、多日持仓卡 day）"
           if null_mode == "auto" else ""),
        f"- G2 随机入场蒙特卡洛：本实验降至 {meta.get('g2_random_entry_sims', '?')}"
        " 次（正式关卡 200 次不变）",
        f"- 生成：{meta.get('generated_utc', '?')}（gate_calibration.py）", "",
        "## 决策规则", "",
        f"真实（校准后）夏普 {DECISION_LEVEL} 档某关卡子检查检出率 "
        f"< {LOW_DETECTION:.0%}，或负对照误报率（**任一零假设**）"
        f"> {HIGH_FALSE_POSITIVE:.0%}，即在下一节给出门槛调整建议；"
        "**只建议**——修改 config/gates.toml 须经用户批准，并在同一提交"
        "更新 FROZEN_GATES_SHA256 重新冻结。", "",
    ]
    negative = results.get("negative") or {}
    by_null: dict = negative.get("by_null") or {}
    if by_null:
        nulls = [n for n in NULL_ORDER if n in by_null]
        lines += ["## 负对照误报率（打乱数据 × 基线/随机策略 × 种子）", "",
                  "按零假设分列：day = 日块置换（保留每日日内形状，破坏跨日"
                  "次序；多日持仓卡的零假设）；slot = 槽位置换（同一纽约本地"
                  "时刻的 bar 收益跨日期置换，保留各时段波动分布，破坏日内"
                  "先后依赖与跨日次序；日内卡的零假设，T39/M4）。",
                  "",
                  "| 关卡子检查 | " + " | ".join(
                      f"{n} 置换" for n in nulls) + " |",
                  "| --- " * (len(nulls) + 1) + "|"]
        checks = sorted({c for rates in by_null.values() for c in rates})
        for check in checks:
            row = [_fmt_pct(by_null.get(n, {}).get(check)) for n in nulls]
            lines.append(f"| {check} | " + " | ".join(row) + " |")
        lines.append("")
    else:
        fp = negative.get("false_positive_rates") or {}
        if fp:
            lines += ["## 负对照误报率（打乱数据 × 基线/随机策略 × 种子）", "",
                      "| 关卡子检查 | 误报率 |", "| --- | --- |"]
            for check in sorted(fp):
                lines.append(f"| {check} | {_fmt_pct(fp[check])} |")
            lines.append("")
    merged_rates = negative.get("merged_pass_rates") or {}
    if merged_rates:
        lines += ["## 合并评估变体（蓝图 §6 提议；只出数据，不改门槛）", "",
                  "判据：多品种卡交易数按全部品种合计 ≥ G1 门槛"
                  "（日内 300 / 小时线 150），**且**每个品种净利为正。"
                  "下表为负对照下的通过率——即这种评估方式在零假设下的"
                  "误报率，供决策是否在卡模板启用，本实验不据此改任何门槛。",
                  "",
                  "| 基线 \\ 零假设 | " + " | ".join(
                      f"{n} 置换" for n in NULL_ORDER) + " |",
                  "| --- " * (len(NULL_ORDER) + 1) + "|"]
        names = sorted({k.split("|")[0] for k in merged_rates})
        for name in names:
            row = [_fmt_pct(merged_rates.get(f"{name}|{n}"))
                   for n in NULL_ORDER]
            lines.append(f"| {name} | " + " | ".join(row) + " |")
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
    g5 = results.get("g5") or {}
    if g5:
        windows = g5.get("window_bars") or {}
        lines += ["## G5 判别力（与锁箱等长的合成窗口；非真实 G5）", "",
                  f"窗口长度（bar 数）：{windows or '?'}；自助 "
                  f"{g5.get('bootstrap_sims', '?')} 次。设计：参照分布 = 同一"
                  "策略全研究段无植入运行的权益收益（G3 拼接样本外收益的"
                  "替身）块自助；检出臂 = 植入校准漂移；误报臂 = 零假设打乱"
                  "（drift=0）。判定为 gates.run_g5 的**实验镜像**（同阈值同"
                  "口径，无台账/批准，不消耗一次性机会）。", ""]
        g5_det = g5.get("detection_rates") or {}
        for key, by_level in g5_det.items():
            lines += [f"### G5 检出率：{key}", "",
                      "| 检查 | " + " | ".join(
                          f"夏普≈{lv}" for lv in levels) + " |",
                      "| --- " * (len(levels) + 1) + "|"]
            checks = sorted({c for rates in by_level.values() for c in rates})
            for check in checks:
                row = [_fmt_pct(by_level.get(str(lv), {}).get(check))
                       for lv in levels]
                lines.append(f"| {check} | " + " | ".join(row) + " |")
            lines.append("")
        g5_fp = g5.get("false_positive_rates") or {}
        if g5_fp:
            nulls = [n for n in NULL_ORDER if n in g5_fp]
            lines += ["### G5 误报率（零假设打乱窗口）", "",
                      "| 检查 | " + " | ".join(
                          f"{n} 置换" for n in nulls) + " |",
                      "| --- " * (len(nulls) + 1) + "|"]
            checks = sorted({c for rates in g5_fp.values() for c in rates})
            for check in checks:
                row = [_fmt_pct(g5_fp.get(n, {}).get(check)) for n in nulls]
                lines.append(f"| {check} | " + " | ".join(row) + " |")
            lines.append("")
    flags = results.get("flags") or []
    lines += ["## 门槛调整建议", ""]
    if flags:
        for flag in flags:
            lines.append(f"- **{flag['check']}**：{flag['rule']}——"
                         f"实测 {flag['value'] * 100:.0f}%。"
                         "建议复核该门槛（调整方案与证据请交用户批准）。")
    else:
        lines += [f"无——夏普 {DECISION_LEVEL} 档所有关卡子检查检出率 "
                  f"≥ {LOW_DETECTION:.0%}，负对照误报率均 "
                  f"≤ {HIGH_FALSE_POSITIVE:.0%}。"]
    lines += ["", "## 口径备注", "",
              "- 负对照零假设按卡持仓周期选（T39/M4）：日内卡（当日平仓，"
              "H003/H004/H006 与随机策略）用槽位置换，多日持仓卡"
              "（H002/H005/H007）用日块置换；本报告各表分列呈现。基线参数"
              "取 gates-calibration-v2 选中组合。",
              "- 正对照：事件 bar 后 k 根 OHLC 一致 +d；PlantedEdgeStrategy "
              "只交易事件；d 由先验公式 + 割线迭代校准（详见 JSON 的 "
              "calibration.history）。",
              "- G4 n_trials=1（实验不经网格选择，选择效应为 0）；G2 跨品种"
              "检查在单品种运行中不适用（按通过记录，与单品种卡正式行为一致）。",
              "- 结构性通过检查（G0、G2.buy_hold_benchmark、G2.cross_symbol、"
              "G4.mc_drawdown、G5.trade_count）检验代码卫生/离群/适用性/"
              "运维口径而非优势判别，负对照高通过率属预期——仍列于上表，"
              "但不参与决策规则标记。",
              "- G5 段为判定逻辑的实验镜像（evaluate_g5_checks）：阈值与冻结"
              "的 gates.toml 同源，但无台账、无批准、不消耗一次性机会；"
              "真实 G5 仍只在 bt --approve 路径跑（锁箱段 K 线从未被本实验"
              "读取，窗口长度只用了 lockbox.json 的 cutoff 日期）。",
              "- 合并评估变体（蓝图 §6）只出数据：启用与否须待正式决策，"
              "不在本实验改任何门槛。", ""]
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
        SESSIONS_PATH, build_costs, classify_store, entry_policy_for,
        load_series, resolve_contract_size, sessions_sha256,
    )

    sess_policy = load_session_policy(str(SESSIONS_PATH))
    sess_sha = sessions_sha256(str(SESSIONS_PATH))
    # T38/D24：研究库走 research_store 成本路径（无 tick、缓存必须存在）；
    # 主库/自定义根按原口径（水位对照/直建回退）
    research_store = classify_store(str(data_root)) == "research"
    out: dict[str, dict] = {}
    for key in keys:
        symbol, timeframe = key.split()
        bars = load_series(data_root, symbol, timeframe, None)
        if max_bars and len(bars) > int(max_bars):
            bars = bars.iloc[-int(max_bars):].reset_index(drop=True)
        costs, _version = build_costs(data_root, symbol, 0.1, 0.0,
                                      sessions_sha=sess_sha,
                                      research_store=research_store)
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
        description="T22+T39 关卡正负对照实验：负对照误报率（两种零假设）"
                    "+ 正对照检出率 + G5 判别力 + 合并评估变体 → JSON+MD")
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
    ap.add_argument("--null-hypothesis",
                    choices=("auto", "day", "slot", "both"), default="auto",
                    help="负对照零假设（T39/M4）：auto=按卡持仓周期选（日内卡"
                         " slot、多日持仓卡 day）；day/slot=全部强制；"
                         "both=两种各跑一遍（正式实验口径）")
    ap.add_argument("--g5-window-bars", type=int, default=None,
                    help="G5 判别力实验的窗口长度（缺省 = 实测 bars/year × "
                         "锁箱已历时长，与锁箱等长；只读 cutoff 日期不读锁箱"
                         " K 线）")
    ap.add_argument("--g5-bootstrap-sims", type=int, default=None,
                    help="G5 参照分布自助次数（缺省用冻结 gates.toml 的 2000；"
                         "--quick 降至 200）")
    ap.add_argument("--out", default=str(REPO_ROOT / "data" / "reports" / "gates"))
    ap.add_argument("--data-root", default="data_research",
                    help="研究库数据根（T38 缺省 data_research；主库须显式 "
                         "指 data）")
    ap.add_argument("--initial-capital", type=float, default=100_000.0)
    ap.add_argument("--risk-per-trade", type=float, default=0.005)
    ap.add_argument("--max-bars", type=int, default=None,
                    help="每序列只保留最近 N 根（迭代用；正式实验不截）")
    ap.add_argument("--g2-random-sims", type=int,
                    default=EXPERIMENT_G2_RANDOM_SIMS,
                    help="实验用 G2 随机入场次数（正式关卡 200 不变）")
    ap.add_argument("--quick", action="store_true",
                    help="迭代模式：2 种子、只跑 1.0 档、随机入场 3 次、"
                         "最多 20000 根、G5 自助 200 次")
    return ap


def nulls_for(mode: str, baseline_default: str) -> list[str]:
    """--null-hypothesis 模式 → 本次要跑的零假设列表（T39/M4）。"""
    if mode == "auto":
        return [baseline_default]
    if mode == "both":
        return list(NULL_ORDER)
    if mode in NULL_SHUFFLERS:
        return [mode]
    raise ValueError(f"未知零假设模式 {mode!r}")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    seeds = list(range(args.seeds))
    levels = [float(x) for x in str(args.levels).split(",") if x.strip()]
    g2_sims = args.g2_random_sims
    max_bars = args.max_bars
    g5_sims = args.g5_bootstrap_sims
    if args.quick:
        seeds = seeds[:2]
        levels = [DECISION_LEVEL]
        g2_sims = min(g2_sims, 3)
        max_bars = min(max_bars or 10**9, 20_000)
        g5_sims = min(g5_sims or 10**9, 200)
    cfg = load_gates_for_run()
    cfg = dataclasses.replace(cfg, random_entry_sims=int(g2_sims))
    g5_cfg = (dataclasses.replace(cfg, lockbox_bootstrap_sims=int(g5_sims))
              if g5_sims else cfg)
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
            policies[sym] = pack["session_policy"]
        return dict(bars_by_symbol=bars_by_symbol,
                    costs_by_symbol=costs_by_symbol,
                    sizes_by_symbol=sizes_by_symbol,
                    vols_by_symbol=vols_by_symbol,
                    session_policy=policies[entry["symbols"][0]],
                    session_policies=policies)

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
    lb_cutoff = load_lockbox_cutoff(args.data_root)

    results: dict = {"meta": {
        "phase": args.phase, "seeds": len(seeds), "levels": levels,
        "g2_random_entry_sims": int(g2_sims),
        "null_hypothesis": args.null_hypothesis,
        "null_defaults": {b["name"]: b.get("null", "day")
                          for b in baselines},
        "null_basis": {b["name"]: b.get("null_basis", "")
                       for b in baselines},
        "g5_bootstrap_sims": int(g5_cfg.lockbox_bootstrap_sims),
        "g5_window_source": ("override" if args.g5_window_bars
                             else "lockbox_cutoff"),
        "g5_cutoff": str(lb_cutoff) if lb_cutoff is not None else None,
        "initial_capital": args.initial_capital,
        "risk_per_trade": args.risk_per_trade,
        "max_bars": max_bars, "baselines": [b["name"] for b in baselines],
        "positive_symbols": positive_keys,
        "engine_note": "run_gate_suite: n_trials=1（选择效应为 0），"
                       "G2 随机入场实验降次；G5 为实验镜像（无台账/批准，"
                       "不消耗一次性机会，锁箱 K 线未读取）",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
    }}
    if args.phase in ("negative", "both"):
        runs: list[dict] = []
        merged: list[dict] = []
        for entry in baselines:
            for null in nulls_for(args.null_hypothesis,
                                  entry.get("null", "day")):
                sub = negative_control(
                    [(entry["name"], entry["factory"], entry["params"],
                      entry["grid"], entry["timeframe"])],
                    seeds=seeds, cfg=cfg, null=null,
                    initial_capital=args.initial_capital,
                    risk_fraction=args.risk_per_trade, log=log,
                    **_suite_inputs(entry))
                runs.extend(sub["runs"])
                merged.extend(sub["merged"])
        for key in positive_keys:      # 随机策略（同样在打乱数据上；日内持有）
            symbol, timeframe = key.split()
            pack = inputs_by_key[key]
            for null in nulls_for(args.null_hypothesis, "slot"):
                sub = negative_control(
                    [(f"RND@{key}",
                      lambda seed: RandomEntries(seed=int(seed), hold_bars=4,
                                                 event_every_bars=24), {}, {},
                      timeframe)],
                    seeds=seeds, cfg=cfg, null=null,
                    bars_by_symbol={symbol: pack["bars"]},
                    costs_by_symbol={symbol: pack["costs"]},
                    sizes_by_symbol={symbol: pack["sizes"]},
                    vols_by_symbol={symbol: pack["vols"]},
                    initial_capital=args.initial_capital,
                    risk_fraction=args.risk_per_trade,
                    session_policy=pack["session_policy"], log=log)
                runs.extend(sub["runs"])
                merged.extend(sub["merged"])
        # 误报率按零假设分列；决策规则取任一零假设的最差值（最保守）
        buckets: dict[tuple, list] = {}
        for run in runs:
            for check, value in run["checks"].items():
                buckets.setdefault((run.get("null", "day"), check), []) \
                    .append(bool(value))
        by_null: dict[str, dict[str, float]] = {}
        for (null, check), values in buckets.items():
            by_null.setdefault(null, {})[check] = float(np.mean(values))
        worst = {check: max(rates.get(check, 0.0)
                            for rates in by_null.values())
                 for check in {c for r in by_null.values() for c in r}}
        mbuckets: dict[str, list[bool]] = {}
        for row in merged:
            mbuckets.setdefault(f"{row['name']}|{row['null']}", []) \
                .append(bool(row["merged_pass"]))
        results["negative"] = {
            "runs": runs, "by_null": by_null,
            "false_positive_rates": worst,
            "merged": merged,
            "merged_pass_rates": {k: float(np.mean(v))
                                  for k, v in mbuckets.items()}}

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
        results["g5"] = g5_power(
            setups, levels=levels, seeds=seeds, cfg=g5_cfg,
            nulls=list(NULL_ORDER), window_bars=args.g5_window_bars,
            cutoff=lb_cutoff,
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

    detection_for_flags = (results.get("positive", {}).get("detection_rates")
                           or {})
    fp_rates = dict((results.get("negative") or {})
                    .get("false_positive_rates") or {})
    g5_block = results.get("g5") or {}
    for null, rates in (g5_block.get("false_positive_rates") or {}).items():
        for check, rate in rates.items():
            if check == "G5.passed":        # 汇总数据，不进决策规则
                continue
            fp_rates[check] = max(fp_rates.get(check, 0.0), float(rate))
    level_rates: dict[str, float] = {}
    for key, by_level in (detection_for_flags
                          or g5_block.get("detection_rates") or {}).items():
        for check, rate in (by_level.get(str(DECISION_LEVEL)) or {}).items():
            if check == "G5.passed":
                continue
            prev = level_rates.get(check)
            level_rates[check] = rate if prev is None else min(prev, rate)
    if level_rates or fp_rates:
        results["flags"] = collect_flags(level_rates, fp_rates)
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
