"""低频事件模式（蓝图 v4 §6.4、计划 T66/D35）：事件窗构造、净收益的
事前成本口径、年块自助单侧检验、事件时间随机化零假设与植入正对照。

适用对象：日历与资金流效应（每年约 12 次事件）、月度/周度再平衡类——
**评估单位是事件**，主统计量为事件净收益均值的单侧检验（均值 > 0），
按年块自助；同一机制的多个品种合并时**同一天按日聚合**后再检验
（跨品种同日相关性的简化处理：折叠成单一观测）。

成本口径（与 T62 预筛 `_ex_ante_cost_per_symbol` 同一份语义，铁律：
成本模型只有一份口径）：
- 点差 p90 是**来回点差全幅、每来回恰收一次**——方向未定时取入/出场
  两小时中较不利的一侧（保守）；
- 滑点开、平两侧各扣一次；
- 佣金为每手来回佣金 ÷ 1 手合约价值（contract_size × 参考价）；
- 隔夜费取 long/short 每夜费率中更不利的一侧 × **窗口实际计费夜数**
  （SwapRule 计费时刻/DST/三倍日规则，charge_nights 同一实现）。参考价 =
  序列最后一根收盘价（event_returns 的 reference_price）——每手点数费率
  是当前价格水平的绝对金额，跨数十年价格水平差百倍的序列必须按相对
  比率外推（与相对点差模型同一理由），否则早年隔夜费被放大成每夜百分之
  几；点差/滑点已由相对模型随成交价缩放，不受此影响。

零假设与正对照（标定框架，scripts/gate_calibration.py 事件模式对照臂）：
- 零假设 = **事件时间随机化**：randomize_event_times 保持事件层的星期
  分布（严格）与月内位置（三分位分桶，尽力分层——池不足时逐级放宽），
  抽样与原事件窗**不重叠**（matched 抽样池：无效应时的可比时点）；
- 正对照 = plant_event_effect 在事件窗出场端植入已知相对漂移 δ
  （入场 bar 不动 → 入场价不变、出场价 ×(1±δ)，事件毛收益 ≈ ±δ）。

检验力与样本量（取代 G1 的固定笔数，蓝图 §6.4）：event_power /
required_events 用 Lo 2002 近似的事件口径——每事件夏普 SR = 每事件净
效应 ÷ 窗口波动 σ_w，SE = sqrt((1+SR²/2)/n)，检验力 = Φ(SR/SE − z_α)；
z 值一律取 mt5quant.research.power 的同一函数（千分位/万分位约定，
不各自重算分位数产生口径漂移）。样本量规则（D35 修改 4/T84）：折算后
预期净效应下检验力 ≥ 50% 所需事件数有闭式 n = (z_{α/N}+z_p)²·(1+SR²/2)/
SR²——多重校正按本轮候选数 N（Bonferroni α/N；预筛仍按单次判定）。

随机化参照检验（randomization_test）：p = (1 + #{随机化统计量 ≥ 观测})
/ (sims+1)。相对年块自助（event_test）它是漂移稳健的——观测均值要与
matched 随机化零分布比，而不是与 0 比；两者都在标定报告里分列
（D35 素材，关卡 v3 事件模式主检验的选择留 T65）。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from mt5quant.costs.swap import charge_nights
from mt5quant.research.power import _norm_ppf, _phi, _z_alpha

__all__ = [
    "EventWindow", "month_end_events", "london_fix_events",
    "event_returns", "event_test", "randomize_event_times",
    "randomization_test", "aggregate_daily", "window_volatility",
    "plant_event_effect", "event_power", "required_events",
]

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_MONTH_BUCKETS = 3          # 月内位置分桶数（三分位：早/中/晚）


@dataclass(frozen=True)
class EventWindow:
    """一个事件窗（naive UTC；入场/出场均为**成交参考时刻**）。

    kind：事件机制标识（"month_end" / "london_fix" / 自定义）；
    label：唯一标识（随机化事件加 rand 前缀防撞）；
    entry_time/exit_time：入场/出场的最早成交时刻——event_returns 取
    该时刻之后首根 bar 的开盘价成交。
    """

    kind: str
    label: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp


# ---------------------------------------------------------------- 日历工具

def _normalize_days(trading_days) -> pd.DatetimeIndex:
    """交易日历 → 升序去重的零点 naive UTC 日期索引（向量化：np.unique，
    不逐元素装箱）。"""
    idx = pd.DatetimeIndex(pd.to_datetime(trading_days))
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    values = np.unique(idx.normalize().to_numpy())
    if len(values) == 0:
        raise ValueError("交易日历为空——事件无从构造")
    return pd.DatetimeIndex(values)


def _months_of(days: pd.DatetimeIndex) -> list[tuple[tuple[int, int],
                                                     np.ndarray]]:
    """(年, 月) → 交易日索引数组（保持日历顺序；向量化找月边界）。"""
    ym = days.year.to_numpy(dtype="int64") * 12 + days.month.to_numpy()
    change = np.flatnonzero(np.diff(ym)) + 1
    starts = np.concatenate(([0], change))
    ends = np.concatenate((change, [len(days)]))
    out = []
    for s, e in zip(starts.tolist(), ends.tolist()):
        key = (int(days[s].year), int(days[s].month))
        out.append((key, np.arange(s, e, dtype="int64")))
    return out


def _next_month(key: tuple[int, int]) -> tuple[int, int]:
    y, m = key
    return (y + 1, 1) if m == 12 else (y, m + 1)


# ---------------------------------------------------------------- 事件构造

def month_end_events(trading_days, *, last_n: int = 1,
                     first_n: int = 3) -> list[EventWindow]:
    """月末（换月）事件窗：每月最后 last_n 根交易日 + 次月前 first_n 根。

    - 入场 = 本月倒数第 last_n 根交易日（last_n=1 → 月末最后一根；
      周末/节假日不在交易日历上，自然跳过——事件永远落在真实交易日）；
    - 出场 = **次月**第 first_n 根交易日（次月交易日不足 first_n、或样本
      末月没有次月 → 该月不出事件）；
    - 时刻取交易日零点：event_returns 解析为当日首根 bar 的开盘成交。
    """
    if not isinstance(last_n, int) or last_n < 1:
        raise ValueError(f"last_n 须为 ≥1 的整数，收到 {last_n!r}")
    if not isinstance(first_n, int) or first_n < 1:
        raise ValueError(f"first_n 须为 ≥1 的整数，收到 {first_n!r}")
    days = _normalize_days(trading_days)
    months = _months_of(days)
    index = dict(months)
    events: list[EventWindow] = []
    for key, idxs in months[:-1]:
        nxt = index.get(_next_month(key))
        if nxt is None or len(nxt) < first_n or len(idxs) < last_n:
            continue                     # 次月交易日不足 / 日历断月 → 不出事件
        i_entry = int(idxs[-last_n])
        i_exit = int(nxt[first_n - 1])
        events.append(EventWindow(
            kind="month_end", label=f"month_end:{key[0]}-{key[1]:02d}",
            entry_time=days[i_entry], exit_time=days[i_exit]))
    return events


def london_fix_events(trading_days, *, tz: str = "Europe/London",
                      hhmm: str = "16:00",
                      lead_minutes: int = 60) -> list[EventWindow]:
    """定盘类事件窗：trading_days 每个交易日的 tz 本地 hhmm 时刻定盘，
    入场提前 lead_minutes（伦敦 16:00 定盘 = C002 的机制锚点）。

    时区换算逐事件经 zoneinfo（DST 由 tz 数据库处理：欧洲 3 月末切夏令时
    16:00 BST=15:00 UTC、10 月末切回 16:00 GMT=16:00 UTC）；返回 naive UTC。
    只对交易日历上的日子出事件（周末/节假日定盘不在日历上，自然跳过）。
    月末定盘类事件 = 先筛出月末交易日再传入（组合使用）。
    """
    m = _HHMM.match(str(hhmm))
    if not m:
        raise ValueError(f"hhmm 须形如 'HH:MM'（24 小时制），收到 {hhmm!r}")
    if not isinstance(lead_minutes, int) or lead_minutes < 0:
        raise ValueError(f"lead_minutes 须为 ≥0 的整数，收到 {lead_minutes!r}")
    try:
        zone = ZoneInfo(str(tz))
    except Exception as e:      # zoneinfo 对未知时区抛 KeyError 等
        raise ValueError(f"未知时区 {tz!r}：{e}") from e
    hh, mm = int(m.group(1)), int(m.group(2))
    lead = pd.Timedelta(minutes=int(lead_minutes))
    days = _normalize_days(trading_days)
    events: list[EventWindow] = []
    for d in days:
        local = datetime(d.year, d.month, d.day, hh, mm, tzinfo=zone)
        fix_utc = pd.Timestamp(local).tz_convert("UTC").tz_localize(None)
        events.append(EventWindow(
            kind="london_fix", label=f"london_fix:{d.date().isoformat()}",
            entry_time=fix_utc - lead, exit_time=fix_utc))
    return events


# ---------------------------------------------------------------- 净收益

def _side_sign(side) -> int:
    if isinstance(side, str):
        s = side.strip().lower()
        if s in ("long", "buy", "+1"):
            return 1
        if s in ("short", "sell", "-1"):
            return -1
    if side in (1, 1.0, True):
        return 1
    if side in (-1, -1.0):
        return -1
    raise ValueError(f"side_fn 须返回 'long'/'short'（或 ±1），收到 {side!r}")


def event_returns(bars, events, *, side_fn: Callable[[EventWindow], object],
                  costs, symbol: str,
                  contract_size: float | None = None,
                  reference_price: float | None = None) -> pd.DataFrame:
    """每事件净收益（相对价格）：毛收益 − 来回成本 − 隔夜费。

    成交口径：入场 = entry_time 之后首根 bar 的开盘价、出场 = exit_time
    之后首根 bar 的开盘价（searchsorted；出场 bar 须严格晚于入场 bar），
    出场越过数据末尾的事件剔除（attrs["dropped"] 计数，绝不静默外推）。
    成本按事件入场/出场 bar 的**纽约本地小时**取点差/滑点（DST 感知），
    口径见模块 docstring（与 T62 预筛同一份语义）。

    contract_size：佣金/隔夜费从每手金额折成相对价格需要它；两者都为零
    时可不传，任一非零而不传 → ValueError（fail-closed）。

    reference_price：隔夜费率的折算基准（缺省 = 序列最后一根收盘价）。
    点数/每手计的隔夜费是**当前价格水平**的绝对金额——跨 40 年价格差
    190 倍的序列上按事件当时价格折会严重失真（1986 年的名义值被放大成
    每夜百分之几），按参考价折成相对比率再乘计费夜数（与相对点差模型
    "近期结构外推到历史价格水平"同一理由，蓝图 §6.5 早年成本口径）。
    """
    times = pd.to_datetime(bars["time"])
    if not times.is_monotonic_increasing:
        order = np.argsort(times.to_numpy(), kind="stable")
        times = times.iloc[order].reset_index(drop=True)
        bars = bars.iloc[order].reset_index(drop=True)
    arr = times.to_numpy()
    opens = pd.to_numeric(bars["open"]).to_numpy(dtype="float64")
    closes = pd.to_numeric(bars["close"]).to_numpy(dtype="float64")
    # 纽约本地小时（T20 引擎口径）向量化换算：逐 bar tz 循环在长序列上
    # 是主要热点（一次 tz_localize+tz_convert 完成全部 bar）
    hours_ny = (times.dt.tz_localize("UTC")
                .dt.tz_convert("America/New_York").dt.hour.to_numpy(
                    dtype="int64"))
    swap_model = getattr(costs, "swap", None)
    ref_price = (float(reference_price) if reference_price is not None
                 else float(closes[-1]))

    rows: list[dict] = []
    dropped = 0
    for e in events:
        i_e = int(np.searchsorted(arr, e.entry_time.to_datetime64(), "left"))
        i_x = int(np.searchsorted(arr, e.exit_time.to_datetime64(), "left"))
        if i_e >= len(arr) or i_x >= len(arr) or i_x <= i_e:
            dropped += 1
            continue
        sign = _side_sign(side_fn(e))
        entry_ts = pd.Timestamp(arr[i_e])   # 直接从 numpy 取（iloc 逐条太慢）
        exit_ts = pd.Timestamp(arr[i_x])
        entry, exit_ = float(opens[i_e]), float(opens[i_x])
        if not (entry > 0.0 and exit_ > 0.0):
            dropped += 1
            continue
        gross = sign * (exit_ / entry - 1.0)
        h_e, h_x = int(hours_ny[i_e]), int(hours_ny[i_x])
        # 点差：来回全幅一次，入/出场小时较不利侧（T62 同口径）
        spr_e = costs.spread_p90(symbol, h_e, price=entry)
        spr_x = costs.spread_p90(symbol, h_x, price=entry)
        spread = max(spr_e, spr_x) / entry
        slip = (costs.slippage(symbol, h_e, price=entry)
                + costs.slippage(symbol, h_x, price=entry)) / entry
        comm_lot = float(costs.commission_per_lot(symbol))
        nights: int | None = None
        worst_rate = 0.0
        if swap_model is not None:
            rule = swap_model.rule(symbol)
            nights = int(charge_nights(entry_ts, exit_ts, rule))
            worst_rate = max(abs(float(rule.long_per_day)),
                             abs(float(rule.short_per_day)))
        if (comm_lot > 0.0 or worst_rate > 0.0) and contract_size is None:
            raise ValueError(
                f"{symbol} 的佣金/隔夜费非零，折算相对价格需要 contract_size"
                f"（每手金额 ÷ contract_size×入场价）——不传则静默按 0，"
                "违反成本口径（与 T62 预筛同一语义）")
        notional = (float(contract_size) * ref_price
                    if contract_size is not None else 0.0)
        commission = comm_lot / notional if notional > 0.0 else 0.0
        overnight = ((nights or 0) * worst_rate / notional
                     if notional > 0.0 else 0.0)
        cost = spread + slip + commission + overnight
        rows.append({
            "label": e.label, "kind": e.kind, "side": "long" if sign > 0
            else "short",
            "entry_time": e.entry_time, "exit_time": e.exit_time,
            "entry_bar_time": entry_ts, "exit_bar_time": exit_ts,
            "entry_price": entry, "exit_price": exit_,
            "gross": gross, "spread": spread, "slippage": slip,
            "commission": commission, "overnight": overnight,
            "nights": nights, "cost": cost, "net": gross - cost,
        })
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.set_index("label")
    frame.attrs["dropped"] = dropped
    frame.attrs["symbol"] = str(symbol)
    return frame


def aggregate_daily(frames: Mapping[str, pd.DataFrame]) -> pd.Series:
    """多品种合并（相关性调整的简化实现）：同一机制的各品种事件收益，
    **同一天（按入场日）等权聚合成一个观测**后再检验——跨品种同日相关
    被折叠，不会按独立观测虚增样本量。

    frames：{symbol: event_returns 的产出}（entry_time + net 列）；返回
    以入场日为 DatetimeIndex 的日聚合净收益序列（event_test 直接可检）。
    """
    parts = []
    for symbol, frame in frames.items():
        if frame is None or len(frame) == 0:
            continue
        parts.append(pd.DataFrame({
            "symbol": str(symbol),
            "day": pd.DatetimeIndex(frame["entry_time"]).normalize(),
            "net": pd.to_numeric(frame["net"]).to_numpy(),
        }))
    if not parts:
        return pd.Series(dtype="float64", name="net",
                         index=pd.DatetimeIndex([], name="day"))
    all_rows = pd.concat(parts, ignore_index=True)
    daily = all_rows.groupby("day")["net"].mean().sort_index()
    daily.name = "net"
    return daily


# ---------------------------------------------------------------- 检验

def _blocks_of(returns) -> tuple[np.ndarray, list]:
    """收益 → (值数组, 年块标签列表)；DataFrame 用 entry_time，带
    DatetimeIndex 的序列用索引。块内保持原顺序。"""
    if isinstance(returns, pd.DataFrame):
        if "net" not in returns.columns:
            raise ValueError(f"returns DataFrame 缺 'net' 列（event_returns "
                             f"产出），收到列 {list(returns.columns)}")
        values = pd.to_numeric(returns["net"]).to_numpy(dtype="float64")
        if "entry_time" in returns.columns:
            keys = pd.DatetimeIndex(pd.to_datetime(returns["entry_time"]))
        else:
            keys = pd.DatetimeIndex(pd.to_datetime(returns.index))
    else:
        s = pd.Series(returns)
        values = s.astype("float64").to_numpy()
        idx = pd.DatetimeIndex(pd.to_datetime(s.index))
        keys = idx
    if len(values) == 0:
        raise ValueError("收益序列为空——检验无定义")
    labels = keys.year
    order = np.argsort(labels.to_numpy(), kind="stable")
    return values[order], list(labels[order])


def event_test(returns, *, block: str = "year", sims: int = 1000,
               seed: int = 0) -> dict:
    """事件净收益均值的单侧检验（H1: 均值 > 0），按年块自助。

    零分布：年份块有放回重采样（块内结构保留——事件收益的年内相关、
    跨品种同日聚合后的残差相关都按年折叠），p = 重采样均值 ≤ 0 的比例。
    注意两点（标定实测，见 D35 报告）：
    - p 与 0 比较：**漂移资产上未中心化的均值>0 检验会把漂移当效应**
      ——漂移稳健的 randomization_test 分列报告，关卡 v3 主检验选择留 T65；
    - 年块数 B 小时百分位自助的经验水平偏高（有效临界分布 ≈ t_{B−1}
      而非 z：合成 iid 实测 B=6→约 9.5%、12→约 7.8%、20→约 6.1%、
      40→约 5.05%）——事件模式按年块检验需要长历史（≥ 40 年接近名义），
      或改用随机化参照检验。

    block 目前只实现 "year"（蓝图 §6.4 按年块自助）；年块 < 2 时自助
    零分布退化（恒等观测均值）→ ValueError，fail-closed。
    """
    if block != "year":
        raise ValueError(f"block 只支持 'year'（蓝图 §6.4 按年块自助），"
                         f"收到 {block!r}")
    if not isinstance(sims, int) or sims < 2:
        raise ValueError(f"sims 须为 ≥2 的整数，收到 {sims!r}")
    values, labels = _blocks_of(returns)
    uniq = sorted(set(labels))
    if len(uniq) < 2:
        raise ValueError(f"年块数 {len(uniq)} < 2——年块自助零分布退化"
                         "（重采样恒等观测），拒绝检验（fail-closed）")
    groups = [values[[i for i, lb in enumerate(labels) if lb == y]]
              for y in uniq]
    sums = np.asarray([g.sum() for g in groups], dtype="float64")
    counts = np.asarray([len(g) for g in groups], dtype="float64")
    observed = float(values.mean())
    rng = np.random.default_rng(int(seed))
    idx = rng.integers(0, len(groups), size=(int(sims), len(groups)))
    boot = sums[idx].sum(axis=1) / counts[idx].sum(axis=1)
    p = float(np.mean(boot <= 0.0))
    return {"p_value": p, "mean": observed, "n_events": int(len(values)),
            "n_blocks": len(uniq), "blocks": [int(y) for y in uniq],
            "block": "year", "sims": int(sims), "seed": int(seed)}


# ---------------------------------------------------------------- 随机化

def _day_strata(days: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray]:
    """每个交易日的（星期, 月内三分位桶）数组（向量化，供分层掩码用）。

    桶 = 月内交易日序（从月初数）× 3 ÷ 当月交易日数，夹在 [0, 2]——
    月末事件落在"晚"桶；保持分桶的边缘分布即保持粗粒度月内位置。
    """
    n = len(days)
    rank = np.empty(n, dtype="int64")
    month_len = np.empty(n, dtype="int64")
    for _key, idxs in _months_of(days):
        rank[idxs] = np.arange(len(idxs))
        month_len[idxs] = len(idxs)
    bucket = np.minimum(_MONTH_BUCKETS - 1,
                        rank * _MONTH_BUCKETS // np.maximum(1, month_len))
    return days.weekday.to_numpy(dtype="int64"), bucket


def randomize_event_times(events, trading_days, *,
                          seed: int) -> list[EventWindow]:
    """事件时间随机化（标定零假设：matched 抽样池）。

    保持：事件数、窗长（交易日步数）、日内时刻偏移、入场**星期分布**
    （严格——同星期池内抽样）与**月内位置**（三分位桶尽力分层）；
    破坏：与原事件时间的对齐。抽样池与**原事件窗不重叠**（无效应时
    "别的可比时点"——植入在原窗的效应对随机化检验不可见，植入效应
    消失）。池不足时逐级放宽：同星期任意桶（仍不重叠）→ 同桶放回抽样
    （保层数、允许重叠，如实降级）。

    事件入场/出场日不在交易日历上（数据边缘）原样保留——event_returns
    对它们同样剔除，两侧口径一致。
    """
    days = _normalize_days(trading_days)
    day_values = days.to_numpy()              # 日期查位走 searchsorted（不做
    weekday_arr, bucket_arr = _day_strata(days)   # 万级 Timestamp 哈希字典）
    n = len(days)

    def _day_index(ts: pd.Timestamp) -> int:
        v = ts.normalize().to_datetime64()
        i = int(np.searchsorted(day_values, v))
        return i if i < n and day_values[i] == v else -1

    events = list(events)
    # seq → (事件, 入场索引, 出场索引)；日历外的边缘事件原样保留
    resolvable: list[tuple[int, EventWindow, int, int]] = []
    for seq, e in enumerate(events):
        a, b = _day_index(e.entry_time), _day_index(e.exit_time)
        if a >= 0 and b >= 0:
            resolvable.append((seq, e, a, b))
    orig_spans = [(a, b) for _seq, _e, a, b in resolvable]

    def _blocked_days(hold: int) -> np.ndarray:
        """与任何原事件窗重叠的候选锚点掩码：候选窗 [c, c+hold] 与
        [a, b] 重叠 ⟺ c ∈ [a−hold, b]（按窗标记，O(spans×窗长)——比逐
        候选 × 逐窗比较快三个量级）。"""
        mask = np.zeros(n, dtype=bool)
        for a, b in orig_spans:
            mask[max(0, a - hold):b + 1] = True
        return mask

    rng = np.random.default_rng(int(seed))
    used: set[int] = set()                     # 层内不放回：已用锚点
    picks: dict[int, tuple[int, int]] = {}     # seq → (new_a, new_b)
    by_stratum: dict[tuple, list[tuple[int, int, int]]] = {}
    for seq, e, a, b in resolvable:
        wd, bucket = int(weekday_arr[a]), int(bucket_arr[a])
        by_stratum.setdefault((wd, bucket, b - a), []).append((seq, a, b))
    blocked_cache: dict[int, np.ndarray] = {}
    fit_cache: dict[int, np.ndarray] = {}
    for (wd, bucket, hold), items in sorted(by_stratum.items()):
        if hold not in blocked_cache:
            blocked_cache[hold] = _blocked_days(hold)
        blocked = blocked_cache[hold]
        if hold not in fit_cache:
            fit_cache[hold] = np.arange(n) < n - hold   # i + hold < n
        fit = fit_cache[hold]
        in_stratum = (weekday_arr == wd) & (bucket_arr == bucket)
        same_weekday = weekday_arr == wd
        free = ~blocked
        clean_mask = in_stratum & fit & free
        clean_wd_mask = same_weekday & fit & free
        if used:                               # 层内不放回：去掉已用锚点
            for i in used:
                clean_mask[i] = clean_wd_mask[i] = False
        clean = np.flatnonzero(clean_mask)
        clean_wd = np.flatnonzero(clean_wd_mask)
        need = len(items)
        # 降级链：同层不重叠 → 同星期不重叠 → 同层放回（可重叠）
        source = (clean if len(clean) >= need
                  else clean_wd if len(clean_wd) >= need
                  else np.flatnonzero(in_stratum & fit))
        if len(source) == 0:
            continue                           # 无池：原样保留该层
        chosen = rng.choice(np.asarray(source, dtype="int64"), size=need,
                            replace=len(source) < need)
        for (seq, _a, _b), c in zip(items, chosen):
            c = int(c)
            picks[seq] = (c, c + hold)
            used.add(c)
    out: list[EventWindow] = []
    for seq, e in enumerate(events):
        if seq not in picks:
            out.append(e)                      # 边缘事件/无池：原样
            continue
        new_a, new_b = picks[seq]
        e_off = e.entry_time - e.entry_time.normalize()
        x_off = e.exit_time - e.exit_time.normalize()
        out.append(EventWindow(
            kind=e.kind, label=f"{e.kind}:rand{seed}:{seq}",
            entry_time=pd.Timestamp(day_values[new_a]) + e_off,
            exit_time=pd.Timestamp(day_values[new_b]) + x_off))
    return out


def randomization_test(bars, events, *, side_fn, costs, symbol: str,
                       trading_days=None, sims: int = 999, seed: int = 0,
                       contract_size: float | None = None) -> dict:
    """随机化参照检验（漂移稳健）：观测事件收益均值 vs matched 随机化
    零分布（randomize_event_times 的不重叠抽样池）。

    p = (1 + #{随机化均值 ≥ 观测均值}) / (sims + 1)（保守加一，标准
    随机化检验约定）。随机化种子由 (seed, k) 派生，同种子逐位一致。
    trading_days 缺省用 bars 的交易日历。
    """
    if not isinstance(sims, int) or sims < 1:
        raise ValueError(f"sims 须为 ≥1 的整数，收到 {sims!r}")
    days = (_normalize_days(trading_days) if trading_days is not None
            else _normalize_days(pd.DatetimeIndex(
                pd.to_datetime(bars["time"])).normalize()))
    obs_frame = event_returns(bars, events, side_fn=side_fn, costs=costs,
                              symbol=symbol, contract_size=contract_size)
    if len(obs_frame) == 0:
        raise ValueError("事件全部不可解析——随机化检验无定义")
    observed = float(pd.to_numeric(obs_frame["net"]).mean())
    master = np.random.default_rng(int(seed))
    child_seeds = master.integers(0, 2**63 - 1, size=int(sims))
    null_stats = np.empty(int(sims), dtype="float64")
    for k, cs in enumerate(child_seeds):
        rand = randomize_event_times(events, days, seed=int(cs))
        frame = event_returns(bars, rand, side_fn=side_fn, costs=costs,
                              symbol=symbol, contract_size=contract_size)
        null_stats[k] = (float(pd.to_numeric(frame["net"]).mean())
                         if len(frame) else np.nan)
    valid = null_stats[~np.isnan(null_stats)]
    p = float((1 + int(np.sum(valid >= observed))) / (len(valid) + 1))
    return {"p_value": p, "observed_mean": observed,
            "null_mean": float(valid.mean()) if len(valid) else float("nan"),
            "null_std": float(valid.std(ddof=1)) if len(valid) > 1 else 0.0,
            "n_events": int(len(obs_frame)), "sims": int(len(valid)),
            "seed": int(seed)}


# ---------------------------------------------------------------- 工具

def window_volatility(bars, hold_bars: int,
                      *, entry_hour_ny: int | None = None) -> float:
    """事件窗波动 σ_w（相对价格）：同窗长全样本滚动窗口收益的样本标准差
    （ddof=1）。随机时点口径（与 T80/G2 的 σ 同思想、作用在窗口长度上）
    ——品种性质，不是候选样本内结果；无种子、确定性可复算。

    entry_hour_ny（T84/K4）：给定时段起点时，只取**窗口起点时刻**的
    纽约本地小时等于该值的窗口——窗口起点 = 锚 bar 的收盘时刻（入场
    成交时点：bar 收盘价即下一窗起点的成交参考），DST 由 zoneinfo 处理。
    日内候选的"同一时段起点"（定盘前一小时等时段的波动异于全天平均，
    K4 的 C002 更正）；bar 时长取序列内最小正间隔（日内序列等间隔）。
    过滤后不足 2 个窗口收益 → ValueError（σ 无定义，fail-closed；
    D1 及以上周期全天只有一种起点，调用方不传该参数）。
    """
    if not isinstance(hold_bars, int) or hold_bars < 1:
        raise ValueError(f"hold_bars 须为 ≥1 的整数，收到 {hold_bars!r}")
    if entry_hour_ny is not None and (
            isinstance(entry_hour_ny, bool) or not isinstance(entry_hour_ny, int)
            or not 0 <= entry_hour_ny <= 23):
        raise ValueError(f"entry_hour_ny 须为 0..23 的整数（纽约本地小时）"
                         f"或 None，收到 {entry_hour_ny!r}")
    times = pd.to_datetime(bars["time"])
    closes = pd.to_numeric(bars["close"]).to_numpy(dtype="float64")
    if len(closes) < hold_bars + 2:
        raise ValueError(f"收盘价 {len(closes)} 根不足窗长 {hold_bars} + 2"
                         "——σ_w 无定义")
    anchors = np.arange(len(closes) - hold_bars)
    if entry_hour_ny is not None:
        if len(closes) < 2:
            raise ValueError("序列不足 2 根——bar 时长无从推断，σ_w 无定义")
        diffs = np.diff(times.to_numpy()).astype("timedelta64[s]") \
            .astype("int64")
        pos = diffs[diffs > 0]
        if len(pos) == 0:
            raise ValueError("时间列无正间隔——bar 时长无从推断，σ_w 无定义")
        bar_seconds = int(np.min(pos))
        start_hours = (times + pd.Timedelta(seconds=bar_seconds)) \
            .dt.tz_localize("UTC").dt.tz_convert("America/New_York") \
            .dt.hour.to_numpy(dtype="int64")
        anchors = anchors[start_hours[anchors] == int(entry_hour_ny)]
        if len(anchors) < 2:
            raise ValueError(
                f"时段起点（纽约 {entry_hour_ny} 点）的窗口数 {len(anchors)}"
                " < 2——该时段的 σ_w 无定义（fail-closed，不静默退回全天"
                "平均）")
    rets = closes[anchors + hold_bars] / closes[anchors] - 1.0
    return float(np.std(rets, ddof=1))


def plant_event_effect(bars, events, delta: float, *,
                       side_fn: Callable[[EventWindow], object] | None = None
                       ) -> pd.DataFrame:
    """植入正对照：事件窗**出场端**相对漂移 δ（入场 bar 不动——入场价
    不变，出场价 ×(1 + side×δ)，事件毛收益 ≈ +side×δ）。

    对 (入场 bar, 出场 bar] 的 OHLC 一致缩放；相邻事件窗重叠时会复合
    （月末窗按月相接不重叠；报告注明假设）。返回拷贝，不改入参。
    """
    if not math.isfinite(float(delta)) or float(delta) <= 0.0:
        raise ValueError(f"delta 须为有限正数（相对漂移），收到 {delta!r}")
    out = bars.copy()
    times = pd.to_datetime(out["time"])
    if not times.is_monotonic_increasing:
        raise ValueError("bars 时间须单调递增（植入按索引缩放）")
    arr = times.to_numpy()
    n = len(arr)
    factor = np.ones(n, dtype="float64")
    for e in events:
        i_e = int(np.searchsorted(arr, e.entry_time.to_datetime64(), "left"))
        i_x = int(np.searchsorted(arr, e.exit_time.to_datetime64(), "left"))
        if i_e >= n or i_x >= n or i_x <= i_e:
            continue
        sign = _side_sign(side_fn(e)) if side_fn is not None else 1
        step = 1.0 + sign * float(delta)
        if step <= 0.0:
            raise ValueError(f"δ 过大：出场价因子 {step} ≤ 0")
        factor[i_e + 1:i_x + 1] *= step     # 窗相接不重叠（月末窗按月相接）
    for col in ("open", "high", "low", "close"):
        out[col] = pd.to_numeric(out[col]).to_numpy(
            dtype="float64") * factor
    return out


def event_power(effect_per_event: float, sigma_window: float, n_events: int,
                *, alpha: float = 0.05) -> float:
    """事件模式检验力（Lo 2002 iid 近似的事件口径）。

    每事件夏普 SR = 净效应/σ_w，SE = sqrt((1+SR²/2)/n)（T 年 → n 事件的
    同一近似），检验力 = Φ(SR/SE − z_α)；z_α 经 mt5quant.research.power
    的同一函数（千分位约定 0.05→1.645），不各自重算分位数。
    """
    sr = _check_effect(effect_per_event, sigma_window)
    if not isinstance(n_events, int) or n_events < 1:
        raise ValueError(f"n_events 须为 ≥1 的整数，收到 {n_events!r}")
    z = _z_alpha(float(alpha), 1)
    se = math.sqrt((1.0 + sr * sr / 2.0) / float(n_events))
    return _phi(sr / se - z)


def required_events(effect_per_event: float, sigma_window: float, *,
                    alpha: float = 0.05, power: float = 0.5,
                    n_tests: int = 1) -> int:
    """样本量规则（蓝图 §6.4；D35 修改 4/T84）：折算后预期净效应下检验力
    ≥ power 所需事件数（闭式）：n = (z_{α/N}+z_p)²·(1+SR²/2)/SR²，向上
    取整。

    n_tests（T84）：多重校正按 α/N（本轮候选数/主检验数，Bonferroni），
    z_{α/N} 走 mt5quant.research.power._z_alpha 的同一约定（千分位：
    0.05→1.645、0.05/2→1.960、0.05/6→2.394）；预筛（蓝图 §6.2 条件 4）
    仍按单次 n_tests=1 判定，α/N 用于事件模式样本量与开轮复核。
    power 缺省 0.5（预筛条件 4 同门槛）；z_p 取万分位（与
    min_detectable_sharpe 同约定：0.5→0.0、0.8→0.8416）。
    """
    sr = _check_effect(effect_per_event, sigma_window)
    if not 0.0 < float(power) < 1.0:
        raise ValueError(f"power 须在开区间 (0,1)，收到 {power!r}")
    if isinstance(n_tests, bool) or not isinstance(n_tests, int) \
            or n_tests < 1:
        raise ValueError(f"n_tests 必须是 ≥1 的整数，收到 {n_tests!r}")
    z_sum = _z_alpha(float(alpha), int(n_tests)) \
        + round(_norm_ppf(float(power)), 4)
    n = math.ceil(z_sum * z_sum * (1.0 + sr * sr / 2.0) / (sr * sr))
    return int(n)


def _check_effect(effect: float, sigma: float) -> float:
    if not math.isfinite(float(effect)) or float(effect) <= 0.0:
        raise ValueError(f"effect_per_event 须为有限正数（净效应>0 才谈"
                         f"检验力），收到 {effect!r}")
    if not math.isfinite(float(sigma)) or float(sigma) <= 0.0:
        raise ValueError(f"sigma_window 须为有限正数，收到 {sigma!r}")
    return float(effect) / float(sigma)
