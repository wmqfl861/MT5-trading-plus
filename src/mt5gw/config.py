"""网关配置与实盘保险丝。

时间戳/模式常量说明：0/1/2 对应 mt5.ACCOUNT_TRADE_MODE_DEMO/CONTEST/REAL，
这里用字面量以免在纯逻辑模块里 import MetaTrader5。
"""
import json
import os
from dataclasses import dataclass

from mt5gw.errors import LiveTradingBlockedError  # noqa: F401 — 本模块是既有导入路径的重导出点

__all__ = [
    "GatewayConfig",
    "load",
    "ensure_trade_allowed",
    "LiveTradingBlockedError",
    "ACCOUNT_TRADE_MODE_DEMO",
    "ACCOUNT_TRADE_MODE_CONTEST",
    "ACCOUNT_TRADE_MODE_REAL",
]

ACCOUNT_TRADE_MODE_DEMO = 0
ACCOUNT_TRADE_MODE_CONTEST = 1
ACCOUNT_TRADE_MODE_REAL = 2


@dataclass(frozen=True)
class GatewayConfig:
    terminal_path: str = r"C:\Program Files\MetaTrader 5\terminal64.exe"
    allow_live_trading: bool = False
    magic: int = 20261022


_EXPECTED_TYPES = {"terminal_path": str, "allow_live_trading": bool, "magic": int}


def load(path: str) -> GatewayConfig:
    """从 JSON 文件读配置；文件缺失或键未知时用默认值，类型不对当场拒绝。"""
    cfg = GatewayConfig()
    if not os.path.exists(path):
        return cfg
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    for key, expected in _EXPECTED_TYPES.items():
        if key in data:
            value = data[key]
            # bool 是 int 的子类，但 magic 不该接受 True/False
            if not isinstance(value, expected) or (
                expected is int and isinstance(value, bool)
            ):
                raise ValueError(
                    f"配置项 {key} 类型应为 {expected.__name__}，"
                    f"实际为 {type(value).__name__}：{value!r}。"
                    "（例如 allow_live_trading 必须写 true/false，不能是字符串——"
                    "字符串 \"false\" 为真值，会静默解锁实盘。）"
                )
    known = {k: v for k, v in data.items() if k in cfg.__dataclass_fields__}
    return GatewayConfig(**known)


def ensure_trade_allowed(account_trade_mode: int, allow_live: bool) -> None:
    """实盘保险丝：实盘账户且未解锁 → 拒绝；模拟/竞赛账户放行。"""
    if account_trade_mode == ACCOUNT_TRADE_MODE_REAL and not allow_live:
        raise LiveTradingBlockedError(
            "实盘账户被网关保险丝拦截：如确要实盘交易，请在配置中将 "
            "allow_live_trading 设为 true（当前默认关闭）。"
        )
