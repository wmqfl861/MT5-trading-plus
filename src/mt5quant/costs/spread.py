"""点差模型：tick 实测点差，按**纽约本地小时**聚合全周；绝对/相对两种口径。

T20（H2）修订：
- 分桶从 UTC 小时改为纽约本地小时（BUCKET_TZ）。换日尖峰固定在纽约 17 点：
  夏令时的 21:00 UTC 与冬令时的 22:00 UTC 落进**同一个桶**，不再被 DST 摊薄
  到两个 UTC 小时、两边都被低估；查询接口（spread_p90/stat）的 hour 参数
  一律指纽约本地小时（引擎侧已同步换算）。
- 某小时缺统计不再抛错（旧版直接 ValueError，整次回测失败——H006 因此无法
  评估）。按 config/sessions.toml 的 missing_hour_policy 处理：
  "forbid" = 该小时禁入（missing_hours 供接线层并入 SessionPolicy），
  出场/估值成本取全天最大 p90（保守）。落盘文件写入 bucket_tz 标注；
  读到无标注的旧（UTC 分桶）文件 → 拒绝加载，绝不静默错读。

T36（H2）修订——采样口径（sampling，整模型统一）：
- "bar_open_m1"（from_ticks 缺省）：每个 M1 边界后的**第一笔 tick** 才进
  统计。回测成交发生在 bar 边界，tick 加权会把繁忙时段（tick 多、点差窄）
  的权重放大；每分钟恰一个样本 ≈ 按时间加权，且样本量只随分钟数增长
  （XAUUSD 全库 7,800 万 tick → ~50 万分钟样本），配合 SpreadModelBuilder
  分块喂入即可把构建内存压到常数级。
- "tick"：全部 tick 进统计（旧口径，保留作对比）。
- 每小时统计记 n（样本数）与 n_days（样本覆盖的不同纽约日期数）；
  n_days < min_days_per_hour（config/sessions.toml [costs]，缺省 20）→
  视为缺统计（forbid 语义）：missing_hours / spread_p90 的
  min_days_per_hour 参数；缺省 0 = 不启用该阈值（旧语义，只看小时是否
  有统计）。
- 落盘文件写 sampling 键；读到无标注的旧文件按 "tick" 解释（统计本身
  就是全 tick 的，不会错读）。

口径（mode，整模型统一）：
- "relative"（from_ticks 默认）：统计 (ask−bid)/((ask+bid)/2) 的分位——无量纲
  比率，不随价格水平漂移。spread_p90(..., price=p) 返回 rel_p90×p（价格单位），
  price 缺失或非有限正数即抛 ValueError。用于"近期 tick 的点差结构外推到价格
  水平不同的历史"（黄金 2014 年约 $1,300 vs 今天 $2,400+，绝对点差大致随价格
  放大，直接套用会把早年成本高估数倍）。
- "absolute"：统计 ask−bid（价格单位），spread_p90 忽略 price——旧口径，
  行为逐位一致；load 读不到 mode 的旧文件按此解释。

其它约定：
- 只按列名 time_msc（epoch 毫秒）/bid/ask 读帧，不依赖 mt5data.ticks 模块；
- epoch 毫秒直读为 UTC，换算到 BUCKET_TZ 本地小时后按小时聚合全周
  （最终统计键 = 纽约本地小时 0..23）；
- 分位用线性插值（numpy 默认），与 pandas quantile 一致；
- spread_p90 取该小时 p90（保守估计）＝一次来回的完整点差成本（relative
  模型为 rel_p90×price）：bar 按 bid 计，多头按 ask(=bid+点差) 入场、空头按
  ask 出场，每个来回恰收一次。
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

MODES = ("relative", "absolute")
SAMPLINGS = ("bar_open_m1", "tick")   # T36：采样口径（见模块 docstring）
BUCKET_TZ = "America/New_York"   # T20：小时分桶口径（换日 17:00 所在时区）


@dataclass(frozen=True)
class SpreadStat:
    """单小时点差统计。relative 模式为无量纲比率；absolute 模式为价格单位。

    n_days（T36）= 样本覆盖的不同纽约日期数（n_days < min_days_per_hour
    视为缺统计）；旧文件无此键时按 0 读入（阈值判定自然判为缺）。
    """

    median: float
    p90: float
    p99: float
    n: int
    n_days: int = 0


class SpreadModelBuilder:
    """流式构建 SpreadModel：按时间顺序分块喂 tick，结果与一次性喂入逐位一致。

    内存控制（T36）：bar_open_m1 口径每块只保留"分钟首笔"样本（每块峰值 =
    该块行数，累计 = 分钟数）；tick 口径累计全量样本（与旧一次性构建同量级）。
    跨块去重：上一块末尾未走完的分钟，下一块的同分钟 tick 不再采样。
    bar_open_m1 要求每块 time_msc 升序（库内写盘保证；违反即 ValueError）。
    """

    def __init__(self, symbol: str, *, mode: str = "relative",
                 sampling: str = "bar_open_m1"):
        if mode not in MODES:
            raise ValueError(f"mode 必须是 {MODES} 之一，收到 {mode!r}")
        if sampling not in SAMPLINGS:
            raise ValueError(f"sampling 必须是 {SAMPLINGS} 之一，收到 {sampling!r}")
        self._symbol = str(symbol)
        self._mode = mode
        self._sampling = sampling
        self._vals: dict[int, list[np.ndarray]] = {}
        self._days: dict[int, set[int]] = {}
        self._last_minute: int | None = None   # 跨块分钟去重（bar_open_m1）

    def add_ticks(self, ticks_df: pd.DataFrame) -> None:
        missing = [c for c in ("time_msc", "bid", "ask") if c not in ticks_df.columns]
        if missing:
            raise ValueError(f"tick 帧缺列 {missing}（只需 time_msc/bid/ask）")
        if len(ticks_df) == 0:
            return
        t = ticks_df["time_msc"].to_numpy(dtype="int64")
        if self._sampling == "bar_open_m1":
            if len(t) > 1 and bool((t[1:] < t[:-1]).any()):
                raise ValueError(
                    f"{self._symbol} tick 的 time_msc 必须升序（bar_open_m1 采样"
                    "按分钟游程取首笔的前提；库内写盘已保证）")
            minute = t // 60_000
            first = np.empty(len(t), dtype=bool)
            first[0] = True
            first[1:] = minute[1:] != minute[:-1]
            if self._last_minute is not None and int(minute[0]) == self._last_minute:
                first[0] = False        # 上一块末尾未走完的分钟：不再采样
            self._last_minute = int(minute[-1])
            idx = np.flatnonzero(first)
        else:
            idx = np.arange(len(t))
        bid = ticks_df["bid"].to_numpy(dtype="float64")[idx]
        ask = ticks_df["ask"].to_numpy(dtype="float64")[idx]
        if self._mode == "relative":
            mid = (ask + bid) / 2.0
            if not bool(np.all(mid > 0)):
                raise ValueError(
                    f"{self._symbol} tick 存在 (ask+bid)/2 ≤ 0 的样本，"
                    "无法计算相对点差")
            vals = (ask - bid) / mid
        else:
            vals = ask - bid
        local = (pd.to_datetime(pd.Series(t[idx]), unit="ms", utc=True)
                 .dt.tz_convert(BUCKET_TZ))
        hours = local.dt.hour.to_numpy(dtype="int64")
        days = local.dt.normalize().astype("int64").to_numpy()
        for h in np.unique(hours):
            sel = hours == h
            key = int(h)
            self._vals.setdefault(key, []).append(vals[sel])
            self._days.setdefault(key, set()).update(int(d) for d in np.unique(days[sel]))

    def build(self) -> "SpreadModel":
        if not self._vals:
            raise ValueError(f"{self._symbol} tick 数据为空（无采样样本）")
        stats: dict[int, SpreadStat] = {}
        all_vals: list[np.ndarray] = []
        for h in sorted(self._vals):
            v = np.concatenate(self._vals[h])
            stats[h] = SpreadStat(
                median=float(np.quantile(v, 0.50)),
                p90=float(np.quantile(v, 0.90)),
                p99=float(np.quantile(v, 0.99)),
                n=int(v.size), n_days=len(self._days[h]),
            )
            all_vals.append(v)
        overall = float(np.quantile(np.concatenate(all_vals), 0.50))
        return SpreadModel({self._symbol: stats}, {self._symbol: overall},
                           mode=self._mode, sampling=self._sampling)


class SpreadModel:
    """多品种点差统计：{symbol: {纽约本地小时: SpreadStat}}。

    单品种模型可直接 stat(hour)/rollover_hours()；多品种（merge 后）
    须显式传 symbol。mode/sampling 是整模型统一口径（merge 时口径不一致即抛）。
    hour 参数一律为**纽约本地小时**（0..23）；缺统计小时见 missing_hours。
    """

    def __init__(self, per_symbol: dict[str, dict[int, SpreadStat]] | None = None,
                 overall_median: dict[str, float] | None = None,
                 *, mode: str = "relative",
                 sampling: str = "bar_open_m1"):
        if mode not in MODES:
            raise ValueError(f"mode 必须是 {MODES} 之一，收到 {mode!r}")
        if sampling not in SAMPLINGS:
            raise ValueError(f"sampling 必须是 {SAMPLINGS} 之一，收到 {sampling!r}")
        self.mode = str(mode)
        self.sampling = str(sampling)
        self._per_symbol: dict[str, dict[int, SpreadStat]] = {
            s: dict(h) for s, h in (per_symbol or {}).items()
        }
        self._overall_median: dict[str, float] = dict(overall_median or {})

    # ---- 构建 ----

    @classmethod
    def from_ticks(cls, symbol: str, ticks_df: pd.DataFrame,
                   *, mode: str = "relative",
                   sampling: str = "bar_open_m1") -> "SpreadModel":
        """从 tick 帧实测点差统计（口径见模块 docstring；小时=纽约本地）。

        sampling（T36）：bar_open_m1（缺省）= 每 M1 边界后第一笔 tick；
        tick = 全部 tick（旧口径）。分块喂入用 SpreadModelBuilder（结果一致）。
        """
        if mode not in MODES:
            raise ValueError(f"mode 必须是 {MODES} 之一，收到 {mode!r}")
        if sampling not in SAMPLINGS:
            raise ValueError(f"sampling 必须是 {SAMPLINGS} 之一，收到 {sampling!r}")
        if len(ticks_df) == 0:
            raise ValueError(f"{symbol} tick 数据为空")
        builder = SpreadModelBuilder(symbol, mode=mode, sampling=sampling)
        builder.add_ticks(ticks_df)
        return builder.build()

    def merge(self, *others: "SpreadModel") -> "SpreadModel":
        """合并多品种模型（品种重复或口径不一致即抛；空模型继承对方口径）。"""
        per = {s: dict(h) for s, h in self._per_symbol.items()}
        om = dict(self._overall_median)
        mode: str | None = self.mode if self._per_symbol else None
        sampling: str | None = self.sampling if self._per_symbol else None
        for o in others:
            if o._per_symbol:
                if mode is None:
                    mode = o.mode
                elif o.mode != mode:
                    raise ValueError(
                        f"点差模型口径不一致（{mode} vs {o.mode}），不能合并")
                if sampling is None:
                    sampling = o.sampling
                elif o.sampling != sampling:
                    raise ValueError(
                        f"点差模型采样口径不一致（{sampling} vs {o.sampling}），"
                        "不能合并")
            for s, h in o._per_symbol.items():
                if s in per:
                    raise ValueError(f"品种 {s} 重复，不能合并")
                per[s] = dict(h)
                om[s] = o._overall_median[s]
        return SpreadModel(per, om, mode=mode if mode is not None else self.mode,
                           sampling=(sampling if sampling is not None
                                     else self.sampling))

    # ---- 查询 ----

    def _resolve(self, symbol: str | None) -> str:
        if symbol is not None:
            if symbol not in self._per_symbol:
                raise ValueError(
                    f"{symbol} 不在点差模型中（品种：{sorted(self._per_symbol)}）")
            return symbol
        if len(self._per_symbol) == 1:
            return next(iter(self._per_symbol))
        raise ValueError(
            f"点差模型含 {len(self._per_symbol)} 个品种，须显式指定 symbol"
            f"（{sorted(self._per_symbol)}）")

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._per_symbol))

    def _sufficient(self, sym: str, min_days: int) -> dict[int, SpreadStat]:
        """覆盖达标（n_days ≥ min_days）的小时表；min_days ≤ 0 = 全表。"""
        table = self._per_symbol[sym]
        if min_days <= 0:
            return table
        return {h: st for h, st in table.items() if st.n_days >= min_days}

    def stat(self, hour: int, *, symbol: str | None = None) -> SpreadStat:
        """该小时（纽约本地）统计（单位见 SpreadStat）；多品种模型须显式 symbol。

        缺统计小时照抛 ValueError——本方法是原始查询；带保守回退的口径
        用 spread_p90 / max_p90（T20）。
        """
        sym = self._resolve(symbol)
        h = int(hour)
        table = self._per_symbol[sym]
        if h not in table:
            raise ValueError(
                f"{sym} 纽约本地小时 {h} 无点差统计（tick 覆盖缺失？）")
        return table[h]

    def missing_hours(self, symbol: str | None = None, *,
                      min_days_per_hour: int = 0) -> tuple[int, ...]:
        """该品种缺统计的纽约本地小时（升序；T20 禁入口径的数据源）。

        min_days_per_hour（T36）> 0 时，n_days 不足的小时也算缺统计
        （config/sessions.toml [costs] min_days_per_hour，缺省 20 由接线层
        传入；0 = 只看小时是否有统计的旧语义）。
        """
        sym = self._resolve(symbol)
        good = self._sufficient(sym, int(min_days_per_hour))
        return tuple(h for h in range(24) if h not in good)

    def max_p90(self, symbol: str | None = None, *,
                min_days_per_hour: int = 0) -> float:
        """全天（全部**覆盖达标**小时）最大 p90——缺统计小时的保守出场成本。"""
        sym = self._resolve(symbol)
        pool = self._sufficient(sym, int(min_days_per_hour))
        if not pool:                      # 无一小时达标：退回全表（仍需保守值）
            pool = self._per_symbol[sym]
        if not pool:
            raise ValueError(f"{sym} 无任何小时统计")
        return max(st.p90 for st in pool.values())

    def spread_p90(self, symbol: str, hour_ny: int,
                   *, price: float | None = None,
                   min_days_per_hour: int = 0) -> float:
        """结构化接口：价格单位，该小时 p90＝来回点差成本全幅（每来回恰收一次）。

        hour_ny 为**纽约本地小时**。缺统计小时不抛错：按 sessions.toml 的
        missing_hour_policy="forbid" 口径取全天最大 p90（出场侧保守成本；
        入场侧由接线层用 missing_hours 禁入该小时）。min_days_per_hour（T36）
        > 0 时 n_days 不足的小时同样按缺统计处理，且回退的最大 p90 只取
        覆盖达标的小时。
        relative 模型：返回 rel_p90×price——price 为该笔成交的 bid 基准价，
        缺失或非有限正数即抛 ValueError（相对口径必须知道当时价格，禁止静默
        按 0/按 1）；absolute 模型：忽略 price，直接返回价格单位 p90。
        """
        sym = self._resolve(symbol)
        h = int(hour_ny)
        eff = self._sufficient(sym, int(min_days_per_hour))
        if h in eff:
            v = eff[h].p90
        else:
            pool = eff or self._per_symbol[sym]
            v = max(st.p90 for st in pool.values()) if pool else 0.0
        if self.mode == "relative":
            if price is None:
                raise ValueError(
                    f"{symbol} 小时 {h}：相对点差口径需要 price"
                    f"（该笔成交的 bid 基准价），缺失即拒绝")
            p = float(price)
            if not math.isfinite(p) or p <= 0:
                raise ValueError(
                    f"{symbol} 小时 {h}：price 须为有限正数，收到 {price!r}")
            return float(v * p)
        return float(v)

    def rollover_hours(self, symbol: str | None = None) -> tuple[int, ...]:
        """结算/换日风险时段：p99 > 3×全体中位的整点（**纽约本地小时**）。

        阈值比较在同一口径内进行（relative/absolute 均适用）。
        """
        sym = self._resolve(symbol)
        base = 3.0 * self._overall_median[sym]
        return tuple(sorted(h for h, st in self._per_symbol[sym].items() if st.p99 > base))

    def overall_median(self, symbol: str | None = None) -> float:
        """该品种全部样本点差的中位数（rollover 判定基准；单位同口径）。"""
        return float(self._overall_median[self._resolve(symbol)])

    # ---- 持久化 ----

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "sampling": self.sampling,   # T36：采样口径（缺标注=旧 tick 口径）
            "bucket_tz": BUCKET_TZ,   # T20：小时键的时区口径（缺标注=旧 UTC 分桶）
            "per_symbol": {
                s: {str(h): asdict(st) for h, st in hours.items()}
                for s, hours in self._per_symbol.items()
            },
            "overall_median": dict(self._overall_median),
        }

    def save(self, path: str) -> None:
        """原子写 JSON（临时文件 + os.replace，读者不会看到半截文件）。"""
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str) -> "SpreadModel":
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        mode = d.get("mode", "absolute")   # 旧文件无 mode → 绝对口径（兼容）
        if mode not in MODES:
            raise ValueError(f"点差文件 mode 非法：{mode!r}（合法 {MODES}）")
        sampling = d.get("sampling", "tick")   # T36 前的文件无键 → tick 口径
        if sampling not in SAMPLINGS:
            raise ValueError(
                f"点差文件 sampling 非法：{sampling!r}（合法 {SAMPLINGS}）")
        bucket_tz = d.get("bucket_tz")
        if bucket_tz != BUCKET_TZ:
            raise ValueError(
                f"点差文件 bucket_tz={bucket_tz!r}（缺标注=UTC 分桶的旧文件），"
                f"与纽约本地小时口径不兼容，请用当前代码重建模型")
        per = {
            s: {int(h): SpreadStat(**st) for h, st in hours.items()}
            for s, hours in d["per_symbol"].items()
        }
        return cls(per, {s: float(v) for s, v in d["overall_median"].items()},
                   mode=mode, sampling=sampling)
