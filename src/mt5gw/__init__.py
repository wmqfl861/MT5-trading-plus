"""mt5gw: MT5 数据与交易网关（量化系统基石）。用法见 README。"""
from mt5gw.config import GatewayConfig, load
from mt5gw.gateway import MT5Gateway

__all__ = ["MT5Gateway", "GatewayConfig", "load"]
