"""交易服务：开仓、挂单、改单、平仓。

铁律（见设计文档）：网关不含任何策略逻辑，SL/TP 只透传；
实盘保险丝在触达 order_send 之前拦截；失败如实抛 TradeRejectedError。
"""
from __future__ import annotations

import math

from mt5gw.account import AccountService
from mt5gw.config import GatewayConfig, ensure_trade_allowed
from mt5gw.connection import MT5Connection
from mt5gw.errors import RETCode, DataUnavailableError, TradeRejectedError
from mt5gw.models import OrderResult
from mt5gw.symbols import SymbolCatalog

_FILL_FOK, _FILL_IOC, _FILL_RETURN = 0, 1, 2  # ORDER_FILLING_*，与包一致
_TIME_GTC = 0  # ORDER_TIME_GTC

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
    """向步长向下取整并夹在 [vol_min, vol_max]；取整后低于最小量即非法。"""
    if volume <= 0:
        raise ValueError(f"非法交易量 {volume}")
    steps = math.floor(volume / step + 1e-9)
    rounded = round(steps * step, 8)
    if rounded < vol_min:
        raise ValueError(f"交易量 {volume} 低于最小交易量 {vol_min}")
    return min(rounded, vol_max)


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
        self._fuse()
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
        self._fuse()
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
        self._fuse()
        request: dict = {"action": self._mt5.TRADE_ACTION_MODIFY, "order": ticket}
        if price is not None:
            request["price"] = price
        if sl is not None:
            request["sl"] = sl
        if tp is not None:
            request["tp"] = tp
        return self._send(request)

    def close_position(self, ticket: int, volume: float | None = None) -> OrderResult:
        self._fuse()
        pos = next((p for p in self._account.positions() if p.ticket == ticket), None)
        if pos is None:
            raise DataUnavailableError(f"持仓 {ticket} 不存在（可能已平仓）")
        spec = self._symbols.get(pos.symbol)
        vol = pos.volume if volume is None else self._volume(volume, spec)
        request = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "type": self._mt5.ORDER_TYPE_BUY if pos.side == "sell" else self._mt5.ORDER_TYPE_SELL,
            "position": ticket,
            "volume": vol,
            "deviation": 20,
            "magic": self._config.magic,
            "comment": f"close #{ticket}",
            "type_time": _TIME_GTC,
            "type_filling": pick_filling(spec.filling_mode),
        }
        return self._send(request)

    def _fuse(self) -> None:
        ensure_trade_allowed(
            self._account.state().trade_mode, self._config.allow_live_trading
        )

    def _volume(self, volume: float, spec) -> float:
        try:
            return clamp_volume(
                volume, spec.volume_min, spec.volume_max, spec.volume_step
            )
        except ValueError as exc:
            raise TradeRejectedError(RETCode.INVALID_VOLUME, str(exc)) from exc

    def _send(self, request: dict) -> OrderResult:
        result = self._conn.call(self._mt5.order_send, request)
        if result is None:
            raise TradeRejectedError(0, f"order_send 无返回：{self._conn.last_error()}")
        if result.retcode != RETCode.DONE:
            raise TradeRejectedError(result.retcode, result.comment)
        return OrderResult(
            ok=True,
            retcode=result.retcode,
            comment=result.comment,
            order_ticket=result.order,
            deal_ticket=result.deal,
            deal_price=result.price,
            volume=result.volume,
        )
