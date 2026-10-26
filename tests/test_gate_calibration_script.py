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
                         "baselines": ["X"],
                         "generated_utc": "2026-10-26T00:00:00+00:00"},
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
    """按 gate_calibration 当前单进程产物的真实 schema 造一个小分片：

    - negative.runs 带 null；positive.runs 带 setup（detection 的分组键）、
      calibration 是 {setup: {level: {drift, achieved_sharpe}}} **字典**；
    - g5.runs 带 setup/arm/null（arm="detection"|"false_positive"，
      误报臂 null 区分 day/slot 两种零假设）。
    """
    neg = [{"name": "H009-Sample", "seed": s, "null": "slot",
            "sharpe": 0.1, "net_profit": 1.0, "trade_count": 10,
            "passed_all": True,
            "checks": {"G0": True, "G1.profit_factor": True}}
           for s in seeds]
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
                 "null_hypothesis": "both", "initial_capital": 100000.0,
                 "risk_per_trade": 0.005, "max_bars": None,
                 "baselines": ["H009-Sample"], "positive_symbols": ["TST H1"],
                 "g5_bootstrap_sims": 200, "generated_utc":
                     "2026-10-26T00:00:00+00:00"},
        "negative": {"runs": neg, "by_null": {"slot": {"G0": 1.0}},
                     "false_positive_rates": {"G0": 1.0},
                     "merged": [], "merged_pass_rates": {}},
        "positive": {
            "runs": pos,
            "detection_rates": {"TST H1": {"1.0": {
                "G0": 1.0, "G1.profit_factor": 1.0}}},
            "calibration": {"TST H1": {"1.0": {"drift": cal_drift,
                                               "achieved_sharpe": 1.02}}}},
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
