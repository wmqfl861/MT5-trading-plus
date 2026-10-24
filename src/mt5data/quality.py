"""K 线质检：只读输入 DataFrame，只报告，绝不改数、不删数。

缺口分类次序（前者优先）：
1. weekend——与 sessions 判定的周末窗口相交（DST 感知，非"恒 48 小时"）；
2. daily_break——缺失起始小时在 ≥60% 的交易日重复出现（常规日间休市）；
3. holiday——缺口 ≥20 小时且不跨周末；
4. unexpected——其余，需人工排查。

注意：样本只有 1 个交易日时，任何缺口都满足"≥60% 交易日同一小时"
（1/1=100%），会被归为 daily_break——单日样本本身无统计意义，属预期。
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from mt5data.sessions import NY_TZ, gap_is_weekend
from mt5data.store import TIMEFRAME_SECONDS

_HOLIDAY_HOURS = 20.0
_DAILY_BREAK_NUM, _DAILY_BREAK_DEN = 6, 10  # ≥60%，整数比较免浮点误差
_SPIKE_MULT = 20.0
_SPIKE_WINDOW = 20
_SPIKE_RETRACE = 0.5
_SPREAD_P99_MULT = 10.0
_PRICE_FIELDS = ("open", "high", "low", "close")


@dataclass
class Gap:
    """一处缺口。start=首根缺失 bar 的 time；end=缺口后首根现存 bar 的 time。"""

    kind: str  # weekend / daily_break / holiday / unexpected
    start: datetime
    end: datetime
    bars_missing: int

    def to_dict(self) -> dict:
        return {"kind": self.kind, "start": self.start.isoformat(),
                "end": self.end.isoformat(), "bars_missing": self.bars_missing}


@dataclass
class QualityReport:
    symbol: str
    timeframe: str
    first_time: datetime | None
    last_time: datetime | None
    rows: int
    expected_rows: int
    coverage_pct: float
    gaps: list[Gap] = field(default_factory=list)
    duplicates: int = 0
    disorder: int = 0
    spikes: list[dict] = field(default_factory=list)
    spread_anomalies: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "first_time": self.first_time.isoformat() if self.first_time else None,
            "last_time": self.last_time.isoformat() if self.last_time else None,
            "rows": self.rows,
            "expected_rows": self.expected_rows,
            "coverage_pct": self.coverage_pct,
            "gaps": [g.to_dict() for g in self.gaps],
            "duplicates": self.duplicates,
            "disorder": self.disorder,
            "spikes": self.spikes,
            "spread_anomalies": self.spread_anomalies,
            "notes": list(self.notes),
        }

    def render_markdown(self) -> str:
        counts: dict[str, int] = {}
        for g in self.gaps:
            counts[g.kind] = counts.get(g.kind, 0) + 1
        lines = [f"# 质检报告：{self.symbol or '(未命名)'} {self.timeframe}", ""]
        if self.first_time is None:
            lines.append("- 空数据")
        else:
            lines.append(f"- 区间：{self.first_time.isoformat()} → "
                         f"{self.last_time.isoformat()}")
            lines.append(f"- 行数：{self.rows}（期望 {self.expected_rows}，"
                         f"覆盖率 {self.coverage_pct:.2f}%）")
        lines.append(f"- 重复时间戳：{self.duplicates}；乱序：{self.disorder}")
        lines.append("- 缺口：weekend={0}，daily_break={1}，holiday={2}，"
                     "unexpected={3}".format(
                         counts.get("weekend", 0), counts.get("daily_break", 0),
                         counts.get("holiday", 0), counts.get("unexpected", 0)))
        lines.append(f"- Spike：{len(self.spikes)}；点差异常：{len(self.spread_anomalies)}")
        for g in self.gaps:
            if g.kind == "unexpected":
                lines.append(f"  - unexpected 缺口：{g.start.isoformat()} → "
                             f"{g.end.isoformat()}（缺 {g.bars_missing} 根）")
        for a in self.spikes:
            lines.append(f"  - spike：{a['time']} 幅度={a['range']:.6g}"
                         f"（邻域中位={a['neighbor_median']:.6g}，"
                         f"回撤={a['retrace']:.6g}）")
        for a in self.spread_anomalies:
            lines.append(f"  - 点差异常：{a['time']} spread={a['spread']}（{a['reason']}）")
        if self.notes:
            lines.append("- 备注：" + "；".join(self.notes))
        return "\n".join(lines) + "\n"


def _neighbor_medians(rng: np.ndarray, w: int) -> np.ndarray:
    """每根 bar 前后各 w 根（不含自身）|close-open| 的中位数。

    边缘不足 w 根时用现有邻域；空邻域返回 NaN（后续比较自然为 False）。
    """
    n = len(rng)
    padded = np.full(n + 2 * w, np.nan)
    padded[w:w + n] = rng
    win = np.lib.stride_tricks.sliding_window_view(padded, 2 * w + 1)
    neighbors = np.concatenate([win[:, :w], win[:, w + 1:]], axis=1)  # 去掉中位
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # 全 NaN 切片
        return np.nanmedian(neighbors, axis=1)


def quality_report(df: pd.DataFrame | None, timeframe: str, *,
                   symbol: str = "", tz_name: str = NY_TZ) -> QualityReport:
    """对单序列做全面体检。只读 df，任何异常只进报告，不修改不删除。"""
    tf = timeframe.upper()
    if tf not in TIMEFRAME_SECONDS:
        raise ValueError(f"未知周期 {timeframe!r}")
    seconds = TIMEFRAME_SECONDS[tf]
    period = timedelta(seconds=seconds)

    if df is None or len(df) == 0:
        return QualityReport(symbol=symbol, timeframe=tf, first_time=None,
                             last_time=None, rows=0, expected_rows=0,
                             coverage_pct=0.0, notes=["空数据"])

    times = pd.to_datetime(df["time"])
    rows = int(len(df))

    duplicates = int(rows - times.nunique())
    disorder = int((times.diff().dropna() < pd.Timedelta(0)).sum())
    notes: list[str] = []
    if duplicates:
        notes.append(f"重复时间戳 {duplicates} 根（应恒为 0）")
    if disorder:
        notes.append(f"时间乱序 {disorder} 处（应恒为 0）")

    first_time = times.iloc[0].to_pydatetime()
    last_time = times.iloc[-1].to_pydatetime()
    expected_rows = int((last_time - first_time).total_seconds() // seconds) + 1
    coverage_pct = rows / expected_rows * 100.0 if expected_rows else 0.0

    # ---- 缺口 ----
    t = times.to_numpy(dtype="datetime64[ns]")
    dt = np.diff(t)
    gap_pos = np.nonzero(dt > np.timedelta64(seconds, "s"))[0]
    weekend_gaps: list[Gap] = []
    pending: list[tuple[datetime, datetime, int]] = []  # (start, end, missing)
    for i in gap_pos:
        t_prev = pd.Timestamp(t[i]).to_pydatetime()
        t_next = pd.Timestamp(t[i + 1]).to_pydatetime()
        missing = int((t_next - t_prev).total_seconds() // seconds) - 1
        start, end = t_prev + period, t_next
        if gap_is_weekend(t_prev, t_next, tz_name=tz_name):
            weekend_gaps.append(Gap("weekend", start, end, missing))
        else:
            pending.append((start, end, missing))

    trading_days = times.dt.date.nunique()
    hour_days: dict[int, set] = {}
    for start, _, _ in pending:
        hour_days.setdefault(start.hour, set()).add(start.date())
    other_gaps: list[Gap] = []
    for start, end, missing in pending:
        if len(hour_days[start.hour]) * _DAILY_BREAK_DEN \
                >= trading_days * _DAILY_BREAK_NUM:
            kind = "daily_break"
        elif (end - start) >= timedelta(hours=_HOLIDAY_HOURS):
            kind = "holiday"
        else:
            kind = "unexpected"
        other_gaps.append(Gap(kind, start, end, missing))
    gaps = weekend_gaps + other_gaps

    # ---- spike：|close-open| > 20×前后各 20 根中位数，且下一根反向回撤过半 ----
    signed = (df["close"].astype("float64") - df["open"].astype("float64")).to_numpy()
    rng = np.abs(signed)
    med = _neighbor_medians(rng, _SPIKE_WINDOW)
    spikes: list[dict] = []
    with np.errstate(invalid="ignore"):
        candidates = np.nonzero(rng[:-1] > _SPIKE_MULT * med[:-1])[0]
    for i in candidates:
        nxt = signed[i + 1]
        if signed[i] == 0 or nxt == 0 or (signed[i] > 0) == (nxt > 0):
            continue  # 需下一根反向走且幅度过半，才像数据毛刺而非趋势突破
        if abs(nxt) > _SPIKE_RETRACE * rng[i]:
            spikes.append({
                "time": pd.Timestamp(t[i]).isoformat(),
                "open": float(df["open"].iloc[i]),
                "close": float(df["close"].iloc[i]),
                "range": float(rng[i]),
                "neighbor_median": float(med[i]),
                "retrace": float(abs(nxt)),
            })

    # ---- 点差异常：<=0 或 > p99×10 ----
    sp = df["spread"]
    p99 = float(sp.quantile(0.99))
    spread_anomalies: list[dict] = []
    bad = np.nonzero((sp.to_numpy() <= 0) | (sp.to_numpy() > _SPREAD_P99_MULT * p99))[0]
    for i in bad:
        s = int(sp.iloc[i])
        reason = "spread<=0" if s <= 0 else "spread>p99*10"
        spread_anomalies.append({"time": pd.Timestamp(t[i]).isoformat(),
                                 "spread": s, "reason": reason})

    return QualityReport(
        symbol=symbol, timeframe=tf, first_time=first_time, last_time=last_time,
        rows=rows, expected_rows=expected_rows, coverage_pct=coverage_pct,
        gaps=gaps, duplicates=duplicates, disorder=disorder, spikes=spikes,
        spread_anomalies=spread_anomalies, notes=notes,
    )
