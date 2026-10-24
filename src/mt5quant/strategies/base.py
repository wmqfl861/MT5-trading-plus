"""策略基类：参数模式校验、数据需求与出场规则声明。

约定：
- 策略只实现 generate_signals（纯函数：bars + params → 信号 Series），
  不做 I/O、不持有跨调用状态；回测引擎与实盘信号运行器共用同一份代码。
- 信号语义：s[i] 为 bar i **收盘**时的目标仓位，取值 ∈ {-1, 0, 1}，
  引擎在 bar i+1 开盘调仓（见 backtest.engine_a）。
- 参数约束（定义即校验，违反直接抛）：参数 ≤4 个；网格笛卡尔积 ≤100。
"""
from __future__ import annotations

import abc
import math
from dataclasses import dataclass

import pandas as pd

MAX_PARAMS = 4
MAX_GRID_POINTS = 100

# ExitRules.reentry 的合法取值（R7，引擎 A-1.3 消费）
REENTRY_MODES = ("immediate", "after_signal_change")


@dataclass(frozen=True)
class ParamSpec:
    """单个策略参数：名字、类型（"int" | "float" | "bool"）与网格取值。"""

    name: str
    type: str
    grid: tuple


@dataclass(frozen=True)
class RequiredData:
    """策略声明所需数据：品种、周期、最少回看 bar 数（引擎/运行器据此备料）。"""

    symbols: tuple[str, ...]
    timeframe: str
    lookback_bars: int


@dataclass(frozen=True)
class ExitRules:
    """出场规则（引擎消费）：SL/TP 为入场 ATR 的倍数，time_stop 按 bar 数。

    reentry（R7，引擎 A-1.3）——保护性出场（stop / stop_gap / take_profit /
    take_profit_gap / time_stop / week_close；signal 出场不算）之后的同向再入场：
    - "immediate"（默认）：目标仓位语义，信号仍指向该方向时下一根开盘重入；
    - "after_signal_change"：信号离开该方向（变 0 或反向）之前禁止同向再入场。
    非法值在构造时抛 ValueError——策略类体里的 `exit_rules = ExitRules(...)`
    赋值发生在类定义求值时，即"策略定义时抛"。
    week_close_exit 只允许 ≤ H1 的周期：按 bar 收盘时刻判定周五收盘，D1/H4
    会提前一整根平仓（D1 在周四收盘就强平），更高周期由引擎在运行前拒绝（R8）。
    """

    stop_loss_atr: float | None = None
    take_profit_atr: float | None = None
    time_stop_bars: int | None = None
    week_close_exit: bool = False
    reentry: str = "immediate"

    def __post_init__(self):
        if self.reentry not in REENTRY_MODES:
            raise ValueError(
                f"reentry 必须是 {REENTRY_MODES} 之一，收到 {self.reentry!r}"
            )


_TYPE_CHECKS = {
    # bool 是 int 的子类，必须显式排除，否则 int 网格会放行 True/False
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "float": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "bool": lambda v: isinstance(v, bool),
}


class BaseStrategy(abc.ABC):
    """所有策略的基类：子类定义即校验参数模式。"""

    params_schema: tuple[ParamSpec, ...] = ()
    required_data: RequiredData = RequiredData(symbols=(), timeframe="", lookback_bars=0)
    exit_rules: ExitRules = ExitRules()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        specs = cls.params_schema
        if len(specs) > MAX_PARAMS:
            raise ValueError(
                f"策略 {cls.__name__} 参数 {len(specs)} 个超过上限 {MAX_PARAMS}"
            )
        for spec in specs:
            if not isinstance(spec, ParamSpec):
                raise TypeError(f"params_schema 项必须是 ParamSpec：{spec!r}")
            if spec.type not in _TYPE_CHECKS:
                raise ValueError(f"参数 {spec.name} 类型未知：{spec.type!r}")
            if not spec.grid:
                raise ValueError(f"参数 {spec.name} 网格为空")
            for v in spec.grid:
                if not _TYPE_CHECKS[spec.type](v):
                    raise ValueError(
                        f"参数 {spec.name} 网格值 {v!r} 与类型 {spec.type!r} 不符"
                    )
        points = math.prod(len(spec.grid) for spec in specs)
        if points > MAX_GRID_POINTS:
            raise ValueError(
                f"策略 {cls.__name__} 网格组合 {points} 超过上限 {MAX_GRID_POINTS}"
            )

    def validate_params(self, params: dict) -> dict:
        """校验参数字典：未知名字 / 缺参 / 类型不符 / 越网格即抛 ValueError。"""
        by_name = {spec.name: spec for spec in self.params_schema}
        unknown = set(params) - set(by_name)
        if unknown:
            raise ValueError(
                f"未知参数：{sorted(unknown)}；合法参数：{sorted(by_name)}"
            )
        missing = [name for name in by_name if name not in params]
        if missing:
            raise ValueError(f"缺少参数：{missing}")
        for name, value in params.items():
            spec = by_name[name]
            if not _TYPE_CHECKS[spec.type](value):
                raise ValueError(
                    f"参数 {name}={value!r} 类型不符，要求 {spec.type!r}"
                )
            if value not in spec.grid:
                raise ValueError(f"参数 {name}={value!r} 越网格 {spec.grid}")
        return dict(params)

    @abc.abstractmethod
    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        """返回目标仓位 Series（值 ∈ {-1,0,1}，index=bars.index）。"""
