"""隔夜费模型：计费夜计数 + 三倍日，恒 ≤ 0（成本方向）。

口径（与经纪商对账时逐项核对）：
- 每日 charge_hour_utc（默认 21:00 UTC）为计费时刻；持仓区间 (from_ts, to_ts]
  跨过的每个计费时刻记 1 夜（from 恰在计费时刻不记、to 恰在计费时刻要记）；
- 计费时刻所在日期的星期 ∈ triple_weekdays（默认周三——FX 把周六日两夜并入
  周三夜）→ 该夜 ×3；
- **恒返回 ≤ 0**：费率本身为正（罕见）也按成本方向取负——回测宁可高估成本。

T5（R4）：SwapModel.from_snapshot 从品种快照（scripts/snapshot_specs.py 产出）
构造规则——swap_mode 决定换算，swap_rollover3days（MT5 星期枚举）决定三倍日。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

import pandas as pd

# ---- MT5 SYMBOL_SWAP_MODE_* 枚举（本机 MetaTrader5 包只读打印核对 2026-10-24，----
# ---- 与 mt5.SYMBOL_SWAP_MODE_* 逐个相等；写死常量避免研究层依赖终端包）        ----
SWAP_MODE_DISABLED = 0          # mt5.SYMBOL_SWAP_MODE_DISABLED：无隔夜费
SWAP_MODE_POINTS = 1            # mt5.SYMBOL_SWAP_MODE_POINTS：点数计
SWAP_MODE_CURRENCY_SYMBOL = 2   # mt5.SYMBOL_SWAP_MODE_CURRENCY_SYMBOL：品种基础货币计
SWAP_MODE_CURRENCY_MARGIN = 3   # mt5.SYMBOL_SWAP_MODE_CURRENCY_MARGIN：保证金货币计
SWAP_MODE_CURRENCY_DEPOSIT = 4  # mt5.SYMBOL_SWAP_MODE_CURRENCY_DEPOSIT：账户存款货币计
SWAP_MODE_INTEREST_CURRENT = 5  # mt5.SYMBOL_SWAP_MODE_INTEREST_CURRENT：年利率×现价
SWAP_MODE_INTEREST_OPEN = 6     # mt5.SYMBOL_SWAP_MODE_INTEREST_OPEN：年利率×开仓价
SWAP_MODE_REOPEN_CURRENT = 7    # mt5.SYMBOL_SWAP_MODE_REOPEN_CURRENT：重开仓（现价结算）
SWAP_MODE_REOPEN_BID = 8        # mt5.SYMBOL_SWAP_MODE_REOPEN_BID：重开仓（Bid 结算）

_SWAP_MODE_NAMES = {
    SWAP_MODE_DISABLED: "禁用",
    SWAP_MODE_POINTS: "点数",
    SWAP_MODE_CURRENCY_SYMBOL: "品种货币",
    SWAP_MODE_CURRENCY_MARGIN: "保证金货币",
    SWAP_MODE_CURRENCY_DEPOSIT: "存款货币",
    SWAP_MODE_INTEREST_CURRENT: "年利率(现价)",
    SWAP_MODE_INTEREST_OPEN: "年利率(开仓价)",
    SWAP_MODE_REOPEN_CURRENT: "重开仓(现价)",
    SWAP_MODE_REOPEN_BID: "重开仓(Bid)",
}


def mt5_to_py_weekday(mt5_day: int) -> int:
    """MT5 星期枚举（DAY_OF_WEEK_*：周日=0…周六=6）→ Python weekday（周一=0…周日=6）。"""
    if not 0 <= mt5_day <= 6:
        raise ValueError(f"MT5 星期枚举须 0..6（周日=0），收到 {mt5_day}")
    return (mt5_day - 1) % 7


@dataclass(frozen=True)
class SwapRule:
    """单品种隔夜规则（weekday 约定 Monday=0 … Sunday=6）。"""

    long_per_day: float
    short_per_day: float
    triple_weekdays: frozenset = frozenset({2})  # 周三
    charge_hour_utc: int = 21

    def __post_init__(self):
        if not 0 <= self.charge_hour_utc <= 23:
            raise ValueError(f"charge_hour_utc 须 0..23，收到 {self.charge_hour_utc}")
        object.__setattr__(self, "triple_weekdays", frozenset(self.triple_weekdays))
        for wd in self.triple_weekdays:
            if not 0 <= wd <= 6:
                raise ValueError(f"triple_weekdays 须 0..6（Monday=0），收到 {wd}")


def _as_naive_utc(ts) -> pd.Timestamp:
    """aware → 转 UTC 后贴 naive 标签（库内既定约定）。"""
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    return t


def charge_nights(from_ts, to_ts, charge_hour_utc: int,
                  triple_weekdays: frozenset = frozenset()) -> int:
    """(from_ts, to_ts] 内跨过的计费夜数（三倍日记 3；weekday 约定 Monday=0）。

    单位：夜（无单位）；from/to 接受 naive UTC 或 aware（后者先转 UTC）。
    """
    f = _as_naive_utc(from_ts)
    t = _as_naive_utc(to_ts)
    if t < f:
        raise ValueError(f"to_ts({to_ts}) 早于 from_ts({from_ts})")
    total = 0
    day = f.date()
    end = t.date()
    while day <= end:
        m = pd.Timestamp(day.year, day.month, day.day, charge_hour_utc)
        if f < m <= t:
            total += 3 if day.weekday() in triple_weekdays else 1
        day += timedelta(days=1)
    return total


def per_lot_per_night(entry: dict, *, account_currency: str = "USD") -> tuple[float, float]:
    """单个品种的快照条目 → (多头, 空头) 每手每夜金额（账户货币）。

    按 swap_mode 换算：
    - 点数（1）：金额 = swap × point × contract_size；
    - 品种/保证金/存款货币（2/3/4）：swap 即每手金额——但 2/3 的计价货币须等于
      账户货币（模式 2 按 MQL5 定义是品种基础货币计，快照无 currency_base，
      用计价货币 currency_profit 做一致性代理检查），不等则不猜汇率；
    - 禁用（0）：0；
    - 利率（5/6）与重开仓（7/8）：抛 NotImplementedError（fail-fast，不猜）。

    条目缺 swap_mode / swap_rollover3days（R4 之前的旧快照）→ KeyError，
    提示重跑 scripts/snapshot_specs.py——绝不静默按 0 计。
    """
    for key in ("swap_mode", "swap_rollover3days"):
        if key not in entry:
            raise KeyError(
                f"快照条目 {entry.get('name', '?')} 缺 {key}（旧快照），"
                "重跑 scripts/snapshot_specs.py 抓取后再构建隔夜费模型"
            )
    mode = entry["swap_mode"]
    swap_long = float(entry.get("swap_long", 0.0))
    swap_short = float(entry.get("swap_short", 0.0))
    name = entry.get("name", "?")
    if mode == SWAP_MODE_DISABLED:
        return 0.0, 0.0
    if mode == SWAP_MODE_POINTS:
        point = float(entry["point"])
        contract = float(entry["contract_size"])
        per_point = point * contract  # 1 点（point 单位）× 1 手的账户货币金额
        return swap_long * per_point, swap_short * per_point
    if mode in (SWAP_MODE_CURRENCY_SYMBOL, SWAP_MODE_CURRENCY_MARGIN,
                SWAP_MODE_CURRENCY_DEPOSIT):
        if mode == SWAP_MODE_CURRENCY_MARGIN:
            ccy = entry.get("currency_margin", "")
            if ccy != account_currency:
                raise NotImplementedError(
                    f"{name}：swap 按保证金货币 {ccy} 计，≠ 账户货币 "
                    f"{account_currency}，不做汇率换算（fail-fast）"
                )
        if mode == SWAP_MODE_CURRENCY_SYMBOL:
            ccy = entry.get("currency_profit", "")
            if ccy != account_currency:
                raise NotImplementedError(
                    f"{name}：swap 按品种货币计（快照以计价货币 {ccy} 代理），"
                    f"≠ 账户货币 {account_currency}，不做汇率换算（fail-fast）"
                )
        return swap_long, swap_short  # 已是每手金额（账户货币）
    raise NotImplementedError(
        f"{name}：swap_mode={mode}（{_SWAP_MODE_NAMES.get(mode, '未知')}）"
        "的换算未实现——利率类需年化口径、重开仓类需成交重开语义，均不猜"
    )


class SwapModel:
    """品种 → SwapRule 映射；未配置品种直接抛（fail-fast，不静默按 0 算）。"""

    def __init__(self, per_symbol: dict[str, SwapRule] | None = None):
        self._per_symbol: dict[str, SwapRule] = dict(per_symbol or {})

    @classmethod
    def from_snapshot(cls, snap: dict, symbols: Iterable[str], *,
                      charge_hour_utc: int,
                      account_currency: str = "USD") -> "SwapModel":
        """从品种快照（load_snapshot 的 dict）为指定品种构建规则。

        每手每夜金额换算见 per_lot_per_night；三倍日由 swap_rollover3days
        （MT5 星期枚举周日=0）换算成 Python weekday。旧快照缺字段或遇到
        未实现的 swap_mode 时如实抛错（KeyError / NotImplementedError）。
        """
        rules: dict[str, SwapRule] = {}
        for name in symbols:
            try:
                entry = snap[name]
            except KeyError:
                raise KeyError(
                    f"快照缺品种 {name}（有：{sorted(snap)}）") from None
            long_pd, short_pd = per_lot_per_night(
                entry, account_currency=account_currency)
            triple_py = mt5_to_py_weekday(entry["swap_rollover3days"])
            rules[name] = SwapRule(
                long_per_day=long_pd, short_per_day=short_pd,
                triple_weekdays=frozenset({triple_py}),
                charge_hour_utc=charge_hour_utc,
            )
        return cls(rules)

    def rule(self, symbol: str) -> SwapRule:
        try:
            return self._per_symbol[symbol]
        except KeyError:
            raise KeyError(
                f"{symbol} 未配置 SwapRule（已配置：{sorted(self._per_symbol)}）") from None

    def overnight_fee(self, symbol: str, side: str,
                      from_ts: datetime, to_ts: datetime) -> float:
        """结构化接口：计价货币/手，恒 ≤ 0（正费率也按成本方向取负）。"""
        rule_ = self.rule(symbol)
        if side not in ("long", "short"):
            raise ValueError(f"side 须为 'long'/'short'，收到 {side!r}")
        f = _as_naive_utc(from_ts)
        t = _as_naive_utc(to_ts)
        if t < f:
            raise ValueError(f"to_ts({to_ts}) 早于 from_ts({from_ts})")
        per_day = rule_.long_per_day if side == "long" else rule_.short_per_day
        nights = self._charge_nights(rule_, f, t)
        return -abs(nights * per_day)

    @staticmethod
    def _charge_nights(rule_: SwapRule, f: pd.Timestamp, t: pd.Timestamp) -> int:
        """(f, t] 内跨过的计费夜数（三倍日记 3）。"""
        return charge_nights(f, t, rule_.charge_hour_utc,
                             triple_weekdays=rule_.triple_weekdays)
