"""换日时刻历史测量（T35 / H5 / 决策 D28）：DST 错位窗口里的每日无 bar 空档。

原理：每年两次美欧 DST 不同步的"错位窗口"——
- 3 月：美国第二个周日先切夏令时，欧洲最后一个周日才切（约 3 周）；
- 10 月底：欧洲最后一个周日先切冬令时，美国 11 月第一个周日才切（约 1 周）。
此时若换日/每日休市跟随纽约（17:00 America/New_York），空档 UTC 位置与
"美欧同为夏令时"的对照周一致（夏令时 21:00 UTC）；若跟随欧洲（服务器
EET/EEST 零点），空档与"美欧同为冬令时"的对照周一致（冬令时 22:00 UTC）。
USTEC 2021–2023H1 的旧休市表（纽约 16:00 起 2.5 小时）同样适用该判别：
错位窗口停在"夏令时位置"= 跟随纽约，移到"冬令时位置"= 跟随欧洲。

用法（真数据实跑，2021–2025 为锁箱前年份，无需 --ops-ack）：
    ./.venv/Scripts/python.exe scripts/measure_rollover.py
    ./.venv/Scripts/python.exe scripts/measure_rollover.py --symbols USTEC XAUUSD --years 2021 2022 2023 2024 2025

锁箱纪律（Global Constraints）：默认只读 global_cutoff 之前的 bar；cutoff 之后
（2025-10-24 起，含 2025-10 错位窗与 2026 年全部）属锁箱期，仅运维测量可用，
须显式 --ops-ack 才读入。判定表人工誊入
docs/research/rollover-dst-2026-10-25.md 第 3 节。

注意：XAUUSD 2017 年的 parquet 历史含每小时 1 根的回填伪空档，2021 年起为
实时记录数据——本脚本默认只统计 2021+。
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from mt5data import store  # noqa: E402

US_TZ = "America/New_York"
EU_TZ = "Europe/Helsinki"   # Exness 服务器惯例 EET/EEST；全欧 DST 切换日一致
DEFAULT_SYMBOLS = ("USTEC", "XAUUSD")
DEFAULT_YEARS = tuple(range(2021, 2026))
TF_MINUTES = 5
MIN_GAP_MINUTES = 30    # 每日休市空档下限（滤掉 5–25 分钟的数据毛刺）
MAX_GAP_MINUTES = 355   # 上限（周末 47–49 小时、全天假期不参与；总断档 < 6 小时）
CONTROL_DAYS = 14       # 对照周长度：切换日前两周


@dataclass(frozen=True)
class Gap:
    """一段无 bar 空档（naive UTC）：start=前一根 bar 时间+周期，end=后一根 bar 时间。"""

    start: datetime
    end: datetime
    minutes: int


def find_gaps(times, *, tf_minutes: int = TF_MINUTES,
              min_minutes: int = MIN_GAP_MINUTES,
              max_minutes: int = MAX_GAP_MINUTES) -> list[Gap]:
    """升序去重的 bar 时间序列 → 每日休市规模的空档列表。

    只收 [min_minutes, max_minutes] 的空档：小空档是数据毛刺，大空档是
    周末/假期（另有 sessions.gap_is_weekend 的口径，此处只做尺寸过滤）。
    """
    ts = pd.Series(pd.to_datetime(list(times))).drop_duplicates().sort_values()
    out: list[Gap] = []
    tf = pd.Timedelta(minutes=tf_minutes)
    for a, b in zip(ts.iloc[:-1], ts.iloc[1:]):
        missing = (b - a - tf).total_seconds() / 60.0
        if min_minutes <= missing <= max_minutes:
            out.append(Gap(start=(a + tf).to_pydatetime(),
                           end=b.to_pydatetime(),
                           minutes=int(missing)))
    return out


# ---- DST 切换日（zoneinfo 扫描，不写死表） ----


def dst_switch(tz_name: str, year: int, first_month: int, last_month: int) -> date:
    """在 [first_month, last_month] 内扫描该时区 UTC 偏移发生变化的日期。

    每天取 UTC 正午的偏移比较——切换总是发生在凌晨/清晨，正午必已切换。
    """
    tz = ZoneInfo(tz_name)
    d = date(year, first_month, 1)
    end = date(year, last_month, calendar.monthrange(year, last_month)[1])
    prev = None
    while d <= end:
        off = datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc)\
            .astimezone(tz).utcoffset()
        if prev is not None and off != prev:
            return d
        prev = off
        d += timedelta(days=1)
    raise ValueError(f"{tz_name} 在 {year}-{first_month:02d}..{last_month:02d} 无 DST 切换")


@dataclass(frozen=True)
class DSTDates:
    us_spring: date   # 美国第二个周日（3 月）
    eu_spring: date   # 欧洲最后一个周日（3 月）
    eu_autumn: date   # 欧洲最后一个周日（10 月）
    us_autumn: date   # 美国第一个周日（11 月）


def dst_dates(year: int) -> DSTDates:
    return DSTDates(
        us_spring=dst_switch(US_TZ, year, 3, 3),
        eu_spring=dst_switch(EU_TZ, year, 3, 3),
        eu_autumn=dst_switch(EU_TZ, year, 10, 10),
        us_autumn=dst_switch(US_TZ, year, 11, 11),
    )


@dataclass(frozen=True)
class Window:
    """一个统计窗口：[start, end)（UTC 日期），label 区分错位窗与对照周。"""

    label: str    # march_mismatch | october_mismatch | winter_control | summer_control
    year: int
    start: date
    end: date


def windows_for_year(year: int) -> list[Window]:
    d = dst_dates(year)
    return [
        Window("march_mismatch", year, d.us_spring, d.eu_spring),
        Window("october_mismatch", year, d.eu_autumn, d.us_autumn),
        # 对照周：美国春拨前两周（美欧同冬令时）、欧洲秋拨前两周（美欧同夏令时）
        Window("winter_control", year,
               d.us_spring - timedelta(days=CONTROL_DAYS), d.us_spring),
        Window("summer_control", year,
               d.eu_autumn - timedelta(days=CONTROL_DAYS), d.eu_autumn),
    ]


@dataclass
class WindowSummary:
    """一个窗口内每日空档的众数统计（空档起始为 UTC 时刻）。"""

    year: int
    label: str
    modal_start: str | None     # "HH:MM"（UTC）众数；None=窗口内无空档/未读
    modal_minutes: int | None   # 众数空档的无数据分钟数
    n_days: int                 # 有空档的天数
    variants: dict = field(default_factory=dict)  # {("HH:MM", 分钟): 天数}
    skipped: str | None = None  # 未读原因（锁箱期 / 无数据）


def summarize_window(gaps: list[Gap], win: Window) -> WindowSummary:
    s = WindowSummary(year=win.year, label=win.label, modal_start=None,
                      modal_minutes=None, n_days=0)
    in_win = [g for g in gaps
              if win.start <= g.start.date() < win.end
              and g.start.date() == g.start.date()]
    if not in_win:
        s.skipped = "窗口内无符合尺寸的空档"
        return s
    variants: Counter = Counter()
    days = set()
    for g in in_win:
        variants[(g.start.strftime("%H:%M"), g.minutes)] += 1
        days.add(g.start.date())
    (start_hhmm, minutes), _cnt = variants.most_common(1)[0]
    s.modal_start = start_hhmm
    s.modal_minutes = minutes
    s.n_days = len(days)
    s.variants = {f"{k[0]}+{k[1]}min": v for k, v in sorted(variants.items())}
    return s


# ---- 判定 ----


@dataclass
class SymbolResult:
    symbol: str
    windows: list[WindowSummary]
    follows: str       # new_york | europe | ambiguous | insufficient
    votes: list = field(default_factory=list)  # [(year, 窗口, 票)]


def _vote(summaries: list[WindowSummary]) -> tuple[list, str]:
    by = {(s.year, s.label): s for s in summaries}
    votes: list[tuple[int, str, str]] = []
    for year in sorted({s.year for s in summaries}):
        summer = by.get((year, "summer_control"))
        winter = by.get((year, "winter_control"))
        if (summer is None or winter is None
                or summer.modal_start is None or winter.modal_start is None):
            continue
        if summer.modal_start == winter.modal_start:
            continue  # 锚点不随任一 DST 移动（固定 UTC？），该年无法判别
        for lbl in ("march_mismatch", "october_mismatch"):
            mm = by.get((year, lbl))
            if mm is None or mm.modal_start is None:
                continue
            if mm.modal_start == summer.modal_start:
                votes.append((year, lbl, "new_york"))
            elif mm.modal_start == winter.modal_start:
                votes.append((year, lbl, "europe"))
            else:
                votes.append((year, lbl, "anomalous"))
    if not votes:
        return votes, "insufficient"
    kinds = {v for _, _, v in votes}
    if kinds == {"new_york"}:
        return votes, "new_york"
    if kinds == {"europe"}:
        return votes, "europe"
    return votes, "ambiguous"


def measure_symbol(symbol: str, bars: pd.DataFrame | None, *, years,
                   cutoff: datetime | None = None, ops_ack: bool = False
                   ) -> SymbolResult:
    """单品种：读 bar → 锁箱过滤 → 逐年逐窗口汇总 → 判定。"""
    summaries: list[WindowSummary] = []
    if bars is None or len(bars) == 0:
        for year in years:
            for w in windows_for_year(year):
                s = WindowSummary(year=year, label=w.label, modal_start=None,
                                  modal_minutes=None, n_days=0,
                                  skipped="该品种无 K 线数据")
                summaries.append(s)
        return SymbolResult(symbol, summaries, "insufficient", [])

    ts = pd.to_datetime(bars["time"]).drop_duplicates().sort_values()
    if cutoff is not None and not ops_ack:
        n_before = len(ts)
        ts = ts[ts < pd.Timestamp(cutoff)]
        dropped = n_before - len(ts)
    else:
        dropped = 0
    gaps = find_gaps(ts)
    for year in years:
        for w in windows_for_year(year):
            if (cutoff is not None and not ops_ack
                    and datetime.combine(w.start, time()) >= cutoff):
                summaries.append(WindowSummary(
                    year=year, label=w.label, modal_start=None,
                    modal_minutes=None, n_days=0,
                    skipped=f"锁箱期（cutoff {cutoff:%Y-%m-%d} 之后）未读，--ops-ack 可含"))
                continue
            summaries.append(summarize_window(gaps, w))
    votes, follows = _vote(summaries)
    res = SymbolResult(symbol, summaries, follows, votes)
    res.dropped_lockbox_rows = dropped  # noqa: 属性式记录，供 CLI 打印
    return res


# ---- 渲染 ----


def _fmt_summary(s: WindowSummary) -> str:
    if s.modal_start is None:
        return f"—（{s.skipped or '无空档'}）"
    end_total = int(s.modal_start[:2]) * 60 + int(s.modal_start[3:]) + s.modal_minutes
    end_h, end_m = divmod(end_total, 60)
    extra = "; ".join(f"{k}×{v}" for k, v in s.variants.items())
    return (f"{s.modal_start}–{end_h:02d}:{end_m:02d}"
            f"（{s.modal_minutes} 分 × {s.n_days} 天）{extra}")


def render_markdown(results: list[SymbolResult]) -> str:
    lines = []
    for res in results:
        lines.append(f"### {res.symbol}：跟随 "
                     f"{'纽约' if res.follows == 'new_york' else '欧洲' if res.follows == 'europe' else res.follows}"
                     f"（{res.follows}）")
        lines.append("")
        lines.append("| 年份 | 3 月错位窗（美夏欧冬） | 10 月错位窗（欧冬美夏）"
                     "| 冬令时对照周 | 夏令时对照周 |")
        lines.append("| --- | --- | --- | --- | --- |")
        by = {(s.year, s.label): s for s in res.windows}
        for year in sorted({s.year for s in res.windows}):
            row = [str(year)]
            for lbl in ("march_mismatch", "october_mismatch",
                        "winter_control", "summer_control"):
                s = by.get((year, lbl))
                row.append(_fmt_summary(s) if s else "—")
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
        if res.votes:
            votes = "；".join(f"{y} {lbl}→{v}" for y, lbl, v in res.votes)
            lines.append(f"判定票：{votes}")
        lines.append("")
    return "\n".join(lines)


# ---- CLI ----


def load_cutoff(data_root: str) -> datetime | None:
    p = os.path.join(data_root, "lockbox.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return datetime.fromisoformat(json.load(f)["global_cutoff"])


def main() -> int:
    ap = argparse.ArgumentParser(
        description="DST 错位窗口的每日空档测量 → 换日跟随纽约还是欧洲（T35/H5）")
    ap.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    ap.add_argument("--data-root", default="data")
    ap.add_argument("--tf", default="M5")
    ap.add_argument("--years", nargs="+", type=int, default=list(DEFAULT_YEARS))
    ap.add_argument("--ops-ack", action="store_true",
                    help="承认读取锁箱期（cutoff 之后）数据仅用于运维测量")
    args = ap.parse_args()

    cutoff = load_cutoff(args.data_root)
    results = []
    for sym in args.symbols:
        bars = store.load_bars(args.data_root, sym, args.tf)
        if bars is None:
            print(f"· {sym}：无 {args.tf} 数据，跳过", file=sys.stderr)
            continue
        res = measure_symbol(sym, bars, years=tuple(args.years),
                             cutoff=cutoff, ops_ack=args.ops_ack)
        results.append(res)

    cutoff_txt = (cutoff.strftime("%Y-%m-%d") if cutoff is not None else "无")
    print(f"换日口径测量（{', '.join(args.symbols)}；cutoff={cutoff_txt}"
          f"{'，含锁箱期(ops-ack)' if args.ops_ack else '，锁箱期未读'}）")
    print()
    print(render_markdown(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
