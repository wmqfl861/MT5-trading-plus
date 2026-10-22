"""账户状态、持仓、挂单与历史。

包的歧义约定：positions_get/orders_get 返回 None 既可能是"没有"也可能
是"出错"——本层按空集处理并记 debug 日志（见 README 已知约定）。
时间窗参数统一转整数秒传入，避开宿主机时区解释（见台账 Task5 裁决）。
"""
from __future__ import annotations

import logging
from datetime import datetime

from mt5gw.connection import MT5Connection
from mt5gw.errors import DataUnavailableError
from mt5gw.models import (
    AccountState, Deal, HistoryOrder, PendingOrder, Position,
    _epoch_seconds, _utc,
)

logger = logging.getLogger(__name__)

_SIDES = {0: "buy", 1: "sell"}  # POSITION_TYPE_*
# DEAL_TYPE_*：除买卖外还有出入金等流水（volume=0, price=0）
_DEAL_SIDES = {0: "buy", 1: "sell", 2: "balance", 3: "credit", 4: "charge",
               5: "correction", 6: "bonus"}
# ORDER_TYPE_*：0/1 是市价单（历史委托里最常见），2-5 挂单，6/7 止损限价等
_ORDER_KINDS = {0: "buy", 1: "sell", 2: "buy_limit", 3: "sell_limit",
                4: "buy_stop", 5: "sell_stop"}


def _kind(order_type: int) -> str:
    # 未知类型如实标注而非崩溃（券商自定义类型）
    return _ORDER_KINDS.get(order_type, f"type{order_type}")


def _state(row) -> AccountState:
    return AccountState(
        login=row.login, server=row.server, company=row.company,
        currency=row.currency, leverage=row.leverage,
        balance=row.balance, equity=row.equity,
        margin=row.margin, margin_free=row.margin_free,
        margin_mode=row.margin_mode, trade_mode=row.trade_mode,
    )


def _position(row) -> Position:
    return Position(
        ticket=row.ticket, symbol=row.symbol, side=_SIDES[row.type],
        volume=row.volume, price_open=row.price_open, sl=row.sl, tp=row.tp,
        profit=row.profit, time=_utc(row.time), comment=row.comment,
        magic=row.magic,
    )


def _pending(row) -> PendingOrder:
    return PendingOrder(
        ticket=row.ticket, symbol=row.symbol, kind=_kind(row.type),
        volume=row.volume, price_open=row.price_open,
        price_current=row.price_current, sl=row.sl, tp=row.tp,
        time_setup=_utc(row.time_setup), comment=row.comment,
    )


def _deal(row) -> Deal:
    side = _DEAL_SIDES.get(row.type, f"type{row.type}")
    return Deal(
        ticket=row.ticket, order=row.order, symbol=row.symbol,
        side=side, volume=row.volume, price=row.price,
        profit=row.profit, commission=row.commission, swap=row.swap,
        fee=row.fee, time=_utc(row.time), comment=row.comment,
    )


def _history_order(row) -> HistoryOrder:
    return HistoryOrder(
        ticket=row.ticket, symbol=row.symbol, kind=_kind(row.type),
        volume=row.volume, price_open=row.price_open, state=row.state,
        time_setup=_utc(row.time_setup),
        time_done=_utc(row.time_done) if row.time_done else _utc(row.time_setup),
        comment=row.comment,
    )


class AccountService:
    def __init__(self, conn: MT5Connection, mt5_module):
        self._conn = conn
        self._mt5 = mt5_module

    def state(self) -> AccountState:
        row = self._conn.call(self._mt5.account_info)
        if row is None:
            raise DataUnavailableError(f"账户信息不可用：{self._conn.last_error()}")
        return _state(row)

    def positions(self, symbol: str | None = None) -> list[Position]:
        rows = (
            self._conn.call(self._mt5.positions_get, symbol=symbol)
            if symbol is not None
            else self._conn.call(self._mt5.positions_get)
        )
        if rows is None:
            logger.debug("positions_get 返回 None（无持仓或查询失败），按空集处理")
            return []
        return [_position(r) for r in rows]

    def pending_orders(self, symbol: str | None = None) -> list[PendingOrder]:
        rows = (
            self._conn.call(self._mt5.orders_get, symbol=symbol)
            if symbol is not None
            else self._conn.call(self._mt5.orders_get)
        )
        if rows is None:
            logger.debug("orders_get 返回 None（无挂单或查询失败），按空集处理")
            return []
        return [_pending(r) for r in rows]

    def deals(self, since: datetime, to: datetime) -> list[Deal]:
        rows = self._conn.call(
            self._mt5.history_deals_get,
            _epoch_seconds(since), _epoch_seconds(to),
        )
        if rows is None:
            return []
        return [_deal(r) for r in rows]

    def order_history(self, since: datetime, to: datetime) -> list[HistoryOrder]:
        rows = self._conn.call(
            self._mt5.history_orders_get,
            _epoch_seconds(since), _epoch_seconds(to),
        )
        if rows is None:
            return []
        return [_history_order(r) for r in rows]
