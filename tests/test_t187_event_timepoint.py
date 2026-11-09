"""T187（第十七轮 P1，D74/H17-1，2026-11-09）：事件时间口径——价格时点。

计划 docs/handoff/2026-11-08-round17-plan.md §2 P1 T187 三条测试的
合成实现（全部合成数据；真实冻结装配上 213 事件的等价锚点与 §6 重算
在主树 .superpowers/sdd/2026-11-09-t187/ 离线留档）：

1. 事件窗按价格时点定义：fomc_calendar_events 声明
   price_timepoint="day_close"（入场=锚日收盘、出场=公告日收盘）；
2. 合并序列接缝两侧同一种 bar 口径：close-only 外部段（manifest
   close_only=true）在 merged_research_series 里改写为"开=前一观测
   收盘、收=当日收盘"（与 MT5 D1 同口径）；
3. 接缝合成数据（close-only 段接 OHLC 段）上，两段量到的事件收益都
   **逐位**等于登记窗收益（锚日收→公告日收）；引擎 A 事件窗策略与
   研究口径、登记窗三方逐位一致；
4. 等价锚点的合成形态：close-only 行（open==close）上 day_close 与
   bar_open 两种解析的逐事件收益逐位相同（R3 主检验段逐位不变性的
   机制说明——FRED 段 open==close，改写只动 open/high/low、close 列
   逐字节不动）。
"""
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from mt5data.ext.approvals import SOURCE_APPROVALS
from mt5data.ext.core import ExtSeries, save_ext_series
from mt5data.ext.merge import merged_research_series, rewrite_close_only_segment
from mt5data.store import _COLUMNS, save_bars


class ZeroCosts:
    """零成本（三方对账只比价格时点，不掺成本口径差）。"""

    def spread_p90(self, symbol, hour, *, price=None):
        return 0.0

    def slippage(self, symbol, hour, *, price=None):
        return 0.0

    def commission_per_lot(self, symbol):
        return 0.0

    def overnight_fee(self, *args, **kwargs):
        return 0.0

    def stop_overshoot(self, *args, **kwargs):
        return 0.0


# ------------------------------------------------------------ 改写单元

def test_rewrite_close_only_segment_unit():
    """开=前一观测收盘、收=当日收盘（首行退化开=收）；high/low 自洽；
    close 列逐字节不动；不改入参。"""
    closes = np.array([100.0, 101.0, 99.0, 100.5])
    df = pd.DataFrame({
        "time": pd.bdate_range("2021-05-03", periods=4),
        "open": closes.copy(), "high": closes.copy(),
        "low": closes.copy(), "close": closes.copy(),
        "tick_volume": 0, "spread": 0, "real_volume": 0})[_COLUMNS]
    before = df.copy()
    out = rewrite_close_only_segment(df)
    assert df.equals(before), "改写不得改入参"
    assert list(out["close"]) == list(closes), "close 列逐字节不动"
    assert list(out["open"]) == [100.0, 100.0, 101.0, 99.0]
    assert list(out["high"]) == [100.0, 101.0, 101.0, 100.5]
    assert list(out["low"]) == [100.0, 100.0, 99.0, 99.0]
    # 空段安全
    empty = df.iloc[0:0]
    assert len(rewrite_close_only_segment(empty)) == 0


# ------------------------------------------------------------ 合并集成

def _ext_manifest(source, symbol, tf, bars, *, close_only=False,
                  cutoff="2099-01-01"):
    years = sorted({int(t.year) for t in pd.DatetimeIndex(bars["time"])})
    manifest = dict(
        source=source, symbol=symbol, timeframe=tf,
        source_url="https://example.invalid/" + source,
        price_side="synthetic close-only" if close_only else "synthetic",
        original_timezone="UTC", dst_rule="synthetic", terms_note="synthetic only",
        downloaded_at_utc="2026-11-09T00:00:00Z",
        source_files=[{"name": "synthetic.csv", "sha256": "0" * 64}],
        converter_version=1,
        usage_restriction=SOURCE_APPROVALS[source]["usage_restriction"],
        yearly_verification={str(y): {"used": True} for y in years},
        research_cutoff=cutoff, rows=int(len(bars)),
        first_time=pd.Timestamp(bars["time"].iloc[0]).isoformat(),
        last_time=pd.Timestamp(bars["time"].iloc[-1]).isoformat())
    if close_only:
        manifest["close_only"] = True
    return manifest


def _seam_store(tmp_path):
    """FRED close-only 段（2021-05-03..07-01）接 MT5 OHLC 段（07-02 起）。

    两次公告（FRED 段 06-16、MT5 段 07-28）：公告日 close/close +3%、
    锚日持平——若事件窗错一天（量到锚日开盘→公告日开盘≈锚日自己），
    MT5 段会量到 ≈0%，测试即失败。
    """
    fred_ann, mt5_ann = "2021-06-16", "2021-07-28"
    fred_days = pd.bdate_range("2021-05-03", "2021-07-01")
    mt5_days = pd.bdate_range("2021-07-02", "2021-09-30")
    days = fred_days.append(mt5_days)
    rng = np.random.default_rng(187)
    r = rng.normal(0.0, 0.0005, len(days))
    for ann in (fred_ann, mt5_ann):
        i = int(days.get_loc(pd.Timestamp(ann)))
        r[i] = 0.03           # 公告日 close/close = +3%
        r[i - 1] = 0.0        # 锚日 close/close 持平
    close = 14000.0 * np.exp(np.cumsum(r))
    fred_close = close[: len(fred_days)]
    mt5_close = close[len(fred_days):]
    # FRED 原始形态（adapters.fred_daily_from_bytes）：open=high=low=close
    fred = pd.DataFrame({
        "time": fred_days, "open": fred_close, "high": fred_close,
        "low": fred_close, "close": fred_close,
        "tick_volume": 0, "spread": 0, "real_volume": 0})[_COLUMNS]
    # MT5 D1 形态：开=前一收盘（接缝处 = FRED 末行收盘），高低价真实包络
    mt5_open = np.r_[fred_close[-1], mt5_close[:-1]]
    mt5 = pd.DataFrame({
        "time": mt5_days, "open": mt5_open,
        "high": np.maximum(mt5_open, mt5_close) * 1.001,
        "low": np.minimum(mt5_open, mt5_close) * 0.999,
        "close": mt5_close, "tick_volume": 100, "spread": 0,
        "real_volume": 0})[_COLUMNS]
    root = tmp_path / "data_research"
    save_bars(str(root), "USTEC", "D1", mt5)
    save_ext_series(str(root / "ext"), ExtSeries(
        df=fred, manifest=_ext_manifest("fred", "USTEC", "D1", fred,
                                        close_only=True,
                                        cutoff="2021-10-01T00:00:00")))
    return root, str(fred_ann), str(mt5_ann)


def test_merge_rewrites_close_only_ext_convention(tmp_path):
    """合并序列：close_only 段改写（开=前一保留行收盘、首行退化）；非
    close_only 来源（histdata）逐字节不动；MT5 段不受影响。"""
    root, _, _ = _seam_store(tmp_path)
    bars, tags, gaps, bad = merged_research_series(
        root, "USTEC", "D1", allow_unbounded=True)
    assert bad == [] and gaps == []
    ext = bars[bars["time"] < pd.Timestamp("2021-07-02")].reset_index(drop=True)
    close = ext["close"].to_numpy(dtype="float64")
    open_ = ext["open"].to_numpy(dtype="float64")
    assert open_[0] == close[0]                     # 首行无前一观测 → 退化
    assert (open_[1:] == close[:-1]).all()          # 开 = 前一观测收盘
    assert (ext["high"].to_numpy() >= np.maximum(open_, close)).all()
    assert (ext["low"].to_numpy() <= np.minimum(open_, close)).all()
    # MT5 段照常并入（ext 的 research_cutoff 声明上界迟于 MT5 段末，
    # 未写锁箱 → allow_unbounded 直通；段的 bar 值未被改写触碰）
    mt5 = bars[bars["time"] >= pd.Timestamp("2021-07-02")]
    assert len(mt5) == len(pd.bdate_range("2021-07-02", "2021-09-30"))
    assert tags[0].startswith("ext:fred:")
    # 非 close_only 来源不触发改写：histdata 行 open==close 原样
    days = pd.bdate_range("2021-05-03", periods=5)
    ohlc_same = pd.DataFrame({
        "time": days, "open": 100.0, "high": 100.0, "low": 100.0,
        "close": 100.0, "tick_volume": 0, "spread": 0,
        "real_volume": 0})[_COLUMNS]
    root2 = tmp_path / "hist_root"
    save_ext_series(str(root2 / "ext"), ExtSeries(
        df=ohlc_same,
        manifest=_ext_manifest("histdata", "XAUUSD", "D1", ohlc_same)))
    bars2, _, _, _ = merged_research_series(
        root2, "XAUUSD", "D1", allow_unbounded=True)
    assert len(bars2) == 5
    assert (bars2["open"] == bars2["close"]).all(), \
        "非 close_only 来源不得被改写"


# ------------------------------------------------- 接缝两段 == 登记窗

def _fomc_events_for(bars, announcements):
    from mt5quant.validation.events import fomc_calendar_events
    meetings = [{"year": int(d[:4]), "label": f"{d} Meeting", "start_date": d,
                 "end_date": d, "kind": "meeting", "qualifier": None,
                 "source_page": "synthetic.htm", "announcement_date": d,
                 "in_lm_era": True, "in_default_event_set": True}
                for d in announcements]
    doc = {"artifact": "t187-seam", "purpose": "synthetic",
           "event_definition": {"era_start": "1994-09-27",
                                "default_event_set": "kind=meeting with qualifier None",
                                "announcement_day_rule": "last calendar day"},
           "meetings": meetings}
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "fomc_t187.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        days = pd.DatetimeIndex(pd.to_datetime(bars["time"]).dt.normalize())
        events, report = fomc_calendar_events(path, sha, days)
    assert report["events"] == len(announcements), report
    assert all(e.price_timepoint == "day_close" for e in events)
    return events


def test_seam_both_segments_measure_registered_window(tmp_path):
    """T187 测试 3a：接缝合成数据上两段事件收益都逐位等于登记窗收益。

    登记窗 = 锚日收盘 → 公告日收盘（合并序列 close 列）。day_close 成交
    取 bar 收盘——两段同一解析，与段的 bar 形态（close-only / OHLC）
    无关；这正是 H17-1 修复后 P17-1 探针两段一致的原因。"""
    from mt5quant.validation.events import declared_event_side, event_returns
    root, fred_ann, mt5_ann = _seam_store(tmp_path)
    bars, tags, _, _ = merged_research_series(
        root, "USTEC", "D1", allow_unbounded=True)
    bars["_research_source"] = list(tags)
    events = _fomc_events_for(bars, (fred_ann, mt5_ann))
    frame = event_returns(bars, events,
                          side_fn=declared_event_side("static_long"),
                          costs=ZeroCosts(), symbol="USTEC")
    times = pd.DatetimeIndex(pd.to_datetime(bars["time"]))
    close = bars["close"].to_numpy(dtype="float64")

    def registered(ann):
        i = int(times.get_loc(pd.Timestamp(ann)))
        return float(close[i] / close[i - 1] - 1.0)

    for ann in (fred_ann, mt5_ann):
        row = frame.loc[f"fomc_calendar:{ann}"]
        reg = registered(ann)
        assert row["gross"] == reg, (ann, row["gross"], reg)   # 逐位（零成本）
        assert row["net"] == reg
        # 出场 bar = 公告日 bar（价格时点所在 bar）；非 close-only 的
        # MT5 段上，该 bar 开盘价是锚日收盘的近似而非登记价格本身
        assert pd.Timestamp(row["exit_bar_time"]) == pd.Timestamp(ann)
    # MT5 段量到公告日的 +3%（对数收益构造 → exp(0.03)−1；若仍是旧口径
    # 会量到锚日 ≈ 0%——H17-1 的复现形态）
    assert frame.loc[f"fomc_calendar:{mt5_ann}", "gross"] == pytest.approx(
        float(np.exp(0.03) - 1.0), abs=1e-12)


def test_seam_engine_research_registered_three_way(tmp_path):
    """T187 测试 3c（接缝版）：引擎 A 事件窗策略 = 研究口径 = 登记窗，
    三方逐位一致（零成本）。

    引擎在统一口径序列上的成交：信号=锚日 bar → 公告日 bar 开盘入场
    （=锚日收盘）、再下一根开盘平仓（=公告日收盘）；研究口径 day_close
    直接取两日收盘。两段（close-only 改写段 / MT5 段）同构。"""
    from mt5quant.backtest.engine_a import run_backtest
    from mt5quant.strategies.library.fomc_preannouncement import (
        PreFomcAnnouncementDrift,
    )
    from mt5quant.validation.events import declared_event_side, event_returns
    root, fred_ann, mt5_ann = _seam_store(tmp_path)
    bars, tags, _, _ = merged_research_series(
        root, "USTEC", "D1", allow_unbounded=True)
    events = _fomc_events_for(bars, (fred_ann, mt5_ann))
    days = pd.DatetimeIndex(pd.to_datetime(bars["time"]).dt.normalize())
    cal = {"version": 1, "complete_through": str(days[-1].date()),
           "sessions": [{"day": str(d.date()),
                         "close_time": f"{d.date()}T22:00:00"} for d in days]}
    res = run_backtest(
        PreFomcAnnouncementDrift(event_windows=events, event_calendar=cal),
        bars, ZeroCosts(), symbol="USTEC", params={}, contract_size=1.0,
        volume_lots=1.0, timeframe="D1")
    frame = event_returns(bars, events,
                          side_fn=declared_event_side("static_long"),
                          costs=ZeroCosts(), symbol="USTEC")
    times = pd.DatetimeIndex(pd.to_datetime(bars["time"]))
    close = bars["close"].to_numpy(dtype="float64")
    assert len(res.trades) == len(events) == 2
    assert set(res.trades["bars_held"]) == {1}
    assert set(res.trades["exit_reason"]) == {"signal"}
    for ann in (fred_ann, mt5_ann):
        i = int(times.get_loc(pd.Timestamp(ann)))
        reg = float(close[i] / close[i - 1] - 1.0)          # 登记窗
        trade = res.trades[res.trades["entry_time"] == times[i]]
        assert len(trade) == 1, f"事件 {ann} 无引擎成交于公告日 bar"
        eng = float(trade["exit_price"].iloc[0]
                    / trade["entry_price"].iloc[0] - 1.0)   # 引擎相对收益
        research = float(frame.loc[f"fomc_calendar:{ann}", "gross"])
        assert eng == research == reg, (ann, eng, research, reg)
        # 持仓恰为公告日 bar：入场 bar = 公告日、出场 bar = 次一 bar
        assert pd.Timestamp(trade["exit_time"].iloc[0]) == times[i + 1]


# --------------------------------------- close-only 行上两口径逐位等价

def test_day_close_equals_bar_open_on_close_only_rows_bitexact():
    """T187 测试 3b 的机制内核（等价锚点）：close-only 行 open==close，
    day_close 与 bar_open 两种成交解析的逐事件收益（及全部成本分量）
    逐位相同——R3 主检验段（FRED close-only）改写前后逐位不变的原因：
    改写只动 open/high/low，close 列逐字节不动，day_close 只读 close。"""
    from mt5quant.validation.events import (EventWindow, declared_event_side,
                                            event_returns)
    days = pd.bdate_range("2019-01-02", "2020-12-31")
    rng = np.random.default_rng(1871)
    close = 9000.0 * np.exp(np.cumsum(rng.normal(0.0002, 0.009, len(days))))
    bars = pd.DataFrame({
        "time": days, "open": close, "high": close, "low": close,
        "close": close, "tick_volume": 0, "spread": 0, "real_volume": 0})
    announcements = [str(d.date()) for d in days if (
        d.weekday() == 2 and d.day < 8 and d.month in (1, 4, 6, 9, 11))]
    announcements = announcements[1:9]
    events = _fomc_events_for(bars, announcements)
    twins = [EventWindow(kind=e.kind, label=e.label, entry_time=e.entry_time,
                         exit_time=e.exit_time,
                         price_timepoint="bar_open") for e in events]
    side_fn = declared_event_side("static_long")

    class RelCosts(ZeroCosts):
        P, L = 6.7e-5, 6.7e-6

        def spread_p90(self, symbol, hour, *, price=None):
            return self.P * float(price if price is not None else 1.0)

        def slippage(self, symbol, hour, *, price=None):
            return self.L * float(price if price is not None else 1.0)

    costs = RelCosts()
    frame_dc = event_returns(bars, events, side_fn=side_fn, costs=costs,
                             symbol="USTEC")
    frame_bo = event_returns(bars, twins, side_fn=side_fn, costs=costs,
                             symbol="USTEC")
    assert len(frame_dc) == len(events) == 8
    cols = ["entry_time", "exit_time", "entry_bar_time", "exit_bar_time",
            "entry_price", "exit_price", "gross", "spread", "slippage",
            "commission", "overnight", "nights", "cost", "net"]
    for col in cols:
        l, r = frame_dc[col], frame_bo[col]
        if isinstance(l.iloc[0], (str, pd.Timestamp, type(None))):
            assert list(l) == list(r), col
        else:
            assert (l.to_numpy() == r.to_numpy()).all(), col   # 逐位（非近似）


def test_price_timepoint_validation():
    """未知 price_timepoint → ValueError（fail-closed，不静默当 bar_open）。"""
    from mt5quant.validation.events import EventWindow
    with pytest.raises(ValueError, match="price_timepoint"):
        EventWindow(kind="x", label="x",
                    entry_time=pd.Timestamp("2024-01-01"),
                    exit_time=pd.Timestamp("2024-01-02"),
                    price_timepoint="intraday_vwap")
