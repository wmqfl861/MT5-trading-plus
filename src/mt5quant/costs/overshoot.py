"""止损越幅模型（T36/H2；第六轮审核 I3 后为双口径）。

背景（第五轮审核 H2）：A 层止损按价位成交，B 层（逐 tick）按穿过价位后的
第一笔 tick 成交——两者的差即止损越幅。对账显示越幅中位数很小、但尾部可达
0.88 倍初始风险（美国数据发布时）。A-1.6 起 stop 出场成交价加本模型的 p50。

口径 basis（第六轮 I3：真实止损是**预先固定的价位**及首次满足
Bid<=SL／Ask>=SL 的事件，不是相邻跳幅——审核反例：Bid 100→99.9→99.9、
SL=99.9 时真实首次触发越幅为 0，旧算法把前一笔报价当假想止损价记 0.1，
且恰触价位的零越幅事件被排除）：

- **stop_level（缺省，version 2 主口径）——预定价位回放**：
  - bar = 按 tick 时间戳聚合的 1 分钟桶（time_msc // 60_000，与 MT5 M1
    同构；builder 只见 tick 流，bar 由 tick 自建）；
  - 预置止损价（代理）：多头 = 前一根分钟 bar 的最低 **Bid**，空头 =
    前一根分钟 bar 的最高 **Ask**（各取触发侧自身价格序列的极值，与回放
    触发同价类；"止损挂在前根 bar 极值"是趋势策略常见做法）；**只对
    时间紧邻的前一分钟回放**——跨缺口（周末/停盘）的"前根 bar"语义
    不成立，不产生事件；
  - 在当根 bar 的 tick 序列里找**首笔**满足 Bid<=stop（多头）／
    Ask>=stop（空头）的 tick，越幅 = |该 tick 触发侧价 − stop|，相对价
    = 越幅 / stop（无量纲，语义同 SpreadModel 的 relative 口径）；
    **恰触价位（越幅=0）计入分位——零是真实结果**；
  - 每根 bar 每侧至多一个事件（首笔触发）；同一笔 tick 双侧同时触发计
    两个事件（先多头后空头）；
  - 与任意策略实际止损的差别（如实声明）：策略的止损可以挂在任何价位
    （前根 bar 极值只是常见代理之一）、持仓可以跨多根 bar、也可以盘中
    撤改——本口径统计的是"前根分钟 bar 极值这一预定价位的首笔穿越"
    分布，用于近似"若止损恰好挂在前根极值、越幅会有多大"，不是某个
    特定策略的成交分布；
- **adjacent_jump（secondary，version 1 旧口径，仅对比用）**：假想价位 =
  前一笔 tick 的触发侧价格，越幅 = |本 tick − 前一笔|。它统计的是相邻
  跳动分布，条件分布与真实止损回放不同（见上方反例：同一序列新口径记
  0、旧口径记 0.1）。保留只为对比新旧口径，不用于成本结论。

共同口径：多头 Bid 下穿、空头 Ask 上穿两类事件合并统计（同一微观结构
现象，样本翻倍）；键 = 品种 × 纽约本地小时（与 SpreadModel 分桶同时区，
DST 感知）；每小时记 p50/p90、n（事件总数）、n_days（出现事件的不同纽约
日期数）。tick 须按时间升序喂入。

内存控制：每小时**每口径**蓄水池抽样上限 max_samples_per_hour（缺省
200_000）。抽样确定性 = 固定种子 + 随机数严格按**事件全局到达序**消费
（与分块边界无关），且**两口径各持独立随机流**（互不挤占，分块无关对两
口径同时成立）：adjacent_jump 沿用 v1 种子与流（同数据与 v1 缓存数值逐位
一致），stop_level 用独立种子。n_days 记全部事件（非抽样样本）的日期。

查询：overshoot(symbol, hour_ny, price=..., level="p50"|"p90",
basis="stop_level"|"adjacent_jump") 返回价格单位 = 相对分位 × price
（price 缺失/非有限正数即抛，语义同 SpreadModel.spread_p90 的 relative
口径）；缺统计小时回退**该口径**全天（全部有统计小时）最大 p50/p90
（保守，语义同 missing_hour_policy=forbid 的出场侧）。

持久化：to_dict 带 basis 键（version 2 = 主表 basis="stop_level" +
secondary adjacent_jump 对照表）。version 1 旧表（仓库既有缓存内嵌的旧
格式）只有一张 per_symbol——按 adjacent_jump 载入并标记：缺省口径查询
报错指路重建（重建后由 build_cost_model --overshoot 覆盖写新表），绝不把
旧口径数值冒充 stop_level 使用。
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from .spread import BUCKET_TZ   # 纽约本地小时分桶（与点差模型同时区）

LEVELS = ("p50", "p90")
BASES = ("stop_level", "adjacent_jump")
DEFAULT_BASIS = "stop_level"
DEFAULT_MAX_SAMPLES_PER_HOUR = 200_000
OVERSHOOT_RESERVOIR_SEED = 20261026   # adjacent_jump 固定种子（v1 同源）
OVERSHOOT_STOP_LEVEL_SEED = 20261027  # stop_level 独立随机流（两口径互不挤占）
BAR_MS = 60_000                       # stop_level 回放的 bar 粒度（1 分钟桶）


@dataclass(frozen=True)
class OvershootStat:
    """单小时越幅统计（相对价，无量纲）。n = 全部事件数（非抽样样本数）。"""

    p50: float
    p90: float
    n: int
    n_days: int


class _HourReservoir:
    """单小时蓄水池：vals 未满上限前精确累积，满了之后均匀替换。

    随机数按口径独立持有——按该口径事件全局到达序消费，分块无关。
    """

    __slots__ = ("vals", "count")

    def __init__(self) -> None:
        self.vals: list[float] | np.ndarray = []
        self.count = 0


class StopOvershootBuilder:
    """流式构建 StopOvershootModel：按时间顺序分块喂 tick，结果与一次性一致。

    add_ticks 同时计算两个口径（stop_level 预定价位回放 + adjacent_jump
    相邻跳幅对照），两口径各持独立蓄水池与随机流。
    """

    def __init__(self, symbol: str, *,
                 max_samples_per_hour: int = DEFAULT_MAX_SAMPLES_PER_HOUR):
        if not isinstance(max_samples_per_hour, int) or max_samples_per_hour < 1:
            raise ValueError(
                f"max_samples_per_hour 须为正整数，收到 {max_samples_per_hour!r}")
        self._symbol = str(symbol)
        self._cap = int(max_samples_per_hour)
        # adjacent_jump（旧口径，secondary）
        self._rng_adj = np.random.default_rng(OVERSHOOT_RESERVOIR_SEED)
        self._res_adj: dict[int, _HourReservoir] = {}
        self._days_adj: dict[int, set[int]] = {}
        self._prev_bid: float | None = None
        self._prev_ask: float | None = None
        # stop_level（缺省口径）：蓄水池 + 分钟 bar 回放状态（跨块滚动）
        self._rng_stop = np.random.default_rng(OVERSHOOT_STOP_LEVEL_SEED)
        self._res_stop: dict[int, _HourReservoir] = {}
        self._days_stop: dict[int, set[int]] = {}
        self._cur_id: int | None = None       # 开放中的分钟 bar（id、极值、
        self._cur_min_bid = 0.0               # 已触发标记——bar 可能跨块分裂）
        self._cur_max_ask = 0.0
        self._cur_long_fired = False
        self._cur_short_fired = False
        self._prev_id: int | None = None      # 最近一根已完结的分钟 bar
        self._prev_min_bid = 0.0
        self._prev_max_ask = 0.0

    def add_ticks(self, ticks_df: pd.DataFrame) -> None:
        missing = [c for c in ("time_msc", "bid", "ask") if c not in ticks_df.columns]
        if missing:
            raise ValueError(f"tick 帧缺列 {missing}（只需 time_msc/bid/ask）")
        n = len(ticks_df)
        if n == 0:
            return
        t = ticks_df["time_msc"].to_numpy(dtype="int64")
        bid = ticks_df["bid"].to_numpy(dtype="float64")
        ask = ticks_df["ask"].to_numpy(dtype="float64")

        # ---- 口径 adjacent_jump（旧口径；与 version 1 行为逐位一致） ----
        if self._prev_bid is None:
            # 首块首行只作种子（无前一笔，不构成事件）
            prev_b = np.concatenate(([bid[0]], bid[:-1]))
            prev_a = np.concatenate(([ask[0]], ask[:-1]))
            lo = 1
        else:
            prev_b = np.concatenate(([self._prev_bid], bid[:-1]))
            prev_a = np.concatenate(([self._prev_ask], ask[:-1]))
            lo = 0
        self._prev_bid, self._prev_ask = float(bid[-1]), float(ask[-1])
        down = np.flatnonzero(bid < prev_b)          # 多头止损侧：Bid 下穿
        up = np.flatnonzero(ask > prev_a)            # 空头止损侧：Ask 上穿
        down = down[down >= lo]
        up = up[up >= lo]
        # 事件全局序：tick 序号升序，同 tick 先下穿后上穿（分块无关的确定性）
        i_all = np.concatenate((down, up))
        side = np.concatenate((np.zeros(len(down), np.int8),
                               np.ones(len(up), np.int8)))
        order = np.lexsort((side, i_all))
        i_all = i_all[order]
        if len(i_all):
            level = np.concatenate((prev_b[down], prev_a[up]))[order]
            fill = np.concatenate((bid[down], ask[up]))[order]
            if not bool(np.all(level > 0)) or not bool(np.isfinite(level).all()):
                raise ValueError(
                    f"{self._symbol} tick 存在非正/非有限价位（bid/ask ≤ 0），"
                    "无法计算相对越幅")
            rel = (np.abs(fill - level)) / level
            local = (pd.to_datetime(pd.Series(t[i_all]), unit="ms", utc=True)
                     .dt.tz_convert(BUCKET_TZ))
            hours = local.dt.hour.to_numpy(dtype="int64")
            days = local.dt.normalize().astype("int64").to_numpy()
            self._absorb(self._res_adj, self._days_adj, self._rng_adj,
                         hours, rel, days)

        # ---- 口径 stop_level（缺省；预定价位回放，第六轮 I3） ----
        self._replay_stop_level(t, bid, ask)

    # ---- stop_level：前根分钟 bar 极值价位的首笔触发回放 ----

    @staticmethod
    def _first_per_group(cand: np.ndarray, gidx: np.ndarray) -> np.ndarray:
        """候选 tick（升序）→ 每组（bar）首笔触发（np.unique 取首出现）。"""
        if len(cand) == 0:
            return cand
        g = gidx[cand]                    # 候选升序 → 组号非降
        _, first = np.unique(g, return_index=True)
        return cand[first]

    @staticmethod
    def _check_stops(symbol: str, stops: np.ndarray) -> None:
        if not bool(np.all(stops > 0)) or not bool(np.isfinite(stops).all()):
            raise ValueError(
                f"{symbol} tick 存在非正/非有限价位（bid/ask ≤ 0），"
                "无法计算相对越幅")

    def _replay_stop_level(self, t: np.ndarray, bid: np.ndarray,
                           ask: np.ndarray) -> None:
        ids = t // BAR_MS
        n = len(t)
        first_cont = (self._cur_id is not None and ids[0] == self._cur_id)
        # 组 0 恒从 index 0 开始：first_cont 时它是跨块开放 bar 的延续段
        # （极值随后与已累积部分合并），否则是一根新 bar 的首段
        new_grp = np.empty(n, dtype=bool)
        new_grp[0] = True
        if n > 1:
            new_grp[1:] = ids[1:] != ids[:-1]
        gidx = np.cumsum(new_grp) - 1
        g0 = np.flatnonzero(new_grp)
        g_id = ids[g0]
        g_minb = np.minimum.reduceat(bid, g0)
        g_maxa = np.maximum.reduceat(ask, g0)
        eff_minb = g_minb.copy()
        eff_maxa = g_maxa.copy()
        if first_cont:                    # 跨块延续的开放 bar：极值合并
            eff_minb[0] = min(self._cur_min_bid, float(g_minb[0]))
            eff_maxa[0] = max(self._cur_max_ask, float(g_maxa[0]))
        G = len(g0)
        # 各组的预置止损（只对时间紧邻的前一根 bar 回放；±inf = 该侧不回放）
        stop_l = np.full(G, -np.inf)      # Bid <= stop 才触发 → -inf 恒假
        stop_s = np.full(G, np.inf)       # Ask >= stop 才触发 → +inf 恒假
        if first_cont:
            if self._prev_id is not None and self._prev_id == g_id[0] - 1:
                stop_l[0], stop_s[0] = self._prev_min_bid, self._prev_max_ask
                if self._cur_long_fired:      # 该 bar 早前已触发：首笔语义
                    stop_l[0] = -np.inf
                if self._cur_short_fired:
                    stop_s[0] = np.inf
        elif self._cur_id is not None and self._cur_id == g_id[0] - 1:
            stop_l[0], stop_s[0] = self._cur_min_bid, self._cur_max_ask
        if G > 1:
            contig = g_id[1:] == g_id[:-1] + 1
            stop_l[1:] = np.where(contig, eff_minb[:-1], -np.inf)
            stop_s[1:] = np.where(contig, eff_maxa[:-1], np.inf)
        counts = np.diff(np.append(g0, n))
        stop_l_t = np.repeat(stop_l, counts)
        stop_s_t = np.repeat(stop_s, counts)
        cand_l = self._first_per_group(np.flatnonzero(bid <= stop_l_t), gidx)
        cand_s = self._first_per_group(np.flatnonzero(ask >= stop_s_t), gidx)

        rel_l = np.empty(0, dtype="float64")
        if len(cand_l):
            stops = stop_l_t[cand_l]
            self._check_stops(self._symbol, stops)
            rel_l = (stops - bid[cand_l]) / stops
        rel_s = np.empty(0, dtype="float64")
        if len(cand_s):
            stops = stop_s_t[cand_s]
            self._check_stops(self._symbol, stops)
            rel_s = (ask[cand_s] - stops) / stops
        i_all = np.concatenate((cand_l, cand_s))
        side = np.concatenate((np.zeros(len(cand_l), np.int8),
                               np.ones(len(cand_s), np.int8)))
        order = np.lexsort((side, i_all))
        i_all = i_all[order]
        if len(i_all):
            rel = np.concatenate((rel_l, rel_s))[order]
            local = (pd.to_datetime(pd.Series(t[i_all]), unit="ms", utc=True)
                     .dt.tz_convert(BUCKET_TZ))
            hours = local.dt.hour.to_numpy(dtype="int64")
            days = local.dt.normalize().astype("int64").to_numpy()
            self._absorb(self._res_stop, self._days_stop, self._rng_stop,
                         hours, rel, days)
        self._advance_bar_state(g_id, eff_minb, eff_maxa, gidx,
                                cand_l, cand_s, first_cont)

    def _advance_bar_state(self, g_id: np.ndarray, eff_minb: np.ndarray,
                           eff_maxa: np.ndarray, gidx: np.ndarray,
                           cand_l: np.ndarray, cand_s: np.ndarray,
                           first_cont: bool) -> None:
        """滚动分钟 bar 状态：末组保持开放（可能被下一块延续），其余完结。"""
        G = len(g_id)
        fired_l = bool(len(cand_l) and gidx[cand_l][-1] == G - 1)
        fired_s = bool(len(cand_s) and gidx[cand_s][-1] == G - 1)
        if G == 1 and first_cont:
            self._cur_min_bid = float(eff_minb[0])
            self._cur_max_ask = float(eff_maxa[0])
            self._cur_long_fired = self._cur_long_fired or fired_l
            self._cur_short_fired = self._cur_short_fired or fired_s
            return
        if G >= 2:
            self._prev_id = int(g_id[G - 2])
            self._prev_min_bid = float(eff_minb[G - 2])
            self._prev_max_ask = float(eff_maxa[G - 2])
        elif self._cur_id is not None:
            # G == 1 且是新 bar：旧开放 bar 就此完结为前根
            self._prev_id = self._cur_id
            self._prev_min_bid = self._cur_min_bid
            self._prev_max_ask = self._cur_max_ask
        self._cur_id = int(g_id[G - 1])
        self._cur_min_bid = float(eff_minb[G - 1])
        self._cur_max_ask = float(eff_maxa[G - 1])
        self._cur_long_fired = fired_l
        self._cur_short_fired = fired_s

    # ---- 蓄水池吸收（随机数按各口径事件全局到达序消费 → 分块无关） ----

    def _absorb(self, res: dict[int, _HourReservoir], days: dict[int, set[int]],
                rng: np.random.Generator, hours: np.ndarray, rel: np.ndarray,
                days_arr: np.ndarray) -> None:
        m = len(rel)
        uniq = np.unique(hours)
        # 第一遍：按各小时当前水位判定每个事件是否消费随机数（池满后的事件）
        draw_flags = np.empty(m, dtype=bool)
        for h in uniq:
            sel = hours == h
            r = res.get(int(h))
            count_h = r.count if r is not None else 0
            pos = np.cumsum(sel) - 1                # 组内 0 基位置
            flags = (count_h + pos) >= self._cap
            draw_flags[sel] = flags[sel]
        n_draw = int(draw_flags.sum())
        u_assign = np.empty(m, dtype="float64")
        if n_draw:
            u_assign[np.flatnonzero(draw_flags)] = rng.random(n_draw)
        # 第二遍：逐小时吸收（布尔掩码保持组内事件顺序）
        for h in uniq:
            key = int(h)
            sel = hours == key
            r = res.setdefault(key, _HourReservoir())
            v = rel[sel]
            days.setdefault(key, set()).update(
                int(x) for x in np.unique(days_arr[sel]))
            count_before = r.count
            r.count += int(len(v))
            space = max(0, self._cap - len(r.vals))
            direct = min(space, len(v))
            if direct:
                r.vals.extend(v[:direct])          # 池未满：直接入池（不抽随机数）
            rest = v[direct:]
            if len(rest) == 0:
                continue
            u = u_assign[sel][draw_flags[sel]]       # 与 rest 一一对应
            arrivals = np.arange(count_before + direct + 1,
                                 count_before + len(v) + 1, dtype=np.int64)
            j = (u * arrivals).astype(np.int64)      # 均匀 [0, A)
            acc = j < self._cap                      # 以 cap/A 概率替换
            if not acc.any():
                continue
            js, vs = j[acc], rest[acc]
            arr = np.asarray(r.vals, dtype="float64")
            # 同槽多次替换：后到者胜（与逐事件顺序语义一致；反转取每槽末次）
            _, first_in_rev = np.unique(js[::-1], return_index=True)
            last = len(js) - 1 - first_in_rev
            arr[js[last]] = vs[last]
            r.vals = arr

    def build(self) -> "StopOvershootModel":
        if not self._res_stop and not self._res_adj:
            raise ValueError(f"{self._symbol} tick 数据为空（无穿价事件）")

        def _stats(res: dict[int, _HourReservoir],
                   days: dict[int, set[int]]) -> dict[int, OvershootStat]:
            per: dict[int, OvershootStat] = {}
            for h, r in sorted(res.items()):
                v = np.asarray(r.vals, dtype="float64")
                per[h] = OvershootStat(
                    p50=float(np.quantile(v, 0.50)),
                    p90=float(np.quantile(v, 0.90)),
                    n=int(r.count), n_days=len(days[h]))
            return per

        # 两口径各带一张（可能为空的）表：品种键恒存在（空表 = 该口径无事件）
        return StopOvershootModel(
            {self._symbol: _stats(self._res_stop, self._days_stop)},
            adjacent={self._symbol: _stats(self._res_adj, self._days_adj)})


class StopOvershootModel:
    """多品种止损越幅统计（双口径）：主表 stop_level（缺省）+
    adjacent_jump 对照表。结构：{symbol: {纽约本地小时: OvershootStat}}。"""

    def __init__(self, per_symbol: dict[str, dict[int, OvershootStat]] | None = None,
                 *, adjacent: dict[str, dict[int, OvershootStat]] | None = None):
        self._per_symbol: dict[str, dict[int, OvershootStat]] = {
            s: dict(h) for s, h in (per_symbol or {}).items()}
        self._adjacent: dict[str, dict[int, OvershootStat]] = {
            s: dict(h) for s, h in (adjacent or {}).items()}

    # ---- 口径表 ----

    def _tables(self, basis: str) -> dict[str, dict[int, OvershootStat]]:
        if basis == "stop_level":
            return self._per_symbol
        if basis == "adjacent_jump":
            return self._adjacent
        raise ValueError(f"basis 须为 {BASES} 之一，收到 {basis!r}")

    def _legacy_hint(self, symbol: str) -> str:
        """旧 version 1 表（只有 adjacent_jump）被按缺省口径查询时的指路。"""
        if symbol not in self._per_symbol and symbol in self._adjacent:
            return ("——该表只含旧口径 adjacent_jump（version 1 缓存），请重建"
                    "（scripts/build_cost_model.py --overshoot）覆盖写新表，"
                    "或显式 basis='adjacent_jump'（仅对比用）")
        return ""

    # ---- 构建 ----

    @classmethod
    def from_ticks(cls, symbol: str, ticks_df: pd.DataFrame, *,
                   max_samples_per_hour: int = DEFAULT_MAX_SAMPLES_PER_HOUR
                   ) -> "StopOvershootModel":
        """从 tick 帧一次性构建，两口径同时统计（流式分块喂入用
        StopOvershootBuilder，结果一致）。"""
        if len(ticks_df) == 0:
            raise ValueError(f"{symbol} tick 数据为空")
        builder = StopOvershootBuilder(symbol,
                                       max_samples_per_hour=max_samples_per_hour)
        builder.add_ticks(ticks_df)
        return builder.build()

    def merge(self, *others: "StopOvershootModel") -> "StopOvershootModel":
        def _merged(maps: list[dict[str, dict[int, OvershootStat]]]
                    ) -> dict[str, dict[int, OvershootStat]]:
            per = {s: dict(h) for s, h in maps[0].items()}
            for m in maps[1:]:
                for s, h in m.items():
                    if s in per:
                        raise ValueError(f"品种 {s} 重复，不能合并")
                    per[s] = dict(h)
            return per

        return StopOvershootModel(
            _merged([self._per_symbol] + [o._per_symbol for o in others]),
            adjacent=_merged([self._adjacent] + [o._adjacent for o in others]))

    # ---- 查询 ----

    def _resolve(self, symbol: str | None,
                 basis: str = DEFAULT_BASIS) -> str:
        tables = self._tables(basis)
        if symbol is not None:
            if symbol not in tables:
                raise ValueError(
                    f"{symbol} 不在越幅模型 {basis} 口径中（品种："
                    f"{sorted(set(self._per_symbol) | set(self._adjacent))}）"
                    + self._legacy_hint(symbol))
            return symbol
        if len(tables) == 1:
            return next(iter(tables))
        if not tables and len(self._adjacent) == 1:
            # 旧 version 1 单表模型被按缺省口径查询：指路重建
            sym = next(iter(self._adjacent))
            raise ValueError(
                f"{sym} 不在越幅模型 {basis} 口径中" + self._legacy_hint(sym))
        raise ValueError(
            f"越幅模型 {basis} 口径含 {len(tables)} 个品种，须显式指定 symbol"
            f"（{sorted(tables)}）")

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(set(self._per_symbol) | set(self._adjacent)))

    def stat(self, hour: int, *, symbol: str | None = None,
             basis: str = DEFAULT_BASIS) -> OvershootStat:
        """该小时（纽约本地）统计；缺统计小时抛 ValueError（原始查询）。"""
        sym = self._resolve(symbol, basis)
        h = int(hour)
        table = self._tables(basis)[sym]
        if h not in table:
            raise ValueError(
                f"{sym} 纽约本地小时 {h} 无越幅统计（basis={basis}；"
                f"tick 覆盖缺失？）{self._legacy_hint(sym)}")
        return table[h]

    def missing_hours(self, symbol: str | None = None,
                      *, basis: str = DEFAULT_BASIS) -> tuple[int, ...]:
        sym = self._resolve(symbol, basis)
        return tuple(h for h in range(24)
                     if h not in self._tables(basis)[sym])

    def _fallback(self, sym: str, level: str, basis: str) -> float:
        table = self._tables(basis)[sym]
        if not table:
            return 0.0
        key = "p50" if level == "p50" else "p90"
        return max(getattr(st, key) for st in table.values())

    def overshoot(self, symbol: str, hour_ny: int, *,
                  price: float | None = None,
                  level: str = "p50",
                  basis: str = DEFAULT_BASIS) -> float:
        """价格单位越幅 = 相对分位 × price（该笔出场的 SL 价位基准）。

        level："p50"（A 层成交口径，A-1.6）或 "p90"（G2 敏感性备查）；
        basis：缺省 "stop_level"（预定价位回放，第六轮 I3），
        "adjacent_jump" 为旧口径（相邻跳幅代理），仅对比用。缺统计小时
        回退**该口径**全天最大同档分位（保守）。
        """
        if basis not in BASES:
            raise ValueError(f"basis 须为 {BASES} 之一，收到 {basis!r}")
        if level not in LEVELS:
            raise ValueError(f"level 须为 {LEVELS} 之一，收到 {level!r}")
        if price is None:
            raise ValueError(f"{symbol} 小时 {hour_ny}：越幅为相对口径，需要 price")
        p = float(price)
        if not math.isfinite(p) or p <= 0:
            raise ValueError(
                f"{symbol} 小时 {hour_ny}：price 须为有限正数，收到 {price!r}")
        sym = self._resolve(symbol, basis)
        h = int(hour_ny)
        table = self._tables(basis)[sym]
        v = (table[h].p50 if level == "p50" else table[h].p90) if h in table \
            else self._fallback(sym, level, basis)
        return float(v * p)

    # ---- 持久化 ----

    @staticmethod
    def _dump_hours(tables: dict[str, dict[int, OvershootStat]]) -> dict:
        return {s: {str(h): asdict(st) for h, st in hours.items()}
                for s, hours in tables.items()}

    def to_dict(self) -> dict:
        if not self._per_symbol and self._adjacent:
            # 旧 version 1 单表载入的模型：如实标记口径，不伪造空主表
            return {
                "version": 2,
                "bucket_tz": BUCKET_TZ,
                "basis": "adjacent_jump",
                "per_symbol": self._dump_hours(self._adjacent),
            }
        return {
            "version": 2,
            "bucket_tz": BUCKET_TZ,
            "basis": DEFAULT_BASIS,
            "per_symbol": self._dump_hours(self._per_symbol),
            "secondary": {
                "basis": "adjacent_jump",
                "per_symbol": self._dump_hours(self._adjacent),
            },
        }

    def save(self, path: str) -> None:
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    @staticmethod
    def _parse_hours(d: dict) -> dict[str, dict[int, OvershootStat]]:
        return {s: {int(h): OvershootStat(**st) for h, st in hours.items()}
                for s, hours in d.items()}

    @classmethod
    def from_dict(cls, d: dict) -> "StopOvershootModel":
        version = d.get("version")
        if version == 1:
            # 旧表：单张 per_symbol，口径 = adjacent_jump（相邻跳幅）。
            # 标记载入——缺省口径查询报错指路重建（见 _legacy_hint）。
            if d.get("bucket_tz") != BUCKET_TZ:
                raise ValueError(
                    f"越幅表 bucket_tz={d.get('bucket_tz')!r}，与纽约本地小时"
                    "口径不兼容，请重建")
            return cls(adjacent=cls._parse_hours(d["per_symbol"]))
        if version != 2:
            raise ValueError(
                f"越幅表 version 非法：{version!r}（只认 1=旧口径 adjacent_jump"
                " / 2=带 basis 键双口径）")
        if d.get("bucket_tz") != BUCKET_TZ:
            raise ValueError(
                f"越幅表 bucket_tz={d.get('bucket_tz')!r}，与纽约本地小时口径"
                "不兼容，请重建")
        basis = d.get("basis", DEFAULT_BASIS)
        if basis not in BASES:
            raise ValueError(f"越幅表 basis 非法：{basis!r}（只认 {BASES}）")
        primary = cls._parse_hours(d["per_symbol"])
        secondary = d.get("secondary")
        sec_tables: dict[str, dict[int, OvershootStat]] = {}
        if secondary:
            sec_basis = secondary.get("basis")
            if sec_basis not in BASES or sec_basis == basis:
                raise ValueError(
                    f"越幅表 secondary 口径非法：{sec_basis!r}（须为"
                    f"{BASES} 中异于主口径的一项）")
            sec_tables = cls._parse_hours(secondary["per_symbol"])
        if basis == "stop_level":
            return cls(primary, adjacent=sec_tables)
        return cls(sec_tables, adjacent=primary)

    @classmethod
    def load(cls, path: str) -> "StopOvershootModel":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))
