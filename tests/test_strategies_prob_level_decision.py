"""T74 收尾：LevelDecisionV1——拟合/冻结/决策信号与引擎验证。"""
import numpy as np
import pandas as pd
import pytest

from mt5data.store import _COLUMNS, TIMEFRAME_SECONDS
from mt5quant.backtest.engine_a import atr_series, run_backtest
from mt5quant.prob.tables import ProbTable
from mt5quant.strategies import ExitRules
from mt5quant.strategies.library.prob.level_decision import (
    LevelDecisionS1Demo, LevelDecisionV1, ModelMissingError,
)
from mt5quant.strategies.library.prob.situations import Approach


class FlatCosts:
    def spread_p90(self, symbol, hour_ny, *, price=None):
        return 0.02

    def slippage(self, symbol, hour_ny, *, price=None):
        return 0.01

    def commission_per_lot(self, symbol):
        return 0.0

    def overnight_fee(self, symbol, side, from_ts, to_ts, *, price=None):
        return 0.0


def synth_bars(n, *, seed, start="2020-01-02", s0=100.0):
    rng = np.random.default_rng(seed)
    secs = TIMEFRAME_SECONDS["H1"]
    steps = rng.normal(0.0, 0.30, size=n)
    close = s0 + np.cumsum(steps)
    openc = close + rng.normal(0.0, 0.05, size=n)
    high = np.maximum(openc, close) + np.abs(rng.normal(0, 0.12, size=n))
    low = np.minimum(openc, close) - np.abs(rng.normal(0, 0.12, size=n))
    return pd.DataFrame({
        "time": pd.date_range(start, periods=n, freq=f"{secs}s"),
        "open": openc, "high": high, "low": low, "close": close,
        "tick_volume": 10, "spread": 5, "real_volume": 0,
    })[_COLUMNS]


class DemoSmallGrid(LevelDecisionS1Demo):
    """测试用：2.0 网格（事件密度高），K=8。"""

    LEVEL_GRID = {"XAUUSD": 2.0}
    HORIZON_BARS = 8
    APPROACH = Approach(lookback_bars=12, min_distance_atr=1.0,
                        touch_atr=0.25)
    RETEST_WINDOW_BARS = 4


from mt5data.sessions import SessionPolicy   # noqa: E402
PERMISSIVE = SessionPolicy(exempt_symbols=frozenset({"XAUUSD"}))


@pytest.fixture(scope="module")
def fitted():
    bars = synth_bars(4000, seed=42)
    strat = DemoSmallGrid()
    model = strat.fit(
        {"XAUUSD": bars}, {"XAUUSD": FlatCosts()},
        {"__fit__": {"sizes": {"XAUUSD": 100.0},
                     "policies": {"XAUUSD": PERMISSIVE}}},
        train_end=pd.Timestamp("2020-06-01"), purge_bars=10)
    return bars, model


def test_fit_produces_appendix_b_model(fitted):
    bars, model = fitted
    assert model["prob_version"] == "1.0.0"
    assert model["bracket"] == {"sl_atr": 1.0, "tp_atr": 2.0,
                                "horizon_bars": 8}
    assert model["fit_window"]["end"].startswith("2020-06-01")
    assert model["purge_bars"] == 10
    assert len(model["prob_table"]["cells"]) >= 1
    # 同一输入重拟合 → 同一模型（铁律 21：模型身份确定性）
    bars2 = synth_bars(4000, seed=42)
    strat2 = DemoSmallGrid()
    model2 = strat2.fit(
        {"XAUUSD": bars2}, {"XAUUSD": FlatCosts()},
        {"__fit__": {"sizes": {"XAUUSD": 100.0},
                     "policies": {"XAUUSD": PERMISSIVE}}},
        train_end=pd.Timestamp("2020-06-01"), purge_bars=10)
    assert model2 == model


def test_generate_signals_requires_model(fitted):
    bars, model = fitted
    with pytest.raises(ModelMissingError):
        DemoSmallGrid().generate_signals(bars, {})
    strat = DemoSmallGrid().with_model(model)
    sig = strat.generate_signals(bars, {})
    assert set(pd.unique(sig)) <= {-1, 0, 1}


def _strong_model():
    """手工高优势模型：S1×A3 赢率 0.9——保证决策触发（随机游走无优势时
    策略应全'不做'，引擎验证需要一个会入场的模型）。"""
    from mt5quant.prob.tables import ProbTable
    rows = [("win", 1.9)] * 900 + [("loss", -1.05)] * 90 + [("time", 0.1)] * 10
    labels = pd.DataFrame({
        "situation": "S1", "action": "A3",
        "outcome": [o for o, _ in rows], "net_r": [r for _, r in rows],
        "trend_sma200": "na", "atr_pct_2000": "na"})
    tbl = ProbTable.fit(labels, group_by=["trend_sma200", "atr_pct_2000"],
                        prior_strength=100, min_cell=100)
    return {"prob_version": "1.0.0", "prob_table": tbl.to_dict(),
            "bracket": {"sl_atr": 1.0, "tp_atr": 2.0, "horizon_bars": 8},
            "cost_r_by_situation_action": {"S1|A3": 0.001}}


def test_hold_capped_and_chaining_via_engine():
    """信号层持有上限与引擎行为：单次触发的信号恰 K 根后归零；连续新
    触发可链式续持（每次重臂是新的 K 根决策段——bars_held 可大于 K，
    这是设计行为）；止损出场后引擎 reentry=after_signal_change 生效。"""
    bars = synth_bars(4000, seed=42)
    strat = DemoSmallGrid().with_model(_strong_model())
    res = run_backtest(strat, bars, FlatCosts(), symbol="XAUUSD", params={},
                       contract_size=100.0, timeframe="H1",
                       volume_lots=1.0)
    assert len(res.trades) >= 1
    reasons = set(res.trades["exit_reason"])
    assert reasons <= {"signal", "stop", "stop_gap", "take_profit",
                       "take_profit_gap"}


def test_single_trigger_run_is_exactly_K():
    """单一触发（无后续触发可链式）：非零信号段长度恰为 K，随后归零。"""
    plat = (98.5, 99.0, 98.0, 98.5)          # TR=1，high 距 100 为 1
    rows = [plat] * 40
    rows.append((99.5, 99.9, 99.0, 99.0))    # anchor：high 触 100±0.3×ATR 带
    rows += [plat] * 30
    bars = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    bars.insert(0, "time",
                pd.date_range("2024-01-02", periods=len(rows), freq="1h"))
    bars["tick_volume"], bars["spread"], bars["real_volume"] = 10, 5, 0
    bars = bars[_COLUMNS]
    strat = DemoSmallGrid().with_model(_strong_model())
    sig = strat.generate_signals(bars, {}).to_numpy()
    nz = np.flatnonzero(sig != 0)
    assert len(nz) == DemoSmallGrid.HORIZON_BARS      # 恰 K 根
    assert nz[0] == 40                                # anchor 当根起（收盘信号）
    assert (sig[nz] == sig[nz][0]).all()              # 同方向





def test_exit_rules_declared_for_engine(fitted):
    assert DemoSmallGrid.exit_rules.stop_loss_atr == 1.0
    assert DemoSmallGrid.exit_rules.take_profit_atr == 2.0
    assert DemoSmallGrid.exit_rules.reentry == "after_signal_change"
    assert DemoSmallGrid.exit_rules.time_stop_bars is None
    assert DemoSmallGrid.exit_rules.week_close_exit is False
    assert DemoSmallGrid.fittable is True


def test_g0_frozen_model(fitted):
    """G0：冻结模型下截断一致与窗口不变性（≥100 随机探针）。"""
    from mt5quant.validation import lookahead as vlook
    bars, model = fitted
    strat = DemoSmallGrid().with_model(model)
    res = vlook.run_g0(strat, bars, {})
    assert res.get("passed", False) is True, res


def test_with_model_rejects_bad_structure():
    with pytest.raises(ValueError, match="prob_table"):
        DemoSmallGrid().with_model({"schema_version": 1})


# ------------------------------------------------- 审核三（S4/S5/D2）+缺测

def _chain_bars():
    """第 40 根触 100 阻力带（A3 做空）、恰好 K=8 根后第 48 根触 102
    阻力带（又一个 A3）——旧实现会链式续持 16 根（审核三 R3）。"""
    rows = ([(98.5, 99.0, 98.0, 98.5)] * 40 + [(99.5, 99.9, 99.0, 99.0)]
            + [(100.8, 101.3, 100.5, 101.0)] * 7
            + [(101.5, 101.9, 101.2, 101.4)]
            + [(101.2, 101.4, 100.9, 101.1)] * 30)
    bars = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    bars.insert(0, "time",
                pd.date_range("2024-01-02", periods=len(rows), freq="1h"))
    bars["tick_volume"], bars["spread"], bars["real_volume"] = 10, 5, 0
    return bars[_COLUMNS]


def test_s4_no_chaining_hold_capped_at_k():
    """S4：K 根后强制归零一根——同向连续段 ≤ K；第 i+K 根的新触发被
    忽略（不链式续持，每个决策对应一笔与标签同口径的交易）。"""
    strat = DemoSmallGrid().with_model(_strong_model())
    sig = strat.generate_signals(_chain_bars(), {}).to_numpy()
    nz = np.flatnonzero(sig != 0)
    assert len(nz) == DemoSmallGrid.HORIZON_BARS      # 只有第一段（8 根）
    assert nz[0] == 40
    run, best, prev = 0, 0, 0
    for v in sig:
        run = run + 1 if (v != 0 and v == prev) else (1 if v != 0 else 0)
        best = max(best, run)
        prev = v
    assert best <= DemoSmallGrid.HORIZON_BARS
    # 随机序列上同样不越界
    sig2 = strat.generate_signals(synth_bars(4000, seed=42), {}).to_numpy()
    run, best, prev = 0, 0, 0
    for v in sig2:
        run = run + 1 if (v != 0 and v == prev) else (1 if v != 0 else 0)
        best = max(best, run)
        prev = v
    assert best <= DemoSmallGrid.HORIZON_BARS


def test_missing_no_reentry_after_stop_via_engine():
    """计划缺测：止损出场后，同一次触发（信号未归零前）不再同向重入
    （reentry=after_signal_change 经引擎验证）。"""
    from mt5quant.sizing import RiskSizing
    bars = synth_bars(4000, seed=42)
    strat = DemoSmallGrid().with_model(_strong_model())
    res = run_backtest(strat, bars, FlatCosts(), symbol="XAUUSD", params={},
                       contract_size=100.0, timeframe="H1",
                       sizing=RiskSizing(risk_fraction=0.005),
                       vol_min=0.01, vol_step=0.01, vol_max=10.0)
    trades = res.trades
    assert len(trades) >= 1
    sig = strat.generate_signals(bars, {}).to_numpy()
    pos = np.flatnonzero(sig != 0)
    # 每笔交易的入场时刻必须落在信号归零之后重新出发的段里：
    # 同一段（连续同向非零）内的入场数 ≤ 1（止损后无立即同向重入）
    seg_id = np.zeros(len(sig), dtype=int)
    cur = 0
    for i in range(1, len(sig)):
        if sig[i] != 0 and sig[i] == sig[i - 1]:
            seg_id[i] = cur
        else:
            cur += 1
            seg_id[i] = cur if sig[i] != 0 else 0
    times = pd.Index(bars["time"])
    for t in pd.to_datetime(trades["entry_time"]):
        k = times.get_indexer([t])[0]
        entries_in_seg = sum(
            1 for et in pd.to_datetime(trades["entry_time"])
            if seg_id[times.get_indexer([et])[0]] == seg_id[k] != 0)
        assert entries_in_seg <= 1
    # 止损出场的笔数 > 0 的序列上仍成立（强模型+随机游走必有 SL）
    assert (trades["exit_reason"].isin(["stop", "stop_gap"])).any()


def test_missing_volume_unchanged_after_losses():
    """计划缺测：连续亏损后手数不变（basis="initial"，铁律 20）——
    引擎 sizing 模式下所有交易 volume_lots 相同。"""
    from mt5quant.sizing import RiskSizing
    bars = synth_bars(4000, seed=42)
    strat = DemoSmallGrid().with_model(_strong_model())
    res = run_backtest(strat, bars, FlatCosts(), symbol="XAUUSD", params={},
                       contract_size=100.0, timeframe="H1",
                       sizing=RiskSizing(risk_fraction=0.005),
                       vol_min=0.01, vol_step=0.01, vol_max=10.0)
    # 手数 = lots_for_risk(初始资金, rf, SL_ATR×信号bar ATR, cs, vol)
    # ——与累计盈亏无关（basis="initial"；逐笔复算比对）
    from mt5quant.backtest.engine_a import atr_series
    from mt5quant.sizing import lots_for_risk
    atr = atr_series(bars).to_numpy()
    pos = np.array(pd.Index(bars["time"]).get_indexer(
        pd.to_datetime(res.trades["entry_time"])))
    expected = [lots_for_risk(
        10_000.0, 0.005, DemoSmallGrid.SL_ATR * float(atr[k - 1]),
        100.0, 0.01, 0.01, 10.0) for k in pos]
    actual = res.trades["volume_lots"].to_numpy(dtype=float)
    assert np.allclose(actual, expected, rtol=1e-9)


def test_s5_refit_same_instance_twice():
    """S5：同一实例重复拟合不崩且模型相同（成本不再写实例状态）。"""
    bars = synth_bars(3000, seed=17)
    s = DemoSmallGrid()
    kw = dict(train_end=pd.Timestamp("2020-03-01"), purge_bars=10)
    meta = {"__fit__": {"sizes": {"XAUUSD": 100.0},
                        "policies": {"XAUUSD": PERMISSIVE}}}
    m1 = s.fit({"XAUUSD": bars}, {"XAUUSD": FlatCosts()}, meta, **kw)
    m2 = s.fit({"XAUUSD": bars}, {"XAUUSD": FlatCosts()}, meta, **kw)
    assert m1 == m2


def test_s8_fit_requires_sizes_and_policy():
    bars = synth_bars(3000, seed=17)
    s = DemoSmallGrid()
    with pytest.raises(ValueError, match="合约大小"):
        s.fit({"XAUUSD": bars}, {"XAUUSD": FlatCosts()},
              {"__fit__": {"policies": {"XAUUSD": PERMISSIVE}}},
              train_end=pd.Timestamp("2020-03-01"), purge_bars=10)
    # 缺政策回退仓库 sessions.toml（不再按'不限制'）；仓库会话文件被
    # 指向不存在路径时才报错——用 monkeypatch 桩掉默认路径验证报错支路
    from mt5data import sessions as sess_mod
    import pytest as _pytest
    orig = sess_mod.load_session_policy
    sess_mod.load_session_policy = lambda *a, **k: (_ for _ in ()).throw(
        sess_mod.SessionConfigError("桩：不可加载"))
    try:
        with pytest.raises(ValueError, match="入场政策"):
            s.fit({"XAUUSD": bars}, {"XAUUSD": FlatCosts()},
                  {"__fit__": {"sizes": {"XAUUSD": 100.0}}},
                  train_end=pd.Timestamp("2020-03-01"), purge_bars=10)
    finally:
        sess_mod.load_session_policy = orig


def test_d2_choose_among_threshold_passers_and_bidirectional_skip():
    """D2：最高 EV 不过门槛时取**满足门槛**者；同动作双向同 EV → 不做。"""
    from mt5quant.prob.tables import CellEstimate

    def cell(p_win):
        return CellEstimate(n=10_000, p_win=p_win, p_loss=1 - p_win - 0.05,
                            p_time=0.05, r_win=1.9, r_loss=-1.05, r_time=0.1,
                            se_win=0.005, se_loss=0.005)

    class TwoAction(DemoSmallGrid):
        LEVEL_GRID = {"XAUUSD": 2.0}
        HORIZON_BARS = 8
        APPROACH = Approach(lookback_bars=12, min_distance_atr=1.0,
                            touch_atr=0.25)
        RETEST_WINDOW_BARS = 4

    rows_a3 = [("win", 1.9)] * 500 + [("loss", -1.05)] * 500   # A3 EV 高
    labels = pd.DataFrame({
        "situation": "S1", "action": "A3",
        "outcome": [o for o, _ in rows_a3],
        "net_r": [r for _, r in rows_a3],
        "trend_sma200": "na", "atr_pct_2000": "na"})
    # 补 A1 的样本：EV 低但过门槛（赢率 0.42）
    rows_a1 = [("win", 1.9)] * 420 + [("loss", -1.05)] * 580
    labels = pd.concat([labels, pd.DataFrame({
        "situation": "S1", "action": "A1",
        "outcome": [o for o, _ in rows_a1],
        "net_r": [r for _, r in rows_a1],
        "trend_sma200": "na", "atr_pct_2000": "na"})], ignore_index=True)
    tbl = ProbTable.fit(labels, group_by=["trend_sma200", "atr_pct_2000"],
                        prior_strength=100, min_cell=100)
    model = {"prob_version": "1.0.0", "prob_table": tbl.to_dict(),
             "bracket": {"sl_atr": 1.0, "tp_atr": 2.0, "horizon_bars": 8},
             # A3 成本高到不过门槛（EV≈+0.7R < 2×0.5R），A1 过（≈+0.16R ≥ 2×0.02）
             "cost_r_by_situation_action": {"S1|A3": 0.5, "S1|A1": 0.02}}
    strat = TwoAction().with_model(model)
    # 序列同时产生 A3 与 A1 触发：锚点收盘未站上（A3 做空）、此前 A1
    # 已突破（A1 做多出现在锚点后）——用链式序列：40 根 A3、之后上移
    # 产生 A1。构造：40 根平台+锚（A3@40）；随后大幅上破（A1@41）
    rows = ([(98.5, 99.0, 98.0, 98.5)] * 40 + [(99.5, 99.9, 99.0, 99.0)]
            + [(99.6, 100.4, 99.5, 100.3)]      # 41：收盘 > 价位+0.25ATR → A1
            + [(98.5, 99.0, 98.0, 98.5)] * 30)
    bars = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    bars.insert(0, "time",
                pd.date_range("2024-01-02", periods=len(rows), freq="1h"))
    bars["tick_volume"], bars["spread"], bars["real_volume"] = 10, 5, 0
    bars = bars[_COLUMNS]
    sig = strat.generate_signals(bars, {}).to_numpy()
    # A3（bar 40）不过门槛 → 不做；A1（bar 41）过门槛 → 做多
    assert sig[41] == 1, sig[38:46]
    assert sig[40] == 0


def test_d2_same_action_opposite_directions_skip():
    """D2：同一根上同一动作两个方向（阻力 A3 做空 + 支撑 A3 做多）都过
    门槛且 EV 相同（同一格）→ 不做。"""
    strat = DemoSmallGrid().with_model(_strong_model())
    # 走廊 101±0.2（ATR≈0.4）：同时满足 100 支撑与 102 阻力的接近条件
    # （M=12 根都在带外同侧且距价位 ≥ 0.4）；末根 high=102.0、low=100.0
    # 同时触两带 → 同 bar 两个 A3（一空一多），同格同 EV → 放弃。
    rows = [(100.8, 101.2, 100.8, 101.0)] * 30   # ATR14 预热 + 12 根窗口
    rows.append((101.0, 102.0, 100.0, 101.0))
    rows += [(100.8, 101.2, 100.8, 101.0)] * 20
    bars = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    bars.insert(0, "time",
                pd.date_range("2024-01-02", periods=len(rows), freq="1h"))
    bars["tick_volume"], bars["spread"], bars["real_volume"] = 10, 5, 0
    bars = bars[_COLUMNS]
    from mt5quant.strategies.library.prob.situations import (
        Approach, find_level_events, s1_level_rows)
    from mt5quant.backtest.engine_a import atr_series
    atr = atr_series(bars)
    ap = DemoSmallGrid.APPROACH
    levels = s1_level_rows(bars, grid_step=2.0, approach=ap, atr=atr)
    evs = find_level_events(bars, levels, situation="S1", approach=ap,
                            atr=atr)
    assert {(e.kind, e.level) for e in evs} == {
        ("resistance", 102.0), ("support", 100.0)}   # 双向确在
    sig = strat.generate_signals(bars, {}).to_numpy()
    assert (sig == 0).all()          # 同动作双向同 EV → 不做


def test_e3_cross_situation_opposite_directions_skip():
    """E3：同一根上 S1 支撑（A3 做多）与 S3 通道阻力（A3 做空）都过
    门槛——跨局面的方向冲突也不做（蓝图 §2.5 第 4 条）。"""
    from mt5quant.strategies.library.prob.situations import s3_level_rows

    class S1S3(DemoSmallGrid):
        SITUATIONS = ("S1", "S3")

    corridor = (100.8, 101.2, 100.8, 101.0)          # TR=0.4 → ATR≈0.4
    rows = [corridor] * 500
    rows.append((100.8, 103.0, 100.8, 101.0))        # 500：造出通道高点 103
    rows += [corridor] * 59                          # 501..559
    rows.append((101.0, 103.0, 100.0, 101.0))        # 560：双触
    rows += [corridor] * 30
    bars = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    bars.insert(0, "time",
                pd.date_range("2024-01-02", periods=len(rows), freq="1h"))
    bars["tick_volume"], bars["spread"], bars["real_volume"] = 10, 5, 0
    bars = bars[_COLUMNS]
    # 前置核对：S3 通道阻力=103（含 bar500 的窗口）、S1 网格支撑=100，
    # bar 560 同时触发两个事件且方向相反
    atr = atr_series(bars)
    ch = s3_level_rows(bars)
    assert ch["chan_high"].iloc[560] == 103.0
    from mt5quant.strategies.library.prob.situations import (
        find_level_events, s1_level_rows)
    levels_s1 = s1_level_rows(bars, grid_step=2.0,
                              approach=DemoSmallGrid.APPROACH, atr=atr)
    ev_s1 = find_level_events(bars, levels_s1, situation="S1",
                              approach=DemoSmallGrid.APPROACH, atr=atr)
    from mt5quant.prob.counting import level_rows_for_situation
    levels_s3 = level_rows_for_situation(bars, "S3")
    ev_s3 = find_level_events(bars, levels_s3, situation="S3",
                              approach=DemoSmallGrid.APPROACH, atr=atr)
    assert any(e.bar == 560 and e.kind == "support" and e.level == 100.0
               for e in ev_s1)
    assert any(e.bar == 560 and e.kind == "resistance" and e.level == 103.0
               for e in ev_s3)
    model = dict(_strong_model())
    model["cost_r_by_situation_action"] = {"S1|A3": 0.001, "S3|A3": 0.001}
    strat = S1S3().with_model(model)
    sig = strat.generate_signals(bars, {}).to_numpy()
    assert (sig == 0).all()          # 双向过门槛 → 不做


def test_e4_no_cost_key_excluded_from_decision():
    """E4：模型缺 (S3, A3) 成本键时该候选不参与决策（门槛不按 0 计）——
    与上一条同一构造，但成本只给 S1|A3：S3 空头被排除，S1 多头照常入场。"""
    from mt5quant.strategies.library.prob.situations import s1_level_rows

    class S1S3(DemoSmallGrid):
        SITUATIONS = ("S1", "S3")

    corridor = (100.8, 101.2, 100.8, 101.0)
    rows = [corridor] * 500
    rows.append((100.8, 103.0, 100.8, 101.0))
    rows += [corridor] * 59
    rows.append((101.0, 103.0, 100.0, 101.0))
    rows += [corridor] * 30
    bars = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    bars.insert(0, "time",
                pd.date_range("2024-01-02", periods=len(rows), freq="1h"))
    bars["tick_volume"], bars["spread"], bars["real_volume"] = 10, 5, 0
    bars = bars[_COLUMNS]
    model = dict(_strong_model())
    model["cost_r_by_situation_action"] = {"S1|A3": 0.001}   # S3 无成本键
    strat = S1S3().with_model(model)
    sig = strat.generate_signals(bars, {}).to_numpy()
    nz = np.flatnonzero(sig != 0)
    assert len(nz) == DemoSmallGrid.HORIZON_BARS
    assert sig[nz[0]] == 1              # 只剩 S1 支撑的 A3 多头


# ------------------------------------------------- P-07（模块乙）：获准动作
# 白名单、exit_rules 子类重建、单品种绑定与 with_model 配置核对


class OnlyA3(DemoSmallGrid):
    """P-07 测试：仅 A3 获准。"""

    ALLOWED_HYPOTHESES = ("A3",)


class AllAllowed(DemoSmallGrid):
    """P-07 测试：显式空白名单 = 全部 A1–A4 照常（兼容缺省）。"""

    ALLOWED_HYPOTHESES = ()


def _two_action_model():
    """A1 过门槛（EV≈+0.19 ≥ 2×0.02）、A3 不过（EV≈+0.70 < 2×0.5）。"""
    rows_a3 = [("win", 1.9)] * 500 + [("loss", -1.05)] * 500
    rows_a1 = [("win", 1.9)] * 420 + [("loss", -1.05)] * 580
    labels = pd.DataFrame({
        "situation": ["S1"] * (len(rows_a3) + len(rows_a1)),
        "action": ["A3"] * len(rows_a3) + ["A1"] * len(rows_a1),
        "outcome": [o for o, _ in rows_a3 + rows_a1],
        "net_r": [r for _, r in rows_a3 + rows_a1],
        "trend_sma200": "na", "atr_pct_2000": "na"})
    tbl = ProbTable.fit(labels, group_by=["trend_sma200", "atr_pct_2000"],
                        prior_strength=100, min_cell=100)
    return {"prob_version": "1.0.0", "prob_table": tbl.to_dict(),
            "bracket": {"sl_atr": 1.0, "tp_atr": 2.0, "horizon_bars": 8},
            "cost_r_by_situation_action": {"S1|A3": 0.5, "S1|A1": 0.02}}


def _two_action_bars():
    """bar 40 A3（100 阻力、收盘 99.0 未站上 → 做空）；bar 41 收盘 100.3
    突破 100+0.25×ATR → A1 做多（同 test_d2_choose 的构造）。"""
    rows = ([(98.5, 99.0, 98.0, 98.5)] * 40 + [(99.5, 99.9, 99.0, 99.0)]
            + [(99.6, 100.4, 99.5, 100.3)]
            + [(98.5, 99.0, 98.0, 98.5)] * 30)
    bars = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    bars.insert(0, "time",
                pd.date_range("2024-01-02", periods=len(rows), freq="1h"))
    bars["tick_volume"], bars["spread"], bars["real_volume"] = 10, 5, 0
    return bars[_COLUMNS]


def test_p07_whitelist_blocks_higher_ev_a1():
    """P-07：仅 A3 获准但 A1 EV 更高且过门槛 → 不交易 A1（generate_signals
    候选收集过滤）；空白名单 → A1 照常（兼容）。"""
    model = _two_action_model()
    bars = _two_action_bars()
    sig = OnlyA3().with_model(model).generate_signals(bars, {}).to_numpy()
    assert (sig == 0).all()
    # 对照：()=全部允许 → A1 入场（既有 D2 行为不变）
    sig2 = AllAllowed().with_model(model).generate_signals(
        bars, {}).to_numpy()
    assert sig2[41] == 1, sig2[38:46]


def test_p07_whitelisted_action_still_trades():
    """P-07：白名单内的 A3 照常入场（过滤不是全灭）。"""
    rows = [(98.5, 99.0, 98.0, 98.5)] * 40
    rows.append((99.5, 99.9, 99.0, 99.0))     # 40：A3 空头触发
    rows += [(98.5, 99.0, 98.0, 98.5)] * 30
    bars = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    bars.insert(0, "time",
                pd.date_range("2024-01-02", periods=len(rows), freq="1h"))
    bars["tick_volume"], bars["spread"], bars["real_volume"] = 10, 5, 0
    sig = OnlyA3().with_model(_strong_model()).generate_signals(
        bars[_COLUMNS], {}).to_numpy()
    nz = np.flatnonzero(sig != 0)
    assert len(nz) == DemoSmallGrid.HORIZON_BARS
    assert sig[40] == -1                       # A3（阻力反转）做空


def _model_actions(model) -> set:
    t = model["prob_table"]
    acts = set(t["base_rates"])
    acts |= {p["action"] for p in t["parents"]}
    acts |= {c["action"] for c in t["cells"]}
    return acts


def test_p07_fit_filters_labels_and_costs_to_whitelist(fitted):
    """P-07：fit 的标签循环双重过滤——白名单外动作不进概率表与成本表。"""
    bars, m_base = fitted
    meta = {"__fit__": {"sizes": {"XAUUSD": 100.0},
                        "policies": {"XAUUSD": PERMISSIVE}}}
    kw = dict(train_end=pd.Timestamp("2020-06-01"), purge_bars=10)
    m_a3 = OnlyA3().fit({"XAUUSD": bars}, {"XAUUSD": FlatCosts()}, meta, **kw)
    assert _model_actions(m_a3) <= {"A3"}
    assert set(m_a3["cost_r_by_situation_action"]) == {"S1|A3"}
    # 过滤确实生效：同一数据未过滤时本就有其他动作样本
    assert _model_actions(m_base) - {"A3"}
    # 空白名单 = 不过滤（模型逐位一致）
    m_all = AllAllowed().fit({"XAUUSD": bars}, {"XAUUSD": FlatCosts()},
                             meta, **kw)
    assert m_all == m_base


def test_p07_subclass_sl_tp_rebuilds_exit_rules():
    """P-07：子类覆写 SL/TP 后 exit_rules 按最终常量重建（不再停在基类
    类体求值的 1.0/2.0）；常量未变的既有类不受影响。"""
    class WideBracket(DemoSmallGrid):
        SL_ATR = 2.0
        TP_ATR = 3.0

    assert WideBracket.exit_rules.stop_loss_atr == 2.0
    assert WideBracket.exit_rules.take_profit_atr == 3.0
    assert WideBracket.exit_rules.reentry == "after_signal_change"
    assert WideBracket.exit_rules.time_stop_bars is None
    assert WideBracket.exit_rules.week_close_exit is False
    assert DemoSmallGrid.exit_rules.stop_loss_atr == 1.0
    assert LevelDecisionS1Demo.exit_rules.take_profit_atr == 2.0
    assert LevelDecisionV1.exit_rules.stop_loss_atr == 1.0


def test_p07_wide_bracket_labels_and_backtest_agree():
    """P-07：SL=2.0/TP=3.0 子类的标签 bracket 与引擎止损价都按子类常量
    执行（标签与回测同口径）。"""
    class WideBracket(DemoSmallGrid):
        SL_ATR = 2.0
        TP_ATR = 3.0

    bars = synth_bars(3000, seed=7)
    meta = {"__fit__": {"sizes": {"XAUUSD": 100.0},
                        "policies": {"XAUUSD": PERMISSIVE}}}
    model = WideBracket().fit({"XAUUSD": bars}, {"XAUUSD": FlatCosts()},
                              meta, train_end=pd.Timestamp("2020-02-01"),
                              purge_bars=10)
    assert model["bracket"] == {"sl_atr": 2.0, "tp_atr": 3.0,
                                "horizon_bars": 8}
    # 引擎：anchor(40) A3 做空 → 41 开盘入场、当根 high 穿 2×ATR 止损
    rows = [(98.5, 99.0, 98.0, 98.5)] * 40
    rows.append((99.5, 99.9, 99.0, 99.0))          # 40：A3 空头触发
    rows.append((99.5, 102.0, 99.4, 99.8))         # 41：开盘入、盘中穿止损
    rows += [(98.5, 99.0, 98.0, 98.5)] * 30
    bars2 = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    bars2.insert(0, "time",
                 pd.date_range("2024-01-02", periods=len(rows), freq="1h"))
    bars2["tick_volume"], bars2["spread"], bars2["real_volume"] = 10, 5, 0
    bars2 = bars2[_COLUMNS]
    wide_model = dict(_strong_model(),
                      bracket={"sl_atr": 2.0, "tp_atr": 3.0,
                               "horizon_bars": 8})
    strat = WideBracket().with_model(wide_model)    # bracket 一致才装得进
    res = run_backtest(strat, bars2, FlatCosts(), symbol="XAUUSD", params={},
                       contract_size=100.0, timeframe="H1", volume_lots=1.0)
    stops = res.trades[res.trades["exit_reason"] == "stop"]
    assert len(stops) == 1, res.trades
    t = stops.iloc[0]
    entry_atr = float(atr_series(bars2).to_numpy()[40])   # 信号 bar 的 ATR
    # 空头止损成交价 = sl + slip（_close：bid 基准 sl−spr 再加回 spr+slip）
    assert t["exit_price"] == pytest.approx(
        t["entry_price"] + 2.0 * entry_atr + 0.01)
    # 区别于 SL=1.0 的口径（确认 2.0 生效而非碰巧）
    assert abs(t["exit_price"]
               - (t["entry_price"] + 1.0 * entry_atr + 0.01)) > 0.5


def test_p07_with_model_rejects_config_mismatch():
    """P-07：with_model 核对模型 symbols/timeframe/bracket 与类常量一致
    （模型里的值若存在且不匹配 → ValueError；缺省字段跳过）。"""
    good = _strong_model()
    DemoSmallGrid().with_model(good)              # 缺省字段不视为不匹配
    DemoSmallGrid().with_model(dict(good, symbols=["XAUUSD"],
                                    timeframe="H1"))   # 一致 → 不抛
    with pytest.raises(ValueError, match="bracket"):
        DemoSmallGrid().with_model(
            dict(good, bracket={"sl_atr": 1.5, "tp_atr": 2.0,
                                "horizon_bars": 8}))
    with pytest.raises(ValueError, match="symbols"):
        DemoSmallGrid().with_model(dict(good, symbols=["EURUSD"]))
    with pytest.raises(ValueError, match="timeframe"):
        DemoSmallGrid().with_model(dict(good, timeframe="M15"))


def test_p07_multi_symbol_class_rejected():
    """P-07：SYMBOLS 多品种 → fit/generate_signals 入口拒绝（单品种实例
    绑定——generate_signals 只按 SYMBOLS[0] 的网格评估）。"""
    class MultiSym(DemoSmallGrid):
        SYMBOLS = ("XAUUSD", "EURUSD")

    bars = synth_bars(3000, seed=5)
    meta = {"__fit__": {"sizes": {"XAUUSD": 100.0},
                        "policies": {"XAUUSD": PERMISSIVE}}}
    with pytest.raises(ValueError, match="单品种"):
        MultiSym().fit({"XAUUSD": bars}, {"XAUUSD": FlatCosts()}, meta,
                       train_end=pd.Timestamp("2020-02-01"), purge_bars=10)
    with pytest.raises(ValueError, match="单品种"):
        MultiSym().with_model(_strong_model()).generate_signals(bars, {})


def test_p07_unknown_hypothesis_rejected_at_definition():
    """P-07：白名单含未知动作 → 类定义即抛（防手误静默禁掉全部动作）。"""
    with pytest.raises(ValueError, match="ALLOWED_HYPOTHESES"):
        class TypoAllowed(DemoSmallGrid):
            ALLOWED_HYPOTHESES = ("a3",)


# ================================================ P2-01/P2-02（T104 批 2 模块甲）
# audit2 §3.3：白名单升级（局面×动作）+ 实例真实 ExitRules 核验 + 白名单进
# 模型身份；generate_signals 决策起点状态机。失败测试先行于实现提交。


def test_p201_hypothesis_pairs_helper():
    """P2-01：卡假设（S1:A3:fade>base 形式）→（局面,动作）对解析 helper
    （bt checker 与策略共用；从本模块导出）。"""
    from mt5quant.strategies.library.prob.level_decision import (
        parse_hypothesis_pairs,
    )
    got = parse_hypothesis_pairs(["S1:A3:fade>base", "S2:A1:follow>base"])
    assert got == frozenset({("S1", "A3"), ("S2", "A1")})
    with pytest.raises(ValueError, match="局面"):
        parse_hypothesis_pairs(["S9:A3:fade>base"])
    with pytest.raises(ValueError, match="动作"):
        parse_hypothesis_pairs(["S1:A9:fade>base"])
    with pytest.raises(ValueError, match="形式"):
        parse_hypothesis_pairs(["S1"])


def test_p201_pair_whitelist_scopes_situation():
    """P2-01：白名单条目 'S1:A3' 只放行 S1×A3——S1:A1 不放行（动作外），
    'S2:A3' 也不放行 S1 的 A3（局面外——旧实现只比动作）。"""
    model = _two_action_model()           # A1@41 过门槛、A3@40 不过
    bars = _two_action_bars()

    class OnlyS1A3(DemoSmallGrid):
        ALLOWED_HYPOTHESES = ("S1:A3",)

    class OnlyS2A3(DemoSmallGrid):
        ALLOWED_HYPOTHESES = ("S2:A3",)

    class OnlyS1A1(DemoSmallGrid):
        ALLOWED_HYPOTHESES = ("S1:A1",)

    sig = OnlyS1A3().with_model(model).generate_signals(bars, {}).to_numpy()
    assert (sig == 0).all()               # A1 被（局面×动作）集合挡住
    sig = OnlyS2A3().with_model(model).generate_signals(bars, {}).to_numpy()
    assert (sig == 0).all()               # S2:A3 不顺带放行 S1:A3
    sig = OnlyS1A1().with_model(model).generate_signals(bars, {}).to_numpy()
    assert sig[41] == 1, sig[38:46]       # 白名单内的 S1:A1 照常入场


def test_p201_legacy_action_entry_still_allowed():
    """P2-01 兼容：旧式裸动作条目 'A3' = 任意局面下的 A3（OnlyA3 语义不
    变——既有 P-07 测试与探针夹具依赖）。"""
    model = _two_action_model()
    bars = _two_action_bars()

    class AnySituationA3(DemoSmallGrid):
        ALLOWED_HYPOTHESES = ("A3",)

    sig = AnySituationA3().with_model(model).generate_signals(
        bars, {}).to_numpy()
    assert (sig == 0).all()               # A1 仍被挡（动作外）
    # 白名单内动作照常（test_p07_whitelisted_action_still_trades 的同型）


def test_p201_bad_pair_rejected_at_definition():
    """P2-01：白名单条目 'S9:A3'/'S1:a3' → 类定义即抛（局面/动作都校验）。"""
    with pytest.raises(ValueError, match="ALLOWED_HYPOTHESES"):
        class BadSituation(DemoSmallGrid):
            ALLOWED_HYPOTHESES = ("S9:A3",)
    with pytest.raises(ValueError, match="ALLOWED_HYPOTHESES"):
        class BadAction(DemoSmallGrid):
            ALLOWED_HYPOTHESES = ("S1:a3",)


def test_p201_inconsistent_exit_rules_rejected_at_definition():
    """P2-01：显式覆写 exit_rules 与自身 bracket 不一致 → 类定义即拒绝
    （不再'尊重'——标签 bracket 与引擎出场脱节；探针 whitelist 的 WrongExit
    即此形态）。一致覆写（与自身常量推导相等）与 time_stop/week_close 覆写
    分别验证。"""
    with pytest.raises(ValueError, match="exit_rules"):
        class WrongExit(DemoSmallGrid):
            exit_rules = ExitRules(stop_loss_atr=9.0, take_profit_atr=10.0,
                                   reentry="after_signal_change")
    with pytest.raises(ValueError, match="exit_rules"):
        class TimeStopExit(DemoSmallGrid):
            exit_rules = ExitRules(stop_loss_atr=1.0, take_profit_atr=2.0,
                                   time_stop_bars=5,
                                   reentry="after_signal_change")

    class ConsistentExit(DemoSmallGrid):
        SL_ATR = 2.0
        TP_ATR = 3.0
        exit_rules = ExitRules(stop_loss_atr=2.0, take_profit_atr=3.0,
                               reentry="after_signal_change")

    assert ConsistentExit.exit_rules.stop_loss_atr == 2.0   # 一致 → 接受


def test_p201_instance_exit_rules_tamper_rejected():
    """P2-01：实例层改 exit_rules（引擎实际消费的属性）与 bracket 不一致
    → fit / generate_signals 入口拒绝（不只比类常量）。"""
    bars = _two_action_bars()
    strat = DemoSmallGrid().with_model(_strong_model())
    strat.exit_rules = ExitRules(stop_loss_atr=9.0, take_profit_atr=10.0,
                                 reentry="after_signal_change")
    with pytest.raises(ValueError, match="exit_rules"):
        strat.generate_signals(bars, {})
    bars4k = synth_bars(3000, seed=17)
    meta = {"__fit__": {"sizes": {"XAUUSD": 100.0},
                        "policies": {"XAUUSD": PERMISSIVE}}}
    tampered_fit = DemoSmallGrid()
    tampered_fit.exit_rules = ExitRules(stop_loss_atr=9.0, take_profit_atr=10.0,
                                        reentry="after_signal_change")
    with pytest.raises(ValueError, match="exit_rules"):
        tampered_fit.fit({"XAUUSD": bars4k}, {"XAUUSD": FlatCosts()}, meta,
                         train_end=pd.Timestamp("2020-03-01"),
                         purge_bars=10)


def test_p201_model_records_allowed_pairs_and_with_model_checks():
    """P2-01：fit 把获准（局面×动作）集合写进模型 dict（allowed_hypotheses
    键）；with_model 核对模型集合与类常量一致——不一致（装错卡）拒绝；
    旧模型无该键 → 按'全部'处理不拒绝（向后兼容，identity v1 冻结件）。"""
    bars = synth_bars(4000, seed=42)
    meta = {"__fit__": {"sizes": {"XAUUSD": 100.0},
                        "policies": {"XAUUSD": PERMISSIVE}}}
    kw = dict(train_end=pd.Timestamp("2020-06-01"), purge_bars=10)

    class OnlyS1A3(DemoSmallGrid):
        ALLOWED_HYPOTHESES = ("S1:A3",)

    m = OnlyS1A3().fit({"XAUUSD": bars}, {"XAUUSD": FlatCosts()}, meta, **kw)
    assert m["allowed_hypotheses"] == ["S1:A3"]
    OnlyS1A3().with_model(m)                      # 一致 → 装载
    m_base = DemoSmallGrid().fit({"XAUUSD": bars}, {"XAUUSD": FlatCosts()},
                                 meta, **kw)
    assert m_base["allowed_hypotheses"] == []     # 空白名单 = 全部（兼容值）
    with pytest.raises(ValueError, match="allowed_hypotheses"):
        OnlyS1A3().with_model(m_base)             # 全部 ⊅ S1:A3 → 拒绝
    with pytest.raises(ValueError, match="allowed_hypotheses"):
        DemoSmallGrid().with_model(m)             # S1:A3 模型装空白名单卡 → 拒绝
    # 旧模型（批 1 冻结件）无该键 → 不拒绝（按'全部'处理并注明）
    legacy = _strong_model()
    assert "allowed_hypotheses" not in legacy
    DemoSmallGrid().with_model(legacy)
    OnlyS1A3().with_model(legacy)


def test_p201_fit_filters_labels_by_pair():
    """P2-01：fit 的标签/成本过滤按（局面×动作）对——S2:A3 条目不放行
    S1 的 A3（fit 后模型无任何 S1:A3 之外的格），legacy 'A3' 仍放行 S1:A3。"""
    bars = synth_bars(3000, seed=17)
    meta = {"__fit__": {"sizes": {"XAUUSD": 100.0},
                        "policies": {"XAUUSD": PERMISSIVE}}}
    kw = dict(train_end=pd.Timestamp("2020-03-01"), purge_bars=10)

    class OnlyS2A3(DemoSmallGrid):
        ALLOWED_HYPOTHESES = ("S2:A3",)

    with pytest.raises(ValueError, match="没有可用行"):
        OnlyS2A3().fit({"XAUUSD": bars}, {"XAUUSD": FlatCosts()}, meta, **kw)

    class LegacyA3(DemoSmallGrid):
        ALLOWED_HYPOTHESES = ("A3",)

    m = LegacyA3().fit({"XAUUSD": bars}, {"XAUUSD": FlatCosts()}, meta, **kw)
    assert _model_actions(m) <= {"A3"}
    assert m["allowed_hypotheses"] == ["*:A3"]    # 裸动作 → 任意局面


# ------------------------------------------------------- P2-02 决策起点


def _trigger_patched(monkeypatch, trig_bars, situation="S1", action="A3",
                     side=1):
    """把触发钉在给定 bar 上（掩掉局面/动作发现——P2-02 状态机单元测试）。"""
    import mt5quant.strategies.library.prob.level_decision as ld
    trig = pd.DataFrame({"bar": list(trig_bars),
                         "situation": [situation] * len(trig_bars),
                         "action": [action] * len(trig_bars),
                         "side": [side] * len(trig_bars)})
    monkeypatch.setattr(ld, "level_rows_for_situation", lambda *a, **k: [])
    monkeypatch.setattr(ld, "find_level_events", lambda *a, **k: [])
    monkeypatch.setattr(ld, "action_triggers", lambda *a, **k: trig)


def test_p202_decision_start_fills_first_post_split_trigger(monkeypatch):
    """P2-02（探针 boundary_state 场景的正确行为）：训练段触发（bar40）+
    split（bar41）后第一根合法新触发（bar42）→ 该触发成交——起点前不进
    held_until/forced_zero 状态机，bar42 是全新决策段起点（现行输出掩码
    误杀该触发；本测试在**策略层**直接断言成交，不经 bt 掩码层）。"""
    data = synth_bars(80, seed=1)
    split = pd.Timestamp(data["time"].iloc[41])
    _trigger_patched(monkeypatch, [40, 42])
    strat = DemoSmallGrid().with_model(_strong_model())
    sig = strat.generate_signals(data, {}, decision_start=split).to_numpy()
    assert sig[40] == 0 and sig[41] == 0        # 起点前：只算特征，不建仓
    k = DemoSmallGrid.HORIZON_BARS
    assert sig[42:42 + k].tolist() == [1] * k   # bar42 新决策段恰 K 根
    assert sig[42 + k] == 0                     # 其后归零（不链式）


def test_p202_decision_start_only_pre_split_trigger_no_trade(monkeypatch):
    """P2-02：split 前触发+split 后无触发 → 不交易（起点前触发不产生任何
    持仓状态或尾巴）。"""
    data = synth_bars(80, seed=2)
    split = pd.Timestamp(data["time"].iloc[41])
    _trigger_patched(monkeypatch, [40])
    strat = DemoSmallGrid().with_model(_strong_model())
    sig = strat.generate_signals(data, {}, decision_start=split).to_numpy()
    assert (sig == 0).all()


def test_p202_training_tail_state_does_not_leak(monkeypatch):
    """P2-02：训练段尾部的持有/forced_zero 不影响验证段——bar39 触发的
    持有本会压到 bar46、forced_zero 压 bar47；起点=bar41 后 bar47 的新触发
    照常成交（空状态处理）。"""
    data = synth_bars(80, seed=3)
    split = pd.Timestamp(data["time"].iloc[41])
    _trigger_patched(monkeypatch, [39, 47])
    strat = DemoSmallGrid().with_model(_strong_model())
    sig = strat.generate_signals(data, {}, decision_start=split).to_numpy()
    assert sig[39:47].tolist() == [0] * 8       # 训练段尾巴不泄漏
    k = DemoSmallGrid.HORIZON_BARS
    assert sig[47:47 + k].tolist() == [1] * k   # bar47 新段（无视 forced_zero）


def test_p202_decision_start_before_data_equals_full_history(monkeypatch):
    """P2-02 兼容：起点早于序列首根 → 与不传起点逐位一致（全历史决策）。"""
    data = synth_bars(80, seed=4)
    _trigger_patched(monkeypatch, [40, 55])
    strat = DemoSmallGrid().with_model(_strong_model())
    plain = strat.generate_signals(data, {}).to_numpy()
    early = strat.generate_signals(
        data, {}, decision_start=pd.Timestamp("2019-01-01")).to_numpy()
    assert (plain == early).all()
    assert plain.any()                          # 夹具确有触发


def test_p202_set_decision_start_and_reset(monkeypatch):
    """P2-02 实例入口：set_decision_start（适配层可链式设置）与 decision_start
    参数等价；reset_decision_state() 清回全历史语义；with_decision_start
    返回新实例不污染原实例。"""
    data = synth_bars(80, seed=5)
    split = pd.Timestamp(data["time"].iloc[41])
    _trigger_patched(monkeypatch, [40, 42])
    strat = DemoSmallGrid().with_model(_strong_model())
    via_kwarg = strat.generate_signals(data, {}, decision_start=split).to_numpy()
    strat.set_decision_start(split)
    via_setter = strat.generate_signals(data, {}).to_numpy()
    assert (via_kwarg == via_setter).all()
    strat.reset_decision_state()
    plain = strat.generate_signals(data, {}).to_numpy()
    assert plain[40] != 0                       # 全历史：bar40 照常入场
    fresh = strat.with_decision_start(split)
    assert (fresh.generate_signals(data, {}).to_numpy() == via_kwarg).all()
    assert (strat.generate_signals(data, {}).to_numpy() == plain).all()


def test_p202_generate_signals_pure_across_calls(monkeypatch):
    """P2-02：决策状态机不跨调用泄漏——同一实例重复调用输出逐位一致
    （引擎/G0 截断一致性依赖纯函数语义）。"""
    data = synth_bars(80, seed=6)
    _trigger_patched(monkeypatch, [40, 42])
    strat = DemoSmallGrid().with_model(_strong_model())
    a = strat.generate_signals(data, {}).to_numpy()
    b = strat.generate_signals(data, {}).to_numpy()
    assert (a == b).all()
