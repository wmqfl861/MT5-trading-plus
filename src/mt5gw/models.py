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
    # 把 naive UTC 标签 datetime 转回整数秒；传给 mt5 时避免宿主机时区解释
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
    sl: float
    tp: float
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
    symbol: str
    side: str
    volume: float
    price: float
    profit: float
    commission: float
    swap: float
    fee: float
    time: datetime
    comment: str


@dataclass(frozen=True)
class HistoryOrder:
    ticket: int
    symbol: str
    kind: str
    volume: float
    price_open: float
    state: int
    time_setup: datetime
    time_done: datetime
    comment: str
