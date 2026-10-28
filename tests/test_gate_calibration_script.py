"""scripts/gate_calibration.py（T22 + T39）：正负对照实验编排。

离线小样本：全部经注入的合成 K 线 + 常数成本替身运行（不碰真实数据根、
不连 MT5、不跑真实 G5）。CLI 端到端用 monkeypatch 掉装配函数验证
--quick 全流程与产物（JSON + Markdown）。

T39（2026-10-26-round5-hardening）新增覆盖：
- --null-hypothesis day|slot|auto|both（按卡持仓周期选日内/日块零假设）；
- 合并评估变体（多品种合计交易数 + 每品种为正，只出数据不改门槛）；
- G5 判别力（与锁箱等长合成窗口上的 G5 单侧判定镜像：检出/误报）。
"""
import dataclasses
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from mt5quant.validation import stats as vstats
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

    def overnight_fee(self, symbol, side, from_ts, to_ts, *, price=None):   # T82 形参透传
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
    # N-M1（第七轮复审）：校准记录完整写入——目标/容差/收敛标志/迭代历史
    assert out["target_sharpe"] == 1.0
    assert out["tol"] == 0.15
    assert isinstance(out["converged"], bool)
    assert out["converged"] == (abs(out["achieved_sharpe"] - 1.0) <= 0.15)
    assert all({"drift", "sharpe"} <= set(h) for h in out["history"])


def test_positive_control_structure_and_detection(gc, cfg, inputs):
    cal = gc.calibrate_drift(
        "TST", "H1", target_sharpe=1.5, period_days=1, interval_hours=24,
        hold_bars=4, cfg=cfg, seed=0, max_iter=6, tol=0.2,
        initial_capital=10_000.0, risk_fraction=0.005, **inputs)
    level_cal = {1.5: dict(cal)}          # N-M1：完整校准记录直传（不裁剪）
    setup = ("TST", "H1", level_cal, 1, 24, 4)
    kwargs = dict(levels=[1.5], seeds=[0, 1], cfg=cfg,
                  initial_capital=10_000.0, risk_fraction=0.005, **inputs)
    result = gc.positive_control([setup], **kwargs)
    det = result["detection_rates"]["TST H1"]["1.5"]
    assert det["G1.net_profit_positive"] >= 0.5    # 夏普 1.5 净利须为正
    assert det["G0"] == 1.0
    assert 0.0 <= det["G4.deflated_sharpe"] <= 1.0
    assert result["calibration"]["TST H1"]["1.5"]["drift"] == cal["drift"]
    # N-M1：报告承诺的 calibration.history 等完整记录逐字段进 JSON 产物
    recorded = result["calibration"]["TST H1"]["1.5"]
    assert recorded["history"] == cal["history"]
    assert recorded["target_sharpe"] == 1.5
    assert recorded["tol"] == 0.2
    assert recorded["converged"] == cal["converged"]
    assert recorded["iterations"] == len(cal["history"])
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
    # N-M1：CLI 产物里的 calibration 是完整校准记录（history/目标/容差/收敛）
    quick_cal = payload["positive"]["calibration"]["TST H1"]["1.0"]
    assert {"drift", "achieved_sharpe", "history", "target_sharpe", "tol",
            "converged", "iterations"} <= set(quick_cal)
    assert isinstance(quick_cal["converged"], bool)
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


# ------------------------------------------------- T39：零假设选择 / 合并评估

def test_negative_control_uses_selected_null_shuffler(gc, cfg, inputs,
                                                      monkeypatch):
    """--null-hypothesis：负对照按 null 选用打乱器（slot=槽位置换，
    day=日块置换），并把 null 记进每条 run。"""
    calls = {"day": 0, "slot": 0}

    def fake_day(bars, *, seed):
        calls["day"] += 1
        return bars.copy()

    def fake_slot(bars, *, seed):
        calls["slot"] += 1
        return bars.copy()

    monkeypatch.setattr(gc, "NULL_SHUFFLERS",
                        {"day": fake_day, "slot": fake_slot})
    result = gc.negative_control(
        [("RND", lambda seed: RandomEntries(seed=int(seed), hold_bars=4,
                                            event_every_bars=24), {}, {},
          "H1")],
        seeds=[0, 1], cfg=cfg, null="slot", initial_capital=10_000.0,
        risk_fraction=0.005, **inputs)
    assert calls == {"day": 0, "slot": 2}      # 每种子打乱一次（单品种）
    assert all(run["null"] == "slot" for run in result["runs"])
    again = gc.negative_control(
        [("RND", lambda seed: RandomEntries(seed=int(seed), hold_bars=4,
                                            event_every_bars=24), {}, {},
          "H1")],
        seeds=[0], cfg=cfg, null="day", initial_capital=10_000.0,
        risk_fraction=0.005, **inputs)
    assert calls == {"day": 1, "slot": 2}
    assert again["runs"][0]["null"] == "day"


def test_merged_evaluation_criterion(gc):
    """合并评估变体（蓝图 §6 提议，只出数据不改门槛）：交易数按全部品种
    合计过 G1 门槛，且要求每个品种净利为正。"""
    cfg = dataclasses.replace(_quick_cfg(), trade_count_min_hourly=100,
                              trade_count_min_intraday=300)
    per_symbol = {"A": {"trade_count": 60, "net_profit": 10.0},
                  "B": {"trade_count": 100, "net_profit": -5.0}}
    out = gc.merged_evaluation(per_symbol, "H1", cfg)
    assert out["merged_trade_count"] == 160
    assert out["threshold"] == 100
    assert out["each_positive"] is False
    assert out["merged_pass"] is False      # B 为负 → 不过
    ok = gc.merged_evaluation({"A": {"trade_count": 60, "net_profit": 10.0},
                               "B": {"trade_count": 100, "net_profit": 5.0}},
                              "H1", cfg)
    assert ok["merged_pass"] is True
    # 日内档门槛不同（300）
    out_m5 = gc.merged_evaluation({"A": {"trade_count": 200,
                                         "net_profit": 1.0}}, "M5", cfg)
    assert out_m5["threshold"] == 300 and out_m5["merged_pass"] is False


def test_negative_control_merged_multi_symbol(gc, cfg, monkeypatch):
    """多品种基线：负对照逐符号跑基准回测，合并评估行含各品种交易数与
    净利，merged_pass 与判据一致。"""
    two = {
        "bars_by_symbol": {"TST": random_walk_bars(seed=1),
                           "TST2": random_walk_bars(seed=2)},
        "costs_by_symbol": {"TST": ConstCosts(), "TST2": ConstCosts()},
        "sizes_by_symbol": {"TST": 1.0, "TST2": 1.0},
        "vols_by_symbol": {"TST": (0.01, 0.01, 100.0),
                           "TST2": (0.01, 0.01, 100.0)},
    }
    result = gc.negative_control(
        [("RND2", lambda seed: RandomEntries(seed=int(seed), hold_bars=4,
                                             event_every_bars=24), {}, {},
          "H1")],
        seeds=[0], cfg=cfg, null="slot", initial_capital=10_000.0,
        risk_fraction=0.005, **two)
    assert len(result["merged"]) == 1
    row = result["merged"][0]
    assert set(row["per_symbol"]) == {"TST", "TST2"}
    for sym, s in row["per_symbol"].items():
        assert s["trade_count"] >= 0 and isinstance(s["net_profit"], float)
    total = sum(s["trade_count"] for s in row["per_symbol"].values())
    each = all(s["net_profit"] > 0 for s in row["per_symbol"].values())
    assert row["merged_trade_count"] == total
    assert row["each_positive"] is each
    assert row["merged_pass"] == bool(total >= row["threshold"] and each)
    assert row["null"] == "slot" and row["seed"] == 0
    assert result["merged_pass_rates"]["RND2|slot"] == float(row["merged_pass"])


# ------------------------------------------------- T39：G5 判别力

def test_evaluate_g5_checks_mirrors_run_g5(gc, tmp_path):
    """实验镜像与正式 gates.run_g5 同输入同种子 → 判定与参照分位逐位一致
    （锁定镜像不漂移；gates.py 冻结不改，正式 G5 仍只在 bt --approve 跑）。"""
    from mt5quant.research.ledger import Ledger
    from mt5quant.validation import gates as vg

    cfg = dataclasses.replace(load_gates()[0], lockbox_bootstrap_sims=200)
    rng = np.random.default_rng(5)
    reference = pd.Series(7.0e-5 + rng.normal(0.0, 0.004, 30_000))
    lockbox = {"expectancy": 5.0, "sharpe": 2.0, "trade_count": 40,
               "bar_count": 11_250, "annualization_bars_per_year": 6_000.0}
    card_id = "H777"
    led = Ledger(tmp_path / "l.sqlite")
    rid = led.record_run(
        card_id=card_id, card_version=1, strategy="s:X", params={},
        symbol="XAUUSD", timeframe="H1", data_first="2024-01-01",
        data_last="2026-06-30", data_sha256="a" * 64, engine_version="A",
        cost_model_version="zero-v0", gates_hash="", metrics={},
        actor="test")
    led.record_gate(rid, "G3", True, {})

    class _Card:
        id = card_id
        timeframe = "H1"

    real = vg.run_g5(led, _Card(), lockbox_result=dict(lockbox),
                     reference_returns=reference, expected_trades=40.0,
                     approved_by="test", cfg=cfg, bars_per_day=24)
    mirror = gc.evaluate_g5_checks(
        dict(lockbox), reference, 40.0, cfg=cfg, bars_per_day=24,
        bars_per_year=6_000.0, seed=vstats.rng_seed(card_id))
    assert mirror["passed"] == real["passed"]
    # run_g5 的检查值是 {passed, 指标…}；镜像只留布尔——passed 逐项一致
    for name, value in real["checks"].items():
        assert mirror["checks"][name] is value["passed"]
    assert mirror["reference"]["p5"] == pytest.approx(real["reference"]["p5"])
    assert mirror["reference"]["p95"] == pytest.approx(real["reference"]["p95"])
    # 弱表现（夏普 0 / 期望为负 / 交易数不足）→ 各单项如实拒绝
    weak = gc.evaluate_g5_checks(
        {"expectancy": -1.0, "sharpe": 0.0, "trade_count": 5,
         "bar_count": 11_250, "annualization_bars_per_year": 6_000.0},
        reference, 40.0, cfg=cfg, bars_per_day=24, bars_per_year=6_000.0,
        seed=1)
    assert weak["checks"] == {"expectation_positive": False,
                              "trade_count": False,
                              "sharpe_vs_reference": False}
    assert weak["passed"] is False


def test_g5_power_structure_and_reproducible(gc, cfg, inputs):
    """G5 判别力实验：合成窗口（与锁箱等长口径）上，检出臂（植入优势）
    的 G5.passed 率高于误报臂（槽位置换），结构完整且逐位可复现。"""
    small_cfg = dataclasses.replace(cfg, lockbox_bootstrap_sims=100)
    cal = gc.calibrate_drift(
        "TST", "H1", target_sharpe=1.5, period_days=1, interval_hours=24,
        hold_bars=4, cfg=small_cfg, seed=0, max_iter=6, tol=0.2,
        initial_capital=10_000.0, risk_fraction=0.005, **inputs)
    setup = ("TST", "H1", {1.5: {"drift": cal["drift"],
                                 "achieved_sharpe": cal["achieved_sharpe"]}},
             1, 24, 4)
    kwargs = dict(levels=[1.5], seeds=[0, 1], cfg=small_cfg, nulls=("slot",),
                  window_bars=900, initial_capital=10_000.0,
                  risk_fraction=0.005, **inputs)
    result = gc.g5_power([setup], **kwargs)
    det = result["detection_rates"]["TST H1"]["1.5"]
    for key in ("G5.expectation_positive", "G5.trade_count",
                "G5.sharpe_vs_reference", "G5.passed"):
        assert 0.0 <= det[key] <= 1.0
    assert det["G5.expectation_positive"] >= 0.5    # 夏普 1.5 植入优势
    fp = result["false_positive_rates"]["slot"]
    assert 0.0 <= fp["G5.expectation_positive"] <= 1.0
    assert result["window_bars"]["TST H1"] == 900
    assert {r["arm"] for r in result["runs"]} >= {"detection", "false_positive"}
    again = gc.g5_power([setup], **kwargs)
    assert again["runs"] == result["runs"]           # 同参数逐位复现


def test_render_report_t39_sections(gc):
    """报告模板：旧版作废注明、按零假设分列的误报率、合并评估变体、
    G5 判别力（合成窗口口径）各节。"""
    results = {
        "meta": {"phase": "both", "seeds": 2, "levels": [1.0],
                 "g2_random_entry_sims": 3, "null_hypothesis": "both",
                 "null_defaults": {"H003-OpeningRange": "slot",
                                   "H002-Donchian": "day"}},
        "negative": {
            "runs": [], "merged": [{"name": "H002-Donchian", "seed": 0,
                                    "null": "slot",
                                    "merged_trade_count": 210,
                                    "threshold": 150,
                                    "each_positive": True,
                                    "merged_pass": True}],
            "merged_pass_rates": {"H002-Donchian|slot": 1.0,
                                  "H002-Donchian|day": 0.5},
            "by_null": {"day": {"G1.profit_factor": 0.10},
                        "slot": {"G1.profit_factor": 0.30}},
            "false_positive_rates": {"G1.profit_factor": 0.30}},
        "positive": {"detection_rates": {}, "calibration": {}},
        "g5": {"window_bars": {"XAUUSD H1": 4400},
               "detection_rates": {"XAUUSD H1": {
                   "1.0": {"G5.expectation_positive": 0.9,
                           "G5.trade_count": 1.0,
                           "G5.sharpe_vs_reference": 0.4,
                           "G5.passed": 0.4}}},
               "false_positive_rates": {"slot": {"G5.passed": 0.05},
                                        "day": {"G5.passed": 0.10}}},
        "flags": [],
    }
    md = gc.render_report(results)
    assert "作废" in md and "M4" in md                 # 旧版产物作废注明
    assert "slot" in md and "day" in md                # 零假设分列
    assert "合并评估" in md and "不改门槛" in md
    assert "G5" in md and "合成窗口" in md
    assert "G5.sharpe_vs_reference" in md


def test_cli_quick_both_nulls_and_g5(gc, inputs, monkeypatch, tmp_path):
    """--quick --null-hypothesis both 端到端：两种零假设各自出误报率，
    G5 判别力段落落盘，meta 记录模式与窗口长度。"""
    small = {**inputs,
             "bars_by_symbol": {"TST": inputs["bars_by_symbol"]["TST"]
                                .iloc[:1500]}}
    monkeypatch.setattr(gc, "load_gates_for_run", lambda: _quick_cfg())
    monkeypatch.setattr(
        gc, "BASELINES",
        ({"name": "RND-CLI", "null": "slot", "factory":
          lambda seed: RandomEntries(seed=int(seed), hold_bars=4,
                                     event_every_bars=24),
          "params": {}, "grid": {}, "symbols": ("TST",), "timeframe": "H1"},))
    monkeypatch.setattr(gc, "assemble_experiment",
                        lambda data_root, keys, max_bars: {
                            "TST H1": {
                                "bars": small["bars_by_symbol"]["TST"],
                                "costs": small["costs_by_symbol"]["TST"],
                                "sizes": small["sizes_by_symbol"]["TST"],
                                "vols": small["vols_by_symbol"]["TST"],
                                "timeframe": "H1", "session_policy": None},
                        })
    monkeypatch.setattr(gc, "load_lockbox_cutoff", lambda data_root: None)
    out_dir = tmp_path / "gates"
    rc = gc.main(["--phase", "both", "--quick", "--seeds", "2",
                  "--symbols", "TST:H1", "--null-hypothesis", "both",
                  "--g5-window-bars", "600", "--g5-bootstrap-sims", "50",
                  "--out", str(out_dir)])
    assert rc == 0
    payload = json.loads((out_dir / "gate-power.json").read_text(
        encoding="utf-8"))
    assert payload["meta"]["null_hypothesis"] == "both"
    assert payload["meta"]["g5_bootstrap_sims"] == 50
    negative = payload["negative"]
    assert set(negative["by_null"]) == {"day", "slot"}
    assert {r["null"] for r in negative["runs"]} == {"day", "slot"}
    g5 = payload["g5"]
    assert g5["window_bars"]["TST H1"] == 600
    assert set(g5["false_positive_rates"]) == {"day", "slot"}
    assert "detection_rates" in g5
    md = (out_dir / "gate-power.md").read_text(encoding="utf-8")
    assert "G5" in md and "合并评估" in md


# ---- 分片并行：--seeds-from 与合并器 ----

def test_seeds_from_offsets_experiment_seeds(gc, inputs, monkeypatch,
                                             tmp_path):
    """--seeds-from 7 → 负对照运行记录的 seed 全为 7（分片不相交的前提）。

    数据经 assemble_experiment 注入小样本（干净克隆无 data_research/，
    不依赖仓库本机数据）。
    """
    monkeypatch.setattr(
        gc, "BASELINES",
        ({"name": "H002", "factory": lambda seed: RandomEntries(seed=int(seed), hold_bars=4,
                                          event_every_bars=24),
          "params": {}, "grid": {}, "symbols": ("TST",), "timeframe": "H1"},))
    monkeypatch.setattr(
        gc, "assemble_experiment",
        lambda data_root, symbols, max_bars: {
            "TST H1": {"bars": inputs["bars_by_symbol"]["TST"],
                        "costs": inputs["costs_by_symbol"]["TST"],
                        "sizes": inputs["sizes_by_symbol"]["TST"],
                        "vols": inputs["vols_by_symbol"]["TST"],
                        "timeframe": "H1", "session_policy": None},
        })
    out = tmp_path / "shard7"
    rc = gc.main(["--phase", "negative", "--baselines", "H002",
                   "--symbols", "TST:H1",
                   "--null-hypothesis", "day", "--seeds", "1",
                   "--seeds-from", "7", "--max-bars", "4000",
                   "--out", str(out)])
    assert rc == 0
    d = json.loads((out / "gate-power.json").read_text(encoding="utf-8"))
    assert d["meta"]["seeds_from"] == 7
    assert d["negative"]["runs"]
    assert all(r["seed"] == 7 for r in d["negative"]["runs"])


def test_merge_shards_recomputes_rates_and_flags(tmp_path, monkeypatch):
    """两个合成分片（种子不相交）合并：比率按合并后样本重算、种子重叠拒收。
    M2/R6：脚本被复制到临时目录后，仓库根必须经 MT5QUANT_REPO 提供（不再
    有个人工作区绝对路径兜底）。"""
    import importlib.util as _ilu
    monkeypatch.setenv("MT5QUANT_REPO", str(SCRIPT_PATH.parents[1]))
    mp = tmp_path / "merge_script.py"
    mp.write_text((SCRIPT_PATH.parent / "merge_gate_power.py")
                  .read_text(encoding="utf-8"), encoding="utf-8")
    spec = _ilu.spec_from_file_location("merge_gate_power", mp)
    mm = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mm)

    def shard(seeds_from, seeds, pass_g1):
        runs = [{"name": "X", "seed": s, "null": "day",
                 "sharpe": 0.1, "net_profit": 1.0, "trade_count": 10,
                 "passed_all": pass_g1,
                 "checks": {"G1.net_profit_positive": pass_g1,
                            "G1.profit_factor": True}}
                for s in seeds]
        return {"meta": {"seeds": len(seeds), "seeds_from": seeds_from,
                         "baselines": ["X"], "null_hypothesis": "day",
                         "null_defaults": {"X": "day"}, "levels": [],
                         "positive_symbols": [], "phase": "negative",
                         "generated_utc": "2026-10-26T00:00:00+00:00",
                         # R8-I2：fingerprint 必填（夹具补齐；无指纹历史
                         # 产物走 --allow-legacy，另有专测）；R9-I6/T56 起
                         # cost_versions 还须非空 dict[str,str]——真实产物
                         # （data/reports/gates/shard8-*）恒为非空（逐序列
                         # 成本版本），夹具原用的 {} 与真实产物不符且会被
                         # 畸形指纹校验拒绝，故改为非空
                         "fingerprint": {"git_commit": "a" * 40,
                                         "git_dirty": False,
                                         "gates_sha256": "b" * 64,
                                         "cost_versions": {"TST H1": "const-v0"},
                                         "script_sha256": "c" * 64}},
                "negative": {"runs": runs, "by_null": {}, "merged": []},
                "flags": []}

    d1, d2 = tmp_path / "s1", tmp_path / "s2"
    d1.mkdir(); d2.mkdir()
    (d1 / "gate-power.json").write_text(json.dumps(
        shard(0, [0, 1], True)), encoding="utf-8")
    (d2 / "gate-power.json").write_text(json.dumps(
        shard(2, [2, 3], False)), encoding="utf-8")
    out = tmp_path / "merged"
    rc = mm.main(["--shards", f"{d1},{d2}", "--out", str(out)])
    assert rc == 0
    m = json.loads((out / "gate-power.json").read_text(encoding="utf-8"))
    assert len(m["negative"]["runs"]) == 4
    assert m["meta"]["seeds"] == 4
    # 合并后 2/4 过半 → 误报率 0.5（单分片各自为 1.0 / 0.0）
    assert m["negative"]["false_positive_rates"][
        "G1.net_profit_positive"] == pytest.approx(0.5)

    # 种子重叠 → 拒绝
    (d2 / "gate-power.json").write_text(json.dumps(
        shard(1, [1, 2], False)), encoding="utf-8")
    assert mm.main(["--shards", f"{d1},{d2}", "--out", str(out)]) == 1


# ---- R6（C8+M2）：合并器与真实产物 schema / G5 分桶 / flags 双源 ----

def _load_merge_module(tmp_path, monkeypatch, name="merge_gate_power_r6"):
    """把合并器复制到临时目录加载（模拟他机/CI：__file__ 不在仓库内，
    仓库根只能来自 MT5QUANT_REPO）。"""
    import importlib.util as _ilu
    monkeypatch.setenv("MT5QUANT_REPO", str(SCRIPT_PATH.parents[1]))
    mp = tmp_path / "merge_script.py"
    mp.write_text((SCRIPT_PATH.parent / "merge_gate_power.py")
                  .read_text(encoding="utf-8"), encoding="utf-8")
    spec = _ilu.spec_from_file_location(name, mp)
    mm = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mm)
    return mm


def _real_schema_shard(seeds, *, g5_det_pass, g5_fp_day, g5_fp_slot,
                       cal_drift=1.25):
    """按 gate_calibration 当前单进程产物的真实 schema 造一个**条件完整**的
    小分片（N-I4：合并器从实验设计生成应存在的完整条件清单——夹具本身必须
    满足设计，删改后才用来构造缺组/错配场景）：

    - negative.runs 带 null：baselines（H009-Sample，缺省 slot）× both 模式
      两种零假设 + 每个正对照品种的 RND@{key} × 两种零假设；
    - positive.runs 带 setup（detection 的分组键）× levels；
      calibration 是 {setup: {level: 完整校准记录}} **字典**（N-M1 起带
      history/target/tol/converged）；
    - g5.runs 带 setup/arm/null（arm="detection"|"false_positive"，
      误报臂 null 区分 day/slot 两种零假设）× levels × 全部臂。
    """

    def neg_run(name, null, s):
        return {"name": name, "seed": s, "null": null,
                "sharpe": 0.1, "net_profit": 1.0, "trade_count": 10,
                "passed_all": True,
                "checks": {"G0": True, "G1.profit_factor": True}}

    neg = [neg_run("H009-Sample", null, s)
           for null in ("slot", "day") for s in seeds]
    neg += [neg_run("RND@TST H1", null, s)
            for null in ("slot", "day") for s in seeds]
    pos = [{"setup": "TST H1", "level": 1.0, "seed": s, "drift": cal_drift,
            "sharpe": 1.0, "net_profit": 5.0, "trade_count": 40,
            "passed_all": True,
            "checks": {"G0": True, "G1.profit_factor": True}}
           for s in seeds]
    g5 = [{"setup": "TST H1", "arm": "detection", "null": None,
           "level": 1.0, "seed": s, "drift": cal_drift, "sharpe": 1.2,
           "expectancy": 1.0, "trade_count": 40, "p5": -1.3, "warned": False,
           "checks": {"G5.expectation_positive": True,
                      "G5.trade_count": True,
                      "G5.sharpe_vs_reference": g5_det_pass,
                      "G5.passed": g5_det_pass}}
          for s in seeds]
    g5 += [{"setup": "TST H1", "arm": "false_positive", "null": null,
            "level": 1.0, "seed": s, "drift": 0.0, "sharpe": 0.1,
            "expectancy": 0.0, "trade_count": 40, "p5": -1.3, "warned": False,
            "checks": {"G5.expectation_positive": passed,
                       "G5.trade_count": True,
                       "G5.sharpe_vs_reference": passed,
                       "G5.passed": passed}}
           for null, passed in (("day", g5_fp_day), ("slot", g5_fp_slot))
           for s in seeds]
    return {
        "meta": {"phase": "both", "seeds": len(seeds), "seeds_from": min(seeds),
                 "levels": [1.0], "g2_random_entry_sims": 50,
                 "null_hypothesis": "both",
                 "null_defaults": {"H009-Sample": "slot"},
                 "initial_capital": 100000.0,
                 "risk_per_trade": 0.005, "max_bars": None,
                 "baselines": ["H009-Sample"], "positive_symbols": ["TST H1"],
                 "g5_bootstrap_sims": 200, "generated_utc":
                     "2026-10-26T00:00:00+00:00",
                 # R8-I2 起 meta 身份键全集（含 g5_cutoff/g5_window_source/
                 # fingerprint）——夹具与真实产物同构，缺指纹场景在测试里
                 # 显式 pop 出来
                 "g5_cutoff": "2025-10-24 00:00:00",
                 "g5_window_source": "lockbox_cutoff",
                 "fingerprint": {"git_commit": "a" * 40, "git_dirty": False,
                                 "gates_sha256": "b" * 64,
                                 "cost_versions": {"TST H1": "v1"},
                                 "script_sha256": "c" * 64}},
        "negative": {"runs": neg,
                     "by_null": {"slot": {"G0": 1.0}, "day": {"G0": 1.0}},
                     "false_positive_rates": {"G0": 1.0},
                     "merged": [], "merged_pass_rates": {}},
        "positive": {
            "runs": pos,
            "detection_rates": {"TST H1": {"1.0": {
                "G0": 1.0, "G1.profit_factor": 1.0}}},
            "calibration": {"TST H1": {"1.0": {
                "drift": cal_drift, "achieved_sharpe": 1.02,
                "target_sharpe": 1.0, "tol": 0.15, "converged": True,
                "iterations": 3, "max_iter": 6,
                "history": [{"drift": 0.0, "sharpe": -0.1,
                             "trade_count": 30, "net_profit": -2.0},
                            {"drift": cal_drift, "sharpe": 1.02,
                             "trade_count": 40, "net_profit": 5.0}]}}},
        },
        "g5": {"runs": g5, "detection_rates": {}, "false_positive_rates": {},
               "window_bars": {"TST H1": 600}, "bootstrap_sims": 200,
               "design": "synthetic"},
        "flags": [],
    }


def test_merge_real_schema_g5_buckets_flags_calibration(tmp_path, monkeypatch):
    """R6/C8 失败测试（真实 schema）：
    - G5 runs 按 setup 分组（生成器写 setup，不写 key）→ 合并不再 KeyError；
    - G5 误报臂按 null（day/slot）分桶分别聚合（100% 与 0% 不得混成 50%）；
    - flags 同时遍历普通正对照与 G5 检出率（正对照 100% 检出时，G5 检出 0%
      仍须触发低检出率建议）；
    - calibration 保持 {setup: …} 字典（转列表会让 render_report 崩）。
    分片种子数故意不等（2 vs 3）：合并必须按拼接样本重算，而非平均分片比率。"""
    mm = _load_merge_module(tmp_path, monkeypatch)
    d1, d2 = tmp_path / "s1", tmp_path / "s2"
    d1.mkdir(); d2.mkdir()
    # 分片1：种子 0,1（G5 检出全挂；day 误报全过、slot 误报全挂）
    (d1 / "gate-power.json").write_text(json.dumps(_real_schema_shard(
        [0, 1], g5_det_pass=False, g5_fp_day=True, g5_fp_slot=False),
        ensure_ascii=False), encoding="utf-8")
    # 分片2：种子 2,3,4（同判定量，便于核对合并分母是 5 不是平均）
    (d2 / "gate-power.json").write_text(json.dumps(_real_schema_shard(
        [2, 3, 4], g5_det_pass=False, g5_fp_day=True, g5_fp_slot=False),
        ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "merged"
    rc = mm.main(["--shards", f"{d1},{d2}", "--out", str(out)])
    assert rc == 0
    m = json.loads((out / "gate-power.json").read_text(encoding="utf-8"))

    # G5 检出率：setup 键（不是 key），样本 = 两分片 5 个种子
    det = m["g5"]["detection_rates"]["TST H1"]["1.0"]
    assert det["G5.sharpe_vs_reference"] == pytest.approx(0.0)
    assert det["G5.expectation_positive"] == pytest.approx(1.0)
    # G5 误报率按 null 分桶：day=5/5=100%，slot=0/5=0%（不混桶平均成 50%）
    fp = m["g5"]["false_positive_rates"]
    assert fp["day"]["G5.expectation_positive"] == pytest.approx(1.0)
    assert fp["slot"]["G5.expectation_positive"] == pytest.approx(0.0)
    # flags 双源：正对照检出 100% 不挡 G5 检出 0% 的低检出率建议；
    # G5 day 误报 100% > 5% → 高误报建议（混桶平均 50% 同样会触发，但分桶
    # 断言已排除混桶；slot 桶 0% 不得拉低 day 桶的最差值）
    flagged = {(f["check"], f["kind"]) for f in m["flags"]}
    assert ("G5.sharpe_vs_reference", "low_detection") in flagged
    assert ("G5.expectation_positive", "high_false_positive") in flagged
    assert ("G5.passed", "low_detection") not in flagged   # 汇总数据不进规则
    # calibration 仍是字典（render_report 按 setup 取 .get）
    assert isinstance(m["positive"]["calibration"], dict)
    assert m["positive"]["calibration"]["TST H1"]["1.0"]["drift"] == 1.25
    # 报告渲染不炸（旧实现 calibration 转列表 → AttributeError）
    md = (out / "gate-power.md").read_text(encoding="utf-8")
    assert "G5" in md and "门槛调整建议" in md


def test_merge_rejects_actual_seed_defects(tmp_path, monkeypatch, capsys):
    """R6/C8 失败测试：种子校验除 meta 外还核 runs 里的实际种子——
    缺样（某条件少跑一个种子）/重复/越界都拒绝合并。"""
    mm = _load_merge_module(tmp_path, monkeypatch)

    def write(dirname, shard):
        d = tmp_path / dirname
        d.mkdir(exist_ok=True)
        (d / "gate-power.json").write_text(json.dumps(shard,
                                                      ensure_ascii=False),
                                           encoding="utf-8")
        return d

    good = write("good", _real_schema_shard([0, 1], g5_det_pass=True,
                                            g5_fp_day=False, g5_fp_slot=False))
    out = tmp_path / "merged"

    # 1) 缺样：meta 声明 2 个种子，negative 只跑了 seed 0
    missing = _real_schema_shard([2, 3], g5_det_pass=True, g5_fp_day=False,
                                 g5_fp_slot=False)
    missing["negative"]["runs"] = missing["negative"]["runs"][:1]
    d_miss = write("miss", missing)
    assert mm.main(["--shards", f"{good},{d_miss}", "--out", str(out)]) == 1
    assert "缺种子" in capsys.readouterr().out

    # 2) 重复：同一条件同一种子出现两次
    dup = _real_schema_shard([2, 3], g5_det_pass=True, g5_fp_day=False,
                             g5_fp_slot=False)
    dup["negative"]["runs"].append(dict(dup["negative"]["runs"][0]))
    d_dup = write("dup", dup)
    assert mm.main(["--shards", f"{good},{d_dup}", "--out", str(out)]) == 1
    assert "重复" in capsys.readouterr().out

    # 3) 越界：runs 里出现声明段之外的种子
    extra = _real_schema_shard([2, 3], g5_det_pass=True, g5_fp_day=False,
                               g5_fp_slot=False)
    extra["negative"]["runs"][1]["seed"] = 99
    d_extra = write("extra", extra)
    assert mm.main(["--shards", f"{good},{d_extra}", "--out", str(out)]) == 1
    assert "声明段外" in capsys.readouterr().out


def test_merge_rejects_whole_missing_condition_group(tmp_path, monkeypatch,
                                                     capsys):
    """N-I4（第七轮复审）失败测试：删掉某分片一整个实验条件组（正对照
    TST H1 × level=1.0 的全部记录）→ 拒绝合并并列出该条件。

    旧实现只遍历"已出现的组"：整组缺样时该组根本不进校验，种子校验通过、
    合并照常出产物（复现基线）。新实现从实验设计（meta）生成应存在的完整
    条件清单，缺任一条件组即拒绝。"""
    mm = _load_merge_module(tmp_path, monkeypatch, name="merge_gate_power_ni4")

    def write(dirname, shard):
        d = tmp_path / dirname
        d.mkdir(exist_ok=True)
        (d / "gate-power.json").write_text(json.dumps(shard, ensure_ascii=False),
                                           encoding="utf-8")
        return d

    good = write("good", _real_schema_shard([0, 1], g5_det_pass=True,
                                            g5_fp_day=False, g5_fp_slot=False))
    out = tmp_path / "merged"

    # 1) 整组缺样：分片 s1 删掉全部正对照记录（TST H1 × level=1.0）
    lost = _real_schema_shard([2, 3], g5_det_pass=True, g5_fp_day=False,
                              g5_fp_slot=False)
    lost["positive"]["runs"] = []
    d_lost = write("lost", lost)
    assert mm.main(["--shards", f"{good},{d_lost}", "--out", str(out)]) == 1
    text = capsys.readouterr().out
    assert "'positive'" in text and "TST H1" in text and "1.0" in text
    assert "整组" in text
    assert not (out / "gate-power.json").exists()

    # 2) 同理：G5 检出臂整组删除（null 条件组）也要拒绝并列出
    lost_g5 = _real_schema_shard([2, 3], g5_det_pass=True, g5_fp_day=False,
                                 g5_fp_slot=False)
    lost_g5["g5"]["runs"] = [r for r in lost_g5["g5"]["runs"]
                             if r["arm"] != "detection"]
    d_g5 = write("lost_g5", lost_g5)
    assert mm.main(["--shards", f"{good},{d_g5}", "--out", str(out)]) == 1
    text = capsys.readouterr().out
    assert "'g5'" in text and "detection" in text
    assert not (out / "gate-power.json").exists()


def test_merge_rejects_meta_identity_mismatch(tmp_path, monkeypatch, capsys):
    """N-I4（第七轮复审）失败测试：分片 meta 的实验身份——levels、零假设
    配置、资金/风险参数、G2/G5 模拟次数——不一致 → 拒绝合并（不同实验的
    runs 不能拼成同一实验的汇总；旧实现只比 baselines）。"""
    mm = _load_merge_module(tmp_path, monkeypatch, name="merge_gate_power_ni4b")

    def write(dirname, shard):
        d = tmp_path / dirname
        d.mkdir(exist_ok=True)
        (d / "gate-power.json").write_text(json.dumps(shard, ensure_ascii=False),
                                           encoding="utf-8")
        return d

    base = _real_schema_shard([0, 1], g5_det_pass=True, g5_fp_day=False,
                              g5_fp_slot=False)
    d1 = write("s1", base)
    out = tmp_path / "merged"

    for key, bad_value in (
            ("levels", [0.6, 1.0]),
            ("null_hypothesis", "auto"),
            ("initial_capital", 50000.0),
            ("risk_per_trade", 0.01),
            ("g2_random_entry_sims", 200),
            ("g5_bootstrap_sims", 2000),
            ("positive_symbols", ["TST H1", "TST2 M5"])):
        other = _real_schema_shard([2, 3], g5_det_pass=True, g5_fp_day=False,
                                   g5_fp_slot=False)
        other["meta"][key] = bad_value
        d2 = write("s2", other)
        assert mm.main(["--shards", f"{d1},{d2}", "--out", str(out)]) == 1, key
        assert key in capsys.readouterr().out
    # 一致的身份 + 不相交种子 → 仍可正常合并（对照组）
    d_ok = write("s2", _real_schema_shard([2, 3], g5_det_pass=True,
                                          g5_fp_day=False, g5_fp_slot=False))
    assert mm.main(["--shards", f"{d1},{d_ok}", "--out", str(out)]) == 0
    assert (out / "gate-power.json").is_file()


def test_merge_repo_root_requires_env_when_copied(tmp_path, monkeypatch):
    """R6/M2 失败测试：脚本脱离仓库（复制到临时目录）且未设 MT5QUANT_REPO
    → 明确报错（不再回退任何硬编码绝对路径）；指向没有 gate_calibration.py
    的目录同样报错。"""
    import importlib.util as _ilu

    mp = tmp_path / "merge_script.py"
    mp.write_text((SCRIPT_PATH.parent / "merge_gate_power.py")
                  .read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.delenv("MT5QUANT_REPO", raising=False)
    spec = _ilu.spec_from_file_location("merge_no_env", mp)
    mod = _ilu.module_from_spec(spec)
    with pytest.raises(RuntimeError, match="MT5QUANT_REPO"):
        spec.loader.exec_module(mod)

    monkeypatch.setenv("MT5QUANT_REPO", str(tmp_path))
    spec2 = _ilu.spec_from_file_location("merge_bad_env", mp)
    mod2 = _ilu.module_from_spec(spec2)
    with pytest.raises(RuntimeError, match="gate_calibration"):
        spec2.loader.exec_module(mod2)


def test_flags_traverse_positive_and_g5_sources(gc):
    """R6/C8 失败测试（gate_calibration 侧）：决策规则的检出率必须**同时**
    遍历普通正对照与 G5 检出率——存在正对照时 G5 检出为 0 也要能触发低检出
    率建议（主进程与合并器共用同一函数）；同名检查多 setup 取最小值。"""
    level_rates = gc.flags_level_rates(
        {"XAUUSD H1": {"1.0": {"G1.profit_factor": 0.9}},
         "USTEC M5": {"1.0": {"G1.profit_factor": 0.6}}},
        {"XAUUSD H1": {"1.0": {"G5.sharpe_vs_reference": 0.0,
                               "G5.expectation_positive": 0.8,
                               "G5.passed": 0.0}}})
    assert level_rates["G1.profit_factor"] == pytest.approx(0.6)   # 跨 setup 最小
    assert level_rates["G5.sharpe_vs_reference"] == 0.0            # G5 不被正对照挡掉
    assert level_rates["G5.expectation_positive"] == 0.8
    assert "G5.passed" not in level_rates                          # 汇总不进规则

    flags = gc.compute_flags({
        "positive": {"detection_rates": {
            "XAUUSD H1": {"1.0": {"G1.profit_factor": 1.0}}}},
        "g5": {"detection_rates": {"XAUUSD H1": {"1.0": {
            "G5.sharpe_vs_reference": 0.0}}},
            "false_positive_rates": {
                "day": {"G5.expectation_positive": 0.07},
                "slot": {"G5.expectation_positive": 0.0}}},
        "negative": {"false_positive_rates": {"G1.profit_factor": 0.01}},
    })
    flagged = {(f["check"], f["kind"]) for f in flags}
    # G5 检出 0% → 低检出率（正对照 100% 挡不住）
    assert ("G5.sharpe_vs_reference", "low_detection") in flagged
    # G5 误报按 null 取最差（day 7% > 5%，不被 slot 0% 稀释）
    assert ("G5.expectation_positive", "high_false_positive") in flagged
    # 普通误报 1% 不触发
    assert ("G1.profit_factor", "high_false_positive") not in flagged


def test_merge_rejects_fingerprint_mismatch(tmp_path, monkeypatch, capsys):
    """T42（R8 计划）失败测试：分片 meta.fingerprint（git 提交、gates 哈希、
    成本版本、脚本哈希）不一致 → 拒绝合并——不同代码/数据身份的运行不能
    拼成同一实验的汇总（R7-N-I1：产物必须自证运行身份）。"""
    mm = _load_merge_module(tmp_path, monkeypatch,
                            name="merge_gate_power_fp")

    def write(dirname, shard):
        d = tmp_path / dirname
        d.mkdir(exist_ok=True)
        (d / "gate-power.json").write_text(json.dumps(shard, ensure_ascii=False),
                                           encoding="utf-8")
        return d

    fp = {"git_commit": "a" * 40, "git_dirty": False,
          "gates_sha256": "b" * 64, "cost_versions": {"TST H1": "v1"},
          "script_sha256": "c" * 64}
    base = _real_schema_shard([0, 1], g5_det_pass=True, g5_fp_day=False,
                              g5_fp_slot=False)
    base["meta"]["fingerprint"] = dict(fp)
    d1 = write("s1", base)
    out = tmp_path / "merged"
    for key, bad_value in (
            ("git_commit", "d" * 40),
            ("gates_sha256", "e" * 64),
            ("cost_versions", {"TST H1": "v2"}),
            ("script_sha256", "f" * 64)):
        other = _real_schema_shard([2, 3], g5_det_pass=True, g5_fp_day=False,
                                   g5_fp_slot=False)
        other["meta"]["fingerprint"] = {**fp, key: bad_value}
        d2 = write("s2", other)
        assert mm.main(["--shards", f"{d1},{d2}", "--out", str(out)]) == 1, key
        assert "fingerprint" in capsys.readouterr().out
    # 相同指纹 + 不相交种子 → 正常合并（对照组）
    same = _real_schema_shard([2, 3], g5_det_pass=True, g5_fp_day=False,
                              g5_fp_slot=False)
    same["meta"]["fingerprint"] = dict(fp)
    d_ok = write("s2", same)
    assert mm.main(["--shards", f"{d1},{d_ok}", "--out", str(out)]) == 0
    assert (out / "gate-power.json").is_file()


def test_merge_rejects_missing_whole_arm(tmp_path, monkeypatch, capsys):
    """R8-I2（P14 反例）失败测试：删除某分片**整个实验臂**（g5/negative/
    positive 区块）→ 拒绝合并。

    旧实现从"产物有什么区块"反推应有条件——区块整个删掉后该臂从校验里
    消失：P14 在正式五分片副本中删除 shard8-0 的整个 g5 区块，合并仍
    exit 0、meta.seeds=20、G5 只剩 384 条 / 16 个种子。新实现从实验设计
    （meta.phase + G5 配置键）推导应有臂：phase=both 时 negative/positive
    必须存在；phase 含 positive 且 meta 带 G5 配置时 g5 必须存在。"""
    mm = _load_merge_module(tmp_path, monkeypatch, name="merge_gate_power_r8i2")

    def write(dirname, shard):
        d = tmp_path / dirname
        d.mkdir(exist_ok=True)
        (d / "gate-power.json").write_text(json.dumps(shard, ensure_ascii=False),
                                           encoding="utf-8")
        return d

    good = write("good", _real_schema_shard([0, 1], g5_det_pass=True,
                                            g5_fp_day=False, g5_fp_slot=False))
    out = tmp_path / "merged"

    # 1) 整臂删除：g5 / negative / positive 任一区块整体消失 → 拒绝
    for arm in ("g5", "negative", "positive"):
        shard = _real_schema_shard([2, 3], g5_det_pass=True, g5_fp_day=False,
                                   g5_fp_slot=False)
        shard.pop(arm)                 # P14：整臂（整个区块）缺样
        d = write(f"lost_{arm}", shard)
        assert mm.main(["--shards", f"{good},{d}", "--out", str(out)]) == 1, arm
        text = capsys.readouterr().out
        assert "整臂" in text and arm in text, arm
        assert not (out / "gate-power.json").exists(), arm

    # 2) 两分片一致的未知 phase → 无法从设计推导应有臂，拒绝（不静默放行）
    bogus_a = _real_schema_shard([0, 1], g5_det_pass=True, g5_fp_day=False,
                                 g5_fp_slot=False)
    bogus_a["meta"]["phase"] = "everything"
    bogus_b = _real_schema_shard([2, 3], g5_det_pass=True, g5_fp_day=False,
                                 g5_fp_slot=False)
    bogus_b["meta"]["phase"] = "everything"
    d_bogus = write("bogus_phase", bogus_b)
    assert mm.main(["--shards", f"{write('good2', bogus_a)},{d_bogus}",
                    "--out", str(out)]) == 1
    assert "phase" in capsys.readouterr().out

    # 3) 对照组：设计完整（phase=both 三臂齐全）仍正常合并
    d_ok = write("ok", _real_schema_shard([2, 3], g5_det_pass=True,
                                          g5_fp_day=False, g5_fp_slot=False))
    assert mm.main(["--shards", f"{good},{d_ok}", "--out", str(out)]) == 0
    assert (out / "gate-power.json").is_file()


def test_merge_rejects_max_bars_and_g5_window_identity(tmp_path, monkeypatch,
                                                       capsys):
    """R8-I2（P14 反例）失败测试：实验身份键补全——max_bars（输入截断）、
    g5_cutoff（G5 截断日期）、g5_window_source（G5 窗口来源）任一分片
    不一致 → 拒绝合并。P14 改这些字段时旧实现均无身份错误（不同窗口/
    截断/数据范围的 runs 不能拼成同一实验的汇总）。"""
    mm = _load_merge_module(tmp_path, monkeypatch, name="merge_gate_power_r8i2b")

    def write(dirname, shard):
        d = tmp_path / dirname
        d.mkdir(exist_ok=True)
        (d / "gate-power.json").write_text(json.dumps(shard, ensure_ascii=False),
                                           encoding="utf-8")
        return d

    d1 = write("s1", _real_schema_shard([0, 1], g5_det_pass=True,
                                        g5_fp_day=False, g5_fp_slot=False))
    out = tmp_path / "merged"
    for key, bad_value in (
            ("max_bars", 20000),
            ("g5_cutoff", "2024-10-24 00:00:00"),
            ("g5_window_source", "override")):
        other = _real_schema_shard([2, 3], g5_det_pass=True, g5_fp_day=False,
                                   g5_fp_slot=False)
        other["meta"][key] = bad_value
        d2 = write("s2", other)
        assert mm.main(["--shards", f"{d1},{d2}", "--out", str(out)]) == 1, key
        assert key in capsys.readouterr().out, key
        assert not (out / "gate-power.json").exists(), key


def test_merge_fingerprint_required_unless_allow_legacy(tmp_path, monkeypatch,
                                                        capsys):
    """R8-I2（P14 反例）失败测试：fingerprint 必填——**全部**（而非部分）
    分片缺指纹 → 拒绝合并（旧"相等"校验对 None==None 恒过，P14 全删
    fingerprint 无任何错误）；显式 --allow-legacy 才放行，产物 meta 记
    legacy_no_fingerprint=true 降级标记。部分缺部分有属身份不一致，
    --allow-legacy 不豁免。"""
    mm = _load_merge_module(tmp_path, monkeypatch,
                            name="merge_gate_power_r8i2c")

    def write(dirname, shard):
        d = tmp_path / dirname
        d.mkdir(exist_ok=True)
        (d / "gate-power.json").write_text(json.dumps(shard, ensure_ascii=False),
                                           encoding="utf-8")
        return d

    legacy1 = _real_schema_shard([0, 1], g5_det_pass=True, g5_fp_day=False,
                                 g5_fp_slot=False)
    legacy1["meta"].pop("fingerprint")
    legacy2 = _real_schema_shard([2, 3], g5_det_pass=True, g5_fp_day=False,
                                 g5_fp_slot=False)
    legacy2["meta"].pop("fingerprint")
    d1, d2 = write("legacy1", legacy1), write("legacy2", legacy2)
    out = tmp_path / "merged"

    # 1) 全部缺指纹 → 拒绝，并指明 --allow-legacy 逃生口
    assert mm.main(["--shards", f"{d1},{d2}", "--out", str(out)]) == 1
    text = capsys.readouterr().out
    assert "fingerprint" in text and "--allow-legacy" in text
    assert not (out / "gate-power.json").exists()

    # 2) 显式降级：--allow-legacy 放行，meta 记降级标记（诚实呈现证据等级）
    assert mm.main(["--shards", f"{d1},{d2}", "--out", str(out),
                    "--allow-legacy"]) == 0
    m = json.loads((out / "gate-power.json").read_text(encoding="utf-8"))
    assert m["meta"]["legacy_no_fingerprint"] is True
    assert "fingerprint" not in m["meta"]

    # 3) 部分缺部分有：身份不一致，--allow-legacy 不豁免（绝不混合证据等级）
    mixed = write("mixed", _real_schema_shard([4, 5], g5_det_pass=True,
                                              g5_fp_day=False,
                                              g5_fp_slot=False))
    assert mm.main(["--shards", f"{d1},{mixed}", "--out", str(out),
                    "--allow-legacy"]) == 1
    assert "fingerprint" in capsys.readouterr().out


def test_merge_rejects_malformed_fingerprint_beyond_legacy(tmp_path, monkeypatch,
                                                           capsys):
    """R9-I6（T56）失败测试：fingerprint 键存在但畸形——null/{}/缺字段/
    类型错/格式错——拒绝合并，且 --allow-legacy 不豁免（legacy 仅适用于
    键**整体不存在**的历史无指纹产物；键在而不合法是伪造/截断/损坏，不是
    历史遗留，不得借用降级通道）。

    审核探针（docs/reviews/probes/2026-10-27-round9-audit.py
    fingerprint_edges）复现：对正式五分片副本把 meta.fingerprint 全部置
    null 或 {} 时旧实现 exit 0 且无降级标记——"分片间相等"对 None==None
    与 {}=={} 恒过，必填校验形同虚设。

    合法边界：git_commit **空串合法**（gate_calibration._run_fingerprint
    文档化的"git 不可用"降级语义，产物如实展示）；非空必须 40 位十六进制。"""
    mm = _load_merge_module(tmp_path, monkeypatch,
                            name="merge_gate_power_r9i6")

    def write(dirname, shard):
        d = tmp_path / dirname
        d.mkdir(exist_ok=True)
        (d / "gate-power.json").write_text(json.dumps(shard, ensure_ascii=False),
                                           encoding="utf-8")
        return d

    def shard(seeds):
        return _real_schema_shard(seeds, g5_det_pass=True, g5_fp_day=False,
                                  g5_fp_slot=False)

    good_fp = {"git_commit": "a" * 40, "git_dirty": False,
               "gates_sha256": "b" * 64, "cost_versions": {"TST H1": "v1"},
               "script_sha256": "c" * 64}
    out = tmp_path / "merged"

    # 1) 探针场景：全部（而非单个）分片同值畸形——分片间相等比较恒过，
    #    只有逐分片结构校验能拦；--allow-legacy 同样不得放行
    for label, bad in (("null", None), ("empty_object", {})):
        s1, s2 = shard([0, 1]), shard([2, 3])
        s1["meta"]["fingerprint"] = bad
        s2["meta"]["fingerprint"] = bad
        d1, d2 = write(f"all_{label}_1", s1), write(f"all_{label}_2", s2)
        assert mm.main(["--shards", f"{d1},{d2}", "--out", str(out)]) == 1, label
        assert mm.main(["--shards", f"{d1},{d2}", "--out", str(out),
                        "--allow-legacy"]) == 1, label
        assert "fingerprint" in capsys.readouterr().out, label
        assert not (out / "gate-power.json").exists(), label

    # 2) 单分片畸形（另一分片指纹完好）：缺字段/类型错/格式错全维度
    d_good = write("good", shard([0, 1]))
    malformed = [
        (f"missing_{field}",
         {k: v for k, v in good_fp.items() if k != field})
        for field in ("git_commit", "git_dirty", "gates_sha256",
                      "script_sha256", "cost_versions")]
    malformed += [
        ("git_dirty_str", {**good_fp, "git_dirty": "yes"}),
        ("git_commit_short", {**good_fp, "git_commit": "deadbeef"}),
        ("git_commit_not_str",
         {**good_fp, "git_commit": 1234567890123456789012345678901234567890}),
        ("gates_sha256_short", {**good_fp, "gates_sha256": "b" * 63}),
        ("script_sha256_short", {**good_fp, "script_sha256": "c" * 63}),
        ("cost_versions_empty", {**good_fp, "cost_versions": {}}),
        ("cost_versions_not_str", {**good_fp, "cost_versions": {"TST H1": 1}}),
        ("fingerprint_list", [good_fp]),
    ]
    for label, bad_fp in malformed:
        other = shard([2, 3])
        other["meta"]["fingerprint"] = bad_fp
        d2 = write(f"bad_{label}", other)
        assert mm.main(["--shards", f"{d_good},{d2}", "--out", str(out)]) == 1, label
        assert mm.main(["--shards", f"{d_good},{d2}", "--out", str(out),
                        "--allow-legacy"]) == 1, label
        assert "fingerprint" in capsys.readouterr().out, label
        assert not (out / "gate-power.json").exists(), label

    # 3) 对照组合法边界：git_commit 空串（git 不可用降级）+ 其余字段完好
    #    → 正常合并，产物指纹原样保留空串
    s1, s2 = shard([0, 1]), shard([2, 3])
    s1["meta"]["fingerprint"] = {**good_fp, "git_commit": ""}
    s2["meta"]["fingerprint"] = {**good_fp, "git_commit": ""}
    d1, d2 = write("nogit_1", s1), write("nogit_2", s2)
    assert mm.main(["--shards", f"{d1},{d2}", "--out", str(out)]) == 0
    m = json.loads((out / "gate-power.json").read_text(encoding="utf-8"))
    assert m["meta"]["fingerprint"]["git_commit"] == ""
    assert m["meta"]["fingerprint"]["cost_versions"] == {"TST H1": "v1"}


def test_merge_annotates_structural_flag_lines(tmp_path, monkeypatch):
    """R8-I7（渲染部分）失败测试：合并产物 md 的"门槛调整建议"节中，属
    结构性检查的条目（G1.trade_count——"按预期频率交易"的运维口径检查，
    负对照高通过属预期）行内标注"结构性检查——样本数性质，不构成门槛建议
    依据"；JSON 原始 flags 不动；gate_calibration.py 字节不动（其哈希在
    分片运行指纹里）——标注只发生在合并器渲染层。"""
    mm = _load_merge_module(tmp_path, monkeypatch,
                            name="merge_gate_power_r8i7")

    def write(dirname, shard):
        d = tmp_path / dirname
        d.mkdir(exist_ok=True)
        (d / "gate-power.json").write_text(json.dumps(shard, ensure_ascii=False),
                                           encoding="utf-8")
        return d

    def shard_with_trade_count_flag(seeds, *, day_fp):
        s = _real_schema_shard(seeds, g5_det_pass=True, g5_fp_day=day_fp,
                               g5_fp_slot=False)
        for run in s["negative"]["runs"]:    # 结构性检查在负对照上通过属预期
            run["checks"]["G1.trade_count"] = True
        return s

    d1 = write("s1", shard_with_trade_count_flag([0, 1], day_fp=True))
    d2 = write("s2", shard_with_trade_count_flag([2, 3], day_fp=False))
    out = tmp_path / "merged"
    assert mm.main(["--shards", f"{d1},{d2}", "--out", str(out)]) == 0

    m = json.loads((out / "gate-power.json").read_text(encoding="utf-8"))
    flagged = {(f["check"], f["kind"]) for f in m["flags"]}
    assert ("G1.trade_count", "high_false_positive") in flagged
    assert ("G5.expectation_positive", "high_false_positive") in flagged
    # JSON 原始 flags 保持原样（无任何标注/降级字段混入）
    for f in m["flags"]:
        assert set(f) == {"check", "kind", "value", "rule"}

    md = (out / "gate-power.md").read_text(encoding="utf-8")
    note = "结构性检查——样本数性质，不构成门槛建议依据"
    tc_line = next(line for line in md.splitlines()
                   if line.startswith("- **G1.trade_count**："))
    assert note in tc_line
    # 非结构性条目不加标注（只标结构性检查，不整节打折）
    other = next(line for line in md.splitlines()
                 if line.startswith("- **G5.expectation_positive**："))
    assert note not in other


def test_meta_records_run_fingerprint(gc, inputs, monkeypatch, tmp_path):
    """T42（R8 计划）失败测试：产物 meta 带运行指纹——git 提交、工作区
    干净位、冻结门槛哈希、各序列成本版本（含缓存/越幅/隔夜费 sha）、
    校准脚本自身哈希；报告 Markdown 引用运行指纹节。R7-N-I1 的"产物
    未记录运行身份"由此关闭。"""
    small = {**inputs,
             "bars_by_symbol": {"TST": inputs["bars_by_symbol"]["TST"]
                                .iloc[:1200]}}
    monkeypatch.setattr(gc, "load_gates_for_run", lambda: _quick_cfg())
    monkeypatch.setattr(
        gc, "BASELINES",
        ({"name": "RND-FP", "factory":
          lambda seed: RandomEntries(seed=int(seed), hold_bars=4,
                                     event_every_bars=24),
          "params": {}, "grid": {}, "symbols": ("TST",), "timeframe": "H1"},))
    monkeypatch.setattr(
        gc, "assemble_experiment",
        lambda data_root, symbols, max_bars: {
            "TST H1": {
                "bars": small["bars_by_symbol"]["TST"],
                "costs": small["costs_by_symbol"]["TST"],
                "cost_version": "rel@NY/bar_open_m1#01888819+ov[eca09e24]",
                "sizes": small["sizes_by_symbol"]["TST"],
                "vols": small["vols_by_symbol"]["TST"],
                "timeframe": "H1", "session_policy": None},
        })
    out_dir = tmp_path / "gates-fp"
    rc = gc.main(["--phase", "both", "--quick", "--seeds", "2",
                  "--symbols", "TST:H1", "--out", str(out_dir)])
    assert rc == 0
    payload = json.loads((out_dir / "gate-power.json").read_text(
        encoding="utf-8"))
    fp = payload["meta"]["fingerprint"]
    # git 提交：仓库内运行（本地与 CI 都是 git checkout）→ 40 位十六进制
    assert len(fp["git_commit"]) == 40 and \
        all(c in "0123456789abcdef" for c in fp["git_commit"])
    assert isinstance(fp["git_dirty"], bool)
    # 门槛哈希与冻结文件同源；脚本哈希为本文件字节
    assert fp["gates_sha256"] == load_gates()[1]
    assert len(fp["script_sha256"]) == 64
    import hashlib
    from pathlib import Path as _P
    script = _P(gc.__file__).resolve()
    assert fp["script_sha256"] == hashlib.sha256(
        script.read_bytes()).hexdigest()
    # 成本版本按序列记录（monkeypatch 注入的版本字符串原样入指纹）
    assert fp["cost_versions"] == {
        "TST H1": "rel@NY/bar_open_m1#01888819+ov[eca09e24]"}
    md = (out_dir / "gate-power.md").read_text(encoding="utf-8")
    assert "运行指纹" in md and fp["git_commit"][:12] in md


def test_merge_md_includes_r7_supplement_sections(tmp_path, monkeypatch):
    """T42（R7 解释限定 a/c/f/i 正式化）失败测试：合并产物 md 必须直接
    呈现——①完整检查组合通过率（正对照 passed_all 按品种×档位、负对照
    按零假设；正对照也可能全被拒，只看分检查表会漏掉这一事实）；
    ②校准逐种子夏普分布（n/均值/中位/极值——seed=0 单点不代表档位强度）；
    ③分品种 G5 误报率（品种间异质性显著，跨品种汇总会掩盖）。"""
    mm = _load_merge_module(tmp_path, monkeypatch,
                            name="merge_gate_power_supp")

    def write(dirname, shard):
        d = tmp_path / dirname
        d.mkdir(exist_ok=True)
        (d / "gate-power.json").write_text(json.dumps(shard, ensure_ascii=False),
                                           encoding="utf-8")
        return d

    d1 = write("s1", _real_schema_shard([0, 1], g5_det_pass=True,
                                        g5_fp_day=True, g5_fp_slot=False))
    d2 = write("s2", _real_schema_shard([2, 3], g5_det_pass=True,
                                        g5_fp_day=False, g5_fp_slot=False))
    out = tmp_path / "merged-supp"
    assert mm.main(["--shards", f"{d1},{d2}", "--out", str(out)]) == 0
    md = (out / "gate-power.md").read_text(encoding="utf-8")
    # ① 完整组合通过率：正对照（品种×档位）与负对照（零假设）分列
    assert "完整组合通过率" in md
    assert "正对照 passed_all" in md and "负对照 passed_all" in md
    # ② 校准分布：n/均值/中位/极值按品种×档位
    assert "校准逐种子分布" in md and "中位" in md
    # ③ 分品种 G5 误报率
    assert "分品种 G5 误报率" in md
    # 分布行至少给出 TST H1 档位 1.0 的 n=4（两分片各 2 种子合并）
    assert "n=4" in md


# ================================================ T65/D34：关卡 v3 对照臂

def _v3_cfg(gc):
    """v3 配置的小样本替身（窗口/自助次数缩到测试可跑；正式标定用仓库
    config/gates.toml 原值——D34 冻结后即 v3 内容，原 gates-v3.toml 已
    替换退役）。"""
    c, _ = gc.vgate.load_gates(suite="v3")
    return dataclasses.replace(c, train_months_hourly=12, test_months_hourly=3,
                               train_months_intraday=12,
                               test_months_intraday=3,
                               bootstrap_sims=200)


def test_v3_gate_suite_structure_and_layers(gc):
    """T65：run_v3_gate_suite 产出 v3 层级检查（否决/诊断分列），
    evaluate_v3 判定 = 否决层合取；诊断失败不翻转联合结论。"""
    cfg = _v3_cfg(gc)
    bars = random_walk_bars(n_days=900, bars_per_day=8)   # 30 个月 → 6 段 WF
    strat = gc.LongPlantedEdgeStrategy(period_days=1, interval_hours=24)
    out = gc.run_v3_gate_suite(
        strat, {"hold_bars": 4}, timeframe="H1", bars=bars,
        costs=ConstCosts(), contract_size=1.0, vols=(0.01, 0.01, 100.0),
        cfg=cfg, seed=7, grid={"hold_bars": [4, 8]})
    assert isinstance(out["verdict"]["passed"], bool)
    for key in gc.V3_VETO_CHECKS:
        assert key in out["checks"], key
    for key in ("primary.dsr", "primary.year_block", "primary.year_block@N40"):
        assert key in out["checks"], key
    for key in ("diag.param_plateau", "diag.yearly_segments",
                "diag.drop_best_5pct", "diag.g1.trade_count"):
        assert key in out["checks"], key
    # OOS 收益带时间索引（年块自助按年分块）；walk-forward 段数 > 1
    assert isinstance(out["oos_returns"].index, pd.DatetimeIndex)
    assert out["wf_segments"] >= 2
    # 主检验两方案 + Bonferroni 敏感性判定与明细都在
    assert "probability" in out["primary"]["dsr"]
    assert "p_value" in out["primary"]["year_block"]
    assert out["primary"]["year_block@N40"]["bonferroni_n"] \
        == gc.V3_BONFERRONI_SENSITIVITY_N
    # 诊断失败不改结论：手工把全部诊断置 False 重判，passed 不变
    flipped = dict(out["checks"])
    for k in flipped:
        if k.startswith("diag."):
            flipped[k] = False
    veto = {k: out["checks"][k] for k in
            ("G0", "veto.cost_x15", "veto.delay_1_bar", "veto.buyhold_excess")}
    again = gc.vgate.evaluate_v3(
        g0={"passed": veto["G0"]}, primary={"passed": flipped["primary.dsr"]},
        cost_scale={"passed": veto["veto.cost_x15"]},
        delay={"passed": veto["veto.delay_1_bar"]},
        buyhold={"passed": veto["veto.buyhold_excess"]},
        diagnostics={k: {"passed": v} for k, v in flipped.items()
                     if k.startswith("diag.")})
    assert again["passed"] == out["verdict"]["passed"]


def test_v3_gate_suite_cost_veto_binds(gc):
    """成本 ×1.5 否决真实起作用：成本大到吃掉全部毛利 → veto.cost_x15
    False、联合不通过（换 zero 成本同一策略通过与否决无关）。"""
    cfg = _v3_cfg(gc)
    bars = random_walk_bars(n_days=300, bars_per_day=8, seed=9)
    strat = gc.LongPlantedEdgeStrategy(period_days=1, interval_hours=24)
    kw = dict(timeframe="H1", cfg=cfg, seed=7, grid={"hold_bars": [4, 8]},
              contract_size=1.0, vols=(0.01, 0.01, 100.0))
    huge = gc.run_v3_gate_suite(
        strat, {"hold_bars": 4}, bars=bars, costs=ConstCosts(spread=500.0),
        **kw)
    assert huge["checks"]["veto.cost_x15"] is False
    assert huge["verdict"]["passed"] is False
    # failed_vetoes 用关卡层名（cost_scale_x15），与扁平检查键
    # veto.cost_x15 同指一项
    assert "cost_scale_x15" in huge["verdict"]["failed_vetoes"]


def test_v3_joint_pass_recomputed_from_checks(gc):
    """T65：联合通过率由逐条记录的检查布尔重算——joint.* 绝不是运行时
    自报字段（合并器同函数重算，见 merge 专测）。"""
    def rec(dsr, yb, cost, delay):
        return {"checks": {"G0": True, "primary.dsr": dsr,
                           "primary.year_block": yb,
                           "primary.year_block@N40": yb,
                           "veto.cost_x15": cost, "veto.delay_1_bar": delay,
                           "veto.buyhold_excess": True,
                           # T85 N 网格键（联合重算要求逐条在场；本测试只
                           # 断言现行键的率，新键布尔取与 primary.dsr 同值）
                           **{f"primary.dsr_v0@N{n}": dsr
                              for n in gc.V3_N_GRID},
                           **{f"primary.psr0_bonf@N{n}": dsr
                              for n in gc.V3_N_GRID}}}
    runs = [rec(True, True, True, True), rec(True, False, True, True),
            rec(False, True, True, False), rec(True, True, False, True)]
    rates = gc.v3_joint_rates(runs)
    assert rates["joint.dsr"] == pytest.approx(0.5)   # 第 3/4 条否决失败
    assert rates["joint.year_block"] == pytest.approx(0.25)  # 第 2/3/4 条
    assert rates["primary.dsr"] == pytest.approx(0.75)
    # 记录里若混入伪造的 joint 自报字段 → 不参与重算（被忽略）
    tampered = [dict(r, checks={**r["checks"], "joint.dsr": True})
                for r in runs]
    assert gc.v3_joint_rates(tampered)["joint.dsr"] == pytest.approx(0.5)


def test_merge_v3_recomputes_joint_from_records(tmp_path, monkeypatch):
    """T65 失败测试 4：v3 联合通过率在合并器里由逐条记录重算——分片只存
    逐检查布尔；分片自报的 joint 字段被忽略，合并产物 joint.* 对拼接后
    的 runs 重算（种子里换一条否决失败，联合率随之变）。"""
    mm = _load_merge_module(tmp_path, monkeypatch, name="merge_v3")

    def checks(dsr=True, yb=True, cost=True):
        return {"G0": True, "primary.dsr": dsr, "primary.year_block": yb,
                "primary.year_block@N40": yb, "veto.cost_x15": cost,
                "veto.delay_1_bar": True, "veto.buyhold_excess": True,
                "diag.param_plateau": True,
                # T85 N 网格键（新记录模式；布尔随 dsr——本测试断言现行键）
                **{f"primary.dsr_v0@N{n}": dsr for n in (2, 6, 24)},
                **{f"primary.psr0_bonf@N{n}": dsr for n in (2, 6, 24)}}

    def shard(dirname, seeds_from, seeds, joint_lied, cost_last):
        runs = [{"setup": "TST H1", "null": "day", "seed": s,
                 "checks": checks(), "joint": {"dsr": joint_lied}}
                for s in seeds[:-1]]
        runs.append({"setup": "TST H1", "null": "day", "seed": seeds[-1],
                     "checks": checks(cost=cost_last), "joint": {}})
        pos = [{"setup": "TST H1", "level": 1.0, "seed": s,
                "drift": 0.001, "sharpe": 1.0,
                "checks": checks(dsr=s % 2 == 0, yb=True)}
               for s in seeds]
        d = tmp_path / dirname
        d.mkdir()
        (d / "gate-power.json").write_text(json.dumps({
            "meta": {"phase": "v3", "seeds": len(seeds),
                     "seeds_from": seeds_from, "levels": [1.0],
                     "setups": ["TST H1"], "nulls": ["day"],
                     "primary_schemes": ["dsr", "year_block"],
                     "n_trials": 1,
                     "bonferroni_sensitivity_n": 40,
                     "fingerprint": {"git_commit": "a" * 40,
                                     "git_dirty": False,
                                     "gates_sha256": "b" * 64,
                                     "cost_versions": {"TST H1": "const-v0"},
                                     "script_sha256": "c" * 64}},
            "negative": {"runs": runs},
            "positive": {"runs": pos, "calibration": {
                "TST H1": {"TST": {"1.0": {
                    "drift": 0.001, "achieved_sharpe": 1.0}}}}},
        }, ensure_ascii=False), encoding="utf-8")
        return d

    d1 = shard("v3s1", 0, [0, 1], joint_lied=True, cost_last=True)
    d2 = shard("v3s2", 2, [2, 3], joint_lied=True, cost_last=False)
    out = tmp_path / "merged-v3"
    assert mm.main(["--shards", f"{d1},{d2}", "--out", str(out)]) == 0
    m = json.loads((out / "gate-power-v3.json").read_text(encoding="utf-8"))
    neg = m["negative"]["joint_false_positive_rates"]["TST H1|day"]
    # 4 条负对照里恰 1 条 veto.cost_x15=False → 联合误报 3/4（自报全 True
    # 被忽略）；主检验甲单检 4/4
    assert neg["joint.dsr"] == pytest.approx(0.75)
    assert neg["primary.dsr"] == pytest.approx(1.0)
    assert neg["veto.cost_x15"] == pytest.approx(0.75)
    pos = m["positive"]["joint_detection_rates"]["TST H1"]["1.0"]
    # 正对照：dsr 过 2/4（种子 0、2）→ joint.dsr 2/4
    assert pos["joint.dsr"] == pytest.approx(0.5)
    assert pos["primary.dsr"] == pytest.approx(0.5)
    assert pos["joint.year_block"] == pytest.approx(1.0)
    assert m["meta"]["seeds"] == 4 and m["meta"]["merged_from_shards"] is True
    md = (out / "gate-power-v3.md").read_text(encoding="utf-8")
    assert "联合" in md and "负对照" in md and "正对照" in md
    # 分片自报 joint 字段不应渗进合并产物
    assert all("joint" not in r for r in m["negative"]["runs"])


def test_cli_v3_quick_end_to_end(gc, monkeypatch, tmp_path):
    """--phase v3 --quick 端到端：装配注入小样本 → gate-power-v3.json/md
    落盘、退出码 0、meta 带两方案与联合口径。"""
    bars = random_walk_bars(n_days=300, bars_per_day=8)
    monkeypatch.setattr(
        gc, "assemble_experiment",
        lambda data_root, symbols, max_bars: {
            "TST H1": {"bars": bars, "costs": ConstCosts(), "sizes": 1.0,
                       "vols": (0.01, 0.01, 100.0), "timeframe": "H1",
                       "session_policy": None}})
    out_dir = tmp_path / "gates-v3"
    rc = gc.main(["--phase", "v3", "--quick", "--v3-setups", "TST:H1",
                  "--v3-levels", "1.0", "--out", str(out_dir),
                  "--data-root", "data_research"])
    assert rc == 0
    payload = json.loads((out_dir / "gate-power-v3.json").read_text(
        encoding="utf-8"))
    assert payload["meta"]["phase"] == "v3"
    assert payload["meta"]["primary_schemes"] == ["dsr", "year_block"]
    assert "negative" in payload and "positive" in payload
    assert payload["negative"]["runs"], "quick 负对照应有记录"
    assert payload["positive"]["runs"], "quick 正对照应有记录"
    for run in payload["negative"]["runs"] + payload["positive"]["runs"]:
        assert {"G0", "primary.dsr", "primary.year_block",
                "veto.cost_x15", "veto.delay_1_bar",
                "veto.buyhold_excess"} <= set(run["checks"])
    md = (out_dir / "gate-power-v3.md").read_text(encoding="utf-8")
    assert "联合" in md


# ================================================ T85：工作点补充标定（K1/K2）
def test_v3_primary_suite_records_n_grid(gc):
    """T85 失败测试：v3 主检验组逐条记录 N∈{2,6,24} 的方案甲
    （DSR(N, V₀=1/(n−1))）与方案丙（PSR(0) ≥ 1−α/N）；现行键
    （primary.dsr / primary.year_block / @N40）语义不变。"""
    cfg = _v3_cfg(gc)
    hours = pd.date_range("2016-01-01", periods=6000, freq="6h")
    rng = np.random.default_rng(21)
    rets = pd.Series(0.0004 + rng.normal(0.0, 0.004, 6000),
                     index=pd.DatetimeIndex(hours))
    primary = gc.v3_primary_suite(rets, cfg=cfg, seed=5)
    assert "dsr" in primary and "year_block" in primary \
        and "year_block@N40" in primary               # 现行键不变
    for n in gc.V3_N_GRID:
        assert n in (2, 6, 24)
        a = primary[f"dsr_v0@N{n}"]
        c = primary[f"psr0_bonf@N{n}"]
        assert a["test"] == "dsr_v0" and a["n_trials"] == n
        assert a["v0"] == pytest.approx(1.0 / (len(rets) - 1))
        assert c["test"] == "psr0_bonferroni" and c["bonferroni_n"] == n
        assert c["threshold"] == pytest.approx(1.0 - cfg.alpha / n)
    # 甲：N 抬高 → SR₀ 抬高 → 概率单调不增；丙：概率与 N 无关（同一 PSR(0)）
    a_probs = [primary[f"dsr_v0@N{n}"]["probability"] for n in gc.V3_N_GRID]
    assert a_probs == sorted(a_probs, reverse=True)
    c_probs = {primary[f"psr0_bonf@N{n}"]["probability"] for n in gc.V3_N_GRID}
    assert len(c_probs) == 1
    # 联合通过率对新增键逐条重算；缺键拒绝（两方案必须逐条记录）
    checks = {"G0": True, "primary.dsr": True, "primary.year_block": True,
              "primary.year_block@N40": True, "veto.cost_x15": True,
              "veto.delay_1_bar": True, "veto.buyhold_excess": True,
              **{f"primary.dsr_v0@N{n}": True for n in gc.V3_N_GRID},
              **{f"primary.psr0_bonf@N{n}": False for n in gc.V3_N_GRID}}
    rates = gc.v3_joint_rates([{"checks": dict(checks)}])
    assert rates["joint.dsr_v0@N6"] == pytest.approx(1.0)
    assert rates["joint.psr0_bonf@N6"] == pytest.approx(0.0)
    with pytest.raises(KeyError):
        gc.v3_joint_pass({k: v for k, v in checks.items()
                          if k != "primary.psr0_bonf@N24"},
                         "psr0_bonf@N24")


_T85_SETUPS = ("XAUUSD H1", "XAUUSD H4", "MERGED:XAUUSD+USTEC H1")


def _t85_synthetic_results(*, fp_bing=0.0, det_jia=(0.6, 0.6, 0.3),
                           det_bing=(0.8, 0.7, 0.4)):
    """合成 v3 产物（3 设置 × 2 零假设 × 10 种子；档位 0.5/1.0）：否决层
    全过，主检验布尔编码目标率——甲@N6 检出 det_jia、丙@N6 det_bing
    （两档位同率）；丙联合误报 fp_bing（甲恒 0）；N=2 同 N=6，N=24 各再
    收一档（测试只断言 N=6 与合格性，其余档只为凑齐键）。"""
    def checks(j_by_n, b_by_n):
        grid = {f"primary.dsr_v0@N{n}": j_by_n[n] for n in (2, 6, 24)}
        grid.update({f"primary.psr0_bonf@N{n}": b_by_n[n] for n in (2, 6, 24)})
        return {"G0": True, "primary.dsr": True, "primary.year_block": True,
                "primary.year_block@N40": True, "veto.cost_x15": True,
                "veto.delay_1_bar": True, "veto.buyhold_excess": True,
                **grid}

    neg, pos = [], []
    for si, setup in enumerate(_T85_SETUPS):
        for null in ("slot", "day"):
            for s in range(10):
                bing = s < round(fp_bing * 10)
                neg.append({"setup": setup, "null": null, "seed": s,
                            "checks": checks(
                                {n: False for n in (2, 6, 24)},
                                {n: bing for n in (2, 6, 24)})})
        for level in (0.5, 1.0):
            for s in range(10):
                j6 = s < round(det_jia[si] * 10)
                b6 = s < round(det_bing[si] * 10)
                pos.append({"setup": setup, "level": level, "seed": s,
                            "checks": checks(
                                {2: j6, 6: j6,
                                 24: j6 and s < max(0, round(det_jia[si] * 5))},
                                {2: b6, 6: b6,
                                 24: b6 and s < max(0, round(det_bing[si] * 10)
                                                    - 1)})})
    return {"meta": {"phase": "v3", "levels": [0.5, 1.0], "seeds": 10},
            "negative": {"runs": neg}, "positive": {"runs": pos}}


def test_v3_t85_selection_rule(gc):
    """T85 失败测试：预写选择规则（先于结果，不得按结果调整）——
    1) 合格 = 三个 N、全部设置、两种零假设下联合误报都 ≤5%；
    2) 合格方案里选 N=6 时夏普 1.0 联合检出率较高者（逐设置：不低于且
       至少一处严格更高者胜）；处处相同选丙；
    3) 按选中方案在 N=6 重判"测不了"（<50%），N=2/N=24 并列报告。"""
    out = gc.v3_t85_operating_point(_t85_synthetic_results())
    assert out["eligibility"] == {"dsr_v0": True, "psr0_bonf": True}
    assert out["selected"] == "psr0_bonf"
    assert out["detection_at_n6_level_1"]["dsr_v0"] == \
        pytest.approx({"XAUUSD H1": 0.6, "XAUUSD H4": 0.6,
                       "MERGED:XAUUSD+USTEC H1": 0.3})
    assert out["detection_at_n6_level_1"]["psr0_bonf"] == \
        pytest.approx({"XAUUSD H1": 0.8, "XAUUSD H4": 0.7,
                       "MERGED:XAUUSD+USTEC H1": 0.4})
    by_cat = {row["category"]: row for row in out["unmeasurable"]}
    assert "MERGED:XAUUSD+USTEC H1 夏普1.0" in by_cat       # 0.4 < 0.5
    row = by_cat["MERGED:XAUUSD+USTEC H1 夏普1.0"]
    assert row["rates"]["N6"] == pytest.approx(0.4)
    assert set(row["rates"]) == {"N2", "N6", "N24"}         # 并列报告
    assert "XAUUSD H1 夏普1.0" not in by_cat                # 0.8 ≥ 0.5 可测
    # 处处相同 → 选丙（实现更简单）
    tie = gc.v3_t85_operating_point(_t85_synthetic_results(
        det_jia=(0.8, 0.7, 0.4), det_bing=(0.8, 0.7, 0.4)))
    assert tie["selected"] == "psr0_bonf"
    # 甲逐设置不低于且至少一处严格更高 → 选甲
    jia_wins = gc.v3_t85_operating_point(_t85_synthetic_results(
        det_jia=(0.9, 0.8, 0.5), det_bing=(0.8, 0.7, 0.4)))
    assert jia_wins["selected"] == "dsr_v0"
    # 丙任一 N 档联合误报 >5% → 丙不合格，只剩甲
    fp_case = gc.v3_t85_operating_point(_t85_synthetic_results(fp_bing=0.1))
    assert fp_case["eligibility"] == {"dsr_v0": True, "psr0_bonf": False}
    assert fp_case["selected"] == "dsr_v0"
