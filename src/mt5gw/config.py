"""网关配置与实盘保险丝。

时间戳/模式常量说明：0/1/2 对应 mt5.ACCOUNT_TRADE_MODE_DEMO/CONTEST/REAL，
这里用字面量以免在纯逻辑模块里 import MetaTrader5。
"""
import json
import os
from dataclasses import dataclass

ACCOUNT_TRADE_MODE_DEMO = 0
ACCOUNT_TRADE_MODE_CONTEST = 1
ACCOUNT_TRADE_MODE_REAL = 2


class LiveTradingBlockedError(Exception):
    """账户为实盘且未显式解锁时，一切交易调用被拒。"""


@dataclass(frozen=True)
class GatewayConfig:
    terminal_path: str = r"C:\Program Files\MetaTrader 5\terminal64.exe"
    allow_live_trading: bool = False
    magic: int = 20261022


def load(path: str) -> GatewayConfig:
    """从 JSON 文件读配置；文件缺失或键未知时用默认值。"""
    cfg = GatewayConfig()
    if not os.path.exists(path):
        return cfg
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    known = {k: v for k, v in data.items() if k in cfg.__dataclass_fields__}
    return GatewayConfig(**known)


def ensure_trade_allowed(account_trade_mode: int, allow_live: bool) -> None:
    """实盘保险丝：实盘账户且未解锁 → 拒绝；模拟/竞赛账户放行。"""
    if account_trade_mode == ACCOUNT_TRADE_MODE_REAL and not allow_live:
        raise LiveTradingBlockedError(
            "实盘账户被网关保险丝拦截：如确要实盘交易，请在配置中将 "
            "allow_live_trading 设为 true（当前默认关闭）。"
        )
