"""T97 波次 2 + T98 P-09 + T104 P2-03：合并研究序列的洞抑制、统一资格
（EligibilityContext / eligible_bars）与 era 判定 helper（P 线数据解锁批）。

- 抑制口径（计划 v2 先决 4，包含式）：洞 G = {(s, e)}（s = 洞前末 bar、
  e = 洞后首 bar，来自 mt5data.ext.merge 的 gap_spans，>30 天口径）；
  触发 bar i 的回看窗 W = [W首, i]。**抑制 ⇔ ∃(s,e)∈G 使 t[W首] ≤ s 且
  e ≤ t[i]**——等价实现：窗内相邻 bar 对恰为洞两侧（s 恰为窗首 / e 恰为
  触发 bar 的"贴边洞"都算跨洞：回看窗仍横跨了洞；洞止于窗首（e = t[W首]，
  洞在窗前）或始于触发 bar（s = t[i]，洞在触发后）不抑制）。
- 窗首按局面（先决 4）：S1 → max(0, i−M)（M = approach.lookback_bars）；
  S3 → max(0, i−480)；S2 → 前一个**有 bar**会话的首 bar（17:00 NY 划界，
  与 strategies.library.prob.situations.s2_level_rows 同口径）。
- 统一资格（T98 P-09 + **T104 P2-03 补漏**）：eligibility_mask /
  EligibilityContext.eligible_bars 在局面回看窗之外再查——
  **特征回看窗**（i ≥ warmup 且 [i−warmup, i] 不跨洞：atr_pct_2000 的
  2000 根排名窗跨洞时档位失真）、**A2/A4 的 anchor→action 窗**（[anchor,
  触发根] 不跨洞）、**未来窗 t+K+1 完整可观察**（labels.py 的实际需要：
  信号出场在 bar t+K+1 的开盘——不是 t+K；窗 [t, t+K+1] 还不得跨宽洞）。
  counting 与 p1 共用——真实触发与随机对照同一合格口径；逐原因记录剔除
  （exclusion_reasons）。
- era 判定（计划 v2 波次 2）：bar 时刻 < MT5 段首 bar 时刻 → ext 段
  （早年外部数据）；≥ → MT5 段。首 bar 时刻优先用 merge 的
  source_tags 实测（P-09：不信水位），旧路径读 bars/ 目录 meta.json
  的 first_time。

评估代码（D45：研究角色只读）。不 import 标签/引擎（计数侧结构保证，
tests/test_prob_counting.py 的静态检查口径）。
"""
from __future__ import annotations

from dataclasses import dataclass

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

# T104 P2-03：统一资格的规则版本（资格口径变化时递增——计数/P1/标定/
# 拟合的资格报告随身份携带；v2 = 补 anchor→action 窗、特征窗跨洞、
# t+K+1 完整未来窗）
ELIGIBILITY_VERSION = "eligibility-v2"

# P2-03 逐原因剔除码（exclusion_reasons 的取值；报告口径与代码共用）
REASON_FEATURE_WARMUP = "feature_warmup"      # 触发根 < 特征预热
REASON_FEATURE_GAP = "feature_gap"            # 特征回看窗跨洞
REASON_SITUATION_GAP = "situation_gap"        # 局面回看窗跨洞（先决 4）
REASON_ANCHOR_ACTION_GAP = "anchor_action_gap"  # A2/A4 的 anchor→action 窗跨洞
REASON_FUTURE_WINDOW = "future_window"        # t+K+1 不存在（未来窗不完整）
REASON_FUTURE_GAP = "future_gap"              # 未来窗跨宽洞（> max_gap_days）
REASON_ERA = "era_unavailable"                # 要求 era 而边界不可得（fail-closed）

__all__ = ["S3_WINDOW_BARS", "FEATURE_WARMUP_BARS", "ELIGIBILITY_VERSION",
           "mt5_first_time", "situation_window_start", "suppression_mask",
           "suppress_across_gaps", "eligibility_mask", "eligibility_reasons",
           "EligibilityContext", "eligible_bars"]


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


def _eligibility_of(i, *, bars, n, gap_spans, wide, feature_warmup,
                    hold_bars, origin, window_start,
                    anchor_action_end=None) -> tuple[bool, str]:
    """单触发的统一资格 → (合格, 剔除原因；合格为 "")。

    P2-03 全部窗（与 EligibilityContext.eligible_bars 同一核心）：
    1. 特征：触发根 ≥ feature_warmup 且特征回看窗 [max(0, i−warmup), i]
       不跨洞（gap_spans 原样；0 = 不检查）；
    2. 局面回看窗 [window_start, origin] 不跨洞（先决 4；A2/A4 的
       origin=anchor，由调用方给出 window_start）；
    3. anchor→action 窗 [origin, anchor_action_end] 不跨洞（P2-03 新增：
       A2/A4 在 anchor 之后触发，anchor 与触发根之间的洞使局面/动作口径
       脱节；anchor_action_end=None = 不查）；
    4. 未来窗（hold_bars > 0）：bar t+K+1 须存在（labels.py:92——信号
       出场发生在 t+K+1 的开盘，该根 open 是标签用到的最后信息；不是
       t+K）且 [i, i+K+1] 不跨宽洞（wide = > max_gap_days 的洞）。
    """
    warm = int(feature_warmup)
    if warm and i < warm:
        return False, REASON_FEATURE_WARMUP
    if warm and suppress_across_gaps(i, bars, gap_spans, max(0, i - warm)):
        return False, REASON_FEATURE_GAP
    if suppress_across_gaps(origin, bars, gap_spans, int(window_start)):
        return False, REASON_SITUATION_GAP
    if anchor_action_end is not None and suppress_across_gaps(
            int(anchor_action_end), bars, gap_spans, origin):
        return False, REASON_ANCHOR_ACTION_GAP
    if hold_bars:
        end = i + int(hold_bars) + 1
        if end > n - 1:
            return False, REASON_FUTURE_WINDOW
        if suppress_across_gaps(end, bars, wide, i):
            return False, REASON_FUTURE_GAP
    return True, ""


def eligibility_reasons(triggers, bars, gap_spans, situation,
                        approach_lookback, s3_bars: int = S3_WINDOW_BARS,
                        feature_warmup: int = FEATURE_WARMUP_BARS,
                        hold_bars: int = 0,
                        max_gap_days: int = 30) -> list[str]:
    """eligibility_mask 的逐原因版（P2-03"逐原因记录剔除"）：每个触发行
    的剔除原因（""=合格；原因码见模块 REASON_* 常量），口径/参数与
    eligibility_mask 逐位一致。p1/P1 报告与计数侧消费（乙的
    _eligibility_drop_reasons 重算可退役，改消费本函数）。
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
    out: list[str] = []
    for i, a_raw, act in zip(t_bars, t_anchor, t_action):
        origin = i
        anchor_action_end = None
        if act in _ANCHOR_ACTIONS:            # A2/A4：局面窗从 anchor 起
            try:
                anc = int(a_raw)
            except (TypeError, ValueError):
                anc = i
            if 0 <= anc < n:
                origin = anc
                anchor_action_end = i         # P2-03：anchor→action 窗
        if situation == "S2":
            if not 0 <= origin < n:
                raise ValueError(
                    f"anchor/bar 越界：origin={origin}（序列长 {n}）")
            w = s2_starts[origin]
        else:
            w = situation_window_start(origin, situation, bars,
                                       approach_lookback, s3_bars)
        ok, reason = _eligibility_of(
            i, bars=bars, n=n, gap_spans=gap_spans, wide=wide,
            feature_warmup=feature_warmup, hold_bars=hold_bars,
            origin=origin, window_start=w, anchor_action_end=anchor_action_end)
        out.append(reason)
    return out


def eligibility_mask(triggers, bars, gap_spans, situation, approach_lookback,
                     s3_bars: int = S3_WINDOW_BARS,
                     feature_warmup: int = FEATURE_WARMUP_BARS,
                     hold_bars: int = 0,
                     max_gap_days: int = 30) -> list[bool]:
    """P-09 + P2-03 共同可用集合：每个触发行是否合格（counting 与 p1 共用）。

    合格 ⇔ `_eligibility_of` 的全部窗成立：
    1. **局面回看窗不跨洞**：窗首按局面（先决 4：S1=max(0, origin−M)、
       S3=max(0, origin−s3_bars)、S2=前一个有 bar 会话的首 bar）。origin
       对 **A2/A4 是 anchor 根**（局面依赖从 anchor 起——audit P-09：从
       action bar 回看会遗漏 anchor 前窗口），其余动作是触发根；anchor
       列缺失/非数/越界 → 退回触发根。洞集合 = gap_spans 原样（先决 4
       口径；merge 的 gap_spans 本就是 >30 天洞）。
    2. **特征回看窗**（P2-03 补洞检查）：触发根 ≥ feature_warmup（缺省
       2000 = atr_pct_2000 的排名窗；0 = 不检查）**且** [i−warmup, i]
       不跨洞（排名窗横跨洞 → ATR 分位档失真）。
    3. **anchor→action 窗**（P2-03 新增）：A2/A4 的 [anchor, 触发根]
       不跨洞（anchor 列缺失/退回触发根时该窗退化为 [i, i] 恒不跨）。
    4. **未来窗 t+K+1 完整可观察**（hold_bars > 0 时；P2-03 对齐
       labels.py:92 的实际需要——信号出场在 bar t+K+1 的开盘）：bar
       t+K+1 须存在（旧实现把窗截到末根仍判合格）且 [t, t+K+1] 不跨
       **宽**洞（gap_spans 中宽 > max_gap_days 的对；调用方可收紧/放宽
       洞宽阈值，恰 max_gap_days 天宽的不算洞）。
    triggers 为空 → []；未知局面 → ValueError。返回与 triggers 等长的
    bool 列表（True=合格）；逐原因版本见 eligibility_reasons（口径一致）。
    """
    return [r == "" for r in eligibility_reasons(
        triggers, bars, gap_spans, situation, approach_lookback,
        s3_bars=s3_bars, feature_warmup=feature_warmup, hold_bars=hold_bars,
        max_gap_days=max_gap_days)]


# ------------------------------------------------- 统一资格（T104 P2-03）

def _norm_gaps(gap_spans) -> tuple[tuple[pd.Timestamp, pd.Timestamp], ...]:
    """洞集合规范化（None/空 → ()；成员转 Timestamp 对）。"""
    if not gap_spans:
        return ()
    return tuple((pd.Timestamp(s), pd.Timestamp(e)) for s, e in gap_spans)


@dataclass(frozen=True)
class EligibilityContext:
    """P2-03 统一资格上下文：计数 / P1 / 标定 / 拟合共用的**同一**资格判定
    配置与规则版本（audit P2-03：不再各入口各查一套窗）。

    窗口径见 `_eligibility_of`（特征回看 + 局面回看 + A2/A4 anchor→action
    + t+K+1 完整未来窗 + 宽洞阈值）。era（数据时代/来源）：mt5_first_time
    给出 MT5 段首 bar 时刻，era_of 逐 bar 分类 ext/mt5；require_era=True
    而 mt5_first_time 缺失/NaT → 全部剔除（原因 era_unavailable，与 P1
    "声明情景档而 era 边界不可得即拒绝"同口径）。session_policy 只随身份
    携带（禁入窗影响的是成交/未成交——标签阶段产生 skipped——不改变
    **不依赖结局**的 bar 资格；计数/P1/拟合报告据此对齐同一政策版本）。
    """

    feature_warmup: int = FEATURE_WARMUP_BARS
    hold_bars: int = 0
    gap_spans: tuple = ()
    max_gap_days: int = 30
    mt5_first_time: pd.Timestamp | None = None   # era 边界（merge 实测）
    require_era: bool = False                    # 要求 era 边界可得（fail-closed）
    session_policy: object = None                # 入场政策（随身份携带）
    version: str = ELIGIBILITY_VERSION           # 规则版本（资格身份键）

    def era_of(self, times) -> pd.Series:
        """逐时刻的数据时代："ext"（< mt5_first_time）/"mt5"（≥）；边界
        不可得（None/NaT）→ None（require_era 时由 eligible_bars 剔除）。"""
        first = self.mt5_first_time
        unknown = first is None or pd.isna(first)
        ts = pd.to_datetime(pd.Series(list(times))).reset_index(drop=True)
        if unknown:
            return pd.Series([None] * len(ts), dtype="object")
        return pd.Series(
            ["ext" if t < pd.Timestamp(first) else "mt5" for t in ts],
            dtype="object")

    def _era_reason(self) -> str | None:
        if self.require_era and (self.mt5_first_time is None
                                 or pd.isna(self.mt5_first_time)):
            return REASON_ERA
        return None

    def _evaluate(self, bars, *, i, origin, window_start,
                  anchor_action_end) -> tuple[bool, str]:
        era = self._era_reason()
        if era is not None:
            return False, era
        return _eligibility_of(
            i, bars=bars, n=len(bars), gap_spans=self.gap_spans,
            wide=(_wide_gaps(self.gap_spans, self.max_gap_days)
                  if self.hold_bars else ()),
            feature_warmup=self.feature_warmup, hold_bars=self.hold_bars,
            origin=origin, window_start=window_start,
            anchor_action_end=anchor_action_end)

    def eligible_bars(self, bars, *, situation_windows=None,
                      anchor_of=None) -> pd.Series[bool]:
        """逐 bar 资格掩码（与 bars 等长、同 index；True=合格）。

        situation_windows：{局面: 逐 bar 窗首下标数组}——每个给出的局面
        都须合格（窗 [窗首, bar] 不跨洞）；None = 不查局面窗。与 anchor_of
        同用时窗首按 **anchor 语义**由调用方对齐（A2/A4 的窗从 anchor 起
        ——situation_window_start(anchor, …)，如同 eligibility_mask 的
        origin 口径）。
        anchor_of：逐 bar 的 anchor 下标（A2/A4 的 anchor→action 窗
        [anchor, bar] 不跨洞）；None = 不查。两者缺省 None → 只查特征/
        未来窗/era（与 randomize_triggers 的逐 bar 抽样池同型）。
        """
        reasons = self.exclusion_reasons(bars, situation_windows=situation_windows,
                                         anchor_of=anchor_of)
        return reasons == ""

    def exclusion_reasons(self, bars, *, situation_windows=None,
                          anchor_of=None) -> pd.Series[str]:
        """逐 bar 剔除原因（""=合格；原因码见模块常量）——P2-03"逐原因
        记录剔除"的统一出口（计数/P1 报告消费；不按 win/loss 选择样本）。"""
        n = len(bars)
        era = self._era_reason()
        wins = {}
        for sit, starts in (situation_windows or {}).items():
            arr = list(starts)
            if len(arr) != n:
                raise ValueError(
                    f"situation_windows[{sit!r}] 须与 bars 等长（{n}），"
                    f"收到 {len(arr)}")
            wins[sit] = arr
        anchors = None
        if anchor_of is not None:
            anchors = list(anchor_of)
            if len(anchors) != n:
                raise ValueError(
                    f"anchor_of 须与 bars 等长（{n}），收到 {len(anchors)}")
        out: list[str] = []
        for i in range(n):
            if era is not None:
                out.append(era)
                continue
            origin, anchor_end = i, None
            if anchors is not None:
                try:
                    anc = int(anchors[i])
                except (TypeError, ValueError):
                    anc = i
                if 0 <= anc < n and anc != i:
                    origin, anchor_end = anc, i
            # 须合格的局面窗：给出的每个局面窗首 + （无局面窗时）平凡窗
            window_starts = [starts[i] for _sit, starts in wins.items()] \
                or [i]
            reason = ""
            for w in window_starts:
                ok, why = self._evaluate(
                    bars, i=i, origin=origin, window_start=w,
                    anchor_action_end=anchor_end)
                if not ok:
                    reason = why
                    break
            out.append(reason)
        return pd.Series(out, index=bars.index, dtype="object")


def eligible_bars(bars, *, feature_warmup, hold_bars, gap_spans,
                  situation_windows=None, anchor_of=None,
                  max_gap_days: int = 30) -> pd.Series[bool]:
    """P2-03 统一资格接口（**模块乙/丁消费；签名写死**）：
    eligible_bars(bars, *, feature_warmup, hold_bars, gap_spans,
    situation_windows=None, anchor_of=None) -> pd.Series[bool]

    与 randomize_triggers 的 eligible_bars 参数（布尔掩码抽样池）兼容的
    生成器：逐 bar 判定（True=该 bar 可作为触发/抽样时刻）。覆盖 P2-03
    全部窗——特征回看窗（含跨洞）、局面回看窗（situation_windows 逐局面
    窗首）、A2/A4 的 anchor→action 窗（anchor_of）、未来窗 t+K+1 完整
    可观察（hold_bars=K）+ 宽洞阈值（max_gap_days）。完整口径/era/政策
    组装用 EligibilityContext（本函数为其窗判定的薄封装）。
    """
    return EligibilityContext(
        feature_warmup=int(feature_warmup), hold_bars=int(hold_bars),
        gap_spans=_norm_gaps(gap_spans), max_gap_days=int(max_gap_days),
    ).eligible_bars(bars, situation_windows=situation_windows,
                    anchor_of=anchor_of)


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
