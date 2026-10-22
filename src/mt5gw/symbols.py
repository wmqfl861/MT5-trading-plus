"""品种目录：全部品种及规格。

注：按星期的报价/交易时段时间表是 MQL5 专有接口，Python 包取不到，
本层不提供也不伪造（见设计文档反目标）。
"""
from __future__ import annotations

from mt5gw.connection import MT5Connection
from mt5gw.errors import DataUnavailableError
from mt5gw.models import SymbolSpec


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
        contract_size=row.contract_size,
        volume_min=row.volume_min,
        volume_max=row.volume_max,
        volume_step=row.volume_step,
        filling_mode=row.filling_mode,
        raw=row,
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
        row = self._conn.call(self._mt5.symbol_info, name)
        if row is None:
            raise DataUnavailableError(
                f"品种 {name} 不存在或不可见：{self._conn.last_error()}"
            )
        return _to_spec(row)

    def ensure_visible(self, name: str) -> None:
        """不在 Market Watch 里的品种先加入，否则 copy_* 取不到数据。"""
        row = self._conn.call(self._mt5.symbol_info, name)
        if row is not None and not getattr(row, "visible", False):
            self._conn.call(self._mt5.symbol_select, name, True)
