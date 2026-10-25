"""止损越幅模型（T36/H2）：tick 上"首笔穿价"与价位之差的相对幅统计。

背景（第五轮审核 H2）：A 层止损按价位成交，B 层（逐 tick）按穿过价位后的
第一笔 tick 成交——两者的差即止损越幅。对账显示越幅中位数很小、但尾部可达
0.88 倍初始风险（美国数据发布时）。A-1.6 起 stop 出场成交价加本模型的 p50。

口径（钉死）：
- 多头止损按 Bid 下穿触发、空头按 Ask 上穿触发（与 A 层引擎/MT5 同侧）；
- 假想价位 = 前一笔 tick 的触发侧价格：本 tick 即该价位的**首笔穿价 tick**
  （严格越过它的第一个价格），越幅 = |本 tick − 前一笔|，
  相对价 = 越幅 / 价位（无量纲，随价格水平缩放，语义同 SpreadModel 的
  relative 口径）；
- bid 下穿与 ask 上穿两类事件合并统计（同一微观结构现象，样本翻倍）；
  同一笔 tick 双侧同时穿价计两个事件；
- 键 = 品种 × 纽约本地小时（与 SpreadModel 分桶同时区，DST 感知）；每小时
  记 p50/p90、n（事件总数）、n_days（出现事件的不同纽约日期数）。

内存控制：每小时蓄水池抽样上限 max_samples_per_hour（缺省 200_000；
XAUUSD 全库约 7,800 万事件，全量 float64 约 600 MB → 抽样上限下峰值
约 38 MB）。抽样确定性 = 固定种子 + 随机数严格按**事件全局到达序**消费
（与分块边界无关），因此 StopOvershootBuilder 分块喂入（逐月流式）与
一次性喂入同一数据的结果逐位一致。n_days 记全部事件（非抽样样本）的日期。

查询：overshoot(symbol, hour_ny, price=..., level="p50"|"p90") 返回
价格单位 = 相对分位 × price（price 缺失/非有限正数即抛，语义同
SpreadModel.spread_p90 的 relative 口径）；缺统计小时回退全天（全部有统计
小时）最大 p50/p90（保守，语义同 missing_hour_policy=forbid 的出场侧）。
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
DEFAULT_MAX_SAMPLES_PER_HOUR = 200_000
OVERSHOOT_RESERVOIR_SEED = 20261026   # 固定种子：同数据 → 同抽样（可复现）


@dataclass(frozen=True)
class OvershootStat:
    """单小时越幅统计（相对价，无量纲）。n = 全部事件数（非抽样样本数）。"""

    p50: float
    p90: float
    n: int
    n_days: int


class _HourReservoir:
    """单小时蓄水池：vals 未满上限前精确累积，满了之后均匀替换。

    随机数由 builder 统一持有——按事件全局到达序消费，分块无关。
    """

    __slots__ = ("vals", "count")

    def __init__(self) -> None:
        self.vals: list[float] | np.ndarray = []
        self.count = 0


class StopOvershootBuilder:
    """流式构建 StopOvershootModel：按时间顺序分块喂 tick，结果与一次性一致。"""

    def __init__(self, symbol: str, *,
                 max_samples_per_hour: int = DEFAULT_MAX_SAMPLES_PER_HOUR):
        if not isinstance(max_samples_per_hour, int) or max_samples_per_hour < 1:
            raise ValueError(
                f"max_samples_per_hour 须为正整数，收到 {max_samples_per_hour!r}")
        self._symbol = str(symbol)
        self._cap = int(max_samples_per_hour)
        self._rng = np.random.default_rng(OVERSHOOT_RESERVOIR_SEED)
        self._res: dict[int, _HourReservoir] = {}
        self._days: dict[int, set[int]] = {}
        self._prev_bid: float | None = None
        self._prev_ask: float | None = None

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
        if len(i_all) == 0:
            return
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
        self._absorb(hours, rel, days)

    # ---- 蓄水池吸收（随机数按事件全局到达序消费 → 分块无关） ----

    def _absorb(self, hours: np.ndarray, rel: np.ndarray,
                days: np.ndarray) -> None:
        m = len(rel)
        uniq = np.unique(hours)
        # 第一遍：按各小时当前水位判定每个事件是否消费随机数（池满后的事件）
        draw_flags = np.empty(m, dtype=bool)
        for h in uniq:
            sel = hours == h
            res = self._res.get(int(h))
            count_h = res.count if res is not None else 0
            pos = np.cumsum(sel) - 1                # 组内 0 基位置
            flags = (count_h + pos) >= self._cap
            draw_flags[sel] = flags[sel]
        n_draw = int(draw_flags.sum())
        u_assign = np.empty(m, dtype="float64")
        if n_draw:
            u_assign[np.flatnonzero(draw_flags)] = self._rng.random(n_draw)
        # 第二遍：逐小时吸收（布尔掩码保持组内事件顺序）
        for h in uniq:
            key = int(h)
            sel = hours == h
            res = self._res.setdefault(key, _HourReservoir())
            v = rel[sel]
            self._days.setdefault(key, set()).update(
                int(x) for x in np.unique(days[sel]))
            count_before = res.count
            res.count += int(len(v))
            space = max(0, self._cap - len(res.vals))
            direct = min(space, len(v))
            if direct:
                res.vals.extend(v[:direct])          # 池未满：直接入池（不抽随机数）
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
            arr = np.asarray(res.vals, dtype="float64")
            # 同槽多次替换：后到者胜（与逐事件顺序语义一致；反转取每槽末次）
            _, first_in_rev = np.unique(js[::-1], return_index=True)
            last = len(js) - 1 - first_in_rev
            arr[js[last]] = vs[last]
            res.vals = arr

    def build(self) -> "StopOvershootModel":
        if not self._res:
            raise ValueError(f"{self._symbol} tick 数据为空（无穿价事件）")
        per: dict[int, OvershootStat] = {}
        for h, res in sorted(self._res.items()):
            v = np.asarray(res.vals, dtype="float64")
            per[h] = OvershootStat(
                p50=float(np.quantile(v, 0.50)),
                p90=float(np.quantile(v, 0.90)),
                n=int(res.count), n_days=len(self._days[h]))
        return StopOvershootModel({self._symbol: per})


class StopOvershootModel:
    """多品种止损越幅统计：{symbol: {纽约本地小时: OvershootStat}}。"""

    def __init__(self, per_symbol: dict[str, dict[int, OvershootStat]] | None = None):
        self._per_symbol: dict[str, dict[int, OvershootStat]] = {
            s: dict(h) for s, h in (per_symbol or {}).items()
        }

    # ---- 构建 ----

    @classmethod
    def from_ticks(cls, symbol: str, ticks_df: pd.DataFrame, *,
                   max_samples_per_hour: int = DEFAULT_MAX_SAMPLES_PER_HOUR
                   ) -> "StopOvershootModel":
        """从 tick 帧一次性构建（流式分块喂入用 StopOvershootBuilder，结果一致）。"""
        if len(ticks_df) == 0:
            raise ValueError(f"{symbol} tick 数据为空")
        builder = StopOvershootBuilder(symbol,
                                       max_samples_per_hour=max_samples_per_hour)
        builder.add_ticks(ticks_df)
        return builder.build()

    def merge(self, *others: "StopOvershootModel") -> "StopOvershootModel":
        per = {s: dict(h) for s, h in self._per_symbol.items()}
        for o in others:
            for s, h in o._per_symbol.items():
                if s in per:
                    raise ValueError(f"品种 {s} 重复，不能合并")
                per[s] = dict(h)
        return StopOvershootModel(per)

    # ---- 查询 ----

    def _resolve(self, symbol: str | None) -> str:
        if symbol is not None:
            if symbol not in self._per_symbol:
                raise ValueError(
                    f"{symbol} 不在越幅模型中（品种：{sorted(self._per_symbol)}）")
            return symbol
        if len(self._per_symbol) == 1:
            return next(iter(self._per_symbol))
        raise ValueError(
            f"越幅模型含 {len(self._per_symbol)} 个品种，须显式指定 symbol"
            f"（{sorted(self._per_symbol)}）")

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._per_symbol))

    def stat(self, hour: int, *, symbol: str | None = None) -> OvershootStat:
        """该小时（纽约本地）统计；缺统计小时抛 ValueError（原始查询）。"""
        sym = self._resolve(symbol)
        h = int(hour)
        table = self._per_symbol[sym]
        if h not in table:
            raise ValueError(f"{sym} 纽约本地小时 {h} 无越幅统计（tick 覆盖缺失？）")
        return table[h]

    def missing_hours(self, symbol: str | None = None) -> tuple[int, ...]:
        sym = self._resolve(symbol)
        return tuple(h for h in range(24) if h not in self._per_symbol[sym])

    def _fallback(self, sym: str, level: str) -> float:
        table = self._per_symbol[sym]
        if not table:
            return 0.0
        key = "p50" if level == "p50" else "p90"
        return max(getattr(st, key) for st in table.values())

    def overshoot(self, symbol: str, hour_ny: int, *,
                  price: float | None = None,
                  level: str = "p50") -> float:
        """价格单位越幅 = 相对分位 × price（该笔出场的 SL 价位基准）。

        level："p50"（A 层成交口径，A-1.6）或 "p90"（G2 敏感性备查）。
        缺统计小时回退全天最大同档分位（保守）。
        """
        if level not in LEVELS:
            raise ValueError(f"level 须为 {LEVELS} 之一，收到 {level!r}")
        if price is None:
            raise ValueError(f"{symbol} 小时 {hour_ny}：越幅为相对口径，需要 price")
        p = float(price)
        if not math.isfinite(p) or p <= 0:
            raise ValueError(
                f"{symbol} 小时 {hour_ny}：price 须为有限正数，收到 {price!r}")
        sym = self._resolve(symbol)
        h = int(hour_ny)
        table = self._per_symbol[sym]
        v = (table[h].p50 if level == "p50" else table[h].p90) if h in table \
            else self._fallback(sym, level)
        return float(v * p)

    # ---- 持久化 ----

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "bucket_tz": BUCKET_TZ,
            "per_symbol": {
                s: {str(h): asdict(st) for h, st in hours.items()}
                for s, hours in self._per_symbol.items()
            },
        }

    def save(self, path: str) -> None:
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    @classmethod
    def from_dict(cls, d: dict) -> "StopOvershootModel":
        if d.get("version") != 1:
            raise ValueError(f"越幅表 version 非法：{d.get('version')!r}（只认 1）")
        if d.get("bucket_tz") != BUCKET_TZ:
            raise ValueError(
                f"越幅表 bucket_tz={d.get('bucket_tz')!r}，与纽约本地小时口径"
                "不兼容，请重建")
        per = {
            s: {int(h): OvershootStat(**st) for h, st in hours.items()}
            for s, hours in d["per_symbol"].items()
        }
        return cls(per)

    @classmethod
    def load(cls, path: str) -> "StopOvershootModel":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))
