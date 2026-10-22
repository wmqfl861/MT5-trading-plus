"""对上暴露的数据结构（frozen dataclass）。

时间约定：MT5 包返回的秒/毫秒时间戳转成"贴着 UTC 标签的 naive datetime"
（实为经纪商服务器时间，见 README）。raw 字段保留包返回的原始对象。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def _utc(ts_seconds: float) -> datetime:
    # 3.12 起 utcfromtimestamp 已弃用；等价写法且无时区偏移
    return datetime.fromtimestamp(ts_seconds, tz=timezone.utc).replace(tzinfo=None)


def _epoch_seconds(dt: datetime) -> int:
    # 只接受 naive（UTC 标签）datetime；带时区的输入会被静默错移，当场拒绝
    if dt.tzinfo is not None:
        raise ValueError(
            "只接受不带时区的 naive datetime（服务器时间贴 UTC 标签），"
            f"收到 tzinfo={dt.tzinfo}"
        )
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


@dataclass(frozen=True)
class SymbolSpec:
    name: str
    description: str
    digits: int
    point: float
    spread: int
    trade_mode: int
    currency_profit: str
    currency_margin: str
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    filling_mode: int
    raw: Any


@dataclass(frozen=True)
class Quote:
    symbol: str
    time: datetime
    bid: float
    ask: float
    last: float
    volume: float


@dataclass(frozen=True)
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    tick_volume: int
    spread: int
    real_volume: int


@dataclass(frozen=True)
class Tick:
    time: datetime
    bid: float
    ask: float
    last: float
    volume: float


@dataclass(frozen=True)
class AccountState:
    login: int
    server: str
    company: str
    currency: str
    leverage: int
    balance: float
    equity: float
    margin: float
    margin_free: float
    margin_mode: int
    trade_mode: int  # 0=DEMO 1=CONTEST 2=REAL，实盘保险丝依赖


@dataclass(frozen=True)
class Position:
    ticket: int
    symbol: str
    side: str  # "buy"/"sell"
    volume: float
    price_open: float
    price_current: float
    sl: float
    tp: float
    swap: float
    profit: float
    time: datetime
    comment: str
    magic: int


@dataclass(frozen=True)
class PendingOrder:
    ticket: int
    symbol: str
    kind: str  # buy_limit/sell_limit/buy_stop/sell_stop
    volume: float
    price_open: float
    price_current: float
    sl: float
    tp: float
    time_setup: datetime
    comment: str


@dataclass(frozen=True)
class Deal:
    ticket: int
    order: int
    position_id: int
    symbol: str
    side: str
    entry: str | None  # "in"/"out"/"inout"/"out_by"，出入金流水为 None
    volume: float
    price: float
    profit: float
    commission: float
    swap: float
    fee: float
    time: datetime
    comment: str
    magic: int


@dataclass(frozen=True)
class HistoryOrder:
    ticket: int
    symbol: str
    kind: str
    volume: float
    price_open: float
    state: int
    time_setup: datetime
    time_done: datetime | None  # 0（未完成）如实为 None
    comment: str


@dataclass(frozen=True)
class OrderResult:
    ok: bool  # retcode ∈ {DONE, DONE_PARTIAL, PLACED}
    confirmed: bool  # PLACED（已受理未确认成交）为 False，调用方需自行核对
    retcode: int
    comment: str
    order_ticket: int | None
    deal_ticket: int | None
    deal_price: float | None
    volume: float | None  # 服务器回报的实际成交量（部分成交时 < 请求量）
