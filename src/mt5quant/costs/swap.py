"""隔夜费模型：计费夜计数 + 三倍日，恒 ≤ 0（成本方向）。

口径 v2（T20 / H1 / 决策 D10，2026-10-26；Exness 官方规则）：
- 计费时刻 = charge_local（默认 "17:00"）在 charge_tz（默认 America/New_York）
  的墙上时刻，逐日经 zoneinfo 换算成 UTC——夏令时 21:00 GMT、冬令时 22:00 GMT。
  **禁止固定 UTC 小时**（铁律 v3 第 1 条：随夏令时移动的事件用本地时间定义）；
- 只数落在 charge_weekdays（默认周一..周五）上的计费时刻——Exness 全部品种组
  （含加密货币）周六、周日不收隔夜费；周末由三倍日补回；
- 计费时刻所在**本地日期**的星期 ∈ triple_weekdays（默认周三——FX/金属把
  周六日两夜并入周三夜）→ 该夜 ×3；
- 持仓区间 (from_ts, to_ts] 跨过的计费时刻记夜：from 恰在计费时刻不记、
  to 恰在计费时刻要记；
- **恒返回 ≤ 0**：费率本身为正（罕见）也按成本方向取负——回测宁可高估成本。

T5（R4）：SwapModel.from_snapshot 从品种快照（scripts/snapshot_specs.py 产出）
构造规则——swap_mode 决定换算，swap_rollover3days（MT5 星期枚举，经
mt5_to_py_weekday）决定三倍日。T20 起三倍日一律取快照值，实测校准路径同源
（不再出现 triple_weekdays=frozenset() 的"摊薄"口径）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from mt5data.sessions import _parse_hhmm, local_to_utc_naive

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
    """单品种隔夜规则（weekday 约定 Monday=0 … Sunday=6）。

    charge_local/charge_tz 定义计费时刻的**本地墙上时间**（DST 感知）；
    charge_weekdays 上的计费时刻才计费（默认工作日——周末不计，Exness 全品种组）。
    """

    long_per_day: float
    short_per_day: float
    triple_weekdays: frozenset = frozenset({2})  # 周三
    charge_weekdays: frozenset = frozenset({0, 1, 2, 3, 4})  # 周末不计（Exness 全部品种组）
    charge_local: str = "17:00"                   # 计费时刻：交易所本地（Exness 换日）
    charge_tz: str = "America/New_York"           # 夏令时 21:00 GMT / 冬令时 22:00 GMT

    def __post_init__(self):
        try:
            _parse_hhmm(self.charge_local)
        except ValueError as e:
            raise ValueError(f"charge_local 非法：{e}") from e
        try:
            ZoneInfo(self.charge_tz)
        except Exception as e:
            raise ValueError(f"charge_tz 未知时区：{self.charge_tz!r}") from e
        object.__setattr__(self, "triple_weekdays", frozenset(self.triple_weekdays))
        object.__setattr__(self, "charge_weekdays", frozenset(self.charge_weekdays))
        for name in ("triple_weekdays", "charge_weekdays"):
            for wd in getattr(self, name):
                if not 0 <= wd <= 6:
                    raise ValueError(f"{name} 须 0..6（Monday=0），收到 {wd}")


def _as_naive_utc(ts) -> pd.Timestamp:
    """aware → 转 UTC 后贴 naive 标签（库内既定约定）。"""
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    return t


def _charge_moment_utc(day: date, rule: SwapRule) -> datetime:
    """day（本地日）的计费时刻 → naive UTC datetime（zoneinfo，DST 感知）。"""
    return local_to_utc_naive(day, rule.charge_tz, rule.charge_local)


def charge_nights(from_ts, to_ts, rule: SwapRule) -> int:
    """(from_ts, to_ts] 内跨过的计费夜数（三倍日记 3；weekday 约定 Monday=0）。

    只数 charge_weekdays 上的计费时刻（默认周一..周五——Exness 周六周日不收）；
    计费时刻由 rule.charge_local / rule.charge_tz 经 zoneinfo 逐日换算。
    单位：夜（无单位）；from/to 接受 naive UTC 或 aware（后者先转 UTC）。
    """
    f = _as_naive_utc(from_ts)
    t = _as_naive_utc(to_ts)
    if t < f:
        raise ValueError(f"to_ts({to_ts}) 早于 from_ts({from_ts})")
    tz = ZoneInfo(rule.charge_tz)
    # 本地日范围放宽一天：UTC 时刻与其本地日可能差一个日历日
    day = f.replace(tzinfo=timezone.utc).astimezone(tz).date() - timedelta(days=1)
    end = t.replace(tzinfo=timezone.utc).astimezone(tz).date() + timedelta(days=1)
    total = 0
    while day <= end:
        wd = day.weekday()
        if wd in rule.charge_weekdays:
            m = _charge_moment_utc(day, rule)
            if f < m <= t:
                total += 3 if wd in rule.triple_weekdays else 1
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
                      account_currency: str = "USD") -> "SwapModel":
        """从品种快照（load_snapshot 的 dict）为指定品种构建规则。

        每手每夜金额换算见 per_lot_per_night；三倍日由 swap_rollover3days
        （MT5 星期枚举周日=0）换算成 Python weekday——一律取快照值（T20）。
        计费时刻用 SwapRule 默认（17:00 America/New_York，DST 感知）。
        旧快照缺字段或遇到未实现的 swap_mode 时如实抛错
        （KeyError / NotImplementedError）。
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
        nights = charge_nights(f, t, rule_)
        return -abs(nights * per_day)
