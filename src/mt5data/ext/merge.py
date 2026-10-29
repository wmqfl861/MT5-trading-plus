"""T97/M1：研究用合并序列——MT5 主库 + ext 前向延长（P 线数据解锁批）。

合并语义（docs/superpowers/plans/2026-10-29-p-line-t97-data-unlock.md v2
先决 1/4）：

- **前向延长 + MT5 为准**：ext 只取 MT5 首根 bar（first_time，按数据
  实测而非水位）之前的 bar；相同 time 的重复 bar 以 MT5 为准
  （``concat_mt5_priority``）。正常路径下前向延长过滤已消除重叠，
  去重是防御性安全网（去重证明入测试）。
- **裁 research_cutoff（有效上界 = 两者更早者）**：ext manifest 的
  ``research_cutoff`` 字段是铁律 19 的**严格**上界（bar.time < cutoff；
  真实库 data_research/ext/histdata/XAUUSD/H1/manifest.json 为
  ``research_cutoff``）；MT5 水位 ``last_closed_time``
  （data_research/bars/XAUUSD/H1/meta.json）语义是最后一根**已收盘**
  bar，故为**含端点**上界（bar.time <= last_closed）。两条约束同时
  施加于合并序列，实际生效的是更早者（真实库两者同界：
  research_cutoff = last_closed + 1h）。
- **统一有效上界（T98/P-01）**：合并后对**全序列**统一施加有效上界
  （各来源带各自的比较语义、同时施加即取最严）：全局锁箱 cutoff
  （``mt5data.lockbox.load_config``+``cutoff_for``——data_root 下没有
  lockbox.json 则无此界；研究导出库既有设计不带锁箱文件，H-01 明确
  **不**要求必须有）、ext manifest 的 ``research_cutoff``（严格 <；
  ext 数据在库却缺该字段 → ValueError 保守拒绝——主库 ext 未经导出
  盖章即此形态）、导出 meta ``source_cutoff``（严格 <）、MT5 水位
  ``last_closed_time``（含端点）、显式 ``end``（含端点；只收紧不放宽，
  比其他界晚则被支配，绝不借 end 读出锁箱段）。多个已批准 ext 来源
  同时提供同一序列 → 来源冲突，保守拒绝（不按字母序静默选边）。
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
from mt5data.lockbox import cutoff_for, load_config
from mt5data.store import _COLUMNS, load_bars, read_watermark

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

def _meta_bound(meta, key):
    """水位 meta 的时间字段 → Timestamp；缺/空 → None；不可解析 → ValueError
    （来源 meta 不明，保守拒绝）。"""
    raw = (meta or {}).get(key)
    if not raw:
        return None
    try:
        return pd.Timestamp(raw)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"水位字段 {key} 不可解析（来源 meta 不明，保守拒绝）：{raw!r}"
        ) from e


def _effective_upper_bounds(root, symbol: str, timeframe: str,
                            meta, manifest, end):
    """收集合并后全序列的有效上界（T98/P-01），返回 [(标签, 时刻, 严格?)]。

    各来源带着各自的比较语义同时施加即"取最严"：

    - 全局锁箱 cutoff（mt5data.lockbox.load_config + cutoff_for）：data_root
      下没有 lockbox.json → 无此界（研究导出库既有设计不带锁箱文件，
      H-01：不要求必须有，缺文件不是拒绝理由）；
    - ext manifest research_cutoff（铁律 19，严格 <）：ext 数据在库却缺
      该字段 → ValueError（来源上界不明，保守拒绝——主库 ext 未经
      export_research_store 盖章即此形态）；
    - 导出 meta source_cutoff（export_research_store 落盘，导出段
      time < cutoff，严格 <）；
    - MT5 水位 last_closed_time（最后一根已收盘 bar，含端点）；
    - 显式 end（含端点，与 scripts.run_backtest.load_series 同语义）：
      永不报"越过锁箱"——end 只会收紧合并上界，比其他界晚则被支配。
    """
    bounds = []
    lb_cut = cutoff_for(load_config(str(root)), symbol, timeframe)
    if lb_cut is not None:
        bounds.append(("全局锁箱 cutoff", pd.Timestamp(lb_cut), True))
    if manifest is not None:
        raw = manifest.get("research_cutoff")
        if not raw:
            raise ValueError(
                f"ext 序列 {symbol}/{timeframe.upper()} 的 manifest 缺 "
                "research_cutoff——ext 来源上界不明，保守拒绝（T98/P-01；"
                "主库 ext 未经过导出盖章，请改用研究导出库或补齐 manifest）")
        try:
            bounds.append(("ext manifest research_cutoff",
                           pd.Timestamp(raw), True))
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"ext manifest research_cutoff 不可解析（保守拒绝）：{raw!r}"
            ) from e
    source_cutoff = _meta_bound(meta, "source_cutoff")
    if source_cutoff is not None:
        bounds.append(("导出 source_cutoff", source_cutoff, True))
    last_closed = _meta_bound(meta, "last_closed_time")
    if last_closed is not None:
        bounds.append(("MT5 水位 last_closed_time", last_closed, False))
    if end is not None:
        try:
            end_ts = pd.Timestamp(end)
        except (TypeError, ValueError) as e:
            raise ValueError(f"end 不可解析（保守拒绝）：{end!r}") from e
        bounds.append(("显式 end", end_ts, False))
    return bounds


def _apply_upper_bounds(merged: pd.DataFrame, bounds) -> pd.DataFrame:
    """对合并后全序列逐条施加上界（各自语义：True=严格 <，False=含端点）。"""
    for _label, ts, strict in bounds:
        merged = (merged[merged["time"] < ts] if strict
                  else merged[merged["time"] <= ts])
    return merged.reset_index(drop=True)


def merged_research_series(data_root, symbol: str, timeframe: str, *,
                           end=None):
    """合并 MT5 主库与 ext 前向延长，返回研究用四元组。

    布局：``{data_root}/bars/{symbol}/{tf}``（MT5，mt5data.store）与
    ``{data_root}/ext/<来源>/<品种>/<周期>/``（mt5data.ext.core）。
    全部语义（前向延长 / cutoff 口径 / 年份闸门 / 来源优先级 / 周日
    自检 / gap 口径）见模块 docstring。

    end（T98/P-01，pd.Timestamp 可解析物或 None）：显式上界（含端点），
    在合并后与其他上界（全局锁箱 cutoff / ext manifest research_cutoff /
    导出 source_cutoff / MT5 水位）一起取最严——end 比其他界晚则被支配，
    绝不因 end 越过锁箱而读出锁箱段。

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

    mt5 = load_bars(root, symbol, tf)
    meta = read_watermark(root, symbol, tf) or {}
    source, ext_df, manifest = _load_ext_segment(root, symbol, tf)

    research_cutoff = None
    if manifest and manifest.get("research_cutoff"):
        research_cutoff = pd.Timestamp(manifest["research_cutoff"])
    last_closed = None
    if meta.get("last_closed_time"):
        last_closed = pd.Timestamp(meta["last_closed_time"])

    def _clip(df: pd.DataFrame) -> pd.DataFrame:
        # ext 段：铁律 19 严格 < research_cutoff；MT5 段：另受水位
        # last_closed_time 含端点约束。两条都施加，生效的是更早者。
        # （合并前的分段防线；合并后另有统一上界，见 _apply_upper_bounds。）
        if research_cutoff is not None:
            df = df[df["time"] < research_cutoff]
        if last_closed is not None:
            df = df[df["time"] <= last_closed]
        return df

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
        ext_part = _clip(ext_part).reset_index(drop=True)

    mt5_part = None
    if mt5 is not None and len(mt5):
        mt5_part = _clip(mt5).reset_index(drop=True)

    merged = concat_mt5_priority(mt5_part, ext_part)

    # T98/P-01：合并后全序列统一校验有效上界（取最严）——全局锁箱 cutoff
    # / ext manifest research_cutoff / 导出 source_cutoff / 水位 / end。
    # ext 数据在库却缺 research_cutoff、多个批准来源撞同一序列 → 上面
    # _load_ext_segment / _effective_upper_bounds 已保守拒绝。
    merged = _apply_upper_bounds(
        merged, _effective_upper_bounds(root, symbol, tf, meta, manifest, end))

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
