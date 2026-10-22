"""交易服务：开仓、挂单、改单、改持仓、撤单、平仓。

铁律（见设计文档）：网关不含任何策略逻辑，SL/TP 只透传；
一切交易请求经 MT5Connection.trade_send 在同一次持锁内通过实盘保险丝；
失败如实抛错；"只改一侧、另一侧保持现值"——绝不把未传的字段当 0 发出。
"""
from __future__ import annotations

import math

from mt5gw.account import AccountService
from mt5gw.config import GatewayConfig, ensure_trade_allowed
from mt5gw.connection import MT5Connection
from mt5gw.errors import (
    RETCode, DataUnavailableError, OrderResultUnknownError, TradeRejectedError,
)
from mt5gw.models import OrderResult
from mt5gw.symbols import SymbolCatalog

_FILL_FOK, _FILL_IOC, _FILL_RETURN = 0, 1, 2  # ORDER_FILLING_*，与包一致
_TIME_GTC = 0  # ORDER_TIME_GTC
# DONE=完成；DONE_PARTIAL=部分成交（仓位已建立）；PLACED=已受理（多见于挂单）
_OK_RETCODES = frozenset({RETCode.DONE, RETCode.DONE_PARTIAL, RETCode.PLACED})

_PENDING_TYPE = {
    ("buy", "limit"): "ORDER_TYPE_BUY_LIMIT",
    ("sell", "limit"): "ORDER_TYPE_SELL_LIMIT",
    ("buy", "stop"): "ORDER_TYPE_BUY_STOP",
    ("sell", "stop"): "ORDER_TYPE_SELL_STOP",
}


def pick_filling(filling_mode_bits: int) -> int:
    """按品种支持的位掩码（1=FOK，2=IOC）选首个可用成交方式，缺省 RETURN。"""
    if filling_mode_bits & 1:
        return _FILL_FOK
    if filling_mode_bits & 2:
        return _FILL_IOC
    return _FILL_RETURN


def clamp_volume(volume: float, vol_min: float, vol_max: float, step: float) -> float:
    """向步长向下取整并夹在 [vol_min, vol_max]；越界/非法一律拒绝，不静默修正。"""
    if not math.isfinite(volume) or volume <= 0:
        raise ValueError(f"非法交易量 {volume}")
    if step is None or step <= 0 or not math.isfinite(step):
        raise ValueError(f"品种步长非法 {step}")
    steps = math.floor(volume / step + 1e-9)
    rounded = round(steps * step, 8)
    if rounded < vol_min:
        raise ValueError(f"交易量 {volume} 低于最小交易量 {vol_min}")
    if rounded > vol_max:
        raise ValueError(f"交易量 {volume} 超过最大交易量 {vol_max}（拒绝，不静默压量）")
    return rounded


class TradingService:
    def __init__(
        self,
        conn: MT5Connection,
        config: GatewayConfig,
        account: AccountService,
        symbols: SymbolCatalog,
        mt5_module,
    ):
        self._conn = conn
        self._config = config
        self._account = account
        self._symbols = symbols
        self._mt5 = mt5_module

    def market_order(
        self,
        symbol: str,
        side: str,
        volume: float,
        *,
        sl: float | None = None,
        tp: float | None = None,
        deviation: int = 20,
        comment: str = "",
    ) -> OrderResult:
        if side not in ("buy", "sell"):
            raise ValueError(f"非法方向 {side}，仅接受 buy/sell")
        spec = self._symbols.get(symbol)
        request = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "type": self._mt5.ORDER_TYPE_BUY if side == "buy" else self._mt5.ORDER_TYPE_SELL,
            "volume": self._volume(volume, spec),
            "deviation": deviation,
            "magic": self._config.magic,
            "comment": comment,
            "type_time": _TIME_GTC,
            "type_filling": pick_filling(spec.filling_mode),
        }
        if sl is not None:
            request["sl"] = sl
        if tp is not None:
            request["tp"] = tp
        ref = self._ref_price(symbol, side)  # Exness 等券商要求市价单也带参考价
        if ref is not None:
            request["price"] = ref
        return self._send(request)

    def pending_order(
        self,
        symbol: str,
        side: str,
        kind: str,
        volume: float,
        price: float,
        *,
        sl: float | None = None,
        tp: float | None = None,
        comment: str = "",
    ) -> OrderResult:
        if side not in ("buy", "sell") or kind not in ("limit", "stop"):
            raise ValueError(f"非法挂单参数 side={side} kind={kind}")
        spec = self._symbols.get(symbol)
        request = {
            "action": self._mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "type": getattr(self._mt5, _PENDING_TYPE[(side, kind)]),
            "price": price,
            "volume": self._volume(volume, spec),
            "magic": self._config.magic,
            "comment": comment,
            "type_time": _TIME_GTC,
            "type_filling": pick_filling(spec.filling_mode),
        }
        if sl is not None:
            request["sl"] = sl
        if tp is not None:
            request["tp"] = tp
        return self._send(request)

    def modify(
        self,
        ticket: int,
        *,
        sl: float | None = None,
        tp: float | None = None,
        price: float | None = None,
    ) -> OrderResult:
        """改挂单；未传的字段回填当前值——MT5 语义里缺省即 0（等于删除）。"""
        order = self._account.pending_order(ticket)
        request = {
            "action": self._mt5.TRADE_ACTION_MODIFY,
            "order": ticket,
            "symbol": order.symbol,
            "price": order.price_open if price is None else price,
            "sl": order.sl if sl is None else sl,
            "tp": order.tp if tp is None else tp,
            "type_time": _TIME_GTC,
        }
        return self._send(request)

    def modify_position(
        self,
        ticket: int,
        *,
        sl: float | None = None,
        tp: float | None = None,
    ) -> OrderResult:
        """改持仓的 SL/TP（纯透传）。只改一侧时另一侧保持现值，绝不发 0。"""
        if sl is None and tp is None:
            raise ValueError("modify_position 至少要给 sl 或 tp 之一")
        pos = self._account.position(ticket)
        request = {
            "action": self._mt5.TRADE_ACTION_SLTP,
            "symbol": pos.symbol,
            "position": ticket,
            "sl": pos.sl if sl is None else sl,
            "tp": pos.tp if tp is None else tp,
        }
        return self._send(request)

    def cancel_order(self, ticket: int) -> OrderResult:
        """撤销挂单（TRADE_ACTION_REMOVE）。"""
        self._account.pending_order(ticket)  # 确认存在并拿到上下文，失败如实抛
        request = {"action": self._mt5.TRADE_ACTION_REMOVE, "order": ticket}
        return self._send(request)

    def close_position(
        self, ticket: int, volume: float | None = None, *, deviation: int = 20
    ) -> OrderResult:
        pos = self._account.position(ticket)
        spec = self._symbols.get(pos.symbol)
        if volume is None:
            vol = pos.volume
        else:
            vol = self._volume(volume, spec)
            if vol > pos.volume:
                raise TradeRejectedError(
                    RETCode.INVALID_VOLUME,
                    f"请求平仓 {vol} 手超出持仓余量 {pos.volume} 手",
                )
        close_side = "sell" if pos.side == "buy" else "buy"
        request = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "type": self._mt5.ORDER_TYPE_BUY if close_side == "buy" else self._mt5.ORDER_TYPE_SELL,
            "position": ticket,
            "volume": vol,
            "deviation": deviation,
            "magic": self._config.magic,
            "comment": f"close #{ticket}",
            "type_time": _TIME_GTC,
            "type_filling": pick_filling(spec.filling_mode),
        }
        ref = self._ref_price(pos.symbol, close_side)
        if ref is not None:
            request["price"] = ref
        return self._send(request)

    def _fuse_check(self, trade_mode: int) -> None:
        ensure_trade_allowed(trade_mode, self._config.allow_live_trading)

    def _volume(self, volume: float, spec) -> float:
        try:
            return clamp_volume(
                volume, spec.volume_min, spec.volume_max, spec.volume_step
            )
        except ValueError as exc:
            raise TradeRejectedError(RETCode.INVALID_VOLUME, str(exc)) from exc

    def _send(self, request: dict) -> OrderResult:
        # 唯一出口：保险丝+order_send 在连接层的同一次持锁内原子执行
        result = self._conn.trade_send(request, self._fuse_check)
        if result is None:
            raise OrderResultUnknownError(
                f"order_send 无返回（IPC 中断等）：订单实际状态未知，"
                f"请核对持仓后再决定是否重发。{self._conn.last_error()}"
            )
        if result.retcode not in _OK_RETCODES:
            raise TradeRejectedError(result.retcode, result.comment)
        confirmed = result.retcode != RETCode.PLACED
        return OrderResult(
            ok=True,
            confirmed=confirmed,
            retcode=result.retcode,
            comment=result.comment,
            order_ticket=result.order,
            deal_ticket=result.deal if result.deal else None,
            deal_price=result.price if confirmed and result.price else None,
            volume=result.volume,
        )

    def _ref_price(self, symbol: str, side: str) -> float | None:
        """下单参考价：买用 ask，卖用 bid；取不到就省略，让服务器如实拒绝。"""
        tick = self._conn.call(self._mt5.symbol_info_tick, symbol)
        if tick is None:
            return None
        return tick.ask if side == "buy" else tick.bid
