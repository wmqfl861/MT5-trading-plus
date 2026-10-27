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
# 冻结指纹（2026-10-26，T21 第二次冻结：[g3] 改日历月窗口（H5）、
# [g5] 改单侧判定（C3）；键名随蓝图 v3 附录 B 变更）
FROZEN_GATES_SHA256 = "68f49237d1b56df62691ab8516804349fce245e1dc3cdb8896bf03716f594357"

GATE_ORDER = ("G0", "G1", "G2", "G3", "G4", "G5")


class GatesConfigError(RuntimeError):
    """门槛文件缺失 / sha256 与冻结值不符 / 模式不合法——拒绝运行。"""


class GateApprovalError(RuntimeError):
    """G5 锁箱一次性测试缺少显式批准（--approve）——未执行，机会未消耗。"""


class LockboxAlreadyUsedError(RuntimeError):
    """该卡的锁箱一次性测试已经用过——第二次请求直接拒绝。"""


class LockboxPrerequisiteError(RuntimeError):
    """未过 G3 的卡不允许跑 G5（参照分布 = G3 拼接的样本外收益）。"""


class LockboxBudgetExhaustedError(RuntimeError):
    """该锁箱（cutoff）的项目级使用预算已用尽（T27/M11/D16）——拒绝，
    且不消耗当前卡的一次性机会。"""


def _budget_check(ledger, cid: str, cutoff) -> str:
    """预算检查 + cutoff 键规范化：用尽 → LockboxBudgetExhaustedError。
    返回规范化后的 cutoff ISO 串（写进 lockbox_uses 供计数）。"""
    from mt5quant.research.ledger import _cutoff_iso

    key = _cutoff_iso(cutoff)
    b = ledger.lockbox_budget(key)
    if b["exhausted"]:
        raise LockboxBudgetExhaustedError(
            f"卡 {cid} 的 G5 被拒：锁箱 cutoff={key} 的使用预算已用尽"
            f"（{b['used']}/{b['max_uses']}，T27/M11——多张卡各看一次锁箱"
            "等于在锁箱上做选择）。预算与退役规则见蓝图决策 D16；调整"
            "预算是管理员动作（改台账库），不是代码缺省")
    return key


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
    # [g3]（T21b/H5：日历月窗口）
    train_months_hourly: int
    test_months_hourly: int
    train_months_intraday: int
    test_months_intraday: int
    min_oos_ratio: float
    # [g4]
    dsr_min_probability: float
    mc_drawdown_sims: int
    mc_drawdown_percentile_max: float
    # [g5]（T21a/C3：单侧判定）
    lockbox_sharpe_min_percentile: float
    lockbox_warn_percentile: float
    lockbox_min_trades_ratio: float
    lockbox_bootstrap_sims: int
    # [g4] 可选（T64/D33）：G4 的 N 与试验夏普取数范围 family|project。
    # 现行 config/gates.toml 无此键 → 缺省 family（冻结语义逐位不变）；
    # gates-v3.toml（T65）写 project。只支持新键，不改现行文件。
    dsr_trial_scope: str = "family"


_SCHEMA: dict[str, dict[str, type]] = {
    "": {"version": int},
    "g1": {"trade_count_min_intraday": int, "trade_count_min_hourly": int,
           "gross_cost_multiple_min": float, "profit_factor_min": float},
    "g2": {"cost_scale_pass": float, "cost_scale_record": float,
           "plateau_min_fraction": float, "yearly_min_fraction": float,
           "yearly_max_concentration": float,
           "random_entry_percentile": float, "random_entry_sims": int,
           "drop_best_fraction": float, "cross_symbol_min": int},
    "g3": {"train_months_hourly": int, "test_months_hourly": int,
           "train_months_intraday": int, "test_months_intraday": int,
           "min_oos_ratio": float},
    "g4": {"dsr_min_probability": float, "mc_drawdown_sims": int,
           "mc_drawdown_percentile_max": float,
           "dsr_trial_scope": str},
    "g5": {"lockbox_sharpe_min_percentile": float,
           "lockbox_warn_percentile": float,
           "lockbox_min_trades_ratio": float,
           "lockbox_bootstrap_sims": int},
}

# 可选键及其缺省（T64/D33）：现行 gates.toml 不含 dsr_trial_scope——
# 缺省 family 保持冻结语义（缺省口径结果与现行逐位一致）；显式给出时
# 只接受 family|project。
_OPTIONAL_DEFAULTS = {("g4", "dsr_trial_scope"): "family"}
_TRIAL_SCOPES = ("family", "project")


def gates_sha256(path) -> str:
    """门槛文件的 sha256（写进每次运行的 gates_hash 字段）。

    哈希前统一换行为 LF：git 在 Windows 检出为 CRLF、Linux 为 LF，
    裸字节哈希会让同一提交在不同平台"被篡改"（本机实测踩过）。
    """
    CRLF = bytes([13, 10])
    raw = Path(path).read_bytes().replace(CRLF, bytes([10]))
    return hashlib.sha256(raw).hexdigest()


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
                default = _OPTIONAL_DEFAULTS.get((section, key))
                if default is None:
                    raise GatesConfigError(
                        f"{source}：缺门槛键 [{section}].{key}")
                v = default          # 可选键缺省（T64：现行文件无此键）
            else:
                v = raw[key]
            if isinstance(v, bool) or not isinstance(v, typ):
                raise GatesConfigError(
                    f"{source}：[{section}].{key} 必须是 {typ.__name__}，"
                    f"收到 {v!r}")
            if key == "dsr_trial_scope" and v not in _TRIAL_SCOPES:
                raise GatesConfigError(
                    f"{source}：[g4].dsr_trial_scope 只能是 "
                    f"{list(_TRIAL_SCOPES)}，收到 {v!r}（T64/D33：G4 的 N 与"
                    "试验夏普按谱系或项目（研究口径）取数）")
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
           cfg: GatesConfig, seed: int, block_len: int = 1) -> dict:
    """统计显著性：DSR ≥ dsr_min_probability 且回撤不差于 99% 的重采样路径。

    block_len（T21c/H6）：蒙特卡洛回撤的块自助块长 = 一个交易日的 bar 数
    （保留日级波动聚集）；缺省 1 = iid 自助（回归兼容）。调用方（bt.py，
    T21e 接线）应按周期传 round(86400 / TIMEFRAME_SECONDS[tf])。
    n_trials / trial_sharpes 的取数范围由 [g4].dsr_trial_scope 决定
    （T64/D33，缺省 family——调用方按 cfg 取数，details 记 trial_scope）。
    """
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
        "trial_scope": str(cfg.dsr_trial_scope),
        **({"reason": reason} if reason else {})}
    try:
        dist = vstats.mc_drawdown_distribution(
            returns, sims=cfg.mc_drawdown_sims, seed=seed,
            block_len=int(block_len))
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


# ---------------------------------------------------------------- G5（T21a/C3 重写）

def _card_id_of(card) -> str:
    """run_g5/begin_lockbox 的 card 参数：Card 对象或卡 id 字符串。"""
    if isinstance(card, str):
        return card
    cid = getattr(card, "id", None)
    if not isinstance(cid, str) or not cid:
        raise TypeError(f"card 须为 Card 对象或卡 id 字符串，收到 {card!r}")
    return cid


def begin_lockbox(ledger, card, *, approved_by: str,
                  cutoff=None) -> None:
    """G5 编排第一步：**读锁箱数据之前**调用（T21a/C3）。

    - 未批准 → GateApprovalError（不消耗一次性机会）；
    - 未过 G3 → LockboxPrerequisiteError（不消耗一次性机会）；
    - 已有任何记录（含上次崩溃残留的 started）→ LockboxAlreadyUsedError；
    - cutoff 给出且该锁箱预算用尽 → LockboxBudgetExhaustedError
      （T27/M11：不消耗一次性机会，预算计数见 ledger.lockbox_budget）；
    - 否则写 status='started' 行——此后该卡锁箱视为已用，中途崩溃也
      不允许重跑（防多次窥视锁箱段）。

    编排协议（bt.py，T21e 接线）：
    begin_lockbox → 读锁箱段（load_lockbox_window，带预热）→ 回测 →
    run_g5（评估并把 started 行写成 done）。cutoff 由调用方从
    data/lockbox.json 读出传入（load_lockbox_window 用同一来源）。
    """
    cid = _card_id_of(card)
    if not approved_by:
        raise GateApprovalError(
            f"卡 {cid} 的锁箱（G5）是一次性测试，必须显式 --approve "
            "（并指定批准人）——本次未执行，一次性机会未消耗")
    if not ledger.gate_passed(cid, "G3"):
        raise LockboxPrerequisiteError(
            f"卡 {cid} 未通过 G3——G5 的参照分布取自 G3 拼接的样本外收益，"
            "没有可信参照不允许跑 G5")
    if ledger.lockbox_used(cid):
        raise LockboxAlreadyUsedError(
            f"卡 {cid} 的锁箱已用过（lockbox_uses 有记录，含进行中的 "
            "started）——第二次请求直接拒绝；一次性机会已消耗，结论以"
            "第一次为准")
    key = _budget_check(ledger, cid, cutoff) if cutoff is not None else None
    ledger.record_lockbox_start(cid, approved_by=approved_by, cutoff=key)


def run_g5(ledger, card, *, lockbox_result: dict, reference_returns,
           expected_trades: float, approved_by: str, cfg: GatesConfig,
           bars_per_day: int | None = None,
           cutoff=None) -> dict:
    """锁箱一次性（T21a/C3 重写）：**单侧**判定，参照策略自身的样本外分布。

    参照分布：reference_returns（策略 G3 拼接的样本外收益）按锁箱长度
    （lockbox_result["bar_count"]）做块自助重采样（块长 = 一个交易日的
    bar 数；缺省按卡周期折算 round(86400/tf 秒)）得到的年化夏普分布，
    年化口径与 report.summarize 一致（优先取 lockbox_result 的
    annualization_bars_per_year）。

    判定（全部单侧）：
    - expectation_positive：锁箱期望（每笔净利）> 0；
    - trade_count：锁箱交易数 ≥ cfg.lockbox_min_trades_ratio ×
      expected_trades（按样本内频率 × 锁箱长度折算的预期）；
    - sharpe_vs_reference：锁箱夏普 ≥ 参照分布 p5
      （cfg.lockbox_sharpe_min_percentile）；高于 p95
      （cfg.lockbox_warn_percentile）只记 result["warning"]
      （"异常好，复核数据与前视"），**不判失败**。

    lockbox_result 需含键：expectancy / sharpe / trade_count / bar_count
    （评估窗 = cutoff 之后的 bar 数；运行从 cutoff 前 lookback_bars 根
    起步、只统计 entry_time ≥ cutoff 的交易，由调用方组装，见
    load_lockbox_window）；可选 annualization_bars_per_year。

    前置拒绝（不消耗一次性）：approved_by 为空 → GateApprovalError；
    已有 done 记录 → LockboxAlreadyUsedError；未过 G3 →
    LockboxPrerequisiteError。cutoff 给出且预算用尽 →
    LockboxBudgetExhaustedError（T27/M11；直调路径没有 started 行时才查——
    begin_lockbox 已消耗预算槽的卡在这里复查会把自己拒掉）。
    评估完成（无论过没过）写终态：started 行
    → done；无 started 行（直接调用路径）→ 插入 done 行。看过一眼即消耗。
    """
    cid = _card_id_of(card)
    if not approved_by:
        raise GateApprovalError(
            f"卡 {cid} 的锁箱（G5）是一次性测试，必须显式 --approve "
            "（并指定批准人）——本次未执行，一次性机会未消耗")
    if ledger.lockbox_status(cid) == "done":
        raise LockboxAlreadyUsedError(
            f"卡 {cid} 的锁箱已用过一次（lockbox_uses 有终态记录）——"
            "第二次请求直接拒绝；一次性机会已消耗，结论以第一次为准")
    if not ledger.gate_passed(cid, "G3"):
        raise LockboxPrerequisiteError(
            f"卡 {cid} 未通过 G3——G5 的参照分布取自 G3 拼接的样本外收益，"
            "没有可信参照不允许跑 G5")
    # 预算检查（T27/M11）：只查直调路径（该卡无任何行）；begin_lockbox
    # 已写 started 的卡预算槽已占，不复查
    if cutoff is not None and ledger.lockbox_status(cid) is None:
        _budget_check(ledger, cid, cutoff)
    timeframe = getattr(card, "timeframe", None)
    if bars_per_day is None:
        if not timeframe or str(timeframe).upper() not in TIMEFRAME_SECONDS:
            raise ValueError(
                "card 无有效 timeframe 且未传 bars_per_day——块长"
                "（一个交易日的 bar 数）无法确定")
        bars_per_day = max(1, round(86400.0
                                    / TIMEFRAME_SECONDS[timeframe.upper()]))
    if reference_returns is None or len(reference_returns) == 0:
        raise ValueError(
            "reference_returns 为空：参照分布需要 G3 拼接的样本外收益"
            "（未过 G3 的卡不允许跑 G5）")
    from mt5quant.backtest.report import bars_per_year
    bpy = lockbox_result.get("annualization_bars_per_year")
    if bpy is None:
        if not timeframe or str(timeframe).upper() not in TIMEFRAME_SECONDS:
            raise ValueError(
                "lockbox_result 缺 annualization_bars_per_year 且 card 无"
                "有效 timeframe——年化口径无法确定")
        bpy = bars_per_year(str(timeframe).upper())

    seed = vstats.rng_seed(cid)
    boot = vstats.block_bootstrap_sharpes(
        reference_returns, window_len=int(lockbox_result.get("bar_count") or 0),
        block_len=int(bars_per_day), sims=int(cfg.lockbox_bootstrap_sims),
        seed=seed, bars_per_year=float(bpy))
    p5 = float(np.percentile(boot, cfg.lockbox_sharpe_min_percentile))
    p95 = float(np.percentile(boot, cfg.lockbox_warn_percentile))

    expectancy = lockbox_result.get("expectancy")
    sharpe = lockbox_result.get("sharpe")
    trades = lockbox_result.get("trade_count")
    expected = float(expected_trades) if expected_trades is not None else None
    checks: dict[str, dict] = {
        "expectation_positive": {
            "passed": bool(expectancy is not None and expectancy > 0),
            "expectancy": expectancy},
        "trade_count": {
            "passed": bool(
                trades is not None and expected is not None
                and trades >= cfg.lockbox_min_trades_ratio * expected),
            "trades": trades, "expected_trades": expected,
            "min_ratio": float(cfg.lockbox_min_trades_ratio)},
        "sharpe_vs_reference": {
            "passed": bool(sharpe is not None and sharpe >= p5),
            "sharpe": sharpe, "p5": p5, "p95": p95,
            "percentile_min": float(cfg.lockbox_sharpe_min_percentile)},
    }
    result = {
        "passed": bool(all(c["passed"] for c in checks.values())),
        "checks": checks,
        "metrics": {k: lockbox_result.get(k) for k in
                    ("expectancy", "sharpe", "trade_count", "bar_count")},
        "reference": {"p5": p5, "p95": p95,
                      "sims": int(cfg.lockbox_bootstrap_sims),
                      "window_len": int(lockbox_result.get("bar_count") or 0),
                      "block_len": int(bars_per_day),
                      "bars_per_year": float(bpy)},
        "seed": int(seed),
    }
    if sharpe is not None and sharpe > p95:
        result["warning"] = (f"锁箱夏普 {sharpe:.2f} 高于参照分布 "
                             f"p95（{p95:.2f}）——异常好，复核数据与前视")
    from mt5quant.research.ledger import _cutoff_iso
    ledger.finish_lockbox_use(cid, result=result, approved_by=approved_by,
                              cutoff=_cutoff_iso(cutoff)
                              if cutoff is not None else None)
    return result


def load_lockbox_window(data_root, symbol: str, timeframe: str, *,
                        lookback_bars: int) -> tuple[pd.DataFrame,
                                                     pd.Timestamp]:
    """读锁箱**运行窗**（T21a/C3）：从 cutoff 前 lookback_bars 根起步。

    旧 load_lockbox_segment 只取 cutoff 之后的 bar，策略没有预热段时
    前段信号与全史不一致（B1 回看 1,704 根 H1）。本函数返回
    (bars, cutoff)：bars = [cutoff 前 lookback_bars 根, 数据末尾]，引擎在
    切片上空仓起步；调用方只统计 entry_time ≥ cutoff 的交易与 cutoff 之后
    的权益收益（bar_count 也按 cutoff 之后计）。
    """
    from mt5data.lockbox import LockboxError, cutoff_for, load_config
    from mt5data.store import load_bars

    if lookback_bars < 0:
        raise ValueError(f"lookback_bars 必须 ≥0，收到 {lookback_bars}")
    cutoff = cutoff_for(load_config(data_root), symbol, timeframe)
    if cutoff is None:
        raise LockboxError(
            f"{data_root} 无锁箱配置（lockbox.json）——G5 无从谈起，"
            "先跑 scripts/lockbox_init.py")
    df = load_bars(data_root, symbol, timeframe)
    if df is None or len(df) == 0:
        raise LockboxError(f"{symbol} {timeframe} 无 K 线数据")
    k = int(df["time"].searchsorted(pd.Timestamp(cutoff), side="left"))
    if k >= len(df):
        raise LockboxError(
            f"{symbol} {timeframe} 的锁箱段（time ≥ {cutoff.isoformat()}）为空")
    seg = df.iloc[max(0, k - int(lookback_bars)):].reset_index(drop=True)
    return seg, pd.Timestamp(cutoff)


def load_lockbox_segment(data_root, symbol: str, timeframe: str) -> pd.DataFrame:
    """读锁箱段（time ≥ 全局 cutoff）——只有 G5 一次性测试允许碰。

    T21a 起编排请改用 load_lockbox_window（带预热段）；本函数保留给
    无预热需求的只读检查。
    """
    seg, _ = load_lockbox_window(data_root, symbol, timeframe, lookback_bars=0)
    return seg


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
