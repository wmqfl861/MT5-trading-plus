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
