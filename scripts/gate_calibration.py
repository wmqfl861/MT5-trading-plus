"""gate_calibration：T22 关卡正负对照实验（H7 判别力）+ T39 收尾
（M4 日内零假设 / M5 G5 判别力 / 合并评估变体）+ T66/D35 事件模式对照臂。

用法（Git Bash，仓库根）：
    .venv/Scripts/python.exe scripts/gate_calibration.py --phase both \
        --seeds 20 --null-hypothesis both --out data/reports/gates/
    .venv/Scripts/python.exe scripts/gate_calibration.py --phase positive --quick
    .venv/Scripts/python.exe scripts/gate_calibration.py --phase negative \
        --baselines H002,H003 --null-hypothesis slot --seeds 5 --max-bars 60000
    .venv/Scripts/python.exe scripts/gate_calibration.py --phase event \
        --event-symbols "USTEC D1" --out data/reports/gates/   # T66/D35
    .venv/Scripts/python.exe scripts/gate_calibration.py --phase v3 \
        --seeds 20 --out data/reports/gates/                    # T65/D34

关卡 v3 对照臂（--phase v3，蓝图 v4 附录 B 提案框架）：G0 数据卫生否决 +
主检验（甲=WF 拼接 OOS 收益的 PSR/DSR、乙=同序列年块自助单侧 p——两方案
逐条都算并标定）+ 三稳健性否决（成本 ×1.5 / 延迟一根 / 多头相对买入持有
超额）+ 诊断（只报告）。正对照=LONG 植入（多头让 buyhold 否决真实参与
判定），档位=扣成本后年化夏普；负对照=day/slot 双零假设。报告**联合**
通过率（主检验+全部否决项，由逐条记录重算）。产物 gate-power-v3.json/md；
--v3-ext-source fred 给延长后（外部数据）对照。事件模式分支引用 T66 标定
（主检验乙 = event_test 同一实现），不在此重标。

事件模式对照臂（--phase event，蓝图 v4 §6.4）：评估单位=事件（月末换月
窗 / 伦敦定盘窗），主统计量=事件净收益均值的单侧检验。零假设=事件时间
随机化（randomize_event_times：保持星期分布与月内三分位、matched 不重叠
抽样池）；正对照=事件窗植入 δ（σ_w 倍数档位）。统计量分列：年块自助
（event_test，与 0 比较——漂移资产上会把漂移当效应）、随机化参照
（randomization_test，漂移稳健）、成本 ×1.5 否决（G2 成本放大的事件模式
类比）与两者的联合口径。数据：已批准外部序列优先（fred USTEC D1 39.8
年——月末事件天然适用），成本来自研究库缓存（与 T62 预筛同一份口径）。
产物 event-power.json / event-power.md（不覆盖 gate-power.*）；与关卡 v3
主检验的对接留 T65。

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
import hashlib
import inspect
import itertools
import json
import math
import subprocess
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


def _run_fingerprint(inputs_by_key: dict, gates_sha: str) -> dict:
    """T42（R7-N-I1）：运行身份指纹——git 提交、工作区干净位、冻结门槛
    哈希、各序列成本版本（含点差缓存/越幅/隔夜费 sha）、本脚本字节哈希。
    记录在产物 meta 里，合并器校验五分片一致；git 不可用时 commit 为空串
    （合并器按"全部相同"处理，报告如实展示 '?'）。"""
    repo = Path(__file__).resolve().parents[1]
    commit, dirty = "", False
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo),
            capture_output=True, text=True, timeout=15).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(repo),
            capture_output=True, text=True, timeout=15).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    script = Path(__file__).resolve()
    return {
        "git_commit": commit,
        "git_dirty": dirty,
        "gates_sha256": str(gates_sha),
        "cost_versions": {key: str(pack.get("cost_version", "") or "")
                          for key, pack in sorted(inputs_by_key.items())},
        "script_sha256": hashlib.sha256(
            script.read_bytes()).hexdigest(),
    }


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
                    log=None, signal_sign: int = -1) -> dict:
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
    signal_sign（T65/D34）：-1 = 原空头植入（缺省，行为逐位不变）；
    +1 = 多头植入（压价 −d + 信号 +1，LongPlantedEdgeStrategy）——d 恒为
    正强度，植入值 = signal_sign×d，history 记 d（强度）。
    """
    bars = bars_by_symbol[symbol]
    costs = costs_by_symbol[symbol]
    vols = vols_by_symbol[symbol]
    contract_size = sizes_by_symbol[symbol]
    phase_hours, day_phase = seed_phases(seed, interval_hours, period_days)
    if int(signal_sign) >= 0:
        strategy = LongPlantedEdgeStrategy(period_days=period_days,
                                           interval_hours=interval_hours,
                                           phase_hours=phase_hours,
                                           day_phase=day_phase)
    else:
        strategy = PlantedEdgeStrategy(period_days=period_days,
                                       interval_hours=interval_hours,
                                       phase_hours=phase_hours,
                                       day_phase=day_phase)
    params = {"hold_bars": int(hold_bars)}

    def plant(drift: float) -> pd.DataFrame:
        # 空头（signal_sign=-1）：价格抬 +d，做空次根开盘卖出、信号归零后
        # 次根回补到未抬价水平 → 每笔毛利 ≈ +d；多头（+1）：价格压 −d，
        # 买入低、卖回未压价水平。植入偏移 = −signal_sign×d，d 恒为正强度。
        return plant_edge(bars, period_days=period_days,
                          interval_hours=interval_hours,
                          hold_bars=hold_bars,
                          drift=-float(signal_sign) * drift,
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
           "tol": float(tol), "max_iter": int(max_iter),
           "converged": bool(abs(best["sharpe"] - target_sharpe) <= tol),
           "signal_sign": int(signal_sign),
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
            f"({'收敛' if out['converged'] else '未收敛'}，"
            f"{out['iterations']} runs)")
    return out


def positive_control(setups, *, levels, seeds, cfg, bars_by_symbol,
                     costs_by_symbol, sizes_by_symbol, vols_by_symbol,
                     initial_capital=100_000.0, risk_fraction=0.005,
                     session_policy=None, session_policies=None,
                     log=None) -> dict:
    """植入已知优势 → 各档 × 各种子的关卡套件 → 检出率。

    setups: (symbol, timeframe, level_cal, period_days, interval_hours,
    hold_bars)；level_cal = {level: calibrate_drift 的完整产出}（N-M1/R7：
    history/target/tol/converged 等完整校准记录原样进 JSON——报告引用
    calibration.history，逐字段可复核；positive_control 只执行不校准）。
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
        calibration[key] = {str(level): dict(info)
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


# ------------------------------------------------------- 事件模式对照（T66/D35）

# 事件模式对照臂（蓝图 v4 §6.4）：评估单位=事件，主统计量=事件净收益均值
# 的单侧检验。零假设=事件时间随机化（保持星期分布与月内位置、matched
# 不重叠抽样池）；正对照=事件窗植入已知效应 δ（σ_w 倍数档位）。三个
# 统计量分列：年块自助（event_test，与 0 比较）、随机化参照
# （randomization_test，漂移稳健）、成本 ×1.5 否决（G2 成本放大的事件
# 模式类比：×1.5 成本下净收益均值仍 > 0）。联合口径（主检验+成本否决）
# 的误报/检出率都报——关卡 v3 事件模式主检验的选择规则留 T65。
EVENT_DEFAULT_SYMBOLS = ("USTEC D1",)     # fred ext 39.8 年：现成的长序列
EVENT_DELTA_LEVELS = (0.1, 0.25, 0.5, 1.0)   # δ 档位 = 每事件净效应 ÷ σ_w
EVENT_POWER_N_GRID = (60, 120, 240, 480)     # 检验力曲线的事件数轴
EVENT_ALPHA = 0.05


class _ScaledCosts:
    """成本模型 ×factor 包装（成本 ×1.5 否决臂；事件模式走
    spread_p90/slippage/commission_per_lot/swap.rule 四个入口）。"""

    def __init__(self, base, factor: float):
        self._base, self._f = base, float(factor)

    def spread_p90(self, symbol, hour_ny, *, price=None):
        return self._base.spread_p90(symbol, hour_ny, price=price) * self._f

    def slippage(self, symbol, hour_ny, *, price=None):
        return self._base.slippage(symbol, hour_ny, price=price) * self._f

    def commission_per_lot(self, symbol):
        return self._base.commission_per_lot(symbol) * self._f

    class _Swap:
        def __init__(self, base_swap, factor):
            self._base_swap, self._f = base_swap, factor

        def rule(self, symbol):
            r = self._base_swap.rule(symbol)
            return dataclasses.replace(
                r, long_per_day=r.long_per_day * self._f,
                short_per_day=r.short_per_day * self._f)

    @property
    def swap(self):
        base_swap = getattr(self._base, "swap", None)
        return None if base_swap is None else self._Swap(base_swap, self._f)


def load_event_bars(data_root, symbol: str, timeframe: str,
                    *, ext_source: str | None = "fred",
                    max_bars=None) -> tuple[pd.DataFrame, str]:
    """事件臂 K 线：已批准外部序列（fred USTEC D1 39.8 年等）优先，缺则
    研究库 bars（load_series 同源锁箱语义）。ext 读经 mt5data.ext 装载器
    （manifest 完整性 fail-fast）并按 manifest.research_cutoff 防御性再截
    （导出侧已 assert，读取侧不重复信任）。"""
    from mt5data.ext.core import load_ext_series

    if ext_source:
        loaded = load_ext_series(str(Path(data_root) / "ext"), ext_source,
                                 symbol, timeframe.upper())
        if loaded is not None:
            df, manifest = loaded
            cutoff = manifest.get("research_cutoff")
            if cutoff:
                df = df[df["time"] < pd.Timestamp(cutoff)] \
                    .reset_index(drop=True)
            if max_bars and len(df) > int(max_bars):
                df = df.iloc[-int(max_bars):].reset_index(drop=True)
            return df, f"ext/{ext_source}"
    from run_backtest import load_series
    bars = load_series(str(data_root), symbol, timeframe.upper(), None)
    if max_bars and len(bars) > int(max_bars):
        bars = bars.iloc[-int(max_bars):].reset_index(drop=True)
    return bars, "research"


def event_mode_calibration(bars, *, symbol: str, timeframe: str, costs,
                           contract_size: float, kind: str = "month_end",
                           side_fn=None, last_n: int = 1, first_n: int = 3,
                           neg_seeds: int = 20, levels=EVENT_DELTA_LEVELS,
                           pos_seeds: int = 20, test_sims: int = 1000,
                           neg_rand_sims: int = 199, pos_rand_sims: int = 99,
                           alpha: float = EVENT_ALPHA,
                           power_n_grid=EVENT_POWER_N_GRID,
                           log=None) -> dict:
    """事件模式正负对照（T66/D35 标定素材）。

    - 负对照（误报率）：真实数据不植入 × randomize_event_times × 20 种子
      → 三个统计量各自的拒绝率（年块自助 / 随机化参照 / ×1.5 成本否决
      下的联合）；
    - 正对照（检出率）：plant_event_effect 植入 δ 档位（σ_w 倍数）× 20
      种子 → 同三个统计量的检出率 + 植入精度（净收益均值实移 vs δ）；
    - 检验力曲线：事件数 × δ → 经验检出率（前 n 个等距事件子样本）+
      解析 event_power 对照；
    - 样本量规则：required_events 按 σ_w 与各 δ_net 档位折算。
    """
    from mt5quant.validation import events as vevents

    if side_fn is None:
        side_fn = lambda event: "long"  # noqa: E731
    times = pd.to_datetime(bars["time"])
    days = pd.DatetimeIndex(sorted(set(times.dt.normalize())))
    if kind == "month_end":
        events = vevents.month_end_events(days, last_n=last_n, first_n=first_n)
        hold_bars = None
    elif kind == "london_fix":
        anchors = vevents.month_end_events(days, last_n=1, first_n=1)
        month_end_days = pd.DatetimeIndex([e.entry_time for e in anchors])
        events = vevents.london_fix_events(month_end_days)
        hold_bars = 1
    else:
        raise ValueError(f"未知事件类别 {kind!r}（month_end | london_fix）")
    if len(events) < 24:
        raise ValueError(f"{symbol} {timeframe} 事件数 {len(events)} < 24"
                         "——事件模式检验无从标定")
    hold_bars = hold_bars if hold_bars is not None else max(
        1, int(np.median([
            days.get_loc(e.exit_time.normalize())
            - days.get_loc(e.entry_time.normalize()) for e in events])))
    sigma_w = vevents.window_volatility(bars, hold_bars)

    def _returns(data, evs, cost_model):
        return vevents.event_returns(data, evs, side_fn=side_fn,
                                     costs=cost_model, symbol=symbol,
                                     contract_size=contract_size)

    costs_x15 = _ScaledCosts(costs, 1.5)
    raw = _returns(bars, events, costs)
    cost_mean = float(pd.to_numeric(raw["cost"]).mean())
    seed_list = list(range(neg_seeds))

    # 真实事件时间上的观测（诊断，不是对照臂：真实数据可能真有效应——
    # 随机化检验拒绝=检出真实月末效应，如实单列供 C001 材料）
    obs_boot = vevents.event_test(raw, sims=test_sims, seed=0)
    obs_rand = vevents.randomization_test(
        bars, events, side_fn=side_fn, costs=costs, symbol=symbol,
        trading_days=days, sims=neg_rand_sims, seed=90_000,
        contract_size=contract_size)
    observed = {
        "net_mean": float(pd.to_numeric(raw["net"]).mean()),
        "gross_mean": float(pd.to_numeric(raw["gross"]).mean()),
        "boot_p": obs_boot["p_value"], "rand_p": obs_rand["p_value"],
        "note": ("真实事件时间的观测诊断（非对照臂）：随机化检验拒绝"
                 "意味着真实数据在事件窗存在系统性收益——真实效应的检出，"
                 "不是误报；候选论证（C001）用发表后证据，不取本观测当预期"),
    }

    # ---- 负对照：零假设 = 事件时间随机化（matched 不重叠抽样池）。
    # 观测事件本身也在随机化时刻上——真实数据里存在真实效应时，在真实
    # 事件时间上观测会**正确拒绝**（那不是误报）；误报率的定义是"零假设
    # 下的拒绝率"，观测与零分布都取随机化时刻才是可交换的。
    negative_runs = []
    for seed in seed_list:
        t0 = time.monotonic()
        rand = vevents.randomize_event_times(events, days, seed=seed)
        rets = _returns(bars, rand, costs)
        boot = vevents.event_test(rets, sims=test_sims, seed=seed)
        rets15 = _returns(bars, rand, costs_x15)
        mean15 = float(pd.to_numeric(rets15["net"]).mean())
        rat = vevents.randomization_test(
            bars, rand, side_fn=side_fn, costs=costs, symbol=symbol,
            trading_days=days, sims=neg_rand_sims, seed=10_000 + seed,
            contract_size=contract_size)
        negative_runs.append({
            "seed": seed,
            "boot_p": boot["p_value"], "boot_mean": boot["mean"],
            "rand_p": rat["p_value"], "rand_null_mean": rat["null_mean"],
            "x15_mean": mean15,
            "reject_boot": bool(boot["p_value"] < alpha),
            "reject_rand": bool(rat["p_value"] < alpha),
            "reject_joint": bool(boot["p_value"] < alpha and mean15 > 0.0),
            "reject_joint_rand": bool(rat["p_value"] < alpha and mean15 > 0.0),
        })
        if log:
            log(f"[event/neg] {symbol} {timeframe} seed={seed} "
                f"boot_p={boot['p_value']:.4f} rand_p={rat['p_value']:.4f} "
                f"x15_mean={mean15:.5f} ({time.monotonic() - t0:.1f}s)")

    # ---- 正对照：植入 δ 档位（σ_w 倍数）× 种子。植入在**随机化基线事件**
    # 上（matched 零基线）：真实数据在真实月末窗存在系统性收益（观测诊断
    # 单列）——在真实事件上植入会把检出率变成"真实效应+δ"的检出，归因
    # 不清；基线随机化后窗上只剩 δ，检出率才对应档位本身。
    positive_runs = []
    for level in levels:
        delta = float(level) * sigma_w
        for seed in range(pos_seeds):
            t0 = time.monotonic()
            base = vevents.randomize_event_times(events, days,
                                                 seed=30_000 + seed)
            planted = vevents.plant_event_effect(bars, base, delta,
                                                 side_fn=side_fn)
            rets = _returns(planted, base, costs)
            boot = vevents.event_test(rets, sims=test_sims, seed=seed)
            rets15 = _returns(planted, base, costs_x15)
            mean15 = float(pd.to_numeric(rets15["net"]).mean())
            rat = vevents.randomization_test(
                planted, base, side_fn=side_fn, costs=costs, symbol=symbol,
                trading_days=days, sims=pos_rand_sims, seed=20_000 + seed,
                contract_size=contract_size)
            positive_runs.append({
                "level": float(level), "delta": delta, "seed": seed,
                "boot_p": boot["p_value"], "boot_mean": boot["mean"],
                "rand_p": rat["p_value"],
                "x15_mean": mean15,
                "reject_boot": bool(boot["p_value"] < alpha),
                "reject_rand": bool(rat["p_value"] < alpha),
                "reject_joint": bool(boot["p_value"] < alpha and mean15 > 0.0),
                "reject_joint_rand": bool(rat["p_value"] < alpha
                                          and mean15 > 0.0),
            })
            if log:
                log(f"[event/pos] {symbol} {timeframe} level={level} "
                    f"seed={seed} boot_p={boot['p_value']:.4f} "
                    f"rand_p={rat['p_value']:.4f} "
                    f"({time.monotonic() - t0:.1f}s)")

    # ---- 检验力曲线：事件数 × δ → 经验检出率 + 解析对照。基线同样取
    # 随机化事件（零基线，检出归因于 δ；真实月末窗的系统性收益在观测
    # 诊断里单列，不混进曲线）
    power_runs = []
    n_total = len(events)
    n_grid = [n for n in power_n_grid if n < n_total] + [n_total]
    for n in sorted(set(n_grid)):
        stride = max(1, n_total // int(n))
        subset = events[::stride][:int(n)]
        for level in levels:
            delta = float(level) * sigma_w
            hits = 0
            for seed in range(pos_seeds):
                base = vevents.randomize_event_times(subset, days,
                                                     seed=40_000 + seed)
                planted = vevents.plant_event_effect(bars, base, delta,
                                                     side_fn=side_fn)
                rets = _returns(planted, base, costs)
                hits += int(vevents.event_test(
                    rets, sims=test_sims, seed=seed)["p_value"] < alpha)
            # pos_seeds<1（CLI 正常用法不可达）跳过检验力行防除零（T66 审核修正）
            empirical = (hits / float(pos_seeds)) if pos_seeds >= 1 else None
            net_delta = delta - cost_mean
            analytic = (vevents.event_power(net_delta, sigma_w, len(subset),
                                            alpha=alpha)
                        if net_delta > 0.0 else 0.0)
            power_runs.append({
                "n_events": len(subset), "level": float(level),
                "delta": delta, "net_delta": net_delta,
                "empirical_power": empirical, "analytic_power": float(analytic),
                "seeds": int(pos_seeds),
            })
            if log:
                log(f"[event/power] n={len(subset)} level={level} "
                    f"empirical={empirical:.2f} analytic={analytic:.3f}")

    # ---- 样本量规则（折算后净效应下检验力 ≥50% 所需事件数）
    def _required(net: float, power: float):
        return (vevents.required_events(net, sigma_w, alpha=alpha,
                                        power=power) if net > 0.0 else None)

    sample_size = {
        "sigma_window": float(sigma_w), "cost_mean": cost_mean,
        "hold_bars": int(hold_bars), "n_events_available": n_total,
        "rows": [{
            "level": float(level),
            "delta_gross": float(level) * sigma_w,
            "delta_net": float(level) * sigma_w - cost_mean,
            "required_events_power50": _required(
                float(level) * sigma_w - cost_mean, 0.5),
            "required_events_power80": _required(
                float(level) * sigma_w - cost_mean, 0.8),
        } for level in levels],
        "method": ("闭式 n=(z_α+z_p)²·(1+SR²/2)/SR²（Lo 2002 事件口径，"
                   "SR=每事件净效应/σ_w；z 取 mt5quant.research.power 同一"
                   "约定）；经验曲线见 power_runs（事件数×δ→检出率）"),
    }

    def _rate(rows, key):
        return float(np.mean([bool(r[key]) for r in rows])) if rows else 0.0

    detection = {}
    for level in levels:
        bucket = [r for r in positive_runs if r["level"] == float(level)]
        detection[str(level)] = {key: _rate(bucket, key) for key in
                                 ("reject_boot", "reject_rand",
                                  "reject_joint", "reject_joint_rand")}
    return {
        "symbol": symbol, "timeframe": timeframe, "kind": kind,
        "n_events": n_total, "first": str(days[0].date()),
        "last": str(days[-1].date()),
        "sigma_window": float(sigma_w), "cost_mean": cost_mean,
        "hold_bars": int(hold_bars),
        "levels": [float(x) for x in levels],
        "alpha": float(alpha), "test_sims": int(test_sims),
        "neg_rand_sims": int(neg_rand_sims), "pos_rand_sims": int(pos_rand_sims),
        "negative_runs": negative_runs,
        "false_positive_rates": {key: _rate(negative_runs, key) for key in
                                 ("reject_boot", "reject_rand",
                                  "reject_joint", "reject_joint_rand")},
        "observed_diagnostic": observed,
        "positive_runs": positive_runs, "detection_rates": detection,
        "power_runs": power_runs, "sample_size_rule": sample_size,
        "cost_model_note": ("点差来回全幅一次（入/出场小时较不利侧）/"
                            "滑点双边/佣金每手来回/隔夜更不利侧×实际计费夜"
                            "——与 T62 预筛同一份口径（mt5quant.validation."
                            "events docstring）"),
    }


def render_event_report(results: dict) -> str:
    """事件模式对照（T66/D35）的 Markdown 汇总——D35 标定报告的素材。"""
    meta = results.get("meta", {})
    fmt_pct = lambda v: f"{float(v) * 100:.0f}%"  # noqa: E731
    setups = results.get("setups", [])
    setup_desc = "; ".join(
        f"{s['symbol']} {s['timeframe']}（{s.get('bars_source', '?')}，"
        f"{s['first']}..{s['last']}，{s['n_events']} 事件）" for s in setups)
    lines = ["# 事件模式对照实验（event-power，T66/D35）", "",
             f"- 序列：{setup_desc or '?'}",
             f"- 种子：负对照 {meta.get('seeds', '?')}、正对照 "
             f"{meta.get('seeds', '?')}；显著性 α={meta.get('alpha', '?')}"
             f"（单侧）；自助 {meta.get('test_sims', '?')} 次",
             f"- 生成：{meta.get('generated_utc', '?')}（gate_calibration.py "
             "--phase event）", ""]
    for block in results.get("arms", []):
        key = f"{block['symbol']} {block['timeframe']} {block['kind']}"
        obs = block.get("observed_diagnostic") or {}
        lines += [f"## {key}", "",
                  f"σ_w={block['sigma_window']:.5f}（{block['hold_bars']} 根/"
                  f"事件窗）、均值成本={block['cost_mean']:.5f}"
                  f"（相对价格；与 T62 预筛同口径）", ""]
        if obs:
            lines += [f"真实事件时间观测（诊断，非对照臂）：净收益均值 "
                      f"{obs.get('net_mean', float('nan')):.5f}（毛 "
                      f"{obs.get('gross_mean', float('nan')):.5f}）、"
                      f"自助 p={obs.get('boot_p', float('nan')):.4f}、"
                      f"随机化 p={obs.get('rand_p', float('nan')):.4f}——"
                      "随机化检验拒绝=真实数据在事件窗存在系统性收益（真实"
                      "效应检出，不是误报）。", ""]
        lines += ["### 负对照误报率（零假设=事件时间随机化，观测也取随机"
                  "化时刻，20 种子）", "",
                  "| 统计量 | 误报率 |", "| --- | --- |"]
        for name, k in (("年块自助均值>0（event_test）", "reject_boot"),
                        ("随机化参照（randomization_test）", "reject_rand"),
                        ("联合：自助 + 成本×1.5 净均值>0", "reject_joint"),
                        ("联合：随机化 + 成本×1.5 净均值>0",
                         "reject_joint_rand")):
            lines.append(f"| {name} | {fmt_pct(block['false_positive_rates'][k])} |")
        lines += ["", "### 正对照检出率（植入 δ = level×σ_w 于随机化基线事件，"
                      "20 种子——真实月末窗的系统性收益不混入，检出归因 δ）",
                  "",
                  "| δ/σ_w | 自助 | 随机化 | 联合(自助+×1.5) | 联合(随机化+×1.5) |",
                  "| --- | --- | --- | --- | --- |"]
        for level in block["levels"]:
            d = block["detection_rates"][str(level)]
            lines.append(
                f"| {level:g} | {fmt_pct(d['reject_boot'])} | "
                f"{fmt_pct(d['reject_rand'])} | {fmt_pct(d['reject_joint'])} | "
                f"{fmt_pct(d['reject_joint_rand'])} |")
        lines += ["", "### 检验力曲线（事件数 × δ → 检出率；随机化基线事件，"
                      "经验 20 种子 vs 解析）", "",
                  "| 事件数 | δ/σ_w | 经验检验力 | 解析（Lo 事件口径） |",
                  "| --- | --- | --- | --- |"]
        for row in block["power_runs"]:
            lines.append(
                f"| {row['n_events']} | {row['level']:g} | "
                f"{fmt_pct(row['empirical_power'])} | "
                f"{fmt_pct(row['analytic_power'])} |")
        ss = block["sample_size_rule"]
        lines += ["", f"### 样本量规则（检验力 ≥50%/≥80% 所需事件数；"
                      f"σ_w={ss['sigma_window']:.5f}、均值成本="
                      f"{ss['cost_mean']:.5f}）", "",
                  "| δ/σ_w | 毛效应 | 净效应（−成本） | n(≥50%) | n(≥80%) |",
                  "| --- | --- | --- | --- | --- |"]
        for row in ss["rows"]:
            n50 = row["required_events_power50"]
            n80 = row["required_events_power80"]
            lines.append(
                f"| {row['level']:g} | {row['delta_gross']:.5f} | "
                f"{row['delta_net']:.5f} | {n50 if n50 else '—'} | "
                f"{n80 if n80 else '—'} |")
        lines += [f"\n可用事件数 {ss['n_events_available']}（{ss['method']}）", ""]
    lines += ["## 口径备注", "",
              "- 事件净收益的成本口径与 T62 预筛同一份语义（见 "
              "mt5quant.validation.events docstring）；×1.5 否决是 G2 成本"
              "放大的事件模式类比（×1.5 成本下净收益均值仍 > 0）。",
              "- 负对照零假设 = 事件时间随机化（matched 不重叠抽样池，保持"
              "星期分布与月内三分位），**观测也取随机化时刻**——真实数据"
              "在真实月末窗存在系统性收益（见观测诊断），在真实事件上观测"
              "的拒绝是正确检出而非误报；随机化参照检验对资产漂移稳健。",
              "- 正对照/检验力曲线的 δ 植入在**随机化基线事件**上（净效应 = "
              "δ − 均值成本）——真实月末效应不混入，检出率归因档位本身。"
              "年块自助百分位检验的经验水平随年块数收敛（合成 iid 实测 "
              "6 块约 9.5%、40 块约 5.05%，有效临界分布 ≈ t_{B−1}），长历史"
              "才接近名义。与关卡 v3 主检验的对接（事件模式主统计量选择、"
              "联合否决结构）留 T65。", ""]
    return "\n".join(lines)


# ------------------------------------------------- 关卡 v3 对照（T65/D34）
# 蓝图 v4 附录 B"关卡 v3 提案框架"的对照标定臂（--phase v3）：数据卫生否决
# （G0）+ 主检验（否决；甲=DSR、乙=年块自助，两方案都算）+ 稳健性否决
# （成本 ×1.5 / 延迟一根 / 多头相对买入持有超额）+ 诊断（只报告，不否决）。
# 设计：正对照 = LONG 植入（事件后价格压低 d、信号 +1——多头控制让
# buyhold 否决真实参与判定）按"扣成本后年化夏普 ≈ 档位"校准 × 每档多种子；
# 负对照 = 两种零假设（day/slot）打乱同一研究段子窗 × 多种子。报告**联合**
# 通过率（主检验+全部否决项一起，由逐条记录重算），不再只报单项。
# 事件模式分支不在此重标（T66 已标）：gates-v3 的主检验乙即事件模式的
# event_test 同一实现，结论引用 event-mode-calibration-2026-10-28。

V3_DEFAULT_SETUPS = ("XAUUSD:H1", "XAUUSD:H4", "XAUUSD+USTEC:H1")
V3_DEFAULT_LEVELS = (0.5, 1.0, 1.5)       # 校准后年化夏普档位（扣成本）
V3_BONFERRONI_SENSITIVITY_N = 40          # 方案乙 Bonferroni 敏感性档
# （R1 草案的轮次预算量级；正式取数按台账 project_n_dedup）
V3_N_GRID = (2, 6, 24)                    # T85 工作点：重算的 N 档（预写）
V3_PRIMARY_CHECKS = {"dsr": "primary.dsr",
                     "year_block": "primary.year_block",
                     f"year_block@N{V3_BONFERRONI_SENSITIVITY_N}":
                         f"primary.year_block@N{V3_BONFERRONI_SENSITIVITY_N}",
                     # T85 甲（DSR(N, V₀)）/ 丙（PSR(0) ≥ 1−α/N）逐 N 记录
                     **{f"dsr_v0@N{n}": f"primary.dsr_v0@N{n}"
                        for n in V3_N_GRID},
                     **{f"psr0_bonf@N{n}": f"primary.psr0_bonf@N{n}"
                        for n in V3_N_GRID}}
V3_VETO_CHECKS = ("G0", "veto.cost_x15", "veto.delay_1_bar",
                  "veto.buyhold_excess")

# v3 植入节奏按周期分档：日事件（每 24h）在 H1/H4 上每 24/6 根一个事件、
# 事件间有空档（植入可被持有期捕获）；D1 上 24h 事件=每根 bar 都是事件、
# 信号连续覆盖全史——植入的压价被入场/出场同幅抵消（事件策略退化为
# 买入持有），故 D1 用周节奏（每 7 根日 bar 一个事件，hold 4 → 3 根空档）。
V3_PLANTED_RHYTHM = {"D1": (7, 24)}


def v3_rhythm(timeframe: str) -> tuple[int, int]:
    """v3 植入事件节奏：(period_days, interval_hours)，缺省 (1, 24)。"""
    return V3_PLANTED_RHYTHM.get(str(timeframe).upper(), (1, 24))



class LongPlantedEdgeStrategy(PlantedEdgeStrategy):
    """多头版植入策略（事件后压价、信号 +1）：让 buyhold 否决（纯多头
    策略须跑赢买入持有）在 v3 对照里真实参与判定。与 plant_edge(drift<0)
    配套：入场次根开盘价已被压低、信号归零后次根回补到未压价水平。"""

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        from mt5quant.validation.synthetic import _expand_events
        events = event_bars(
            bars, period_days=self._period_days,
            interval_hours=self._interval_hours,
            phase_hours=self._phase_hours, day_phase=self._day_phase)
        hold = int(params["hold_bars"])
        return pd.Series(_expand_events(events, hold, 1),
                         index=bars.index, dtype="int64")


def walk_forward_oos(spec, bars, costs, grid, cfg) -> tuple[pd.Series, int]:
    """walk-forward 拼接样本外收益（**带测试窗时间索引**——方案乙按自然年
    分块）。窗口计划 / 训练窗网格内重选 / 测试窗尾部收益拼接 = vwf.run_g3
    同一语义的代码镜像（run_g3 只回聚合 oos_sharpe，不带时间戳）。"""
    strategy = spec.strategy
    lookback = max(int(getattr(strategy.required_data, "lookback_bars", 0)
                       or 0), 0)
    train_m, test_m = vwf.month_window_spec(spec.timeframe, cfg)
    windows = vwf.plan_windows(bars["time"], train_months=train_m,
                               test_months=test_m)
    times = pd.to_datetime(bars["time"]).reset_index(drop=True)
    combos = [dict(zip(grid, values))
              for values in itertools.product(*(grid.values() or [()]))] or [{}]
    rets_parts, time_parts, n_seg = [], [], 0
    for w in windows:
        t0 = int(times.searchsorted(w["train_start"], side="left"))
        t1 = int(times.searchsorted(w["test_start"], side="left"))
        t2 = int(times.searchsorted(w["test_end"], side="left"))
        if t1 <= t0 or t2 <= t1:
            continue
        train = bars.iloc[max(0, t0 - lookback): t1]
        best_params, best_net = None, -math.inf
        for combo in combos:
            trades = vrob.run_engine(spec, train, costs, combo).trades
            net = float(trades["net"].sum()) if len(trades) else 0.0
            if net > best_net:
                best_net, best_params = net, dict(combo)
        test = bars.iloc[max(0, t1 - lookback): t2]
        oos = vrob.run_engine(spec, test, costs, best_params)
        tail = oos.equity.astype("float64").iloc[-(t2 - t1 + 1):]
        rets = tail.pct_change().dropna().reset_index(drop=True)
        seg_times = times.iloc[t1:t2].iloc[-len(rets):].reset_index(drop=True)
        rets_parts.append(rets)
        time_parts.append(seg_times)
        n_seg += 1
    if not rets_parts:
        return pd.Series(dtype="float64"), 0
    oos_returns = pd.Series(
        pd.concat(rets_parts, ignore_index=True).to_numpy(),
        index=pd.DatetimeIndex(pd.concat(time_parts, ignore_index=True)
                               .to_numpy()))
    return oos_returns, n_seg


def v3_primary_suite(oos_returns, cfg, seed: int,
                      *, n_trials: int = 1,
                      n_grid=V3_N_GRID) -> dict:
    """v3 主检验组（现行键语义不变 + T85 工作点键）：现行三键
    （primary.dsr / primary.year_block / @N40 敏感性，n_trials=1——实验
    不经台账选择）逐位照旧；新增 N ∈ n_grid 的方案甲（DSR(N, V₀=1/(n−1))
    ——v3_primary_dsr_null_variance）与方案丙（PSR(0) ≥ 1−α/N——
    v3_primary_psr_bonferroni）逐档记录（T85 预写：同一 OOS 收益重算
    联合误报/检出）。"""
    primary = {
        "dsr": vgate.v3_primary_dsr(oos_returns, n_trials=n_trials,
                                    trial_sharpes=None, cfg=cfg),
        "year_block": vgate.v3_primary_year_block(
            oos_returns, n_trials=n_trials, cfg=cfg, seed=seed),
        f"year_block@N{V3_BONFERRONI_SENSITIVITY_N}":
            vgate.v3_primary_year_block(
                oos_returns, n_trials=V3_BONFERRONI_SENSITIVITY_N,
                cfg=cfg, seed=seed),
    }
    for n in n_grid:
        primary[f"dsr_v0@N{n}"] = vgate.v3_primary_dsr_null_variance(
            oos_returns, n_trials=int(n), cfg=cfg)
        primary[f"psr0_bonf@N{n}"] = vgate.v3_primary_psr_bonferroni(
            oos_returns, n_trials=int(n), cfg=cfg)
    return primary


def run_v3_gate_suite(strategy, params, *, timeframe, bars, costs,
                      contract_size, vols, cfg, seed, grid=None,
                      symbol: str = "TST", session_policy=None,
                      initial_capital=100_000.0, risk_fraction=0.005,
                      n_trials: int = 1, log=None) -> dict:
    """关卡 v3 套件（suite="v3" 实验镜像）：G0 + 主检验（甲/乙 + Bonferroni
    敏感性都算）+ 三否决 + 诊断 → 逐检查布尔 + vgate.evaluate_v3 判定。

    n_trials：实验不经 screen/台账的网格选择 → 1（DSR 退化为 PSR(0)、
    Bonferroni 阈值 = alpha，与现行 gate_calibration 口径一致；正式路径
    由 bt 接线按台账 project dedup 取数）。诊断集 = G1 样本内线 + 参数
    邻域 + 按年分段 + 去最好 5%（全部只报告）；随机入场蒙特卡洛 / 跨品种 /
    回撤分位不在实验臂复算（不进判定，计算量控制——批准后正式路径照跑）。
    """
    sizing = RiskSizing(risk_fraction=float(risk_fraction))
    spec = vrob.BacktestSpec(
        strategy=strategy, symbol=symbol, timeframe=timeframe,
        contract_size=contract_size, volume_lots=None,
        initial_capital=initial_capital, sizing=sizing,
        vol_min=vols[0], vol_step=vols[1], vol_max=vols[2])
    base = run_backtest(
        strategy, bars, costs, symbol=symbol, params=dict(params),
        contract_size=contract_size, volume_lots=None, sizing=sizing,
        vol_min=vols[0], vol_step=vols[1], vol_max=vols[2],
        initial_capital=initial_capital, timeframe=timeframe,
        session_policy=session_policy)
    summary = summarize(base)
    base_sharpe = summary.get("sharpe")
    oos_returns, n_seg = walk_forward_oos(spec, bars, costs,
                                          dict(grid or {}), cfg)
    primary = v3_primary_suite(oos_returns, cfg, seed, n_trials=n_trials)
    veto_cost = vgate.v3_veto_cost_scale(spec, bars, costs, params, cfg)
    veto_delay = vgate.v3_veto_delay(spec, bars, costs, params, cfg)
    veto_buyhold = vgate.v3_veto_buyhold(spec, bars, costs, base.trades,
                                         base_sharpe, cfg)
    g1 = vgate.run_g1(base.trades, timeframe=timeframe, cfg=cfg)
    diag = {
        "param_plateau": vrob.param_plateau(spec, bars, costs, params,
                                            dict(grid or {}), cfg),
        "yearly_segments": vrob.yearly_segments(base.trades, cfg),
        "drop_best_5pct": vrob.drop_best_check(base.trades, cfg),
        **{f"g1.{k}": c for k, c in g1["checks"].items()},
    }
    g0 = vlook.run_g0(strategy, bars, params,
                      # 静态扫描只扫策略类体：LongPlantedEdgeStrategy 定义在
                      # 本脚本内，缺省会扫整个标定脚本（aggregate_rates 的
                      # np.mean 全是"全样本统计"误报——扫类体才是 G0 语义）
                      source=inspect.getsource(type(strategy)))
    verdict = vgate.evaluate_v3(
        g0=g0, primary=primary["dsr"], cost_scale=veto_cost,
        delay=veto_delay, buyhold=veto_buyhold,
        diagnostics=diag)
    checks = {
        "G0": bool(g0["passed"]),
        **{f"primary.{scheme}": bool(c["passed"])
           for scheme, c in primary.items()},
        "veto.cost_x15": bool(veto_cost["passed"]),
        "veto.delay_1_bar": bool(veto_delay["passed"]),
        "veto.buyhold_excess": bool(veto_buyhold["passed"]),
        **{f"diag.{name}": bool(c.get("passed", False))
           for name, c in diag.items()},
    }
    return {
        "checks": checks,
        "verdict": verdict,
        "primary": primary,
        "vetoes": {"cost_scale_x15": veto_cost, "delay_1_bar": veto_delay,
                   "buyhold_excess": veto_buyhold},
        "diagnostics": diag,
        "g0": g0,
        "oos_returns": oos_returns,
        "wf_segments": int(n_seg),
        "summary": {"net_profit": summary["net_profit"],
                    "sharpe": summary.get("sharpe"),
                    "trade_count": summary.get("trade_count")},
    }


def v3_joint_pass(checks: dict, scheme: str) -> bool:
    """联合通过（主检验 scheme + 全部否决项）——**由逐条记录的检查布尔
    重算**（分片/合并器共用；记录里的任何自报 joint 字段不参与）。"""
    key = V3_PRIMARY_CHECKS[scheme]
    if key not in checks:
        raise KeyError(f"v3 记录缺 {key}（两方案都必须逐条记录）")
    return bool(checks[key]) and all(bool(checks.get(k)) for k in
                                     V3_VETO_CHECKS)


def v3_joint_rates(runs: list[dict]) -> dict[str, float]:
    """联合通过率 + 分检查通过率（对 runs 的 checks 逐条重算；合并器与
    单进程共用同一函数——联合口径只有这一处定义）。"""
    rates = aggregate_rates(runs)
    for scheme in V3_PRIMARY_CHECKS:
        rates[f"joint.{scheme}"] = (
            float(np.mean([v3_joint_pass(r["checks"], scheme)
                           for r in runs])) if runs else 0.0)
    return rates


# ------------------------------------------------ T85：工作点补充标定（K1）
# 预写规则（写于结果之前，第十三轮审核 §4.1/计划 T85，不得按结果调整）：
# 1. N 口径 = project_n_dedup_rounds + lineage_n_dedup（ledger.primary_test_n）；
# 2. 甲 = DSR(N, V₀) ≥ 0.95（V₀ = 1/(n−1)，n = 样本外 bar 数）；
#    丙 = PSR(0) ≥ 1 − α/N；同一样本外收益；
# 3. 重算 N ∈ {2, 6, 24} 的联合误报率与检出率；
# 4. 选择：在三个 N、全部设置、两种零假设下联合误报率都 ≤ 5% 的方案里，
#    选 N=6 时夏普 1.0 联合检出率较高的（逐设置：不低于且至少一处严格
#    更高者胜；互有高低取最差设置较高者）；处处相同选丙（实现更简单）；
# 5. 按选中方案在 N=6 的联合检出率重判"测不了"（< 50%），N=2 与 N=24
#    两列并列报告。
V3_T85_SCHEMES = ("dsr_v0", "psr0_bonf")
V3_T85_SCHEME_LABELS = {"dsr_v0": "甲 DSR(N,V₀)",
                        "psr0_bonf": "丙 PSR(0)≥1−α/N"}
V3_T85_FP_MAX = 0.05          # 联合误报合格线（预写）
V3_T85_LOW_DETECTION = 0.50   # "测不了"线（预写）
V3_T85_DECISION_LEVEL = 1.0   # 选择规则盯住的档位（预写）
V3_T85_DECISION_N = 6         # 选择规则盯住的 N 档（预写）


def v3_t85_operating_point(results: dict) -> dict:
    """T85 工作点补充标定的汇总与判定（规则文本见上方注释——先于结果
    预写）。输入 = --phase v3 的（合并）产物 dict；联合率全部由逐条记录
    重算（v3_joint_pass），与主标定同一口径。"""
    neg = (results.get("negative") or {}).get("runs") or []
    pos = (results.get("positive") or {}).get("runs") or []
    if not neg or not pos:
        raise ValueError("产物缺 negative/positive runs——T85 判定无从做起")
    setups = sorted({r["setup"] for r in neg})
    nulls = sorted({r.get("null") for r in neg if r.get("null")})
    levels = sorted({float(r["level"]) for r in pos})

    fp: dict[str, dict[str, float]] = {}
    det: dict[str, dict[str, float]] = {}
    for scheme in V3_T85_SCHEMES:
        for n in V3_N_GRID:
            key = f"{scheme}@N{n}"
            fp[key] = {}
            for setup in setups:
                for null in nulls:
                    bucket = [r for r in neg
                              if r["setup"] == setup and r.get("null") == null]
                    fp[key][f"{setup}|{null}"] = float(np.mean(
                        [v3_joint_pass(r["checks"], key) for r in bucket]))
            det[key] = {}
            for setup in setups:
                for level in levels:
                    bucket = [r for r in pos
                              if r["setup"] == setup
                              and float(r["level"]) == level]
                    det[key][f"{setup}|{level:.1f}"] = float(np.mean(
                        [v3_joint_pass(r["checks"], key) for r in bucket]))

    # ---- 预写规则 4a：合格 = 三个 N、全部设置、两种零假设联合误报都 ≤5%
    eligibility: dict[str, bool] = {}
    violations: dict[str, list[str]] = {}
    for scheme in V3_T85_SCHEMES:
        bad = [f"{scheme}@N{n}：{cell}={rate:.0%}"
               for n in V3_N_GRID
               for cell, rate in fp[f"{scheme}@N{n}"].items()
               if rate > V3_T85_FP_MAX]
        eligibility[scheme] = not bad
        violations[scheme] = bad

    # ---- 预写规则 4b：合格方案里选 N=6 夏普 1.0 联合检出率较高者
    level_key = f"{V3_T85_DECISION_LEVEL:.1f}"
    at_n6 = {scheme: {setup: det[f"{scheme}@N{V3_T85_DECISION_N}"]
                      .get(f"{setup}|{level_key}", 0.0)
                      for setup in setups}
             for scheme in V3_T85_SCHEMES}
    eligible = [s for s in V3_T85_SCHEMES if eligibility[s]]
    if not eligible:
        raise ValueError(
            "甲丙两方案都不满足'三个 N 全设置双零假设联合误报 ≤5%'——按预写"
            "规则须收紧显著性重新标定（不得就地放宽规则）")
    if len(eligible) == 1:
        selected, reason = eligible[0], "唯一合格方案"
    else:
        a, b = eligible[0], eligible[1]
        da, db = at_n6[a], at_n6[b]
        a_ge = all(da[s] >= db[s] for s in setups)
        a_hi = any(da[s] > db[s] for s in setups)
        b_ge = all(db[s] >= da[s] for s in setups)
        b_hi = any(db[s] > da[s] for s in setups)
        if a_ge and a_hi:
            selected, reason = a, "N=6 夏普1.0 联合检出逐设置不低于且至少一处严格更高"
        elif b_ge and b_hi:
            selected, reason = b, "N=6 夏普1.0 联合检出逐设置不低于且至少一处严格更高"
        elif min(da.values()) != min(db.values()):
            winner = a if min(da.values()) > min(db.values()) else b
            selected = winner
            reason = "互有高低 → 取最差设置联合检出较高者（运行时补充平局规则——T85 审核更名：非预写文本）"
        else:
            selected, reason = "psr0_bonf", "处处相同 → 选丙（实现更简单）"

    # ---- 预写规则 5：按选中方案 N=6 重判"测不了"（<50%），N=2/N=24 并列
    unmeasurable = []
    for setup in setups:
        for level in levels:
            cell = f"{setup}|{level:.1f}"
            rates_by_n = {f"N{n}": det[f"{selected}@N{n}"].get(cell, 0.0)
                          for n in V3_N_GRID}
            if rates_by_n[f"N{V3_T85_DECISION_N}"] < V3_T85_LOW_DETECTION:
                unmeasurable.append({
                    "category": f"{setup} 夏普{level:.1f}", "setup": setup,
                    "level": level, "rates": rates_by_n,
                    "verdict": "现有数据上测不了（N=6 联合检出 <50%）"})
    return {
        "n_grid": list(V3_N_GRID),
        "schemes": dict(V3_T85_SCHEME_LABELS),
        "joint_false_positive": fp,
        "joint_detection": det,
        "eligibility": eligibility,
        "eligibility_violations": violations,
        "detection_at_n6_level_1": at_n6,
        "selected": selected,
        "selected_label": V3_T85_SCHEME_LABELS[selected],
        "selection_reason": reason,
        "unmeasurable": unmeasurable,
        "rule": ("预写（先于结果，第十三轮 §4.1）：合格=三 N 全设置双零假设"
                 "联合误报≤5%；合格方案里选 N=6 夏普1.0 联合检出较高者，"
                 "处处相同选丙；测不了=选中方案 N=6 联合检出<50%，N=2/24 并列"),
    }


def _v3_suite_for_symbols(symbols, timeframe, packs, cfg, seed, *,
                          planted: float | None = None,
                          shuffle_null: str | None = None,
                          initial_capital, risk_fraction,
                          session_policy=None, log=None) -> dict:
    """一套（可多品种）v3 检查记录：单品种 = 该品种套件；多品种（合并
    变体）= 主检验按**拼接 OOS 收益**（同年内跨品种折叠进同一年块）、
    否决/G0 按品种合取（每个品种都必须过）。planted>0 = 多头植入强度 d
    （压价 −d）；shuffle_null = 零假设打乱（同种子逐品种独立打乱）。"""
    per_symbol: dict[str, dict] = {}
    oos_parts = []
    bpd = bars_per_day_of(timeframe)
    period_days, interval_hours = v3_rhythm(timeframe)
    for symbol in symbols:
        pack = packs[symbol]
        bars = pack["bars"].iloc[(int(seed) % 10) * bpd:].reset_index(
            drop=True)
        phase_hours, day_phase = seed_phases(seed, interval_hours,
                                             period_days)
        if shuffle_null is not None:
            bars = NULL_SHUFFLERS[shuffle_null](bars, seed=seed)
        elif planted:
            bars = plant_edge(
                bars, period_days=period_days, interval_hours=interval_hours,
                hold_bars=PLANTED_HOLD_BARS, drift=-float(planted),
                phase_hours=phase_hours, day_phase=day_phase)
        strategy = LongPlantedEdgeStrategy(
            period_days=period_days, interval_hours=interval_hours,
            phase_hours=phase_hours, day_phase=day_phase)
        per_symbol[symbol] = run_v3_gate_suite(
            strategy, {"hold_bars": PLANTED_HOLD_BARS}, timeframe=timeframe,
            bars=bars, costs=pack["costs"], contract_size=pack["sizes"],
            vols=pack["vols"], cfg=cfg, seed=seed,
            grid=dict(PLANTED_GRID), symbol=symbol,
            session_policy=pack.get("session_policy"),
            initial_capital=initial_capital, risk_fraction=risk_fraction,
            log=log)
        oos_parts.append(per_symbol[symbol]["oos_returns"])
        if log:
            log(f"[v3] {symbol} {timeframe} seed={seed} "
                f"sharpe={per_symbol[symbol]['summary']['sharpe']} "
                f"segs={per_symbol[symbol]['wf_segments']}")
    if len(symbols) == 1:
        only = per_symbol[symbols[0]]
        return {"checks": only["checks"], "verdict": only["verdict"],
                "summary": only["summary"],
                "sharpe_by_symbol": {symbols[0]: only["summary"]["sharpe"]},
                "wf_segments": only["wf_segments"]}
    pooled = pd.concat(oos_parts)
    primary = v3_primary_suite(pooled, cfg, seed, n_trials=1)
    veto_all = {
        "G0": all(s["checks"]["G0"] for s in per_symbol.values()),
        **{f"veto.{k}": all(s["checks"][f"veto.{k}"]
                            for s in per_symbol.values())
           for k in ("cost_x15", "delay_1_bar", "buyhold_excess")},
    }
    checks = {
        **veto_all,
        **{f"primary.{scheme}": bool(c["passed"])
           for scheme, c in primary.items()},
        **{f"diag.merged_{sym}_{k}": s["checks"][f"diag.{k}"]
           for sym, s in per_symbol.items()
           for k in ("param_plateau", "yearly_segments", "drop_best_5pct")},
    }
    verdict = vgate.evaluate_v3(
        g0={"passed": veto_all["G0"]}, primary=primary["dsr"],
        cost_scale={"passed": veto_all["veto.cost_x15"]},
        delay={"passed": veto_all["veto.delay_1_bar"]},
        buyhold={"passed": veto_all["veto.buyhold_excess"]},
        diagnostics={k: {"passed": v} for k, v in checks.items()
                     if k.startswith("diag.")})
    return {"checks": checks, "verdict": verdict, "primary": primary,
            "sharpe_by_symbol": {sym: s["summary"]["sharpe"]
                                 for sym, s in per_symbol.items()},
            "wf_segments": sum(s["wf_segments"] for s in
                               per_symbol.values())}


def _v3_setup_key(symbols: list[str], timeframe: str) -> str:
    return ("MERGED:" + "+".join(symbols) if len(symbols) > 1
            else symbols[0]) + f" {timeframe}"


def parse_v3_setups(spec: str) -> list[tuple[list[str], str]]:
    """--v3-setups："SYM:TF,SYM+SYM:TF" → [(["SYM"], "TF"), …]。"""
    out: list[tuple[list[str], str]] = []
    for item in str(spec).split(","):
        item = item.strip()
        if not item:
            continue
        body, _, tf = item.partition(":")
        tf = (tf or "H1").strip().upper()
        syms = [s.strip().upper() for s in body.split("+") if s.strip()]
        if not syms:
            raise SystemExit(f"--v3-setups 条目 {item!r} 缺品种")
        out.append((syms, tf))
    if not out:
        raise SystemExit("--v3-setups 为空")
    return out


def v3_calibrate_level(symbol, timeframe, packs, cfg, level, *,
                       initial_capital, risk_fraction, log=None) -> dict:
    """多头植入校准到"扣成本后年化夏普 ≈ level"（calibrate_drift 的
    signal_sign=+1 变体：d 为压价强度，植入值 = −d）。"""
    pack = packs[symbol]
    period_days, interval_hours = v3_rhythm(timeframe)
    return calibrate_drift(
        symbol, timeframe, target_sharpe=level, period_days=period_days,
        interval_hours=interval_hours, hold_bars=PLANTED_HOLD_BARS, cfg=cfg,
        seed=0,
        initial_capital=initial_capital, risk_fraction=risk_fraction,
        bars_by_symbol={symbol: pack["bars"]},
        costs_by_symbol={symbol: pack["costs"]},
        sizes_by_symbol={symbol: pack["sizes"]},
        vols_by_symbol={symbol: pack["vols"]},
        session_policy=pack.get("session_policy"), log=log,
        signal_sign=1)


def run_v3_phase(args, log) -> dict:
    """--phase v3（T65/D34）：v3 提案套件的正负对照——负对照 = 两种零假设
    打乱 × 种子（drift=0），正对照 = 多头植入校准档位 × 种子；记录逐检查
    布尔（两方案 + 否决 + 诊断），联合通过率由 v3_joint_rates 重算。"""
    setups = parse_v3_setups(args.v3_setups)
    levels = [float(x) for x in str(args.v3_levels).split(",") if x.strip()]
    seeds = list(range(args.seeds_from, args.seeds_from + args.seeds))
    test_sims = int(args.v3_test_sims)
    nulls = list(NULL_ORDER)
    if args.quick:
        seeds = seeds[:2]
        levels = [max(levels)]
        test_sims = min(test_sims, 200)
    cfg, gates_v3_sha = vgate.load_gates(suite="v3")
    cfg = dataclasses.replace(cfg, bootstrap_sims=test_sims)
    needed = sorted({f"{sym} {tf}" for symbols, tf in setups for sym in symbols})
    print(f"— [v3] 装配 {len(needed)} 个序列：{needed}", flush=True)
    inputs_by_key = assemble_experiment(args.data_root, needed,
                                        max_bars=args.max_bars)
    ext_source = (args.v3_ext_source or "").strip()
    bars_source: dict[str, str] = {}
    if ext_source:
        # 延长前/后对照（T67/T81 外部数据）：ext 优先装载（与 T66
        # load_event_bars 同一装载器，manifest fail-fast + research_cutoff
        # 防御性再截），成本/合约/手数仍取研究库缓存
        for key in needed:
            sym, tf = key.split()
            bars, src = load_event_bars(args.data_root, sym, tf,
                                        ext_source=ext_source,
                                        max_bars=args.max_bars)
            inputs_by_key[key]["bars"] = bars
            bars_source[key] = src
            print(f"— [v3] {key}：ext 装载 {src}，{len(bars)} 根", flush=True)
    # 按 (品种, 周期) 取包——XAUUSD H1 与 H4 同时在场，只按品种键会互相覆盖
    packs_by_key = {tuple(key.split()): pack
                    for key, pack in inputs_by_key.items()}
    fingerprint = _run_fingerprint(inputs_by_key, gates_v3_sha)

    negative_runs: list[dict] = []
    positive_runs: list[dict] = []
    calibration: dict[str, dict] = {}
    for symbols, timeframe in setups:
        key = _v3_setup_key(symbols, timeframe)
        packs = {sym: packs_by_key[(sym, timeframe)] for sym in symbols}
        # ---- 负对照：两种零假设 × 种子（与正对照同一研究段子窗，配对）
        for null in nulls:
            for seed in seeds:
                t0 = time.monotonic()
                out = _v3_suite_for_symbols(
                    symbols, timeframe, packs, cfg, seed,
                    shuffle_null=null, initial_capital=args.initial_capital,
                    risk_fraction=args.risk_per_trade, log=log)
                negative_runs.append({
                    "setup": key, "null": null, "seed": int(seed),
                    "sharpe_by_symbol": out["sharpe_by_symbol"],
                    "wf_segments": out["wf_segments"],
                    "checks": out["checks"]})
                if log:
                    log(f"[v3/neg] {key} {null} seed={seed} "
                        f"({time.monotonic() - t0:.1f}s)")
        # ---- 正对照：档位校准（逐品种）+ 植入 × 种子
        level_cal: dict[str, dict[float, dict]] = {sym: {} for sym in symbols}
        for sym in symbols:
            for level in levels:
                level_cal[sym][level] = dict(v3_calibrate_level(
                    sym, timeframe, packs, cfg, level,
                    initial_capital=args.initial_capital,
                    risk_fraction=args.risk_per_trade, log=log))
        calibration[key] = {sym: {str(level): cal
                                  for level, cal in by_level.items()}
                            for sym, by_level in level_cal.items()}
        for level in levels:
            for seed in seeds:
                t0 = time.monotonic()
                # 逐品种用各自校准出的压价强度（同档位可比：都校准到
                # "扣成本后年化夏普 ≈ level"）
                out_by_symbol: dict[str, dict] = {}
                oos_parts = []
                bpd = bars_per_day_of(timeframe)
                period_days, interval_hours = v3_rhythm(timeframe)
                for sym in symbols:
                    pack = packs[sym]
                    drift = float(level_cal[sym][level]["drift"])
                    phase_hours, day_phase = seed_phases(seed, interval_hours,
                                                         period_days)
                    segment = pack["bars"].iloc[
                        (int(seed) % 10) * bpd:].reset_index(drop=True)
                    planted_bars = plant_edge(
                        segment, period_days=period_days,
                        interval_hours=interval_hours,
                        hold_bars=PLANTED_HOLD_BARS, drift=-drift,
                        phase_hours=phase_hours, day_phase=day_phase)
                    strategy = LongPlantedEdgeStrategy(
                        period_days=period_days, interval_hours=interval_hours,
                        phase_hours=phase_hours, day_phase=day_phase)
                    out_by_symbol[sym] = run_v3_gate_suite(
                        strategy, {"hold_bars": PLANTED_HOLD_BARS},
                        timeframe=timeframe, bars=planted_bars,
                        costs=pack["costs"], contract_size=pack["sizes"],
                        vols=pack["vols"], cfg=cfg, seed=seed,
                        grid=dict(PLANTED_GRID), symbol=sym,
                        session_policy=pack.get("session_policy"),
                        initial_capital=args.initial_capital,
                        risk_fraction=args.risk_per_trade)
                    oos_parts.append(out_by_symbol[sym]["oos_returns"])
                if len(symbols) == 1:
                    only = out_by_symbol[symbols[0]]
                    checks, verdict = only["checks"], only["verdict"]
                else:
                    pooled = pd.concat(oos_parts)
                    primary = v3_primary_suite(pooled, cfg, seed, n_trials=1)
                    veto_all = {
                        "G0": all(s["checks"]["G0"] for s in
                                  out_by_symbol.values()),
                        **{f"veto.{k}": all(s["checks"][f"veto.{k}"] for s
                                            in out_by_symbol.values())
                           for k in ("cost_x15", "delay_1_bar",
                                     "buyhold_excess")}}
                    checks = {**veto_all,
                              **{f"primary.{sc}": bool(c["passed"])
                                 for sc, c in primary.items()},
                              **{f"diag.merged_{sym}_{k}":
                                 s["checks"][f"diag.{k}"]
                                 for sym, s in out_by_symbol.items()
                                 for k in ("param_plateau", "yearly_segments",
                                           "drop_best_5pct")}}
                    verdict = vgate.evaluate_v3(
                        g0={"passed": veto_all["G0"]},
                        primary=primary["dsr"],
                        cost_scale={"passed": veto_all["veto.cost_x15"]},
                        delay={"passed": veto_all["veto.delay_1_bar"]},
                        buyhold={"passed": veto_all["veto.buyhold_excess"]},
                        diagnostics={k: {"passed": v} for k, v in
                                     checks.items() if k.startswith("diag.")})
                positive_runs.append({
                    "setup": key, "level": float(level), "seed": int(seed),
                    "drift_by_symbol": {sym: float(level_cal[sym][level]
                                                   ["drift"])
                                        for sym in symbols},
                    "sharpe_by_symbol": {sym: s["summary"]["sharpe"] for
                                         sym, s in out_by_symbol.items()},
                    "checks": checks})
                if log:
                    log(f"[v3/pos] {key} level={level} seed={seed} "
                        f"({time.monotonic() - t0:.1f}s)")

    fp_rates: dict[str, dict[str, float]] = {}
    for setup in {r["setup"] for r in negative_runs}:
        for null in nulls:
            bucket = [r for r in negative_runs
                      if r["setup"] == setup and r["null"] == null]
            if bucket:
                fp_rates[f"{setup}|{null}"] = v3_joint_rates(bucket)
    detection: dict[str, dict[str, dict[str, float]]] = {}
    for setup in {r["setup"] for r in positive_runs}:
        detection[setup] = {}
        for level in levels:
            bucket = [r for r in positive_runs
                      if r["setup"] == setup and r["level"] == level]
            if bucket:
                detection[setup][str(level)] = v3_joint_rates(bucket)
    return {
        "meta": {
            "phase": "v3", "seeds": len(seeds),
            "seeds_from": int(args.seeds_from),
            "levels": levels, "setups": [_v3_setup_key(s, tf)
                                         for s, tf in setups],
            "nulls": nulls, "primary_schemes": ["dsr", "year_block"],
            "bonferroni_sensitivity_n": V3_BONFERRONI_SENSITIVITY_N,
            "n_trials": 1,
            "t85_n_grid": list(V3_N_GRID),
            "test_sims": int(test_sims),
            "gates_v3_sha256": gates_v3_sha,
            "initial_capital": float(args.initial_capital),
            "risk_per_trade": float(args.risk_per_trade),
            "max_bars": args.max_bars, "data_root": str(args.data_root),
            "ext_source": ext_source or None,
            "bars_source": bars_source,
            "fingerprint": fingerprint,
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "design": (
                "正对照=LONG 植入（事件后压价 d、信号 +1，buyhold 否决真实"
                "参与判定），d 校准到扣成本后年化夏普≈档位（逐品种）；负对照"
                "=day/slot 两种零假设打乱同一研究段子窗（与正对照配对）；"
                "主检验两方案（甲 DSR / 乙 年块自助）逐条都算，乙另报 "
                f"Bonferroni N={V3_BONFERRONI_SENSITIVITY_N} 敏感性；联合"
                "通过率=主检验+全部否决项（G0/成本×1.5/延迟一根/多头超额），"
                "由逐条记录重算；诊断（G1/参数邻域/按年分段/去最好 5%）只"
                "报告不否决。事件模式分支引用 T66 标定，不在此重标。"),
        },
        "negative": {"runs": negative_runs,
                     "joint_false_positive_rates": fp_rates},
        "positive": {"runs": positive_runs,
                     "joint_detection_rates": detection,
                     "calibration": calibration},
    }


def render_v3_report(results: dict) -> str:
    """--phase v3 产物（gate-power-v3.md）：联合误报/检出表（两方案 +
    Bonferroni 敏感性 + T85 N 网格甲/丙）+ 分检查率 + 校准 + 口径。"""
    meta = results.get("meta", {})
    fmt = lambda v: "—" if v is None else f"{float(v) * 100:.0f}%"  # noqa: E731
    cols = ["joint.dsr", "joint.year_block",
            f"joint.year_block@N{V3_BONFERRONI_SENSITIVITY_N}",
            "primary.dsr", "primary.year_block", "G0", "veto.cost_x15",
            "veto.delay_1_bar", "veto.buyhold_excess",
            *[f"joint.{s}@N{n}" for s in ("dsr_v0", "psr0_bonf")
              for n in V3_N_GRID]]

    def table(rows: list[tuple[str, dict[str, float]]], first_header: str
              ) -> list[str]:
        lines = [f"| {first_header} | " + " | ".join(cols) + " |",
                 "| --- " * (len(cols) + 1) + "|"]
        for label, rates in rows:
            lines.append(f"| {label} | "
                         + " | ".join(fmt(rates.get(c)) for c in cols) + " |")
        return lines

    lines = ["# 关卡 v3 对照实验（gate-power-v3，T65/D34 提案标定"
             " + T85 工作点重跑）", "",
             f"- 序列/设置：{'; '.join(meta.get('setups', []))}",
             f"- 档位（校准后年化夏普）：{meta.get('levels')}；"
             f"种子 {meta.get('seeds')}；零假设 {meta.get('nulls')}",
             f"- 主检验两方案：甲 DSR（N={meta.get('n_trials')}，实验不经"
             "台账选择）/ 乙 年块自助（Bonferroni 同 N；另报 "
             f"N={meta.get('bonferroni_sensitivity_n')} 敏感性档）；自助 "
             f"{meta.get('test_sims')} 次",
             f"- T85 工作点键：甲 DSR(N,V₀=1/(n−1)) 与丙 PSR(0)≥1−α/N 逐 "
             f"N∈{meta.get('t85_n_grid', list(V3_N_GRID))} 记录（同一 OOS "
             "收益；joint.dsr_v0@N* / joint.psr0_bonf@N* 两列）",
             f"- 门槛文件 sha256：{str(meta.get('gates_v3_sha256', ''))[:12]}"
             f"（T85/D34 冻结后 = config/gates.toml，v3 单文件）"
             f"；生成：{meta.get('generated_utc', '?')}", "",
             "联合通过率 = 主检验 + 全部否决项（G0 / 成本 ×1.5 / 延迟一根 /"
             " 多头相对买入持有超额）——由逐条记录重算；诊断项（G1/参数邻域/"
             "按年分段/去最好 5%）只报告，不进判定。", "",
             "## 负对照联合误报率（零假设 × 设置）", ""]
    fp = results.get("negative", {}).get("joint_false_positive_rates") or {}
    lines += table(sorted(fp.items()), "设置 \\ 零假设")
    lines += ["", "## 正对照联合检出率（档位 × 设置）", ""]
    det = results.get("positive", {}).get("joint_detection_rates") or {}
    pos_rows: list[tuple[str, dict[str, float]]] = []
    for setup in sorted(det):
        for level in sorted(det[setup], key=float):
            pos_rows.append((f"{setup} 夏普≈{level}", det[setup][level]))
    lines += table(pos_rows, "设置 × 档位")
    cal = results.get("positive", {}).get("calibration") or {}
    if cal:
        lines += ["", "## 校准（多头压价强度 d → 扣成本后年化夏普）", ""]
        for setup, by_symbol in sorted(cal.items()):
            for symbol, by_level in sorted(by_symbol.items()):
                parts = []
                for level, info in sorted(by_level.items(),
                                          key=lambda kv: float(kv[0])):
                    mark = "" if info.get("converged", True) else "，未收敛"
                    parts.append(f"≈{level} → d={info['drift']:.6g}"
                                 f"（实测 {info['achieved_sharpe']:.2f}"
                                 f"{mark}）")
                lines.append(f"- {setup} / {symbol}：" + "；".join(parts))
    lines += ["", "## 口径备注", "",
              "- 正对照为 LONG 植入（事件后压价、信号 +1）：多头让 buyhold "
              "否决（纯多头须跑赢买入持有）真实参与判定；负对照与正对照用"
              "同一研究段子窗（同种子偏移），配对可比。",
              "- 主检验甲（DSR）实验 N=1（退化为 PSR(0)）：实验不经 screen/"
              "台账的网格选择；正式路径按 [g4].dsr_trial_scope=project 从"
              "台账 dedup 取数。方案乙 Bonferroni 同理，敏感性档 N="
              f"{V3_BONFERRONI_SENSITIVITY_N}（R1 草案轮次预算量级）。",
              "- 年块自助的年块数为研究段 OOS 覆盖年数（XAUUSD 约 2016 起 "
              "≈10 块）：T66 标定指出块数 <40 时经验水平偏高（有效临界分布"
              "≈t_{B−1}），本表按联合口径如实呈现。",
              "- 多品种合并变体：主检验按拼接 OOS 收益（同年内跨品种折叠进"
              "同一年块）、否决/G0 按品种合取；诊断只报参数邻域/按年分段/"
              "去最好 5% 三项（计算量控制；正式路径全量诊断照跑）。",
              "- 事件模式分支不在此重标：主检验乙 = T66 event_test 同一"
              "实现，误报/检出见 docs/research/event-mode-calibration-"
              "2026-10-28.md。", ""]
    return "\n".join(lines)


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


def flags_level_rates(positive_detection: dict, g5_detection: dict
                      ) -> dict[str, float]:
    """决策规则盯的 1.0 档检出率（R6/C8）：普通正对照与 G5 检出率是**两套
    来源的关卡子检查，必须同时遍历**——绝不能 either/or（旧实现 `positive
    or g5`：只要存在普通正对照，G5 检出率为零也不产生低检出率告警）。
    同名检查跨 setup / 跨来源取最小值（最保守）；G5.passed 是汇总数据不参与。
    """
    level_rates: dict[str, float] = {}
    for source in (positive_detection, g5_detection):
        for by_level in source.values():
            for check, rate in (by_level.get(str(DECISION_LEVEL))
                                or {}).items():
                if check == "G5.passed":
                    continue
                prev = level_rates.get(check)
                level_rates[check] = (float(rate) if prev is None
                                      else min(prev, float(rate)))
    return level_rates


def compute_flags(results: dict) -> list[dict]:
    """产出报告的决策规则标记（主进程与 merge_gate_power 共用同一函数，
    R6/C8）：

    - 误报率：负对照 false_positive_rates（单进程/合并器均已按 null 取最差）
      与 G5 各 null 误报率**逐检查取最大**（不得混桶平均或被 0% 桶稀释）；
    - 检出率：flags_level_rates（正对照 + G5 双源，取最小）。
    """
    fp_rates = dict((results.get("negative") or {})
                    .get("false_positive_rates") or {})
    g5_block = results.get("g5") or {}
    for null, rates in (g5_block.get("false_positive_rates") or {}).items():
        for check, rate in rates.items():
            if check == "G5.passed":        # 汇总数据，不进决策规则
                continue
            fp_rates[check] = max(fp_rates.get(check, 0.0), float(rate))
    level_rates = flags_level_rates(
        (results.get("positive") or {}).get("detection_rates") or {},
        g5_block.get("detection_rates") or {})
    if not (level_rates or fp_rates):
        return []
    return collect_flags(level_rates, fp_rates)


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
        f"- 生成：{meta.get('generated_utc', '?')}（gate_calibration.py）",
    ]
    fp = meta.get("fingerprint") or {}
    if fp:
        lines += [
            f"- 运行指纹：git {str(fp.get('git_commit', ''))[:12] or '?'}"
            + ("（工作区不干净）" if fp.get("git_dirty") else "")
            + f"；gates[{str(fp.get('gates_sha256', ''))[:12]}]"
            + f"；脚本[{str(fp.get('script_sha256', ''))[:12]}]",
            "- 成本版本：" + "; ".join(
                f"{k} → {v}" for k, v in
                (fp.get("cost_versions") or {}).items()),
        ]
    lines += [
        "",
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
            parts = []
            for lv, info in sorted(cal.items(), key=lambda kv: float(kv[0])):
                mark = "" if info.get("converged", True) else "，未收敛"
                parts.append(f"≈{lv} → d={info['drift']:.6g}"
                             f"（实测 {info['achieved_sharpe']:.2f}{mark}）")
            lines += ["", "校准（扣成本后年化夏普 → 漂移 d；迭代过程/目标/容差"
                      "见 JSON 的 calibration.history）：" + "；".join(parts)]
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
                                      research_store=research_store,
                                      # T83/R13-K5：点数模式隔夜费相对价格基准
                                      # = 研究段 K 线最后收盘（与 CLI 主路径
                                      # 同一约定）——长历史对照实验不再按快照
                                      # 时代绝对额放大早年隔夜费（K5）
                                      swap_ref_price=float(
                                          bars["close"].iloc[-1]))
        out[key] = {
            "bars": bars,
            "costs": costs,
            "cost_version": str(_version),
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
                    "+ 正对照检出率 + G5 判别力 + 合并评估变体 → JSON+MD；"
                    "--phase event 为 T66/D35 事件模式对照臂（event-power）")
    ap.add_argument("--phase", choices=("negative", "positive", "both",
                                        "event", "v3"),
                    default="both")
    ap.add_argument("--seeds-from", type=int, default=0,
                    help="种子起点（分片并行：各分片用不相交种子段，结果经 "
                         "scripts/merge_gate_power.py 合并）")
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
    # ---- 事件模式对照臂（T66/D35，--phase event）
    ap.add_argument("--event-symbols", default=",".join(EVENT_DEFAULT_SYMBOLS),
                    help="事件臂 SYMBOL TF 列表（缺省 USTEC D1——fred ext "
                         "39.8 年现成长序列；格式 'USTEC D1,EURUSD H1'）")
    ap.add_argument("--event-kind", choices=("month_end", "london_fix"),
                    default="month_end",
                    help="事件机制：月末换月窗（D1）或伦敦定盘窗（H1，月末"
                         "交易日 16:00 London 定盘前 lead 60 分钟）")
    ap.add_argument("--event-ext-source", default="fred",
                    help="已批准外部序列来源（fred/cdd；空串=只用研究库 "
                         "bars）——ext 经 manifest fail-fast 装载")
    ap.add_argument("--event-levels",
                    default=",".join(str(v) for v in EVENT_DELTA_LEVELS),
                    help="正对照 δ 档位（每事件毛效应 ÷ σ_w 的倍数）")
    ap.add_argument("--event-seeds", type=int, default=DEFAULT_SEEDS,
                    help="事件臂负/正对照各自的种子数（缺省 20）")
    ap.add_argument("--event-test-sims", type=int, default=1000,
                    help="年块自助次数（event_test）")
    ap.add_argument("--event-rand-sims", type=int, default=199,
                    help="随机化参照检验的随机化次数（正对照减半至 "
                         "ceil/2——检出率粒度 20 种子已够）")
    # ---- 关卡 v3 对照臂（T65/D34，--phase v3）
    ap.add_argument("--v3-setups",
                    default=",".join(V3_DEFAULT_SETUPS),
                    help="v3 臂设置（缺省 XAUUSD:H1,XAUUSD:H4,XAUUSD+USTEC"
                         ":H1；SYM+SYM:TF = 多品种合并变体——主检验按拼接"
                         "OOS 收益、否决按品种合取）")
    ap.add_argument("--v3-levels",
                    default=",".join(str(v) for v in V3_DEFAULT_LEVELS),
                    help="v3 正对照夏普档位（逗号分隔，缺省 0.5,1.0,1.5）")
    ap.add_argument("--v3-test-sims", type=int, default=1000,
                    help="方案乙年块自助次数（--quick 降至 200）")
    ap.add_argument("--v3-ext-source", default="",
                    help="v3 臂外部数据来源（fred/cdd；空串=只用研究库 bars）"
                         "——延长前/后对照用：ext 优先装载（T66 同一装载器），"
                         "成本/合约仍取研究库缓存")
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


def run_event_phase(args, log) -> dict:
    """--phase event（T66/D35）：事件模式对照臂——独立装配（ext/研究库
    K 线 + 研究库成本 + specs 合约大小），产物 event-power.json/md。"""
    from run_backtest import SESSIONS_PATH, build_costs, classify_store, \
        resolve_contract_size, sessions_sha256

    levels = [float(x) for x in str(args.event_levels).split(",") if x.strip()]
    seeds = int(args.event_seeds)
    test_sims, rand_sims = int(args.event_test_sims), int(args.event_rand_sims)
    power_grid = list(EVENT_POWER_N_GRID)
    if args.quick:
        seeds, levels, test_sims = 2, [max(levels)], 200
        rand_sims, power_grid = 49, [60]
    keys = []
    for item in str(args.event_symbols).split(","):
        parts = item.split()
        if len(parts) == 2:
            keys.append((parts[0].upper(), parts[1].upper()))
        elif parts:
            raise SystemExit(f"--event-symbols 条目 {item!r} 须形如 'USTEC D1'")
    research_store = classify_store(str(args.data_root)) == "research"
    sess_sha = sessions_sha256(str(SESSIONS_PATH))
    arms: list[dict] = []
    setups: list[dict] = []
    for symbol, timeframe in keys:
        t0 = time.monotonic()
        bars, source = load_event_bars(args.data_root, symbol, timeframe,
                                       ext_source=args.event_ext_source or None,
                                       max_bars=args.max_bars)
        costs, cost_version = build_costs(
            str(args.data_root), symbol, 0.1, 0.0, sessions_sha=sess_sha,
            research_store=research_store,
            # T83/R13-K5：事件臂 K 线（ext/研究库同源）最后收盘——与
            # assemble_experiment/CLI 主路径同一约定，relref 入 cost_version
            # （进运行指纹，合并器可区分新旧口径）
            swap_ref_price=float(bars["close"].iloc[-1]))
        contract_size = resolve_contract_size(str(args.data_root), symbol, None)
        log(f"[event] {symbol} {timeframe}：{len(bars)} 根（{source}），"
            f"成本 {cost_version}，合约 {contract_size} "
            f"({time.monotonic() - t0:.1f}s)")
        arm = event_mode_calibration(
            bars, symbol=symbol, timeframe=timeframe, costs=costs,
            contract_size=contract_size, kind=args.event_kind,
            neg_seeds=seeds, levels=levels, pos_seeds=seeds,
            test_sims=test_sims, neg_rand_sims=rand_sims,
            pos_rand_sims=max(19, rand_sims // 2), power_n_grid=power_grid,
            log=log)
        arm["bars_source"] = source
        arm["cost_version"] = str(cost_version)
        arms.append(arm)
        setups.append({"symbol": symbol, "timeframe": timeframe,
                       "kind": arm["kind"], "bars_source": source,
                       "n_events": arm["n_events"], "first": arm["first"],
                       "last": arm["last"]})
    fingerprint = _run_fingerprint({}, vgate.load_gates()[1])
    for arm in arms:
        arm["fingerprint_inputs"] = {
            "bars_source": arm.pop("bars_source"),
            "cost_version": arm.pop("cost_version"),
        }
    return {
        "meta": {
            "phase": "event", "seeds": seeds, "levels": levels,
            "alpha": EVENT_ALPHA, "test_sims": test_sims,
            "rand_sims": rand_sims, "kind": args.event_kind,
            "ext_source": args.event_ext_source or None,
            "data_root": str(args.data_root), "max_bars": args.max_bars,
            "fingerprint": fingerprint,
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "design": ("负对照=事件时间随机化（matched 不重叠抽样池，保持"
                       "星期分布与月内三分位）；正对照=事件窗植入 δ（σ_w "
                       "倍数）；统计量分列年块自助/随机化参照/成本×1.5 联合"),
        },
        "setups": setups, "arms": arms,
    }


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    log = lambda msg: print(msg, flush=True)      # noqa: E731

    if args.phase == "event":          # T66/D35 事件模式对照臂（独立装配）
        results = run_event_phase(args, log)
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        json_path = out_dir / "event-power.json"
        md_path = out_dir / "event-power.md"
        json_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2,
                       default=_json_default), encoding="utf-8")
        md_path.write_text(render_event_report(results), encoding="utf-8")
        print(f"— 产出：{json_path} 与 {md_path}", flush=True)
        return 0

    if args.phase == "v3":             # T65/D34 关卡 v3 对照臂
        results = run_v3_phase(args, log)
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        json_path = out_dir / "gate-power-v3.json"
        md_path = out_dir / "gate-power-v3.md"
        json_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2,
                       default=_json_default), encoding="utf-8")
        md_path.write_text(render_v3_report(results), encoding="utf-8")
        print(f"— 产出：{json_path} 与 {md_path}", flush=True)
        fp = (results.get("negative", {})
              .get("joint_false_positive_rates") or {})
        worst = max((rates.get("joint.dsr", 0.0) for rates in fp.values()),
                    default=0.0)
        print(f"— 负对照联合误报率（甲）最差 {worst:.0%}（详见 "
              "gate-power-v3.md）", flush=True)
        return 0

    seeds = list(range(args.seeds_from, args.seeds_from + args.seeds))
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
        "fingerprint": _run_fingerprint(inputs_by_key,
                                        vgate.load_gates()[1]),
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
        "seeds_from": int(args.seeds_from),
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
                # N-M1/R7：完整校准记录（history/目标/容差/收敛）进 JSON——
                # 报告"详见 JSON 的 calibration.history"的承诺由此成立
                level_cal[level] = dict(cal)
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

    # R6/C8：flags 经 compute_flags——正对照与 G5 检出率同时遍历
    # （旧实现 either/or：存在正对照时 G5 低检出率被整体跳过）。
    results["flags"] = compute_flags(results)

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
