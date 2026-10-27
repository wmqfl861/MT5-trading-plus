"""G2 稳健性关卡：成本放大、参数邻域、按年分段、跨品种、随机入场基准、
买入持有基准、去最好 5% 交易、延迟一根 bar。

计划失败测试落点：
- 故意过拟合策略（噪声上挑最优参数）→ 参数邻域高原失败（test_run_g2_overfit…）；
- 随机信号策略 → 过不了随机入场基准（test_random_signal_fails_random_entry…）。
"""
import dataclasses

import numpy as np
import pandas as pd
import pytest

from mt5quant.backtest.engine_a import run_backtest
from mt5quant.backtest.report import summarize
from mt5quant.strategies import BaseStrategy, ParamSpec, RequiredData
from mt5quant.strategies.base import ExitRules
from mt5quant.validation import robustness as rob
from mt5quant.validation.gates import load_gates


@pytest.fixture(scope="module")
def cfg():
    return load_gates()[0]


class ConstCosts:
    """固定成本替身：点差/滑点为价格单位，佣金/隔夜费为每手账户货币。"""

    def __init__(self, spread=0.0, slippage=0.0, commission=0.0,
                 swap_per_night=0.0):
        self._spread, self._slip = spread, slippage
        self._commission = commission
        self._swap_per_night = swap_per_night

    def spread_p90(self, symbol, hour_utc, *, price=None):
        return self._spread

    def slippage(self, symbol, hour_utc, *, price=None):
        return self._slip

    def commission_per_lot(self, symbol):
        return self._commission

    def overnight_fee(self, symbol, side, from_ts, to_ts):
        days = ((pd.Timestamp(to_ts).normalize()
                 - pd.Timestamp(from_ts).normalize()).days)
        return -abs(self._swap_per_night) * max(0, days)


def make_bars(closes, start="2024-01-01", freq="1h"):
    closes = list(map(float, closes))
    times = pd.date_range(start, periods=len(closes), freq=freq)
    opens = [closes[0]] + closes[:-1]                    # open = 前一根 close
    return pd.DataFrame({
        "time": times, "open": opens,
        "high": [max(o, c) + 0.2 for o, c in zip(opens, closes)],
        "low": [min(o, c) - 0.2 for o, c in zip(opens, closes)],
        "close": closes, "tick_volume": 1, "spread": 1, "real_volume": 0,
    })


def uptrend(n=60, base=100.0, step=0.5):
    return make_bars([base + step * i for i in range(n)])


class LongMid(BaseStrategy):
    """中段做多：入场 bar6 开盘、出至倒数第 5 根（趋势段内）。"""

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        s = pd.Series(0, index=bars.index, dtype="int64")
        s.iloc[5:len(s) - 5] = 1
        return s


class ShortThenLong(LongMid):
    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        s = super().generate_signals(bars, params)
        s.iloc[1] = -1                                  # 有一笔空头 → 非纯多头
        return s


class SpikeSignal(BaseStrategy):
    """bar1 信号 1：原版恰好吃到 bar2 的跳升，延迟一根就吃到下跌。"""

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        s = pd.Series(0, index=bars.index, dtype="int64")
        s.iloc[1] = 1
        return s


class KPicker(BaseStrategy):
    """k=2 做多（赚），k=1/3 做空（亏）：参数邻域检查的确定性夹具。"""

    params_schema = (ParamSpec("k", "int", (1, 2, 3)),)

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        s = pd.Series(0, index=bars.index, dtype="int64")
        s.iloc[5:len(s) // 2] = 1 if params["k"] == 2 else -1
        return s


class SmaK(BaseStrategy):
    """close > SMA(k) 做多：在噪声上挑最优 k 即"故意过拟合"。"""

    params_schema = (ParamSpec("k", "int", tuple(range(2, 22))),)
    required_data = RequiredData(symbols=(), timeframe="", lookback_bars=25)

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        m = bars["close"].rolling(params["k"]).mean()
        return (bars["close"] > m).astype("int64").set_axis(bars.index)


def spec_of(strategy, symbol="TST", timeframe="H1", cs=1.0):
    return rob.BacktestSpec(strategy=strategy, symbol=symbol,
                            timeframe=timeframe, contract_size=cs)


def noise_bars(n=1200, seed=42, s0=100.0, vol=0.004):
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0, vol, n)
    closes = s0 * np.cumprod(1.0 + rets)
    return make_bars(closes)


def random_signal_array(n, n_trades, rng):
    """随机 ±1 入场、持仓 3–12 根、互不重叠的信号（确定性 rng）。"""
    sig = np.zeros(n, dtype=int)
    order = rng.permutation(range(2, n - 14))
    placed = 0
    for start in order:
        if placed >= n_trades:
            break
        d = int(rng.integers(3, 13))
        if sig[max(0, start - 1):start + d + 1].any():
            continue
        sig[start:start + d] = 1 if rng.integers(0, 2) else -1
        placed += 1
    return sig


def tr_row(net, when="2024-05-01 12:00", bars_held=3):
    t = pd.Timestamp(when)
    return {"entry_time": t, "exit_time": t + pd.Timedelta(hours=bars_held),
            "side": "long", "volume_lots": 1.0, "entry_price": 100.0,
            "exit_price": 100.0 + net, "gross": net, "commission": 0.0,
            "slippage_cost": 0.0, "spread_cost": 0.0, "swap": 0.0, "net": net,
            "r_multiple": float("nan"), "bars_held": bars_held,
            "exit_reason": "signal"}


def trades_df(rows):
    return pd.DataFrame(rows, columns=list(tr_row(0.0).keys()))


# ---------------------------------------------------------------- 基础件

def test_scaled_costs_multiplies_every_component():
    base = ConstCosts(spread=0.2, slippage=0.05, commission=1.0,
                      swap_per_night=0.1)
    scaled = rob.ScaledCosts(base, 1.5)
    assert scaled.spread_p90("X", 3, price=10.0) == pytest.approx(0.3)
    assert scaled.slippage("X", 3, price=10.0) == pytest.approx(0.075)
    assert scaled.commission_per_lot("X") == pytest.approx(1.5)
    assert scaled.overnight_fee("X", "long", pd.Timestamp("2024-01-01 01:00"),
                                pd.Timestamp("2024-01-03 01:00")) == pytest.approx(-0.3)


def test_fixed_signals_roundtrip_and_length_guard():
    bars = uptrend(10)
    sig = rob.FixedSignals(np.array([0, 1, 1, 0] + [0] * 6))
    out = sig.generate_signals(bars, {})
    assert out.tolist() == [0, 1, 1, 0, 0, 0, 0, 0, 0, 0]
    with pytest.raises(ValueError, match="长度"):
        sig.generate_signals(bars.iloc[:5], {})


def test_equity_sharpe_flat_is_none():
    bars = uptrend(10)
    assert rob.equity_sharpe(pd.Series(1.0, index=bars.index),
                             bars["time"], "H1") is None


# ---------------------------------------------------------------- 成本放大

def test_cost_scaling_passes_with_small_costs(cfg):
    costs = ConstCosts(spread=0.02, commission=0.5)
    out = rob.cost_scaling_check(spec_of(LongMid()), uptrend(), costs, {}, cfg)
    assert out["passed"] is True
    assert out["expectancy_1_5x"] > 0
    assert "net_2x" in out                    # ×2 只记录备查


def test_cost_scaling_fails_when_costs_dominant(cfg):
    """基准仍盈利（净 +5）但 ×1.5 后为负：成本 20 → 25−20>0、25−30<0。"""
    costs = ConstCosts(spread=10.0, commission=10.0)
    base = run_backtest(LongMid(), uptrend(), costs, symbol="TST", params={},
                        contract_size=1.0, timeframe="H1", volume_lots=1.0)
    assert base.trades["net"].sum() > 0
    out = rob.cost_scaling_check(spec_of(LongMid()), uptrend(), costs, {}, cfg)
    assert out["passed"] is False
    assert out["expectancy_1_5x"] < 0


# ---------------------------------------------------------------- 参数邻域

def test_param_plateau_lonely_peak_fails(cfg):
    costs = ConstCosts(spread=0.05)
    out = rob.param_plateau(spec_of(KPicker()), uptrend(), costs, {"k": 2},
                            {"k": (1, 2, 3)}, cfg)
    assert out["passed"] is False
    assert out["n_neighbors"] == 2
    assert out["fraction"] == 0.0


def test_param_plateau_all_profitable_passes(cfg):
    costs = ConstCosts(spread=0.05)

    class AllWin(KPicker):
        def generate_signals(self, bars, params):
            s = pd.Series(0, index=bars.index, dtype="int64")
            s.iloc[5:len(s) - 5] = 1
            return s

    out = rob.param_plateau(spec_of(AllWin()), uptrend(), costs, {"k": 2},
                            {"k": (1, 2, 3)}, cfg)
    assert out["passed"] is True
    assert out["fraction"] == 1.0


def test_param_plateau_single_grid_point_is_na(cfg):
    out = rob.param_plateau(spec_of(KPicker()), uptrend(), ConstCosts(),
                            {"k": 2}, {"k": (2,)}, cfg)
    assert out["passed"] is True
    assert out["n_neighbors"] == 0


def test_param_plateau_rejects_param_outside_grid(cfg):
    out = rob.param_plateau(spec_of(KPicker()), uptrend(), ConstCosts(),
                            {"k": 7}, {"k": (1, 2, 3)}, cfg)
    assert out["passed"] is False


# ---------------------------------------------------------------- 按年分段

def test_yearly_segments_pass(cfg):
    df = trades_df(
        [tr_row(50.0, f"2024-03-0{i % 9 + 1} 10:00") for i in range(2)]
        + [tr_row(70.0, "2025-06-01 10:00"), tr_row(70.0, "2026-06-01 10:00")])
    out = rob.yearly_segments(df, cfg)
    assert out["passed"] is True
    assert out["profitable_fraction"] == 1.0
    assert out["max_concentration"] <= 0.5


def test_yearly_segments_too_few_profitable_years_fails(cfg):
    df = trades_df([tr_row(100.0, "2024-06-01 10:00"),
                    tr_row(5.0, "2025-06-01 10:00"),
                    tr_row(-10.0, "2026-06-01 10:00")])
    out = rob.yearly_segments(df, cfg)
    assert out["passed"] is False
    assert out["profitable_fraction"] < 0.7


def test_yearly_segments_single_year_dominance_fails(cfg):
    df = trades_df([tr_row(100.0, "2024-06-01 10:00"),
                    tr_row(40.0, "2025-06-01 10:00"),
                    tr_row(40.0, "2026-06-01 10:00")])
    out = rob.yearly_segments(df, cfg)
    assert out["passed"] is False
    assert out["max_concentration"] > 0.5


def test_yearly_segments_nonpositive_total_fails(cfg):
    df = trades_df([tr_row(-30.0, "2024-06-01 10:00"),
                    tr_row(10.0, "2025-06-01 10:00")])
    assert rob.yearly_segments(df, cfg)["passed"] is False


# ---------------------------------------------------------------- T79：known_gaps

def test_yearly_segments_notes_known_gaps_sample_incomplete(cfg):
    """T79/R12-G1：按年分段读到 known_gaps（序列 meta.json 登记、
    read_known_gaps 读取）时，与缺口相交的年份注明"样本不完整"——
    只注记，不改判定。"""
    from datetime import datetime as _dt

    df = trades_df([tr_row(50.0, "2024-03-01 10:00"),
                    tr_row(50.0, "2024-06-01 10:00"),
                    tr_row(70.0, "2025-06-01 10:00"),
                    tr_row(70.0, "2026-06-01 10:00")])
    plain = rob.yearly_segments(df, cfg)
    out = rob.yearly_segments(df, cfg,
                              known_gaps=[(_dt(2025, 3, 1), _dt(2025, 4, 1))])
    # 判定与分年数字逐位不变（缺口是数据事实，不是策略失败）
    assert out["passed"] == plain["passed"]
    assert out["by_year"] == plain["by_year"]
    assert out["sample_incomplete_years"] == {
        "2025": ["2025-03-01T00:00:00→2025-04-01T00:00:00"]}
    assert "样本不完整" in out["known_gaps_note"]
    # 不与任何交易年份相交的缺口不注记；不传 known_gaps 时无新键（向后兼容）
    far = rob.yearly_segments(df, cfg,
                              known_gaps=[(_dt(2027, 3, 1), _dt(2027, 4, 1))])
    assert "sample_incomplete_years" not in far
    assert "known_gaps_note" not in plain


def test_yearly_segments_known_gaps_noted_even_when_check_fails(cfg):
    # 总净利 ≤ 0 的失败路径同样带注记（读报告的人两件事都要看到）
    from datetime import datetime as _dt

    df = trades_df([tr_row(-30.0, "2025-06-01 10:00"),
                    tr_row(10.0, "2026-06-01 10:00")])
    out = rob.yearly_segments(df, cfg,
                              known_gaps=[(_dt(2025, 1, 1), _dt(2025, 12, 1))])
    assert out["passed"] is False
    assert out["sample_incomplete_years"] == {"2025": [
        "2025-01-01T00:00:00→2025-12-01T00:00:00"]}


def test_run_g2_passes_known_gaps_through_to_yearly_segments(cfg):
    """bt 装配层经 run_g2 传入的 known_gaps 必须落到 yearly_segments 注记。"""
    from datetime import datetime as _dt

    cfg_small = dataclasses.replace(cfg, random_entry_sims=8)
    bars = uptrend()
    costs = ConstCosts(spread=0.05)
    spec = spec_of(KPicker())
    base = run_backtest(KPicker(), bars, costs, symbol="TST", params={"k": 2},
                        contract_size=1.0, timeframe="H1", volume_lots=1.0)
    kwargs = dict(spec=spec, bars=bars, costs=costs, params={"k": 2},
                  base_result=base, base_summary=summarize(base),
                  grid={"k": (2,)}, bars_by_symbol={"TST": bars},
                  costs_by_symbol={"TST": costs}, sizes_by_symbol={"TST": 1.0},
                  cfg=cfg_small, seed=555)
    out = rob.run_g2(**kwargs)
    assert "sample_incomplete_years" not in out["checks"]["yearly_segments"]
    out2 = rob.run_g2(known_gaps=[(_dt(2024, 1, 1), _dt(2024, 12, 31))],
                      **kwargs)
    ys = out2["checks"]["yearly_segments"]
    assert ys.get("sample_incomplete_years") == {"2024": [
        "2024-01-01T00:00:00→2024-12-31T00:00:00"]}
    assert ys["passed"] == out["checks"]["yearly_segments"]["passed"]


# ---------------------------------------------------------------- 去最好 5%

def test_drop_best_passes_when_robust(cfg):
    df = trades_df([tr_row(10.0, f"2024-05-01 {10 + i % 10:02d}:00")
                    for i in range(20)])
    out = rob.drop_best_check(df, cfg)
    assert out["passed"] is True
    assert out["remaining_net"] > 0


def test_drop_best_fails_when_one_trade_carries_all(cfg):
    rows = [tr_row(-1.0, f"2024-05-01 {10 + i % 10:02d}:00") for i in range(19)]
    rows.append(tr_row(100.0, "2024-05-02 09:00"))
    out = rob.drop_best_check(trades_df(rows), cfg)
    assert out["passed"] is False
    assert out["remaining_net"] < 0


def test_drop_best_needs_enough_trades(cfg):
    assert rob.drop_best_check(trades_df([tr_row(5.0)]), cfg)["passed"] is False


# ---------------------------------------------------------------- 延迟一根 bar

def test_delay_one_bar_spike_strategy_fails(cfg):
    bars = make_bars([100, 100, 130, 100, 100, 100])
    costs = ConstCosts(spread=0.5)
    out = rob.delay_check(spec_of(SpikeSignal()), bars, costs, {}, cfg)
    assert out["net_original"] > 0
    assert out["net_delayed"] < 0
    assert out["passed"] is False


def test_delay_one_bar_trend_strategy_passes(cfg):
    out = rob.delay_check(spec_of(LongMid()), uptrend(80), ConstCosts(0.05),
                          {}, cfg)
    assert out["passed"] is True


# ---------------------------------------------------------------- 买入持有基准

def test_buy_hold_benchmark_long_only_partial_exposure_fails(cfg):
    bars = uptrend(200, step=0.3)
    costs = ConstCosts(spread=0.02)
    result = run_backtest(LongMid(), bars, costs, symbol="TST", params={},
                          contract_size=1.0, timeframe="H1", volume_lots=1.0)
    base_sharpe = summarize(result)["sharpe"]
    out = rob.buy_hold_check(spec_of(LongMid()), bars, costs,
                             result.trades, base_sharpe, cfg)
    assert out["passed"] is False
    assert out["buy_hold_sharpe"] > out["base_sharpe"]


def test_buy_hold_benchmark_not_long_only_is_na(cfg):
    bars = uptrend(200, step=0.3)
    result = run_backtest(ShortThenLong(), bars, ConstCosts(0.02), symbol="TST",
                          params={}, contract_size=1.0, timeframe="H1", volume_lots=1.0)
    out = rob.buy_hold_check(spec_of(ShortThenLong()), bars, ConstCosts(0.02),
                             result.trades, 0.5, cfg)
    assert out["passed"] is True
    assert out.get("applicable") is False


# ---------------------------------------------------------------- 跨品种

def test_cross_symbol_two_profitable_passes(cfg):
    bars = {"XAUUSD": uptrend(80), "EURUSD": make_bars(
        [100 + 0.4 * i for i in range(80)])}
    costs = {s: ConstCosts(0.05) for s in bars}
    sizes = {s: 1.0 for s in bars}
    out = rob.cross_symbol_check(LongMid(), bars, costs, sizes, {}, "H1",
                                 1.0, 10_000.0, cfg)
    assert out["passed"] is True
    assert out["positive_symbols"] == 2


def test_cross_symbol_one_negative_fails(cfg):
    bars = {"XAUUSD": uptrend(80), "EURUSD": make_bars(
        [100 - 0.4 * i for i in range(80)])}
    costs = {s: ConstCosts(0.05) for s in bars}
    out = rob.cross_symbol_check(LongMid(), bars, costs,
                                 {s: 1.0 for s in bars}, {}, "H1",
                                 1.0, 10_000.0, cfg)
    assert out["passed"] is False


def test_cross_symbol_single_declared_is_na(cfg):
    bars = {"XAUUSD": uptrend(80)}
    out = rob.cross_symbol_check(LongMid(), bars, {"XAUUSD": ConstCosts(0.05)},
                                 {"XAUUSD": 1.0}, {}, "H1", 1.0, 10_000.0, cfg)
    assert out["passed"] is True
    assert out.get("applicable") is False


# ---------------------------------------------------------------- 随机入场基准

def test_random_signal_fails_random_entry_benchmark(cfg):
    """计划失败测试 3：随机信号策略的夏普不应高于随机入场蒙特卡洛 p95。"""
    bars = noise_bars(700, seed=11)
    sig = random_signal_array(700, 40, np.random.default_rng(61))
    strategy = rob.FixedSignals(sig)
    costs = ConstCosts(spread=0.3, commission=0.5)
    result = run_backtest(strategy, bars, costs, symbol="TST", params={},
                          contract_size=1.0, timeframe="H1", volume_lots=1.0)
    assert len(result.trades) >= 30            # 夹具自检：交易数足够
    base_sharpe = rob.equity_sharpe(result.equity, bars["time"], "H1")
    out = rob.random_entry_check(
        spec_of(strategy), bars, costs, result.trades, base_sharpe,
        sims=60, percentile=95.0, seed=1234)
    assert out["valid_sims"] >= 30
    assert out["passed"] is False
    assert base_sharpe <= out["p95"]


def test_strong_trend_beats_random_entry_benchmark(cfg):
    """对照：反复抓到跳升的策略应赢过随机入场（防关卡只会说"不"）。"""
    # 40 个周期：3 根阴跌 −1、1 根跳升 +4；信号打在跳升前一根
    closes, signal_at = [], []
    px = 100.0
    for _ in range(40):
        for _ in range(3):
            px -= 1.0
            closes.append(px)
        px += 4.0
        closes.append(px)
        signal_at.append(len(closes) - 2)
    bars = make_bars(closes)
    sig = np.zeros(len(closes), dtype=int)
    for j in signal_at:
        sig[j] = 1
    strategy = rob.FixedSignals(sig)
    costs = ConstCosts(spread=0.05)
    result = run_backtest(strategy, bars, costs, symbol="TST", params={},
                          contract_size=1.0, timeframe="H1", volume_lots=1.0)
    assert len(result.trades) == 40
    base_sharpe = rob.equity_sharpe(result.equity, bars["time"], "H1")
    out = rob.random_entry_check(
        spec_of(strategy), bars, costs, result.trades, base_sharpe,
        sims=40, percentile=95.0, seed=99)
    assert out["passed"] is True


# ---------------------------------------------------------------- run_g2 组合

def test_run_g2_overfit_on_noise_fails(cfg):
    """计划失败测试 2（G2 部分）：噪声上挑最优 k → 参数邻域必然不是高原。"""
    bars = noise_bars(1200, seed=42)
    costs = ConstCosts(spread=0.15, commission=0.3)
    grid = {"k": tuple(range(2, 22))}
    spec = spec_of(SmaK())
    nets = {}
    runs = {}
    for k in grid["k"]:
        r = run_backtest(SmaK(), bars, costs, symbol="TST", params={"k": k},
                         contract_size=1.0, timeframe="H1", volume_lots=1.0)
        nets[k] = r.trades["net"].sum()
        runs[k] = r
    best_k = max(nets, key=nets.get)
    cfg_small = dataclasses.replace(cfg, random_entry_sims=8)
    out = rob.run_g2(
        spec, bars, costs, {"k": best_k},
        base_result=runs[best_k], base_summary=summarize(runs[best_k]),
        grid=grid, bars_by_symbol={"TST": bars},
        costs_by_symbol={"TST": costs}, sizes_by_symbol={"TST": 1.0},
        cfg=cfg_small, seed=555)
    assert out["checks"]["param_plateau"]["passed"] is False
    assert out["passed"] is False
    for key in ("cost_scaling", "param_plateau", "yearly_segments",
                "cross_symbol", "random_entry_benchmark", "buy_hold_benchmark",
                "drop_best_5pct", "delay_1_bar"):
        assert key in out["checks"]


# ------------------------------------------- 随机入场口径（T21e/M7 计划失败测试）

def ny_hours_of(times):
    """测试助手：naive-UTC 时间序列 → 纽约本地小时数组（与引擎分桶同口径）。"""
    return (pd.to_datetime(pd.Series(times)).dt.tz_localize("UTC")
            .dt.tz_convert("America/New_York").dt.hour.to_numpy())


def hour_entry_bars(n_days=10, signal_hours=(9,), step=0.3):
    """上行 H1 bars；信号只打在纽约本地 hour ∈ signal_hours 的 bar 上。

    引擎在信号 bar 的**下一根开盘**入场（信号 bar hour=9 → 入场 10:00），
    故 signal_hours=(9,) 得到"只在 10:00–11:00 入场"的策略。
    """
    bars = make_bars([100.0 + step * i for i in range(n_days * 24)])
    h = ny_hours_of(bars["time"])
    sig = np.zeros(len(bars), dtype=int)
    sig[np.isin(h, signal_hours)] = 1
    return bars, sig


def random_entry_of(sig, bars, *, side=1, sims=12, seed=2026):
    """跑一次随机入场基准（FixedSignals×side），返回其结果 dict。"""
    strategy = rob.FixedSignals(np.asarray(sig, dtype=int) * side)
    costs = ConstCosts(spread=0.05)
    result = run_backtest(strategy, bars, costs, symbol="TST", params={},
                          contract_size=1.0, timeframe="H1", volume_lots=1.0)
    assert len(result.trades) >= 3            # 夹具自检：确有交易
    base_sharpe = rob.equity_sharpe(result.equity, bars["time"], "H1")
    return rob.random_entry_check(
        spec_of(strategy), bars, costs, result.trades, base_sharpe,
        sims=sims, percentile=95.0, seed=seed)


def test_random_entry_samples_only_strategy_entry_hours_ny():
    """M7 计划失败测试：只在 10:00–11:00（纽约）入场的策略，随机基准的
    入场也全部落在该时段（旧实现全场随机，含高成本小时）。"""
    bars, sig = hour_entry_bars(signal_hours=(9,))
    out = random_entry_of(sig, bars)
    assert out["base_entry_hours_ny"] == [10]
    assert {int(h) for h in out["sampled_hours_ny"]} == {10}


def test_random_entry_hour_mixture_limited_to_strategy_support():
    """M7：入场分布在小时段集合 {10, 14} 上 → 随机基准只在支撑集内抽样。"""
    bars, sig = hour_entry_bars(signal_hours=(9, 13))
    out = random_entry_of(sig, bars)
    assert out["base_entry_hours_ny"] == [10, 14]
    assert {int(h) for h in out["sampled_hours_ny"]} <= {10, 14}
    assert {int(h) for h in out["sampled_hours_ny"]} == {10, 14}


def test_random_entry_side_ratio_matches_strategy():
    """M7：策略全空（全多）→ 随机基准方向也全空（全多），不再五五开。"""
    bars, sig = hour_entry_bars(signal_hours=(9,))
    out_short = random_entry_of(sig, bars, side=-1)
    assert out_short["base_long_fraction"] == 0.0
    assert out_short["sampled_long_fraction"] == 0.0
    out_long = random_entry_of(sig, bars, side=1)
    assert out_long["base_long_fraction"] == 1.0
    assert out_long["sampled_long_fraction"] == 1.0


# ------------------------------------------------- T36（H2/M7）：成本敏感性报告

class OvershootConstCosts(ConstCosts):
    """带 stop_overshoot 的替身：p50/p90 两档恒定越幅（价格单位）。"""

    def __init__(self, *a, ov=0.1, ov_p90=0.5, **kw):
        super().__init__(*a, **kw)
        self._ov, self._ov90 = ov, ov_p90

    def stop_overshoot(self, symbol, hour_utc, *, price=None, level="p50"):
        return self._ov90 if level == "p90" else self._ov


class EnterAndStop(BaseStrategy):
    """bar15 起做多 + 崩盘 bar：ATR≈0.4（TR 恒 0.4）→ SL=入场−1.6 触发。

    SL 距离取 4×ATR（1.6）：远大于常态 bar 的 low 缓冲（−0.2），点差放大
    （敏感性变体）不会把 SL 抬进常态 low 而变成连续即时空损。
    """

    def __init__(self):
        self.exit_rules = ExitRules(stop_loss_atr=4.0)

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        s = pd.Series(0, index=bars.index, dtype="int64")
        s.iloc[15:] = 1
        return s


def crash_bars(n=40, crash_at=20, start="2024-01-01"):
    closes = [100.0] * n
    closes[crash_at] = 97.0                        # low = 96.8 <= SL 99.2
    return make_bars(closes, start=start)


def test_cost_scaling_reports_stop_overshoot_sensitivity(cfg):
    """T36：0.5x/1x/p90 三档净利只报告不改门槛（净利随越幅成本单调递减）。"""
    costs = OvershootConstCosts(ov=0.1, ov_p90=0.5)
    out = rob.cost_scaling_check(spec_of(EnterAndStop()), crash_bars(),
                                 costs, {}, cfg)
    sens = out["stop_overshoot_sensitivity"]
    assert sens["applicable"] is True and sens["record_only"] is True
    assert sens["net_overshoot_1x"] > sens["net_overshoot_p90"]
    assert sens["net_overshoot_0_5x"] > sens["net_overshoot_1x"]
    # 门槛判定不受影响：仍只看 x1.5 的期望
    assert out["passed"] is True


def test_cost_scaling_overshoot_sensitivity_na_without_model(cfg):
    out = rob.cost_scaling_check(spec_of(LongMid()), uptrend(),
                                 ConstCosts(spread=0.02), {}, cfg)
    assert out["stop_overshoot_sensitivity"]["applicable"] is False


def test_cost_scaling_reports_early_years_spread_x1_5(cfg):
    """T36/M7：2020 年以前点差 x1.5 情形——只报告，不改判定。

    2019 年的 bars（全部交易都在 cutoff 前出场）：点差 0.5 -> x1.5 后
    早年净利下降；passed 仍由全期 x1.5 的期望决定。
    """
    costs = ConstCosts(spread=0.5)
    bars = crash_bars(n=40, crash_at=20, start="2019-06-01")
    out = rob.cost_scaling_check(spec_of(EnterAndStop()), bars, costs, {}, cfg)
    rep = out["early_years_spread_x1_5"]
    assert rep["applicable"] is True and rep["record_only"] is True
    assert rep["cutoff"] == "2020-01-01" and rep["factor"] == 1.5
    assert rep["trades_pre_cutoff"] >= 1
    assert rep["net_pre_cutoff_spread_scaled"] < rep["net_pre_cutoff_base"]
    assert out["passed"] is True


def test_cost_scaling_early_years_na_without_pre2020_trades(cfg):
    # 2024 年的 bars：无 2020 年以前交易 -> 不适用（字段仍存在）
    out = rob.cost_scaling_check(spec_of(LongMid()), uptrend(),
                                 ConstCosts(spread=0.02), {}, cfg)
    assert out["early_years_spread_x1_5"]["applicable"] is False
