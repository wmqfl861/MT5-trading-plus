"""FakeMT5：离线替身，模拟 MetaTrader5 包的接口子集。

- 常量与真实包一致，供被测代码引用
- 每个 API 方法经 _record() 分发：并发检测 + 按名配置返回值/异常 + 调用计数
- 未配置的行为默认返回 None（贴近真实包的失败形态）
"""
import threading
from collections import namedtuple

SendResult = namedtuple(
    "SendResult",
    "retcode deal order volume price bid ask comment request_id retcode_external request",
)


def ok_send(price=1.1010, deal=777, order=888, volume=0.01, comment="done"):
    return SendResult(
        retcode=10009, deal=deal, order=order, volume=volume, price=price,
        bid=price - 0.0001, ask=price + 0.0001, comment=comment,
        request_id=1, retcode_external=0, request=None,
    )


class FakeMT5:
    # account trade modes
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_CONTEST = 1
    ACCOUNT_TRADE_MODE_REAL = 2

    # timeframes（值与真实包一致）
    TIMEFRAME_M1 = 1
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    TIMEFRAME_M30 = 30
    TIMEFRAME_H1 = 16385
    TIMEFRAME_H4 = 16388
    TIMEFRAME_D1 = 16408
    TIMEFRAME_W1 = 32769
    TIMEFRAME_MN1 = 49153

    # ticks
    COPY_TICKS_ALL = 2

    # order types / filling / actions
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_SLTP = 6
    TRADE_ACTION_MODIFY = 7
    TRADE_ACTION_REMOVE = 8
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_INVALID_FILL = 10030

    # symbol filling bits
    SYMBOL_FILLING_FOK = 1
    SYMBOL_FILLING_IOC = 2

    def __init__(self):
        self.initialize_result = True
        self.terminal_connected_value = True
        self.last_error_value = (0, "success")
        self.behaviors = {}  # name -> {"value":..., "error":..., "delay":...}
        self.call_counts = {}
        self._guard = threading.Lock()
        self._inside = 0
        self.max_concurrent = 0

    # ---- 行为配置与检查 ----
    def set_behavior(self, name, value=None, error=None, delay=0.0):
        self.behaviors[name] = {"value": value, "error": error, "delay": delay}

    def calls(self, name):
        return self.call_counts.get(name, 0)

    def _record(self, name, *args, **kwargs):
        with self._guard:
            self.call_counts[name] = self.call_counts.get(name, 0) + 1
            self._inside += 1
            self.max_concurrent = max(self.max_concurrent, self._inside)
        try:
            beh = self.behaviors.get(name)
            if beh is None:
                return None
            import time
            if beh["delay"]:
                time.sleep(beh["delay"])
            if beh["error"] is not None:
                raise beh["error"]
            value = beh["value"]
            return value(*args, **kwargs) if callable(value) else value
        finally:
            with self._guard:
                self._inside -= 1

    # ---- 生命周期 ----
    def initialize(self, path=None, **kwargs):
        ok = self.initialize_result
        self.call_counts["initialize"] = self.call_counts.get("initialize", 0) + 1
        return ok

    def shutdown(self):
        self.call_counts["shutdown"] = self.call_counts.get("shutdown", 0) + 1

    def terminal_connected(self):
        return self.terminal_connected_value

    def last_error(self):
        return self.last_error_value

    # ---- 数据 / 交易接口（默认 None，按需配置）----
    def symbols_total(self):
        return self._record("symbols_total")

    def symbols_get(self, *args, **kwargs):
        return self._record("symbols_get", *args, **kwargs)

    def symbol_info(self, symbol):
        return self._record("symbol_info", symbol)

    def symbol_select(self, symbol, enable=True):
        return self._record("symbol_select", symbol, enable)

    def symbol_info_tick(self, symbol):
        return self._record("symbol_info_tick", symbol)

    def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        return self._record("copy_rates_from_pos", symbol, timeframe, start_pos, count)

    def copy_ticks_from(self, symbol, from_time, count, flags):
        return self._record("copy_ticks_from", symbol, from_time, count, flags)

    def account_info(self):
        return self._record("account_info")

    def positions_get(self, symbol=None):
        return self._record("positions_get", symbol)

    def orders_get(self, symbol=None):
        return self._record("orders_get", symbol)

    def history_deals_get(self, date_from, date_to, **kwargs):
        return self._record("history_deals_get", date_from, date_to, **kwargs)

    def history_orders_get(self, date_from, date_to, **kwargs):
        return self._record("history_orders_get", date_from, date_to, **kwargs)

    def order_send(self, request):
        return self._record("order_send", request)

    def order_check(self, request):
        return self._record("order_check", request)
