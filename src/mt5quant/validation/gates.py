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
import math
import tomllib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from mt5data.store import TIMEFRAME_SECONDS
from mt5quant.validation import stats as vstats

DEFAULT_GATES_TOML = Path(__file__).resolve().parents[3] / "config" / "gates.toml"
# 冻结指纹（2026-10-29，T85/D34 第三次冻结：v3——T65 提案 + T85 工作点
# 补充标定按提案 §8 替换 gates.toml，v2/v3 双轨收束（load_gates 缺省即
# v3 语义，gates-v3.toml 随替换退役）；R14 §2 四项冻结条件同一提交：
# [v3].primary_test="dsr_v0"（L1，方案甲 V₀ 工作点）、[v3].n_min=2（L2）、
# [g4].dsr_trial_scope="rounds_lineage"（L1）。冻结历史：2026-10-24 T12
# 初值；2026-10-26 T21 第二次冻结（v2，68f49237…）。）
FROZEN_GATES_SHA256 = "d0a8b2f3105366e4173adcf15fb6dc650a032e277425ca7277abdc92086bb17b"

# G0–G5 = v2 关卡路径（顺序冻结不变，T86 守护测试锁定前六位）；V3 =
# 关卡 v3 套件（T86 bt 接线：主检验 + 三否决 + 诊断，bar 级 / 事件模式
# 两分支，--gates V3 显式选择——G1–G4 现行路径不受影响）。
GATE_ORDER = ("G0", "G1", "G2", "G3", "G4", "G5", "V3")


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
    # [g4] 可选（T64/D33）：G4 的 N 与试验夏普取数范围
    # family|project|rounds_lineage。缺键 → 缺省 family（v2 形态文件的
    # 冻结语义）；D34 冻结后的 config/gates.toml 显式写 rounds_lineage
    # （T85 冻结 L1：N = ledger.primary_test_n 的门槛部分）。
    dsr_trial_scope: str = "family"


@dataclass(frozen=True)
class GatesV3EventConfig:
    """关卡 v3 事件模式分支（config/gates.toml 的 [v3.event]，T85 写入
    D35 修改三条——T86 接线生效）。bar 级 [v3] 节的 alpha / cost_scale_veto
    复用不另设（同一 α、同一 ×1.5），本节只放事件模式特有键。"""

    primary_test: str = "randomization"   # 主检验：随机化参照（漂移稳健）
    # 随机化次数下限：max(draws_min_base, ⌈draws_per_test_coeff × N / α⌉)
    # ——保证 p 值分辨率足以判到 α/N（D35 修改 1；α 取 [v3].alpha）
    draws_min_base: int = 1999
    draws_per_test_coeff: float = 20.0
    delay_bars_veto: bool = False         # D35 修改 2：事件模式不适用"延迟一根"
    buyhold_excess_veto: bool = False     # D35 修改 2：不适用"买入持有超额"
    # D35 修改 3：诊断四项（只报告）
    diagnostics: tuple[str, ...] = (
        "year_block_vs_zero",     # 年块自助对 0 的检验
        "window_shift_1_bar",     # 事件窗前后各移一根 bar 的结果
        "segmented_means",        # 按 10 年或文献制度变化点分段均值
        "known_gaps",             # 已知缺口注记
    )


@dataclass(frozen=True)
class GatesV3Config(GatesConfig):
    """关卡 v3 配置（config/gates.toml，T85/D34 冻结 2026-10-29——缺省
    加载即 v3 语义，原 gates-v3.toml 已按提案 §8 替换退役）。[g1]–[g5]
    字段沿用 GatesConfig（[g1] 与 [g3].min_oos_ratio、[g4].mc_drawdown_*
    在 v3 归诊断层；[g5] 不变；[g3] 窗口是主检验的数据源），[v3] 节为
    主检验/否决键（冻结值 primary_test="dsr_v0"、n_min=2）与
    [v3.event] 事件模式分支（T85/D35，T86 接线）。"""

    # [v3]（蓝图 v4 附录 B"关卡 v3 提案框架"；T85/D34 冻结值）
    primary_test: str = "dsr_v0"     # 方案甲 dsr_v0（冻结生效）| 对照锚点
                                     # dsr | 方案乙 year_block
    n_min: int = 2                   # L2（R14 §2）：主检验 N 下限——
                                     # N_eff = max(N, n_min)；事件分支不受影响
    alpha: float = 0.05              # 方案乙名义单侧显著性（Bonferroni 前）
    bootstrap_sims: int = 1000       # 方案乙年块自助次数
    cost_scale_veto: float = 1.5     # 否决：成本 ×1.5 期望仍 > 0
    delay_bars: int = 1              # 否决：延迟一根 bar 净利仍 > 0
    buyhold_excess_veto: bool = True  # 否决：纯多头策略超额（vs 买入持有）> 0
    # [v3.event]（T85/D35 修改）：事件模式分支（T86 接线）
    event: GatesV3EventConfig = GatesV3EventConfig()


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

# 可选键及其缺省（T64/D33）：缺键 → 缺省 family（v2 形态文件的冻结
# 语义，缺省口径结果逐位一致）；D34 冻结后的 gates.toml 显式写
# rounds_lineage。合法值 family|project|rounds_lineage（第三值 T85
# 冻结 L1：N = ledger.primary_test_n 的门槛部分，轮次内 + 延续谱系）。
_OPTIONAL_DEFAULTS = {("g4", "dsr_trial_scope"): "family"}
_TRIAL_SCOPES = ("family", "project", "rounds_lineage")

# [v3] 节（T65/D34 冻结）：只在 suite="v3"（缺省）的解析路径出现；v2 的
# _parse 把 v3 当未知顶层键拒绝（历史 v2 形态文件不含它）。
_V3_SCHEMA: dict[str, dict[str, type]] = {
    "v3": {"primary_test": str, "n_min": int, "alpha": float,
           "bootstrap_sims": int, "cost_scale_veto": float, "delay_bars": int,
           "buyhold_excess_veto": bool}}
# T85 冻结（R14 §2 条件 1）：生效主检验 = 方案甲 V₀ 工作点 dsr_v0；
# dsr 保留为经验方差对照锚点（标定臂 N=1 ↔ PSR(0) 语义），year_block
# 为方案乙（备选，未选用）。
V3_PRIMARY_TESTS = ("dsr", "dsr_v0", "year_block")

# [v3.event]（T85/D35 修改）：事件模式分支——主检验=随机化参照 p ≤ α/N
# （N 口径同 [v3] 主检验：project_n_dedup_rounds + lineage_n_dedup）、
# 随机化次数下限 max(1999, ⌈20N/α⌉)、否决=成本 ×1.5 净均值 > 0、
# 不适用"买入持有超额"与"延迟一根"两项 bar 级否决、诊断四项（只报告）。
# alpha / cost_scale_veto 复用 [v3] 节（同一值，不另设第二来源）。
_V3_EVENT_SCHEMA: dict[str, dict[str, type]] = {
    "v3.event": {"primary_test": str, "draws_min_base": int,
                 "draws_per_test_coeff": float, "delay_bars_veto": bool,
                 "buyhold_excess_veto": bool}}
_V3_EVENT_PRIMARY_TESTS = ("randomization",)
_V3_EVENT_DIAGNOSTICS = ("year_block_vs_zero", "window_shift_1_bar",
                         "segmented_means", "known_gaps")


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
                    f"{list(_TRIAL_SCOPES)}，收到 {v!r}（T64/D33 + T85 冻结"
                    " L1：family=谱系 | project=项目 | rounds_lineage=轮次内"
                    "+延续谱系，即 ledger.primary_test_n 的门槛部分）")
            # 各节键名全局唯一，直接作字段名（顶层 version 除外）
            by_field[key] = v
    return GatesConfig(**by_field)


def _parse_v3(data: dict, source: str) -> GatesV3Config:
    """suite="v3" 的解析：[g1]–[g5] 复用 _parse（[v3]/[v3.event] 节先
    剥离），再校验 [v3] 节、[v3.event] 事件模式分支（T85/D35）与
    version=3。"""
    if data.get("version") != 3:
        raise GatesConfigError(
            f"{source}：version 必须是 3（关卡 v3 提案文件），收到 "
            f"{data.get('version')!r}")
    raw = data.get("v3")
    if not isinstance(raw, dict):
        raise GatesConfigError(f"{source}：[v3] 必须是表（主检验/否决键）")
    raw_event = raw.pop("event", None)          # [v3.event]（T85）单独校验
    fields = _V3_SCHEMA["v3"]
    unknown = sorted(set(raw) - set(fields))
    if unknown:
        raise GatesConfigError(
            f"{source}：[v3] 有未知键 {unknown}，合法键 {list(fields)}")
    base = _parse({k: v for k, v in data.items() if k != "v3"}, source)
    v3: dict = {}
    for key, typ in fields.items():
        if key not in raw:
            raise GatesConfigError(f"{source}：[v3] 缺键 {key}")
        v = raw[key]
        if isinstance(v, bool) is not (typ is bool) \
                or (typ is not bool and (isinstance(v, bool)
                                         or not isinstance(v, typ))):
            raise GatesConfigError(
                f"{source}：[v3].{key} 必须是 {typ.__name__}，收到 {v!r}")
        v3[key] = v
    if v3["primary_test"] not in V3_PRIMARY_TESTS:
        raise GatesConfigError(
            f"{source}：[v3].primary_test 只能是 {list(V3_PRIMARY_TESTS)}"
            f"（方案甲 V₀ 工作点 dsr_v0（冻结生效）/ 对照锚点 dsr / 方案乙"
            f" 年块自助），收到 {v3['primary_test']!r}")
    if int(v3["n_min"]) < 1:
        raise GatesConfigError(
            f"{source}：[v3].n_min 须 ≥1（L2/R14 §2：主检验 N 下限，"
            f"冻结值 2），收到 {v3['n_min']!r}")
    if not 0.0 < v3["alpha"] < 1.0:
        raise GatesConfigError(
            f"{source}：[v3].alpha 必须在 (0,1) 开区间，收到 {v3['alpha']!r}")
    if int(v3["bootstrap_sims"]) < 2:
        raise GatesConfigError(
            f"{source}：[v3].bootstrap_sims 须 ≥2（年块自助），收到 "
            f"{v3['bootstrap_sims']!r}")
    if float(v3["cost_scale_veto"]) < 1.0:
        raise GatesConfigError(
            f"{source}：[v3].cost_scale_veto 须 ≥1（成本放大倍数），收到 "
            f"{v3['cost_scale_veto']!r}")
    if int(v3["delay_bars"]) < 1:
        raise GatesConfigError(
            f"{source}：[v3].delay_bars 须 ≥1（蓝图：延迟一根），收到 "
            f"{v3['delay_bars']!r}")
    return GatesV3Config(
        **{f: getattr(base, f) for f in base.__dataclass_fields__},
        **v3, event=_parse_v3_event(raw_event, v3, source))


def _parse_v3_event(raw_event, v3: dict, source: str) -> GatesV3EventConfig:
    """[v3.event]（T85/D35）解析：键类型 + primary_test 枚举 + 诊断名
    枚举；diagnostics 是字符串数组（每个 ∈ 四项枚举，去重）。"""
    if not isinstance(raw_event, dict):
        raise GatesConfigError(
            f"{source}：[v3.event] 必须是表（事件模式分支，T85/D35）——"
            "缺表不可装载")
    fields = _V3_EVENT_SCHEMA["v3.event"]
    unknown = sorted(set(raw_event) - set(fields) - {"diagnostics"})
    if unknown:
        raise GatesConfigError(
            f"{source}：[v3.event] 有未知键 {unknown}，合法键 "
            f"{list(fields) + ['diagnostics']}")
    ev: dict = {}
    for key, typ in fields.items():
        if key not in raw_event:
            raise GatesConfigError(f"{source}：[v3.event] 缺键 {key}")
        v = raw_event[key]
        if isinstance(v, bool) is not (typ is bool) \
                or (typ is not bool and (isinstance(v, bool)
                                         or not isinstance(v, typ))):
            raise GatesConfigError(
                f"{source}：[v3.event].{key} 必须是 {typ.__name__}，"
                f"收到 {v!r}")
        ev[key] = v
    if ev["primary_test"] not in _V3_EVENT_PRIMARY_TESTS:
        raise GatesConfigError(
            f"{source}：[v3.event].primary_test 只能是 "
            f"{list(_V3_EVENT_PRIMARY_TESTS)}（D35 修改 1：随机化参照，"
            f"对资产漂移稳健），收到 {ev['primary_test']!r}")
    if int(ev["draws_min_base"]) < 2:
        raise GatesConfigError(
            f"{source}：[v3.event].draws_min_base 须 ≥2，收到 "
            f"{ev['draws_min_base']!r}")
    if float(ev["draws_per_test_coeff"]) <= 0.0:
        raise GatesConfigError(
            f"{source}：[v3.event].draws_per_test_coeff 须为正（下限公式 "
            f"max(base, ⌈coeff×N/α⌉)），收到 "
            f"{ev['draws_per_test_coeff']!r}")
    diags_raw = raw_event.get("diagnostics", list(_V3_EVENT_DIAGNOSTICS))
    if (not isinstance(diags_raw, list)
            or not all(isinstance(d, str) and d.strip() for d in diags_raw)):
        raise GatesConfigError(
            f"{source}：[v3.event].diagnostics 必须是字符串数组"
            f"（D35 修改 3 的四项诊断，只报告），收到 {diags_raw!r}")
    bad = sorted(set(diags_raw) - set(_V3_EVENT_DIAGNOSTICS))
    if bad:
        raise GatesConfigError(
            f"{source}：[v3.event].diagnostics 含未知项 {bad}，合法项 "
            f"{list(_V3_EVENT_DIAGNOSTICS)}")
    ev["diagnostics"] = tuple(diags_raw)
    return GatesV3EventConfig(**ev)


def load_gates(path=None, *, frozen_sha: str | None = None,
               suite: str = "v3") -> tuple[GatesConfig, str]:
    """加载并核对门槛文件；返回 (配置, 文件 sha256)。

    frozen_sha 为注入点（测试用）；缺省与冻结值比对。suite：
    - "v3"（缺省，D34 冻结 2026-10-29 后唯一现行口径）：config/gates.toml
      （即原 gates-v3.toml 内容，v2/v3 双轨收束）+ FROZEN_GATES_SHA256，
      返回 GatesV3Config；
    - "v2"（冻结前的历史口径，仅测试注入 v2 形态文件用）：必须显式给
      path——仓库缺省文件已是 v3，v2 解析对 [v3] 节按未知顶层键拒绝。
    """
    if suite == "v3":
        p = DEFAULT_GATES_TOML if path is None else Path(path)
        default_sha = FROZEN_GATES_SHA256
    elif suite == "v2":
        if path is None:
            raise GatesConfigError(
                'suite="v2" 需显式 path：D34 冻结（2026-10-29）后'
                " config/gates.toml 即 v3 内容，缺省加载即 v3 语义")
        p = Path(path)
        default_sha = FROZEN_GATES_SHA256
    else:
        raise GatesConfigError(
            f"suite 只能是 v2|v3（v3 为现行冻结口径），收到 {suite!r}")
    if not p.is_file():
        raise GatesConfigError(
            f"门槛文件不存在：{p}（config/gates.toml 必须入库）")
    sha = gates_sha256(p)
    expected = default_sha if frozen_sha is None else frozen_sha
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
    if suite == "v3":
        return _parse_v3(data, str(p)), sha
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


# ------------------------------------------------ 关卡 v3（T65/D34，冻结）
# 蓝图 v4 附录 B"关卡 v3 提案框架"：数据卫生否决（G0）+ 一个主检验（否决）
# + 稳健性否决（成本 ×1.5 / 延迟一根 / 多头相对买入持有超额为正）+ 其余
# 诊断（只报告）+ G5 不变。T85 冻结（R14 §2 批准，2026-10-29）后按方案甲：
# - 生效主检验 = 甲的 V₀ 工作点 dsr_v0：DSR(N, V₀) ≥ 0.95，V₀ = 1/(n−1)，
#   N 取 rounds_lineage 口径（[g4].dsr_trial_scope = "rounds_lineage"：
#   ledger.primary_test_n 的门槛部分 = project_n_dedup_rounds +
#   lineage_n_dedup），N_eff = max(N, [v3].n_min=2)；
# - 台账试验夏普的经验方差只进诊断；方案乙（year_block）保留为备选实现。

def v3_primary_dsr(oos_returns, *, n_trials: int, trial_sharpes,
                   cfg: GatesV3Config) -> dict:
    """主检验方案甲：walk-forward 拼接样本外收益的 PSR/DSR（否决）。

    n_trials / trial_sharpes 按项目级计数由调用方从台账 dedup 取数后传入
    （[g4].dsr_trial_scope="project"；标定臂不经台账，n_trials=1——DSR
    退化为 PSR(0)，与 gate_calibration 现行实验口径一致）。
    OOS 无方差/观测不足 → fail-closed（概率 0 + reason）。
    """
    try:
        probability = vstats.deflated_sharpe_probability(
            oos_returns, n_trials=n_trials, trial_sharpes=trial_sharpes)
        reason = None
    except ValueError as e:
        probability, reason = 0.0, str(e)
    return {
        "passed": bool(probability >= cfg.dsr_min_probability),
        "probability": float(probability),
        "min": float(cfg.dsr_min_probability),
        "n_trials": int(n_trials),
        "trial_scope": str(cfg.dsr_trial_scope),
        "test": "dsr",
        **({"reason": reason} if reason else {}),
    }


def null_variance_trial_sharpes(variance: float) -> list[float]:
    """V₀ 的确定性两点表示 [a, −a]（ddof=1 样本方差 = 2a² = variance）。

    DSR 的 trial_sharpes 只通过其样本方差进入检验（SR₀ =
    expected_max_sharpe(N, V)），两点集把"给定方差"表示成确定值——与 N
    无关、可逐位复现（第十三轮审核探针同法）。T85/§4.1 方差口径：方案甲
    的 V 取零假设抽样方差 V₀ = 1/(n−1)（n = 样本外 bar 数），台账里试验
    夏普的经验方差只作诊断。
    """
    if variance <= 0.0 or not math.isfinite(variance):
        raise ValueError(f"variance 必须为正有限值，收到 {variance!r}")
    a = math.sqrt(variance / 2.0)
    return [a, -a]


def v3_primary_dsr_null_variance(oos_returns, *, n_trials: int,
                                 cfg: GatesV3Config) -> dict:
    """主检验方案甲（T85 工作点口径）：DSR(N, V₀) ≥ 0.95，V₀ = 1/(n−1)
    为零假设下每根 bar 夏普估计的抽样方差（n = OOS bar 数）——同一 OOS
    收益，N 为项目级口径（T85：project_n_dedup_rounds + lineage）。

    N=1 时 SR₀=0，概率与 v3_primary_dsr(n_trials=1, trial_sharpes=None)
    逐位相同（现行 primary.dsr 键的语义对照锚点）。OOS 无方差/观测不足 →
    fail-closed（概率 0 + reason）。
    """
    arr = np.asarray(pd.Series(oos_returns).dropna(), dtype="float64")
    n = int(arr.size)
    if n < 3:
        return {"passed": False, "probability": 0.0, "min":
                float(cfg.dsr_min_probability), "n_trials": int(n_trials),
                "trial_scope": str(cfg.dsr_trial_scope), "test": "dsr_v0",
                "v0": None,
                "reason": f"OOS 观测不足（{n} < 3），V₀=1/(n−1) 无定义"}
    v0 = 1.0 / (n - 1.0)
    out = v3_primary_dsr(oos_returns, n_trials=n_trials,
                         trial_sharpes=null_variance_trial_sharpes(v0),
                         cfg=cfg)
    return {**out, "test": "dsr_v0", "v0": v0,
            "n_oos_bars": n}


def v3_primary_psr_bonferroni(oos_returns, *, n_trials: int,
                              cfg: GatesV3Config) -> dict:
    """主检验方案丙（T85/第十三轮 §4.1）：PSR(0) ≥ 1 − α/N（Bonferroni）。

    同一 OOS 收益：概率就是 PSR(0)（DSR 取 N=1、SR₀=0），与 N 无关；
    阈值 1 − α/N 随 N 收紧。OOS 无方差/观测不足 → fail-closed。"""
    n = max(1, int(n_trials))
    threshold = 1.0 - float(cfg.alpha) / n
    try:
        probability = vstats.deflated_sharpe_probability(
            oos_returns, n_trials=1, trial_sharpes=None)
        reason = None
    except ValueError as e:
        probability, reason = 0.0, str(e)
    return {
        "passed": bool(probability >= threshold),
        "probability": float(probability),
        "threshold": threshold,
        "alpha": float(cfg.alpha),
        "bonferroni_n": n,
        "n_trials": n,
        "trial_scope": str(cfg.dsr_trial_scope),
        "test": "psr0_bonferroni",
        **({"reason": reason} if reason else {}),
    }


def v3_event_draws_min(n_tests: int, cfg: GatesV3Config) -> int:
    """事件模式随机化次数下限（D35 修改 1/T85）：
    max(draws_min_base, ⌈draws_per_test_coeff × N / α⌉)——α 取 [v3].alpha。
    保证 p 值分辨率（≥1/次数）足以判到 α/N。"""
    n = max(1, int(n_tests))
    ev = cfg.event
    return max(int(ev.draws_min_base),
               math.ceil(float(ev.draws_per_test_coeff) * n
                         / float(cfg.alpha)))


def v3_primary_year_block(oos_returns, *, n_trials: int,
                          cfg: GatesV3Config, seed: int) -> dict:
    """主检验方案乙：同一 OOS 收益序列按年块自助的单侧 p 值（否决）。

    Bonferroni：p ≤ alpha / N（N = 项目级试验数，与方案甲同一 [g4].
    dsr_trial_scope 取数）。序列须带时间索引（年块按自然年分块；块内
    结构保留、年内相关折叠）；事件模式传 event_returns DataFrame
    （entry_time 列）——同一实现（events.event_test，T66 已单独标定）。
    年块 <2 → fail-closed（不通过 + reason，绝不猜）。
    """
    from mt5quant.validation import events as vevents
    n = max(1, int(n_trials))
    threshold = float(cfg.alpha) / n
    try:
        test = vevents.event_test(oos_returns, sims=int(cfg.bootstrap_sims),
                                  seed=int(seed))
    except ValueError as e:
        return {"passed": False, "p_value": None, "reason": str(e),
                "threshold": threshold, "alpha": float(cfg.alpha),
                "bonferroni_n": n, "test": "year_block"}
    return {
        "passed": bool(test["p_value"] <= threshold),
        "p_value": float(test["p_value"]),
        "threshold": threshold,
        "alpha": float(cfg.alpha),
        "bonferroni_n": n,
        "n_blocks": int(test["n_blocks"]),
        "mean": float(test["mean"]),
        "test": "year_block",
    }


def v3_n_eff(n_trials: int, cfg: GatesV3Config) -> int:
    """主检验有效试验数 N_eff = max(N, [v3].n_min)（L2/R14 §2 条件 2）。

    冻结值 n_min=2：N=1 不在标定网格（{2, 6, 24}）内，且 T85 §4.3 实测
    PSR 家族在 N=1 的联合误报偏高（H4|slot 25%）——主检验按下限 2 取数。
    事件模式分支不适用（随机化检验在 N=1 时也是精确的，
    v3_event_draws_min 直接用 raw N）。"""
    return max(int(n_trials), int(cfg.n_min))


def run_primary_test(oos_returns, *, n_trials: int, cfg: GatesV3Config,
                     seed: int, trial_sharpes=None) -> dict:
    """按 [v3].primary_test 分派主检验（T86 bt 接线的生产入口；标定臂
    两方案都算，不经此函数）。

    T85 冻结（R14 §2 条件 1）：生效主检验 = 方案甲 V₀ 工作点
    （"dsr_v0" → v3_primary_dsr_null_variance，V₀=1/(n−1)）；台账试验
    夏普的经验方差（trial_sharpes）只进诊断——dsr_v0 分派忽略它。
    L2（条件 2）：分派层取 N_eff = max(n_trials, cfg.n_min)（[v3].n_min=2）
    ——直调函数按传入 N 原样计算（对照锚点语义不变）；事件模式分支
    不经此函数（随机化检验 N=1 也精确，不吃 n_min）。"""
    n_eff = v3_n_eff(n_trials, cfg)
    if cfg.primary_test == "dsr_v0":
        return v3_primary_dsr_null_variance(oos_returns, n_trials=n_eff,
                                            cfg=cfg)
    if cfg.primary_test == "dsr":
        return v3_primary_dsr(oos_returns, n_trials=n_eff,
                              trial_sharpes=trial_sharpes, cfg=cfg)
    if cfg.primary_test == "year_block":
        return v3_primary_year_block(oos_returns, n_trials=n_eff,
                                     cfg=cfg, seed=seed)
    raise GatesConfigError(
        f"[v3].primary_test 只能是 {list(V3_PRIMARY_TESTS)}，收到 "
        f"{cfg.primary_test!r}（schema 已挡——此处 fail-closed 兜底）")


def v3_veto_cost_scale(spec, bars, costs, params, cfg: GatesV3Config) -> dict:
    """v3 稳健性否决 1：成本 ×[v3].cost_scale_veto 期望（每笔净利均值）
    仍 > 0。G2 cost_scaling 的 v3 精简版（×2 备查与越幅/早年点差敏感性
    归诊断报告，不在此复算）。"""
    from mt5quant.validation import robustness as vrob
    scaled = vrob.run_engine(
        spec, bars, vrob.ScaledCosts(costs, float(cfg.cost_scale_veto)),
        params)
    exp = (float(scaled.trades["net"].mean())
           if len(scaled.trades) else None)
    return {"passed": bool(exp is not None and exp > 0),
            "expectancy_scaled": exp,
            "scale": float(cfg.cost_scale_veto)}


def v3_veto_delay(spec, bars, costs, params, cfg: GatesV3Config) -> dict:
    """v3 稳健性否决 2：全部信号延迟 [v3].delay_bars 根 bar 执行（缺省 1，
    蓝图"延迟一根"）净利仍 > 0。"""
    import numpy as _np
    from mt5quant.validation import robustness as vrob
    k = int(cfg.delay_bars)
    strategy = spec.strategy
    values = strategy.generate_signals(bars, params).to_numpy()
    delayed = _np.concatenate((_np.zeros(k, dtype="int64"),
                               values[:-k])).astype("int64")
    fixed = vrob.FixedSignals(delayed, exit_rules=strategy.exit_rules)
    delayed_run = vrob.run_engine(spec, bars, costs, {}, strategy=fixed)
    original_run = vrob.run_engine(spec, bars, costs, params)
    net_delayed = (float(delayed_run.trades["net"].sum())
                   if len(delayed_run.trades) else 0.0)
    net_original = (float(original_run.trades["net"].sum())
                    if len(original_run.trades) else 0.0)
    return {"passed": bool(net_delayed > 0), "delay_bars": k,
            "net_original": net_original, "net_delayed": net_delayed}


def v3_veto_buyhold(spec, bars, costs, trades, base_sharpe,
                    cfg: GatesV3Config) -> dict:
    """v3 稳健性否决 3：纯多头策略夏普须高于同期买入持有（超额为正）；
    非纯多头不适用（passed=True，不否决）。复用 vrob.buy_hold_check。"""
    from mt5quant.validation import robustness as vrob
    if not cfg.buyhold_excess_veto:
        return {"passed": True, "applicable": False,
                "note": "[v3].buyhold_excess_veto = false——否决关闭"}
    return vrob.buy_hold_check(spec, bars, costs, trades, base_sharpe, cfg)


V3_VETO_LAYERS = ("G0", "primary", "cost_scale_x15", "delay_1_bar",
                  "buyhold_excess")


def evaluate_v3(*, g0: dict, primary: dict, cost_scale: dict, delay: dict,
                buyhold: dict, diagnostics: dict | None = None) -> dict:
    """关卡 v3 判定（蓝图附录 B 提案框架）：**否决层全过才通过**——
    G0 ∧ 主检验 ∧ 成本 ×1.5 ∧ 延迟一根 ∧ 多头超额；诊断层（参数邻域、
    按年分段、去最好 5%、G1 样本内线、回撤分位等）只报告，失败不改变
    通过结论（异常时人工复核）。G5 不变，仍是独立的一次性关卡。
    """
    veto = {
        "G0": bool(g0["passed"]),
        "primary": bool(primary["passed"]),
        "cost_scale_x15": bool(cost_scale["passed"]),
        "delay_1_bar": bool(delay["passed"]),
        "buyhold_excess": bool(buyhold["passed"]),
    }
    diags = {name: bool(check.get("passed", False))
             for name, check in (diagnostics or {}).items()}
    return {
        "passed": bool(all(veto.values())),
        "veto": veto,
        "diagnostics": diags,
        "failed_vetoes": [k for k in V3_VETO_LAYERS if not veto[k]],
    }


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
