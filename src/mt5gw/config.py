"""网关配置与实盘保险丝。

模式常量说明：0/1/2 对应 mt5.ACCOUNT_TRADE_MODE_DEMO/CONTEST/REAL，
这里用字面量以免在纯逻辑模块里 import MetaTrader5。
保险丝语义（白名单）：只有模拟盘放行；竞赛/实盘/未知值一律拒绝，
除非 allow_live_trading is True（严格布尔）。
"""
import json
import logging
import math
import os
from dataclasses import dataclass

from mt5gw.errors import LiveTradingBlockedError  # noqa: F401 — 本模块是既有导入路径的重导出点

logger = logging.getLogger(__name__)

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

_EXPECTED_TYPES = {"terminal_path": str, "allow_live_trading": bool, "magic": int,
                   "expected_login": int, "server_utc_offset_hours": float,
                   "initialize_timeout_ms": int}

# 类型不符时的补充说明（按字段类型给最贴切的提示）
_TYPE_HINTS = {
    bool: "（例如 allow_live_trading 必须写 true/false，不能是字符串——"
          "字符串 \"false\" 为真值，会静默解锁实盘。）",
    float: "（例如 server_utc_offset_hours 必须写带小数点的数字，如 0.0 / 2.0 / 3.0；"
           "整数与字符串一律拒绝——R5：时差声明错了，时段与按小时成本会全部错位。）",
}


@dataclass(frozen=True)
class GatewayConfig:
    terminal_path: str = r"C:\Program Files\MetaTrader 5\terminal64.exe"
    allow_live_trading: bool = False
    magic: int = 20261022
    expected_login: int | None = None  # 钉死账号；解锁实盘时必填
    # 服务器时间 − UTC 的声明时差（小时，R5）：库内时间实为服务器时间，
    # Exness 为 GMT+0 故默认 0.0；换 GMT+2/+3 券商必须显式声明并经
    # mt5data.clock.check_server_offset 用实测时钟校验
    server_utc_offset_hours: float = 0.0
    query_timeout_s: float = 30.0
    order_timeout_s: float = 60.0
    # T201（第十九轮 O19-1）：mt5.initialize 的 MT5 侧超时（毫秒）。必须
    # 显式传给 mt5.initialize——官方缺省 60 秒，会在"终端在线、只是与交易
    # 服务器断开"时挂过 T110 的 query_timeout_s 期限（11-10 三进程 rc=75
    # 的根因）。上界钉在查询期限减 10 秒：1000 ≤ 值 ≤ query_timeout_s×1000
    # −10000（缺省 30 秒期限 → 上界 20000）。
    initialize_timeout_ms: int = 15000

    def __post_init__(self):
        for key in ("query_timeout_s", "order_timeout_s"):
            value = getattr(self, key)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError(f"{key} must be finite and positive: {value!r}")
        # 代码构造（GatewayConfig(...)、dataclasses.replace）与 JSON 装载同一条
        # 类型底线：字符串 "false" 是真值，混进来等于静默武装实盘。
        for key, expected in _EXPECTED_TYPES.items():
            value = getattr(self, key)
            if key == "expected_login" and value is None:
                continue  # None 合法：表示不钉死（解锁实盘时才强制必填）
            ok = isinstance(value, expected) and not (
                expected is int and isinstance(value, bool)
            )
            if not ok:
                raise ValueError(
                    f"配置项 {key} 类型应为 {expected.__name__}，"
                    f"实际为 {type(value).__name__}：{value!r}"
                )
        # T201：initialize 的 MT5 侧超时（毫秒）。下界 1000；上界随查询
        # 期限联动（query_timeout_s×1000−10000），确保它永远低于 T110 给
        # initialize 的期限。期限小到上界不足 1000 的配置（T110 期限测试
        # 用亚秒级 query_timeout_s）不存在可成立的生产值，跳过上界——
        # 该值只透传给假模块，从不实际等待。
        if self.initialize_timeout_ms < 1000:
            raise ValueError(
                f"initialize_timeout_ms must be >= 1000 (ms): "
                f"{self.initialize_timeout_ms!r}")
        init_upper_ms = int(self.query_timeout_s * 1000) - 10_000
        if init_upper_ms >= 1000 and self.initialize_timeout_ms > init_upper_ms:
            raise ValueError(
                f"initialize_timeout_ms={self.initialize_timeout_ms!r} 超出上界 "
                f"{init_upper_ms}（query_timeout_s×1000−10000，须给 T110 查询"
                f"期限留 10 秒余量）"
            )
        require_login_pinned(self.allow_live_trading, self.expected_login)


def _validate_types(data: dict) -> None:
    for key, expected in _EXPECTED_TYPES.items():
        if key not in data:
            continue
        value = data[key]
        ok = isinstance(value, expected) and not (
            expected is int and isinstance(value, bool)
        )
        if not ok:
            raise ValueError(
                f"配置项 {key} 类型应为 {expected.__name__}，"
                f"实际为 {type(value).__name__}：{value!r}。"
                f"{_TYPE_HINTS.get(expected, '')}"
            )


def load(path: str) -> GatewayConfig:
    """从 JSON 文件读配置；文件必须存在（路径写错当场暴露），未知键告警。"""
    if not os.path.exists(path):
        raise FileNotFoundError(f"配置文件不存在：{path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    _validate_types(data)
    default = GatewayConfig()
    unknown = set(data) - set(default.__dataclass_fields__)
    if unknown:
        logger.warning("配置中存在未知键（可能是拼写错误，已被忽略）：%s", sorted(unknown))
    known = {k: v for k, v in data.items() if k in default.__dataclass_fields__}
    return GatewayConfig(**known)


def ensure_trade_allowed(account_trade_mode: int, allow_live: bool) -> None:
    """实盘保险丝（白名单）：仅模拟盘放行；其余一律拒绝，除非显式解锁。"""
    if account_trade_mode == ACCOUNT_TRADE_MODE_DEMO:
        return
    if allow_live is True:
        return
    raise LiveTradingBlockedError(
        f"账户 trade_mode={account_trade_mode} 非模拟盘，被网关保险丝拦截："
        "如确要实盘交易，必须在配置中将 allow_live_trading 显式设为 true。"
    )


def require_login_pinned(allow_live: bool, expected_login: int | None) -> None:
    """解锁实盘必须同时钉死账号（expected_login），防止解锁后被切到别的实盘账户。"""
    if allow_live and expected_login is None:
        raise LiveTradingBlockedError(
            "已解锁实盘但未配置 expected_login：必须钉死目标账号后才允许实盘下单。"
        )
