"""K 线质检：只读输入 DataFrame，只报告，绝不改数、不删数。

缺口分类次序（前者优先）：
1. weekend——与周末窗口**基本重合**（T79/R12-G1 收紧）：恰覆盖一个窗口，
   且缺口起点不早于周五收盘前一根 bar、终点不晚于周日开盘后一根 bar
   （容差按周期换算；DST 感知，非"恒 48 小时"）；
2. long_gap——覆盖 ≥2 个周末窗口的跨多周长缺口（T79 新类别；旧判定把
   这类整段归 weekend 而从不报警——真实案例：EURUSD 2021-05/06 的
   27 天缺口直到外部数据比对才被发现）；
3. daily_break——缺失起始的交易所本地小时（DST 换算后聚合）在 ≥60% 的
   交易日重复出现（按本地小时分桶：固定的本地休市跨夏令时会落在不同
   UTC 标签小时，按标签分桶会被拆裂而漏判）；
4. holiday——缺口 ≥20 小时且不跨周末；
5. micro_gap——仅缺 ≤2 根的微型缺口（换日/静默时段散落的单分钟掉数据，
   逐条排查无意义，计数即可；真实案例：Exness EURUSD M1 五年 1 万余处）；
6. unexpected——其余，需人工排查。

与周末窗相交但超出容差的缺口（如周五提前收盘 2 根以上、周日晚开 2 根
以上、单边节假日并周末）走 3–6 分类——可见而非静默并入 weekend。

known_gaps（T79）：终端补不了的长缺口登记进序列 meta.json（write_known_gaps），
研究工具（按年分段、walk-forward；事件研究待 T66）经 read_known_gaps 读取，
在报告里注明相关窗口样本不完整。

注意：出现次数不足 _MIN_RECUR 的本地小时用全局交易日做分母（防单日样本
1/1=100% 的假阳性）；达到 _MIN_RECUR 的用"首末出现日窗口"做分母——常规
休市可能起始于历史某时刻，且其周五那次会先被归入 weekend，全局分母会
系统性低估频率（真实案例：Exness 2017 年起的 NY17:00 周一至周四维护窗，
全局分母下只有 48%）。
"""
from __future__ import annotations

import json
import os
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from mt5data.sessions import (
    NY_TZ, gap_is_weekend, overlapped_weekend_windows,
)
from mt5data.store import TIMEFRAME_SECONDS, read_watermark, series_dir

_HOLIDAY_HOURS = 20.0
_DAILY_BREAK_NUM, _DAILY_BREAK_DEN = 6, 10  # ≥60%，整数比较免浮点误差
_SPIKE_MULT = 20.0
_SPIKE_WINDOW = 20
_SPIKE_RETRACE = 0.5
_SPREAD_P99_MULT = 10.0
_MAX_SAMPLES = 100  # 异常明细最多列出条数（其余只计数+直方图）
_MIN_RECUR = 10     # 窗口分母的最低出现次数（低于则用全局分母）
_MICRO_GAP_BARS = 2  # ≤ 该缺失根数且不属常规休市 → micro_gap
_PRICE_FIELDS = ("open", "high", "low", "close")


@dataclass
class Gap:
    """一处缺口。start=首根缺失 bar 的 time；end=缺口后首根现存 bar 的 time。"""

    kind: str  # weekend / long_gap / daily_break / holiday / micro_gap / unexpected
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
    spread_anomalies: list[dict] = field(default_factory=list)  # 样例（≤ _MAX_SAMPLES）
    spread_anomaly_count: int = 0
    spread_anomaly_by_hour: dict[int, int] = field(default_factory=dict)
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
            "spread_anomaly_count": self.spread_anomaly_count,
            "spread_anomaly_by_hour": dict(self.spread_anomaly_by_hour),
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
        lines.append("- 缺口：weekend={0}，long_gap={1}，daily_break={2}，"
                     "holiday={3}，micro_gap={4}，unexpected={5}".format(
                         counts.get("weekend", 0), counts.get("long_gap", 0),
                         counts.get("daily_break", 0),
                         counts.get("holiday", 0), counts.get("micro_gap", 0),
                         counts.get("unexpected", 0)))
        lines.append(f"- Spike：{len(self.spikes)}；点差异常：{self.spread_anomaly_count}")
        if self.spread_anomaly_by_hour:
            top = sorted(self.spread_anomaly_by_hour.items(),
                         key=lambda kv: -kv[1])[:3]
            hist = "、".join(f"{h:02d}:00×{n}" for h, n in top)
            lines.append(f"- 点差异常小时分布（前3）：{hist}")
        for g in self.gaps:
            if g.kind in ("unexpected", "long_gap"):
                lines.append(f"  - {g.kind} 缺口：{g.start.isoformat()} → "
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
    long_gaps: list[Gap] = []   # T79：跨多周末的长缺口（覆盖 ≥2 个周末窗）
    pending: list[tuple[datetime, datetime, int]] = []  # 与周末窗零相交
    # 与周末窗相交但超出容差（周末+额外缺失）：按长度归类，**不进日间休市
    # 分桶**——它们的首末出现日会拉宽重现率分母（T79 实测把 USTEC H1 的
    # 384 处日维护窗从 daily_break 挤成 micro_gap），且周末相邻缺口本来
    # 就不是纯日间休市形态
    weekend_adjacent: list[tuple[datetime, datetime, int]] = []
    for i in gap_pos:
        t_prev = pd.Timestamp(t[i]).to_pydatetime()
        t_next = pd.Timestamp(t[i + 1]).to_pydatetime()
        missing = int((t_next - t_prev).total_seconds() // seconds) - 1
        start, end = t_prev + period, t_next
        n_windows = len(overlapped_weekend_windows(t_prev, t_next, tz_name))
        if n_windows >= 2:
            long_gaps.append(Gap("long_gap", start, end, missing))
        elif n_windows == 1 and gap_is_weekend(t_prev, t_next, tz_name,
                                               bar=period):
            weekend_gaps.append(Gap("weekend", start, end, missing))
        elif n_windows == 1:
            weekend_adjacent.append((start, end, missing))
        else:
            pending.append((start, end, missing))

    all_dates = set(times.dt.date)
    trading_days = len(all_dates)
    tz = ZoneInfo(tz_name)

    def _local_hour(ts: datetime) -> int:
        # 交易所本地小时（见模块 docstring 第 2 条的分桶理由）
        return ts.replace(tzinfo=timezone.utc).astimezone(tz).hour

    hour_days: dict[int, set] = {}
    for start, _, _ in pending:
        hour_days.setdefault(_local_hour(start), set()).add(start.date())

    def _is_recurrent(days_h: set) -> bool:
        """该本地小时是否构成常规日间休市。

        分母：出现 ≥ _MIN_RECUR 次的，用"首末出现日窗口"内的交易日数——
        常规休市可能起始于历史某时刻（Exness 2017 年起的 NY17:00 维护窗），
        且其周五那次会先被归入 weekend，全局分母会系统性低估频率。
        零星缺口仍用全局分母（防单日样本 1/1=100% 的假阳性）。
        """
        if len(days_h) >= _MIN_RECUR:
            lo, hi = min(days_h), max(days_h)
            denom = sum(1 for d in all_dates if lo <= d <= hi)
        else:
            denom = trading_days
        return len(days_h) * _DAILY_BREAK_DEN >= denom * _DAILY_BREAK_NUM

    other_gaps: list[Gap] = []
    for start, end, missing in pending:
        if _is_recurrent(hour_days[_local_hour(start)]):
            kind = "daily_break"
        elif (end - start) >= timedelta(hours=_HOLIDAY_HOURS):
            kind = "holiday"
        elif missing <= _MICRO_GAP_BARS:
            kind = "micro_gap"
        else:
            kind = "unexpected"
        other_gaps.append(Gap(kind, start, end, missing))
    for start, end, missing in weekend_adjacent:
        # 跳过 daily_break（理由见上方分桶注释）：≥20h 归 holiday（周末+
        # 节假日提前收盘/晚开），≤2 根归 micro_gap，其余 unexpected
        if (end - start) >= timedelta(hours=_HOLIDAY_HOURS):
            kind = "holiday"
        elif missing <= _MICRO_GAP_BARS:
            kind = "micro_gap"
        else:
            kind = "unexpected"
        other_gaps.append(Gap(kind, start, end, missing))
    gaps = weekend_gaps + long_gaps + other_gaps

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
    spv = sp.to_numpy()
    if p99 > 0:
        bad = np.nonzero((spv <= 0) | (spv > _SPREAD_P99_MULT * p99))[0]
    else:
        # p99=0（如 Exness 部分品种 bar 记录的最小点差为 0）：>p99×10 会把
        # 所有 spread>0 的 bar 全判异常——退化时只按 spread<=0 计
        bad = np.nonzero(spv <= 0)[0]
        notes.append("spread p99=0（该序列 bar 记录的最小点差为 0），"
                     "「>p99×10」判定退化，仅按 spread<=0 计数")
    spread_count = int(bad.size)
    spread_by_hour: dict[int, int] = {}
    for i in bad:
        h = int(pd.Timestamp(t[i]).hour)
        spread_by_hour[h] = spread_by_hour.get(h, 0) + 1
    spread_anomalies: list[dict] = []
    for i in bad[:_MAX_SAMPLES]:
        s = int(sp.iloc[i])
        reason = "spread<=0" if s <= 0 else "spread>p99*10"
        spread_anomalies.append({"time": pd.Timestamp(t[i]).isoformat(),
                                 "spread": s, "reason": reason})
    if spread_count > _MAX_SAMPLES:
        notes.append(f"点差异常共 {spread_count} 处，明细仅列前 {_MAX_SAMPLES} 条"
                     "（全量见 spread_anomaly_count 与 spread_anomaly_by_hour）")

    if len(spikes) > _MAX_SAMPLES:
        notes.append(f"spike 共 {len(spikes)} 处，明细仅列前 {_MAX_SAMPLES} 条")
        spikes = spikes[:_MAX_SAMPLES]

    return QualityReport(
        symbol=symbol, timeframe=tf, first_time=first_time, last_time=last_time,
        rows=rows, expected_rows=expected_rows, coverage_pct=coverage_pct,
        gaps=gaps, duplicates=duplicates, disorder=disorder, spikes=spikes,
        spread_anomalies=spread_anomalies, spread_anomaly_count=spread_count,
        spread_anomaly_by_hour=spread_by_hour, notes=notes,
    )


# ------------------------------------------------ T79：known_gaps 登记与读取

def _known_gap_entry(raw) -> tuple[datetime, datetime]:
    """校验一条 known_gaps 记录，返回 (start, end)。非法即 ValueError。"""
    if not isinstance(raw, dict) or "start" not in raw or "end" not in raw:
        raise ValueError(
            f"known_gaps 条目须为含 start/end 的对象，收到 {raw!r}")
    try:
        s = datetime.fromisoformat(str(raw["start"]))
        e = datetime.fromisoformat(str(raw["end"]))
    except ValueError as exc:
        raise ValueError(f"known_gaps start/end 须为 ISO 时间：{raw!r}") from exc
    if not s < e:
        raise ValueError(f"known_gaps 条目 start 须早于 end：{raw!r}")
    return s, e


def read_known_gaps(root, symbol: str,
                    timeframe: str) -> list[tuple[datetime, datetime]]:
    """读序列 meta.json 的 known_gaps（普查登记、终端补不了的已知缺口）。

    返回 [(start, end), ...]（与 Gap 口径一致：start=首根缺失 bar、
    end=缺口后首根现存 bar）；无文件/无键 → []。条目格式非法 → ValueError：
    已知缺口是研究工具注记样本完整性的依据，宁可拒读也不静默丢弃。
    """
    meta = read_watermark(root, symbol, timeframe)
    if not meta:
        return []
    raw = meta.get("known_gaps")
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"known_gaps 须为数组，收到 {type(raw).__name__}")
    return [_known_gap_entry(item) for item in raw]


def write_known_gaps(root, symbol: str, timeframe: str,
                     entries: list[dict]) -> None:
    """把已知缺口登记进序列 meta.json（原子写；保留其余键；同 start 去重替换）。

    entries 每条 {"start": iso, "end": iso, "reason": str}。整表先校验后
    写入（一条非法全部拒绝，不留半截登记）。离线运维操作（scripts/scan_gaps
    --register-known），不走跨进程序列锁——不与下载/导出并发运行。
    """
    if not entries:
        raise ValueError("known_gaps 登记条目不能为空")
    validated = [(_known_gap_entry(e), e) for e in entries]
    d = series_dir(root, symbol, timeframe)
    path = os.path.join(d, "meta.json")
    meta: dict = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            meta = json.load(f)
    by_start = {item["start"]: item
                for item in meta.get("known_gaps", [])
                if isinstance(item, dict) and "start" in item}
    for (s, e), raw in validated:
        by_start[raw["start"]] = {
            "start": raw["start"], "end": raw["end"],
            "reason": str(raw.get("reason", "")),
            "registered_utc": datetime.now(timezone.utc).isoformat(),
        }
    meta["known_gaps"] = sorted(by_start.values(), key=lambda x: x["start"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
