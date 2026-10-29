"""交易服务：开仓、挂单、改单、改持仓、撤单、平仓。

铁律（见设计文档）：网关不含任何策略逻辑，SL/TP 只透传；
一切交易请求经 MT5Connection.trade_send 在同一次持锁内通过实盘保险丝
（解锁实盘必须钉死 expected_login）；失败如实抛错；
"只改一侧、另一侧保持现值"——绝不把未传的字段当 0 发出。
"""
from __future__ import annotations

import logging
import math
import re

from mt5gw.account import AccountService
from mt5gw.config import GatewayConfig, ensure_trade_allowed
from mt5gw.connection import MT5Connection
from mt5gw.errors import (
    RETCode, DataUnavailableError, LiveTradingBlockedError,
    OrderResultUnknownError, TradeRejectedError,
)
from mt5gw.models import OrderResult, _epoch_seconds
from mt5gw.symbols import SymbolCatalog

_FILL_FOK, _FILL_IOC, _FILL_RETURN = 0, 1, 2  # ORDER_FILLING_*，与包一致
_TIME_GTC = 0  # ORDER_TIME_GTC
# DONE=完成；DONE_PARTIAL=部分成交（仓位已建立）；PLACED=已受理（多见于挂单）
_OK_RETCODES = frozenset({RETCode.DONE, RETCode.DONE_PARTIAL, RETCode.PLACED})
_REQUOTE_ATTEMPTS = 3  # 10004 Requote 的有界重试（每次重取参考价）

logger = logging.getLogger(__name__)

# MT5 订单 comment 安全化（F 阶段停修 2026-10-28；T89/R14-L5 根因修正）：
# 首战事故 comment="XAUUSD|M15|2026-10-28T06:30:00"（30 字符）被拒，
# last_error=(-2, 'Invalid "comment" argument')。**根因是长度，不是字符**——
# 2026-10-29 17:33 UTC 真实 Demo 终端 order_check 实测（scripts/
# order_check_comments.py，只读校验未发单，XAUUSD 最小手数）：
#   29 字符纯字母数字 → retcode=0 "Done"（通过）
#   30 字符纯字母数字 → 无返回，last_error=(-2, 'Invalid "comment" argument')（被拒）
#   短注释 "XAUUSD|M15"（10 字符含 '|'）→ retcode=0 "Done"（通过：'|' 不致拒）
# 与公开报告一致（≥30 拒/29 过）。字符白名单仍保留（保守卫生），但上限改为
# 25（留 4 字符余量，docs/reviews/2026-10-29-review-round14.md L5）。
_COMMENT_UNSAFE_RE = re.compile(r"[^A-Za-z0-9 _\-.]")
MT5_COMMENT_MAX = 25  # 实测 ≥30 被 MT5 Python 库客户端校验拒绝，保守取 25


def sanitize_comment(comment: str | None) -> str:
    """把 comment 安全化为 MT5 可接受形态（网关最后一道防线）。

    白名单外字符逐个替换为 '_'（保对齐、不丢位），再截断至 25 字符
    （实测 ≥30 被 MT5 Python 库以 -2 拒绝）；None/空串 → ""（合法缺省）。
    合法 comment 原样返回。
    """
    if not comment:
        return ""
    return _COMMENT_UNSAFE_RE.sub("_", comment)[:MT5_COMMENT_MAX]

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
        magic: int | None = None,
    ) -> OrderResult:
        if side not in ("buy", "sell"):
            raise ValueError(f"非法方向 {side}，仅接受 buy/sell")
        comment = self._safe_comment(comment)
        spec = self._symbols.get(symbol)  # 含可见性保证（不在 Market Watch 会自动加入）
        request = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "type": self._mt5.ORDER_TYPE_BUY if side == "buy" else self._mt5.ORDER_TYPE_SELL,
            "volume": self._volume(volume, spec),
            "deviation": deviation,
            "magic": self._config.magic if magic is None else magic,  # 每策略可逐单覆盖
            "comment": comment,
            "type_time": _TIME_GTC,
            "type_filling": pick_filling(spec.filling_mode),
        }
        if sl is not None:
            request["sl"] = sl
        if tp is not None:
            request["tp"] = tp
        return self._send_deal_with_requote(request, symbol, side)

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
        magic: int | None = None,
    ) -> OrderResult:
        if side not in ("buy", "sell") or kind not in ("limit", "stop"):
            raise ValueError(f"非法挂单参数 side={side} kind={kind}")
        comment = self._safe_comment(comment)
        spec = self._symbols.get(symbol)
        request = {
            "action": self._mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "type": getattr(self._mt5, _PENDING_TYPE[(side, kind)]),
            "price": price,
            "volume": self._volume(volume, spec),
            "magic": self._config.magic if magic is None else magic,  # 每策略可逐单覆盖
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
        """改挂单；未传的字段回填当前值——MT5 语义里缺省即 0（等于删除/重置）。"""
        order = self._account.pending_order(ticket)
        request = {
            "action": self._mt5.TRADE_ACTION_MODIFY,
            "order": ticket,
            "symbol": order.symbol,
            "price": order.price_open if price is None else price,
            "sl": order.sl if sl is None else sl,
            "tp": order.tp if tp is None else tp,
            "type_time": order.type_time,          # 保持原有效期（GTC/DAY/…）
            "stoplimit": order.stoplimit,          # STOP_LIMIT 单的触发价，保持现值
        }
        if order.expiration is not None:
            request["expiration"] = _epoch_seconds(order.expiration)
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
        self, ticket: int, volume: float | None = None, *, deviation: int = 20,
        magic: int | None = None,
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
            "magic": self._config.magic if magic is None else magic,  # 每策略可逐单覆盖
            # '#' 不在白名单：构造即安全（close 9001），免得每次平仓告警
            "comment": f"close {ticket}",
            "type_time": _TIME_GTC,
            "type_filling": pick_filling(spec.filling_mode),
        }
        return self._send_deal_with_requote(request, pos.symbol, close_side)

    # ---- 内部 ----

    def _safe_comment(self, comment: str) -> str:
        """入口预校验：不合法即安全化并告警记录 原文→安全文（绝不静默）。"""
        safe = sanitize_comment(comment)
        if safe != comment:
            logger.warning(
                "MT5 comment 不合法，已安全化：原文=%r → 安全文=%r"
                "（白名单 [A-Za-z0-9 _-.] + %d 截断；F 阶段首战根因"
                ' \'Invalid "comment" argument\'）',
                comment, safe, MT5_COMMENT_MAX)
        return safe

    def _send_deal_with_requote(self, request: dict, symbol: str, side: str) -> OrderResult:
        """市价类请求：10004 Requote 时重取参考价有界重试（执行层鲁棒性）。"""
        ref = self._ref_price(symbol, side)
        if ref is not None:
            request["price"] = ref
        for attempt in range(1, _REQUOTE_ATTEMPTS + 1):
            try:
                return self._send(request)
            except TradeRejectedError as exc:
                if exc.retcode != RETCode.REQUOTE or attempt == _REQUOTE_ATTEMPTS:
                    raise
                ref = self._ref_price(symbol, side)  # 行情在走，重取参考价
                if ref is not None:
                    request["price"] = ref
        raise AssertionError("unreachable")

    def _fuse_check(self, trade_mode: int) -> None:
        ensure_trade_allowed(trade_mode, self._config.allow_live_trading)

    def _login_check(self, login: int) -> None:
        expected = self._config.expected_login
        if expected is not None and login != expected:
            raise LiveTradingBlockedError(
                f"当前账号 {login} 与配置钉死的 expected_login={expected} 不符，拒单。"
            )

    def _volume(self, volume: float, spec) -> float:
        try:
            return clamp_volume(
                volume, spec.volume_min, spec.volume_max, spec.volume_step
            )
        except ValueError as exc:
            raise TradeRejectedError(RETCode.INVALID_VOLUME, str(exc)) from exc

    def _send(self, request: dict) -> OrderResult:
        # 唯一出口：保险丝+账号钉死+order_send 在连接层同一次持锁内原子执行
        result, err = self._conn.trade_send(
            request, self._fuse_check, self._login_check
        )
        if result is None:
            # T89/R14-L5 错误分类（按状态确定性，不按错误来源）：
            # - last_error 码 -2 = **客户端**参数校验拒绝——请求根本没发出
            #   （order_check 实测同形态：30 字符注释 → 无返回 + -2），
            #   必然零成交零挂单，状态**确定** → TradeRejectedError，
            #   不走"核对持仓"流程；
            # - 其余（IPC 中断等）order_send 无返回 → 状态**未知** →
            #   OrderResultUnknownError，必须核对持仓后再决定是否重发。
            code = err[0] if err else None
            if code == RETCode.INVALID_ARGUMENT:
                raise TradeRejectedError(
                    int(code),
                    f"客户端参数校验拒绝（请求未发出，确定态）：{err}",
                )
            raise OrderResultUnknownError(
                f"order_send 无返回（IPC 中断等）：订单实际状态未知，"
                f"请核对持仓后再决定是否重发。last_error={err}"
            )
        if result.retcode not in _OK_RETCODES:
            raise TradeRejectedError(result.retcode, result.comment)
        filled = result.retcode in (RETCode.DONE, RETCode.DONE_PARTIAL)
        return OrderResult(
            ok=True,
            filled=filled,  # True=已回报成交（DONE/DONE_PARTIAL）；PLACED 为未成交
            partial=result.retcode == RETCode.DONE_PARTIAL,
            retcode=result.retcode,
            comment=result.comment,
            order_ticket=result.order,
            deal_ticket=result.deal if result.deal else None,
            deal_price=result.price if filled and result.price else None,
            requested_volume=request.get("volume"),
            volume=result.volume,
        )

    def _ref_price(self, symbol: str, side: str) -> float | None:
        """下单参考价：买用 ask，卖用 bid；取不到就省略，让服务器如实拒绝。"""
        tick = self._conn.call(self._mt5.symbol_info_tick, symbol)
        if tick is None:
            return None
        return tick.ask if side == "buy" else tick.bid
