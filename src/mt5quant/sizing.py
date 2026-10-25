"""共享定手数（T23/M2，蓝图铁律 v3 第 2 条、决策 D15）：

**定手数只有一个函数**（lots_for_risk：按风险比例与止损距离），回测
（A/B 两层）、关卡（G2–G5 的复算）、实盘（T24 的风控只设上限）共用；
风控进程不负责算手数。

背景（第四轮审核 M2）：固定 1 手黄金在 1 万本金下首次止损即爆仓
（校准报告 run#4–21），指标随手数失真。按风险定手数后，单笔最大亏损
（触发止损）≈ 资金 × risk_fraction，指标跨品种、跨资金可比。

口径：
- risk_fraction：单笔风险占资金比例（默认 0.005 = 0.5%）；
- basis："initial"（默认，按初始资金算——不复利，便于跨运行比较）|
  "equity"（按入场时权益算——权益 = 初始资金 + 已实现净利；入场时点
  不持仓，无浮动盈亏）；
- 止损距离（价格单位）：策略有 SL 时 = stop_loss_atr × 信号 bar ATR；
  策略无 SL 时 = fallback_stop_atr × ATR（估风险距离）。ATR 不可得
  （回看不足）→ 无风险距离 → 手数按 0 处理（跳过该入场，计数见引擎）；
- 手数 = floor(风险额 / (止损距离 × contract_size) / vol_step) × vol_step
  ——**向下取整**（宁可少冒风险）；低于 vol_min → 0.0（跳过并记录）；
  高于 vol_max → 封顶 vol_max（品种上限）；
- 品种 vol_min/vol_step/vol_max 来自快照 data/specs/latest.json 的
  volume_min/volume_step/volume_max（load_volume_limits；缺数据报错，
  绝不静默假设 0.01）。
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass

# 体积下取整的相对容差：抵消 0.1/0.01=9.999999999999998 这类浮点噪声，
# 不改变真正低于步长的取值（容差 1e-9 远小于任何实际步长差）
_FLOOR_EPS = 1e-9
# 与 vol_min 比较的绝对容差（手数量级 0.01–200，1e-9 足够安全）
_MIN_EPS = 1e-9

_SIZING_BASES = ("initial", "equity")


def _finite_positive(value, name: str) -> float:
    """有限正数校验（拒绝 bool/NaN/inf/0/负值），错误信息带参数名。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是有限正数，收到 {value!r}")
    v = float(value)
    if not math.isfinite(v) or v <= 0:
        raise ValueError(f"{name} 必须是有限正数，收到 {value!r}")
    return v


@dataclass(frozen=True)
class RiskSizing:
    """按风险定手数的参数（T23 接口，计划逐字）。

    risk_fraction：单笔风险占资金比例，如 0.005（0.5%）；
    basis："initial"（默认；按初始资金，不复利，便于比较）| "equity"
    （按入场时权益，随盈亏变化）；
    fallback_stop_atr：策略无止损时的风险距离倍数（× 信号 bar ATR）。
    """

    risk_fraction: float
    basis: str = "initial"
    fallback_stop_atr: float = 1.0

    def __post_init__(self):
        _finite_positive(self.risk_fraction, "risk_fraction")
        if self.basis not in _SIZING_BASES:
            raise ValueError(
                f"basis 必须是 {_SIZING_BASES} 之一，收到 {self.basis!r}")
        _finite_positive(self.fallback_stop_atr, "fallback_stop_atr")


def validate_volume_bounds(vol_min: float, vol_step: float,
                           vol_max: float) -> tuple[float, float, float]:
    """vol 三元组校验（lots_for_risk 与引擎 sizing 模式共用）：全部有限
    为正、步长 > 0、上限 ≥ 最小。返回 float 三元组。"""
    lo = _finite_positive(vol_min, "vol_min")
    step = _finite_positive(vol_step, "vol_step")
    hi = _finite_positive(vol_max, "vol_max")
    if hi + _MIN_EPS < lo:
        raise ValueError(
            f"vol_max 必须不低于 vol_min，收到 min={vol_min!r} max={vol_max!r}")
    return lo, step, hi


def lots_for_risk(capital: float, risk_fraction: float, stop_distance: float,
                  contract_size: float, vol_min: float, vol_step: float,
                  vol_max: float) -> float:
    """按风险比例定手数（**全项目唯一**的定手数函数，铁律 v3 第 2 条）。

    手数 = floor(风险额 / 每手风险 / vol_step) × vol_step，其中
    风险额 = capital × risk_fraction（账户货币）、
    每手风险 = stop_distance × contract_size（价格距离 × 每 1.0 价格变动
    的账户货币盈亏）。向下取整到步长（宁可少冒风险）；取整后低于 vol_min
    → 返回 0.0（调用方跳过该入场并记录）；高于 vol_max → 封顶 vol_max。

    严格校验：capital / risk_fraction / stop_distance / contract_size /
    vol 三元组的任何非有限、零、负值（含零步长）一律 ValueError——
    静默的错误手数正是 M2 要消灭的爆仓来源。
    """
    cap = _finite_positive(capital, "capital")
    rf = _finite_positive(risk_fraction, "risk_fraction")
    dist = _finite_positive(stop_distance, "stop_distance")
    cs = _finite_positive(contract_size, "contract_size")
    lo, step, hi = validate_volume_bounds(vol_min, vol_step, vol_max)

    risk_amount = cap * rf
    per_lot_risk = dist * cs
    raw = risk_amount / per_lot_risk
    steps = math.floor(raw / step + _FLOOR_EPS)
    lots = steps * step
    if lots < lo - _MIN_EPS:
        return 0.0                       # 风险额不够最小手数 → 跳过
    return min(lots, hi)


def load_volume_limits(data_root: str, symbol: str) -> tuple[float, float, float]:
    """从 {data_root}/specs/latest.json 读品种的
    (volume_min, volume_step, volume_max)——scripts/snapshot_specs.py 产出。

    缺文件 / 缺品种 / 缺字段（旧快照）→ ValueError（指路 snapshot_specs，
    绝不静默假设 0.01）；字段非法（0/负）同样拒绝（经 validate_volume_bounds）。
    """
    snap_path = os.path.join(data_root, "specs", "latest.json")
    try:
        with open(snap_path, encoding="utf-8") as f:
            snap = json.load(f)
        entry = snap[symbol]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ValueError(
            f"{symbol} 的手数上下限（volume_min/step/max）未知：读 "
            f"{snap_path} 失败（{e}）——先跑 scripts/snapshot_specs.py 生成"
            "最新快照（按风险定手数需要品种的 vol 三元组，绝不静默假设）"
        ) from e
    if not isinstance(entry, dict):
        raise ValueError(
            f"{symbol} 的快照条目结构非法（{type(entry).__name__}）——"
            f"重跑 scripts/snapshot_specs.py")
    missing = [k for k in ("volume_min", "volume_step", "volume_max")
               if not isinstance(entry.get(k), (int, float))
               or isinstance(entry.get(k), bool)]
    if missing:
        raise ValueError(
            f"{symbol} 快照条目缺 {missing}（旧快照无手数字段）——"
            f"重跑 scripts/snapshot_specs.py")
    return validate_volume_bounds(entry["volume_min"], entry["volume_step"],
                                  entry["volume_max"])
