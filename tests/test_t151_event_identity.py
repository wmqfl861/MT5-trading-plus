"""T151（第十六轮 D65/D66，2026-11-08）：事件模式 G5 拒绝、事件窗策略与
逐事件对账。

覆盖计划 docs/handoff/2026-11-08-round16-plan.md T151 五条（全部合成
数据，不读真实行情；台账一律 tmp_path 合成库）：

1. D65：`bt gate`/`_g5_orchestrate` 对事件模式卡请求 G5 在 begin_lockbox
   **之前**拒绝（真实 H102 卡 + CLI 反例断言零 runs）；
2. D66：`PreFomcAnnouncementDrift` 生成事件窗信号（事件日前一交易日
   收盘进场、事件日收盘离场；日历构造注入；无注入 → 同序列近似仅诊断）；
3. D66：逐事件对账（C005 与 C002 两张卡）——引擎 A 成交与
   `event_returns` 的入场/出场**时刻**一致，净收益差只来自已说明的成本
   口径差（闭合推导：引擎把滑点折进成交价 → 差 = 单边滑点 × 毛收益，
   二阶小量）；
4. D66：试验身份 verdict_code_sha（validation/events.py + bt.py 事件
   分支 + 日历工件哈希）——台账 additive 迁移（旧行不改）、改指纹 =
   新试验（N 计数）、LockboxTrial 匹配；
5. D66：事件模式下 bar 级 G1–G4 标"诊断，不进判定"；策略类不生成
   事件窗信号 → 直接拒绝运行。
"""
import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "bt.py"
REPO_ROOT = Path(__file__).resolve().parents[1]


def load_bt():
    spec = importlib.util.spec_from_file_location("bt_script_t151", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FlatRelCosts:
    """相对点差假成本（≈USTEC 0.67bp 半幅，与第十六轮探针同一模型）：
    点差全幅 p=6.7e-5、单边滑点 l=6.7e-6，零佣金、零隔夜费。"""

    P = 6.7e-5
    L = 6.7e-6

    def spread_p90(self, symbol, hour, *, price=None):
        return self.P * float(price if price is not None else 1.0)

    def slippage(self, symbol, hour, *, price=None):
        return self.L * float(price if price is not None else 1.0)

    def commission_per_lot(self, symbol):
        return 0.0

    def overnight_fee(self, symbol, side, from_ts, to_ts, *, price=None):
        return 0.0

    def stop_overshoot(self, *args, **kwargs):
        return 0.0


# ================================================== C005/H102：事件窗信号

def _fomc_daily_bars():
    days = pd.bdate_range("2023-09-01", "2024-06-30")
    rng = np.random.default_rng(151)
    close = 15000.0 * np.exp(np.cumsum(rng.normal(0.0002, 0.008, len(days))))
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({
        "time": days, "open": open_,
        "high": np.maximum(open_, close) * 1.002,
        "low": np.minimum(open_, close) * 0.998,
        "close": close, "tick_volume": 100, "spread": 0, "real_volume": 0})


def _v1_calendar_dict(days) -> dict:
    return {"version": 1, "complete_through": str(pd.DatetimeIndex(days)[-1].date()),
            "sessions": [{"day": str(pd.Timestamp(d).date()),
                          "close_time": f"{pd.Timestamp(d).date()}T22:00:00"}
                         for d in pd.DatetimeIndex(days)]}


def _fomc_events(bars):
    """合成冻结工件 → fomc_calendar_events 事件窗（研究装配同一链路）。"""
    import os
    import tempfile
    from mt5quant.validation.events import fomc_calendar_events
    meetings = []
    for end in ("2023-09-20", "2023-11-01", "2023-12-13",
                "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-19"):
        meetings.append({"year": int(end[:4]), "label": f"{end} Meeting",
                         "start_date": end, "end_date": end, "kind": "meeting",
                         "qualifier": None, "source_page": "synthetic.htm",
                         "announcement_date": end, "in_lm_era": True,
                         "in_default_event_set": True})
    doc = {"artifact": "t151-test", "purpose": "synthetic",
           "event_definition": {"era_start": "1994-09-27",
                                "default_event_set": "kind=meeting with qualifier None",
                                "announcement_day_rule": "last calendar day"},
           "meetings": meetings}
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "fomc_t151.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        events, report = fomc_calendar_events(
            path, sha, pd.DatetimeIndex(pd.to_datetime(bars["time"]).dt.normalize()))
    assert report["events"] == len(meetings), report
    return events


def test_prefomc_event_window_signals_from_injected_calendar():
    """注入（构造参数）事件窗 + 显式交易日历 → 信号只落在锚日前一交易日；
    引擎成交 = 事件窗（1 根 D1 持仓、signal 出场）。"""
    from mt5quant.strategies.library.fomc_preannouncement import (
        PreFomcAnnouncementDrift,
    )
    from mt5quant.backtest.engine_a import run_backtest
    bars = _fomc_daily_bars()
    cal = _v1_calendar_dict(pd.bdate_range("2023-08-01", "2024-08-31"))
    events = _fomc_events(bars)
    st = PreFomcAnnouncementDrift(event_windows=events, event_calendar=cal)
    res = run_backtest(st, bars, FlatRelCosts(), symbol="USTEC", params={},
                       contract_size=1.0, volume_lots=1.0, timeframe="D1")
    assert len(res.trades) == len(events)
    assert set(res.trades["bars_held"]) == {1}
    assert set(res.trades["exit_reason"]) == {"signal"}
    assert set(res.trades["side"]) == {"long"}
    times = pd.DatetimeIndex(pd.to_datetime(bars["time"]))
    for ev in events:
        i_entry = int(times.searchsorted(ev.entry_time))
        trade = res.trades[res.trades["entry_time"] == times[i_entry]]
        assert len(trade) == 1, f"事件 {ev.label} 无引擎成交"
        assert trade["exit_time"].iloc[0] == times[i_entry + 1]


def test_prefomc_diagnostic_fallback_shape_multiple_one_day_trades():
    """无注入日历 → 同序列日历近似（仅诊断）：10 年 D1 上多笔 1 根持仓、
    持仓时间远小于 0.9（P16-2 判据：NOT(≤1 笔 且 >90% 持仓)）。"""
    from mt5quant.strategies.library.fomc_preannouncement import (
        PreFomcAnnouncementDrift,
    )
    from mt5quant.backtest.engine_a import run_backtest
    rng = np.random.default_rng(11)
    days = pd.bdate_range("2015-01-02", "2024-12-31")
    close = 15000.0 * np.exp(np.cumsum(rng.normal(0.0004, 0.012, len(days))))
    open_ = np.r_[close[0], close[:-1]]
    bars = pd.DataFrame({
        "time": days, "open": open_,
        "high": np.maximum(open_, close) * 1.004,
        "low": np.minimum(open_, close) * 0.996,
        "close": close, "tick_volume": 1000, "spread": 0, "real_volume": 0})
    res = run_backtest(PreFomcAnnouncementDrift(), bars, FlatRelCosts(),
                       symbol="USTEC", params={}, contract_size=1.0,
                       volume_lots=0.01, timeframe="D1")
    t = pd.to_datetime(bars["time"])
    held = np.zeros(len(t), dtype=bool)
    for _, tr in res.trades.iterrows():
        held |= ((t >= pd.Timestamp(tr["entry_time"])).to_numpy()
                 & (t < pd.Timestamp(tr["exit_time"])).to_numpy())
    tim = float(held.mean())
    assert not (len(res.trades) <= 1 and tim > 0.9)     # P16-2 判据翻转
    assert len(res.trades) > 1 and tim < 0.10
    assert set(res.trades["bars_held"]) == {1}
    # 近似节奏 ≈ FOMC 常规会议量级（8 次/年 → 10 年约 80±15）
    assert 60 <= len(res.trades) <= 100


def test_prefomc_windows_without_calendar_fail_closed():
    """注入事件窗但缺显式交易日历 → SignalUnavailable（fail-closed，不
    静默回落同序列近似跑研究）。"""
    from mt5quant.strategies.library.fomc_preannouncement import (
        PreFomcAnnouncementDrift,
    )
    from mt5quant.validation.events import SignalUnavailable
    bars = _fomc_daily_bars()
    st = PreFomcAnnouncementDrift(event_windows=_fomc_events(bars))
    with pytest.raises(SignalUnavailable, match="显式交易日历"):
        st.generate_signals(bars, {})


# ================================================== T151-3：逐事件对账

def _engine_relative_nets(trades, bars, *, lots, contract_size):
    """引擎净收益折相对价（÷ 手数 × 合约大小 × 入场 bar 开盘 mid）。"""
    times = pd.DatetimeIndex(pd.to_datetime(bars["time"]))
    opens = bars["open"].to_numpy(dtype="float64")
    out = []
    for _, tr in trades.iterrows():
        i = int(times.get_loc(pd.Timestamp(tr["entry_time"])))
        out.append(float(tr["net"]) / (lots * contract_size * opens[i]))
    return out


def test_c005_per_event_reconciliation_engine_vs_event_returns():
    """C005/H102：引擎 A 成交与 event_returns 逐事件对账——入场/出场
    **时刻**（bar 时间）逐笔一致；净收益差只来自已说明的成本口径差。

    口径差闭合推导（FlatRelCosts：点差全幅 P、单边滑点 L，零佣金/隔夜）：
    - event_returns：毛收益（开盘 mid 对开盘 mid）− [max(点差两侧，基准
      价=入场价) + 双边滑点（基准价=入场价）] = gross − (P + 2L)；
    - 引擎 A 把成本折进成交价：多头 entry = O_e(1+P+L)、exit = O_x(1−L)
      → 相对净 = gross − P − L(1+r)（r = O_x/O_e），差 = L(1−r)；
      空头 entry = O_e(1−L)、exit = O_x(1+P+L)（点差计在**出场腿**，
      基准价=出场价 O_x）→ 差 = (P+L)(1−r)；
    - 闭合界（覆盖双向）：|引擎相对净 − event 净| = (P+L)×|毛收益|——
      点差/滑点折进成交价与显式扣除之间的二阶交叉项。"""
    from mt5quant.strategies.library.fomc_preannouncement import (
        PreFomcAnnouncementDrift,
    )
    from mt5quant.backtest.engine_a import run_backtest
    from mt5quant.validation.events import declared_event_side, event_returns
    bars = _fomc_daily_bars()
    events = _fomc_events(bars)
    cal = _v1_calendar_dict(pd.bdate_range("2023-08-01", "2024-08-31"))
    st = PreFomcAnnouncementDrift(event_windows=events, event_calendar=cal)
    res = run_backtest(st, bars, FlatRelCosts(), symbol="USTEC", params={},
                       contract_size=1.0, volume_lots=1.0, timeframe="D1")
    side_fn = declared_event_side("static_long")
    frame = event_returns(bars, events, side_fn=side_fn, costs=FlatRelCosts(),
                          symbol="USTEC", contract_size=1.0)
    assert len(frame) == len(events)
    eng_by_entry = {pd.Timestamp(t["entry_time"]): t
                    for _, t in res.trades.iterrows()}
    assert len(eng_by_entry) == len(events)
    for label, row in frame.iterrows():
        trade = eng_by_entry[pd.Timestamp(row["entry_bar_time"])]
        # 入场/出场时刻逐笔一致（引擎 bar 时间 = event_returns 成交 bar）
        assert pd.Timestamp(trade["entry_time"]) == pd.Timestamp(
            row["entry_bar_time"]), label
        assert pd.Timestamp(trade["exit_time"]) == pd.Timestamp(
            row["exit_bar_time"]), label
        assert trade["side"] == row["side"]
        assert int(trade["bars_held"]) == 1
        gross = float(row["gross"])
        eng_net = float(trade["net"]) / (
            float(trade["volume_lots"]) * bars["open"].iloc[
                pd.DatetimeIndex(pd.to_datetime(bars["time"])).get_loc(
                    pd.Timestamp(trade["entry_time"]))])
        diff = eng_net - float(row["net"])
        # 已说明口径差的闭合界：|差| = (P+L)×|毛收益|（推导见 docstring）
        assert abs(diff) <= (FlatRelCosts.P + FlatRelCosts.L) * abs(gross) \
            + 1e-12, (label, diff, gross)


# ---- C002/H101（b7 月末定盘，零改动直接消费既有类）

def _b7_world():
    """C002 研究装配形状的合成世界：EURUSD（成交）+ USTEC（信号）H1，
    伦敦冬令期（GMT==UTC，14:00 UTC bar 即伦敦本地 14:00 事件 bar）；
    USTEC 月度水平交替升降 → mtd_sign 方向逐月交替。"""
    from mt5quant.strategies.library.b7_monthend_hedge_rebalance import (
        LOOKBACK_BARS,
    )
    days = pd.bdate_range("2023-07-01", "2024-03-29")      # 覆盖首事件的锚月
    hours = list(range(7, 21))                             # 07..20 UTC
    # 月度水平（Jul 2023..Mar 2024 交替升降 → mtd_sign 交替 ±1）
    levels = {m: v for m, v in zip(
        pd.period_range("2023-07", "2024-03", freq="M"),
        (1.000, 1.010, 0.995, 1.008, 0.992, 1.006, 0.990, 1.004, 0.996))}

    def eur_rows():
        rng = np.random.default_rng(7)
        rows, prev_close = [], 1.08
        for d in days:
            level = 1.08 * levels[d.to_period("M")]
            for j, h in enumerate(hours):
                o = prev_close + 0.0002 * np.sin(3.0 * j + d.dayofyear)
                c = o + 0.0002 * np.cos(2.0 * j + d.dayofyear)
                hi = max(o, c) + 0.0003
                lo = min(o, c) - 0.0003
                t = pd.Timestamp(d) + pd.Timedelta(hours=h)
                rows.append({"time": t, "open": o, "high": hi, "low": lo,
                             "close": c, "tick_volume": 10, "spread": 0,
                             "real_volume": 0, "_lvl": level})
                prev_close = c
        return pd.DataFrame(rows)

    def ust_rows():
        rows = []
        for d in days:
            level = 100.0 * levels[d.to_period("M")]
            c = level * (1.0 + 0.0005 * np.sin(d.dayofyear))
            for h in hours:
                t = pd.Timestamp(d) + pd.Timedelta(hours=h)
                rows.append({"time": t, "open": c, "high": c * 1.0001,
                             "close": c, "low": c * 0.9999,
                             "close_time": t + pd.Timedelta(hours=1),
                             "tick_volume": 10, "spread": 0, "real_volume": 0})
        return pd.DataFrame(rows)

    def cal_dict(sym):
        return {"version": 1, "complete_through": str(days[-1].date()),
                "sessions": [{"day": str(d.date()),
                              "close_time": f"{d.date()}T21:00:00"}
                             for d in days]}

    return eur_rows(), ust_rows(), cal_dict("EURUSD"), cal_dict("USTEC"), days


def test_c002_per_event_reconciliation_engine_vs_event_returns():
    """C002/H101：b7（零改动）带真实输入（信号序列 + 双显式日历）在
    引擎 A 上的成交与 london_fix 事件窗 event_returns 逐事件对账——
    入场/出场时刻一致；净收益差只来自同一份已说明口径差（见 C005 用例
    的闭合推导：多头 |差| = L×|毛收益|、空头 |差| = (P+L)×|毛收益|
    ——引擎空头点差计在出场腿、基准价随出场价缩放）。"""
    from mt5quant.strategies.library.b7_monthend_hedge_rebalance import (
        MonthEndHedgeRebalance,
    )
    from mt5quant.backtest.engine_a import run_backtest
    from mt5quant.validation.events import (
        declared_event_side, event_returns, london_fix_events, trading_calendar,
    )
    eur, ust, eur_cal, ust_cal, days = _b7_world()
    st = MonthEndHedgeRebalance(signal_bars=ust, signal_timeframe="H1",
                                signal_calendar=ust_cal, event_calendar=eur_cal)
    res = run_backtest(st, eur, FlatRelCosts(), symbol="EURUSD",
                       params={"threshold_pct": 0.0}, contract_size=1.0,
                       volume_lots=1.0, timeframe="H1")
    assert len(res.trades) >= 5, "月末事件应有逐月成交"
    assert set(res.trades["exit_reason"]) == {"signal"}, dict(
        res.trades["exit_reason"].value_counts())
    # 事件集 = _h_event_setup london_fix 分支同一构造（月末交易日 16:00
    # 定盘、提前 60 分钟入场）
    cal = trading_calendar(eur_cal)
    complete = cal.attrs["complete_through"]
    last_days = pd.DatetimeIndex([
        g.day.iloc[-1] for _, g in cal.groupby(cal.day.dt.to_period("M"))
        if g.day.iloc[0].to_period("M").end_time.normalize() <= complete])
    events = [e for e in london_fix_events(last_days)
              if e.entry_time >= pd.Timestamp("2023-08-01")]   # 首月锚前置月已备
    side_fn = declared_event_side("mtd_sign", signal_bars=ust,
                                  signal_timeframe="H1", calendar=ust_cal,
                                  threshold=0.0)
    frame = event_returns(eur, events, side_fn=side_fn, costs=FlatRelCosts(),
                          symbol="EURUSD", contract_size=1.0)
    eng_by_entry = {pd.Timestamp(t["entry_time"]): t
                    for _, t in res.trades.iterrows()}
    assert len(eng_by_entry) == len(frame) == len(events)
    opens = eur["open"].to_numpy(dtype="float64")
    times = pd.DatetimeIndex(pd.to_datetime(eur["time"]))
    for label, row in frame.iterrows():
        trade = eng_by_entry[pd.Timestamp(row["entry_bar_time"])]
        assert pd.Timestamp(trade["entry_time"]) == pd.Timestamp(
            row["entry_bar_time"]), label
        assert pd.Timestamp(trade["exit_time"]) == pd.Timestamp(
            row["exit_bar_time"]), label
        assert trade["side"] == row["side"]
        assert int(trade["bars_held"]) == 1
        gross = float(row["gross"])
        i = int(times.get_loc(pd.Timestamp(trade["entry_time"])))
        eng_net = float(trade["net"]) / (float(trade["volume_lots"]) * opens[i])
        diff = eng_net - float(row["net"])
        assert abs(diff) <= (FlatRelCosts.P + FlatRelCosts.L) * abs(gross) \
            + 1e-12, (label, diff, gross)
    assert set(frame["side"]) == {"long", "short"}   # mtd_sign 双向都覆盖


# ================================================== T151-1/D65：G5 拒绝

def test_g5_orchestrate_rejects_event_card_before_lockbox(monkeypatch):
    """真实 H102 卡（轮次 [evaluation] mode="event"）：_g5_orchestrate 在
    begin_lockbox 之前抛 ValueError（消息含 D65）——P16-1 的复现路径。"""
    from mt5quant.research import cards as cards_mod
    mod = load_bt()
    card = cards_mod.load_card(REPO_ROOT / "research" / "cards" / "H102.toml")
    assert mod._round_eval_mode(card) == "event"

    def _must_not_run(*a, **k):
        raise AssertionError("begin_lockbox must not be reached (D65)")

    monkeypatch.setattr(mod.vgate, "begin_lockbox", _must_not_run)
    from types import SimpleNamespace
    spec = SimpleNamespace(symbol="USTEC", contract_size=1.0,
                           initial_capital=10_000.0, sizing=None,
                           volume_lots=0.01, vol_min=None, vol_step=None,
                           vol_max=None)
    with pytest.raises(ValueError, match="D65"):
        mod._g5_orchestrate(
            None, card, strategy=object(), spec=spec, params={},
            base_summary={"trade_count": 1}, bars=_fomc_daily_bars(),
            costs=FlatRelCosts(), session_policy=None, data_root=".",
            approved_by="probe", cfg=None, trial=None, round_id=card.round,
            round_lockbox_uses_max=1)


def test_gate_cli_rejects_g5_for_event_card_before_any_runs(
        tmp_path, monkeypatch, capsys):
    """CLI：事件模式卡请求 G5 → D65 拒绝（消息引用 D65），零 runs、零
    lockbox_uses（不烧一次性）。"""
    from tests.test_fomc_calendar_engine import (
        _setup_fomc_round, _fomc_gate_args, meeting, write_calendar,
    )
    from mt5quant.research.ledger import Ledger
    mod = load_bt()
    root, cards_dir, ledger_path = _setup_fomc_round(tmp_path, monkeypatch, mod)
    cal_path, sha = write_calendar(tmp_path, [meeting("2024-01-31")])
    args = _fomc_gate_args(root, cards_dir, ledger_path, cal_path, sha)
    i = args.index("--gates")
    args[i + 1] = "G3,V3,G5"
    args += ["--approve", "--approved-by", "t151-test"]
    assert mod.main(args) == 1
    out = capsys.readouterr().out
    assert "D65" in out and "事件模式" in out and "begin_lockbox" in out
    with Ledger(ledger_path) as led:
        assert led.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert led.conn.execute(
            "SELECT COUNT(*) FROM lockbox_uses").fetchone()[0] == 0


# ================================================== T151-4：verdict_code_sha

def test_verdict_code_sha_composition_and_sensitivity(tmp_path):
    """指纹组成逐字（D66）：events.py + bt.py（事件分支所在模块）+ 日历
    工件哈希——任一成分变化 → 指纹变化；CRLF 规范化后同指纹（跨平台）。"""
    mod = load_bt()
    a1 = mod._verdict_code_sha(calendar_component=f"fomc:{'a' * 64}")
    a2 = mod._verdict_code_sha(calendar_component=f"fomc:{'a' * 64}")
    assert a1 == a2 and len(a1) == 64
    assert a1 != mod._verdict_code_sha(calendar_component=f"fomc:{'b' * 64}")
    assert a1 != mod._verdict_code_sha(calendar_component=None)
    ev_py = tmp_path / "events.py"
    bt_py = tmp_path / "bt.py"
    ev_py.write_text("x = 1\n", encoding="utf-8")
    bt_py.write_text("y = 1\n", encoding="utf-8")
    comp = dict(calendar_component="none", events_path=ev_py, bt_path=bt_py)
    c1 = mod._verdict_code_sha(**comp)
    ev_py.write_text("x = 2\n", encoding="utf-8")     # 判决代码改动 → 指纹变
    c2 = mod._verdict_code_sha(**comp)
    assert c1 != c2
    ev_py.write_bytes(b"x = 2\r\n")                   # CRLF 检出 → 同指纹
    assert mod._verdict_code_sha(**comp) == c2


def test_ledger_verdict_code_sha_additive_migration(tmp_path):
    """台账迁移按铁律：旧行不改——旧库（无该列）重开后补列、旧行内容值
    一字不改（verdict_code_sha=NULL 不回填），哈希链重算后校验通过；
    新行写入指纹。"""
    from mt5quant.research.ledger import Ledger
    path = tmp_path / "led.sqlite"
    kw = dict(card_id="H102", card_version=1, strategy="s", params={},
              symbol="USTEC", timeframe="D1", data_first="", data_last="",
              data_sha256="d" * 64, engine_version="A",
              cost_model_version="c", gates_hash="g",
              metrics={"sharpe": 1.0}, actor="bt", round_id="R3")
    with Ledger(path) as led:
        rid = led.record_run(**kw)
        row = led.conn.execute(
            "SELECT * FROM runs WHERE run_id=?", (rid,)).fetchone()
        before = {k: row[k] for k in row.keys()
                  if k not in ("prev_hash", "row_hash", "verdict_code_sha")}
    # 模拟 T151 之前的旧库：列不存在（旧行没有裁决指纹概念）
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE runs DROP COLUMN verdict_code_sha")
    con.commit()
    con.close()
    with Ledger(path) as led:                        # 打开即迁移
        assert led.verify_chain() == []
        row = led.conn.execute(
            "SELECT * FROM runs WHERE run_id=?", (rid,)).fetchone()
        assert row["verdict_code_sha"] is None       # 旧行不回填
        for k, v in before.items():
            assert row[k] == v, k                    # 旧行内容值一字不改
        rid2 = led.record_run(**kw, verdict_code_sha="e" * 64)
        row2 = led.conn.execute(
            "SELECT verdict_code_sha FROM runs WHERE run_id=?",
            (rid2,)).fetchone()
        assert row2["verdict_code_sha"] == "e" * 64  # 新行写入
        assert led.verify_chain() == []


def test_verdict_code_sha_is_trial_identity(tmp_path):
    """verdict_code_sha 进试验身份：同七元组不同指纹 = 两个去重试验
    （H16-2 的口子闭合）；LockboxTrial 匹配按指纹区分，None 只匹配旧行。"""
    from mt5quant.research.ledger import Ledger
    from mt5quant.validation.gates import LockboxTrial, _matching_gate_runs
    mod = load_bt()
    path = tmp_path / "identity.sqlite"
    kw = dict(card_id="H102", card_version=1, strategy="s", params={},
              symbol="USTEC", timeframe="D1", data_first="", data_last="",
              data_sha256="d" * 64, engine_version="A",
              cost_model_version="c", gates_hash="g",
              metrics={"sharpe": 1.0}, actor="bt", round_id="R3")
    with Ledger(path) as led:
        led.record_run(**kw)                              # 旧行为（NULL）
        led.record_run(**kw)                              # 幂等重录
        assert led.round_n_dedup("R3") == 1
        led.record_run(**kw, verdict_code_sha="1" * 64)   # 改判决代码重跑
        assert led.round_n_dedup("R3") == 2               # = 新试验
        led.record_run(**kw, verdict_code_sha="1" * 64)   # 同指纹幂等
        assert led.round_n_dedup("R3") == 2
        r_sha = led.conn.execute(
            "SELECT run_id FROM runs WHERE verdict_code_sha=?",
            ("1" * 64,)).fetchone()["run_id"]
        led.record_gate(r_sha, "G3", True, {"segments": []})
        base = dict(params={}, data_sha256="d" * 64, cost_model_version="c",
                    strategy_sha="legacy", gates_hash="g")
        assert _matching_gate_runs(led, "H102", LockboxTrial(**base), "G3") \
            or True     # NULL 行没有 gate——只验指纹行的匹配语义：
        hits = _matching_gate_runs(
            led, "H102", LockboxTrial(**base, verdict_code_sha="1" * 64), "G3")
        assert hits and hits[0][0] == r_sha
        assert _matching_gate_runs(
            led, "H102", LockboxTrial(**base, verdict_code_sha="2" * 64),
            "G3") == []
        assert _matching_gate_runs(led, "H102", LockboxTrial(**base), "G3") == []


# ================================================== T151-5：诊断标记与脚手架拒绝

def test_event_gate_marks_bar_gates_diagnostic_and_records_verdict_sha(
        tmp_path, monkeypatch, capsys):
    """事件模式 G1–G4 标"诊断，不进判定"（details+报告），退出码只随
    判定关卡（V3）；runs 行带 verdict_code_sha。"""
    from tests.test_fomc_calendar_engine import (
        _setup_fomc_round, _fomc_gate_args, meeting, write_calendar,
    )
    from mt5quant.research.ledger import Ledger
    mod = load_bt()
    root, cards_dir, ledger_path = _setup_fomc_round(tmp_path, monkeypatch, mod)
    cal_path, sha = write_calendar(
        tmp_path, [meeting("2024-01-31"), meeting("2024-03-20"),
                   meeting("2024-11-29")])
    args = _fomc_gate_args(root, cards_dir, ledger_path, cal_path, sha)
    i = args.index("--gates")
    args[i + 1] = "G1,V3"
    rc = mod.main(args)
    out = capsys.readouterr().out
    assert "诊断，不进判定" in out and "verdict_code_sha" in out
    with Ledger(ledger_path) as led:
        g1 = json.loads(led.conn.execute(
            "SELECT details_json FROM gates WHERE gate='G1'").fetchone()
            ["details_json"])
        v3 = json.loads(led.conn.execute(
            "SELECT details_json FROM gates WHERE gate='V3'").fetchone()
            ["details_json"])
        assert g1["diagnostic_only"] is True
        assert "diagnostic_only" not in v3
        assert len(v3.get("verdict_code_sha") or "") == 64
        run = led.conn.execute(
            "SELECT verdict_code_sha FROM runs WHERE card_id='H704'"
        ).fetchone()
        assert run["verdict_code_sha"] == v3["verdict_code_sha"]
        assert run["verdict_code_sha"] == mod._verdict_code_sha(
            calendar_component=f"fomc:{sha}")
    # G1（交易数不足门槛）必失败；退出码只随 V3 判定，不随诊断关卡
    assert not g1["passed"]
    assert rc == (0 if v3["passed"] else 1)


def test_event_mode_scaffold_strategy_refused_before_runs(
        tmp_path, monkeypatch, capsys):
    """D66：事件模式卡的策略类不生成事件窗信号（BuyHold 脚手架——
    H16-2 的被测对象）→ 直接拒绝运行（零 runs）。"""
    from tests.test_fomc_calendar_engine import (
        _setup_fomc_round, _fomc_gate_args, meeting, write_calendar,
    )
    from mt5quant.research.ledger import Ledger
    mod = load_bt()
    root, cards_dir, ledger_path = _setup_fomc_round(tmp_path, monkeypatch, mod)
    cal_path, sha = write_calendar(tmp_path, [meeting("2024-01-31")])
    args = _fomc_gate_args(root, cards_dir, ledger_path, cal_path, sha)
    i = args.index("--strategy")
    args[i + 1] = "mt5quant.strategies.library.buy_hold:BuyHold"
    assert mod.main(args) == 1
    out = capsys.readouterr().out
    assert "不生成事件窗信号" in out and "D66" in out
    with Ledger(ledger_path) as led:
        assert led.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


# ====================================== T151 聚焦评审修复（F-1/F-2，2026-11-07）

def test_screen_combo_strategy_holds_injected_event_windows(
        tmp_path, monkeypatch, capsys):
    """F-1（T151 聚焦评审）：bt screen 组合回测的每组合策略实例持有装配
    段注入的**真实**事件窗（冻结工件构造的事件集，非每 6 周三的同序列
    近似）+ 显式交易日历——组合回测成交逐位落在事件窗锚日（非近似断言）。
    gate 路径用持久实例（不经 _combo_strategy），既有 gate 测试作证零变化。"""
    from tests.test_fomc_calendar_engine import (
        _setup_fomc_round, meeting, write_calendar,
    )
    from mt5quant.research.ledger import Ledger
    mod = load_bt()
    root, cards_dir, ledger_path = _setup_fomc_round(tmp_path, monkeypatch, mod)
    cal_path, sha = write_calendar(
        tmp_path, [meeting("2024-01-31"), meeting("2024-03-20"),
                   meeting("2024-05-01"), meeting("2024-11-29")])
    captured = []
    real_run = mod.run_backtest

    def spy(strategy, *a, **k):
        result = real_run(strategy, *a, **k)
        captured.append((strategy, result))
        return result

    monkeypatch.setattr(mod, "run_backtest", spy)
    rc = mod.main(
        ["screen", "--card", "H704", "--allow-unbounded-data",
         "--cards-dir", str(cards_dir), "--data-root", str(root),
         "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.fomc_preannouncement:"
                       "PreFomcAnnouncementDrift",
         "--costs", "zero", "--volume-lots", "0.01",
         "--event-kind", "fomc_calendar",
         "--fomc-calendar", str(cal_path), "--fomc-calendar-sha256", sha,
         "--event-days-start", "2024-01-01", "--event-days-end", "2024-12-31"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert captured, out
    st, res = captured[0]
    # 实例级：持有装配段注入的事件窗 + 显式日历（修复前为 None → 近似）
    assert st.event_windows is not None
    assert [e.label for e in st.event_windows] == [
        "fomc_calendar:2024-01-31", "fomc_calendar:2024-03-20",
        "fomc_calendar:2024-05-01", "fomc_calendar:2024-11-29"]
    assert st.event_calendar is not None
    # 行为级（非近似）：4 事件 4 笔、入场逐位 = 事件锚日（前一交易日），
    # 1 根持仓——近似路径给的是每 6 周三节奏（入场为周一，集合不同）
    assert len(res.trades) == 4
    assert set(res.trades["bars_held"]) == {1}
    assert set(pd.to_datetime(res.trades["entry_time"])) == {
        e.entry_time for e in st.event_windows}


def test_screen_warns_dropped_event_when_signal_day_bar_missing(
        tmp_path, monkeypatch, capsys):
    """F-2（T151 聚焦评审）：7 事件、1 个信号日（锚日前一显式交易日）
    缺 bar → 引擎 6 笔（缺 bar fail-closed 丢事件，成交语义不变），screen
    输出显式 dropped_events=1 告警行，计数随 metrics 入台账——丢事件不再
    静默（评审实证：删除信号日 bar 后 6 vs 7 零告警）。"""
    import pandas as _pd
    from tests.test_fomc_calendar_engine import (
        _setup_fomc_round, meeting, write_calendar,
    )
    from mt5quant.research.ledger import Ledger
    mod = load_bt()
    root, cards_dir, ledger_path = _setup_fomc_round(tmp_path, monkeypatch, mod)
    # 7 个公告日（锚=前一交易日、信号日=锚前一交易日，连续日历下即 D-2/D-1）；
    # 删 2024-06-17 bar（2024-06-19 事件的信号日；非其他事件的锚/公告日）
    meetings = [meeting(d) for d in ("2024-02-28", "2024-03-20", "2024-05-01",
                                     "2024-06-19", "2024-07-31", "2024-09-18",
                                     "2024-11-27")]
    cal_path, sha = write_calendar(tmp_path, meetings)
    pq = root / "bars" / "XAUUSD" / "D1" / "data.parquet"
    bars = _pd.read_parquet(pq)
    kept = bars[_pd.to_datetime(bars["time"]).dt.normalize()
                != _pd.Timestamp("2024-06-17")]
    assert len(kept) == len(bars) - 1
    kept.to_parquet(pq)
    rc = mod.main(
        ["screen", "--card", "H704", "--allow-unbounded-data",
         "--cards-dir", str(cards_dir), "--data-root", str(root),
         "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.fomc_preannouncement:"
                       "PreFomcAnnouncementDrift",
         "--costs", "zero", "--volume-lots", "0.01",
         "--event-kind", "fomc_calendar",
         "--fomc-calendar", str(cal_path), "--fomc-calendar-sha256", sha,
         "--event-days-start", "2024-01-01", "--event-days-end", "2024-12-31"])
    out = capsys.readouterr().out
    assert rc == 0, out
    # 显式告警行：7 事件 vs 6 笔 → dropped_events=1
    assert "事件对账" in out and "dropped_events=1" in out
    with Ledger(ledger_path) as led:
        row = led.conn.execute(
            "SELECT metrics_json FROM runs WHERE card_id='H704'").fetchone()
    m = json.loads(row["metrics_json"])["metrics"]   # record_run 包一层 metrics
    assert m["usable_events"]["XAUUSD"] == 7   # 事件覆盖仍在（锚/公告日有 bar）
    assert m["trade_count"] == 6               # 引擎丢 1 事件（语义不变）
    assert m["dropped_events"] == 1            # 计数随 metrics 入台账
