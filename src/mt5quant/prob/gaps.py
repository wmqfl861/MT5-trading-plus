"""T97 波次 2：合并研究序列的洞抑制与 era 判定 helper（P 线数据解锁批）。

- 抑制口径（计划 v2 先决 4，包含式）：洞 G = {(s, e)}（s = 洞前末 bar、
  e = 洞后首 bar，来自 mt5data.ext.merge 的 gap_spans，>30 天口径）；
  触发 bar i 的回看窗 W = [W首, i]。**抑制 ⇔ ∃(s,e)∈G 使 t[W首] ≤ s 且
  e ≤ t[i]**——等价实现：窗内相邻 bar 对恰为洞两侧（s 恰为窗首 / e 恰为
  触发 bar 的"贴边洞"都算跨洞：回看窗仍横跨了洞；洞止于窗首（e = t[W首]，
  洞在窗前）或始于触发 bar（s = t[i]，洞在触发后）不抑制）。
- 窗首按局面（先决 4）：S1 → max(0, i−M)（M = approach.lookback_bars）；
  S3 → max(0, i−480)；S2 → 前一个**有 bar**会话的首 bar（17:00 NY 划界，
  与 strategies.library.prob.situations.s2_level_rows 同口径）。
- era 判定（计划 v2 波次 2）：bar 时刻 < 该品种 {data_root}/bars/ 目录
  meta.json 的 first_time → ext 段（早年外部数据）；≥ → MT5 段。

评估代码（D45：研究角色只读）。不 import 标签/引擎（计数侧结构保证，
tests/test_prob_counting.py 的静态检查口径）。
"""
from __future__ import annotations

import pandas as pd

from mt5data.store import read_watermark
from mt5quant.strategies.library.prob.situations import NY_TZ, SESSION_HHMM

# 先决 4：S3 回看窗 480 根（H1 ≈ 20 日通道 ×2 缓冲；参数化仅为可测性）
S3_WINDOW_BARS = 480

__all__ = ["S3_WINDOW_BARS", "mt5_first_time", "situation_window_start",
           "suppression_mask", "suppress_across_gaps"]


# ---------------------------------------------------------------- 抑制判定

def suppress_across_gaps(bar_index, bars, gap_spans,
                         window_start_index) -> bool:
    """触发 bar 的回看窗 [window_start_index, bar_index] 是否跨洞（先决 4）。

    抑制 ⇔ ∃(s,e)∈gap_spans: t[window_start_index] ≤ s 且 e ≤ t[bar_index]
    （含式边界，见模块 docstring）。gap_spans 为空/None → False。
    """
    if not gap_spans:
        return False
    times = pd.DatetimeIndex(pd.to_datetime(bars["time"]))
    t_start = times[int(window_start_index)]
    t_trig = times[int(bar_index)]
    for s, e in gap_spans:
        s, e = pd.Timestamp(s), pd.Timestamp(e)
        if t_start <= s and e <= t_trig:
            return True
    return False


def situation_window_start(bar_index, situation, bars, approach_lookback,
                           s3_bars: int = S3_WINDOW_BARS) -> int:
    """局面 → 回看窗首 bar 下标（先决 4）。

    S1：max(0, i−approach_lookback)；S3：max(0, i−s3_bars)（缺省 480）；
    S2：前一个有 bar 会话的首 bar（17:00 NY 划界，与 s2_level_rows 同
    口径——会话日期 = 纽约本地日期，本地 ≥17:00 归次日；序列首会话内
    的 bar 无前一会话 → 0）。未知局面 → ValueError。
    """
    i = int(bar_index)
    n = len(bars)
    if not 0 <= i < n:
        raise ValueError(f"bar_index 越界：{i}（序列长 {n}）")
    if situation == "S1":
        return max(0, i - int(approach_lookback))
    if situation == "S3":
        return max(0, i - int(s3_bars))
    if situation == "S2":
        hh, mm = (int(x) for x in SESSION_HHMM.split(":"))
        local = (pd.to_datetime(bars["time"]).dt.tz_localize("UTC")
                 .dt.tz_convert(NY_TZ))
        after_close = (local.dt.hour * 60 + local.dt.minute) \
            >= hh * 60 + mm
        sess = local.dt.date.where(
            ~after_close, (local + pd.Timedelta(days=1)).dt.date).to_numpy()
        cur = sess[i]
        j = i
        while j > 0 and sess[j - 1] == cur:
            j -= 1                      # j = 当前会话首 bar
        if j == 0:
            return 0                    # 序列首会话：无前一会话 → 0
        prev = sess[j - 1]
        k = j - 1
        while k > 0 and sess[k - 1] == prev:
            k -= 1                      # k = 前一会话首 bar
        return k
    raise ValueError(f"未知局面编号：{situation!r}")


def suppression_mask(trigger_bars, bars, gap_spans, situation,
                     approach_lookback) -> list[bool]:
    """触发 bar 下标序列 → 保留掩码（True=保留；counting 与 p1 共用）。"""
    return [not suppress_across_gaps(
        int(b), bars, gap_spans,
        situation_window_start(int(b), situation, bars, approach_lookback))
        for b in trigger_bars]


# ---------------------------------------------------------------- era 判定

def mt5_first_time(data_root, symbol, timeframe):
    """该品种 MT5 段首 bar 时刻（bars/meta.json 的 first_time）。

    era 判定基准（计划 v2 波次 2）：bar 时刻 < 该时刻 → ext 段。缺
    meta.json / 缺字段 → None（调用方按"无 ext 段"处理）。
    """
    meta = read_watermark(str(data_root), symbol, str(timeframe).upper()) \
        or {}
    ft = meta.get("first_time")
    return pd.Timestamp(ft) if ft else None
