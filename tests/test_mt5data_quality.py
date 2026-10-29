"""quality 质检测试：缺口分类（weekend/daily_break/holiday/unexpected）、
spike/点差异常、只读不改数、JSON/markdown 输出、CLI 脚本。

DST 切换周用例（Review Focus #2）直接钉死 2026-10-23（48h 周末）与
2026-10-30→11-01（49h 不对称周末，美国当晨切冬令时）两个真实窗口。
"""
import importlib.util
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from mt5data.quality import Gap, QualityReport, quality_report
from mt5data.store import _COLUMNS

MON = date(2026, 10, 26)  # 该周周一；周五 10-30、周日 11-01（美国切冬令时当日）
WED = date(2026, 10, 28)


def make_bars(times, *, move=0.1, base=100.0, spread=5,
              tick_volume=5, real_volume=0) -> pd.DataFrame:
    n = len(times)
    if not isinstance(move, (list, tuple)):
        move = [move] * n
    if not isinstance(spread, (list, tuple)):
        spread = [spread] * n
    return pd.DataFrame({
        "time": pd.to_datetime(list(times)),
        "open": [base] * n,
        "high": [base + m for m in move],
        "low": [base - 0.05] * n,
        "close": [base + m for m in move],
        "tick_volume": [tick_volume] * n,
        "spread": list(spread),
        "real_volume": [real_volume] * n,
    })


def day_minutes(day: date, hours=range(2, 20), skip_hours=frozenset()):
    """某交易日的 M1 时刻（默认 02:00–19:59，整小时跳过 skip_hours）。"""
    return [datetime(day.year, day.month, day.day, h, m)
            for h in hours if h not in skip_hours for m in range(60)]


# ---- c) 缺口分类 ----


def test_weekend_gap_oct23_week_classified():
    # 10-23 周（48h 周末）：周五末根 20:59 → 周日首根 21:00
    times = ([datetime(2026, 10, 23, 20, 55) + timedelta(minutes=i) for i in range(5)]
             + [datetime(2026, 10, 25, 21, 0) + timedelta(minutes=i) for i in range(5)])
    rep = quality_report(make_bars(times), "M1", symbol="XAUUSD")
    kinds = [g.kind for g in rep.gaps]
    assert kinds == ["weekend"]
    g = rep.gaps[0]
    assert isinstance(g, Gap)
    assert g.start == datetime(2026, 10, 23, 21, 0)   # 首根缺失 bar
    assert g.end == datetime(2026, 10, 25, 21, 0)     # 缺口后首根现存 bar
    assert g.bars_missing == 2880                     # 48h - 1 根


def test_weekend_gap_49h_us_switch_week_not_misjudged():
    # Review Focus #2：10-30→11-01 切换周，49 小时缺口必须仍是 weekend，
    # 不许因 ≥20h 被误判 holiday、更不许 unexpected
    times = ([datetime(2026, 10, 30, 20, 55) + timedelta(minutes=i) for i in range(5)]
             + [datetime(2026, 11, 1, 22, 0) + timedelta(minutes=i) for i in range(5)])
    rep = quality_report(make_bars(times), "M1", symbol="XAUUSD")
    assert [g.kind for g in rep.gaps] == ["weekend"]
    assert rep.gaps[0].bars_missing == 2940  # 49h1min 间隔 - 1
    # coverage 数字与缺口自洽
    assert rep.rows == 10


def test_midweek_one_hour_gap_is_unexpected_and_daily_breaks_recognized():
    days = [MON + timedelta(days=i) for i in range(5)]  # 周一…周五
    skip = {WED: {10}}  # 仅周三缺 10 点这一个小时
    times = [t for d in days for t in day_minutes(d, skip_hours=skip.get(d, frozenset()))]
    rep = quality_report(make_bars(times), "M1")
    kinds = [g.kind for g in rep.gaps]
    assert kinds.count("weekend") == 0 and kinds.count("holiday") == 0
    # 交易日之间 02:00–19:59 的 6 小时休市每天出现 → daily_break（4 处）
    assert kinds.count("daily_break") == 4
    # 周三 10 点只出现一次（1/5=20% < 60%）→ unexpected
    unexpected = [g for g in rep.gaps if g.kind == "unexpected"]
    assert len(unexpected) == 1
    u = unexpected[0]
    assert u.start == datetime(2026, 10, 28, 10, 0)
    assert u.end == datetime(2026, 10, 28, 11, 0)
    assert u.bars_missing == 60


def test_daily_break_at_exactly_60_percent_threshold():
    # 5 个交易日中 3 天同一小时缺（恰 60%）→ daily_break（边界含）
    days = [MON + timedelta(days=i) for i in range(5)]
    skip = {days[2]: {10}, days[3]: {10}, days[4]: {10}}
    times = [t for d in days for t in day_minutes(d, skip_hours=skip.get(d, frozenset()))]
    rep = quality_report(make_bars(times), "M1")
    kinds = [g.kind for g in rep.gaps]
    assert kinds.count("unexpected") == 0
    assert kinds.count("daily_break") == 4 + 3  # 日间休市 4 + 10 点缺 3
    ten_oclock = [g for g in rep.gaps
                  if g.kind == "daily_break" and g.start.hour == 10]
    assert len(ten_oclock) == 3


def test_whole_wednesday_missing_is_holiday():
    # 周三整日缺（24h、不跨周末）→ holiday；注意别误判 weekend/daily_break
    days = [MON, MON + timedelta(days=1), MON + timedelta(days=3), MON + timedelta(days=4)]
    times = [t for d in days for t in day_minutes(d, hours=range(24))]
    rep = quality_report(make_bars(times), "M1")
    assert [g.kind for g in rep.gaps] == ["holiday"]
    g = rep.gaps[0]
    assert g.start == datetime(2026, 10, 28, 0, 0)
    assert g.end == datetime(2026, 10, 29, 0, 0)
    assert g.bars_missing == 1440


# ---- 覆盖率 / 行数 ----


def test_coverage_rows_and_expected():
    t0 = datetime(2026, 10, 26, 0, 0)
    times = [t0 + timedelta(minutes=i) for i in range(100)]
    rep = quality_report(make_bars(times), "M1")
    assert isinstance(rep, QualityReport)
    assert rep.rows == 100 and rep.expected_rows == 100
    assert rep.coverage_pct == pytest.approx(100.0)
    assert rep.first_time == t0 and rep.last_time == times[-1]

    times_hole = [t for t in times if t != t0 + timedelta(minutes=50)]
    rep2 = quality_report(make_bars(times_hole), "M1")
    assert rep2.rows == 99 and rep2.expected_rows == 100
    assert rep2.coverage_pct == pytest.approx(99.0)


# ---- 重复 / 乱序 ----


def test_duplicates_and_disorder_counted_and_noted():
    t0 = datetime(2026, 10, 26, 0, 0)
    times = [t0 + timedelta(minutes=i) for i in range(10)]
    dup = make_bars(times + [t0 + timedelta(minutes=5)])  # 末尾重复一根
    rep = quality_report(dup, "M1")
    assert rep.duplicates == 1 and rep.disorder == 1  # 9→5 回退即乱序
    assert any("重复" in n for n in rep.notes)
    assert any("乱序" in n for n in rep.notes)

    interior = make_bars(times[:3] + [times[2]] + times[3:])  # 相邻重复
    rep2 = quality_report(interior, "M1")
    assert rep2.duplicates == 1 and rep2.disorder == 0

    swapped = make_bars([times[0], times[1], times[3], times[2], times[4]])
    rep3 = quality_report(swapped, "M1")
    assert rep3.disorder == 1 and rep3.duplicates == 0


def test_quality_report_does_not_mutate_input():
    t0 = datetime(2026, 10, 26, 0, 0)
    times = [t0 + timedelta(minutes=i) for i in range(10)]
    df = make_bars(times + [t0 + timedelta(minutes=5)])
    before = df.copy(deep=True)
    quality_report(df, "M1")
    assert df.equals(before)


# ---- f) spike / 点差异常 ----


def _spike_frames():
    t0 = datetime(2026, 10, 26, 0, 0)
    n = 100
    opens = [100.0] * n
    closes = [100.1] * n
    spike, retrace = 50, 51
    opens[spike], closes[spike] = 100.0, 105.0  # 上冲 5.0（背景 0.1）
    retraced = list(zip(opens, closes))
    # 回撤版：下一根反向走 4.5（> 5.0 的一半）
    o2, c2 = list(opens), list(closes)
    o2[retrace], c2[retrace] = 105.0, 100.5
    # 不回撤版：下一根同向 +0.2
    o3, c3 = list(opens), list(closes)
    o3[retrace], c3[retrace] = 105.0, 105.2
    return t0, spike, retraced, (o2, c2), (o3, c3)


def test_spike_with_retrace_detected():

    def build(opens, closes):
        df = make_bars([t0 + timedelta(minutes=i) for i in range(len(opens))], move=0.1)
        df["open"], df["close"] = opens, closes
        df["high"] = [max(o, c) + 0.01 for o, c in zip(opens, closes)]
        df["low"] = [min(o, c) - 0.01 for o, c in zip(opens, closes)]
        return df

    t0, spike, _, retraced, not_retraced = _spike_frames()
    rep = quality_report(build(*retraced), "M1")
    assert len(rep.spikes) == 1
    s = rep.spikes[0]
    assert s["time"] == (t0 + timedelta(minutes=spike)).isoformat()
    assert s["range"] == pytest.approx(5.0)          # |close-open|
    assert s["neighbor_median"] == pytest.approx(0.1)
    assert s["retrace"] == pytest.approx(4.5)        # 下一根反向幅度

    rep2 = quality_report(build(*not_retraced), "M1")
    assert rep2.spikes == []  # 下一根同向延续 → 不算 spike（只报回撤型）


def test_spread_anomalies_zero_and_extreme():
    t0 = datetime(2026, 10, 26, 0, 0)
    n = 100
    spreads = [10] * n
    spreads[40] = 0       # <=0
    spreads[80] = 10000   # > p99×10（p99≈109.9 → 阈值≈1099）
    times = [t0 + timedelta(minutes=i) for i in range(n)]
    rep = quality_report(make_bars(times, spread=spreads), "M1")
    assert len(rep.spread_anomalies) == 2
    by_reason = {a["reason"]: a for a in rep.spread_anomalies}
    assert by_reason["spread<=0"]["time"] == (t0 + timedelta(minutes=40)).isoformat()
    assert by_reason["spread>p99*10"]["spread"] == 10000
    # 正常值不报
    normal = quality_report(make_bars(times), "M1")
    assert normal.spread_anomalies == []


# ---- 空数据 / 输出 ----


def test_empty_df_returns_zero_report_without_crash():
    rep = quality_report(pd.DataFrame(columns=_COLUMNS), "M1")
    assert rep.rows == 0 and rep.gaps == [] and rep.first_time is None
    assert rep.coverage_pct == 0.0
    assert any("空" in n for n in rep.notes)


def test_to_dict_is_json_serializable():
    t0 = datetime(2026, 10, 26, 0, 0)
    times = [t0 + timedelta(minutes=i) for i in range(10)]
    rep = quality_report(make_bars(times), "M1", symbol="XAUUSD")
    d = rep.to_dict()
    assert d["symbol"] == "XAUUSD" and d["timeframe"] == "M1"
    assert d["rows"] == 10 and d["coverage_pct"] == pytest.approx(100.0)
    assert d["gaps"] == [] and d["notes"] == []
    json.dumps(d)  # 不抛即通过


def test_render_markdown_contains_key_lines():
    t0 = datetime(2026, 10, 26, 0, 0)
    times = [t0 + timedelta(minutes=i) for i in range(10)]
    rep = quality_report(make_bars(times), "M1", symbol="XAUUSD")
    md = rep.render_markdown()
    assert "XAUUSD" in md and "M1" in md
    assert "覆盖率" in md and "unexpected" in md
    # 有 unexpected 缺口时逐条列出（注意：单日样本任何缺口都满足
    # "≥60% 交易日同一小时"（1/1=100%）会归 daily_break，故用两日样本：
    # 周一全天 + 周二仅缺 1 根 → 1/2=50% < 60% → unexpected）
    mon_full = day_minutes(MON, hours=range(24))
    tue_hole = [t for t in day_minutes(MON + timedelta(days=1), hours=range(24))
                if t != datetime(2026, 10, 27, 2, 30)]
    md2 = quality_report(make_bars(mon_full + tue_hole), "M1",
                         symbol="XAUUSD").render_markdown()
    assert "micro_gap=1" in md2  # 单分钟缺失归 micro_gap


# ---- CLI 脚本 ----


def test_quality_report_script_writes_json_and_summary(tmp_path):
    from mt5data.store import save_bars

    script = Path(__file__).resolve().parents[1] / "scripts" / "quality_report.py"
    spec = importlib.util.spec_from_file_location("quality_report_script", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    t0 = datetime(2026, 10, 26, 0, 0)
    times = [t0 + timedelta(minutes=i) for i in range(200)]
    save_bars(str(tmp_path / "data"), "XAUUSD", "M1", make_bars(times))

    rc = mod.main(["--symbols", "XAUUSD,NOSUCH", "--timeframes", "M1",
                   "--data-root", str(tmp_path / "data"),
                   "--out", str(tmp_path / "reports")])
    assert rc == 0

    j = tmp_path / "reports" / "XAUUSD_M1.json"
    assert j.exists()
    d = json.loads(j.read_text(encoding="utf-8"))
    assert d["symbol"] == "XAUUSD" and d["rows"] == 200

    summary = (tmp_path / "reports" / "summary.md").read_text(encoding="utf-8")
    assert "XAUUSD" in summary and "100.00" in summary
    assert "NOSUCH" in summary and "跳过" in summary  # 缺文件在汇总注明


# ---- I3 回归：同一物理休市不被夏令时拆裂 ----

def test_daily_break_same_local_hour_across_dst_not_split():
    """同一物理维护窗（NY 17:00）夏令时落在标签 21 点、冬令时落在 22 点——
    按本地小时聚合后两周合计 8/8 有效交易日 → 周一至周四的 8 处全为
    daily_break、unexpected=0。（按 UTC 标签小时分桶则各 4/8=50% <60%，
    会全部误报 unexpected。周五的维护窗与收市窗口相交，归 weekend 属合理。）"""
    week_a = [date(2026, 10, 19) + timedelta(days=i) for i in range(5)]  # EDT 周
    week_b = [date(2026, 11, 2) + timedelta(days=i) for i in range(5)]   # EST 周
    times = []
    for d in week_a:
        times += day_minutes(d, hours=range(24), skip_hours={21})  # EDT: 21:00 UTC=NY17
    for d in week_b:
        times += day_minutes(d, hours=range(24), skip_hours={22})  # EST: 22:00 UTC=NY17
    rep = quality_report(make_bars(times), "M1")
    kinds = [g.kind for g in rep.gaps]
    assert kinds.count("daily_break") == 8   # 周一至周四 × 2 周
    assert kinds.count("unexpected") == 0     # 关键：不被 DST 拆成 unexpected
    # T79：周五收市窗仍 weekend（2 处）；两周之间横跨两个周末的 9 天大缺口
    # 不再静默归 weekend，改判 long_gap
    assert kinds.count("weekend") == 2
    assert kinds.count("long_gap") == 1


# ---- T79/R12-G1：长缺口判定——weekend 须与周末窗"基本重合" ----


def test_round12_probe_27day_hole_is_long_gap_not_weekend():
    """复刻 docs/reviews/probes/2026-10-27-round12-gap-probe.py（先红后绿）：
    工作日 H1 序列挖掉 2021-06-03 10:00 → 2021-07-01 08:00——修复前该 27 天
    缺口整段归 weekend（探针红）；修复后须为 long_gap，横跨多周末不得
    归 weekend。"""
    idx = pd.date_range("2021-05-03", "2021-07-30 20:00", freq="h")
    idx = idx[idx.dayofweek < 5]
    hole = ((idx >= pd.Timestamp("2021-06-03 10:00"))
            & (idx < pd.Timestamp("2021-07-01 08:00")))
    df = pd.DataFrame({"time": idx[~hole], "open": 1.2, "high": 1.21,
                       "low": 1.19, "close": 1.2, "tick_volume": 1,
                       "spread": 1, "real_volume": 0})
    rep = quality_report(df, "H1")
    big = [g for g in rep.gaps if (g.end - g.start).days >= 3]
    assert len(big) == 1
    assert big[0].kind == "long_gap"
    assert big[0].start == datetime(2021, 6, 3, 10, 0)
    assert big[0].end == datetime(2021, 7, 1, 8, 0)
    # 任何 ≥3 天的缺口都不再是 weekend
    assert all(g.kind != "weekend" for g in rep.gaps
               if (g.end - g.start).days >= 3)
    # 探针的合成日历是"工作日 24 小时"：周五数据流到 23:00（越过收盘 2 小时）、
    # 周一 00:00 才恢复（越过周日开盘 3 小时）——周缺口超出周末窗两端容差，
    # 如实判 holiday（可见），周末窗本身没有吞掉它
    kinds = [g.kind for g in rep.gaps]
    assert kinds.count("long_gap") == 1
    assert kinds.count("weekend") == 0
    assert kinds.count("holiday") == 8
    md = rep.render_markdown()
    assert "long_gap=1" in md  # 计数行含新类别


def test_real_shape_friday_early_close_and_sunday_late_open_still_weekend():
    # 真实库形态（EDT 周，H1）：周五末根 20:00（提前 1 根收）→ 周日首根
    # 22:00（晚开 1 根）——都在一根 bar 容差内，仍是 weekend（不误报）
    times = ([datetime(2026, 7, 17, 18, 0) + timedelta(hours=i) for i in range(3)]
             + [datetime(2026, 7, 19, 22, 0)])
    rep = quality_report(make_bars(times), "H1")
    assert [g.kind for g in rep.gaps] == ["weekend"]
    assert rep.gaps[0].bars_missing == 49  # 20:00 → 22:00 = 50h 间隔 - 1 根


def test_sunday_late_open_two_bars_is_holiday_not_silent_weekend():
    # 晚开 2 根（首根 23:00）超出容差 → 不归 weekend；≥20h → holiday（可见）
    times = ([datetime(2026, 7, 17, 18, 0) + timedelta(hours=i) for i in range(3)]
             + [datetime(2026, 7, 19, 23, 0)])
    rep = quality_report(make_bars(times), "H1")
    assert [g.kind for g in rep.gaps] == ["holiday"]


def test_friday_early_close_three_hours_is_holiday_not_silent_weekend():
    # 提前 3 小时收盘（末根 17:00）超出 1 根容差 → holiday（可见），不静默
    times = ([datetime(2026, 7, 17, 15, 0) + timedelta(hours=i) for i in range(3)]
             + [datetime(2026, 7, 19, 21, 0)])
    rep = quality_report(make_bars(times), "H1")
    assert [g.kind for g in rep.gaps] == ["holiday"]


def test_long_gap_listed_in_markdown_details():
    # 构造最小 27 天洞：6/2 末根 → 7/1 首根（跨 Jun 4-6/11-13/18-20/25-27）
    times = [datetime(2021, 6, 2, 20, 0), datetime(2021, 7, 1, 1, 0)]
    rep = quality_report(make_bars(times), "H1", symbol="EURUSD")
    assert [g.kind for g in rep.gaps] == ["long_gap"]
    md = rep.render_markdown()
    assert "long_gap 缺口" in md and "2021-06-02T21:00:00" in md


# ---- T79：known_gaps 的登记与读取（meta.json） ----


def test_weekend_adjacent_gaps_do_not_poison_daily_break_denominator():
    """T79 回归：与周末窗相交但超出容差的缺口不得进入日间休市分桶——
    否则其首末出现日会拉宽重现率分母，把真正的日维护窗挤出 daily_break
    （真实案例：收紧判定首版把 USTEC H1 的 384 处维护窗挤成 micro_gap）。

    构造：9-18 周五提前收盘 1 小时（周末相邻缺口 → holiday）；10-05 起
    周一至周四 21:00 UTC（17:00 NY，全程 EDT）维护窗 ×16（≥ _MIN_RECUR，
    走窗口分母）。若周末相邻缺口进了分桶，lo 被拉回 9-18，分母 19→30，
    60% 重现率不成立，16 处维护窗全变 unexpected。"""
    days = [date(2026, 9, 16) + timedelta(days=i) for i in range(45)]  # →10-30
    times = []
    for d in days:
        wd = d.weekday()
        if wd == 5:
            continue                            # 周六无数据
        if wd == 6:                             # 周日 21:00 开盘（EDT）
            times += day_minutes(d, hours=range(21, 24))
            continue
        if wd == 4:                             # 周五 20:59 收
            last_hour = 19 if d == date(2026, 9, 18) else 20
            times += day_minutes(d, hours=range(0, last_hour + 1))
            continue
        skip = {21} if d >= date(2026, 10, 5) else frozenset()   # 维护窗
        times += day_minutes(d, hours=range(24), skip_hours=skip)
    rep = quality_report(make_bars(times), "M1")
    kinds = [g.kind for g in rep.gaps]
    assert kinds.count("daily_break") == 16     # 维护窗全部保住
    assert kinds.count("unexpected") == 0       # 不被挤出成 unexpected
    assert kinds.count("holiday") == 1          # 9-18 周末相邻缺口按长度归类
    assert kinds.count("weekend") == 5          # 其余周末照旧（末周五是序列终点）


# ---- T79：known_gaps 的登记与读取（meta.json） ----


def test_known_gaps_roundtrip_and_validation(tmp_path):
    from mt5data.quality import read_known_gaps, write_known_gaps
    from mt5data.store import save_bars

    t0 = datetime(2026, 10, 26, 0, 0)
    save_bars(str(tmp_path), "EURUSD", "H1",
              make_bars([t0 + timedelta(hours=i) for i in range(5)]))
    assert read_known_gaps(str(tmp_path), "EURUSD", "H1") == []

    write_known_gaps(str(tmp_path), "EURUSD", "H1", [
        {"start": "2021-05-17T14:00:00", "end": "2021-06-14T11:00:00",
         "reason": "终端无该段历史"}])
    assert read_known_gaps(str(tmp_path), "EURUSD", "H1") == [
        (datetime(2021, 5, 17, 14, 0), datetime(2021, 6, 14, 11, 0))]
    # meta 其余键不被覆盖；条目带 reason/登记时间
    meta = json.loads((tmp_path / "bars" / "EURUSD" / "H1" / "meta.json")
                      .read_text(encoding="utf-8"))
    assert meta["symbol"] == "EURUSD" and meta["rows"] == 5
    assert meta["known_gaps"][0]["reason"] == "终端无该段历史"
    assert "registered_utc" in meta["known_gaps"][0]
    # 同 start 重复登记 → 去重替换
    write_known_gaps(str(tmp_path), "EURUSD", "H1", [
        {"start": "2021-05-17T14:00:00", "end": "2021-06-14T11:00:00",
         "reason": "更新理由"}])
    assert len(read_known_gaps(str(tmp_path), "EURUSD", "H1")) == 1
    # 非法条目拒绝：缺 end、start ≥ end
    with pytest.raises(ValueError):
        write_known_gaps(str(tmp_path), "EURUSD", "H1",
                         [{"start": "2021-05-17T14:00:00"}])
    with pytest.raises(ValueError):
        write_known_gaps(str(tmp_path), "EURUSD", "H1",
                         [{"start": "2021-06-14T11:00:00",
                           "end": "2021-05-17T14:00:00"}])
    # meta 里被写坏（非数组）→ 读取拒绝而非静默
    meta["known_gaps"] = {"start": "x"}
    (tmp_path / "bars" / "EURUSD" / "H1" / "meta.json").write_text(
        json.dumps(meta), encoding="utf-8")
    with pytest.raises(ValueError):
        read_known_gaps(str(tmp_path), "EURUSD", "H1")


def test_read_known_gaps_tolerates_missing_meta(tmp_path):
    from mt5data.quality import read_known_gaps

    assert read_known_gaps(str(tmp_path), "NOSUCH", "H1") == []


def test_write_known_gaps_uses_unique_tmp_per_write(tmp_path, monkeypatch):
    """H-11 失败测试：两次登记各用各的临时文件（固定 .tmp 会被并发写者互踩）。"""
    import os

    from mt5data.quality import write_known_gaps
    from mt5data.store import save_bars

    t0 = datetime(2026, 10, 26, 0, 0)
    save_bars(str(tmp_path), "EURUSD", "H1",
              make_bars([t0 + timedelta(hours=i) for i in range(5)]))

    recorded = []
    real_replace = os.replace

    def rec_replace(tmp, final):
        recorded.append(tmp)
        return real_replace(tmp, final)

    monkeypatch.setattr(os, "replace", rec_replace)

    write_known_gaps(str(tmp_path), "EURUSD", "H1", [
        {"start": "2021-05-17T14:00:00", "end": "2021-06-14T11:00:00",
         "reason": "a"}])
    write_known_gaps(str(tmp_path), "EURUSD", "H1", [
        {"start": "2022-01-03T00:00:00", "end": "2022-01-04T00:00:00",
         "reason": "b"}])

    assert len(recorded) == 2
    assert len(set(recorded)) == 2, f"临时文件名复用：{recorded}"


# ---- I4 回归：异常列表限量与 p99=0 防退化 ----

def test_spread_anomaly_capped_with_count_and_histogram():
    """150 根 spread=0：p99=0 → 退化只按 <=0 计；明细限量 100 条但计数/直方图全量。"""
    start = datetime(2026, 10, 5, 0, 0)
    times = [start + timedelta(minutes=i) for i in range(150)]
    rep = quality_report(make_bars(times, spread=[0] * 150), "M1")
    assert rep.spread_anomaly_count == 150
    assert len(rep.spread_anomalies) == 100  # 明细限量
    assert rep.spread_anomaly_by_hour == {0: 60, 1: 60, 2: 30}
    assert any("仅按 spread<=0 计数" in n for n in rep.notes)
    assert any("明细仅列前 100 条" in n for n in rep.notes)
    d = rep.to_dict()
    assert d["spread_anomaly_count"] == 150 and d["spread_anomaly_by_hour"][0] == 60


def test_daily_break_starting_mid_history_uses_window_denominator():
    """真实案例回归：Exness 的 NY17:00 维护窗 2017 年才开始、且只在周一至周四
    （周五那次并入 weekend）——全局分母下频率只有约 48% 而全部误报 unexpected；
    窗口分母（首末出现日内的交易日）必须把它判成 daily_break。"""
    import itertools
    days = [date(2026, 3, 2) + timedelta(days=i) for i in range(40)]  # 含周末
    times = []
    for i, d in enumerate(days):
        skip = {21} if (i >= 20 and d.weekday() < 4) else frozenset()
        times += day_minutes(d, hours=range(24), skip_hours=skip)
    rep = quality_report(make_bars(times), "M1")
    kinds = [g.kind for g in rep.gaps]
    assert kinds.count("daily_break") >= 12      # 后半段周一至周四的维护窗全中
    assert kinds.count("unexpected") == 0        # 关键：不再误报
