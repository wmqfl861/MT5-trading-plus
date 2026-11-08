"""错误层级与 retcode 可读映射。"""


class GatewayError(Exception):
    """网关一切错误的基类。"""


class NotConnectedError(GatewayError):
    """终端不可达：初始化失败或重连仍失败。"""


class PreSendUnavailableError(NotConnectedError):
    """发送前无法确认账户状态，请求未发出（T183/O17-3）。

    trade_send 在调用 order_send 前的账户检查读不到账户（account_info
    为空）——此时请求**确定没有发出**（区别于回报未知）。归入
    NotConnectedError：执行器按 T141 的 blocked 路径处理（本轮作废、
    journal 终态 rejected/not_connected、不开新仓、不退出、写告警）。
    保险丝与账号不符（LiveTradingBlockedError）不在归并范围，仍照常
    上抛退出。
    """


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
    # T140（第十六轮 O16-2）：补齐 MQL5 文档中的确定态拒单码——此前缺失
    # 10021 等码，11-05 14:45:06 的 "10021 No prices" 拒单只能按"未知
    # 返回码"落日志/告警。
    INVALID_REQUEST = 10013     # TRADE_RETCODE_INVALID：请求无效
    INVALID_PRICE = 10015  # noqa: 保留语义映射（部分券商市价单要求参考价）
    INVALID_VOLUME = 10014
    INVALID_STOPS = 10016
    TRADE_DISABLED = 10017
    MARKET_CLOSED = 10018
    NO_MONEY = 10019
    PRICE_CHANGED = 10020       # TRADE_RETCODE_PRICE_CHANGED：价格已变化
    PRICE_OFF = 10021           # TRADE_RETCODE_PRICE_OFF：无报价可处理请求
    TOO_MANY_REQUESTS = 10024   # TRADE_RETCODE_TOO_MANY_REQUESTS：请求过频
    INVALID_FILL = 10030
    AUTO_TRADING_DISABLED = 10027  # 客户端（终端）侧未开启自动交易
    # T140（O16-7 更正）：MQL5 文档中 10031 是 TRADE_RETCODE_CONNECTION
    #（与交易服务器无连接），不是"客户端禁止此类交易"；10027 才是终端侧
    # 禁自动交易。旧名 CLIENT_DISABLED 已移除。
    NO_CONNECTION = 10031
    LIMIT_ORDERS = 10033        # TRADE_RETCODE_LIMIT_ORDERS：挂单数达上限
    LIMIT_VOLUME = 10034        # TRADE_RETCODE_LIMIT_VOLUME：总量达上限


_RETCODE_TEXT = {
    RETCode.INVALID_ARGUMENT: "客户端参数校验拒绝（请求未发出，状态确定）",
    RETCode.REQUOTE: "重新报价",
    RETCode.REJECT: "请求被拒绝",
    RETCode.CANCEL: "请求被交易者取消",
    RETCode.PLACED: "订单已受理（成交未确认）",
    RETCode.DONE: "请求已完成",
    RETCode.DONE_PARTIAL: "请求部分成交",
    RETCode.INVALID_REQUEST: "请求无效（缺必填字段或字段值非法）",
    RETCode.INVALID_PRICE: "请求中的价格无效",
    RETCode.INVALID_VOLUME: "请求中的订单量无效",
    RETCode.INVALID_STOPS: "请求中的止损/止盈无效",
    RETCode.TRADE_DISABLED: "交易被禁用",
    RETCode.MARKET_CLOSED: "市场关闭",
    RETCode.NO_MONEY: "资金不足",
    RETCode.PRICE_CHANGED: "价格已变化（请求发出后报价变动，需重新请求）",
    RETCode.PRICE_OFF: "无报价可处理请求（No prices）",
    RETCode.TOO_MANY_REQUESTS: "请求过于频繁（服务器限流）",
    RETCode.INVALID_FILL: "不支持的成交方式（filling mode）",
    RETCode.AUTO_TRADING_DISABLED: "终端未开启自动交易（Algo Trading 按钮）",
    RETCode.NO_CONNECTION: "与交易服务器无连接（TRADE_RETCODE_CONNECTION）",
    RETCode.LIMIT_ORDERS: "挂单数量已达账户上限",
    RETCode.LIMIT_VOLUME: "订单与持仓总量已达账户上限",
}

# T140（O16-7）：断线语义的 retcode。10031 在 MQL5 中是 TRADE_RETCODE_
# CONNECTION（与交易服务器无连接）——此前误标为"客户端禁止此类交易"会把
# 断线误导成配置问题。本次只更正描述与归类（网关仍按确定态被拒上抛
# TradeRejectedError，describe_retcode/日志按断线口径呈现）；断线的
# 有界重连与不退出处理属 T141，不在本层做。
DISCONNECTION_RETCODES = frozenset({RETCode.NO_CONNECTION})


def describe_retcode(retcode: int) -> str:
    return _RETCODE_TEXT.get(retcode, f"未知返回码 {retcode}")
