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
import re
import tomllib
from pathlib import Path
from dataclasses import dataclass, fields
from datetime import datetime, timezone

from mt5data.store import TIMEFRAME_SECONDS

_DAY_SECONDS = 86400
_SCALARS = (int, float, bool, str)
MAX_RISK_FRACTION = 0.05       # 单笔风险占权益比例的保守上限（5%）
MODEL_CONFIG_KEYS = frozenset({"model_path", "model_sha256", "card_path",
                               "experiment_sha256", "experiment"})


class ModelConfigError(ValueError):
    """T78 新模型配置不完整；runner 在监控/运行状态写入前拒绝。"""


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_json_value(value) -> bool:
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_json_value(v) for k, v in value.items())
    if isinstance(value, list):
        return all(_is_json_value(v) for v in value)
    return (value is None or isinstance(value, (str, bool, int))
            or isinstance(value, float) and math.isfinite(value))


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
    model_path: str | None = None       # T78：冻结 artifact；与 model_sha256 成对
    model_sha256: str | None = None     # artifact 原始文件字节的 SHA256
    card_path: str | None = None        # 显式卡；相对路径按配置文件目录解析
    experiment_sha256: str | None = None  # 复算的装配指纹，绝非研究/发布批准
    experiment: dict | None = None     # fingerprint 所需 data/cost/session 元信息

    def __post_init__(self):
        supplied = {k for k in MODEL_CONFIG_KEYS if getattr(self, k) is not None}
        if supplied:
            missing = MODEL_CONFIG_KEYS - supplied
            if missing:
                raise ModelConfigError(f"模型配置须完整成组提供，缺 {sorted(missing)}")
            for name in ("model_path", "card_path"):
                value = getattr(self, name)
                if not isinstance(value, str) or not value.strip():
                    raise ModelConfigError(f"{name} 必须是非空路径字符串")
            for name in ("model_sha256", "experiment_sha256"):
                value = getattr(self, name)
                if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
                    raise ModelConfigError(f"{name} 必须是 64 位十六进制 SHA256")
                object.__setattr__(self, name, value.lower())
            exp = self.experiment
            required = {"bars_meta", "cost_version", "sessions_sha"}
            if (not isinstance(exp, dict) or required - exp.keys()
                    or exp.keys() - required - {"evaluator_sha"}):
                raise ModelConfigError(
                    "experiment 须含 bars_meta/cost_version/sessions_sha，"
                    "只另允许 evaluator_sha（不读取行情/成本文件）")
            if (not isinstance(exp["bars_meta"], dict) or not exp["bars_meta"]
                    or not _is_json_value(exp["bars_meta"])):
                raise ModelConfigError("experiment.bars_meta 必须是非空 JSON 表且数值有限")
            for name in ("cost_version", "sessions_sha", "evaluator_sha"):
                if name in exp and (not isinstance(exp[name], str) or not exp[name].strip()):
                    raise ModelConfigError(f"experiment.{name} 必须是非空字符串")
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
            raw = f.read()
    except FileNotFoundError:
        raise FileNotFoundError(f"配置文件不存在：{path}") from None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        # Malformed model declarations still fail before runtime state. Keep
        # the legacy monitor path for malformed configurations without them.
        keys = "|".join(sorted(MODEL_CONFIG_KEYS))
        model_declared = re.search(
            rf"(?m)^\s*(?:[\"']?(?:{keys})[\"']?\s*=|\[\s*[\"']?experiment(?:[\"']?\s*\]|\.))",
            raw.decode("utf-8", errors="replace"))
        error = ModelConfigError if model_declared else ValueError
        raise error(f"配置文件 {path} 不是合法 TOML：{exc}") from exc
    error = ModelConfigError if MODEL_CONFIG_KEYS & data.keys() else ValueError
    known = {f.name for f in fields(LiveConfig)}
    unknown = set(data) - known
    if unknown:
        raise error(f"未知配置项（可能是拼写错误）：{sorted(unknown)}")
    required = {"symbol", "timeframe", "strategy", "magic", "lookback_bars"}
    missing = required - set(data)
    if missing:
        raise error(f"缺少必填配置项：{sorted(missing)}")
    data.setdefault("created_utc",
                    datetime.now(timezone.utc).isoformat(timespec="seconds"))
    try:
        cfg = LiveConfig(**data)
        resolved = {}
        for name in ("model_path", "card_path"):
            value = getattr(cfg, name)
            if value is not None:
                resolved[name] = str((Path(path).resolve().parent / value).resolve())
        if resolved:
            from dataclasses import replace
            cfg = replace(cfg, **resolved)
        return cfg
    except (TypeError, ValueError) as exc:
        if MODEL_CONFIG_KEYS & data.keys():
            raise ModelConfigError(f"模型配置不合法：{exc}") from exc
        if isinstance(exc, TypeError):
            raise ValueError(f"配置结构不符：{exc}") from exc
        raise
