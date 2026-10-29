"""T97 波次 2 + T98 P-09：合并研究序列的洞抑制、共同可用集合与 era
判定 helper（P 线数据解锁批）。

- 抑制口径（计划 v2 先决 4，包含式）：洞 G = {(s, e)}（s = 洞前末 bar、
  e = 洞后首 bar，来自 mt5data.ext.merge 的 gap_spans，>30 天口径）；
  触发 bar i 的回看窗 W = [W首, i]。**抑制 ⇔ ∃(s,e)∈G 使 t[W首] ≤ s 且
  e ≤ t[i]**——等价实现：窗内相邻 bar 对恰为洞两侧（s 恰为窗首 / e 恰为
  触发 bar 的"贴边洞"都算跨洞：回看窗仍横跨了洞；洞止于窗首（e = t[W首]，
  洞在窗前）或始于触发 bar（s = t[i]，洞在触发后）不抑制）。
- 窗首按局面（先决 4）：S1 → max(0, i−M)（M = approach.lookback_bars）；
  S3 → max(0, i−480)；S2 → 前一个**有 bar**会话的首 bar（17:00 NY 划界，
  与 strategies.library.prob.situations.s2_level_rows 同口径）。
- 共同可用集合（T98 audit P-09）：eligibility_mask 在回看窗之外再查
  特征预热与持有窗（未来可观察），counting 与 p1 共用——真实触发与
  随机对照同一合格口径。
- era 判定（计划 v2 波次 2）：bar 时刻 < MT5 段首 bar 时刻 → ext 段
  （早年外部数据）；≥ → MT5 段。首 bar 时刻优先用 merge 的
  source_tags 实测（P-09：不信水位），旧路径读 bars/ 目录 meta.json
  的 first_time。

评估代码（D45：研究角色只读）。不 import 标签/引擎（计数侧结构保证，
tests/test_prob_counting.py 的静态检查口径）。
"""
from __future__ import annotations

import pandas as pd

from mt5data.store import read_watermark
from mt5quant.strategies.library.prob.situations import NY_TZ, SESSION_HHMM

# 先决 4：S3 回看窗 480 根（H1 ≈ 20 日通道 ×2 缓冲；参数化仅为可测性）
S3_WINDOW_BARS = 480

# P-09 特征预热缺省：atr_pct_2000 的排名窗 2000 根（触发根不足时 ATR
# 分位档为 None——共同可用集合把特征未预热的触发排除在真实/随机两侧之外）
FEATURE_WARMUP_BARS = 2000

# P-09：局面依赖从 anchor 根起算的动作（A2 回踩/A4 假突破都在事件 anchor
# 之后最多 2m 根触发，从 action bar 回看会漏掉 anchor 前的局面窗口）
_ANCHOR_ACTIONS = ("A2", "A4")

__all__ = ["S3_WINDOW_BARS", "FEATURE_WARMUP_BARS", "mt5_first_time",
           "situation_window_start", "suppression_mask",
           "suppress_across_gaps", "eligibility_mask"]


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
    """触发 bar 下标序列 → 保留掩码（True=保留；counting 与 p1 共用）。

    T98 P-09 起计数/P1 路径改用 eligibility_mask（A2/A4 的局面窗从
    anchor 根起算 + 预热/持有窗）；本函数保留给只需要回看窗抑制的
    调用方与既有测试。
    """
    return [not suppress_across_gaps(
        int(b), bars, gap_spans,
        situation_window_start(int(b), situation, bars, approach_lookback))
        for b in trigger_bars]


# ------------------------------------------------ 共同可用集合（T98 P-09）

def _wide_gaps(gap_spans, max_gap_days
               ) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """gap_spans 里宽 **> max_gap_days** 的洞（None/空 → []）——持有窗
    检查用的洞宽阈值（回看窗按先决 4 用 gap_spans 原样）。

    merge 的 gap_spans 本就是 >30 天口径；max_gap_days 只让调用方
    收紧/放宽持有窗的洞宽阈值（恰 max_gap_days 天宽的不算洞）。
    """
    if not gap_spans:
        return []
    thr = pd.Timedelta(days=int(max_gap_days))
    out = []
    for s, e in gap_spans:
        s, e = pd.Timestamp(s), pd.Timestamp(e)
        if e - s > thr:
            out.append((s, e))
    return out


def _s2_prev_session_starts(bars) -> list[int]:
    """每 bar 的 S2 窗首（前一个有 bar 会话的首 bar；序列首会话 → 0）。

    situation_window_start(·, "S2", ·) 的 O(n) 批量版——逐 bar 调后者
    要各重算一次会话划分（O(n²)），eligibility_mask 对全序列评估时不可
    用；口径逐位一致（tests/test_prob_t97_wiring.py 对拍）。
    """
    hh, mm = (int(x) for x in SESSION_HHMM.split(":"))
    local = (pd.to_datetime(bars["time"]).dt.tz_localize("UTC")
             .dt.tz_convert(NY_TZ))
    after_close = (local.dt.hour * 60 + local.dt.minute) >= hh * 60 + mm
    sess = local.dt.date.where(
        ~after_close, (local + pd.Timedelta(days=1)).dt.date).to_numpy()
    n = len(sess)
    first_of_sess = [0] * n
    for i in range(1, n):
        first_of_sess[i] = (first_of_sess[i - 1]
                            if sess[i] == sess[i - 1] else i)
    return [0 if first_of_sess[i] == 0 else first_of_sess[first_of_sess[i] - 1]
            for i in range(n)]


def eligibility_mask(triggers, bars, gap_spans, situation, approach_lookback,
                     s3_bars: int = S3_WINDOW_BARS,
                     feature_warmup: int = FEATURE_WARMUP_BARS,
                     hold_bars: int = 0,
                     max_gap_days: int = 30) -> list[bool]:
    """P-09 共同可用集合：每个触发行是否合格（counting 与 p1 共用）。

    合格 ⇔ 三个条件都成立：
    1. **局面回看窗不跨洞**：窗首按局面（先决 4：S1=max(0, origin−M)、
       S3=max(0, origin−s3_bars)、S2=前一个有 bar 会话的首 bar）。origin
       对 **A2/A4 是 anchor 根**（局面依赖从 anchor 起——audit P-09：从
       action bar 回看会遗漏 anchor 前窗口），其余动作是触发根；anchor
       列缺失/非数/越界 → 退回触发根。洞集合 = gap_spans 原样（先决 4
       口径；merge 的 gap_spans 本就是 >30 天洞）。
    2. **特征预热**：触发根 ≥ feature_warmup（缺省 2000 =
       atr_pct_2000 的排名窗；0 = 不检查）。
    3. **持有窗不跨洞**（hold_bars > 0 时）：[触发根, 触发根+hold_bars]
       （含端点、越界截到序列尾）——标签持有窗须未来可观察；此处洞
       集合 = gap_spans 中宽 **> max_gap_days** 的对（调用方可收紧/放宽
       洞宽阈值，恰 max_gap_days 天宽的不算洞）。
    triggers 为空 → []；未知局面 → ValueError。返回与 triggers 等长的
    bool 列表（True=合格）。
    """
    n = len(bars)
    if situation not in ("S1", "S2", "S3"):
        raise ValueError(f"未知局面编号：{situation!r}")
    if len(triggers) == 0:
        return []
    wide = _wide_gaps(gap_spans, max_gap_days) if hold_bars else []
    s2_starts = _s2_prev_session_starts(bars) if situation == "S2" else None
    columns = set(triggers.columns) if hasattr(triggers, "columns") else set()
    t_bars = [int(b) for b in triggers["bar"]]
    t_anchor = (list(triggers["anchor"]) if "anchor" in columns
                else [None] * len(t_bars))
    t_action = (list(triggers["action"]) if "action" in columns
                else [None] * len(t_bars))
    out: list[bool] = []
    for i, a_raw, act in zip(t_bars, t_anchor, t_action):
        origin = i
        if act in _ANCHOR_ACTIONS:            # A2/A4：局面窗从 anchor 起
            try:
                anc = int(a_raw)
            except (TypeError, ValueError):
                anc = i
            if 0 <= anc < n:
                origin = anc
        if situation == "S2":
            if not 0 <= origin < n:
                raise ValueError(
                    f"anchor/bar 越界：origin={origin}（序列长 {n}）")
            w = s2_starts[origin]
        else:
            w = situation_window_start(origin, situation, bars,
                                       approach_lookback, s3_bars)
        ok = (not suppress_across_gaps(origin, bars, gap_spans, w)) \
            and i >= int(feature_warmup)
        if ok and hold_bars:
            j = min(i + int(hold_bars), n - 1)
            ok = not suppress_across_gaps(j, bars, wide, i)
        out.append(bool(ok))
    return out


# ---------------------------------------------------------------- era 判定

def mt5_first_time(data_root, symbol, timeframe, *, bars=None,
                   source_tags=None):
    """该品种 MT5 段首 bar 时刻（era 判定基准，计划 v2 波次 2）。

    优先用 **merge 实测**（T98 audit P-09）：给出 merged_research_series
    的 (bars, source_tags) → 首个 tag == "mt5" 的 bar 时刻（按数据实测，
    不信水位——与 merge 的前向延长过滤同口径；此时不再读 meta.json，
    即使 meta.first_time 与实测不一致）。tags 在场但无 "mt5"（全 ext
    序列）→ None：era 边界不可得，声明了 cost_scenario_mult 的调用方
    应拒绝（p1.run_p1_card 即此——不静默按 1× 计 ext 段）。

    未给 source_tags → 旧路径：{data_root}/bars/{symbol}/{tf}/meta.json
    的 first_time；缺 meta/缺字段 → None（调用方按"无 ext 段"处理——
    meta 与实测起点可能不一致，走 merge 的调用方应传 tags）。
    """
    if source_tags is not None:
        if bars is None:
            raise ValueError(
                "source_tags 须与 bars 成对给出（merged_research_series "
                "的产出：bars 与等长 source_tags）")
        if len(source_tags) != len(bars):
            raise ValueError(f"source_tags 须与 bars 等长（{len(bars)}），"
                             f"收到 {len(source_tags)}")
        for t, tag in zip(pd.to_datetime(bars["time"]), source_tags):
            if tag == "mt5":
                return pd.Timestamp(t)
        return None
    meta = read_watermark(str(data_root), symbol, str(timeframe).upper()) \
        or {}
    ft = meta.get("first_time")
    return pd.Timestamp(ft) if ft else None
