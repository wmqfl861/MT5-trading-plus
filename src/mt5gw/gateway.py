"""MT5Gateway：门面。策略代码只认识这一个类。

    with MT5Gateway() as gw:
        specs = gw.symbols.all()
        q = gw.market.quote("EURUSD")
        res = gw.trading.market_order("EURUSD", "buy", 0.01)
"""
from __future__ import annotations

from mt5gw.account import AccountService
from mt5gw.config import GatewayConfig, load
from mt5gw.connection import MT5Connection
from mt5gw.market import MarketData
from mt5gw.symbols import SymbolCatalog
from mt5gw.trading import TradingService


class MT5Gateway:
    def __init__(self, config: GatewayConfig | None = None, mt5_module=None):
        self.config = config or GatewayConfig()
        if mt5_module is None:
            import MetaTrader5 as mt5_module  # 延迟导入：纯逻辑测试不触终端
        self._mt5 = mt5_module
        self._conn = MT5Connection(self.config, mt5_module=mt5_module)
        self._built = False
        self.symbols: SymbolCatalog | None = None
        self.market: MarketData | None = None
        self.account: AccountService | None = None
        self.trading: TradingService | None = None

    def connect(self) -> None:
        self._conn.connect()
        if not self._built:
            self.symbols = SymbolCatalog(self._conn, self._mt5)
            self.market = MarketData(self._conn, self._mt5)
            self.account = AccountService(self._conn, self._mt5)
            self.trading = TradingService(
                self._conn, self.config, self.account, self.symbols, self._mt5
            )
            self._built = True

    def shutdown(self) -> None:
        self._conn.shutdown()

    @property
    def connected(self) -> bool:
        return self._conn.connected

    @classmethod
    def from_config_file(cls, path: str, mt5_module=None) -> "MT5Gateway":
        return cls(load(path), mt5_module=mt5_module)

    def __enter__(self) -> "MT5Gateway":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown()
