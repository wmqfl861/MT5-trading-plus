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
    trade_stops_level: int  # 最小止损距离（点），改单/挂单参考
    raw: Any
    # 以下为 P0 追加的合约参数（快照/成本模型用）：真包 5.0.6231 提供，
    # 旧式 row / 假件可能缺——映射侧 getattr 兜底，这里带默认值保证直接构造不崩
    swap_long: float = 0.0        # 多头隔夜库存费（点/手/日，负=付费）
    swap_short: float = 0.0       # 空头隔夜库存费
    trade_freeze_level: int = 0   # 冻结级别（点）：距现价过近的改单/挂单会被拒
    tick_size: float = 0.0        # 最小价格变动（包内名 trade_tick_size）
    tick_value: float = 0.0       # 单个 tick 的计价值（包内名 trade_tick_value）
    trade_calc_mode: int = 0      # 盈利计算模式
    margin_initial: float = 0.0   # 初始保证金率
    margin_maintenance: float = 0.0  # 维持保证金率
    # T5（R4）追加的隔夜费计费口径：真包 symbol_info 提供 swap_mode /
    # swap_rollover3days；旧式 row 缺——getattr 兜底（见 symbols._to_spec）。
    # swap_mode 取值（本机 MetaTrader5 包打印核对 2026-10-24，与
    # mt5.SYMBOL_SWAP_MODE_* 一致）：0=禁用 1=点数 2=品种货币 3=保证金货币
    # 4=存款货币 5=年利率(现价) 6=年利率(开仓价) 7=重开仓(现价) 8=重开仓(Bid)
    swap_mode: int = 0
    swap_rollover3days: int = 3   # 三倍隔夜费星期（MT5 枚举周日=0…周六=6；3=周三）
    # H2-03：MT5 SYMBOL_CURRENCY_BASE；旧终端/旧快照缺失时保持 None，
    # 不得用 currency_profit 冒充基础币种参与 mode=2 隔夜费换算。
    currency_base: str | None = None


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
    magic: int
    type_time: int  # 有效期类型（0=GTC，1=DAY…）
    stoplimit: float  # STOP_LIMIT 单的触发价（普通挂单为 0）
    expiration: datetime | None  # 0（无到期）如实为 None


# MT5 deal.reason 常量（T30/C1）：成交原因，外部出场识别（SL/TP/SO=已解释）
# 依据。取值以本机 MetaTrader5 包（5.0.6231）只读打印核对（2026-10-24，
# 仅 dir()/getattr，绝不 initialize），与包内 DEAL_REASON_* 逐字一致：
# 0=CLIENT（人工/终端）1=MOBILE 2=WEB 3=EXPERT（EA，即本执行器）
# 4=SL（止损）5=TP（止盈）6=SO（强平/保证金）7=ROLLOVER 8=VMARGIN 9=SPLIT
DEAL_REASON_CLIENT = 0
DEAL_REASON_MOBILE = 1
DEAL_REASON_WEB = 2
DEAL_REASON_EXPERT = 3
DEAL_REASON_SL = 4
DEAL_REASON_TP = 5
DEAL_REASON_SO = 6
DEAL_REASON_ROLLOVER = 7
DEAL_REASON_VMARGIN = 8
DEAL_REASON_SPLIT = 9


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
    # T30：MT5 deal.reason 原值（见上方常量；旧式 row / 出入金流水可能缺 → None）
    reason: int | None = None


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
    filled: bool  # True=已回报成交（DONE/DONE_PARTIAL）；PLACED（多见于挂单受理）为 False
    partial: bool  # True=DONE_PARTIAL 部分成交
    retcode: int
    comment: str
    order_ticket: int | None
    deal_ticket: int | None  # PLACED/未成交时为 None
    deal_price: float | None
    requested_volume: float | None
    volume: float | None  # 服务器回报的实际成交量（部分成交时 < 请求量）
