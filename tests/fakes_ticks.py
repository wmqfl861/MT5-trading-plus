"""FakeTicksMT5：tick 假件，逐条贴真包 MetaTrader5（5.0.x）的行为基线。

真包实测行为（与 tests/fakes.py 的 FakeMT5 同源约定）：
- ``COPY_TICKS_ALL == -1``
- ``copy_ticks_range`` 返回 numpy 结构化数组，dtype 字段名恰为
  ``mt5data.ticks.TICK_COLUMNS``（time_msc/bid/ask/last/volume/flags），
  单行是 numpy.void；窗口内无数据返回 ``()``；失败返回 ``None``
  且 ``last_error()`` 给出 (code, "描述")（成功码 (1, 'Success')）。
"""
from collections import namedtuple

import numpy as np

TerminalInfo = namedtuple("TerminalInfo", "connected trade_allowed")

# 字段名/顺序贴真包；位宽按本库存储规范（flags 用 int64）
TICK_DTYPE = np.dtype([
    ("time_msc", "int64"),
    ("bid", "float64"),
    ("ask", "float64"),
    ("last", "float64"),
    ("volume", "float64"),
    ("flags", "int64"),
])


def ticks_array(rows):
    """[(time_msc, bid, ask, last, volume, flags), ...] → 结构化数组（贴真包）。"""
    return np.array(list(rows), dtype=TICK_DTYPE)


class FakeTicksMT5:
    """按页持有 tick 历史：请求窗口完整落入某页的 [from_s, to_s] 范围即返回该页过滤结果。

    pages 元素是 dict：
    - ``from_s`` / ``to_s``：页覆盖的 epoch 秒范围（含两端）
    - ``rows``：该页全部 tick 元组（按请求窗口的毫秒范围过滤后返回）
    - 或 ``error``：如 (4, "No history data") → 返回 None 且 last_error 同步
    未被任何页覆盖的窗口返回 ``()``（真包无数据形态）。
    时间参数约定为 epoch 秒 int（与 mt5gw.market._epoch_seconds 一致）。
    """

    COPY_TICKS_ALL = -1  # 真包值

    def __init__(self):
        self.pages = []
        self.last_error_value = (1, "Success")  # 真包成功码
        self.copy_ticks_range_calls = []

    # ---- MT5Connection 所需最小面 ----
    def initialize(self, path=None, **kwargs):
        return True

    def shutdown(self):
        pass

    def terminal_info(self):
        return TerminalInfo(connected=True, trade_allowed=True)

    def last_error(self):
        return self.last_error_value

    # ---- 数据接口 ----
    def copy_ticks_range(self, symbol, from_time, to_time, flags):
        self.copy_ticks_range_calls.append((symbol, from_time, to_time, flags))
        for page in self.pages:
            if page["from_s"] <= from_time and to_time <= page["to_s"]:
                if page.get("error") is not None:
                    self.last_error_value = page["error"]
                    return None
                if "result" in page:  # 直接指定返回形态（() / None / 数组）
                    return page["result"]
                lo, hi = from_time * 1000, to_time * 1000
                rows = [r for r in page["rows"] if lo <= r[0] <= hi]
                return ticks_array(rows) if rows else ()
        return ()
