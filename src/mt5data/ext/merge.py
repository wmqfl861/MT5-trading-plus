"""T97/M1：研究用合并序列——MT5 主库 + ext 前向延长（P 线数据解锁批）。

合并语义（docs/superpowers/plans/2026-10-29-p-line-t97-data-unlock.md v2
先决 1/4）：

- **前向延长 + MT5 为准**：ext 只取 MT5 首根 bar（first_time，按数据
  实测而非水位）之前的 bar；相同 time 的重复 bar 以 MT5 为准
  （``concat_mt5_priority``）。正常路径下前向延长过滤已消除重叠，
  去重是防御性安全网（去重证明入测试）。
- **边界（X-03 唯一契约，直接消费 lockbox.effective_research_bound）**：
  ext manifest 的 ``research_cutoff`` 是铁律 19 的声明**严格**上界
  （bar.time < cutoff）；MT5 水位 ``last_closed_time`` 语义是最后一根
  **已收盘** bar（含端点）。统一契约会把声明上界 / last_closed+1 周期 /
  end+1µs 换算为一个严格上界一次性施加于合并序列（网格数据上与旧的
  "ext 严格 <、MT5 含端点、取更早者"逐位一致；真实库两者同界：
  research_cutoff = last_closed + 1h）。
- **统一有效上界（T98/P-01 → X-03 唯一契约）**：合并读取**直接消费**
  ``mt5data.lockbox.effective_research_bound``，本模块不再独立收集/取最严
  （旧 ``_effective_upper_bounds``/``_apply_upper_bounds`` 已删）。统一
  语义：全局锁箱 cutoff、ext manifest ``research_cutoff``（ext 数据在库
  却缺该字段 → LockboxError 保守拒绝——主库 ext 未经导出盖章即此形态）、
  导出 meta ``source_cutoff``——三者是**声明上界**，不一致即冲突
  fail-closed（不再各自施加取最严）；显式 ``end``（含端点）越过声明上界
  → LockboxError（与 research_bars 同一结论，不再"永不报越界"）；MT5
  水位 ``last_closed_time``（含端点）与声明/end 换算为严格上界
  （time < strict）一次性施加于合并后全序列。
  X-03：裸库（有数据、无任何声明边界）→ LockboxError（"未初始化"）；
  ``allow_unbounded=True`` 为显式测试/迁移通道。
- **X-04 一致性读**：MT5 段取数走 ``store.load_bars_consistent``
  （内容指纹校验）——撕裂快照明确拒绝，不配读混版。
- **ext 年份闸门（T94/R14）**：只取 manifest.yearly_verification 里
  ``used == true`` 的自然年段；used=false 与未列年份都不进。
- **来源优先级**：批准来源（approvals.SOURCE_APPROVALS /
  approved_sources）按字母序先命中即用。当前实际提供 K 线的只有
  histdata 一个 H1 来源（fred/cdd 为 D1 指数/加密日线，对 H1 品种
  自然落空；stooq/yahoo 不批准）——多来源竞争路径尚未出现，按字母序
  简单实现。
- **月度周日开盘小时自检（先决 4，仅 ext 段；MT5 段豁免——实证其
  周日首 bar 多为 0 点属券商时段结构）**：对每个自然月 × 来源段，收集
  该月内每个周日（weekday==6）第一根 bar 的 UTC 小时；不含美国 DST
  切换的月（该月内 America/New_York 的 utcoffset 发生变化与否，用
  zoneinfo 判）应唯一；含切换的月允许两个相邻小时（|差|恰为 1，不取
  模跨午夜）。违例月记入 bad_months、从序列剔除，剔除块的两端
  （前末 bar、后首 bar）记入 gap_spans。
- **gap_spans**：相邻 bar 时间差 > ``MAX_GAP_DAYS``（30 天）的对
  (前末 bar 时刻, 后首 bar 时刻)，恰 30 天不算——与
  mt5quant.research.candidates._GAP_SPLIT 口径一致（测试直接对拍）。
"""
from __future__ import annotations

import calendar
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from mt5data.ext.approvals import approved_sources
from mt5data.ext.core import load_ext_series
from mt5data.lockbox import effective_research_bound
from mt5data.store import _COLUMNS, load_bars_consistent

# 与 mt5quant.research.candidates._GAP_SPLIT 同口径（T94/R14-L3）：
# 长于 30 天的内部空洞才算（恰 30 天不算）。
MAX_GAP_DAYS = 30
_GAP_SPLIT = pd.Timedelta(days=MAX_GAP_DAYS)

_SUNDAY = 6  # pandas weekday：Mon=0 … Sun=6
_NY_TZ = ZoneInfo("America/New_York")


# ---------------------------------------------------------------- 去重拼接

def concat_mt5_priority(mt5_df, ext_df) -> pd.DataFrame:
    """按 time 排序去重：相同 time 的 bar 以 MT5 行为准（先决 1）。

    实现上给 MT5 行更高优先级后按 (time, 优先级) 稳定排序、同刻取首行。
    空输入安全：全空时返回带标准列的空 DataFrame。
    """
    parts = []
    for prio, df in ((0, mt5_df), (1, ext_df)):
        if df is not None and len(df):
            d = df[_COLUMNS].copy()
            d["_prio"] = prio
            parts.append(d)
    if not parts:
        return pd.DataFrame(columns=list(_COLUMNS))
    merged = pd.concat(parts, ignore_index=True)
    merged = (merged.sort_values(["time", "_prio"], kind="stable")
              .drop_duplicates(subset="time", keep="first")
              .drop(columns="_prio")
              .sort_values("time")
              .reset_index(drop=True))
    return merged[_COLUMNS]


# ---------------------------------------------------------------- ext 装载

def _load_ext_segment(data_root, symbol: str, timeframe: str):
    """已批准来源按字母序探测（来源优先级，见模块 docstring）。

    多个已批准来源同时提供同一序列 → 来源 meta 冲突，保守拒绝
    （T98/P-01：多来源竞争路径尚未定义，不按字母序静默选边）。
    返回 (source, df, manifest)；无可用序列 → (None, None, None)。
    manifest 缺字段/与数据不一致由 load_ext_series 读取侧 fail-fast。
    """
    ext_root = os.path.join(str(data_root), "ext")
    hits = []
    for source in approved_sources():
        loaded = load_ext_series(ext_root, source, symbol, timeframe)
        if loaded is not None:
            hits.append((source, loaded[0], loaded[1]))
    if len(hits) > 1:
        raise ValueError(
            f"来源冲突：{symbol}/{timeframe.upper()} 同时有多个已批准 ext "
            f"来源提供数据（{', '.join(s for s, _, _ in hits)}）——多来源"
            "竞争的取数语义尚未定义，保守拒绝（T98/P-01；请清理 ext 库，"
            "同一序列只保留一个来源）")
    if not hits:
        return None, None, None
    return hits[0]


# ---------------------------------------------------------------- 周日自检

def _month_has_us_dst_transition(year: int, month: int) -> bool:
    """该自然月内 America/New_York 的 utcoffset 是否发生变化（zoneinfo 判）。"""
    ndays = calendar.monthrange(year, month)[1]
    offs = [_NY_TZ.utcoffset(datetime(year, month, d))
            for d in range(1, ndays + 1)]
    return any(offs[i] != offs[i + 1] for i in range(ndays - 1))


def _sunday_open_hours(df: pd.DataFrame) -> set[int]:
    """该段数据里每个周日（weekday==6）第一根 bar 的 UTC 小时集合。"""
    if not len(df):
        return set()
    sun = df[df["time"].dt.dayofweek == _SUNDAY]
    if not len(sun):
        return set()
    firsts = sun.groupby(sun["time"].dt.normalize())["time"].min()
    return {int(h) for h in firsts.dt.hour}


def _month_sunday_hours_ok(year: int, month: int, hours: set[int]) -> bool:
    """先决 4 口径：不含美国 DST 切换的月应唯一；含切换的月允许两个
    相邻小时（其余情形——双值不相邻、三个及以上——一律违例）。"""
    if len(hours) <= 1:
        return True
    if (len(hours) == 2 and max(hours) - min(hours) == 1
            and _month_has_us_dst_transition(year, month)):
        return True
    return False


def _observed_gaps(times) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """>30 天的相邻间隔 → (末根现存 bar, 空洞后首根 bar)。

    与 candidates._observed_gaps 同法：pandas Timedelta 比较（单位安全），
    恰 30 天不算（>，不含等号）。
    """
    t = pd.DatetimeIndex(times)
    if len(t) < 2:
        return []
    diffs = pd.Series(t).diff()
    idx = np.nonzero((diffs > _GAP_SPLIT).to_numpy())[0]
    return [(t[i - 1], t[i]) for i in idx]


# ---------------------------------------------------------------- 主管线

def merged_research_series(data_root, symbol: str, timeframe: str, *,
                           end=None, allow_unbounded: bool = False):
    """合并 MT5 主库与 ext 前向延长，返回研究用四元组。

    布局：``{data_root}/bars/{symbol}/{tf}``（MT5，mt5data.store）与
    ``{data_root}/ext/<来源>/<品种>/<周期>/``（mt5data.ext.core）。
    全部语义（前向延长 / cutoff 口径 / 年份闸门 / 来源优先级 / 周日
    自检 / gap 口径）见模块 docstring。

    X-03：有效上界**唯一**由 ``mt5data.lockbox.effective_research_bound``
    决定（本函数直接消费，不再独立取最严）：声明上界冲突 → LockboxError；
    显式 end 越过声明上界 → LockboxError（含端点语义与 research_bars 一致：
    end == 上界合法、结果仍严格 < 上界）；裸库（有数据、无任何声明边界）
    → LockboxError（"未初始化"）。``allow_unbounded=True`` 为显式测试/
    迁移通道（保持旧的裸库合并行为）。

    X-04：MT5 段取数走 ``store.load_bars_consistent``——撕裂快照
    （发布中断 / 内容指纹不符）明确拒绝，绝不新 parquet 配旧 meta。

    返回 ``(bars, source_tags, gap_spans, bad_months)``：
    - bars：naive UTC 的仓库格式 DataFrame（store._COLUMNS），升序无重复；
    - source_tags：与 bars 等长，"mt5" 或 "ext:{来源}:{bar 年份}"；
    - gap_spans：[(前末 bar 时刻, 后首 bar 时刻)]，升序——自然洞（>30
      天相邻间隔）∪ 坏月剔除块两端（后者宽度通常也 >30 天，被前者
      覆盖；显式并入以保两端必记）；
    - bad_months：["YYYY-MM", ...]，周日小时自检违例的 ext 月（MT5 段
      豁免）。
    """
    root = str(data_root)
    tf = timeframe.upper()

    # 来源选择（多来源冲突保守拒绝）先于边界契约——多来源竞争的取数
    # 语义属来源选择问题，不是边界问题。
    source, ext_df, manifest = _load_ext_segment(root, symbol, tf)

    # X-03：边界唯一契约（冲突 / end 越界 / ext 未打戳 / 裸库未初始化
    # 均在此 LockboxError，与 research_bars 同一结论）。
    bound = effective_research_bound(root, symbol, tf, end=end,
                                     allow_unbounded=allow_unbounded)

    # X-04：主库段走一致性读（撕裂快照明确拒绝；显式通道且无 meta 的
    # 手写夹具退化为直读）。
    mt5 = load_bars_consistent(root, symbol, tf,
                               allow_unverified=allow_unbounded)

    used_years: set[int] = set()
    if manifest:
        yv = manifest.get("yearly_verification")
        if isinstance(yv, dict):
            for y, rec in yv.items():
                if isinstance(rec, dict) and rec.get("used") is True:
                    try:
                        used_years.add(int(str(y)))
                    except ValueError:
                        continue

    mt5_first = None
    if mt5 is not None and len(mt5):
        mt5_first = pd.Timestamp(mt5["time"].min())  # 按数据实测，不信水位

    ext_part = None
    if ext_df is not None and len(ext_df):
        ext_part = ext_df[ext_df["time"].dt.year.isin(used_years)]
        if mt5_first is not None:
            ext_part = ext_part[ext_part["time"] < mt5_first]
        ext_part = ext_part.reset_index(drop=True)

    mt5_part = None
    if mt5 is not None and len(mt5):
        mt5_part = mt5.reset_index(drop=True)

    merged = concat_mt5_priority(mt5_part, ext_part)

    # X-03：统一严格上界（声明上界 / last_closed+1 周期 / end+1µs 的
    # 最早者）一次性施加于合并后全序列——合法研究行一律 time < strict。
    if bound.strict is not None:
        merged = merged[merged["time"] < bound.strict].reset_index(drop=True)

    # source_tags：先登 ext 再被 MT5 覆盖（同刻冲突 MT5 为准）
    tag_by_time = {}
    if ext_part is not None and len(ext_part):
        for t, yr in zip(ext_part["time"], ext_part["time"].dt.year):
            tag_by_time[t] = f"ext:{source}:{yr}"
    if mt5_part is not None and len(mt5_part):
        for t in mt5_part["time"]:
            tag_by_time[t] = "mt5"
    source_tags = [tag_by_time[t] for t in merged["time"]]

    # ---- 月度周日开盘小时自检（仅 ext 段；按 自然月 × 来源段 分组）
    is_ext = np.asarray([t.startswith("ext:") for t in source_tags],
                        dtype=bool)
    bad_keys: set[tuple[int, int]] = set()
    ext_rows = merged[is_ext] if len(merged) else merged.iloc[0:0]
    if len(ext_rows):
        grouped = ext_rows.groupby([ext_rows["time"].dt.year,
                                    ext_rows["time"].dt.month])
        for (yy, mm), g in grouped:
            hours = _sunday_open_hours(g)
            if hours and not _month_sunday_hours_ok(int(yy), int(mm), hours):
                bad_keys.add((int(yy), int(mm)))

    times = list(merged["time"])
    n = len(times)
    year_month = [(t.year, t.month) for t in times]
    drop = is_ext & np.asarray([ym in bad_keys for ym in year_month],
                               dtype=bool)

    # 坏月剔除块的两端记入 gap（块贴序列头/尾时缺一端，不记）
    removal_gaps: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    i = 0
    while i < n:
        if drop[i]:
            j = i
            while j + 1 < n and drop[j + 1]:
                j += 1
            if i > 0 and j < n - 1:
                removal_gaps.append((times[i - 1], times[j + 1]))
            i = j + 1
        else:
            i += 1

    kept_times = [times[k] for k in range(n) if not drop[k]]
    gap_spans = sorted(set(_observed_gaps(kept_times)) | set(removal_gaps))
    bad_months = sorted(f"{y:04d}-{m:02d}" for (y, m) in bad_keys)

    if n:
        bars = merged.loc[~drop].reset_index(drop=True)[_COLUMNS]
    else:
        bars = merged
    tags_out = [source_tags[k] for k in range(n) if not drop[k]]
    return bars, tags_out, gap_spans, bad_months
