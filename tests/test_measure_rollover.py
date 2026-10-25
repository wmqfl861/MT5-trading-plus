"""measure_rollover 纯逻辑测试：DST 错位窗口里的每日空档 → 换日跟随哪边（T35/H5）。

合成 K 线先行（计划失败测试清单第 3 条）：构造"21:00 UTC 空档"（跟随纽约：
错位窗口里美东仍是夏令时，17:00 NY = 21:00 UTC）与"22:00 UTC 空档"（跟随
欧洲：服务器 EET 冬令时零点 = 22:00 UTC）两种形态，判定必须正确。
真数据实跑结果写进 docs/research/rollover-dst-2026-10-25.md 第 3 节。
"""
import importlib.util
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "measure_rollover.py"


def load_module():
    spec = importlib.util.spec_from_file_location("measure_rollover", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure_rollover"] = mod   # dataclass field 解析需要模块已注册
    spec.loader.exec_module(mod)
    return mod


mr = load_module()


# ---- 构造工具：一天的 M5 bar 序列，挖掉 [gap_start, gap_start+gap_minutes) ----


def synth_day(day: date, gap_start_hh: int, gap_minutes: int) -> list[pd.Timestamp]:
    """12:00–23:55 的 M5 bar，挖掉 [gap_start_hh:00, +gap_minutes) 的空档。

    日与日之间的 12:00–23:55 断档（约 12 小时）远超空档上限，不参与统计。
    """
    base = pd.Timestamp(day)
    out = []
    for t in pd.date_range(base + pd.Timedelta(hours=12),
                           base + pd.Timedelta(hours=23, minutes=55), freq="5min"):
        gs = base + pd.Timedelta(hours=gap_start_hh)
        if gs <= t < gs + pd.Timedelta(minutes=gap_minutes):
            continue
        out.append(t)
    return out


def bars_df(days_gaps: list[tuple[date, int, int]]) -> pd.DataFrame:
    rows = []
    for day, gh, gm in days_gaps:
        rows.extend(synth_day(day, gh, gm))
    return pd.DataFrame({"time": sorted(rows)})


def fill(year: int, pattern: dict[str, int]) -> list[tuple[date, int, int]]:
    """按窗口类生成合成日：pattern = {窗口标签: 空档起始 UTC 小时}（空档 60 分钟）。

    每个窗口取其中 3 个工作日，足以让众数唯一。
    """
    wins = {w.label: w for w in mr.windows_for_year(year)}
    out = []
    for label, gap_h in pattern.items():
        w = wins[label]
        d = w.start
        taken = 0
        while d < w.end and taken < 3:
            if d.weekday() < 5:  # 周一..周五
                out.append((d, gap_h, 60))
                taken += 1
            d += timedelta(days=1)
    return out


# ---- find_gaps：空档检测的起止与过滤 ----


def test_find_gaps_reports_start_end_and_minutes():
    day = date(2024, 6, 3)
    df = bars_df([(day, 21, 60)])
    gaps = mr.find_gaps(df["time"])
    daily = [g for g in gaps if g.minutes == 60]
    assert len(daily) == 1
    g = daily[0]
    assert g.start == pd.Timestamp(day) + pd.Timedelta(hours=21)
    assert g.end == pd.Timestamp(day) + pd.Timedelta(hours=22)
    assert g.minutes == 60


def test_find_gaps_filters_small_and_weekend_sized():
    day = date(2024, 6, 3)
    # 删掉 15:00 一根 → 14:55→15:05 缺 5 分钟（数据毛刺，应滤除）
    ts = [t for t in synth_day(day, 21, 60)
          if t != pd.Timestamp(day) + pd.Timedelta(hours=15)]
    # 再加两天后的一根孤 bar → 周末级断档（>6 小时，滤除）
    ts.append(pd.Timestamp(day) + pd.Timedelta(days=2, hours=12))
    gaps = mr.find_gaps(pd.Series(sorted(ts)))
    assert all(g.minutes >= mr.MIN_GAP_MINUTES for g in gaps)
    assert all(g.minutes <= mr.MAX_GAP_MINUTES for g in gaps)
    assert [g.start.hour for g in gaps] == [21]  # 只剩每日休市空档


# ---- DST 切换日（zoneinfo 独立钉死） ----


@pytest.mark.parametrize("year,us_spring,eu_spring,eu_autumn,us_autumn", [
    (2021, date(2021, 3, 14), date(2021, 3, 28), date(2021, 10, 31), date(2021, 11, 7)),
    (2024, date(2024, 3, 10), date(2024, 3, 31), date(2024, 10, 27), date(2024, 11, 3)),
    (2025, date(2025, 3, 9),  date(2025, 3, 30), date(2025, 10, 26), date(2025, 11, 2)),
])
def test_dst_dates_pinned(year, us_spring, eu_spring, eu_autumn, us_autumn):
    d = mr.dst_dates(year)
    assert d.us_spring == us_spring
    assert d.eu_spring == eu_spring
    assert d.eu_autumn == eu_autumn
    assert d.us_autumn == us_autumn


def test_windows_for_year_labels_and_bounds():
    wins = {w.label: w for w in mr.windows_for_year(2024)}
    d = mr.dst_dates(2024)
    assert wins["march_mismatch"].start == d.us_spring
    assert wins["march_mismatch"].end == d.eu_spring
    assert wins["october_mismatch"].start == d.eu_autumn
    assert wins["october_mismatch"].end == d.us_autumn
    # 对照周：美欧同冬令时（美国春拨前两周）、同夏令时（欧洲秋拨前两周）
    assert wins["winter_control"].end == d.us_spring
    assert wins["winter_control"].start == d.us_spring - timedelta(days=14)
    assert wins["summer_control"].end == d.eu_autumn
    assert wins["summer_control"].start == d.eu_autumn - timedelta(days=14)


# ---- 判定：合成 21:00 / 22:00 两种空档（失败测试清单第 3 条） ----


def test_judge_follows_new_york_on_synthetic_bars():
    # 夏令时对照 21:00、冬令时对照 22:00、两个错位窗口都停在 21:00 → 跟随纽约
    days = fill(2024, {"summer_control": 21, "winter_control": 22,
                       "march_mismatch": 21, "october_mismatch": 21})
    res = mr.measure_symbol("SYNTH", bars_df(days), years=(2024,))
    assert res.follows == "new_york"
    assert res.votes == [(2024, "march_mismatch", "new_york"),
                         (2024, "october_mismatch", "new_york")]


def test_judge_follows_europe_on_synthetic_bars():
    # 错位窗口空档移到 22:00（欧洲已切冬令时、服务器零点前移）→ 跟随欧洲
    days = fill(2024, {"summer_control": 21, "winter_control": 22,
                       "march_mismatch": 22, "october_mismatch": 22})
    res = mr.measure_symbol("SYNTH", bars_df(days), years=(2024,))
    assert res.follows == "europe"


def test_judge_insufficient_without_controls():
    days = fill(2024, {"march_mismatch": 21})
    res = mr.measure_symbol("SYNTH", bars_df(days), years=(2024,))
    assert res.follows == "insufficient"
    assert res.votes == []


# ---- 锁箱：错位窗口落在锁箱期内默认不读，--ops-ack 才包含 ----


def _synth_2025_ny_pattern() -> pd.DataFrame:
    return bars_df(fill(2025, {"summer_control": 21, "winter_control": 22,
                               "march_mismatch": 21, "october_mismatch": 21}))


def test_lockbox_window_skipped_without_ops_ack():
    cutoff = datetime(2025, 10, 24)   # 2025-10 错位窗（10-26 起）全在锁箱内
    res = mr.measure_symbol("SYNTH", _synth_2025_ny_pattern(), years=(2025,),
                            cutoff=cutoff, ops_ack=False)
    by = {(s.year, s.label): s for s in res.windows}
    oct_win = by[(2025, "october_mismatch")]
    assert oct_win.modal_start is None
    assert "锁箱" in (oct_win.skipped or "")
    # 三月错位窗与对照周在锁箱之前，照常统计并参与判定
    assert by[(2025, "march_mismatch")].modal_start == "21:00"
    assert res.follows == "new_york"
    assert res.votes == [(2025, "march_mismatch", "new_york")]


def test_lockbox_window_included_with_ops_ack():
    cutoff = datetime(2025, 10, 24)
    res = mr.measure_symbol("SYNTH", _synth_2025_ny_pattern(), years=(2025,),
                            cutoff=cutoff, ops_ack=True)
    by = {(s.year, s.label): s for s in res.windows}
    assert by[(2025, "october_mismatch")].modal_start == "21:00"
    assert res.follows == "new_york"
    assert res.votes == [(2025, "march_mismatch", "new_york"),
                         (2025, "october_mismatch", "new_york")]


# ---- 渲染：判定表可进报告 ----


def test_render_markdown_contains_verdict_table():
    days = fill(2024, {"summer_control": 21, "winter_control": 22,
                       "march_mismatch": 21, "october_mismatch": 21})
    res = mr.measure_symbol("SYNTH", bars_df(days), years=(2024,))
    md = mr.render_markdown([res])
    assert "| 年份 |" in md
    assert "SYNTH" in md
    assert "new_york" in md
