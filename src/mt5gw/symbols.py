"""品种目录：全部品种及规格。

注：按星期的报价/交易时段时间表是 MQL5 专有接口，Python 包取不到，
本层不提供也不伪造（见设计文档反目标）。
"""
from __future__ import annotations

from mt5gw.connection import MT5Connection
from mt5gw.errors import DataUnavailableError
from mt5gw.models import SymbolSpec


def ensure_symbol_visible(conn, mt5_module, name: str) -> None:
    """不在 Market Watch 里的品种先加入，否则 copy_* 取不到数据；失败如实上抛。"""
    row = conn.call(mt5_module.symbol_info, name)
    if row is not None and not getattr(row, "visible", False):
        ok = conn.call(mt5_module.symbol_select, name, True)
        if not ok:
            raise DataUnavailableError(
                f"{name} 无法加入 Market Watch：{conn.last_error()}"
            )


def _to_spec(row) -> SymbolSpec:
    return SymbolSpec(
        name=row.name,
        description=row.description,
        digits=row.digits,
        point=row.point,
        spread=row.spread,
        trade_mode=row.trade_mode,
        currency_profit=row.currency_profit,
        currency_margin=row.currency_margin,
        contract_size=row.trade_contract_size,  # 包内字段名为 trade_contract_size
        volume_min=row.volume_min,
        volume_max=row.volume_max,
        volume_step=row.volume_step,
        filling_mode=row.filling_mode,
        trade_stops_level=getattr(row, "trade_stops_level", 0),
        raw=row,
        # P0 新增合约参数：getattr 兜底，旧式 row（假件/低版本包）缺字段不崩
        swap_long=getattr(row, "swap_long", 0.0),
        swap_short=getattr(row, "swap_short", 0.0),
        trade_freeze_level=getattr(row, "trade_freeze_level", 0),
        tick_size=getattr(row, "trade_tick_size", 0.0),
        tick_value=getattr(row, "trade_tick_value", 0.0),
        trade_calc_mode=getattr(row, "trade_calc_mode", 0),
        margin_initial=getattr(row, "margin_initial", 0.0),
        margin_maintenance=getattr(row, "margin_maintenance", 0.0),
    )


class SymbolCatalog:
    def __init__(self, conn: MT5Connection, mt5_module):
        self._conn = conn
        self._mt5 = mt5_module

    def all(self) -> list[SymbolSpec]:
        rows = self._conn.call(self._mt5.symbols_get)
        if rows is None:
            raise DataUnavailableError(f"品种列表不可用：{self._conn.last_error()}")
        return [_to_spec(r) for r in rows]

    def get(self, name: str) -> SymbolSpec:
        ensure_symbol_visible(self._conn, self._mt5, name)
        row = self._conn.call(self._mt5.symbol_info, name)
        if row is None:
            raise DataUnavailableError(
                f"品种 {name} 不存在或不可见：{self._conn.last_error()}"
            )
        return _to_spec(row)

