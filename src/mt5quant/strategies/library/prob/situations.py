"""局面库 v1（T74，蓝图 P 线 §2.1）：S1 整数关口 / S2 前一交易日高低点 /
S3 20 日高低点。

价位必须来自订单可能聚集的地方，不允许事后画线（机制依据 Osler 2003：
止盈单集中在整数位，止损单集中在整数位稍外侧）。三种局面统一输出
"价位 × 方向（阻力/支撑）"事件：

- **接近价位**（Approach，审核修正 3）：本根的 high（阻力）或 low（支撑）
  进入价位 ±d 个 ATR 的触及带；前 M 根 bar 都在触及带之外且位于价位同侧
  （阻力：high_j < L − d·ATR_j；支撑：low_j > L + d·ATR_j），且其中
  **至少一根**离价位不小于 D 个 ATR（曾从足够远处接近）。M、D、d 来自卡
  的 approach 配置，不进参数网格。（旧定义"前 M 根每根都 ≥ D·ATR"要求本根
  比上一根至少冲高 (D−d) 个 ATR，事件稀少且偏向大阳线——规格错误已在
  蓝图 P 线 §2.1 同步更正。）
- S1 网格间距写死在候选里（XAUUSD 50、EURUSD 0.0050、USTEC 100、
  BTCUSD 1000）；S2 交易日按 17:00 America/New_York 划分（铁律 1，DST 由
  zoneinfo 处理，"前一交易日"= 前一个**有 bar 的**会话）；S3 复用 B1 的
  通道计算（rolling 极值整体右移一根，本根不进通道）。
- 全部只用已收盘 bar（铁律 3）；ATR 与引擎同口径（engine_a.atr_series，
  Wilder ATR14）。本模块属策略库（D45：研究角色可写，计入代码指纹）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

NY_TZ = "America/New_York"
SESSION_HHMM = "17:00"
BARS_PER_DAY_H1 = 24

# S1 价位网格缺省（蓝图 P 线 §2.1；卡的 level_grid 可覆盖）
GRID_DEFAULTS = {"XAUUSD": 50.0, "EURUSD": 0.0050, "USTEC": 100.0,
                 "BTCUSD": 1000.0}

_EPS = 1e-9


@dataclass(frozen=True)
class Approach:
    """"接近价位"的参数（卡的 approach：M / D / d）。

    M：回看 bar 数（前 M 根都在触及带外、价位同侧，且至少一根达到 D）。
    """

    lookback_bars: int        # M：接近阶段回看 bar 数
    min_distance_atr: float   # D：窗口内至少一根 bar 离价位达到 D 个 ATR
    touch_atr: float          # d：触及带半宽（ATR 倍数）

    def __post_init__(self):
        if isinstance(self.lookback_bars, bool) \
                or not isinstance(self.lookback_bars, int) \
                or self.lookback_bars < 1:
            raise ValueError(
                f"lookback_bars 须为 ≥1 整数：{self.lookback_bars!r}")
        for name in ("min_distance_atr", "touch_atr"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) \
                    or not math.isfinite(v) or v <= 0:
                raise ValueError(f"{name} 须为有限正数：{v!r}")


@dataclass(frozen=True)
class LevelEvent:
    """一次局面触发：bar i 的 high（阻力）或 low（支撑）进入价位带。"""

    bar: int                  # 触及价位那根 bar 的下标（anchor，收盘判断）
    time: pd.Timestamp
    situation: str            # "S1" | "S2" | "S3"
    level: float
    kind: str                 # "resistance" | "support"


def grid_levels_between(step: float, lo: float, hi: float) -> list[float]:
    """[lo, hi] 内以 step 为间距的价位网格（含恰在端点上的价位，浮点容差）。"""
    if not (math.isfinite(step) and step > 0):
        raise ValueError(f"grid_step 须为有限正数：{step!r}")
    if not (math.isfinite(lo) and math.isfinite(hi)) or lo > hi:
        raise ValueError(f"网格区间非法：[{lo!r}, {hi!r}]")
    k0 = math.ceil(lo / step - _EPS)
    k1 = math.floor(hi / step + _EPS)
    return [k * step for k in range(k0, k1 + 1)]


def s1_level_rows(bars: pd.DataFrame, *, grid_step: float, approach: Approach,
                  atr: pd.Series) -> pd.Series:
    """每 bar 的候选整数价位：触及带 [low−d·ATR, high+d·ATR] 内的网格点。"""
    highs = bars["high"].to_numpy(dtype="float64")
    lows = bars["low"].to_numpy(dtype="float64")
    atrs = atr.to_numpy(dtype="float64")
    out = []
    for i in range(len(bars)):
        a = atrs[i]
        if not math.isfinite(a):
            out.append([])
            continue
        band = approach.touch_atr * a
        out.append(grid_levels_between(grid_step, lows[i] - band,
                                       highs[i] + band))
    return pd.Series(out, index=bars.index, dtype="object")


def s2_level_rows(bars: pd.DataFrame, *, tz: str = NY_TZ,
                  hhmm: str = SESSION_HHMM) -> pd.DataFrame:
    """每 bar 所属交易会话（17:00 本地划界）与前一**有 bar**会话的高低点。"""
    hh, mm = (int(x) for x in hhmm.split(":"))
    local = pd.to_datetime(bars["time"]).dt.tz_localize("UTC").dt.tz_convert(tz)
    after_close = (local.dt.hour * 60 + local.dt.minute) >= hh * 60 + mm
    sess = local.dt.date.where(~after_close,
                               (local + pd.Timedelta(days=1)).dt.date)
    rows = pd.DataFrame({"sess": sess}, index=bars.index)
    grouped = bars.groupby(sess, sort=True).agg(
        sess_high=("high", "max"), sess_low=("low", "min"))
    grouped["prev_high"] = grouped["sess_high"].shift(1)
    grouped["prev_low"] = grouped["sess_low"].shift(1)
    return rows.join(grouped[["prev_high", "prev_low"]], on="sess")


def s3_level_rows(bars: pd.DataFrame, *, days: int = 20,
                  bars_per_day: int = BARS_PER_DAY_H1) -> pd.DataFrame:
    """20 日高低点通道（B1 同法：rolling 极值整体右移一根，本根不进通道）。"""
    if days < 1 or bars_per_day < 1:
        raise ValueError(f"days/bars_per_day 须为正：{days!r}/{bars_per_day!r}")
    n = days * bars_per_day
    return pd.DataFrame({
        "chan_high": bars["high"].rolling(n).max().shift(1),
        "chan_low": bars["low"].rolling(n).min().shift(1),
    }, index=bars.index)


def levels_from_frame(rows: pd.DataFrame, *, columns: list[str]) -> pd.Series:
    """把帧里的价位列（NaN 剔除、去重、升序）组成每 bar 的候选价位列表。"""
    out = []
    for _, r in rows.iterrows():
        vals = {float(r[c]) for c in columns
                if c in rows.columns and pd.notna(r[c])}
        out.append(sorted(vals))
    return pd.Series(out, index=rows.index, dtype="object")


def _approach_ok(highs, lows, i: int, level: float, kind: str,
                 approach: Approach, atr_arr) -> bool:
    """接近条件（审核修正 3）：[i−M, i−1] 每根都在触及带外且位于价位同侧
    （阻力：high_j < L − d·ATR_j；支撑：low_j > L + d·ATR_j），且其中至少
    一根离价位 ≥ D·ATR（从足够远处接近过）。"""
    far_enough = False
    for j in range(i - approach.lookback_bars, i):
        if j < 0:
            return False
        a = atr_arr[j]
        if not math.isfinite(a):
            return False
        if kind == "resistance":
            if not highs[j] < level - approach.touch_atr * a + _EPS:
                return False
            if level - highs[j] >= approach.min_distance_atr * a - _EPS:
                far_enough = True
        else:
            if not lows[j] > level + approach.touch_atr * a - _EPS:
                return False
            if lows[j] - level >= approach.min_distance_atr * a - _EPS:
                far_enough = True
    return far_enough


def find_level_events(bars: pd.DataFrame, levels_by_bar: pd.Series, *,
                      situation: str, approach: Approach,
                      atr: pd.Series) -> list[LevelEvent]:
    """对每 bar 的候选价位判定阻力/支撑触发（approach + 触及带内）。

    阻力：|high_i − L| ≤ d·ATR_i，且 [i−M, i−1] 每根 high_j < L − d·ATR_j
    （带外同侧）、其中至少一根离价位 ≥ D·ATR；支撑对称（low）。
    输出按 (bar, level, kind) 升序，确定性。
    """
    if situation not in ("S1", "S2", "S3"):
        raise ValueError(f"未知局面编号：{situation!r}")
    if len(levels_by_bar) != len(bars):
        raise ValueError("levels_by_bar 长度须与 bars 一致")
    highs = bars["high"].to_numpy(dtype="float64")
    lows = bars["low"].to_numpy(dtype="float64")
    atrs = atr.to_numpy(dtype="float64")
    events: list[LevelEvent] = []
    times = pd.to_datetime(bars["time"])
    for i, cands in enumerate(levels_by_bar):
        a = atrs[i]
        if not math.isfinite(a) or not cands:
            continue
        band = approach.touch_atr * a + _EPS * max(1.0, abs(highs[i]))
        for level in sorted(cands):
            if abs(highs[i] - level) <= band \
                    and _approach_ok(highs, lows, i, level, "resistance",
                                     approach, atrs):
                events.append(LevelEvent(i, times.iloc[i], situation, float(level),
                                         "resistance"))
            if abs(lows[i] - level) <= band \
                    and _approach_ok(highs, lows, i, level, "support",
                                     approach, atrs):
                events.append(LevelEvent(i, times.iloc[i], situation, float(level),
                                         "support"))
    return events
