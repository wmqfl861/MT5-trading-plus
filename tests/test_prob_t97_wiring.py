"""T97 波次 2 接线（计划 2026-10-29-p-line-t97-data-unlock.md v2）：洞抑制
helper、era 判定、情景点差（precheck 双档 + 标签 ScenarioCosts 分组）、
取数换源（merged_research_series 按新键启用）——全部合成数据的行为级测试。

硬约束（先决 2 / M2）：不带新键（max_gap_days / cost_scenario_mult /
cost_scenario_reference）的候选与卡走原路径，行为逐位不变（探针兼容）。
"""
import json
import types
from pathlib import Path

import pandas as pd
import pytest

import sys
REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "src")]

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("bt_t97w", REPO / "scripts" / "bt.py")
BT = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(BT)

from mt5data.store import _COLUMNS  # noqa: E402
from mt5quant.prob import gaps as prob_gaps  # noqa: E402
from mt5quant.prob import p1 as p1mod  # noqa: E402
from mt5quant.prob.counting import count_triggers  # noqa: E402
from mt5quant.prob.scenarios import ScenarioCosts  # noqa: E402
from mt5quant.research import candidates as cand  # noqa: E402
from mt5quant.strategies.library.prob.actions import ActionParams  # noqa: E402
from mt5quant.strategies.library.prob.situations import Approach  # noqa: E402

APPROACH = Approach(lookback_bars=6, min_distance_atr=5.0, touch_atr=0.3)
PARAMS = ActionParams(breakout_atr=2.0, retest_window_bars=8)
PLAT = (1989.5, 1990.0, 1989.0, 1989.5)


def make_bars_rows(rows, times):
    """OHLC 行 + 显式时间序列（可注入 >30 天洞）→ 仓库格式 DataFrame。"""
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df.insert(0, "time", pd.DatetimeIndex(pd.to_datetime(list(times))))
    df["tick_volume"], df["spread"], df["real_volume"] = 10, 5, 0
    return df[_COLUMNS]


def hourly(n, start="2026-01-02"):
    return list(pd.date_range(start, periods=n, freq="1h"))


def s1_pattern_rows():
    rows = [PLAT] * 26
    rows.append((1992.0, 1999.9, 1991.0, 1997.0))          # anchor（A3@26）
    rows.append((1997.0, 1998.0, 1996.0, 1997.5))
    rows.append((1998.0, 2005.5, 1998.0, 2005.0))          # A1@28
    rows += [PLAT] * 4
    return rows


def s1_bars_with_hole(hole_after=21, jump_days=31):
    """触发形态与 test_prob_counting.s1_bars 相同，但 index 21→22 之间
    时间轴跳 jump_days 天（>30 天 → 合并序列会记为 gap）。"""
    rows = s1_pattern_rows()
    times = hourly(len(rows))
    t = [x if i <= hole_after else x + pd.Timedelta(days=jump_days)
         for i, x in enumerate(times)]
    return make_bars_rows(rows, t)


# ================================================================ 抑制 helper

class TestSuppressAcrossGaps:
    def bars5(self):
        return make_bars_rows([PLAT] * 6, hourly(6))

    def test_table_of_boundary_cases(self):
        """洞 (t2, t3) 对全部 (window_start, bar_index) 组合的抑制真值表：
        抑制 ⇔ window_start ≤ 2 且 bar_index ≥ 3（含式边界：洞恰在窗首
        打开 / 恰在触发 bar 合上均抑制；洞止于窗首 / 始于触发 bar 不抑制）。"""
        bars = self.bars5()
        t = list(pd.to_datetime(bars["time"]))
        gap = [(t[2], t[3])]
        for w in range(6):
            for i in range(w, 6):
                got = prob_gaps.suppress_across_gaps(i, bars, gap, w)
                assert got == (w <= 2 and i >= 3), (w, i, got)

    def test_no_gaps_never_suppresses(self):
        bars = self.bars5()
        for i in range(6):
            assert prob_gaps.suppress_across_gaps(i, bars, [], 0) is False
            assert prob_gaps.suppress_across_gaps(i, bars, None, 0) is False

    def test_gap_outside_window_not_suppressed(self):
        bars = make_bars_rows([PLAT] * 8, hourly(8))
        t = list(pd.to_datetime(bars["time"]))
        gap = [(t[6], t[7])]                     # 窗口 [0,3] 之后的洞
        assert prob_gaps.suppress_across_gaps(3, bars, gap, 0) is False


class TestSituationWindowStart:
    def bars_fri_to_tue(self):
        """2026-01-02（周五）00:00 UTC 起连续 120 根 H1（到周二 23:00）。"""
        return make_bars_rows([PLAT] * 120, hourly(120))

    def test_s1_uses_approach_lookback(self):
        bars = self.bars_fri_to_tue()
        assert prob_gaps.situation_window_start(82, "S1", bars, 24) == 58
        assert prob_gaps.situation_window_start(3, "S1", bars, 24) == 0

    def test_s3_uses_480_bars(self):
        bars = self.bars_fri_to_tue()
        assert prob_gaps.situation_window_start(82, "S3", bars, 6) == 0
        long_bars = make_bars_rows([PLAT] * 600, hourly(600))
        assert prob_gaps.situation_window_start(500, "S3", long_bars, 6) == 20
        # s3_bars 可显式传入
        assert prob_gaps.situation_window_start(500, "S3", long_bars, 6,
                                                s3_bars=100) == 400

    def test_s2_previous_session_first_bar(self):
        """S2 窗首 = 前一个有 bar 会话的首 bar（17:00 NY 划界，与
        situations.s2_level_rows 同口径）。1 月 EST：17:00 NY = 22:00 UTC。

        bar 82 = 周一 2026-01-05 10:00 UTC（sess 周一）；sess 周日的首 bar
        = 本地周六 17:00 = 周六 2026-01-03 22:00 UTC = index 46。"""
        bars = self.bars_fri_to_tue()
        assert prob_gaps.situation_window_start(82, "S2", bars, 24) == 46

    def test_s2_first_session_falls_back_to_zero(self):
        bars = self.bars_fri_to_tue()
        # bar 5 = 周五 05:00 UTC（sess 周五，窗内无更早会话）→ 0
        assert prob_gaps.situation_window_start(5, "S2", bars, 24) == 0

    def test_unknown_situation_raises(self):
        bars = self.bars_fri_to_tue()
        with pytest.raises(ValueError, match="局面"):
            prob_gaps.situation_window_start(10, "S9", bars, 24)


# ================================================================ era 判定

class TestMt5FirstTime:
    def test_reads_meta_json(self, tmp_path):
        d = tmp_path / "bars" / "TEST" / "H1"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps(
            {"first_time": "2015-01-01T00:00:00",
             "last_closed_time": "2020-01-01T00:00:00"}), encoding="utf-8")
        assert prob_gaps.mt5_first_time(tmp_path, "TEST", "H1") == \
            pd.Timestamp("2015-01-01T00:00:00")

    def test_missing_meta_returns_none(self, tmp_path):
        assert prob_gaps.mt5_first_time(tmp_path, "TEST", "H1") is None


# ================================================================ P-09 共同可用集合

class TestEligibilityMask:
    """T98 audit P-09：S1/S3 回看窗（A2/A4 从 anchor 根起算）+ 特征预热
    + 持有窗——counting 与 p1 共用的合格判定。"""

    def trig(self, rows, times):
        return pd.DataFrame(
            [{"action": a, "anchor": anc, "bar": b, "time": times[b]}
             for a, anc, b in rows])

    def test_a2_window_starts_at_anchor_not_action_bar(self):
        """洞在 anchor 之前（t15,t16）：A2（anchor=18、bar=24）的局面窗须
        从 anchor 起 [12,18] → 跨洞 → 不合格；同一位置改 A1 → 窗从触发根
        起 [18,24] → 合格（audit P-09：从 action bar 回看会漏掉 anchor 前
        窗口）。anchor 列缺失时 A2 也退回触发根。"""
        bars = make_bars_rows([PLAT] * 30, hourly(30))
        t = list(pd.to_datetime(bars["time"]))
        gaps = [(t[15], t[16])]
        trig = self.trig([("A2", 18, 24), ("A1", 18, 24)], t)
        keep = prob_gaps.eligibility_mask(
            trig, bars, gaps, "S1", approach_lookback=6, feature_warmup=0)
        assert keep == [False, True]
        keep2 = prob_gaps.eligibility_mask(
            trig.drop(columns=["anchor"]), bars, gaps, "S1",
            approach_lookback=6, feature_warmup=0)
        assert keep2 == [True, True]       # anchor 缺失 → 触发根（旧行为）

    def test_feature_warmup_default_2000(self):
        bars = make_bars_rows([PLAT] * 2100, hourly(2100))
        t = list(pd.to_datetime(bars["time"]))
        trig = self.trig([("A3", 1999, 1999), ("A3", 2000, 2000)], t)
        keep = prob_gaps.eligibility_mask(
            trig, bars, [], "S1", approach_lookback=6)
        assert keep == [False, True]       # 触发根 ≥ 2000 才合格（缺省）

    def test_hold_window_crossing_gap_ineligible(self):
        """洞在触发后（t30,t31）：hold_bars=5 → A3@26 持有窗 [26,31] 跨洞
        → 不合格；hold_bars=3 → [26,29] 合格、A1@28 的 [28,31] 仍跨洞。"""
        times = [x if i <= 30 else x + pd.Timedelta(days=31)
                 for i, x in enumerate(hourly(33))]
        bars = make_bars_rows([PLAT] * 33, times)
        t = list(pd.to_datetime(bars["time"]))
        gaps = [(t[30], t[31])]
        trig = self.trig([("A3", 26, 26), ("A1", 26, 28)], t)
        kw = dict(gap_spans=gaps, situation="S1", approach_lookback=6,
                  feature_warmup=0)
        assert prob_gaps.eligibility_mask(trig, bars, hold_bars=5, **kw) \
            == [False, False]
        assert prob_gaps.eligibility_mask(trig, bars, hold_bars=3, **kw) \
            == [True, False]

    def test_max_gap_days_threshold_applies_to_hold_window(self):
        """持有窗的洞宽阈值（P-09）：40 天洞落在 A3@26 的持有窗 [26,32]
        内——max_gap_days=30（缺省）算洞 → 不合格；45 不算 → 合格；不查
        持有窗（hold_bars=0）且洞不在回看窗内 → 合格（回看窗用
        gap_spans 原样，先决 4 口径）。"""
        times = [x if i <= 30 else x + pd.Timedelta(days=40)
                 for i, x in enumerate(hourly(33))]
        bars = make_bars_rows([PLAT] * 33, times)
        t = list(pd.to_datetime(bars["time"]))
        gaps = [(t[30], t[31])]
        trig = self.trig([("A3", 26, 26)], t)     # S1 回看窗 [20,26] 无洞
        kw = dict(situation="S1", approach_lookback=6, feature_warmup=0,
                  hold_bars=6)
        assert prob_gaps.eligibility_mask(trig, bars, gaps,
                                          max_gap_days=30, **kw) == [False]
        assert prob_gaps.eligibility_mask(trig, bars, gaps,
                                          max_gap_days=45, **kw) == [True]
        assert prob_gaps.eligibility_mask(
            trig, bars, gaps, situation="S1", approach_lookback=6,
            feature_warmup=0) == [True]

    def test_s3_window_uses_s3_bars(self):
        times = [x if i <= 100 else x + pd.Timedelta(days=31)
                 for i, x in enumerate(hourly(600))]
        bars = make_bars_rows([PLAT] * 600, times)
        t = list(pd.to_datetime(bars["time"]))
        gaps = [(t[100], t[101])]
        trig = self.trig([("A3", 500, 500)], t)
        assert prob_gaps.eligibility_mask(
            trig, bars, gaps, "S3", approach_lookback=6,
            feature_warmup=0) == [False]          # 窗 [20,500] 跨洞
        assert prob_gaps.eligibility_mask(
            trig, bars, gaps, "S3", approach_lookback=6, s3_bars=50,
            feature_warmup=0) == [True]           # 窗 [450,500] 干净

    def test_s2_batch_matches_situation_window_start(self):
        """eligibility 的 S2 批量窗首与 situation_window_start 逐位一致
        （O(n) 预计算；逐 bar 调后者是 O(n²)）。"""
        bars = make_bars_rows([PLAT] * 120, hourly(120))
        starts = prob_gaps._s2_prev_session_starts(bars)
        for i in (0, 5, 40, 46, 82, 119):
            assert starts[i] == prob_gaps.situation_window_start(
                i, "S2", bars, 24)

    def test_unknown_situation_raises(self):
        bars = make_bars_rows([PLAT] * 10, hourly(10))
        trig = pd.DataFrame([{"action": "A3", "anchor": 5, "bar": 5}])
        with pytest.raises(ValueError, match="局面"):
            prob_gaps.eligibility_mask(trig, bars, [], "S9",
                                       approach_lookback=6, feature_warmup=0)

    def test_empty_triggers_returns_empty(self):
        bars = make_bars_rows([PLAT] * 10, hourly(10))
        empty = pd.DataFrame(columns=["action", "anchor", "bar"])
        assert prob_gaps.eligibility_mask(
            empty, bars, [], "S1", approach_lookback=6) == []


class TestMt5FirstTimeFromTags:
    """P-09：era 边界消费 merge 实测的 source_tags（不信水位）。"""

    def test_prefers_merge_source_tags_over_meta(self, tmp_path):
        root = root_with_meta(tmp_path, first="2099-01-01T00:00:00")
        bars = make_bars_rows([PLAT] * 4, hourly(4))
        tags = ["ext:histdata:2010", "ext:histdata:2010", "mt5", "mt5"]
        got = prob_gaps.mt5_first_time(root, "TEST", "H1", bars=bars,
                                       source_tags=tags)
        assert got == pd.to_datetime(bars["time"]).iloc[2]

    def test_all_ext_tags_returns_none(self, tmp_path):
        root = root_with_meta(tmp_path, first="2015-01-01T00:00:00")
        bars = make_bars_rows([PLAT] * 3, hourly(3))
        got = prob_gaps.mt5_first_time(
            root, "TEST", "H1", bars=bars,
            source_tags=["ext:histdata:2010"] * 3)
        assert got is None    # era 边界不可得——声明情景倍数的调用方须拒绝

    def test_length_mismatch_rejects(self, tmp_path):
        root = root_with_meta(tmp_path, first=None)
        bars = make_bars_rows([PLAT] * 4, hourly(4))
        with pytest.raises(ValueError, match="等长"):
            prob_gaps.mt5_first_time(root, "TEST", "H1", bars=bars,
                                     source_tags=["mt5"])

    def test_tags_without_bars_rejects(self, tmp_path):
        with pytest.raises(ValueError, match="bars"):
            prob_gaps.mt5_first_time(root_with_meta(tmp_path), "TEST", "H1",
                                     source_tags=["mt5"])


# ================================================================ ScenarioCosts

class RecordingCosts:
    def __init__(self):
        self.calls = []

    def spread_p90(self, symbol, hour_ny, *, price=None):
        self.calls.append(("spread", symbol, hour_ny, price))
        return 0.30

    def slippage(self, symbol, hour_ny, *, price=None):
        self.calls.append(("slip", symbol, hour_ny, price))
        return 0.05

    def commission_per_lot(self, symbol):
        self.calls.append(("comm", symbol))
        return 2.0

    def overnight_fee(self, symbol, side, from_ts, to_ts, *, price=None):
        self.calls.append(("overnight", symbol, side, price))
        return -1.0

    def stop_overshoot(self, symbol, hour_ny, *, price=None, level="p50"):
        self.calls.append(("overshoot", symbol, hour_ny, level))
        return 0.02


class TestScenarioCosts:
    def test_multiplies_spread_only(self):
        inner = RecordingCosts()
        sc = ScenarioCosts(inner, 1.5)
        assert sc.spread_p90("X", 9, price=100.0) == pytest.approx(0.45)
        assert sc.slippage("X", 9, price=100.0) == pytest.approx(0.05)
        assert sc.commission_per_lot("X") == pytest.approx(2.0)
        assert sc.overnight_fee("X", "long", pd.Timestamp("2026-01-01"),
                                pd.Timestamp("2026-01-02"), price=100.0) \
            == pytest.approx(-1.0)
        assert sc.stop_overshoot("X", 9, price=100.0) == pytest.approx(0.02)
        # 内层看到的调用参数原样透传（价格/小时/品种不被动过）
        assert ("spread", "X", 9, 100.0) in inner.calls
        assert ("slip", "X", 9, 100.0) in inner.calls
        # 只有点差调用一次、其余分量各一次（全部透传、无额外缩放）
        assert [c[0] for c in inner.calls] == ["spread", "slip", "comm",
                                               "overnight", "overshoot"]

    def test_identity_at_1x_and_invalid_mult(self):
        sc = ScenarioCosts(RecordingCosts(), 1.0)
        assert sc.spread_p90("X", 9, price=100.0) == pytest.approx(0.30)
        with pytest.raises(ValueError):
            ScenarioCosts(RecordingCosts(), 0.0)
        with pytest.raises(ValueError):
            ScenarioCosts(RecordingCosts(), -1.5)


# ================================================================ 计数抑制接线

class TestCountTriggerSuppression:
    def _count(self, bars, gap_spans=None):
        return count_triggers(bars, symbol="XAUUSD", situation="S1",
                              approach=APPROACH, action_params=PARAMS,
                              grid_step=50.0, gap_spans=gap_spans)

    def test_trigger_window_crossing_gap_suppressed(self):
        """洞 (t21, t22)：A3@26 的 S1 窗 [20,26] 跨洞 → 抑制；A1@28 的窗
        [22,28] 全在洞后 → 保留（先决 4 的两种典型情形）。"""
        bars = s1_bars_with_hole()
        t = list(pd.to_datetime(bars["time"]))
        gap = [(t[21], t[22])]
        plain = self._count(bars)
        assert 26 in set(plain["bar"]) and 28 in set(plain["bar"])
        filtered = self._count(bars, gap_spans=gap)
        assert 26 not in set(filtered["bar"])      # 窗跨洞 → 抑制
        assert 28 in set(filtered["bar"])          # 洞后正常触发保留
        assert len(filtered) < len(plain)

    def test_empty_gap_list_is_noop_and_default_none_unchanged(self):
        bars = s1_bars_with_hole()
        plain = self._count(bars)
        assert self._count(bars, gap_spans=[]).equals(plain)
        assert self._count(bars).equals(plain)


# ================================================================ precheck 情景

LIT = {
    "original": ["Osler (2003), Journal of Finance 58(5)"],
    "replications": [],
    "rebuttals": [],
    "search_log": "JSTOR/JF 2026-10-28: currency orders price levels",
    "published_effect": "止盈/止损单在整数位聚集",
    "expected_effect": "按发表后 −58% 折算：Δp 0.04",
}


def prob_data(**over):
    d = {
        "id": "C104", "title": "整数关口反转", "mechanism_class": "price_levels",
        "status": "registered", "symbols": ["TEST"], "holding": "H1",
        "mechanism": "止盈止损单聚集",
        "literature": dict(LIT),
        "prob": {
            "situations": ["S1"],
            "hypotheses": ["S1:A3:fade>base"],
            "bracket": {"sl_atr": 1.0, "tp_atr": 2.0, "horizon_bars": 24},
            "level_grid": {"TEST": 50.0},
            "approach": {"lookback_bars": 24, "min_distance_atr": 1.0,
                         "touch_atr": 0.25},
            "breakout_atr": 0.25,
            "retest_window_bars": 6,
            "expected_delta_p": 0.04,
        },
    }
    d.update(over)
    return d


class FlatCosts:
    def spread_p90(self, symbol, hour_ny, *, price=None):
        return 0.30

    def slippage(self, symbol, hour_ny, *, price=None):
        return 0.05

    def commission_per_lot(self, symbol):
        return 0.0


def counted_df(rows):
    return pd.DataFrame(
        [{"symbol": s, "situation": sit, "action": a, "side": 1,
          "level": 2000.0, "kind": "resistance", "anchor": i,
          "bar": i, "time": pd.Timestamp(t), "ny_hour": h, "atr": atr,
          "close": c}
         for i, (s, sit, a, h, atr, c, t) in enumerate(rows)])


def root_with_meta(tmp_path, first="2015-01-01T00:00:00"):
    (tmp_path / "specs").mkdir(exist_ok=True)
    (tmp_path / "specs" / "latest.json").write_text(
        json.dumps({"TEST": {"contract_size": 100.0}}), encoding="utf-8")
    if first is not None:
        d = tmp_path / "bars" / "TEST" / "H1"
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(json.dumps(
            {"first_time": first, "last_closed_time": "2020-01-01T00:00:00"}),
            encoding="utf-8")
    return tmp_path


class TestPrecheckScenarioCosts:
    """FlatCosts：spread=0.30、slip=0.05；sl_atr=1、atr=10.5 → R=10.5。

    - 1x 档：cost_R = 0.40/10.5 ≈ 0.0381 → 3× = 0.114 < ΔEV = 0.04×3 = 0.12 ✓
    - 保守 1.5 档（ext 行点差 ×1.5）：0.55/10.5 ≈ 0.0524 → 3× ≈ 0.157 > 0.12 ✗
    """

    def _scenario_candidate(self):
        data = prob_data()
        data["prob"] = {**data["prob"], "max_gap_days": 30,
                        "cost_scenario_mult": [1.0, 1.5],
                        "cost_scenario_reference": 3.0}
        return cand.validate_candidate(data, source="t")

    def test_conservative_scenario_binds_decision(self, tmp_path):
        c = self._scenario_candidate()
        rows = [("TEST", "S1", "A3", 9, 10.5, 2000.0, "2014-06-02 14:00")] * 50
        res = cand.precheck_prob(c, data_root=root_with_meta(tmp_path),
                                 cost_model=FlatCosts(),
                                 counted=counted_df(rows),
                                 split=pd.Timestamp("2020-01-01"))
        ec = res.checks["ex_ante_cost_prob"]
        assert ec["passed"] is False           # 1x 过、保守档不过 → fail
        assert res.decision == "fail"
        assert ec["cost_r_by_symbol_1x"]["TEST"] == pytest.approx(0.40 / 10.5)
        assert ec["cost_r_by_symbol"]["TEST"] == pytest.approx(0.55 / 10.5)
        assert ec["cost_r_by_symbol_ref3"]["TEST"] == pytest.approx(1.00 / 10.5)

    def test_mixed_era_mean_uses_meta_first_time(self, tmp_path):
        """era 判定：触发时刻 < bars meta.json first_time → ext（点差乘档）；
        ≥ first_time → MT5 段（1x）。50 行 ext + 50 行 MT5 → 保守档均值
        = mean(0.55, 0.40)/10.5。"""
        c = self._scenario_candidate()
        rows = ([("TEST", "S1", "A3", 9, 10.5, 2000.0,
                  "2014-06-02 14:00")] * 50
                + [("TEST", "S1", "A3", 9, 10.5, 2000.0,
                    "2016-06-02 14:00")] * 50)
        res = cand.precheck_prob(c, data_root=root_with_meta(tmp_path),
                                 cost_model=FlatCosts(),
                                 counted=counted_df(rows),
                                 split=pd.Timestamp("2020-01-01"))
        ec = res.checks["ex_ante_cost_prob"]
        assert ec["cost_r_by_symbol"]["TEST"] == pytest.approx(0.475 / 10.5)
        assert ec["cost_r_by_symbol_1x"]["TEST"] == pytest.approx(0.40 / 10.5)

    def test_no_meta_means_no_ext_triggers(self, tmp_path):
        c = self._scenario_candidate()
        rows = [("TEST", "S1", "A3", 9, 10.5, 2000.0, "2014-06-02 14:00")] * 50
        res = cand.precheck_prob(c, data_root=root_with_meta(tmp_path,
                                                             first=None),
                                 cost_model=FlatCosts(),
                                 counted=counted_df(rows),
                                 split=pd.Timestamp("2020-01-01"))
        ec = res.checks["ex_ante_cost_prob"]
        assert ec["cost_r_by_symbol"]["TEST"] == \
            pytest.approx(ec["cost_r_by_symbol_1x"]["TEST"])

    def test_without_new_keys_bit_compatible(self, tmp_path):
        """缺省（无新键）：checks 逐位与 T97 之前相同——不出现新键，
        cost_r_by_symbol 仍为 1x 口径。"""
        c = cand.validate_candidate(prob_data(), source="t")
        rows = [("TEST", "S1", "A3", 9, 10.5, 2000.0, "2014-06-02 14:00")] * 100
        res = cand.precheck_prob(c, data_root=root_with_meta(tmp_path),
                                 cost_model=FlatCosts(),
                                 counted=counted_df(rows),
                                 split=pd.Timestamp("2020-01-01"))
        ec = res.checks["ex_ante_cost_prob"]
        assert "cost_r_by_symbol_1x" not in ec
        assert "cost_r_by_symbol_ref3" not in ec
        assert ec["cost_r_by_symbol"]["TEST"] == pytest.approx(0.40 / 10.5)
        assert res.decision == "waiting_data"   # 100 行同日 → 触发日数 1


# ================================================================ 标签分 era

class FlatLabelCosts:
    def spread_p90(self, s, h, *, price=None):
        return 0.02

    def slippage(self, s, h, *, price=None):
        return 0.01

    def commission_per_lot(self, s):
        return 0.0

    def overnight_fee(self, s, side, f, t, *, price=None):
        return 0.0


def fake_card(hypotheses=("S1:A3:fade>base",), **prob_over):
    """触发形态与 test_prob_counting 的 s1_bars 对齐（A3@anchor、A1@28；
    Wilder ATR 前 14 根 NaN——approach 回看窗须短到不落进 NaN 区）。"""
    prob = {"situations": ("S1",),
            "hypotheses": list(hypotheses),
            "bracket": {"sl_atr": 1.0, "tp_atr": 2.0, "horizon_bars": 24},
            "level_grid": {"XAUUSD": 50.0},
            "approach": {"lookback_bars": 6, "min_distance_atr": 5.0,
                         "touch_atr": 0.3},
            "breakout_atr": 2.0, "retest_window_bars": 8}
    prob.update(prob_over)
    return types.SimpleNamespace(
        id="P001-v1", timeframe="H1", segment="discovery",
        evaluation_mode="probability", candidate="C104", round="R2",
        prob=prob)


def fake_round(split="2026-03-20"):
    from mt5quant.research import rounds as rounds_mod
    return rounds_mod.Round(
        id="R2", opened="2026-11-02", candidates=("C104",),
        trial_budget=12, lockbox_uses_max=0, dsr_trial_scope="project",
        advance_rule="a", close_rule="c", approved_by="P线审核会话",
        evaluation={"C104": {"mode": "probability",
                             "primary": "delta_p_one_sided"}},
        segments={"split": split}, hypotheses={"C104": ["S1:A3:fade>base"]},
        path="")


class TestRunP1CardEraSplit:
    def _run(self, monkeypatch, card, bars, gap_spans=None,
             feature_warmup=0):
        from mt5data.sessions import SessionPolicy
        costs = FlatLabelCosts()
        seen = []
        real = p1mod.label_triggers

        def spy(b, trig, c, **kw):
            seen.append({
                "is_scenario": isinstance(c, ScenarioCosts),
                "is_orig": c is costs,
                "mult": getattr(c, "mult", None),
                "times": list(pd.to_datetime(trig["time"])),
                "actions": set(trig["action"]) if len(trig) else set()})
            return real(b, trig, c, **kw)

        monkeypatch.setattr(p1mod, "label_triggers", spy)
        times = list(pd.to_datetime(bars["time"]))
        mt5_first = times[27]                    # A3@26 → ext；A1@28 → MT5
        res = p1mod.run_p1_card(
            card, fake_round(), {"XAUUSD": bars}, {"XAUUSD": costs},
            sizes={"XAUUSD": 100.0},
            policy_by_symbol={"XAUUSD": SessionPolicy(
                exempt_symbols=frozenset({"XAUUSD"}))},
            sims=50, mt5_first_by_symbol={"XAUUSD": mt5_first},
            feature_warmup=feature_warmup,
            gap_spans_by_symbol=({"XAUUSD": gap_spans}
                                 if gap_spans is not None else None))
        return res, seen, costs, times

    def test_ext_era_labels_use_scenario_costs(self, monkeypatch):
        """A3@26（ext 段，time < mt5_first）→ ScenarioCosts(1.5)；
        A1@28（MT5 段）→ 原 costs；同一品种两组标签结果拼接。

        随机化桶在本合成序列里只含触发自身（小时×星期桶唯一），故真实
        与零假设两组的匹配条目同型——全部断言为对应成本包装。"""
        card = fake_card(hypotheses=("S1:A3:fade>base", "S1:A1:follow>base"),
                         cost_scenario_mult=(1.0, 1.5))
        bars = make_bars_rows(s1_pattern_rows(), hourly(33))
        res, seen, costs, times = self._run(monkeypatch, card, bars)
        a3 = [s for s in seen
              if s["actions"] == {"A3"} and s["times"] == [times[26]]]
        a1 = [s for s in seen
              if s["actions"] == {"A1"} and s["times"] == [times[28]]]
        assert len(a3) >= 1 and all(s["is_scenario"] for s in a3)
        assert all(s["mult"] == pytest.approx(1.5) for s in a3)
        assert len(a1) >= 1 and all(s["is_orig"] for s in a1)
        # 两条假设都有结果（拼接后进入 delta_p 检验）
        assert {r["hypothesis"] for r in res} == {
            "S1:A3:fade>base", "S1:A1:follow>base"}

    def test_gap_suppression_applies_to_card_triggers(self, monkeypatch):
        """card_triggers 抑制：洞 (t21,t22) 使 A3@26 的 S1 窗 [20,26] 跨洞
        → 该触发在标签前已被剔除（label_triggers 收不到它）。"""
        card = fake_card(hypotheses=("S1:A3:fade>base", "S1:A1:follow>base"))
        bars = make_bars_rows(s1_pattern_rows(), hourly(33))
        times = list(pd.to_datetime(bars["time"]))
        res, seen, costs, times = self._run(
            monkeypatch, card, bars, gap_spans=[(times[21], times[22])])
        touched = [t for s in seen for t in s["times"]]
        assert times[26] not in touched           # 跨洞触发被抑制
        assert times[28] in touched               # 洞后触发保留

    def test_without_scenario_key_all_labels_use_original_costs(
            self, monkeypatch):
        """硬约束：卡不带 cost_scenario_mult（即使 bt 传了 mt5_first）→
        全部 label_triggers 用原 costs，无 ScenarioCosts 包装。"""
        card = fake_card(hypotheses=("S1:A3:fade>base", "S1:A1:follow>base"))
        bars = make_bars_rows(s1_pattern_rows(), hourly(33))
        res, seen, costs, times = self._run(monkeypatch, card, bars)
        assert seen, "合成形态应产生触发（A3@26 / A1@28）"
        assert all(s["is_orig"] for s in seen)
        assert not any(s["is_scenario"] for s in seen)


class TestRunP1CardEligibility:
    """P-09：run_p1_card 的共同可用集合——真实触发过滤 + 随机对照从同一
    合格集合抽样 + era 边界不可得时拒绝。"""

    def _run(self, monkeypatch, card, bars, **over):
        from mt5data.sessions import SessionPolicy
        kw = dict(sizes={"XAUUSD": 100.0},
                  policy_by_symbol={"XAUUSD": SessionPolicy(
                      exempt_symbols=frozenset({"XAUUSD"}))},
                  sims=50)
        kw.update(over)
        return p1mod.run_p1_card(
            card, fake_round(), {"XAUUSD": bars},
            {"XAUUSD": FlatLabelCosts()}, **kw)

    def test_randomize_receives_eligible_bars(self, monkeypatch):
        """run_p1_card 显式把合格 bar 集传给 randomize_triggers（P-09）：
        无洞、预热 0 → 全序列合格；有预热 → 池从预热根起（真实触发同
        口径过滤——A1@28 ≥ 27 仍在）。"""
        import numpy as np
        captured = []
        real_rnd = p1mod.randomize_triggers

        def spy(trig, bars, *, seed, **kw):
            captured.append(kw.get("eligible_bars"))
            return real_rnd(trig, bars, seed=seed, **kw)

        monkeypatch.setattr(p1mod, "randomize_triggers", spy)
        card = fake_card(hypotheses=("S1:A1:follow>base",))
        bars = make_bars_rows(s1_pattern_rows(), hourly(33))
        self._run(monkeypatch, card, bars, feature_warmup=0)
        assert captured and captured[0] is not None
        pool = np.asarray(captured[0]).astype(int)
        assert set(pool.tolist()) == set(range(len(bars)))   # 预热 0 无洞
        assert {26, 28} <= set(pool.tolist())                # 触发自身在池
        captured.clear()
        self._run(monkeypatch, card, bars, feature_warmup=27)
        assert set(np.asarray(captured[0]).astype(int).tolist()) \
            == set(range(27, len(bars)))

    def test_feature_warmup_filters_real_triggers(self, monkeypatch):
        """缺省预热 2000（atr_pct_2000 排名窗）：63 根合成序列的触发全部
        被剔（真实与随机两侧一致地无触发）；预热 0 → A3@26 进标签
        （序列补长到 63 根——horizon 24 从 26 起要完整可观察）。"""
        seen = []
        real = p1mod.label_triggers

        def spy(b, trig, c, **kw):
            seen.extend(pd.to_datetime(trig["time"]))
            return real(b, trig, c, **kw)

        monkeypatch.setattr(p1mod, "label_triggers", spy)
        card = fake_card(hypotheses=("S1:A3:fade>base",))
        bars = make_bars_rows(s1_pattern_rows() + [PLAT] * 30, hourly(63))
        times = list(pd.to_datetime(bars["time"]))
        res = self._run(monkeypatch, card, bars)     # 不传 → 缺省 2000
        assert seen == []
        assert res[0]["n_total"] == 0 and res[0]["n_triggers"] == 0
        seen.clear()
        res = self._run(monkeypatch, card, bars, feature_warmup=0)
        assert times[26] in seen                     # A3@26（本假设）进标签
        assert res[0]["n_total"] >= 1

    def test_cost_scenario_without_mt5_first_raises(self, monkeypatch):
        """卡声明 cost_scenario_mult 而 era 边界不可得 → ValueError
        （P-09：不静默按 1× 计 ext 段）。"""
        card = fake_card(hypotheses=("S1:A3:fade>base",),
                         cost_scenario_mult=(1.0, 1.5))
        bars = make_bars_rows(s1_pattern_rows(), hourly(33))
        with pytest.raises(ValueError, match="era"):
            self._run(monkeypatch, card, bars, feature_warmup=0,
                      mt5_first_by_symbol=None)
        with pytest.raises(ValueError, match="era"):
            self._run(monkeypatch, card, bars, feature_warmup=0,
                      mt5_first_by_symbol={})
        # NaT 同样视为不可得
        with pytest.raises(ValueError, match="era"):
            self._run(monkeypatch, card, bars, feature_warmup=0,
                      mt5_first_by_symbol={"XAUUSD": pd.NaT})

    def test_cost_scenario_with_boundary_runs(self, monkeypatch):
        """边界可得（merge 实测口径）→ 正常分 era，不抛。"""
        card = fake_card(hypotheses=("S1:A3:fade>base",),
                         cost_scenario_mult=(1.0, 1.5))
        bars = make_bars_rows(s1_pattern_rows(), hourly(33))
        mt5_first = pd.to_datetime(bars["time"]).iloc[27]
        res = self._run(monkeypatch, card, bars, feature_warmup=0,
                        mt5_first_by_symbol={"XAUUSD": mt5_first})
        assert {r["hypothesis"] for r in res} == {"S1:A3:fade>base"}


class TestCardTriggersEligibility:
    def test_feature_warmup_and_hold_passed_through(self):
        """card_triggers 的 P-09 三元组（预热/持有/洞宽）逐层透传。"""
        card = fake_card()
        bars = make_bars_rows(s1_pattern_rows(), hourly(33))
        plain = p1mod.card_triggers(card, {"XAUUSD": bars}, approach=APPROACH,
                                    action_params=PARAMS)
        plain_bars = set(plain["bar"].astype(int))
        assert {26, 28, 29} <= plain_bars           # A3@26、A1@28、A4@29
        warm = p1mod.card_triggers(card, {"XAUUSD": bars}, approach=APPROACH,
                                   action_params=PARAMS, feature_warmup=27)
        assert set(warm["bar"].astype(int)) == plain_bars - {26}
        # 持有窗跨洞：洞 (t30,t31)，hold 5 → 全部触发的持有窗都跨洞
        times = [x if i <= 30 else x + pd.Timedelta(days=31)
                 for i, x in enumerate(hourly(33))]
        holed = make_bars_rows(s1_pattern_rows(), times)
        t = list(pd.to_datetime(holed["time"]))
        kept = p1mod.card_triggers(card, {"XAUUSD": holed}, approach=APPROACH,
                                   action_params=PARAMS, feature_warmup=0,
                                   hold_bars=5,
                                   gap_spans_by_symbol={"XAUUSD": [(t[30],
                                                                   t[31])]})
        assert len(kept) == 0


# ================================================================ 取数换源

class TestBtLoadPath:
    def _plain_bars(self, n=40):
        return make_bars_rows([PLAT] * n, hourly(n))

    def _patch_loaders(self, monkeypatch):
        import mt5data.ext.merge as merge_mod
        synth = self._plain_bars()
        calls = {"load_series": 0, "merged": 0, "merged_end": None}

        def fake_load_series(*a, **k):
            calls["load_series"] += 1
            return synth

        def fake_merged(root, sym, tf, *, end=None, allow_unbounded=False):
            calls["merged"] += 1
            calls["merged_end"] = end
            return synth, ["mt5"] * len(synth), [], []

        monkeypatch.setattr(BT, "load_series", fake_load_series)
        monkeypatch.setattr(merge_mod, "merged_research_series", fake_merged)
        return calls

    def test_candidate_check_prob_old_candidate_uses_load_series(
            self, tmp_path, monkeypatch):
        calls = self._patch_loaders(monkeypatch)
        args = types.SimpleNamespace(split="2020-01-01")
        c = cand.validate_candidate(prob_data(), source="t")
        BT._candidate_check_prob(args, c, root_with_meta(tmp_path, first=None),
                                 FlatCosts(), set())
        assert calls["load_series"] == 1 and calls["merged"] == 0

    def test_candidate_check_prob_new_keys_use_merged(self, tmp_path,
                                                      monkeypatch):
        calls = self._patch_loaders(monkeypatch)
        args = types.SimpleNamespace(split="2020-01-01")
        data = prob_data()
        data["prob"] = {**data["prob"], "max_gap_days": 30}
        c = cand.validate_candidate(data, source="t")
        BT._candidate_check_prob(args, c, root_with_meta(tmp_path, first=None),
                                 FlatCosts(), set())
        assert calls["load_series"] == 0 and calls["merged"] == 1

    def test_merged_gate_helper(self):
        assert BT._prob_use_merged({}) is False
        assert BT._prob_use_merged({"max_gap_days": 30}) is True
        assert BT._prob_use_merged({"cost_scenario_mult": (1.0, 1.5)}) is True
        assert BT._prob_use_merged({"cost_scenario_reference": 3.0}) is True
        p = cand.validate_candidate(prob_data(), source="t").prob_declared
        assert BT._prob_use_merged(p) is False
        data = prob_data()
        data["prob"] = {**data["prob"], "cost_scenario_mult": [1.0, 1.5]}
        p2 = cand.validate_candidate(data, source="t").prob_declared
        assert BT._prob_use_merged(p2) is True

    def test_prob_load_bars_dispatch(self, tmp_path, monkeypatch):
        calls = self._patch_loaders(monkeypatch)
        bars, gaps = BT._prob_load_bars(tmp_path, "TEST", "H1",
                                        use_merged=False, end=None)
        assert gaps is None and len(bars) == 40
        bars, gaps = BT._prob_load_bars(tmp_path, "TEST", "H1",
                                        use_merged=True)
        # 无洞的合并序列：gap_spans 空表归一为 None（不过滤）
        assert gaps is None and len(bars) == 40
        assert calls["load_series"] == 1 and calls["merged"] == 1
        assert calls["merged_end"] is None

    def test_prob_load_bars_merged_forwards_end(self, tmp_path, monkeypatch):
        """T98/P-01：use_merged=True 时 end 不再被丢弃——原样传给
        merged_research_series（合并上界取最严由 merge 侧负责）。"""
        calls = self._patch_loaders(monkeypatch)
        end = pd.Timestamp("2020-06-01T00:00:00")
        BT._prob_load_bars(tmp_path, "TEST", "H1", use_merged=True, end=end)
        assert calls["load_series"] == 0 and calls["merged"] == 1
        assert calls["merged_end"] == end

    def test_prob_load_bars_empty_merged_raises(self, tmp_path, monkeypatch):
        import mt5data.ext.merge as merge_mod
        empty = make_bars_rows([], [])
        monkeypatch.setattr(merge_mod, "merged_research_series",
                            lambda *a, **k: (empty, [], [], []))
        with pytest.raises(ValueError, match="无可用数据"):
            BT._prob_load_bars(tmp_path, "TEST", "H1", use_merged=True)
