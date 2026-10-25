"""合成对照数据与工具策略（T22/H7：关卡正负对照实验）。

负对照（测误报率）——block_permute_bars：以 UTC 日为块做**日收益置换**。
每个日历日保留自己的 bar 序列、时间戳与日内形状（日内 bar 间相对路径
逐位不变，只是整日价格水平按置换后的日收益重锚），只有"日总收益"
（当日末根收盘 / 前日末根收盘）在日期之间重排：

- 保留：逐日日内方差等窗口统计量（留在原日历槽，排序不变）、日收益
  多重集（分布不变）、bar 数与时间戳（时段结构、按小时分桶的成本口径
  不变）→ 波动聚集的日内结构原样保留；
- 破坏：跨日自相关（日收益次序随机化，"用昨天预测今天"的可预测性归零）
  ——六个基线策略打乱后的任何正期望只能来自运气，其过关率即误报率；
- 种子化：np.random.default_rng(seed)，同一种子逐位可复现。

正对照（测检出率）——plant_edge + PlantedEdgeStrategy：
- 固定规则产生"事件 bar"：interval_hours 时间块（epoch 锚定，可带
  phase_hours 相位）的首根 bar，且其 UTC 日序号 + day_phase 满足
  % period_days == 0。规则纯日历且因果（只看当前与前一根 bar 的时间，
  不看价格、不看未来）；
- 植入：每个事件 bar i 之后的 k = hold_bars 根 bar 的 OHLC **一致**加上
  漂移 d（四个价格同加一个 d：价格水平抬升 d，k 根后整体回落——瞬时
  水平移位）；drift=0 时返回与输入逐位相同的副本（零改动路径）；
- PlantedEdgeStrategy（BaseStrategy 子类，只交易事件，参数 1 个 ≤ 4）：
  事件 bar 收盘发 −1（引擎次根开盘做空，成交价已含 +d 抬升），信号持续
  hold_bars 根后回补（回补时价格已回落）→ 每笔期望毛利 ≈ d − 成本，
  与植入器严格配套（事件规则、相位、持有根数一致）；
- phase_hours / day_phase 由实验种子驱动，使各种子的事件相位不同
  （同一数据不同噪声实现 → 检出率是对实验条件的分布，不是单点）；
- 夏普档位校准见 scripts/gate_calibration.py 的 calibrate_drift（先验
  公式 + 割线迭代，扣成本后年化夏普 ≈ 0.3 / 0.6 / 1.0 / 1.5）。

RandomEntries：负对照的"随机策略"——事件时刻与方向由 (seed, bar 时间)
的 sha256 决定。时间锚定（与 bar 在序列中的位置无关）→ 截断一致性与
窗口不变性天然成立，G0 干净；同一 seed 逐位可复现。
"""
from __future__ import annotations

import hashlib
import math

import numpy as np
import pandas as pd

from mt5quant.strategies.base import (
    BaseStrategy,
    ExitRules,
    ParamSpec,
    RequiredData,
)

_EPOCH = pd.Timestamp("1970-01-01")
_PRICE_COLS = ("open", "high", "low", "close")
_SECONDS_PER_DAY = 86400


def _epoch_seconds(times) -> np.ndarray:
    """bar 时间 → epoch 秒（int64；时间语义只依赖绝对时刻，与位置无关）。"""
    idx = pd.DatetimeIndex(pd.Series(times))
    return ((idx - _EPOCH) // pd.Timedelta(seconds=1)).to_numpy(
        dtype="int64")


def _check_positive_int(value, name: str) -> int:
    v = int(value)
    if v < 1:
        raise ValueError(f"{name} 必须是 ≥1 的整数，收到 {value!r}")
    return v


# ---------------------------------------------------------------- 事件规则

def event_bars(bars: pd.DataFrame, *, period_days: int, interval_hours: int,
               phase_hours: int = 0, day_phase: int = 0) -> np.ndarray:
    """事件 bar 布尔掩码（固定日历规则，纯函数、因果、可复现）。

    bar i 是事件 bar ⟺ 它是所属 interval_hours 时间块的首根 bar
    （与前一 bar 不在同一块；bar 0 视作块首）且其 UTC 日序号 +
    day_phase 对 period_days 取余为 0。参数为周期/相位时事件间距
    ≥ interval_hours×3600 秒，配合 hold_bars < 块内 bar 数不会重叠。
    """
    period_days = _check_positive_int(period_days, "period_days")
    interval_hours = _check_positive_int(interval_hours, "interval_hours")
    n = len(bars)
    if n == 0:
        return np.zeros(0, dtype=bool)
    sec = _epoch_seconds(bars["time"])
    block = (sec + int(phase_hours) * 3600) // (interval_hours * 3600)
    day_ok = ((sec // _SECONDS_PER_DAY) + int(day_phase)) % period_days == 0
    boundary = np.ones(n, dtype=bool)
    if n > 1:
        boundary[1:] = block[1:] != block[:-1]
    return boundary & day_ok


# ---------------------------------------------------------------- 植入优势

def plant_edge(bars: pd.DataFrame, *, period_days: int, interval_hours: int,
               hold_bars: int, drift: float, phase_hours: int = 0,
               day_phase: int = 0) -> pd.DataFrame:
    """在事件 bar 之后植入优势：其后 k=hold_bars 根 bar 的 OHLC 一致加 d。

    drift == 0 → 返回逐位相同的副本（不触碰任何值，保证 bitwise 还原）；
    事件落在数据尾部时漂移只加到存在的 bar（部分植入，如实）。
    """
    hold_bars = _check_positive_int(hold_bars, "hold_bars")
    if not isinstance(drift, (int, float)) or not math.isfinite(drift):
        raise ValueError(f"drift 必须是有限数，收到 {drift!r}")
    if drift == 0.0:
        return bars.copy()
    events = np.flatnonzero(event_bars(
        bars, period_days=period_days, interval_hours=interval_hours,
        phase_hours=phase_hours, day_phase=day_phase))
    out = bars.copy()
    n = len(out)
    shift = np.zeros(n, dtype="float64")
    for i in events:
        shift[i + 1: i + 1 + hold_bars] += float(drift)
    for col in _PRICE_COLS:
        out[col] = out[col].astype("float64") + shift
    return out


# ---------------------------------------------------------------- 块置换

def block_permute_bars(bars: pd.DataFrame, *, seed: int) -> pd.DataFrame:
    """负对照打乱：日收益置换（块 = 一个 UTC 日），保留日内结构。

    实现：每个日历日 d 的 OHLC 统一乘以 alpha_d，使重建后的日总收益
    等于从别的日期搬来的 R（多重集不变），而日内价格比值逐位保留
    （日内形状整体缩放）。日 0 原样保留（锚定起点）。价格必须为正
    （乘法重锚的前提），否则如实报错。
    """
    if len(bars) == 0:
        raise ValueError("bars 为空：无日收益可置换")
    sec = _epoch_seconds(bars["time"])
    day_ids = sec // _SECONDS_PER_DAY
    if len(np.unique(day_ids)) < 2:
        raise ValueError("bars 覆盖不足两个 UTC 日：无日收益可置换")
    # 逐日槽位（bars 升序 → 日块连续）：首末 bar 的 open/close
    bounds = np.flatnonzero(np.diff(day_ids) != 0)
    starts = np.concatenate(([0], bounds + 1))
    ends = np.concatenate((bounds, [len(bars) - 1]))    # 每日末根（含）
    opens = bars["open"].to_numpy(dtype="float64")
    closes = bars["close"].to_numpy(dtype="float64")
    o_first = opens[starts]
    c_last = closes[ends]
    if not (np.all(o_first > 0) and np.all(c_last > 0)):
        raise ValueError("块置换要求 OHLC 价格为正（乘法重锚），数据含非正值")

    n_days = len(starts)
    daily_returns = c_last[1:] / c_last[:-1]            # R_1..R_{D-1}
    rng = np.random.default_rng(seed)
    permuted = daily_returns[rng.permutation(n_days - 1)]

    alphas = np.empty(n_days, dtype="float64")
    alphas[0] = 1.0
    prev_close = c_last[0]
    for d in range(1, n_days):
        # 目标：重建后的当日末根收盘 = prev_close × R_perm；
        # 整日缩放保持日内形状（open..close 同乘 alpha，日内比值逐位不变）
        alphas[d] = prev_close * permuted[d - 1] / c_last[d]
        prev_close = closes[ends[d]] * alphas[d]
    out = bars.copy()
    # 逐 bar 的日槽位号（周末等无 bar 的日历日不在 day_ids 里，不能按
    # 日序号差直接索引——按每日 bar 数展开）
    counts = np.diff(np.concatenate((starts, [len(bars)])))
    per_bar_alpha = np.repeat(alphas, counts)
    for col in _PRICE_COLS:
        out[col] = out[col].astype("float64") * per_bar_alpha
    return out


# ---------------------------------------------------------------- 工具策略

# 网格内最大 hold；窗口不变性要求 lookback ≥ max_hold + 2（信号在位置 t
# 由 [t-hold+1, t] 内的事件决定，事件判定还要再看前一根 bar）
_MAX_HOLD_BARS = 8


def _expand_events(events: np.ndarray, hold: int, direction: int) -> np.ndarray:
    """事件 → 持续 hold 根的 ±1 信号段（后发事件覆盖先发，确定性）。"""
    sig = np.zeros(len(events), dtype="int64")
    for i in np.flatnonzero(events):
        sig[i: i + hold] = direction
    return sig


class PlantedEdgeStrategy(BaseStrategy):
    """只交易植入事件（正对照工具策略，与 plant_edge 严格配套）。

    参数网格只有 hold_bars (4, 8) 一个（≤4）；period_days / interval_hours /
    phase_hours / day_phase 是构造参数（实验里由数据与种子决定，不进网格，
    避免邻域检查把"对齐"当自由度）。事件 bar 收盘 → −1 信号持续 hold 根：
    引擎次根开盘做空（成交价含 +d 抬升），信号归零后次根开盘回补（价格已
    回落）→ 每笔期望毛利 ≈ d。无保护性出场（纯信号出场，风险距离按
    sizing 的 fallback ATR 估）。
    """

    params_schema = (ParamSpec(name="hold_bars", type="int", grid=(4, 8)),)
    required_data = RequiredData(symbols=(), timeframe="",
                                 lookback_bars=_MAX_HOLD_BARS + 2)
    exit_rules = ExitRules()

    def __init__(self, *, period_days: int = 1, interval_hours: int = 24,
                 phase_hours: int = 0, day_phase: int = 0):
        self._period_days = _check_positive_int(period_days, "period_days")
        self._interval_hours = _check_positive_int(interval_hours,
                                                   "interval_hours")
        self._phase_hours = int(phase_hours)
        self._day_phase = int(day_phase)

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        p = self.validate_params(dict(params))
        hold = int(p["hold_bars"])
        events = event_bars(
            bars, period_days=self._period_days,
            interval_hours=self._interval_hours,
            phase_hours=self._phase_hours, day_phase=self._day_phase)
        return pd.Series(_expand_events(events, hold, -1),
                         index=bars.index, dtype="int64")


class RandomEntries(BaseStrategy):
    """随机入场对照策略（负对照）：事件与方向由 (seed, bar 时间) 哈希决定。

    每个 bar：h = sha256(f"{seed}|{时间}") 前 8 字节 → h % event_every_bars
    == 0 即事件，方向取 h 的一个比特。时间锚定（不看 bar 位置、不看价格）
    → 截断一致 / 窗口不变天然成立；同 seed 逐位可复现。lookback 按构造的
    hold_bars + 2 声明（实例级，G0 窗口检查直接可用）。
    """

    exit_rules = ExitRules()

    def __init__(self, *, seed: int = 0, hold_bars: int = 4,
                 event_every_bars: int = 24):
        self._seed = int(seed)
        self._hold = _check_positive_int(hold_bars, "hold_bars")
        self._every = _check_positive_int(event_every_bars, "event_every_bars")
        if self._every < 2:
            raise ValueError("event_every_bars 必须 ≥2（否则每根 bar 都是事件）")
        self.required_data = RequiredData(symbols=(), timeframe="",
                                          lookback_bars=self._hold + 2)

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        self.validate_params(dict(params))
        times = pd.DatetimeIndex(bars["time"])
        hashes = [
            int.from_bytes(hashlib.sha256(
                f"{self._seed}|{times[i].isoformat()}".encode("utf-8")
            ).digest()[:8], "big") for i in range(len(bars))]
        sig = np.zeros(len(bars), dtype="int64")
        for i, h in enumerate(hashes):
            if h % self._every == 0:
                sig[i: i + self._hold] = -1 if (h >> 40) & 1 else 1
        return pd.Series(sig, index=bars.index, dtype="int64")
