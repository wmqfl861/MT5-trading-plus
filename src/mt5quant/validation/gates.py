"""关卡门槛配置（config/gates.toml）与 G1/G4/G5 关卡（P4/T12）。

冻结机制（防门槛被悄悄放松）：
- FROZEN_GATES_SHA256 是 config/gates.toml 的冻结指纹；load_gates 每次加载
  先算文件 sha256 并与冻结值比对，不符 → GatesConfigError，拒绝运行
  （bt gate / bt screen 的 gates_hash 都来自这份核对过的文件）；
- 确需调整门槛（P5 校准，T13）：改 config/gates.toml，同步更新本常量，
  一次提交里两处一起变，评审可见。

门槛数值逐条对应 docs/blueprint.md 附录 B（见 config/gates.toml 注释）。
"""
from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from mt5data.store import TIMEFRAME_SECONDS
from mt5quant.validation import stats as vstats

DEFAULT_GATES_TOML = Path(__file__).resolve().parents[3] / "config" / "gates.toml"
# 冻结指纹（2026-10-24，T12 初值；P5 校准后重新冻结）
FROZEN_GATES_SHA256 = "29314cd38aafaba69ed7730139b5618c0619309dc5695408c61bdc55ba6766b9"

GATE_ORDER = ("G0", "G1", "G2", "G3", "G4", "G5")


class GatesConfigError(RuntimeError):
    """门槛文件缺失 / sha256 与冻结值不符 / 模式不合法——拒绝运行。"""


class GateApprovalError(RuntimeError):
    """G5 锁箱一次性测试缺少显式批准（--approve）——未执行，机会未消耗。"""


class LockboxAlreadyUsedError(RuntimeError):
    """该卡的锁箱一次性测试已经用过——第二次请求直接拒绝。"""


@dataclass(frozen=True)
class GatesConfig:
    """门槛集合（字段与 config/gates.toml 一一对应）。"""

    version: int
    # [g1]
    trade_count_min_intraday: int
    trade_count_min_hourly: int
    gross_cost_multiple_min: float
    profit_factor_min: float
    # [g2]
    cost_scale_pass: float
    cost_scale_record: float
    plateau_min_fraction: float
    yearly_min_fraction: float
    yearly_max_concentration: float
    random_entry_percentile: float
    random_entry_sims: int
    drop_best_fraction: float
    cross_symbol_min: int
    # [g3]
    walkforward_train_bars: int
    walkforward_test_bars: int
    walkforward_min_oos_ratio: float
    # [g4]
    dsr_min_probability: float
    mc_drawdown_sims: int
    mc_drawdown_percentile_max: float
    # [g5]
    lockbox_interval_lo_percentile: float
    lockbox_interval_hi_percentile: float


_SCHEMA: dict[str, dict[str, type]] = {
    "": {"version": int},
    "g1": {"trade_count_min_intraday": int, "trade_count_min_hourly": int,
           "gross_cost_multiple_min": float, "profit_factor_min": float},
    "g2": {"cost_scale_pass": float, "cost_scale_record": float,
           "plateau_min_fraction": float, "yearly_min_fraction": float,
           "yearly_max_concentration": float,
           "random_entry_percentile": float, "random_entry_sims": int,
           "drop_best_fraction": float, "cross_symbol_min": int},
    "g3": {"walkforward_train_bars": int, "walkforward_test_bars": int,
           "walkforward_min_oos_ratio": float},
    "g4": {"dsr_min_probability": float, "mc_drawdown_sims": int,
           "mc_drawdown_percentile_max": float},
    "g5": {"lockbox_interval_lo_percentile": float,
           "lockbox_interval_hi_percentile": float},
}


def gates_sha256(path) -> str:
    """门槛文件的 sha256（写进每次运行的 gates_hash 字段）。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _parse(data: dict, source: str) -> GatesConfig:
    allowed_top = set(_SCHEMA[""]) | {k for k in _SCHEMA if k}
    unknown_top = sorted(set(data) - allowed_top)
    if unknown_top:
        raise GatesConfigError(
            f"{source}：未知顶层键 {unknown_top}（门槛文件只允许顶层 "
            f"version 与 [g1]–[g5] 节）")
    by_field: dict = {}
    for section, fields in _SCHEMA.items():
        if section == "":
            # 顶层只管 version；节键的合法性由上面的 allowed_top 把关
            raw = {"version": data.get("version")}
        else:
            raw = data.get(section)
        if not isinstance(raw, dict):
            raise GatesConfigError(f"{source}：[{section or '顶层'}] 必须是表")
        unknown = sorted(set(raw) - set(fields))
        if unknown:
            raise GatesConfigError(
                f"{source}：[{section or '顶层'}] 有未知键 {unknown}，"
                f"合法键 {list(fields)}")
        for key, typ in fields.items():
            if key not in raw:
                raise GatesConfigError(f"{source}：缺门槛键 [{section}].{key}")
            v = raw[key]
            if isinstance(v, bool) or not isinstance(v, typ):
                raise GatesConfigError(
                    f"{source}：[{section}].{key} 必须是 {typ.__name__}，"
                    f"收到 {v!r}")
            # 各节键名全局唯一，直接作字段名（顶层 version 除外）
            by_field[key] = v
    return GatesConfig(**by_field)


def load_gates(path=None, *, frozen_sha: str | None = None
               ) -> tuple[GatesConfig, str]:
    """加载并核对门槛文件；返回 (配置, 文件 sha256)。

    frozen_sha 为注入点（测试用）；缺省与 FROZEN_GATES_SHA256 比对。
    """
    p = Path(path) if path is not None else DEFAULT_GATES_TOML
    if not p.is_file():
        raise GatesConfigError(f"门槛文件不存在：{p}（config/gates.toml 必须入库）")
    sha = gates_sha256(p)
    expected = FROZEN_GATES_SHA256 if frozen_sha is None else frozen_sha
    if sha != expected:
        raise GatesConfigError(
            f"{p} 的 sha256({sha[:12]}…) 与冻结值({expected[:12]}…)不符——"
            "拒绝运行。若是有意调整门槛：修改 config/gates.toml 后同步更新 "
            "mt5quant.validation.gates.FROZEN_GATES_SHA256，两处一次提交")
    try:
        with open(p, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise GatesConfigError(f"{p}：TOML 解析失败：{e}") from e
    return _parse(data, str(p)), sha


def trade_count_min(timeframe: str, cfg: GatesConfig) -> int:
    """交易次数门槛（附录 B）：周期 < 1 小时按日内档 300，≥ 1 小时按 150。

    附录 B 只列了 M5–M15 与 H1；M1/M30 未列，从严归入日内档。
    """
    tf = timeframe.upper()
    if tf not in TIMEFRAME_SECONDS:
        raise ValueError(f"未知 timeframe：{timeframe!r}")
    if TIMEFRAME_SECONDS[tf] < 3600:
        return int(cfg.trade_count_min_intraday)
    return int(cfg.trade_count_min_hourly)


# ---------------------------------------------------------------- G1

def run_g1(trades: pd.DataFrame, *, timeframe: str, cfg: GatesConfig) -> dict:
    """样本内资格线：净盈利、交易次数、每笔毛利/成本倍数、盈亏比。"""
    n = len(trades)
    checks: dict[str, dict] = {}
    total_net = float(trades["net"].sum()) if n else 0.0
    checks["net_profit_positive"] = {"passed": bool(n and total_net > 0),
                                     "net_profit": total_net}
    need = trade_count_min(timeframe, cfg)
    checks["trade_count"] = {"passed": bool(n >= need), "trades": int(n),
                             "min": need}
    if n:
        gross_mid_total = float((trades["gross"] + trades["slippage_cost"]
                                 + trades["spread_cost"]).sum())
        cost_total = float(trades["commission"].sum()
                           + trades["slippage_cost"].sum()
                           + trades["spread_cost"].sum()
                           - trades["swap"].sum())
        if cost_total > 0:
            multiple = (gross_mid_total / n) / (cost_total / n)
            checks["gross_cost_multiple"] = {
                "passed": bool(multiple >= cfg.gross_cost_multiple_min),
                "multiple": float(multiple),
                "min": float(cfg.gross_cost_multiple_min)}
        else:
            checks["gross_cost_multiple"] = {
                "passed": True, "multiple": None,
                "min": float(cfg.gross_cost_multiple_min),
                "note": "零成本运行：毛利/成本倍数无意义，按通过记录"}
        wins = float(trades.loc[trades["net"] > 0, "net"].sum())
        losses = float(trades.loc[trades["net"] < 0, "net"].sum())
        if losses < 0:
            pf = wins / abs(losses)
            checks["profit_factor"] = {
                "passed": bool(pf >= cfg.profit_factor_min),
                "profit_factor": float(pf),
                "min": float(cfg.profit_factor_min)}
        else:
            checks["profit_factor"] = {
                "passed": bool(n > 0), "profit_factor": None,
                "min": float(cfg.profit_factor_min),
                "note": "无亏损交易：PF 视为无穷大，按通过记录" if n else "无交易"}
    return {"passed": bool(all(c["passed"] for c in checks.values())),
            "checks": checks, "trade_count": int(n)}


# ---------------------------------------------------------------- G4

def run_g4(returns, *, n_trials: int, trial_sharpes, actual_max_drawdown,
           cfg: GatesConfig, seed: int) -> dict:
    """统计显著性：DSR ≥ dsr_min_probability 且回撤不差于 99% 的重采样路径。"""
    checks: dict[str, dict] = {}
    try:
        probability = vstats.deflated_sharpe_probability(
            returns, n_trials=n_trials, trial_sharpes=trial_sharpes)
        reason = None
    except ValueError as e:
        probability, reason = 0.0, str(e)
    checks["deflated_sharpe"] = {
        "passed": bool(probability >= cfg.dsr_min_probability),
        "probability": float(probability),
        "min": float(cfg.dsr_min_probability),
        "n_trials": int(n_trials),
        **({"reason": reason} if reason else {})}
    try:
        dist = vstats.mc_drawdown_distribution(
            returns, sims=cfg.mc_drawdown_sims, seed=seed)
        percentile = dist.percentile_of(actual_max_drawdown)
        checks["mc_drawdown"] = {
            "passed": bool(percentile <= cfg.mc_drawdown_percentile_max),
            "actual_max_drawdown": float(actual_max_drawdown),
            "percentile": float(percentile),
            "percentile_max": float(cfg.mc_drawdown_percentile_max),
            **dist.summary()}
    except ValueError as e:
        checks["mc_drawdown"] = {"passed": False, "reason": str(e)}
    return {"passed": bool(all(c["passed"] for c in checks.values())),
            "checks": checks, "seed": int(seed)}


# ---------------------------------------------------------------- G5

def run_g5(ledger, card_id: str, *, approved_by: str | None, metrics: dict,
           mc_sharpes, cfg: GatesConfig) -> dict:
    """锁箱一次性：期望为正 + 指标落在样本外蒙特卡洛区间内。

    - 未批准 → GateApprovalError（不消耗一次性机会）；
    - 已用过 → LockboxAlreadyUsedError（第二次直接拒绝）；
    - 评估完成（无论过没过）→ 写 lockbox_uses：看过一眼即消耗。
    """
    if not approved_by:
        raise GateApprovalError(
            f"卡 {card_id} 的锁箱（G5）是一次性测试，必须显式 --approve "
            "（并指定批准人）——本次未执行，一次性机会未消耗")
    if ledger.lockbox_used(card_id):
        raise LockboxAlreadyUsedError(
            f"卡 {card_id} 的锁箱已用过一次（lockbox_uses 有记录）——"
            "第二次请求直接拒绝；一次性机会已消耗，结论以第一次为准")
    checks: dict[str, dict] = {}
    expectancy = metrics.get("expectancy")
    checks["expectation_positive"] = {
        "passed": bool(expectancy is not None and expectancy > 0),
        "expectancy": expectancy}
    sharpe = metrics.get("sharpe")
    if mc_sharpes and sharpe is not None:
        lo = float(np.percentile(mc_sharpes, cfg.lockbox_interval_lo_percentile))
        hi = float(np.percentile(mc_sharpes, cfg.lockbox_interval_hi_percentile))
        checks["mc_interval"] = {
            "passed": bool(lo <= sharpe <= hi),
            "sharpe": float(sharpe), "lo": lo, "hi": hi}
    else:
        checks["mc_interval"] = {
            "passed": False, "sharpe": sharpe, "lo": None, "hi": None,
            "reason": "无蒙特卡洛样本或夏普无定义，区间检查失败（fail-closed）"}
    result = {"passed": bool(all(c["passed"] for c in checks.values())),
              "checks": checks, "metrics": dict(metrics)}
    ledger.record_lockbox_use(card_id, approved_by=approved_by, result=result)
    return result


def load_lockbox_segment(data_root, symbol: str, timeframe: str) -> pd.DataFrame:
    """读锁箱段（time ≥ 全局 cutoff）——只有 G5 一次性测试允许碰。"""
    from mt5data.lockbox import LockboxError, cutoff_for, load_config
    from mt5data.store import load_bars

    cutoff = cutoff_for(load_config(data_root), symbol, timeframe)
    if cutoff is None:
        raise LockboxError(
            f"{data_root} 无锁箱配置（lockbox.json）——G5 无从谈起，"
            "先跑 scripts/lockbox_init.py")
    df = load_bars(data_root, symbol, timeframe)
    seg = None if df is None else df[df["time"] >= pd.Timestamp(cutoff)]
    if seg is None or len(seg) == 0:
        raise LockboxError(
            f"{symbol} {timeframe} 的锁箱段（time ≥ {cutoff.isoformat()}）为空")
    return seg.reset_index(drop=True)


# ---------------------------------------------------------------- 报告

def render_gate_report(card_id: str, params: dict, results: dict) -> str:
    """关卡报告（Markdown）：关卡表 + 未过子检查明细。"""
    lines = [f"# 关卡报告：{card_id}", "",
             f"- 参数：`{params}`", f"- 关卡：{', '.join(results)}", "",
             "| 关卡 | 结果 |", "| --- | --- |"]
    for gate in GATE_ORDER:
        if gate not in results:
            continue
        lines.append(f"| {gate} | {'✓ 通过' if results[gate]['passed'] else '✗ 未过'} |")
    details = []
    for gate in GATE_ORDER:
        r = results.get(gate)
        if r is None or r.get("passed"):
            continue
        failed = [name for name, c in r.get("checks", {}).items()
                  if not c.get("passed", False)]
        details.append(f"- {gate} 未过子检查：{failed or ['整体']}")
    if details:
        lines += ["", "## 未过明细", ""] + details
    lines.append("")
    return "\n".join(lines)
