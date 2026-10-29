"""错误层级与 retcode 可读映射。"""


class GatewayError(Exception):
    """网关一切错误的基类。"""


class NotConnectedError(GatewayError):
    """终端不可达：初始化失败或重连仍失败。"""


class DataUnavailableError(GatewayError):
    """行情/品种数据取不到（含 mt5.last_error 详情）。"""

    def __init__(self, detail: str):
        super().__init__(f"数据不可用：{detail}")
        self.detail = detail


class TradeRejectedError(GatewayError):
    """服务器明确拒绝交易请求；retcode 与 comment 原样保留。"""

    def __init__(self, retcode: int, comment: str):
        super().__init__(f"交易被拒 [{retcode}] {describe_retcode(retcode)}：{comment}")
        self.retcode = retcode
        self.comment = comment


class OrderResultUnknownError(GatewayError):
    """order_send 无返回（IPC 中断等）——订单实际状态未知，必须核对持仓后再决定。"""


class LiveTradingBlockedError(GatewayError):
    """实盘保险丝：非模拟盘账户且未显式解锁。"""


class RETCode:
    # -2 不是 MT5 服务器 retcode，而是 MetaTrader5 Python 库 last_error 的
    # 客户端码（T89/R14-L5）：参数校验拒绝，请求未发出、状态确定。order_send/
    # order_check 无返回 + last_error=(-2, ...) 时网关归类为确定态被拒。
    INVALID_ARGUMENT = -2
    REQUOTE = 10004
    REJECT = 10006
    CANCEL = 10007
    PLACED = 10008
    DONE = 10009
    DONE_PARTIAL = 10010
    INVALID_PRICE = 10015  # noqa: 保留语义映射（部分券商市价单要求参考价）
    INVALID_VOLUME = 10014
    INVALID_STOPS = 10016
    TRADE_DISABLED = 10017
    MARKET_CLOSED = 10018
    NO_MONEY = 10019
    INVALID_FILL = 10030
    AUTO_TRADING_DISABLED = 10027  # 客户端（终端）侧未开启自动交易
    CLIENT_DISABLED = 10031  # 客户端禁止此类交易


_RETCODE_TEXT = {
    RETCode.INVALID_ARGUMENT: "客户端参数校验拒绝（请求未发出，状态确定）",
    RETCode.REQUOTE: "重新报价",
    RETCode.REJECT: "请求被拒绝",
    RETCode.CANCEL: "请求被交易者取消",
    RETCode.PLACED: "订单已受理（成交未确认）",
    RETCode.DONE: "请求已完成",
    RETCode.DONE_PARTIAL: "请求部分成交",
    RETCode.INVALID_PRICE: "请求中的价格无效",
    RETCode.INVALID_VOLUME: "请求中的订单量无效",
    RETCode.INVALID_STOPS: "请求中的止损/止盈无效",
    RETCode.TRADE_DISABLED: "交易被禁用",
    RETCode.MARKET_CLOSED: "市场关闭",
    RETCode.NO_MONEY: "资金不足",
    RETCode.INVALID_FILL: "不支持的成交方式（filling mode）",
    RETCode.AUTO_TRADING_DISABLED: "终端未开启自动交易（Algo Trading 按钮）",
    RETCode.CLIENT_DISABLED: "客户端禁止此类交易",
}


def describe_retcode(retcode: int) -> str:
    return _RETCODE_TEXT.get(retcode, f"未知返回码 {retcode}")
