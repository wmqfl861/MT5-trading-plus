"""scripts/gate_calibration.py（T22）：正负对照实验编排。

离线小样本：全部经注入的合成 K 线 + 常数成本替身运行（不碰真实数据根、
不连 MT5、不跑真实 G5）。CLI 端到端用 monkeypatch 掉装配函数验证
--quick 全流程与产物（JSON + Markdown）。
"""
import dataclasses
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from mt5quant.validation.gates import load_gates
from mt5quant.validation.synthetic import PlantedEdgeStrategy, RandomEntries

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "gate_calibration.py"


def load_gc():
    spec = importlib.util.spec_from_file_location("gate_calibration_script",
                                                  SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gate_calibration_script"] = mod
    spec.loader.exec_module(mod)
    return mod


class ConstCosts:
    """固定成本替身（价格单位点差/滑点；每手佣金）。"""

    def __init__(self, spread=0.05, slippage=0.02, commission=0.0):
        self._spread, self._slip = spread, slippage
        self._commission = commission

    def spread_p90(self, symbol, hour_ny, *, price=None):
        return self._spread

    def slippage(self, symbol, hour_ny, *, price=None):
        return self._slip

    def commission_per_lot(self, symbol):
        return self._commission

    def overnight_fee(self, symbol, side, from_ts, to_ts):
        return 0.0


def random_walk_bars(n_days=500, bars_per_day=8, s0=2000.0, vol=0.002,
                     seed=42, start="2023-01-01"):
    """跨多个月份的随机游走 K 线（G3 日历月窗口需要 ≥ 训练+测试月数）。

    bars_per_day=8：日事件（interval 24h）与 hold=4 之间留出空档——
    植入事件的信号段不会首尾相接铺满全天。
    """
    rng = np.random.default_rng(seed)
    n = n_days * bars_per_day
    closes = s0 * np.cumprod(1.0 + rng.normal(0.0, vol, n))
    opens = np.concatenate(([s0], closes[:-1]))
    t0 = pd.Timestamp(start)
    times = [t0 + pd.Timedelta(days=d, minutes=60 * k)
             for d in range(n_days) for k in range(bars_per_day)]
    return pd.DataFrame({
        "time": pd.Series(times),
        "open": opens,
        "high": np.maximum(opens, closes) * 1.001,
        "low": np.minimum(opens, closes) * 0.999,
        "close": closes,
        "tick_volume": 1, "spread": 1, "real_volume": 0,
    })


@pytest.fixture(scope="module")
def gc():
    return load_gc()


@pytest.fixture(scope="module")
def cfg():
    c, _ = load_gates()
    return dataclasses.replace(c, random_entry_sims=3, mc_drawdown_sims=40,
                               trade_count_min_hourly=40,
                               trade_count_min_intraday=40,
                               train_months_hourly=12, test_months_hourly=3,
                               train_months_intraday=12,
                               test_months_intraday=3)


@pytest.fixture(scope="module")
def inputs():
    return {
        "bars_by_symbol": {"TST": random_walk_bars()},
        "costs_by_symbol": {"TST": ConstCosts()},
        "sizes_by_symbol": {"TST": 1.0},
        "vols_by_symbol": {"TST": (0.01, 0.01, 100.0)},
    }


# ---------------------------------------------------------------- 套件

def test_run_gate_suite_structure_and_gates(gc, cfg, inputs):
    strat = PlantedEdgeStrategy(period_days=1, interval_hours=24)
    out = gc.run_gate_suite(
        strat, {"hold_bars": 4}, timeframe="H1", cfg=cfg, seed=7,
        grid={"hold_bars": [4, 8]}, **inputs)
    for gate in ("G0", "G1", "G2", "G3", "G4"):
        assert gate in out and isinstance(out[gate].get("passed"), bool)
    for check in ("net_profit_positive", "trade_count", "gross_cost_multiple",
                  "profit_factor"):
        assert check in out["G1"]["checks"]
    for check in ("cost_scaling", "param_plateau", "yearly_segments",
                  "cross_symbol", "random_entry_benchmark", "drop_best_5pct",
                  "delay_1_bar"):
        assert check in out["G2"]["checks"]
    for check in ("deflated_sharpe", "mc_drawdown"):
        assert check in out["G4"]["checks"]
    assert {"net_profit", "sharpe", "trade_count"} <= set(out["summary"])


def test_run_gate_suite_reproducible(gc, cfg, inputs):
    """计划失败测试：同一种子（同一实验条件）两次运行结果逐位一致。"""
    strat = RandomEntries(seed=3, hold_bars=4, event_every_bars=24)
    common = dict(timeframe="H1", cfg=cfg, seed=7, grid={}, **inputs)
    a = gc.run_gate_suite(strat, {}, **common)
    b = gc.run_gate_suite(strat, {}, **common)
    assert a["summary"]["net_profit"] == b["summary"]["net_profit"]
    assert a["summary"]["sharpe"] == b["summary"]["sharpe"]
    assert a["G1"]["checks"] == b["G1"]["checks"]
    assert a["G2"]["checks"] == b["G2"]["checks"]


def test_g2_random_sims_override_reaches_check(gc, cfg, inputs, monkeypatch):
    """实验里 G2 随机入场降到 50（正式关卡 200 不变）：cfg 透传生效。"""
    from mt5quant.validation import robustness as vrob
    seen = {}

    def spy(spec, bars, costs, base_trades, base_sharpe, *, sims, percentile,
            seed):
        seen["sims"] = sims
        return {"passed": True, "valid_sims": sims, "p95": 0.0}

    monkeypatch.setattr(vrob, "random_entry_check", spy)
    gc.run_gate_suite(RandomEntries(seed=1, hold_bars=4), {}, timeframe="H1",
                      cfg=dataclasses.replace(cfg, random_entry_sims=17),
                      seed=1, grid={}, **inputs)
    assert seen["sims"] == 17


# ---------------------------------------------------------------- 负对照

def test_negative_control_structure(gc, cfg, inputs):
    result = gc.negative_control(
        [("RND", lambda seed: RandomEntries(seed=int(seed), hold_bars=4,
                                            event_every_bars=24), {}, {},
          "H1")],
        seeds=[0, 1], cfg=cfg, initial_capital=10_000.0,
        risk_fraction=0.005, **inputs)
    assert set(result) >= {"runs", "false_positive_rates"}
    rates = result["false_positive_rates"]
    assert rates
    for key, rate in rates.items():
        assert key.startswith(("G0", "G1", "G2", "G3", "G4")), key
        assert 0.0 <= rate <= 1.0
    assert len(result["runs"]) == 2       # 1 个基线 × 2 种子
    run = result["runs"][0]
    assert {"name", "seed", "sharpe", "checks"} <= set(run)


# ---------------------------------------------------------------- 正对照

def test_calibrate_drift_reaches_target(gc, cfg, inputs):
    out = gc.calibrate_drift(
        "TST", "H1", target_sharpe=1.0, period_days=1, interval_hours=24,
        hold_bars=4, cfg=cfg, seed=0, max_iter=6, tol=0.15,
        initial_capital=10_000.0, risk_fraction=0.005, **inputs)
    assert out["drift"] > 0.0
    assert abs(out["achieved_sharpe"] - 1.0) <= 0.35
    assert out["iterations"] <= 6
    assert len(out["history"]) >= 2


def test_positive_control_structure_and_detection(gc, cfg, inputs):
    cal = gc.calibrate_drift(
        "TST", "H1", target_sharpe=1.5, period_days=1, interval_hours=24,
        hold_bars=4, cfg=cfg, seed=0, max_iter=6, tol=0.2,
        initial_capital=10_000.0, risk_fraction=0.005, **inputs)
    level_cal = {1.5: {"drift": cal["drift"],
                       "achieved_sharpe": cal["achieved_sharpe"]}}
    setup = ("TST", "H1", level_cal, 1, 24, 4)
    kwargs = dict(levels=[1.5], seeds=[0, 1], cfg=cfg,
                  initial_capital=10_000.0, risk_fraction=0.005, **inputs)
    result = gc.positive_control([setup], **kwargs)
    det = result["detection_rates"]["TST H1"]["1.5"]
    assert det["G1.net_profit_positive"] >= 0.5    # 夏普 1.5 净利须为正
    assert det["G0"] == 1.0
    assert 0.0 <= det["G4.deflated_sharpe"] <= 1.0
    assert result["calibration"]["TST H1"]["1.5"]["drift"] == cal["drift"]
    again = gc.positive_control([setup], **kwargs)
    assert again["runs"] == result["runs"]         # 逐位可复现


# ---------------------------------------------------------------- 报告

def test_render_report_decision_rules_and_flags(gc):
    results = {
        "meta": {"phase": "both", "seeds": 20, "levels": [0.3, 0.6, 1.0, 1.5],
                 "g2_random_entry_sims": 50},
        "negative": {"false_positive_rates": {"G1.profit_factor": 0.30}},
        "positive": {
            "calibration": {"XAUUSD H1": {"1.0": {"drift": 1.2,
                                                  "achieved_sharpe": 1.02}}},
            "detection_rates": {"XAUUSD H1": {
                "1.0": {"G1.profit_factor": 0.40, "G1.trade_count": 1.0}}},
        },
    }
    md = gc.render_report(results)
    assert "误报率" in md and "检出率" in md
    assert "G1.profit_factor" in md
    # 决策规则触发：FP 0.30 > 5%、1.0 档检出 0.40 < 50% → 门槛调整建议
    assert "门槛调整建议" in md
    assert "50 次" in md                     # G2 随机入场降次注明


def test_flag_rule_marks_correct_checks(gc):
    flags = gc.collect_flags(
        {"G1.profit_factor": 0.40},               # 1.0 档检出 < 50% → 标记
        {"G1.trade_count": 0.30},                 # 误报 > 5% → 标记
        level_for_detection="1.0")
    marked = {f["check"] for f in flags}
    assert marked == {"G1.profit_factor", "G1.trade_count"}


def test_flag_rule_skips_structural_checks(gc):
    """结构性通过检查（G0/不适用按通过/回撤离群）不参与误报标记。"""
    flags = gc.collect_flags(
        {"G1.profit_factor": 0.9},
        {"G0": 1.0, "G2.buy_hold_benchmark": 0.9,
         "G2.cross_symbol": 1.0, "G4.mc_drawdown": 1.0})
    assert flags == []


# ---------------------------------------------------------------- CLI

def test_cli_quick_end_to_end(gc, inputs, monkeypatch, tmp_path):
    """--quick 端到端：装配注入小样本 → JSON + Markdown 落盘、退出码 0。"""
    small = {**inputs,
             "bars_by_symbol": {"TST": inputs["bars_by_symbol"]["TST"]
                                .iloc[:1200]}}
    monkeypatch.setattr(gc, "load_gates_for_run", lambda: _quick_cfg())
    monkeypatch.setattr(
        gc, "BASELINES",
        ({"name": "RND-CLI", "factory":
          lambda seed: RandomEntries(seed=int(seed), hold_bars=4,
                                     event_every_bars=24),
          "params": {}, "grid": {}, "symbols": ("TST",), "timeframe": "H1"},))
    monkeypatch.setattr(gc, "assemble_experiment",
                        lambda data_root, symbols, max_bars: {
                            "TST H1": {
                                "bars": small["bars_by_symbol"]["TST"],
                                "costs": small["costs_by_symbol"]["TST"],
                                "sizes": small["sizes_by_symbol"]["TST"],
                                "vols": small["vols_by_symbol"]["TST"],
                                "timeframe": "H1", "session_policy": None},
                        })
    out_dir = tmp_path / "gates"
    rc = gc.main(["--phase", "both", "--quick", "--seeds", "2",
                  "--symbols", "TST:H1", "--out", str(out_dir)])
    assert rc == 0
    files = sorted(p.name for p in out_dir.iterdir())
    assert any(f.endswith(".json") for f in files)
    assert any(f.endswith(".md") for f in files)
    payload = json.loads(next(out_dir.glob("*.json")).read_text(
        encoding="utf-8"))
    assert payload["meta"]["phase"] == "both"
    assert "negative" in payload and "positive" in payload
    md = next(out_dir.glob("*.md")).read_text(encoding="utf-8")
    assert "决策规则" in md


def _quick_cfg():
    c, _ = load_gates()
    return dataclasses.replace(c, random_entry_sims=2, mc_drawdown_sims=20,
                               trade_count_min_hourly=30,
                               trade_count_min_intraday=30,
                               train_months_hourly=24, test_months_hourly=6,
                               train_months_intraday=12,
                               test_months_intraday=3)
