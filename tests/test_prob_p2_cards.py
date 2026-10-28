"""T73A 审核三 D1 补测：P2 决策卡可写可跑、validation 评估切 [split, cutoff)、
probability 卡拒绝 screen/gate、卡/策略常量核对、G0 抓现场统计的坏策略、
轮次假设登记交叉校验。全部合成数据。
"""
import types

import numpy as np
import pandas as pd
import pytest

REPO = __import__("pathlib").Path(__file__).resolve().parents[1]
import sys
sys.path[:0] = [str(REPO / "src"), str(REPO / "scripts")]

import importlib.util
_spec = importlib.util.spec_from_file_location("bt_d1", REPO / "scripts" / "bt.py")
BT = importlib.util.module_from_spec(_spec)
sys.modules["bt_d1"] = BT
_spec.loader.exec_module(BT)

from mt5quant.backtest.engine_a import run_backtest          # noqa: E402
from mt5quant.research import cards as rcards                # noqa: E402
from mt5quant.strategies import BaseStrategy                 # noqa: E402
from mt5quant.strategies.library.prob.level_decision import (  # noqa: E402
    LevelDecisionS1Demo,
)
from mt5quant.strategies.library.prob.situations import (    # noqa: E402
    Approach, find_level_events, s1_level_rows,
)
from mt5quant.backtest.engine_a import atr_series            # noqa: E402


def p2_card_data(**over):
    from tests.test_prob_cards import p_card_data
    d = p_card_data()
    d["evaluation_mode"] = "standard"
    d["segment"] = "validation"
    d.pop("primary_statistic", None)
    d["strategy"] = "x:Y"
    d.update(over)
    return d


# ------------------------------------------------- 卡/策略常量核对

class MatchDemo(LevelDecisionS1Demo):
    pass


class MismatchDemo(LevelDecisionS1Demo):
    SL_ATR = 3.0


def test_constants_check_passes_on_match():
    card = rcards.validate_card(
        _p1_card(strategy="x:Y"), source="t")
    assert BT._check_card_strategy_constants(card, MatchDemo) is None


def test_constants_check_rejects_mismatch():
    card = rcards.validate_card(_p1_card(strategy="x:Y"), source="t")
    msg = BT._check_card_strategy_constants(card, MismatchDemo)
    assert msg and "SL_ATR" in msg


def _p1_card(**over):
    """与 LevelDecisionS1Demo 常量一致的 P1 卡（K=8、网格 2.0、
    approach 12/1.0/0.25、retest 4——与探针 R1 的卡同构）。"""
    from tests.test_prob_cards import p_card_data
    d = p_card_data()
    d["prob"] = {**d["prob"],
                 "bracket": {"sl_atr": 1.0, "tp_atr": 2.0,
                             "horizon_bars": 8},
                 "level_grid": {"XAUUSD": 2.0},
                 "approach": {"lookback_bars": 12,
                              "min_distance_atr": 1.0,
                              "touch_atr": 0.25},
                 "retest_window_bars": 4}
    d.update(over)
    return d


# ------------------------------------------------- G0 抓坏策略（计划缺测）

def test_g0_catches_live_history_stats_strategy():
    """计划缺测：generate_signals 里现场统计全部历史的坏策略被 G0 查出
    （截断一致性：窗口变 → 全历史统计变 → 信号变）。"""
    from mt5quant.validation import lookahead as vlook

    rng = np.random.default_rng(4)
    n = 800
    close = 100 + np.cumsum(rng.normal(0, 0.3, n))
    bars = pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=n, freq="1h"),
        "open": close, "high": close + 0.1, "low": close - 0.1,
        "close": close, "tick_volume": 1, "spread": 5, "real_volume": 0})

    class BadLiveStats(BaseStrategy):
        """现场统计全部历史（铁律 21 违例）：信号依赖全序列均值。"""

        def generate_signals(self, b, params):
            # 逐窗截断时均值不同 → 截断不一致
            med = float(b["close"].expanding().mean().iloc[-1])
            sig = (b["close"] > med).astype(int).to_numpy()
            # 信号只在 0/±1：粗略地把涨过均值当 +1，跌破当 −1
            sig = np.where(b["close"].to_numpy() > med, 1, -1)
            return pd.Series(sig, index=b.index, dtype="int64")

    res = vlook.run_g0(BadLiveStats(), bars, {})
    assert res.get("passed", True) is False, res


def test_g0_passes_frozen_model_strategy():
    """对照：冻结模型的拟合策略过 G0（T74 已有——此处绑定 D1 门（按
    segment）后仍成立）。"""
    from tests.test_strategies_prob_level_decision import (
        DemoSmallGrid, FlatCosts, _strong_model, synth_bars as sb,
    )
    from mt5quant.validation import lookahead as vlook
    bars = sb(3000, seed=42)
    strat = DemoSmallGrid().with_model(_strong_model())
    assert vlook.run_g0(strat, bars, {}).get("passed") is True


# ------------------------------------------------- validation 切片（D1）

def _validation_env(tmp_path):
    """P2 决策卡（standard+validation）+ 轮次 R2（split 2019-12-01）。"""
    from tests.test_prob_cards import p_card_data
    (tmp_path / "cards").mkdir(parents=True, exist_ok=True)
    (tmp_path / "rounds").mkdir(parents=True, exist_ok=True)
    d = p_card_data()
    d["evaluation_mode"] = "standard"
    d["segment"] = "validation"
    d.pop("primary_statistic", None)
    d["strategy"] = "mt5quant.strategies.library.prob.level_decision:LevelDecisionS1Demo"
    card_path = tmp_path / "cards" / "P001-v1.toml"
    card_path.write_text(_dict_to_toml(d), encoding="utf-8")
    round_body = '''id = "R2"
opened = "2026-11-02"
candidates = ["C101"]
trial_budget = 12
lockbox_uses_max = 0
dsr_trial_scope = "project"
advance_rule = "a"
close_rule = "c"
approved_by = "P线审核会话"
[segments]
split = "2019-12-01"
[evaluation.C101]
mode = "probability"
primary = "delta_p_one_sided"
[hypotheses]
C101 = ["S1:A3:fade>base"]
'''
    (tmp_path / "rounds" / "R2.toml").write_text(round_body, encoding="utf-8")
    return card_path


def _dict_to_toml(d):
    """最小 TOML 序列化（测试夹具用）。"""
    lines = []
    for k in ("id", "family", "version", "parent", "candidate", "round",
              "strategy", "evaluation_mode", "primary_statistic", "segment",
              "session", "phenomenon", "mechanism", "should_fail_when",
              "falsified_if"):
        if k in d and d[k] != "":
            v = d[k]
            lines.append(f'{k} = "{v}"' if isinstance(v, str)
                         else f"{k} = {v}")
    lines.append("symbols = [" + ", ".join(f'"{s}"' for s in d["symbols"]) + "]")
    lines.append('timeframe = "H1"')
    lines.append("graveyard_checked = []")
    lines.append("")
    lines.append("[rules]")
    for k, v in d["rules"].items():
        lines.append(f'{k} = "{v}"')
    lines.append("")
    lines.append("[expected]")
    for k, v in d["expected"].items():
        lines.append(f'{k} = "{v}"')
    lines.append("")
    lines.append("[prob]")
    pb = d["prob"]
    lines.append("situations = [" + ", ".join(f'"{x}"' for x in pb["situations"]) + "]")
    lines.append("hypotheses = [" + ", ".join(f'"{x}"' for x in pb["hypotheses"]) + "]")
    b = pb["bracket"]
    lines.append(f'bracket = {{ sl_atr = {b["sl_atr"]}, tp_atr = {b["tp_atr"]}, horizon_bars = {b["horizon_bars"]} }}')
    lines.append("level_grid = {" + ", ".join(
        f"{k} = {v}" for k, v in pb["level_grid"].items()) + "}")
    a = pb["approach"]
    lines.append(f'approach = {{ lookback_bars = {a["lookback_bars"]}, min_distance_atr = {a["min_distance_atr"]}, touch_atr = {a["touch_atr"]} }}')
    lines.append(f'breakout_atr = {pb["breakout_atr"]}')
    lines.append(f'retest_window_bars = {pb["retest_window_bars"]}')
    lines.append(f'expected_delta_p = {pb["expected_delta_p"]}')
    return "\n".join(lines) + "\n"


def test_slice_validation_eval(tmp_path):
    """E1：评估窗 = [split − lookback·周期, cutoff)——预热保留分界前
    lookback 根；分界后无数据 → ValueError。"""
    card_path = _validation_env(tmp_path)
    card = rcards.load_card(card_path)
    assert card.segment == "validation"
    from mt5quant.research import rounds as rnd
    rnd_obj = rnd.load_round(tmp_path / "rounds" / "R2.toml")
    times = pd.date_range("2019-11-01", periods=1000, freq="1h")
    bars_all = pd.DataFrame({"time": times, "open": 100.0, "high": 100.5,
                             "low": 99.5, "close": 100.0})
    out = BT._slice_validation_eval(
        card, rnd_obj, {"XAUUSD": bars_all}, lookback_bars=200)
    t = pd.to_datetime(out["XAUUSD"]["time"])
    split = pd.Timestamp("2019-12-01")
    assert (t >= split).any()                      # 分界后必有
    assert t.min() < split                         # 预热跨过分界
    assert t.min() >= split - pd.Timedelta(hours=200)   # 恰留 200 根预热
    assert len(out["XAUUSD"]) < 1000
    # 分界后无数据 → 拒
    bars_before = {"XAUUSD": bars_all[bars_all["time"] < "2019-11-01"]
                   .reset_index(drop=True)}
    with pytest.raises(ValueError, match="没有数据"):
        BT._slice_validation_eval(card, rnd_obj, bars_before,
                                  lookback_bars=200)


def test_reject_probability_card_backtest():
    card = rcards.validate_card(_p1_card(strategy="x:Y"), source="t")
    msg = BT._reject_probability_card_backtest(card)
    assert msg and "bt prob test" in msg
    from tests.test_prob_cards import _h_card_data
    h = rcards.validate_card(_h_card_data(), source="t")
    assert BT._reject_probability_card_backtest(h) is None


# ------------------------------------------------- 轮次假设登记交叉校验

def test_round_open_rejects_unregistered_hypothesis(tmp_path, capsys):
    """计划缺测：[hypotheses] 列出候选没登记的假设 → bt round open 拒。"""
    (tmp_path / "rounds").mkdir(parents=True, exist_ok=True)
    cands = tmp_path / "candidates"
    cands.mkdir(parents=True, exist_ok=True)
    cand_body = '''id = "C101"
title = "整数关口"
mechanism_class = "price_levels"
status = "registered"
symbols = ["XAUUSD"]
holding = "H1"
mechanism = "止盈止损聚集"
[literature]
original = ["Osler 2003"]
replications = []
rebuttals = []
search_log = "JF 2026-10-28"
published_effect = "聚集"
expected_effect = "Δp 0.04"
[prob]
situations = ["S1"]
hypotheses = ["S1:A3:fade>base"]
bracket = { sl_atr = 1.0, tp_atr = 2.0, horizon_bars = 24 }
level_grid = { XAUUSD = 50.0 }
approach = { lookback_bars = 24, min_distance_atr = 1.0, touch_atr = 0.25 }
breakout_atr = 0.25
retest_window_bars = 6
expected_delta_p = 0.04
'''
    (cands / "C101.toml").write_text(cand_body, encoding="utf-8")
    round_body = '''id = "R5"
opened = "2026-11-05"
candidates = ["C101"]
trial_budget = 12
lockbox_uses_max = 0
dsr_trial_scope = "project"
advance_rule = "a"
close_rule = "c"
approved_by = "P线审核会话"
[segments]
split = "2019-12-01"
[evaluation.C101]
mode = "probability"
primary = "delta_p_one_sided"
[hypotheses]
C101 = ["S1:A3:fade>base", "S1:A5:fade>base"]
'''
    rpath = tmp_path / "rounds" / "R5.toml"
    rpath.write_text(round_body, encoding="utf-8")
    import argparse
    from mt5quant.research.ledger import Ledger
    db = tmp_path / "l.sqlite"
    with Ledger(db) as led:
        led.record_candidate_check("C101", decision="pass", result={})
    args = argparse.Namespace(round="R5", rounds_dir=str(tmp_path / "rounds"),
                              ledger=str(db))
    code = BT.cmd_round_open(args)
    out = capsys.readouterr().out
    assert code == 1
    assert "候选登记之外" in out or "S1:A5" in out
