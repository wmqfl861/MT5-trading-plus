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
BUCKET_TZ = "America/New_York"   # T20：小时分桶口径（换日 17:00 所在时区）


@dataclass(frozen=True)
class SpreadStat:
    """单小时点差统计。relative 模型为无量纲比率；absolute 模型为价格单位。"""

    median: float
    p90: float
    p99: float
    n: int


class SpreadModel:
    """多品种点差统计：{symbol: {纽约本地小时: SpreadStat}}。

    单品种模型可直接 stat(hour)/rollover_hours()；多品种（merge 后）
    须显式传 symbol。mode 是整模型统一口径（merge 时口径不一致即抛）。
    hour 参数一律为**纽约本地小时**（0..23）；缺统计小时见 missing_hours。
    """

    def __init__(self, per_symbol: dict[str, dict[int, SpreadStat]] | None = None,
                 overall_median: dict[str, float] | None = None,
                 *, mode: str = "relative"):
        if mode not in MODES:
            raise ValueError(f"mode 必须是 {MODES} 之一，收到 {mode!r}")
        self.mode = str(mode)
        self._per_symbol: dict[str, dict[int, SpreadStat]] = {
            s: dict(h) for s, h in (per_symbol or {}).items()
        }
        self._overall_median: dict[str, float] = dict(overall_median or {})

    # ---- 构建 ----

    @classmethod
    def from_ticks(cls, symbol: str, ticks_df: pd.DataFrame,
                   *, mode: str = "relative") -> "SpreadModel":
        """从 tick 帧实测点差统计（口径见模块 docstring；小时=纽约本地）。"""
        if mode not in MODES:
            raise ValueError(f"mode 必须是 {MODES} 之一，收到 {mode!r}")
        missing = [c for c in ("time_msc", "bid", "ask") if c not in ticks_df.columns]
        if missing:
            raise ValueError(f"tick 帧缺列 {missing}（只需 time_msc/bid/ask）")
        if len(ticks_df) == 0:
            raise ValueError(f"{symbol} tick 数据为空")
        bid = ticks_df["bid"].to_numpy(dtype="float64")
        ask = ticks_df["ask"].to_numpy(dtype="float64")
        if mode == "relative":
            mid = (ask + bid) / 2.0
            if not bool(np.all(mid > 0)):
                raise ValueError(
                    f"{symbol} tick 存在 (ask+bid)/2 ≤ 0 的样本，无法计算相对点差")
            spread = (ask - bid) / mid
        else:
            spread = ask - bid
        hour = (pd.to_datetime(ticks_df["time_msc"], unit="ms", utc=True)
                .dt.tz_convert(BUCKET_TZ).dt.hour.to_numpy())
        stats: dict[int, SpreadStat] = {}
        for h in np.unique(hour):
            v = spread[hour == h]
            stats[int(h)] = SpreadStat(
                median=float(np.quantile(v, 0.50)),
                p90=float(np.quantile(v, 0.90)),
                p99=float(np.quantile(v, 0.99)),
                n=int(v.size),
            )
        overall = float(np.quantile(spread, 0.50))
        return cls({symbol: stats}, {symbol: overall}, mode=mode)

    def merge(self, *others: "SpreadModel") -> "SpreadModel":
        """合并多品种模型（品种重复或口径不一致即抛；空模型继承对方口径）。"""
        per = {s: dict(h) for s, h in self._per_symbol.items()}
        om = dict(self._overall_median)
        mode: str | None = self.mode if self._per_symbol else None
        for o in others:
            if o._per_symbol:
                if mode is None:
                    mode = o.mode
                elif o.mode != mode:
                    raise ValueError(
                        f"点差模型口径不一致（{mode} vs {o.mode}），不能合并")
            for s, h in o._per_symbol.items():
                if s in per:
                    raise ValueError(f"品种 {s} 重复，不能合并")
                per[s] = dict(h)
                om[s] = o._overall_median[s]
        return SpreadModel(per, om, mode=mode if mode is not None else self.mode)

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

    def missing_hours(self, symbol: str | None = None) -> tuple[int, ...]:
        """该品种缺统计的纽约本地小时（升序；T20 禁入口径的数据源）。"""
        sym = self._resolve(symbol)
        return tuple(h for h in range(24) if h not in self._per_symbol[sym])

    def max_p90(self, symbol: str | None = None) -> float:
        """全天（全部有统计小时）最大 p90——缺统计小时的保守出场成本。"""
        sym = self._resolve(symbol)
        table = self._per_symbol[sym]
        if not table:
            raise ValueError(f"{sym} 无任何小时统计")
        return max(st.p90 for st in table.values())

    def spread_p90(self, symbol: str, hour_ny: int,
                   *, price: float | None = None) -> float:
        """结构化接口：价格单位，该小时 p90＝来回点差成本全幅（每来回恰收一次）。

        hour_ny 为**纽约本地小时**。缺统计小时不抛错：按 sessions.toml 的
        missing_hour_policy="forbid" 口径取全天最大 p90（出场侧保守成本；
        入场侧由接线层用 missing_hours 禁入该小时）。
        relative 模型：返回 rel_p90×price——price 为该笔成交的 bid 基准价，
        缺失或非有限正数即抛 ValueError（相对口径必须知道当时价格，禁止静默
        按 0/按 1）；absolute 模型：忽略 price，直接返回价格单位 p90。
        """
        table = self._per_symbol[self._resolve(symbol)]
        h = int(hour_ny)
        if h in table:
            v = table[h].p90
        else:
            v = max(st.p90 for st in table.values()) if table else 0.0
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
        """该品种全部 tick 点差的中位数（rollover 判定基准；单位同口径）。"""
        return float(self._overall_median[self._resolve(symbol)])

    # ---- 持久化 ----

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
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
                   mode=mode)
