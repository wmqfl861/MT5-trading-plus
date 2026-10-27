"""首达标签（T72，蓝图 P 线 §2.3）：对每次局面触发用引擎 A 跑一笔回测，
记录 K 根之内先发生的事件（止盈=赢 / 止损=输 / 到期=按信号平仓）与净收益（R）。

与引擎 A 严格同口径（成交、成本、止损越幅、跳空、禁入窗、同 bar 双触止损
优先）：每个触发截取有界切片（LOOKBACK_BARS 回看 + horizon_bars+2 根），
信号在触发 bar t..t+K−1 为方向值、t+K 归零——入场 t+1 开盘、到期 t+K+1
开盘按 signal 平仓；ExitRules 为 sl_atr/tp_atr + after_signal_change，
定手数用共享函数（mt5quant.sizing）。本模块属评估代码（D45：研究角色只读）。

禁止事项（蓝图 v4 §2）：在 T75 标定报告与 T77 轮次草案获批之前，本模块
不得在真实局面触发上调用；允许的用途限于随机时点对账、随机化触发与合成
数据单元测试。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from mt5data.store import TIMEFRAME_SECONDS
from mt5quant.backtest.engine_a import run_backtest
from mt5quant.sizing import RiskSizing, validate_volume_bounds
from mt5quant.strategies import BaseStrategy, ExitRules

# 切片回看长度：需覆盖 ATR14 Wilder 平滑的收敛（(13/14)^n 衰减）与
# 特征回看（200 根 SMA / 2000 根分位由 T74 在策略层自备更长窗口）。
LOOKBACK_BARS = 300

# 引擎 exit_reason → 标签 outcome
_WIN_REASONS = frozenset({"take_profit", "take_profit_gap"})
_LOSS_REASONS = frozenset({"stop", "stop_gap"})


@dataclass(frozen=True)
class Bracket:
    """同一张卡全部动作共用的止损止盈与持有上限（蓝图 P 线 §2.2）。"""

    sl_atr: float
    tp_atr: float
    horizon_bars: int

    def __post_init__(self):
        for name in ("sl_atr", "tp_atr"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) \
                    or not math.isfinite(v) or v <= 0:
                raise ValueError(f"{name} 必须是有限正数，收到 {v!r}")
        if isinstance(self.horizon_bars, bool) \
                or not isinstance(self.horizon_bars, int) \
                or self.horizon_bars < 1:
            raise ValueError(
                f"horizon_bars 必须是 ≥1 的整数，收到 {self.horizon_bars!r}")


class _TriggerSignal(BaseStrategy):
    """标签专用：按位置取脚本信号（缺省 0），出场规则由 Bracket 决定。"""

    def __init__(self, script: list[int], rules: ExitRules):
        self.script = list(script)
        self.exit_rules = rules

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        vals = [self.script[i] if i < len(self.script) else 0
                for i in range(len(bars))]
        return pd.Series(vals, index=bars.index, dtype="int64")


def _locate(bars: pd.DataFrame, times) -> list[int]:
    idx = pd.Index(bars["time"]).get_indexer(pd.DatetimeIndex(times))
    missing = [t for t, i in zip(times, idx) if i < 0]
    if missing:
        raise ValueError(f"触发时间不在 bars 里：{missing[:3]}（共 {len(missing)} 个）")
    return [int(i) for i in idx]


def label_triggers(bars: pd.DataFrame, triggers: pd.DataFrame, costs, *,
                   symbol: str, timeframe: str, bracket: Bracket,
                   contract_size: float, sizing: RiskSizing,
                   vol_bounds: tuple[float, float, float],
                   train_end=None, session_policy=None) -> pd.DataFrame:
    """给每次触发打首达标签。

    triggers 列：time（触发 bar，收盘判断）、situation、action、side（±1）、
    level 及特征列（全部原样透传）。返回追加列：
    outcome ∈ {win, loss, time, skipped}、net_r（= 引擎 r_multiple）、
    entry_time、exit_time、exit_reason（成交=引擎原因；未成交=
    zero_lots / no_fill / end_of_data）。

    purge：train_end 给定时按 **bar 下标**剔除（审核修正 2）——保留条件为
    t+K+1 存在于 bars 且 bars.time[t+K+1] < train_end（信号出场发生在
    bar t+K+1 的开盘，该根的 open 是标签用到的最后信息）。按时间外推
    （触发+(K+2)×周期）会跨周末/节假日缺口漏剔。

    vol_bounds = (vol_min, vol_step, vol_max)（品种快照三元组）。
    """
    vol_min, vol_step, vol_max = validate_volume_bounds(*vol_bounds)
    tf_seconds = TIMEFRAME_SECONDS.get(
        timeframe.upper() if timeframe else "")
    if tf_seconds is None:
        raise ValueError(f"timeframe 非法：{timeframe!r}")
    if not isinstance(triggers, pd.DataFrame) or "time" not in triggers \
            or "side" not in triggers:
        raise ValueError("triggers 必须是含 time 与 side 列的 DataFrame")
    sides = pd.to_numeric(triggers["side"], errors="coerce")
    bad = triggers.index[~sides.isin([1, -1])]
    if len(bad):
        raise ValueError(f"side 必须是 ±1，违规行索引：{list(bad)[:5]}")

    positions = _locate(bars, triggers["time"])
    keep = list(range(len(triggers)))
    if train_end is not None:
        limit = pd.Timestamp(train_end)
        times = pd.to_datetime(bars["time"])
        window_bar = bracket.horizon_bars + 1
        keep = [i for i in keep
                if positions[i] + window_bar < len(bars)
                and pd.Timestamp(times.iloc[positions[i] + window_bar]) < limit]

    n = len(bars)
    out = triggers.iloc[keep].copy()
    outcomes: list[str] = []
    net_rs: list[float] = []
    entry_ts, exit_ts, reasons = [], [], []
    for i in keep:
        t, side = positions[i], int(sides.iloc[i])
        start = max(0, t - LOOKBACK_BARS)
        end = min(n, t + bracket.horizon_bars + 2)
        sl = bars.iloc[start:end].reset_index(drop=True)
        i0 = t - start
        script = [0] * len(sl)
        for j in range(i0, min(i0 + bracket.horizon_bars, len(sl))):
            script[j] = side
        strategy = _TriggerSignal(script, ExitRules(
            stop_loss_atr=bracket.sl_atr,
            take_profit_atr=bracket.tp_atr,
            reentry="after_signal_change"))
        res = run_backtest(
            strategy, sl, costs, symbol=symbol, params={},
            contract_size=contract_size, timeframe=timeframe,
            session_policy=session_policy, sizing=sizing,
            vol_min=vol_min, vol_step=vol_step, vol_max=vol_max)
        if len(res.trades) == 0:
            outcomes.append("skipped")
            net_rs.append(float("nan"))
            entry_ts.append(pd.NaT)
            exit_ts.append(pd.NaT)
            reasons.append("zero_lots" if res.skipped_zero_lots else "no_fill")
            continue
        if len(res.trades) != 1:   # 单向脚本信号只会产生一笔；防御性断言
            raise AssertionError(
                f"触发 {triggers['time'].iloc[i]} 产生 {len(res.trades)} 笔交易")
        trade = res.trades.iloc[0]
        reason = str(trade.exit_reason)
        if reason in _WIN_REASONS:
            outcome = "win"
        elif reason in _LOSS_REASONS:
            outcome = "loss"
        elif reason == "signal":
            outcome = "time"
        else:   # end_of_data：数据不足，标签截尾（time_stop/week_close 未声明）
            outcome, reason = "skipped", "end_of_data"
        outcomes.append(outcome)
        net_rs.append(float(trade.r_multiple))
        entry_ts.append(pd.Timestamp(trade.entry_time))
        exit_ts.append(pd.Timestamp(trade.exit_time))
        reasons.append(reason)

    out["outcome"] = outcomes
    out["net_r"] = net_rs
    out["entry_time"] = entry_ts
    out["exit_time"] = exit_ts
    out["exit_reason"] = reasons
    return out
