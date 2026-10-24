"""报告测试：两笔合成交易的手算指标、Manifest 哈希可重复、write_report。"""
import json
import os
from dataclasses import asdict
from datetime import datetime, timedelta

import pandas as pd
import pytest

from mt5data.store import _COLUMNS
from mt5quant.backtest import engine_a
from mt5quant.backtest.engine_a import BacktestResult, Trade
from mt5quant.backtest.report import (
    bars_per_year,
    current_git_commit,
    data_sha256,
    render_markdown,
    summarize,
    write_report,
)


def make_bars(n=5):
    times = [pd.Timestamp("2026-01-05 00:00") + timedelta(hours=i) for i in range(n)]
    return pd.DataFrame({
        "time": pd.to_datetime(times),
        "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0,
        "tick_volume": 1, "spread": 1, "real_volume": 0,
    })[_COLUMNS]


def trade(**kw):
    base = dict(entry_time=pd.Timestamp("2026-01-05 10:00"),
                exit_time=pd.Timestamp("2026-01-05 12:00"),
                side="long", volume_lots=1.0, entry_price=100.6, exit_price=102.9,
                gross=310.0, commission=10.0, slippage_cost=2.0, spread_cost=3.0,
                swap=0.0, net=300.0, r_multiple=1.5, bars_held=2,
                exit_reason="take_profit")
    base.update(kw)
    return Trade(**base)


def two_trade_result():
    t1 = trade()  # 净 +300
    t2 = trade(entry_time=pd.Timestamp("2026-01-06 14:00"),
               exit_time=pd.Timestamp("2026-01-06 15:00"),
               side="short", gross=-90.0, net=-100.0, r_multiple=-0.5,
               bars_held=1, exit_reason="signal")  # 净 −100
    trades = pd.DataFrame([asdict(t1), asdict(t2)])
    equity = pd.Series([10_000.0, 10_000.0, 10_300.0, 10_300.0, 10_200.0])
    return BacktestResult(trades=trades, equity=equity, bars=make_bars(),
                          params={"k": 1}, symbol="XAUUSD", timeframe="H1")


def test_summarize_hand_computed_metrics():
    s = summarize(two_trade_result())
    assert s["net_profit"] == pytest.approx(200.0)
    assert s["return_pct"] == pytest.approx(0.02)
    assert s["profit_factor"] == pytest.approx(3.0)       # 300 / 100
    assert s["win_rate"] == pytest.approx(0.5)
    assert s["trade_count"] == 2
    assert s["long_count"] == 1 and s["short_count"] == 1
    assert s["avg_win"] == pytest.approx(300.0)
    assert s["avg_loss"] == pytest.approx(100.0)
    assert s["payoff_ratio"] == pytest.approx(3.0)
    assert s["max_drawdown"] == pytest.approx(-100.0 / 10_300.0)  # 峰 10300 → 谷 10200
    assert s["expectancy"] == pytest.approx(100.0)
    assert s["expectancy_r"] == pytest.approx(0.5)
    assert s["time_in_market"] == pytest.approx(0.75)     # (2+1)/(5−1)
    # 总成本 = 佣金 20 + 滑点 4 + 价差 6 + |swap| 0 = 30；中价毛利 = 315+(−85)=230
    assert s["cost_to_gross"] == pytest.approx(30.0 / 230.0)
    assert s["expectancy_cost_multiple"] == pytest.approx(100.0 / (30.0 / 2))
    assert isinstance(s["sharpe"], float) and isinstance(s["sortino"], float)
    assert s["cost_model_version"] == "v0"
    assert s["symbol"] == "XAUUSD" and s["timeframe"] == "H1"
    assert s["params"] == {"k": 1}


def test_summarize_breakdowns():
    s = summarize(two_trade_result())
    assert s["by_year"] == {"2026": pytest.approx(200.0)}
    assert s["by_hour"] == {"10": pytest.approx(300.0), "14": pytest.approx(-100.0)}
    assert s["by_side"]["long"]["net"] == pytest.approx(300.0)
    assert s["by_side"]["long"]["win_rate"] == pytest.approx(1.0)
    assert s["by_side"]["short"]["net"] == pytest.approx(-100.0)
    assert s["by_side"]["short"]["win_rate"] == pytest.approx(0.0)


def test_summarize_flat_no_trades():
    empty_trades = pd.DataFrame(columns=list(asdict(trade()).keys()))
    res = BacktestResult(trades=empty_trades,
                         equity=pd.Series([10_000.0] * 5),
                         bars=make_bars(), params={}, symbol="XAUUSD",
                         timeframe="H1")
    s = summarize(res)
    assert s["net_profit"] == 0.0
    assert s["trade_count"] == 0
    assert s["win_rate"] == 0.0
    assert s["sharpe"] is None and s["sortino"] is None   # 零方差/无下行
    assert s["profit_factor"] is None                      # 无亏损也无盈利
    assert s["expectancy"] is None
    assert s["cost_to_gross"] is None
    md = render_markdown(s, res)
    assert "净利" in md and "XAUUSD" in md


def test_bars_per_year_annualization():
    assert bars_per_year("H1") == pytest.approx(365.25 * 24)      # 8766.0
    assert bars_per_year("D1") == pytest.approx(365.25)
    assert bars_per_year("M1") == pytest.approx(365.25 * 1440.0)  # 525960.0
    with pytest.raises(ValueError, match="timeframe"):
        bars_per_year("XX9")


def test_data_sha256_reproducible_and_content_addressed():
    bars = make_bars()
    assert data_sha256(bars) == data_sha256(bars.copy())
    assert data_sha256(bars) == data_sha256(bars.set_index(
        pd.Index(range(100, 105))))  # 只哈希 time+OHLC 内容，不看 index
    other = make_bars()
    other.loc[2, "close"] = 100.5
    assert data_sha256(other) != data_sha256(bars)


def test_current_git_commit_falls_back_to_unknown(monkeypatch):
    import mt5quant.backtest.report as report_mod

    def boom(*a, **kw):
        raise OSError("no git")
    monkeypatch.setattr(report_mod.subprocess, "run", boom)
    assert current_git_commit() == "unknown"


def test_current_git_commit_nonzero_return_is_unknown(monkeypatch):
    import mt5quant.backtest.report as report_mod

    class Fake:
        returncode = 128
        stdout = ""

    monkeypatch.setattr(report_mod.subprocess, "run",
                        lambda *a, **kw: Fake())
    assert current_git_commit() == "unknown"


def test_render_markdown_contains_key_numbers():
    s = summarize(two_trade_result())
    md = render_markdown(s, two_trade_result())
    assert "XAUUSD" in md and "H1" in md
    assert "PF" in md and "3.00" in md
    assert "净利" in md and "200.0" in md


def test_write_report_manifest_repeatable(tmp_path, monkeypatch):
    import mt5quant.backtest.report as report_mod
    monkeypatch.setattr(report_mod, "current_git_commit",
                        lambda: "deadbeef")
    res = two_trade_result()
    out1 = tmp_path / "run1"
    out2 = tmp_path / "run2"
    p1 = write_report(str(out1), res)
    p2 = write_report(str(out2), res)
    assert p1.endswith("report.md") and p2.endswith("report.md")
    for d in (out1, out2):
        assert (d / "summary.json").exists()
        assert (d / "manifest.json").exists()
        assert (d / "report.md").exists()
    m1 = json.loads((out1 / "manifest.json").read_text(encoding="utf-8"))
    m2 = json.loads((out2 / "manifest.json").read_text(encoding="utf-8"))
    assert m1["data_sha256"] == m2["data_sha256"]       # 同输入可重复（用例 h）
    assert m1["data_sha256"] == data_sha256(res.bars)
    assert m1["git_commit"] == "deadbeef"
    assert m1["cost_model_version"] == "v0"
    assert m1["params"] == {"k": 1}
    datetime.fromisoformat(m1["created_utc"])
    s1 = json.loads((out1 / "summary.json").read_text(encoding="utf-8"))
    assert s1["net_profit"] == pytest.approx(200.0)
    # NaN 不得混进 JSON（allow_nan=False 已强制）
    assert "NaN" not in (out1 / "summary.json").read_text(encoding="utf-8")


def test_current_git_commit_marks_dirty(monkeypatch):
    """I6 回归：工作树脏时提交号必须带 +dirty（可复现性声称的前提）。"""
    import subprocess as sp
    from mt5quant.backtest import report

    class R:
        def __init__(self, rc):
            self.returncode = rc
            self.stdout = "abc123"

    calls = []
    def fake_run(args, **kw):
        calls.append(args[1])
        if args[1] == "rev-parse":
            return R(0)
        return R(1)  # diff --quiet 非零 = 工作树脏
    monkeypatch.setattr(report.subprocess, "run", fake_run)
    assert report.current_git_commit() == "abc123+dirty"

    def fake_run_clean(args, **kw):
        if args[1] == "rev-parse":
            return R(0)
        return R(0)
    monkeypatch.setattr(report.subprocess, "run", fake_run_clean)
    assert report.current_git_commit() == "abc123"


# ---------------------------------------------------------- 2026-10-25 修复轮回归

def _flat_result(times, equity_values):
    bars = pd.DataFrame({
        "time": pd.to_datetime(times),
        "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0,
        "tick_volume": 1, "spread": 1, "real_volume": 0,
    })[_COLUMNS]
    empty = pd.DataFrame(columns=list(asdict(trade()).keys()))
    return BacktestResult(trades=empty, equity=pd.Series(equity_values, dtype="float64"),
                          bars=bars, params={}, symbol="XAUUSD", timeframe="H1")


def test_annualization_uses_observed_bar_frequency_for_5_day_markets():
    """外汇/黄金/指数一周只交易 5 天：按日历 8766 根/年会把 Sharpe 高估约 18%。"""
    from mt5quant.backtest.report import observed_bars_per_year
    times = [t for t in pd.date_range("2026-01-05", "2026-03-06 23:00", freq="h")
             if t.weekday() < 5]
    span_days = (times[-1] - times[0]).total_seconds() / 86400.0
    expected = (len(times) - 1) / (span_days / 365.25)
    assert observed_bars_per_year(times) == pytest.approx(expected)
    vals = [10_000.0 + (i % 7) for i in range(len(times))]
    s = summarize(_flat_result(times, vals))
    assert s["annualization_basis"] == "observed"
    assert s["annualization_bars_per_year"] == pytest.approx(expected)
    assert s["annualization_bars_per_year"] < 0.8 * bars_per_year("H1")


def test_annualization_falls_back_to_calendar_for_short_spans():
    s = summarize(two_trade_result())          # 5 根 H1，跨度 4 小时
    assert s["annualization_basis"] == "calendar"
    assert s["annualization_bars_per_year"] == pytest.approx(bars_per_year("H1"))


def test_sortino_uses_full_sample_downside_deviation():
    import math
    times = [pd.Timestamp("2026-01-05 00:00") + timedelta(hours=i) for i in range(4)]
    res = _flat_result(times, [100.0, 110.0, 104.5, 109.725])   # 收益 +10% −5% +5%
    s = summarize(res)
    r = [0.10, -0.05, 0.05]
    mean = sum(r) / 3
    dd = math.sqrt((0.0 + 0.05 ** 2 + 0.0) / 3)   # 分母 = 全部 3 个观测
    assert s["sortino"] == pytest.approx(mean / dd * math.sqrt(bars_per_year("H1")))


def test_make_run_dir_is_unique_and_never_reused(tmp_path):
    from mt5quant.backtest.report import make_run_dir
    a = make_run_dir(str(tmp_path), "XAUUSD", "h1")
    b = make_run_dir(str(tmp_path), "XAUUSD", "h1")
    assert a != b
    for d in (a, b):
        assert os.path.isdir(d)
        assert os.path.basename(d).startswith("XAUUSD_H1_")


def test_manifest_carries_engine_strategy_data_range_and_money_basis(tmp_path, monkeypatch):
    import mt5quant.backtest.report as report_mod
    from mt5quant.backtest.engine_a import ENGINE_VERSION
    monkeypatch.setattr(report_mod, "current_git_commit", lambda: "deadbeef")
    res = two_trade_result()
    res.contract_size, res.volume_lots, res.initial_capital = 100.0, 1.0, 10_000.0
    write_report(str(tmp_path), res, strategy="pkg.mod:Strat", extra={"end": "2025-09-01"})
    m = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert m["engine_version"] == ENGINE_VERSION
    assert m["strategy"] == "pkg.mod:Strat"
    assert m["symbol"] == "XAUUSD" and m["timeframe"] == "H1" and m["n_bars"] == 5
    assert m["data_first_time"] == "2026-01-05T00:00:00"
    assert m["data_last_time"] == "2026-01-05T04:00:00"
    assert m["contract_size"] == 100.0 and m["initial_capital"] == 10_000.0
    assert m["extra"] == {"end": "2025-09-01"}
