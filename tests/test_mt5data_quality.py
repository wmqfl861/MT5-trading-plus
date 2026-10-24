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
    assert "unexpected 缺口" in md2


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
