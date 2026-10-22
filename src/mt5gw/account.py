"""账户状态、持仓、挂单与历史。

包的歧义裁决：positions_get/orders_get/history_*_get 返回 None 既可能是
"没有"也可能是"出错"——在同一次持锁内读取 last_error：成功码 (1,'Success')
视为空集，否则如实抛错。绝不把查询失败伪装成"空仓"。
时间窗参数统一转整数秒传入，避开宿主机时区解释（见 README 时区约定）。
"""
from __future__ import annotations

import logging
from datetime import datetime

from mt5gw.connection import MT5Connection, _is_success
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
_DEAL_ENTRIES = {0: "in", 1: "out", 2: "inout", 3: "out_by"}
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
        volume=row.volume, price_open=row.price_open,
        price_current=row.price_current, sl=row.sl, tp=row.tp,
        swap=row.swap, profit=row.profit, time=_utc(row.time),
        comment=row.comment, magic=row.magic,
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
    entry = _DEAL_ENTRIES.get(getattr(row, "entry", -1), None)
    return Deal(
        ticket=row.ticket, order=row.order,
        position_id=getattr(row, "position_id", 0),
        symbol=row.symbol, side=side, entry=entry,
        volume=row.volume, price=row.price,
        profit=row.profit, commission=row.commission, swap=row.swap,
        fee=row.fee, time=_utc(row.time), comment=row.comment,
        magic=getattr(row, "magic", 0),
    )


def _history_order(row) -> HistoryOrder:
    done_raw = getattr(row, "time_done", 0)
    return HistoryOrder(
        ticket=row.ticket, symbol=row.symbol, kind=_kind(row.type),
        volume=row.volume, price_open=row.price_open, state=row.state,
        time_setup=_utc(row.time_setup),
        time_done=_utc(done_raw) if done_raw else None,  # 0 = 未成交/未完成，如实给 None
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

    def position(self, ticket: int) -> Position:
        """按 ticket 精确取单个持仓；不存在或查询失败都如实抛错。"""
        rows, err = self._conn.call_status(self._mt5.positions_get, ticket=ticket)
        if rows is None:
            if _is_success(err):
                raise DataUnavailableError(f"持仓 {ticket} 不存在（可能已平仓）")
            raise DataUnavailableError(f"查询持仓失败：{err}")
        for r in rows:
            if r.ticket == ticket:
                return _position(r)
        raise DataUnavailableError(f"持仓 {ticket} 不存在（可能已平仓）")

    def pending_order(self, ticket: int) -> PendingOrder:
        rows, err = self._conn.call_status(self._mt5.orders_get, ticket=ticket)
        if rows is None:
            if _is_success(err):
                raise DataUnavailableError(f"挂单 {ticket} 不存在（可能已成交/已撤销）")
            raise DataUnavailableError(f"查询挂单失败：{err}")
        for r in rows:
            if r.ticket == ticket:
                return _pending(r)
        raise DataUnavailableError(f"挂单 {ticket} 不存在（可能已成交/已撤销）")

    def positions(self, symbol: str | None = None) -> list[Position]:
        return self._list_amb(
            "positions_get",
            lambda: (
                self._conn.call_status(self._mt5.positions_get, symbol=symbol)
                if symbol is not None
                else self._conn.call_status(self._mt5.positions_get)
            ),
            _position, "持仓",
        )

    def pending_orders(self, symbol: str | None = None) -> list[PendingOrder]:
        return self._list_amb(
            "pending_orders",
            lambda: (
                self._conn.call_status(self._mt5.orders_get, symbol=symbol)
                if symbol is not None
                else self._conn.call_status(self._mt5.orders_get)
            ),
            _pending, "挂单",
        )

    def deals(self, since: datetime, to: datetime) -> list[Deal]:
        return self._list_amb(
            "deals",
            lambda: self._conn.call_status(
                self._mt5.history_deals_get,
                _epoch_seconds(since), _epoch_seconds(to),
            ),
            _deal, "成交历史",
        )

    def order_history(self, since: datetime, to: datetime) -> list[HistoryOrder]:
        return self._list_amb(
            "order_history",
            lambda: self._conn.call_status(
                self._mt5.history_orders_get,
                _epoch_seconds(since), _epoch_seconds(to),
            ),
            _history_order, "委托历史",
        )

    def _list_amb(self, label, fetch, mapper, cn):
        rows, err = fetch()
        if rows is None:
            if _is_success(err):
                logger.debug("%s 返回 None 且 last_error 为成功码，按空集处理", label)
                return []
            raise DataUnavailableError(f"查询{cn}失败：{err}")
        return [mapper(r) for r in rows]
