"""audit 测试：ATR/hourly_stats（volatility，计划未列独立测试文件，归本文件）、
热力图手算、阈值边界翻转、Costs 聚合体、cost_audit.py 脚本冒烟。"""
import importlib.util
import json
import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from mt5mcp.indicators import atr as mcp_atr
from mt5quant.costs import (
    CommissionModel,
    Costs,
    SlippageModel,
    SpreadModel,
    SwapModel,
    SwapRule,
)
from mt5quant.costs.audit import ScopeDecision, cost_heatmap, decide_scope
from mt5quant.costs.volatility import atr_series, hourly_stats


# ---------- 造数帮手 ----------

def make_bars(times, highs, lows, closes, opens=None):
    n = len(times)
    return pd.DataFrame(
        {
            "time": pd.to_datetime(list(times)),
            "open": np.asarray(list(opens) if opens is not None else list(closes), dtype="float64"),
            "high": np.asarray(list(highs), dtype="float64"),
            "low": np.asarray(list(lows), dtype="float64"),
            "close": np.asarray(list(closes), dtype="float64"),
            "tick_volume": np.ones(n, dtype="int64"),
            "spread": np.zeros(n, dtype="int64"),
            "real_volume": np.zeros(n, dtype="int64"),
        }
    )


def make_ticks(rows, base=1.0):
    return pd.DataFrame(
        {
            "time_msc": np.asarray(
                [pd.Timestamp(t).value // 10**6 for t, _ in rows], dtype="int64"
            ),
            "bid": np.asarray([base for _ in rows], dtype="float64"),
            "ask": np.asarray([base + s for _, s in rows], dtype="float64"),
        }
    )


# ---------- volatility.atr_series ----------

def test_atr_series_wilder_hand_computed():
    bars = make_bars(
        [f"2026-10-19 09:{i:02d}:00" for i in range(5)],
        highs=[11, 13, 12, 12, 14],
        lows=[10, 10, 9, 11, 11],
        closes=[10.5, 12, 10, 11.5, 13],
    )
    out = atr_series(bars, 3)
    # TR = [1, 3, 3, 2, 3]；种子=前3均值 7/3，其后 Wilder 递推 alpha=1/3
    assert out.iloc[:2].isna().all()
    assert float(out.iloc[2]) == pytest.approx(7 / 3)
    assert float(out.iloc[3]) == pytest.approx(20 / 9)
    assert float(out.iloc[4]) == pytest.approx(67 / 27)


def test_atr_series_matches_mt5mcp_implementation():
    # 钉死"同法"：与 mt5mcp.indicators.atr 输出全等（含 NaN 段）
    rng = np.random.default_rng(7)
    n = 60
    close = 100 + np.cumsum(rng.normal(0, 0.8, n))
    high = close + rng.uniform(0.1, 1.5, n)
    low = close - rng.uniform(0.1, 1.5, n)
    bars = make_bars(pd.date_range("2026-10-19", periods=n, freq="5min"), high, low, close)
    mine = atr_series(bars, 14)
    ref = mcp_atr(bars["high"], bars["low"], bars["close"], 14)
    assert np.allclose(mine.to_numpy(), ref.to_numpy(), rtol=0, atol=0, equal_nan=True)


def test_atr_series_insufficient_data_all_nan():
    bars = make_bars(["2026-10-19 09:00:00"], [11.0], [10.0], [10.5])
    assert atr_series(bars, 14).isna().all()


# ---------- volatility.hourly_stats ----------

def test_hourly_stats_hand_computed():
    bars = make_bars(
        ["2026-10-19 09:00:00", "2026-10-19 09:05:00", "2026-10-19 10:00:00"],
        highs=[12.0, 14.0, 11.0],
        lows=[10.0, 10.0, 10.0],
        closes=[11.0, 12.0, 10.5],
    )
    stats = hourly_stats(bars, "M1")
    assert stats.index.name == "hour_utc"
    assert list(stats.index) == [9, 10]
    assert stats.loc[9, "mean_range"] == pytest.approx(3.0)   # (2+4)/2
    assert stats.loc[10, "mean_range"] == pytest.approx(1.0)
    assert math.isnan(stats.loc[9, "atr14_median"])           # 不足 14 根 → NaN


def test_hourly_stats_atr14_median():
    times = [f"2026-10-19 11:{i:02d}:00" for i in range(15)]  # 同一小时 15 根
    bars = make_bars(times, [11.0] * 15, [10.0] * 15, [10.5] * 15)
    stats = hourly_stats(bars, "M1")
    assert stats.loc[11, "mean_range"] == pytest.approx(1.0)
    assert stats.loc[11, "atr14_median"] == pytest.approx(1.0)  # TR 恒 1 → ATR 恒 1


def test_hourly_stats_invalid_timeframe_and_columns():
    bars = make_bars(["2026-10-19 09:00:00"], [11.0], [10.0], [10.5])
    with pytest.raises(ValueError):
        hourly_stats(bars, "H7")
    with pytest.raises(ValueError):
        hourly_stats(bars.drop(columns=["high"]), "M1")


# ---------- audit.cost_heatmap ----------

def _models():
    xau = SpreadModel.from_ticks("XAUUSD", make_ticks(
        [(f"2026-06-01 14:00:{i:02d}", 0.1 * (i + 1)) for i in range(5)], base=2400.0))
    eur = SpreadModel.from_ticks("EURUSD", make_ticks(
        [(f"2026-06-01 14:00:{i:02d}", 0.0001 * (i + 1)) for i in range(5)], base=1.10))
    spread = xau.merge(eur)                       # p90：XAU 0.46 / EUR 0.00046
    slip = SlippageModel(spread, spread_fraction=0.1)  # 0.046 / 0.000046
    comm = CommissionModel(default_per_lot=0.0,
                           per_symbol={"XAUUSD": 6.0, "EURUSD": 3.0})
    return spread, slip, comm


def _vol_fixture():
    xau = make_bars(["2026-10-19 14:00:00", "2026-10-20 14:00:00"],
                    [2008.0, 2008.0], [2000.0, 2000.0], [2004.0, 2004.0])  # 振幅 8
    eur = make_bars(["2026-10-19 14:00:00", "2026-10-20 14:00:00"],
                    [1.1040, 1.1040], [1.0960, 1.0960], [1.1000, 1.1000])  # 振幅 0.008
    return {("XAUUSD", "H1"): hourly_stats(xau, "H1"),
            ("EURUSD", "H1"): hourly_stats(eur, "H1")}


def test_heatmap_hand_computed_both_symbols():
    spread, slip, comm = _models()
    hm = cost_heatmap(spread, slip, comm,
                      {"XAUUSD": 100.0, "EURUSD": 100000.0},
                      _vol_fixture(), ["XAUUSD", "EURUSD"], ["H1"])
    assert list(hm.index) == [("XAUUSD", "H1"), ("EURUSD", "H1")]
    assert list(hm.columns) == [14]
    # XAUUSD：(0.46 + 2×0.046 + 6/100) / 8 = 0.612/8 = 0.0765（点差每来回一次，佣金只折一次）
    assert hm.loc[("XAUUSD", "H1"), 14] == pytest.approx(0.0765)
    # EURUSD：(0.00046 + 2×0.000046 + 3/100000) / 0.008 = 0.000582/0.008 = 0.07275
    assert hm.loc[("EURUSD", "H1"), 14] == pytest.approx(0.07275)


def test_heatmap_missing_spread_hour_is_nan():
    spread, slip, comm = _models()
    extra = make_bars(["2026-10-19 15:00:00"], [2008.0], [2000.0], [2004.0])
    vol = _vol_fixture()
    vol[("XAUUSD", "H1")] = pd.concat(
        [vol[("XAUUSD", "H1")], hourly_stats(extra, "H1")]).sort_index()
    hm = cost_heatmap(spread, slip, comm,
                      {"XAUUSD": 100.0, "EURUSD": 100000.0},
                      vol, ["XAUUSD", "EURUSD"], ["H1"])
    assert list(hm.columns) == [14, 15]  # 列=vol 小时并集
    assert math.isnan(hm.loc[("XAUUSD", "H1"), 15])  # 点差缺 15 点 → NaN
    assert hm.loc[("XAUUSD", "H1"), 14] == pytest.approx(0.0765)


def test_heatmap_fail_fast_on_bad_inputs():
    spread, slip, comm = _models()
    vol = _vol_fixture()
    with pytest.raises(KeyError):  # contract_sizes 缺品种
        cost_heatmap(spread, slip, comm, {"EURUSD": 100000.0}, vol,
                     ["XAUUSD", "EURUSD"], ["H1"])
    with pytest.raises(ValueError):  # contract_size 非正
        cost_heatmap(spread, slip, comm, {"XAUUSD": 0.0, "EURUSD": 100000.0}, vol,
                     ["XAUUSD", "EURUSD"], ["H1"])
    with pytest.raises(KeyError):  # vol 缺 (EURUSD, H1)
        cost_heatmap(spread, slip, comm, {"XAUUSD": 100.0},
                     {("XAUUSD", "H1"): vol[("XAUUSD", "H1")]},
                     ["XAUUSD", "EURUSD"], ["H1"])
    with pytest.raises(ValueError):  # vol 帧缺 mean_range 列
        bad = vol[("XAUUSD", "H1")].drop(columns=["mean_range"])
        cost_heatmap(spread, slip, comm, {"XAUUSD": 100.0, "EURUSD": 100000.0},
                     {("XAUUSD", "H1"): bad, ("EURUSD", "H1"): vol[("EURUSD", "H1")]},
                     ["XAUUSD", "EURUSD"], ["H1"])


# ---------- audit.decide_scope ----------

def _hm_single():
    idx = pd.MultiIndex.from_tuples([("XAUUSD", "H1")], names=["symbol", "timeframe"])
    return pd.DataFrame(
        [[0.12, 0.119, 0.121, float("nan")]], index=idx, columns=[9, 10, 11, 12])


def test_decide_scope_threshold_boundary():
    ds = decide_scope(_hm_single())  # 默认 threshold=0.12
    assert ds[0] == ScopeDecision("XAUUSD", "H1", 9, 0.12, True)     # 恰等阈值 → 纳入
    assert ds[1] == ScopeDecision("XAUUSD", "H1", 10, 0.119, True)
    assert ds[2] == ScopeDecision("XAUUSD", "H1", 11, 0.121, False)  # 略超 → 翻转
    assert ds[3].include is False and math.isnan(ds[3].ratio)         # 无数据 → 排除


def test_decide_scope_custom_threshold_flips():
    assert [d.include for d in decide_scope(_hm_single(), threshold=0.2)[:3]] == [True, True, True]
    assert [d.include for d in decide_scope(_hm_single(), threshold=0.1)[:3]] == [False, False, False]


def test_decide_scope_iteration_order():
    idx = pd.MultiIndex.from_tuples([("A", "H1"), ("A", "H4"), ("B", "H1")],
                                    names=["symbol", "timeframe"])
    hm = pd.DataFrame(np.arange(6, dtype="float64").reshape(3, 2), index=idx, columns=[7, 8])
    ds = decide_scope(hm)
    assert [(d.symbol, d.timeframe, d.hour) for d in ds] == [
        ("A", "H1", 7), ("A", "H1", 8),
        ("A", "H4", 7), ("A", "H4", 8),
        ("B", "H1", 7), ("B", "H1", 8)]


# ---------- Costs 聚合体（四方法签名与 T6 Protocol 同名） ----------

def test_costs_bundle_delegates_four_methods():
    spread, slip, comm = _models()
    swap = SwapModel(per_symbol={"XAUUSD": SwapRule(long_per_day=-7.0, short_per_day=-2.0)})
    costs = Costs(spread=spread, slippage_model=slip, commission=comm, swap=swap)
    assert costs.spread_p90("XAUUSD", 14) == pytest.approx(0.46)
    assert costs.slippage("XAUUSD", 14) == pytest.approx(0.046)
    assert costs.commission_per_lot("XAUUSD") == pytest.approx(6.0)
    assert costs.overnight_fee("XAUUSD", "long",
                               datetime(2026, 10, 21, 20), datetime(2026, 10, 22, 20, 59)
                               ) == pytest.approx(-21.0)


# ---------- scripts/cost_audit.py 冒烟 ----------

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "cost_audit.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("cost_audit_script", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cost_audit_script_smoke(tmp_path, monkeypatch):
    from mt5data.store import save_bars

    root = tmp_path / "data"
    out = tmp_path / "reports" / "costs"

    def bars_frame(day):
        times = [f"{day} {h:02d}:00:00" for h in (9, 10, 11, 12)]
        return make_bars(times, [2008.0] * 4, [1992.0] * 4, [2000.0] * 4)  # 振幅 16

    for sym in ("XAUUSD", "EURUSD"):
        save_bars(str(root), sym, "H1",
                  pd.concat([bars_frame("2026-10-19"), bars_frame("2026-10-20")],
                            ignore_index=True))

    from mt5data.ticks import save_ticks
    ticks = pd.DataFrame(
        {
            "time_msc": np.asarray(
                [pd.Timestamp(f"2026-10-19 {h:02d}:{m:02d}:00").value // 10**6
                 for h in (9, 10, 11, 12) for m in (0, 10, 20, 30, 40)],
                dtype="int64"),
            "bid": [2400.0] * 20,
            "ask": [2400.3] * 20,  # 点差恒 0.3 → p90=0.3
            "last": [2400.15] * 20,
            "volume": [0.0] * 20,
            "flags": [2] * 20,
        }
    )
    save_ticks(str(root), "XAUUSD", ticks)  # 按月分区布局

    specs = root / "specs"
    specs.mkdir(parents=True)
    (specs / "latest.json").write_text(
        json.dumps({"XAUUSD": {"contract_size": 100.0},
                    "EURUSD": {"contract_size": 100000.0}}),
        encoding="utf-8")

    mod = _load_script()
    monkeypatch.setattr(
        sys, "argv",
        ["cost_audit.py", "--symbols", "XAUUSD,EURUSD,BTCUSD", "--timeframes", "H1",
         "--data-root", str(root), "--out", str(out), "--commission", "XAUUSD=6.0"])
    assert mod.main() == 0

    hm = pd.read_csv(out / "heatmap.csv", index_col=[0, 1])
    # XAUUSD：(0.3 + 2×0.03 + 6/100) / 16 = 0.42/16 = 0.02625（点差每来回一次）
    assert hm.loc[("XAUUSD", "H1"), "9"] == pytest.approx(0.02625)
    assert math.isnan(hm.loc[("EURUSD", "H1"), "9"])  # EURUSD 无 tick → 全 NaN
    scope = (out / "scope.md").read_text(encoding="utf-8")
    assert "无tick，跳过点差实测" in scope   # EURUSD/BTCUSD
    assert "无K线" in scope                  # BTCUSD
    assert "XAUUSD" in scope


def test_cost_audit_script_graceful_when_no_data(tmp_path, monkeypatch, capsys):
    mod = _load_script()
    monkeypatch.setattr(
        sys, "argv",
        ["cost_audit.py", "--symbols", "XAUUSD", "--timeframes", "H1",
         "--data-root", str(tmp_path / "empty"), "--out", str(tmp_path / "out")])
    assert mod.main() == 1  # 无任何 tick：优雅说明而非栈崩
    assert "tick" in capsys.readouterr().out
