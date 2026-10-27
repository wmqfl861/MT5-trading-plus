"""外部数据 ↔ MT5 主库的一致性核对（T67/铁律 19）。

原则：**只写实测值，不预设阈值**——每个核对项返回数字与分布，判定
（"多相关才算一致"、"缺口多宽算异常"）留给报告与用户，不在代码里埋线。

MT5 侧 bar 时间是"服务器墙钟贴 UTC 标签"。本仓库券商的实测服务器
时区为 **UTC**（2026-10-27 核对：BTC D1 日期与 CDD/UTC 逐日对齐、
HistData 美东墙钟换算后 H1 逐月偏移全 0），故缺省 server_tz="UTC"；
EET 风格券商（冬 +2/夏 +3）用 server_tz="Europe/Athens" 等显式传入，
zoneinfo 处理 DST，切换周边界由测试钉死。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

SERVER_TZ_DEFAULT = "UTC"


def server_to_utc(times, tz: str = SERVER_TZ_DEFAULT) -> pd.Series:
    """MT5 服务器墙钟（naive，贴 UTC 标签）→ 真 naive UTC。

    ambiguous=True：秋季回拨的重复小时按夏令时一侧解释（每年至多影响
    切换日的一两根 bar，实测值会体现）；nonexistent 前推一小时。
    """
    s = pd.Series(pd.to_datetime(times)).dt.tz_localize(
        tz, ambiguous=True, nonexistent="shift_forward")
    return s.dt.tz_convert("UTC").dt.tz_localize(None)


def utc_to_server(times, tz: str = SERVER_TZ_DEFAULT) -> pd.Series:
    """真 naive UTC → 服务器墙钟标签（合成对齐数据/反查用）。"""
    s = pd.Series(pd.to_datetime(times)).dt.tz_localize("UTC")
    return s.dt.tz_convert(tz).dt.tz_localize(None)


# ---------------------------------------------------------------- 时刻对齐

def _offset_by_median_abs_diff(ext_df: pd.DataFrame, mt5_df: pd.DataFrame,
                               offsets_hours=range(-3, 4),
                               min_pairs: int = 30) -> dict[int, float]:
    """按收盘价对齐测各候选平移 k 的中位绝对差（外部侧须已是真 UTC）。

    时刻集合匹配对连续小时序列无区分度（平移 ±1h 仍落在有效整点上），
    价格才有：错 1h 的中位差通常比正确偏移大一个数量级（实测 XAUUSD
    0.10 对 1.8）。返回 {k: 中位|Δclose|}，min_pairs 以下的 k 不计入。
    """
    e = pd.Series(ext_df["close"].astype(float).values,
                  index=pd.DatetimeIndex(ext_df["time"]))
    m = pd.Series(mt5_df["close"].astype(float).values,
                  index=pd.DatetimeIndex(mt5_df["time"]))
    out: dict[int, float] = {}
    for k in offsets_hours:
        shifted = pd.Series(e.values, index=e.index + pd.Timedelta(hours=k))
        j = pd.concat([shifted, m], axis=1, join="inner",
                      sort=True).dropna()
        if len(j) >= min_pairs:
            out[k] = float((j.iloc[:, 0] - j.iloc[:, 1]).abs().median())
    return out


def detect_hour_misalignment(ext_df: pd.DataFrame, mt5_df: pd.DataFrame, *,
                             server_tz: str = SERVER_TZ_DEFAULT,
                             min_month_bars: int = 50) -> dict:
    """小时级时区/夏令时错位检测（外部侧须已是真 UTC）。

    方法：MT5 标签按 server_tz（zoneinfo，含 DST）换算成 UTC 后，逐月用
    **收盘价中位差**测"模式平移 k"——正确管道任何月份都应为 0h；夏令时
    处理错误表现为部分月份 ±1h（月间差=1）。同时报告：
    - modal_offset_hours：**未换算**原始标签差的价格对齐模式（服务器−UTC
      的实测；如 EET 服务器冬 2/夏 3、UTC 服务器恒 0）；
    - matched_fraction：换算后 ext 时刻在 MT5 时刻集合内的比例。
    """
    ext_t = pd.Series(pd.to_datetime(ext_df["time"]))
    mt5_wall_df = mt5_df.assign(
        time=pd.to_datetime(mt5_df["time"]))
    mt5_utc_df = mt5_wall_df.copy()
    mt5_utc_df["time"] = server_to_utc(mt5_wall_df["time"], server_tz)

    raw = _offset_by_median_abs_diff(ext_df, mt5_wall_df)
    modal_raw = min(raw, key=lambda k: (raw[k], abs(k))) if raw else None

    months = pd.Series(ext_t.dt.to_period("M").astype(str))
    month_offsets: dict[str, int] = {}
    for mth in months.unique():
        part_mask = (months == mth).values
        part = ext_df[part_mask]
        if len(part) < min_month_bars:
            continue
        frac = _offset_by_median_abs_diff(part, mt5_utc_df,
                                          offsets_hours=range(-2, 3),
                                          min_pairs=min_month_bars // 2)
        if frac:
            month_offsets[mth] = min(frac, key=lambda k: (frac[k], abs(k)))

    target = set(pd.DatetimeIndex(mt5_utc_df["time"]))
    base = set(pd.DatetimeIndex(ext_df["time"]))
    matched = (len(base & target) / len(base)) if base else 0.0
    offsets = sorted(set(month_offsets.values()))
    diff = (max(month_offsets.values()) - min(month_offsets.values())) \
        if month_offsets else 0
    misaligned = bool(month_offsets and offsets != [0])
    return {
        "misaligned": misaligned,
        "matched_fraction": round(matched, 4),
        "modal_offset_hours": modal_raw,           # 原始标签差（服务器−UTC）
        "converted_month_offsets": month_offsets,   # 换算后逐月模式平移
        "offset_by_month_diff": diff,              # 月间差 =1 ⇒ 夏令时错 1h
    }


def dst_switch_week_alignment(ext_df: pd.DataFrame, mt5_df: pd.DataFrame, *,
                              server_tz: str = SERVER_TZ_DEFAULT,
                              switches: tuple[str, ...] = ("2020-03-08",
                                                           "2020-11-01",
                                                           "2021-03-14",
                                                           "2021-10-31",
                                                           "2024-03-10",
                                                           "2024-11-03",
                                                           "2025-03-09",
                                                           "2025-10-26")
                              ) -> list[dict]:
    """美欧夏令时切换周（±3 天）逐周的时刻对齐实测：周内 ext bar 数、
    换算后与 MT5 匹配的比例、缺失的小时数（缺口）。"""
    mt5_utc = server_to_utc(mt5_df["time"], server_tz)
    ext_t = pd.DatetimeIndex(pd.to_datetime(ext_df["time"]))
    target = set(pd.DatetimeIndex(mt5_utc))
    rows = []
    for sw in switches:
        c = pd.Timestamp(sw)
        lo, hi = c - pd.Timedelta(days=3), c + pd.Timedelta(days=3)
        part = ext_t[(ext_t >= lo) & (ext_t < hi)]
        hit = sum(1 for t in part if t in target)
        rows.append({"switch": sw, "ext_bars": int(len(part)),
                     "matched": int(hit),
                     "match_rate": round(hit / len(part), 4) if len(part)
                     else None})
    return rows


# ---------------------------------------------------------------- 收益相关

def returns_correlation(ext_df: pd.DataFrame, mt5_df: pd.DataFrame, *,
                        server_tz: str = SERVER_TZ_DEFAULT) -> dict:
    """重叠期 close→close 对数收益相关（时刻精确匹配；写实测值）。

    返回 n（匹配 bar 数）与 corr（皮尔逊相关，实测值不设阈值）。
    """
    e = ext_df[["time", "close"]].copy()
    m = mt5_df[["time", "close"]].copy()
    m["time"] = server_to_utc(m["time"], server_tz)
    j = e.merge(m, on="time", suffixes=("_ext", "_mt5")).sort_values("time")
    r_ext = j["close_ext"].astype(float).pct_change()
    r_mt5 = j["close_mt5"].astype(float).pct_change()
    ok = r_ext.notna() & r_mt5.notna()
    n = int(ok.sum())
    corr = float(r_ext[ok].corr(r_mt5[ok])) if n >= 3 else None
    return {"n": n, "corr": None if corr is None else round(corr, 6)}


# ---------------------------------------------------------------- 价格水平

def price_level_diff(ext_df: pd.DataFrame, mt5_df: pd.DataFrame, *,
                     server_tz: str = SERVER_TZ_DEFAULT) -> dict:
    """重叠期价格水平差的分布（相对差 = (ext−mt5)/mt5，close 对齐）。"""
    e = ext_df[["time", "close"]].copy()
    m = mt5_df[["time", "close"]].copy()
    m["time"] = server_to_utc(m["time"], server_tz)
    j = e.merge(m, on="time", suffixes=("_ext", "_mt5"))
    if len(j) == 0:
        return {"n": 0}
    rel = (j["close_ext"].astype(float) - j["close_mt5"].astype(float)) \
        / j["close_mt5"].astype(float)
    abs_pts = (j["close_ext"].astype(float) - j["close_mt5"].astype(float))
    q = rel.quantile([0.05, 0.5, 0.95])
    return {"n": int(len(j)),
            "rel_p05": round(float(q.iloc[0]), 8),
            "rel_p50": round(float(q.iloc[1]), 8),
            "rel_p95": round(float(q.iloc[2]), 8),
            "rel_max_abs": round(float(rel.abs().max()), 8),
            "abs_p50": round(float(abs_pts.abs().median()), 6)}


# ---------------------------------------------------------------- 结构核对

def gap_stats(df: pd.DataFrame, *, expected_days=None) -> dict:
    """缺口与节假日结构：最大相邻 bar 间隔、>3 天的间隔数、逐星期 bar 占比。"""
    t = pd.DatetimeIndex(pd.to_datetime(df["time"]))
    if len(t) < 2:
        return {"bars": int(len(t))}
    gaps = t.to_series().diff().dropna()
    big = gaps[gaps > pd.Timedelta(days=3)]
    dow = t.dayofweek.to_series().value_counts(normalize=True) \
        .sort_index().round(4)
    return {"bars": int(len(t)),
            "max_gap": str(gaps.max()),
            "gaps_gt_3d": int(len(big)),
            "weekday_share": {int(k): float(v) for k, v in dow.items()}}


def weekend_structure(df: pd.DataFrame) -> dict:
    """周末结构：周六/周日 bar 占比与示例（外部源与 MT5 的交易周差异）。"""
    t = pd.DatetimeIndex(pd.to_datetime(df["time"]))
    if len(t) == 0:
        return {"sat_share": None, "sun_share": None}
    sat = float((t.dayofweek == 5).mean())
    sun = float((t.dayofweek == 6).mean())
    return {"sat_share": round(sat, 4), "sun_share": round(sun, 4)}


# ---------------------------------------------------------------- 逐年口径（G5/T81）

NY_TZ = "America/New_York"
EU_TZ = "Europe/Berlin"


def _hour_bucket(ts) -> int:
    """标签时刻 → 最近整点小时（开市晚几分钟按四舍五入归档，如 17:06→17）。"""
    return ((ts.hour * 60 + ts.minute) + 30) // 60 % 24


def _dst_on(dates, tz: str):
    """每个日期在该时区是否处于夏令时（正午判定，避开切换窗口的歧义）。

    pandas 3 无 Series.dt.dst()，用 zoneinfo 的 datetime.dst() 逐个判定。
    """
    zi = ZoneInfo(tz)
    flags = []
    for d in pd.to_datetime(pd.Series(dates)):
        noon = datetime(d.year, d.month, d.day, 12, tzinfo=zi)
        flags.append(noon.dst() != timedelta(0))
    return pd.Series(flags, dtype=bool).to_numpy()


def yearly_label_calendar(stamps, *, winter_months=(1, 2, 12),
                          gap_min=pd.Timedelta(minutes=30),
                          gap_max=pd.Timedelta(hours=24),
                          tol_rate: float = 0.05, tol_min: int = 2) -> dict:
    """逐年时间口径检测（T81/第十二轮 G5）——为 HistData 早年文件准备。

    输入**原始标签**（naive，未换算；HistData 用 adapters.histdata_raw_stamps
    从年度 zip 取）。市场结构锚点（与品种无关的事实）：外汇每周开盘在纽约
    周日 17:00（黄金 18:00），每日换日空档在纽约 17:00 一侧。锚点小时的
    标签位置随"标签规则"变化：

    - ``us_wall``     标签=美东墙钟（随美国 DST 调表）→ 锚点小时全年不变；
    - ``fixed_est``   标签=固定 EST（UTC−5 不调表，官方 FAQ 口径）→
      美国夏令时期间锚点比墙钟早 1 小时；
    - ``eu_calendar`` 标签=美东量级偏移但切换跟**欧盟日历**（实测 2020–21
      规则，converter v3）→ 仅美欧错位窗（3 月中下旬、10 月底）早 1 小时。

    方法：对每个观测（周首 bar 时刻、换日空档后的恢复时刻）取标签小时
    档；深冬（1/2/12 月，三种规则一致）取模态小时作该类观测的锚；再按
    三种规则在观测日的期望小时逐条比对，偏差最小且 ≤ 容差的规则即该年
    口径；任何规则都超容差 → ``inconsistent``（该年单独登记，不得套用
    同一转换）。所有数字写进返回值，判定阈值效果留给报告复核。

    输入应为**分钟级 cadence** 的原始标签（真实 HistData M1 即是）；换日
    空档下限会自动抬高到 3× 模态步长，小时级 cadence 的喂入不会把常规
    bar 间隔误当空档。

    返回 {年: {calendar, anchor_hours{观测类: 锚小时}, observations,
    hour_counts, mismatch_window_observations, deviations{规则: 偏差数},
    tolerance, distinguishing_observations}}。
    """
    t = pd.DatetimeIndex(pd.to_datetime(stamps)).sort_values()
    if len(t) == 0:
        return {}

    # 观测收集：(观测日, 观测类, 标签小时档)
    obs_day: list = []
    obs_kind: list = []
    obs_bucket: list = []
    sun = t[t.dayofweek == 6]
    if len(sun):
        firsts = pd.Series(sun).groupby(pd.Series(sun).dt.date).min()
        for d, v in firsts.items():
            obs_day.append(pd.Timestamp(d))
            obs_kind.append("week_open")
            obs_bucket.append(_hour_bucket(v))
    if len(t) > 1:
        diffs = t.to_series().diff().iloc[1:]
        modal = diffs.mode().iloc[0] if len(diffs) else gap_min
        gap_lo = max(gap_min, 3 * modal)
        mask = (diffs >= gap_lo) & (diffs < gap_max)
        for end in t[1:][mask.to_numpy()]:
            obs_day.append(pd.Timestamp(end.date()))
            obs_kind.append("daily_gap")
            obs_bucket.append(_hour_bucket(end))
    if not obs_day:
        return {}
    frame = pd.DataFrame({"day": obs_day, "kind": obs_kind,
                          "bucket": obs_bucket})

    rules = ("eu_calendar", "fixed_est", "us_wall")
    out: dict[int, dict] = {}
    for year, grp in frame.groupby(frame["day"].dt.year):
        rep: dict = {"observations": {k: int(n) for k, n in
                                      grp["kind"].value_counts().items()}}
        anchor_hours: dict[str, int] = {}
        winter = grp[grp["day"].dt.month.isin(winter_months)]
        for kind in ("week_open", "daily_gap"):
            wk = winter[winter["kind"] == kind]
            if len(wk):
                anchor_hours[kind] = int(wk["bucket"].mode().iloc[0])
        rep["anchor_hours"] = anchor_hours
        usable = grp[grp["kind"].isin(anchor_hours)]
        if usable.empty:
            rep.update({"calendar": "undetermined",
                        "reason": "缺深冬（1/2/12 月）观测，锚点小时无法确定"})
            out[int(year)] = rep
            continue

        us = _dst_on(usable["day"], NY_TZ)
        eu = _dst_on(usable["day"], EU_TZ)
        anchor = usable["kind"].map(anchor_hours).astype(int).to_numpy()
        expected = {
            "us_wall": anchor % 24,
            "fixed_est": (anchor - us) % 24,
            "eu_calendar": (anchor - (us & ~eu).astype(int)) % 24,
        }
        observed = usable["bucket"].to_numpy()
        deviations = {r: int((observed != expected[r]).sum())
                      for r in rules}
        n = len(usable)
        tol = max(tol_min, -(-int(tol_rate * n * 100) // 100))  # ceil(5%)
        best = min(rules, key=lambda r: (deviations[r], rules.index(r)))
        ties = [r for r in rules if deviations[r] == deviations[best]]
        if deviations[best] > tol:
            calendar = "inconsistent"
        elif len(ties) > 1:
            calendar = "undetermined"
            rep["reason"] = (f"规则 {ties} 偏差同为 {deviations[best]}，"
                             "观测缺区分度（缺美欧错位窗/夏令时窗观测？）")
        else:
            calendar = best
        rep.update({
            "calendar": calendar,
            "hour_counts": {str(h): int(c) for h, c in
                            grp["bucket"].value_counts().sort_index().items()},
            "mismatch_window_observations": int((us & ~eu).sum()),
            "us_dst_observations": int(us.sum()),
            "deviations": deviations,
            "tolerance": tol,
        })
        out[int(year)] = rep
    return out
