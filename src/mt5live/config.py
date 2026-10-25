"""实盘运行配置（TOML）。

LiveConfig 只描述"跑什么"：品种、周期、策略（"module:Class"）、参数、magic、
手数来源与触发容差；"能不能跑"（白名单、手数上限、暴露、日亏、禁时段、
一键停机）属于独立风控进程（mt5live.riskproc / mt5live.gate，T14b/T24），
不在本文件。

- 手数来源二选一（T24/M2）：`volume_lots`（固定手数）或 `risk_fraction`
  （单笔风险占权益比例，执行器经 mt5quant.sizing.lots_for_risk 按风险定
  手数——与回测同一函数；风控只设 max_lots_per_order 上限）。两者都缺
  → 拒绝启动（静默的 1 手黄金就是 M2 要消灭的爆仓来源）。
- risk_check_enabled=False：绕过风控闸门的前提是 volume_lots 已钉死
  （构造校验强制），否则拒绝启动。
- 触发网格假设 bar 开盘时刻与 Unix 纪元对齐（Exness 服务器 GMT+0：M*/H*/D1
  均对齐；W1/MN1 的周/月锚点不对齐，直接拒绝，不做静默错位触发）。
"""
from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, fields
from datetime import datetime, timezone

from mt5data.store import TIMEFRAME_SECONDS

_DAY_SECONDS = 86400
_SCALARS = (int, float, bool, str)
MAX_RISK_FRACTION = 0.05       # 单笔风险占权益比例的保守上限（5%）


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


@dataclass(frozen=True)
class LiveConfig:
    """实盘信号运行器配置；字段校验 fail-fast（代码构造与 TOML 装载同一条底线）。"""

    symbol: str
    timeframe: str
    strategy: str                       # "module:Class"，与回测 CLI 同格式
    magic: int                          # 本策略在 MT5 的仓位/订单标记（差分过滤依据）
    lookback_bars: int                  # 每次决策喂给策略的窗口长度（blueprint §4 第 5 条）
    params: dict = None                 # 策略参数（标量；构造后视为不可变约定）
    volume_lots: float | None = None    # 固定手数；None → 按 risk_fraction 定手数
    risk_fraction: float | None = None  # 单笔风险占权益比例（T24/M2；与 volume_lots 二选一）
    deviation: int = 20                 # 市价单最大滑点（点）
    risk_check_enabled: bool = True     # 风控进程开关（心跳/决策供其裁决，T14b 消费）
    data_root: str = "data"             # K 线仓库根（定时刷新进程写入，这里只读）
    trigger_delay_s: float = 5.0        # bar 收盘后等待秒数（容差：等刷新进程落盘）
    heartbeat_path: str | None = None   # 心跳文件；None → {data_root}/live/heartbeat-<magic>.json
    created_utc: str | None = None      # 装载时间戳（仅审计用）

    def __post_init__(self):
        if not (isinstance(self.symbol, str) and self.symbol.strip()):
            raise ValueError(f"symbol 必须是非空字符串，收到 {self.symbol!r}")
        tf = self.timeframe.upper() if isinstance(self.timeframe, str) else ""
        if tf not in TIMEFRAME_SECONDS:
            raise ValueError(
                f"timeframe 必须是 TIMEFRAME_SECONDS 之一，收到 {self.timeframe!r}")
        if _DAY_SECONDS % TIMEFRAME_SECONDS[tf] != 0:
            raise ValueError(
                f"timeframe {self.timeframe!r} 的 bar 开盘时刻不与日内网格对齐"
                "（W1/MN1 的锚点在周/月边界），触发网格无法按 Unix 纪元取整——拒绝")
        if not (isinstance(self.strategy, str) and ":" in self.strategy
                and all(self.strategy.split(":", 1))):
            raise ValueError(
                f'strategy 必须是 "module:Class" 格式，收到 {self.strategy!r}')
        if not (_is_int(self.magic) and self.magic > 0):
            raise ValueError(f"magic 必须是正整数，收到 {self.magic!r}")
        if not (_is_int(self.lookback_bars) and self.lookback_bars >= 1):
            raise ValueError(
                f"lookback_bars 必须是 ≥1 的整数，收到 {self.lookback_bars!r}")
        if self.params is None:
            object.__setattr__(self, "params", {})
        if not isinstance(self.params, dict):
            raise ValueError(f"params 必须是表（dict），收到 {type(self.params).__name__}")
        for key, value in self.params.items():
            if not isinstance(key, str):
                raise ValueError(f"params 键必须是字符串：{key!r}")
            if not isinstance(value, _SCALARS):
                raise ValueError(
                    f"params[{key!r}] 必须是标量（int/float/bool/str），"
                    f"收到 {type(value).__name__}")
        if self.volume_lots is not None and not (
                _is_num(self.volume_lots) and math.isfinite(self.volume_lots)
                and self.volume_lots > 0):
            raise ValueError(
                f"volume_lots 必须是有限正数或留空（按风险定手数），收到 "
                f"{self.volume_lots!r}")
        if self.risk_fraction is not None and not (
                _is_num(self.risk_fraction) and math.isfinite(self.risk_fraction)
                and 0 < self.risk_fraction <= MAX_RISK_FRACTION):
            raise ValueError(
                f"risk_fraction 必须在 (0, {MAX_RISK_FRACTION}] 内"
                f"（单笔风险占权益比例），收到 {self.risk_fraction!r}")
        if self.volume_lots is None and self.risk_fraction is None:
            raise ValueError(
                "volume_lots 与 risk_fraction 必须二选一：固定手数，或按"
                "风险定手数（mt5quant.sizing.lots_for_risk，与回测同一函数）"
                "——都不给等于静默 1 手，首次止损即巨亏（M2）")
        if self.volume_lots is not None and self.risk_fraction is not None:
            raise ValueError(
                f"volume_lots（{self.volume_lots}）与 risk_fraction"
                f"（{self.risk_fraction}）只能给一个")
        if not (_is_int(self.deviation) and self.deviation >= 0):
            raise ValueError(f"deviation 必须是 ≥0 的整数（点），收到 {self.deviation!r}")
        if not isinstance(self.risk_check_enabled, bool):
            raise ValueError(
                "risk_check_enabled 必须是 true/false（布尔），收到 "
                f"{self.risk_check_enabled!r}")
        if not (isinstance(self.data_root, str) and self.data_root.strip()):
            raise ValueError(f"data_root 必须是非空字符串，收到 {self.data_root!r}")
        if not (_is_num(self.trigger_delay_s) and math.isfinite(self.trigger_delay_s)
                and self.trigger_delay_s >= 0):
            raise ValueError(
                f"trigger_delay_s 必须是 ≥0 的有限秒数，收到 {self.trigger_delay_s!r}")
        if self.heartbeat_path is not None and not isinstance(self.heartbeat_path, str):
            raise ValueError(
                f"heartbeat_path 必须是字符串或留空，收到 {self.heartbeat_path!r}")
        if not self.risk_check_enabled and self.volume_lots is None:
            raise ValueError(
                "risk_check_enabled=false（绕过风控闸门）时必须钉死 volume_lots，"
                "否则等于无上限自由下单")


def load(path: str) -> LiveConfig:
    """从 TOML 读实盘配置。

    fail-fast：文件必须存在；未知键视为拼写错误直接抛错（实盘配置拼错
    绝不静默忽略）；缺必填键、类型不符同样当场抛。
    """
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"配置文件不存在：{path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"配置文件 {path} 不是合法 TOML：{exc}") from exc
    known = {f.name for f in fields(LiveConfig)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"未知配置项（可能是拼写错误）：{sorted(unknown)}")
    required = {"symbol", "timeframe", "strategy", "magic", "lookback_bars"}
    missing = required - set(data)
    if missing:
        raise ValueError(f"缺少必填配置项：{sorted(missing)}")
    data.setdefault("created_utc",
                    datetime.now(timezone.utc).isoformat(timespec="seconds"))
    try:
        return LiveConfig(**data)
    except TypeError as exc:  # TOML 表嵌套给到标量字段等
        raise ValueError(f"配置结构不符：{exc}") from exc
