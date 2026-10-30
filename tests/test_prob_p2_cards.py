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
from mt5quant.strategies import BaseStrategy, ExitRules      # noqa: E402
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


# ------------------------------------------------- P2-01 bt 侧（T104 批 2 丁-1）

class A3OnlyDemo(LevelDecisionS1Demo):
    """白名单 = 精确对 S1:A3（S2:A3 不放行）。"""

    ALLOWED_HYPOTHESES = ("S1:A3",)


class TwoSitA3OnlyDemo(LevelDecisionS1Demo):
    """双局面策略、白名单 = 精确对 S1:A3（S2:A3 不放行）。"""

    SITUATIONS = ("S1", "S2")
    ALLOWED_HYPOTHESES = ("S1:A3",)


class TwoSitBareA3Demo(LevelDecisionS1Demo):
    """双局面策略、白名单 = 旧式裸动作 A3（任意局面获准）。"""

    SITUATIONS = ("S1", "S2")
    ALLOWED_HYPOTHESES = ("A3",)


class TamperedExitDemo(LevelDecisionS1Demo):
    """实例级篡改：__init__ 里塞与标签 bracket 脱节的出场规则（类常量
    一致——甲的 __init_subclass__ 拦不住，须实例级核验）。"""

    def __init__(self):
        super().__init__()
        self.exit_rules = ExitRules(stop_loss_atr=9.0, take_profit_atr=10.0,
                                    reentry="after_signal_change")


def _card_with_hyp(hyp, *, situations=None, **over):
    d = _p1_card(strategy="x:Y", **over)
    prob = {**d["prob"], "hypotheses": [hyp]}
    if situations is not None:
        prob["situations"] = situations
    d["prob"] = prob
    return rcards.validate_card(d, source="t")


def test_p201_constants_check_rejects_hypotheses_outside_whitelist():
    """P2-01 bt 侧：卡假设经 parse_hypothesis_pairs ⊆ 策略类获准（局面×动作）
    集合——白名单非空时，集合外的假设不得上卡（A3-only 白名单配 S1:A2 卡拒）。"""
    msg = BT._check_card_strategy_constants(
        _card_with_hyp("S1:A2:follow>base"), A3OnlyDemo)
    assert msg and "S1:A2" in msg and "ALLOWED_HYPOTHESES" in msg
    # 白名单内的假设照常通过；空白名单（缺省）不启用子集核对（兼容）
    assert BT._check_card_strategy_constants(
        _card_with_hyp("S1:A3:fade>base"), A3OnlyDemo) is None
    assert BT._check_card_strategy_constants(
        _card_with_hyp("S1:A2:follow>base"), MatchDemo) is None


def test_p201_constants_check_whitelist_pair_vs_bare_action():
    """P2-01：精确对 "S1:A3" 不放行 S2:A3；旧式裸动作 "A3" = 任意局面获准。"""
    card = _card_with_hyp("S2:A3:fade>base", situations=["S1", "S2"])
    msg = BT._check_card_strategy_constants(card, TwoSitA3OnlyDemo)
    assert msg and "S2:A3" in msg
    assert BT._check_card_strategy_constants(card, TwoSitBareA3Demo) is None


def test_p201_constants_check_rejects_tampered_instance_exit_rules():
    """P2-01 bt 侧：实例**真实** exit_rules 与标签 bracket 一致——甲的
    实例级核验入口被 checker 调用（类常量一致但实例被覆写 → 拒）。"""
    msg = BT._check_card_strategy_constants(
        _card_with_hyp("S1:A3:fade>base"), TamperedExitDemo)
    assert msg and "exit_rules" in msg and "P2-01" in msg
    # 未篡改的子类照常通过
    assert BT._check_card_strategy_constants(
        _card_with_hyp("S1:A3:fade>base"), MatchDemo) is None


def test_p201_trial_identity_version_v2():
    """P2-01：获准白名单并入试验身份——TRIAL_IDENTITY_VERSION 提升至 v2
    （旧 ledger 历史行不重算，新旧口径以 identity_version 显式标记）。"""
    assert BT.TRIAL_IDENTITY_VERSION == "v2"


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


# ------------------------------------------------- P-08（T98 波次 2）

def test_p08_slice_validation_warmup_counts_actual_bars(tmp_path):
    """P-08：预热按**实际 bar 数**取（searchsorted+iloc）——周末/长休市下
    日历时间差会少留（codex 复现：要求 2100 根、日历口径只留 1476）。"""
    card_path = _validation_env(tmp_path)
    card = rcards.load_card(card_path)
    from mt5quant.research import rounds as rnd
    rnd_obj = rnd.load_round(tmp_path / "rounds" / "R2.toml")
    # FX 周末：小时 bar 剔除周六周日
    t = pd.date_range("2019-05-01", periods=12000, freq="1h")
    t = t[t.dayofweek < 5]
    bars_all = pd.DataFrame({"time": t, "open": 100.0, "high": 100.5,
                             "low": 99.5, "close": 100.0})
    out = BT._slice_validation_eval(
        card, rnd_obj, {"XAUUSD": bars_all}, lookback_bars=2100)
    tt = pd.to_datetime(out["XAUUSD"]["time"])
    split = pd.Timestamp("2019-12-01")
    assert (tt >= split).any()                       # 分界后必有
    n_pre = int((tt < split).sum())
    assert n_pre == 2100                             # 实际 bar 数，非日历时差
    assert tt.min() < split                          # 预热跨过分界


def test_p08_slice_validation_warmup_capped_at_available(tmp_path):
    """P-08：分界前不足 lookback 根时取全部可用（不越界）。"""
    card_path = _validation_env(tmp_path)
    card = rcards.load_card(card_path)
    from mt5quant.research import rounds as rnd
    rnd_obj = rnd.load_round(tmp_path / "rounds" / "R2.toml")
    # 300 根在分界后 + 分界前只有 50 根
    t_pre = pd.date_range("2019-11-29 22:00", periods=50, freq="1h")
    t_post = pd.date_range("2019-12-01", periods=300, freq="1h")
    times = t_pre.append(t_post)
    bars_all = pd.DataFrame({"time": times, "open": 100.0, "high": 100.5,
                             "low": 99.5, "close": 100.0})
    out = BT._slice_validation_eval(
        card, rnd_obj, {"XAUUSD": bars_all}, lookback_bars=2100)
    assert len(out["XAUUSD"]) == 350


class _StubSignalStrategy:
    """P-08 探针：返回预置信号序列（透传 required_data/exit_rules）。"""

    fittable = False

    def __init__(self, sig):
        self._sig = list(sig)

    def generate_signals(self, bars, params):
        return pd.Series(self._sig, index=bars.index, dtype="int64")

    def validate_params(self, params):
        return True

    @property
    def exit_rules(self):
        return object()

    @property
    def required_data(self):
        return None


def _bars_at(n):
    return pd.DataFrame({
        "time": pd.date_range("2019-12-01", periods=n, freq="1h"),
        "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0})


def test_p08_zeroed_before_split_is_run_level():
    """P-08：连续非零信号段（目标仓位 run）起点在 split 前 → 整段归零
    （含 split 后尾巴；codex 复现：split 当根信号=1 → 修复后=0）；
    split 后新触发的段不受影响。"""
    n = 40
    split = "2019-12-01 10:00"          # 第 11 根（index 10）起为分界后
    sig = [0] * 5 + [1] * 16 + [0] * 4 + [1] * 4 + [0] * 11
    # run1 = idx 5..20（起点 5 < split，跨过分界）→ 整段归零
    # run2 = idx 25..28（起点 25 ≥ split）→ 保留
    gate = BT._ZeroedBeforeSplit(_StubSignalStrategy(sig), split)
    out = gate.generate_signals(_bars_at(n), {})
    expect = [0] * n
    for i in range(25, 29):
        expect[i] = 1
    assert list(out) == expect
    # codex 口径：split 当根（index 10）信号必须为 0
    assert int(out.iloc[10]) == 0
    # split 后新触发不受影响
    assert int(out.iloc[25]) == 1


def test_p08_zeroed_before_split_all_runs_post_split_kept():
    """P-08：所有 run 都在 split 后触发 → 全部保留（门不改变验证段信号）。"""
    n = 30
    split = "2019-12-01 10:00"
    sig = [0] * 12 + [1] * 5 + [0] * 3 + [-1] * 4 + [0] * 6
    gate = BT._ZeroedBeforeSplit(_StubSignalStrategy(sig), split)
    out = gate.generate_signals(_bars_at(n), {})
    assert list(out) == sig


def test_p08_zeroed_before_split_sign_flip_in_pre_split_run():
    """P-08：split 前的段内翻向（+1→−1 不归零）也属同一连续非零段——
    起点在 split 前，整段（含翻向尾巴）归零。"""
    n = 30
    split = "2019-12-01 12:00"
    sig = [0] * 5 + [1] * 6 + [-1] * 6 + [0] * 13   # 连续非零段 5..16
    gate = BT._ZeroedBeforeSplit(_StubSignalStrategy(sig), split)
    out = gate.generate_signals(_bars_at(n), {})
    assert list(out) == [0] * n


# ------------------------------------------------- P2-02 决策起点（T104 批 2 丁-2）

def _decision_probe_strategy():
    """决策起点探针策略：S1:A3 恒赢估计 + 近零成本（过决策门槛）。"""
    from types import SimpleNamespace
    from mt5quant.prob.tables import CellEstimate
    est = CellEstimate(n=1000, p_win=1.0, p_loss=0.0, p_time=0.0, r_win=2.0,
                       r_loss=-1.0, r_time=0.0, se_win=0.0, se_loss=0.0)
    strat = LevelDecisionS1Demo()
    strat._table = SimpleNamespace(lookup=lambda *a: est)
    strat._cost_r = {("S1", "A3"): 0.001}
    return strat


def _patch_level_discovery(monkeypatch, trig_rows):
    from mt5quant.strategies.library.prob import level_decision as ld
    monkeypatch.setattr(ld, "level_rows_for_situation",
                        lambda *a, **k: [])
    monkeypatch.setattr(ld, "find_level_events", lambda *a, **k: [])
    monkeypatch.setattr(ld, "action_triggers",
                        lambda *a, **k: pd.DataFrame(trig_rows))


def _probe_bars(n=80):
    return pd.DataFrame({
        "time": pd.date_range("2019-11-01", periods=n, freq="1h"),
        "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0,
        "tick_volume": 1, "spread": 0, "real_volume": 0})


def test_p202_gate_uses_decision_start_first_post_split_trigger_fills(
        monkeypatch):
    """P2-02：_ZeroedBeforeSplit 优先走 with_decision_start 原语——训练段
    触发（bar 40 < split 41）不再经持有尾巴压掉 split 后首根合法新触发
    （bar 42）；split 前信号恒 0（探针 boundary_state 的旧行为即此缺陷）。"""
    data = _probe_bars()
    split = data["time"].iloc[41]
    _patch_level_discovery(
        monkeypatch,
        {"bar": [40, 42], "situation": ["S1", "S1"],
         "action": ["A3", "A3"], "side": [1, 1]})
    out = BT._ZeroedBeforeSplit(
        _decision_probe_strategy(), split).generate_signals(data, {})
    assert int(out.iloc[40]) == 0            # split 前不建任何交易状态
    assert int(out.iloc[42]) == 1            # split 后首根合法新触发 → 成交


def test_p202_gate_pre_split_only_trigger_never_trades(monkeypatch):
    """P2-02：split 前触发、split 后无触发 → 全段不交易（决策起点从空状态
    处理**新**触发，训练段触发不结转）。"""
    data = _probe_bars()
    split = data["time"].iloc[41]
    _patch_level_discovery(
        monkeypatch,
        {"bar": [40], "situation": ["S1"], "action": ["A3"], "side": [1]})
    out = BT._ZeroedBeforeSplit(
        _decision_probe_strategy(), split).generate_signals(data, {})
    assert int(out.to_numpy().nonzero()[0].size) == 0


def test_p202_gate_decision_start_keeps_run_within_horizon(monkeypatch):
    """P2-02：split 后触发的持有段照常持续 K 根后归零（决策起点不改变
    持有上限语义）；段的起点（split 前）不残留。"""
    data = _probe_bars()
    split = data["time"].iloc[10]
    _patch_level_discovery(
        monkeypatch,
        {"bar": [20], "situation": ["S1"], "action": ["A3"], "side": [1]})
    out = BT._ZeroedBeforeSplit(
        _decision_probe_strategy(), split).generate_signals(data, {})
    sig = out.to_numpy()
    assert sig[10:20].tolist() == [0] * 10
    assert sig[20:28].tolist() == [1] * 8     # K=8（Demo 的 HORIZON_BARS）
    assert int(sig[28]) == 0                  # S4：K 根后强制归零一根


def test_p202_oos_summary_sliced_from_split():
    """P2-02：P2 卡评估 summary 以 OOS 起点切取——收益/时间序列从 split
    起算、起点资金基准 = split 当根权益（预热零收益不进统计），split 前
    入场的交易不进指标（兜底：决策起点下本来不可能有）。"""
    from types import SimpleNamespace
    n = 10
    bars = pd.DataFrame({
        "time": pd.date_range("2019-11-01", periods=n, freq="1h"),
        "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0})
    equity = pd.Series([100.0 + 2.0 * i for i in range(n)])
    trades = pd.DataFrame({
        "entry_time": [pd.Timestamp("2019-11-01 01:00")],
        "exit_time": [pd.Timestamp("2019-11-01 03:00")],
        "side": ["long"], "net": [3.0], "gross": [3.0], "commission": [0.0],
        "slippage_cost": [0.0], "spread_cost": [0.0], "swap": [0.0],
        "r_multiple": [1.0], "bars_held": [2]})
    result = SimpleNamespace(
        bars=bars, equity=equity, trades=trades, params={}, symbol="XAUUSD",
        timeframe="H1", ruined=False, ruined_at=None, skipped_zero_lots=0)
    split = pd.Timestamp("2019-11-01 05:00")
    s = BT.summarize(BT._OosSlicedResult(result, split), cost_version="v")
    # 起点资金基准 = split 当根权益（110），不是预热段首（100）
    assert s["return_pct"] == pytest.approx(equity.iloc[-1] / 110.0 - 1.0)
    # split 前入场的交易被剔除（0 笔）；时间序列长度 = OOS 段
    assert s["trade_count"] == 0
    assert s["time_in_market"] == 0.0
    # 对照：全窗 summary 会把预热段收益算进去
    full = BT.summarize(result, cost_version="v")
    assert full["return_pct"] == pytest.approx(equity.iloc[-1] / 100.0 - 1.0)
    assert full["trade_count"] == 1


# ------------------------------------------------- P-07 bt 侧（T98）

def test_p07_constants_check_rejects_symbols_mismatch():
    """P-07 bt 侧：card.symbols 与策略类 SYMBOLS 不一致 → 拒。"""
    d = _p1_card(strategy="x:Y")
    d["symbols"] = ["EURUSD"]
    d["prob"] = {**d["prob"], "level_grid": {"EURUSD": 2.0}}
    card = rcards.validate_card(d, source="t")
    msg = BT._check_card_strategy_constants(card, MatchDemo)
    assert msg and "SYMBOLS" in msg


def test_p07_constants_check_rejects_timeframe_mismatch():
    """P-07 bt 侧：card.timeframe 与策略类 TIMEFRAME/required_data.timeframe
    不一致 → 拒。"""
    card = rcards.validate_card(
        _p1_card(strategy="x:Y", timeframe="M15"), source="t")
    msg = BT._check_card_strategy_constants(card, MatchDemo)
    assert msg and "timeframe" in msg


def test_p07_constants_check_symbols_timeframe_pass_on_match():
    card = rcards.validate_card(_p1_card(strategy="x:Y"), source="t")
    assert BT._check_card_strategy_constants(card, MatchDemo) is None


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
