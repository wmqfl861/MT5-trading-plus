"""G3 walk-forward（T21b/H5）：日历月滚动训练/测试窗，参数只在卡的网格内
重选，只统计 entry_time ≥ 测试窗起点的交易（预热段交易不计入 oos）。
"""
import dataclasses

import pandas as pd
import pytest

from mt5quant.backtest.engine_a import run_backtest
from mt5quant.backtest.report import summarize
from mt5quant.strategies import BaseStrategy, BuyHold, ParamSpec
from mt5quant.strategies.base import ExitRules, RequiredData
from mt5quant.validation import walkforward as wf
from mt5quant.validation.gates import load_gates
from mt5quant.validation.robustness import BacktestSpec


@pytest.fixture(scope="module")
def cfg():
    """小时档缩窗（2+1 个月）：真门槛（24/6）太大，单元测试跑不动。"""
    base = load_gates()[0]
    return dataclasses.replace(base, train_months_hourly=2, test_months_hourly=1)


@pytest.fixture(scope="module")
def cfg_short():
    """1+1 个月的更小窗（预热段排除用例）。"""
    base = load_gates()[0]
    return dataclasses.replace(base, train_months_hourly=1, test_months_hourly=1)


class Zero:
    def spread_p90(self, symbol, hour_utc, *, price=None):
        return 0.0

    def slippage(self, symbol, hour_utc, *, price=None):
        return 0.0

    def commission_per_lot(self, symbol):
        return 0.0

    def overnight_fee(self, symbol, side, from_ts, to_ts, *, price=None):   # T82 形参透传
        return 0.0


def make_bars(closes, start="2024-01-01", freq="1h"):
    closes = list(map(float, closes))
    times = pd.date_range(start, periods=len(closes), freq=freq)
    opens = [closes[0]] + closes[:-1]
    return pd.DataFrame({
        "time": times, "open": opens,
        "high": [max(o, c) + 0.2 for o, c in zip(opens, closes)],
        "low": [min(o, c) - 0.2 for o, c in zip(opens, closes)],
        "close": closes, "tick_volume": 1, "spread": 1, "real_volume": 0,
    })


class SidePicker(BaseStrategy):
    """side=1 全程做多、side=-1 全程做空：网格里只有一个正确答案。"""

    params_schema = (ParamSpec("side", "int", (1, -1)),)
    required_data = RequiredData(symbols=("TST",), timeframe="H1",
                                 lookback_bars=0)

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        return pd.Series(params["side"], index=bars.index, dtype="int64")


class ImmediateLong(BaseStrategy):
    """第一根 bar 就做多并持有到末尾；lookback 可配（预热段排除用例）。"""

    params_schema = ()
    required_data = RequiredData(symbols=("TST",), timeframe="H1",
                                 lookback_bars=0)
    exit_rules = ExitRules()

    def __init__(self, lookback_bars: int = 0):
        self.required_data = RequiredData(symbols=("TST",), timeframe="H1",
                                          lookback_bars=lookback_bars)

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        return pd.Series(1, index=bars.index, dtype="int64")


def spec_of(strategy):
    return BacktestSpec(strategy=strategy, symbol="TST", timeframe="H1",
                        contract_size=1.0)


def base_sharpe(strategy, bars, params):
    base = run_backtest(strategy, bars, Zero(), symbol="TST", params=params,
                        contract_size=1.0, timeframe="H1", volume_lots=1.0)
    return summarize(base)["sharpe"]


# ---------------------------------------------------------------- 窗口计划

def test_month_window_spec_buckets():
    """门槛文件口径：H1 及以上 24/6 个日历月，日内（< H1）12/3。"""
    cfg_default, _ = load_gates()
    assert wf.month_window_spec("H1", cfg_default) == (24, 6)
    assert wf.month_window_spec("H4", cfg_default) == (24, 6)
    assert wf.month_window_spec("M30", cfg_default) == (12, 3)
    assert wf.month_window_spec("M5", cfg_default) == (12, 3)


def test_plan_windows_m5_test_window_is_three_calendar_months():
    """M5（H5/探针 P5）：测试窗必须是 3 个日历月，不再是 500 根（约 2.4 天）。"""
    cfg_default, _ = load_gates()
    train_m, test_m = wf.month_window_spec("M5", cfg_default)
    assert (train_m, test_m) == (12, 3)
    times = pd.date_range("2023-06-01", end="2024-08-15", freq="5min")
    windows = wf.plan_windows(times, train_months=train_m, test_months=test_m)
    assert len(windows) == 1                 # 15 个月恰好容纳 12+3
    w = windows[0]
    assert w["train_start"] == pd.Timestamp("2023-06-01")
    assert w["test_start"] == pd.Timestamp("2024-06-01")
    assert w["test_end"] == pd.Timestamp("2024-09-01")   # 6→9 月 = 3 个日历月
    # 每个测试窗都恰为 test_months 个日历月（起点月初、终点下月月初）
    months = ((w["test_end"].year - w["test_start"].year) * 12
              + w["test_end"].month - w["test_start"].month)
    assert months == 3


def test_plan_windows_rolls_by_test_months():
    times = pd.date_range("2024-01-01", end="2024-12-31", freq="1h")
    windows = wf.plan_windows(times, train_months=2, test_months=1)
    starts = [w["test_start"] for w in windows]
    assert starts == [pd.Timestamp(f"2024-{m:02d}-01") for m in range(3, 13)]
    assert all(w["train_end"] == w["test_start"] for w in windows)


def test_plan_windows_insufficient_span():
    times = pd.date_range("2024-01-01", end="2024-01-31", freq="1h")
    assert wf.plan_windows(times, train_months=2, test_months=1) == []


# ---------------------------------------------------------------- run_g3

def test_walkforward_trending_buy_hold_passes(cfg):
    bars = make_bars([100 + 0.05 * i for i in range(2600)])  # ≈108 天
    out = wf.run_g3(spec_of(BuyHold()), bars, Zero(), grid=[{}],
                    in_sample_sharpe=base_sharpe(BuyHold(), bars, {}),
                    cfg=cfg)
    assert out["passed"] is True
    assert len(out["segments"]) == 2       # 2+1 个月：3 月整月 + 4 月截尾
    assert out["oos_sharpe"] > 0
    for seg in out["segments"]:
        assert seg["test_months"] == 1
        assert seg["test_bars"] >= 1
        assert seg["oos_net"] > 0
        assert seg["oos_trades"] >= 1
    assert out["segments"][0]["test_start"] == "2024-03-01T00:00:00"


def test_walkforward_declining_fails(cfg):
    # H-13：价格须恒正——斜率 0.05 会在 i≈2000 后跌破 0；放缓到 0.03
    # （末根 ≈22）保持全程单调下跌且价格合法，oos_sharpe<0 语义不变
    bars = make_bars([100 - 0.03 * i for i in range(2600)])
    out = wf.run_g3(spec_of(BuyHold()), bars, Zero(), grid=[{}],
                    in_sample_sharpe=base_sharpe(BuyHold(), bars, {}),
                    cfg=cfg)
    assert out["passed"] is False
    assert out["oos_sharpe"] < 0


def test_walkforward_reselects_within_grid(cfg):
    """只在卡网格内重选：上升趋势里每个训练窗都必须挑 side=1。"""
    bars = make_bars([100 + 0.05 * i for i in range(2600)])
    out = wf.run_g3(spec_of(SidePicker()), bars, Zero(),
                    grid=[{"side": 1}, {"side": -1}],
                    in_sample_sharpe=base_sharpe(SidePicker(), bars,
                                                 {"side": 1}),
                    cfg=cfg)
    assert out["passed"] is True
    assert all(seg["params"] == {"side": 1} for seg in out["segments"])


def test_walkforward_insufficient_data_fails(cfg):
    bars = make_bars([100.0] * 720)        # 只有 2024-01 一个月
    out = wf.run_g3(spec_of(BuyHold()), bars, Zero(), grid=[{}],
                    in_sample_sharpe=None, cfg=cfg)
    assert out["passed"] is False
    assert out["segments"] == []
    assert "月" in out["reason"]


def test_walkforward_warmup_trades_excluded_from_oos(cfg_short):
    """M5/H5：预热段（lookback 根，取自训练窗末尾）里的交易不计入
    oos_trades / oos_net；零回看对照则计入。"""
    bars = make_bars([100 + 0.02 * i for i in range(1500)])  # ≈62.5 天

    out5 = wf.run_g3(spec_of(ImmediateLong(lookback_bars=5)), bars, Zero(),
                     grid=[{}], in_sample_sharpe=1.0, cfg=cfg_short)
    assert len(out5["segments"]) == 2       # 2 月整月 + 3 月截尾
    for seg in out5["segments"]:
        # 入场发生在预热段（测试窗起点之前）→ 不计
        assert seg["oos_trades"] == 0
        assert seg["oos_net"] == 0.0

    out0 = wf.run_g3(spec_of(ImmediateLong(lookback_bars=0)), bars, Zero(),
                     grid=[{}], in_sample_sharpe=1.0, cfg=cfg_short)
    for seg in out0["segments"]:
        # 零回看：切片起点即测试窗起点 → 第一根可交易 bar 入场，计入
        assert seg["oos_trades"] == 1
        assert seg["oos_net"] > 0


# ------------------------------------------- T79：known_gaps（样本不完整注记）

def test_run_g3_flags_windows_overlapping_known_gaps(cfg_short):
    """T79/R12-G1：walk-forward 读到 known_gaps（序列 meta.json 登记、
    read_known_gaps 读取）时，训练/测试窗与缺口相交的段标 sample_incomplete
    并在 known_gaps_note 注明样本不完整——只注记，不改变段数与判定。"""
    from datetime import datetime as _dt

    bars = make_bars([100 + 0.05 * i for i in range(24 * 120)])  # ≈5 个月
    kwargs = dict(grid=[{}], in_sample_sharpe=1.0, cfg=cfg_short)
    plain = wf.run_g3(spec_of(ImmediateLong()), bars, Zero(), **kwargs)
    assert all("sample_incomplete" not in s for s in plain["segments"])
    assert "known_gaps_note" not in plain

    kg = [(_dt(2024, 3, 5), _dt(2024, 3, 20))]
    out = wf.run_g3(spec_of(ImmediateLong()), bars, Zero(),
                    known_gaps=kg, **kwargs)
    assert len(out["segments"]) == len(plain["segments"])
    assert out["passed"] == plain["passed"]
    flagged = [s for s in out["segments"] if "sample_incomplete" in s]
    assert flagged, "与已知缺口相交的窗口必须带 sample_incomplete 标记"
    # 训练∪测试窗 [2024-03-01, 2024-05-01) 与缺口 [03-05, 03-20) 相交：
    # 测试窗 3 月（train 02-01 起）与 4 月（train 03-01 起）两段被标记
    assert {s["test_start"][:7] for s in flagged} == {"2024-03", "2024-04"}
    assert "样本不完整" in out["known_gaps_note"]
    # 不相交的缺口不产生注记
    far = wf.run_g3(spec_of(ImmediateLong()), bars, Zero(),
                    known_gaps=[(_dt(2020, 1, 1), _dt(2020, 2, 1))], **kwargs)
    assert all("sample_incomplete" not in s for s in far["segments"])
    assert "known_gaps_note" not in far
