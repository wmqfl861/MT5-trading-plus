"""候选登记（蓝图 v4 附录 C）与预筛（§6.2 五条硬条件；铁律 17、18）。

从 T61 的最小登记（cards.candidate_statuses_by_card，只认 id/status/
source_card 三个字段）迁移为**完整附录 C 装载器**：字段齐全、类型正确、
枚举合法才可加载（缺必填 → CandidateError；search_log 必须非空——检索过
反驳要留记录，没找到也要写检索词与日期）。T61 的 C900–C909（specimen/
withdrawn 登记簿）不带数值声明 [declared]，装载兼容；它们不是研究候选，
precheck 对其短路为 fail（理由写明），不进入预筛数值条件。

预筛（precheck，开卡前的硬闸门，蓝图 §6.2）：
1. 文献：original 非空；replications/rebuttals 可空（检索记录在
   search_log）；expected_effect 必填有据（装载层强制非空；折算依据的
   文字写在同一字段——缺复现按发表后 −58% 折算，McLean & Pontiff 2016）；
2. 持仓周期 ≥ H1（信号周期低于 H1 的候选直接 fail）；
3. 事前成本倍数 ≥ 3：候选声明的每事件/每笔折算后预期毛收益（相对价格）
   ÷ 成本模型 v3 在入场、出场时刻的来回成本（点差+滑点双边，佣金与
   快照隔夜费按保守计入——隔夜费率取 long/short 中更不利的一侧 ×声明
   计费夜数）；
4. 检验力 ≥ 50%：在研究库实际覆盖年数（品种×周期，coverage_years 从
   meta 水位/parquet 实测 + 已批准外部序列合并（T80/G3，data_research/
   ext/ 白名单 fred/cdd，逐段列来源），不用常数）上算单次检验
   （mt5quant.research.power，与 round11 探针逐位一致）；夏普取**声明
   与隐含中较小者**（T80/G2：隐含夏普 =（声明每事件毛收益 − 事前来回
   成本）÷ 品种在事件窗长度上的无条件收益波动 σ × √每年事件数；σ 由
   工具从研究库算——按候选声明的 window_bars 根持仓周期 bar 取同长度
   滚动窗口收益（T84/K4：单根 bar 口径少算 √h），日内周期再按
   entry_hour_ny 取同一时段起点，与 T66 的 σ_w 共用一个函数
   （validation.events.window_volatility）；随机时点抽样的总体极限 =
   品种性质、与候选信号无关）；σ=0 或序列不可用 → 隐含记 None、按
   声明夏普计；不足 → waiting_data（铁律 18"待数据"队列），不是 fail；
   预筛仍按单次检验判定（蓝图 §6.2 条件 4），α/N 只用于事件模式样本
   量规则与 bt round open 的开轮复核（T84）；
5. 机制空缺：组合里已有同类别活跃谱系 → **排后**（排序类理由，不阻塞
   ——决定只有 pass/fail/waiting_data 三值，本条件作为理由记录）。

预筛结果由工具（bt candidate check）生成 research/candidates/
C###.precheck.json 并记入台账 candidate_checks 表（T62；带哈希链），
候选 TOML 里的 [precheck] 段是手填入口——装载器见到即拒。
"""
from __future__ import annotations

import json
import math
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from mt5data.store import TIMEFRAME_SECONDS, load_bars
from mt5quant.research import cards
from mt5quant.research import power

__all__ = [
    "CandidateError", "Candidate", "Literature", "Declared", "ProbDeclared",
    "PrecheckResult", "MECHANISM_CLASSES", "RESEARCH_STATUSES",
    "BOOKKEEPING_STATUSES", "PRECHECK_DECISIONS", "load_candidate",
    "coverage_years", "coverage_segments", "merged_coverage_years",
    "unconditional_volatility", "precheck", "precheck_prob", "propose_split",
    "measured_n_required",
]

# 附录 C：机制类别七分类（蓝图 v4 §6.2 预筛条件 5）+ P 线价位类（D45/T76）
MECHANISM_CLASSES = ("trend", "reversal", "flow_calendar", "risk_premium",
                     "volatility", "cross_asset", "event", "price_levels")

# 研究候选状态（附录 C status 枚举去掉 T61 的簿记两态）
RESEARCH_STATUSES = ("registered", "screened_pass", "screened_fail",
                     "waiting_data", "in_round")
# T61/D30 簿记状态：标定样品 / 撤回——不是研究候选，不进入预筛数值条件
BOOKKEEPING_STATUSES = ("withdrawn", "specimen")

PRECHECK_DECISIONS = ("pass", "fail", "waiting_data")

_COST_MULTIPLE_MIN = 3.0        # 预筛条件 3：每笔预期毛利 ≥ 3 倍来回成本
_POWER_MIN = 0.5                # 预筛条件 4：单次检验力 ≥ 50%（铁律 18）
_HOLDING_MIN = "H1"             # 预筛条件 2：持仓周期 ≥ H1
_POWER_ALPHA = 0.05             # 单侧 5%（与蓝图 §6.3 表同口径）
_POWER_N_TESTS = 1              # 铁律 18：单次检验

_ID_PATTERN = re.compile(r"^C\d{3,}$")
_TOP_KEYS = ("id", "title", "mechanism_class", "status", "symbols",
             "holding", "mechanism", "source_card", "notes",
             "observed_in_sample", "continues_family")
# continues_family 合法形态（T85）：卡谱系 id（H012 这类，cards.family_of
# 的输出域）——字母前缀 + 数字，不带 -v 版本后缀（那是卡 id 不是谱系）。
_FAMILY_PATTERN = re.compile(r"^[A-Z]+\d{2,}$")
_LIT_KEYS = ("original", "replications", "rebuttals", "search_log",
             "published_effect", "expected_effect")
_DECLARED_KEYS = ("events_per_year", "gross_return_per_event",
                  "window_bars", "entry_hour_ny", "exit_hour_ny",
                  "overnight_nights", "expected_sharpe")

_DAYS_PER_YEAR = 365.25


class CandidateError(ValueError):
    """候选登记不符合附录 C 模式（缺字段/类型错/枚举外/[precheck] 手填）。"""


@dataclass(frozen=True)
class Literature:
    """[literature]：原始论文 + 复现/反驳检索记录 + 折算依据（附录 C）。"""
    original: tuple[str, ...]
    replications: tuple[str, ...]
    rebuttals: tuple[str, ...]
    search_log: str
    published_effect: str
    expected_effect: str


@dataclass(frozen=True)
class Declared:
    """[declared]：折算后的事前数值声明（预筛的计算输入）。

    全部为**事前**量：不得取自本项目数据的样本内结果（铁律 17），折算
    依据写在 [literature].expected_effect；缺复现按发表后 −58% 折算
    （McLean & Pontiff 2016）。研究状态（registered/…）的候选必填；
    specimen/withdrawn 簿记不带（兼容 T61 登记）。
    """
    events_per_year: float          # 每年事件数（事件模式）或交易数
    gross_return_per_event: float   # 每事件/每笔预期毛收益（相对价格，0.001=10bp）
    window_bars: int                # 事件窗持仓 bar 数（T84/K4：σ 按此窗长取）
    entry_hour_ny: int              # 入场成交的纽约本地小时（成本取该小时实测）
    exit_hour_ny: int               # 出场成交的纽约本地小时
    overnight_nights: float         # 每笔/每事件预期计费夜数（0=不过夜；保守申报）
    expected_sharpe: float          # 折算后预期年化夏普（检验力用）


# ------------------------------------------------------------------ P 线（T76）
# [prob] 段：概率模式候选（mechanism_class="price_levels"）的事前声明。
# 与 H 线 [declared] 平行：全部事前量（文献折算），不得取自样本内。

_PROB_SITUATIONS = ("S1", "S2", "S3")
_HYPOTHESIS_PATTERN = re.compile(r"^S[123]:A[1-4]:[A-Za-z][A-Za-z0-9_>]*$")


@dataclass(frozen=True)
class ProbDeclared:
    """[prob]：概率模式候选的检验配置与折算后预期 Δp（蓝图 P 线附录 A 的
    候选侧子集；P1 只用发现段——D50）。"""

    situations: tuple[str, ...]     # ⊆ {S1,S2,S3}
    hypotheses: tuple[str, ...]     # "S1:A3:fade>base"（≤4，蓝图 P 线 §7）
    bracket: dict                   # {sl_atr, tp_atr, horizon_bars}（全动作共用，D44）
    level_grid: dict                # 品种 → 网格间距（S1 必填；蓝图 §2.1）
    approach: dict                  # {lookback_bars, min_distance_atr, touch_atr}
    breakout_atr: float             # b（A1 突破幅度，ATR 倍数）
    retest_window_bars: int         # m（A2/A4 窗口）
    expected_delta_p: float         # 折算后预期 Δp（单侧赢率增量，文献折算）
    # 审核四 E2：五个决策字段（缺省 None=P1 未写；写了须与策略类常量一致）
    features: tuple[str, ...] | None = None
    prior_strength: int | None = None
    min_cell: int | None = None
    lb_z: float | None = None
    entry_threshold_cost_mult: float | None = None
    # T97 数据解锁批（M2）三个可选键——缺省 None：不带新键的候选装载与
    # precheck 行为逐位不变（探针兼容，硬约束）。cost_scenario_mult 缺省
    # None 时调用方按 [1.0, 1.0]（恒等，旧卡/探针零变化）处理——字段本身
    # 是 tuple（装载时从 TOML list 转换）。
    max_gap_days: int | None = None             # 触发抑制的洞宽阈值（天数）
    cost_scenario_mult: tuple[float, float] | None = None   # [1x 基准, 情景档]
    cost_scenario_reference: float | None = None  # 非绑定参考档倍数（不参与判定）


def _validate_prob(raw: object, source: str, symbols: tuple[str, ...]
                   ) -> ProbDeclared:
    """[prob] 段校验（fail-fast，与 [declared] 同风格）。"""
    if not isinstance(raw, dict):
        raise CandidateError(
            f"{source}：[prob] 必须是 TOML 表，收到 {type(raw).__name__}")
    unknown = sorted(set(raw) - {
        "situations", "hypotheses", "bracket", "level_grid", "approach",
        "breakout_atr", "retest_window_bars", "expected_delta_p",
        # 审核四 E2：蓝图附录 A 的五个决策字段（P2 必写、P1 可选，
        # 写了即校验并与策略类常量核对）
        "features", "prior_strength", "min_cell", "lb_z",
        "entry_threshold_cost_mult",
        # T97 数据解锁批（M2）：三个可选键——出现才校验，缺省不破坏
        # 旧候选（探针的 prob_data 不带新键，装载行为须逐位不变）
        "max_gap_days", "cost_scenario_mult", "cost_scenario_reference"})
    if unknown:
        raise CandidateError(f"{source}：[prob] 未知字段 {unknown}")

    def _pos(name: str, v) -> float:
        if isinstance(v, bool) or not isinstance(v, (int, float)) \
                or not math.isfinite(v) or v <= 0:
            raise CandidateError(
                f"{source}：[prob].{name} 必须是有限正数，收到 {v!r}")
        return float(v)

    def _int(name: str, v) -> int:
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise CandidateError(
                f"{source}：[prob].{name} 必须是 ≥1 整数，收到 {v!r}")
        return int(v)

    situations = raw.get("situations")
    if (not isinstance(situations, list) or not situations
            or not all(s in _PROB_SITUATIONS for s in situations)):
        raise CandidateError(
            f"{source}：[prob].situations 必须是 {_PROB_SITUATIONS} 的"
            f"非空子集，收到 {situations!r}")
    hypotheses = raw.get("hypotheses")
    if (not isinstance(hypotheses, list) or not hypotheses
            or len(hypotheses) > 4
            or not all(isinstance(h, str) and _HYPOTHESIS_PATTERN.match(h)
                       for h in hypotheses)):
        raise CandidateError(
            f"{source}：[prob].hypotheses 必须形如 'S1:A3:fade>base'，"
            f"每候选 ≤4 条（蓝图 P 线 §7），收到 {hypotheses!r}")
    for h in hypotheses:
        if h.split(":")[0] not in situations:
            raise CandidateError(
                f"{source}：假设 {h!r} 引用了 situations 之外的局面")
    bracket = raw.get("bracket")
    if not isinstance(bracket, dict) \
            or set(bracket) != {"sl_atr", "tp_atr", "horizon_bars"}:
        raise CandidateError(
            f"{source}：[prob].bracket 必须含且只含 "
            "{sl_atr, tp_atr, horizon_bars}（D44：全动作共用）")
    b = {"sl_atr": _pos("bracket.sl_atr", bracket["sl_atr"]),
         "tp_atr": _pos("bracket.tp_atr", bracket["tp_atr"]),
         "horizon_bars": _int("bracket.horizon_bars", bracket["horizon_bars"])}
    level_grid = raw.get("level_grid", {})
    if not isinstance(level_grid, dict):
        raise CandidateError(
            f"{source}：[prob].level_grid 必须是 品种→间距 表，收到 "
            f"{level_grid!r}")
    for sym, step in level_grid.items():
        if sym not in symbols:
            raise CandidateError(
                f"{source}：[prob].level_grid 含候选外品种 {sym!r}")
        _pos(f"level_grid.{sym}", step)
    if "S1" in situations:
        if not level_grid:
            raise CandidateError(
                f"{source}：S1 整数关口需要 [prob].level_grid（品种→网格间距，"
                "蓝图 P 线 §2.1）")
        miss = [s for s in symbols if s not in level_grid]
        if miss:
            raise CandidateError(
                f"{source}：S1 候选的每个品种都要在 [prob].level_grid 里"
                f"（缺 {miss}——计数按品种取网格间距）")
    approach = raw.get("approach")
    if not isinstance(approach, dict) \
            or set(approach) != {"lookback_bars", "min_distance_atr",
                                 "touch_atr"}:
        raise CandidateError(
            f"{source}：[prob].approach 必须含且只含 "
            "{lookback_bars, min_distance_atr, touch_atr}")
    ap = {"lookback_bars": _int("approach.lookback_bars",
                                approach["lookback_bars"]),
          "min_distance_atr": _pos("approach.min_distance_atr",
                                   approach["min_distance_atr"]),
          "touch_atr": _pos("approach.touch_atr", approach["touch_atr"])}
    dp = raw.get("expected_delta_p")
    if isinstance(dp, bool) or not isinstance(dp, (int, float)) \
            or not math.isfinite(dp) or not 0.0 < dp < 0.5:
        raise CandidateError(
            f"{source}：[prob].expected_delta_p 必须在开区间 (0, 0.5)"
            f"（文献折算的预期赢率增量），收到 {dp!r}")
    # ---- 审核四 E2：五个决策字段（可选；写了即校验） ----
    feats = raw.get("features", None)
    if feats is not None:
        if (not isinstance(feats, list)
                or not all(isinstance(x, str) for x in feats)
                or len(feats) > 2
                or not set(feats) <= {"trend_sma200", "atr_pct_2000"}):
            raise CandidateError(
                f"{source}：[prob].features 必须是 "
                '["trend_sma200", "atr_pct_2000"] 的子集（≤2 项），'
                f"收到 {feats!r}")
    for fname, fmin in (("prior_strength", 1), ("min_cell", 1)):
        v = raw.get(fname, None)
        if v is not None and (isinstance(v, bool)
                              or not isinstance(v, int) or v < fmin):
            raise CandidateError(
                f"{source}：[prob].{fname} 必须是 ≥{fmin} 整数，收到 {v!r}")
    for fname in ("lb_z", "entry_threshold_cost_mult"):
        v = raw.get(fname, None)
        if v is not None and (isinstance(v, bool)
                              or not isinstance(v, (int, float))
                              or not math.isfinite(v) or v <= 0):
            raise CandidateError(
                f"{source}：[prob].{fname} 必须是有限正数，收到 {v!r}")
    # ---- T97 数据解锁批（M2）：三个可选键（出现才校验；缺省 None） ----
    mgd = raw.get("max_gap_days", None)
    if mgd is not None and (isinstance(mgd, bool) or not isinstance(mgd, int)
                            or mgd < 1):
        raise CandidateError(
            f"{source}：[prob].max_gap_days 必须是 ≥1 整数（触发抑制的"
            f"洞宽阈值，天数——先决 4），收到 {mgd!r}")
    csm = raw.get("cost_scenario_mult", None)
    if csm is not None:
        if (not isinstance(csm, list) or len(csm) != 2
                or any(isinstance(x, bool)
                       or not isinstance(x, (int, float))
                       or not math.isfinite(x) or x <= 0 for x in csm)
                or float(csm[0]) != 1.0):
            raise CandidateError(
                f"{source}：[prob].cost_scenario_mult 必须是二元列表、元素"
                "为有限正数且首元素 = 1.0（1x 基准档 + 情景档；T97 保守"
                "绑定档沿用 H 线 robustness.EARLY_YEARS_SPREAD_FACTOR），"
                f"收到 {csm!r}")
    csr = raw.get("cost_scenario_reference", None)
    if csr is not None and (isinstance(csr, bool)
                            or not isinstance(csr, (int, float))
                            or not math.isfinite(csr) or csr <= 0):
        raise CandidateError(
            f"{source}：[prob].cost_scenario_reference 必须是有限正数"
            f"（非绑定参考档倍数，只并列报告不参与判定），收到 {csr!r}")
    return ProbDeclared(
        situations=tuple(situations), hypotheses=tuple(hypotheses),
        bracket=b, level_grid=dict(level_grid), approach=ap,
        breakout_atr=_pos("breakout_atr", raw.get("breakout_atr")),
        retest_window_bars=_int("retest_window_bars",
                                raw.get("retest_window_bars")),
        expected_delta_p=float(dp),
        features=(tuple(feats) if feats is not None else None),
        prior_strength=raw.get("prior_strength"),
        min_cell=raw.get("min_cell"),
        lb_z=(float(raw["lb_z"]) if raw.get("lb_z") is not None else None),
        entry_threshold_cost_mult=(
            float(raw["entry_threshold_cost_mult"])
            if raw.get("entry_threshold_cost_mult") is not None else None),
        # T97 数据解锁批（M2）：三新键（list → tuple，缺省 None）
        max_gap_days=(int(mgd) if mgd is not None else None),
        cost_scenario_mult=(tuple(float(x) for x in csm)
                            if csm is not None else None),
        cost_scenario_reference=(float(csr) if csr is not None else None))


@dataclass(frozen=True)
class Candidate:
    """一张已通过附录 C 模式校验的候选登记。"""
    id: str
    title: str
    mechanism_class: str
    status: str
    symbols: tuple[str, ...]
    holding: str
    mechanism: str
    literature: Literature
    source_card: str = ""           # T61 簿记登记指向的卡（研究候选为空）
    notes: str = ""
    observed_in_sample: str = ""    # T84/K3：样本内被看过时如实登记来源（如 "T66 §3.2"）
    continues_family: str = ""      # T85/K1：延续的历史谱系（如 "H012"）——
    # 主检验 N 的 lineage 成分取数对象（ledger.lineage_n_dedup）；空 = 全新
    # 机制（lineage N = 0）。不带 -v 后缀（谱系 id，不是卡 id）。
    declared: Declared | None = None
    prob_declared: ProbDeclared | None = None   # T76：[prob]（price_lines 候选）
    path: str = ""


@dataclass(frozen=True)
class PrecheckResult:
    """预筛结论：决定 + 理由 + 逐条件明细（JSON 可序列化，随候选提交）。"""
    candidate_id: str
    decision: str                   # pass | fail | waiting_data
    reasons: tuple[str, ...]
    checks: dict = field(default_factory=dict)
    events_per_year: float | None = None

    def to_payload(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "decision": self.decision,
            "reasons": list(self.reasons),
            "events_per_year": self.events_per_year,
            "checks": self.checks,
        }


# ---------------------------------------------------------------- 校验工具

def _require_str(data: dict, key: str, source: str, *,
                 default: str | None = None) -> str:
    v = data.get(key, default)
    if not isinstance(v, str):
        raise CandidateError(f"{source}：字段 {key!r} 必须是字符串，收到 {v!r}")
    if default is None and not v.strip():
        raise CandidateError(f"{source}：字段 {key!r} 不能为空")
    return v


def _require_str_list(raw: object, key: str, source: str, *,
                      nonempty: bool) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise CandidateError(
            f"{source}：[literature].{key} 必须是字符串数组，收到 {raw!r}")
    for v in raw:
        if not isinstance(v, str) or not v.strip():
            raise CandidateError(
                f"{source}：[literature].{key} 的元素必须是非空字符串，"
                f"收到 {v!r}")
    if nonempty and not raw:
        raise CandidateError(
            f"{source}：[literature].{key} 不能为空（原始论文必填；"
            "复现/反驳允许为空，但检索记录必须写在 search_log）")
    return tuple(raw)


def _validate_literature(raw: object, source: str) -> Literature:
    if not isinstance(raw, dict):
        raise CandidateError(
            f"{source}：[literature] 表缺失或不是表（附录 C：文献材料是"
            "预筛条件 1 的输入，缺表不可装载）")
    unknown = sorted(set(raw) - set(_LIT_KEYS))
    if unknown:
        raise CandidateError(
            f"{source}：[literature] 有未知键 {unknown}，合法键 {list(_LIT_KEYS)}")
    for k in _LIT_KEYS:
        if k not in raw:
            raise CandidateError(f"{source}：[literature] 缺键 {k!r}")
    return Literature(
        original=_require_str_list(raw["original"], "original", source,
                                   nonempty=True),
        replications=_require_str_list(raw["replications"], "replications",
                                       source, nonempty=False),
        rebuttals=_require_str_list(raw["rebuttals"], "rebuttals", source,
                                    nonempty=False),
        search_log=_require_str(raw, "search_log", source),
        published_effect=_require_str(raw, "published_effect", source),
        expected_effect=_require_str(raw, "expected_effect", source),
    )


def _validate_declared(raw: object, source: str) -> Declared:
    if not isinstance(raw, dict):
        raise CandidateError(f"{source}：[declared] 必须是表")
    unknown = sorted(set(raw) - set(_DECLARED_KEYS))
    if unknown:
        raise CandidateError(
            f"{source}：[declared] 有未知键 {unknown}，合法键 "
            f"{list(_DECLARED_KEYS)}")
    for k in _DECLARED_KEYS:
        if k not in raw:
            raise CandidateError(f"{source}：[declared] 缺键 {k!r}")

    def _num(k: str, *, positive: bool) -> float:
        v = raw[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) \
                or not math.isfinite(float(v)):
            raise CandidateError(
                f"{source}：[declared].{k} 必须是有限数值，收到 {v!r}")
        v = float(v)
        if positive and v <= 0.0:
            raise CandidateError(
                f"{source}：[declared].{k} 必须为正，收到 {v!r}")
        if not positive and v < 0.0:
            raise CandidateError(
                f"{source}：[declared].{k} 不能为负，收到 {v!r}")
        return v

    def _hour(k: str) -> int:
        v = raw[k]
        if isinstance(v, bool) or not isinstance(v, int) \
                or not 0 <= v <= 23:
            raise CandidateError(
                f"{source}：[declared].{k} 必须是 0..23 的整数"
                f"（纽约本地小时），收到 {v!r}")
        return int(v)

    def _window_bars() -> int:
        v = raw["window_bars"]
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise CandidateError(
                f"{source}：[declared].window_bars 必须是 ≥1 的整数"
                f"（事件窗持仓 bar 数——T84/K4：σ 按此窗长取，缺算会少算"
                f"√h、隐含夏普多算 √h），收到 {v!r}")
        return int(v)

    return Declared(
        events_per_year=_num("events_per_year", positive=True),
        gross_return_per_event=_num("gross_return_per_event", positive=True),
        window_bars=_window_bars(),
        entry_hour_ny=_hour("entry_hour_ny"),
        exit_hour_ny=_hour("exit_hour_ny"),
        overnight_nights=_num("overnight_nights", positive=False),
        expected_sharpe=_num("expected_sharpe", positive=False),
    )


def validate_candidate(data: dict, *, source: str) -> Candidate:
    """附录 C 模式校验（fail-fast）。"""
    if not isinstance(data, dict):
        raise CandidateError(f"{source}：候选登记必须是 TOML 表")
    unknown = sorted(set(data) - set(_TOP_KEYS)
                     - {"literature", "declared", "prob"})
    if "precheck" in data:
        raise CandidateError(
            f"{source}：[precheck] 段不允许手填——预筛结果由 bt candidate "
            "check 生成 C###.precheck.json 并记入台账（T62），候选 TOML "
            "只写材料（附录 C）")
    if unknown:
        raise CandidateError(
            f"{source}：未知顶层字段 {unknown}（附录 C 字段齐了才可信，"
            f"拼写错误静默丢约束——全部拒绝）；合法字段 {list(_TOP_KEYS)}")

    cid = _require_str(data, "id", source)
    if not _ID_PATTERN.match(cid):
        raise CandidateError(
            f"{source}：id 必须形如 C001（C + 至少三位数字），收到 {cid!r}")

    title = _require_str(data, "title", source)
    mechanism_class = _require_str(data, "mechanism_class", source)
    if mechanism_class not in MECHANISM_CLASSES:
        raise CandidateError(
            f"{source}：mechanism_class {mechanism_class!r} 不在七类枚举 "
            f"{list(MECHANISM_CLASSES)} 内（蓝图 v4 §6.2：机制空缺按类别判断）")

    status = _require_str(data, "status", source)
    if status not in cards.CANDIDATE_STATUSES:
        raise CandidateError(
            f"{source}：status {status!r} 不在枚举 {list(cards.CANDIDATE_STATUSES)}"
            " 内（附录 C + T61 的 specimen）")

    symbols = data.get("symbols")
    if (not isinstance(symbols, list) or not symbols
            or not all(isinstance(s, str) and s.strip() for s in symbols)):
        raise CandidateError(
            f"{source}：symbols 必须是非空字符串数组，收到 {symbols!r}")

    holding = _require_str(data, "holding", source).upper()
    if holding not in TIMEFRAME_SECONDS:
        raise CandidateError(
            f"{source}：holding {holding!r} 不在 TIMEFRAME_SECONDS 内"
            "（M1/H1/D1 等；附录 C 持仓周期）")

    mechanism = _require_str(data, "mechanism", source)
    source_card = data.get("source_card", "")
    if not isinstance(source_card, str):
        raise CandidateError(
            f"{source}：source_card 必须是字符串，收到 {source_card!r}")
    notes = data.get("notes", "")
    if not isinstance(notes, str):
        raise CandidateError(f"{source}：notes 必须是字符串，收到 {notes!r}")
    observed_in_sample = data.get("observed_in_sample", "")
    if not isinstance(observed_in_sample, str):
        raise CandidateError(
            f"{source}：observed_in_sample 必须是字符串（T84/K3：候选机制"
            f"在本项目样本内被看过时如实登记来源，如 \"T66 §3.2\"），收到"
            f" {observed_in_sample!r}")
    continues_family = str(data.get("continues_family", "") or "").strip()
    if continues_family and not _FAMILY_PATTERN.match(continues_family):
        raise CandidateError(
            f"{source}：continues_family 必须是谱系 id（形如 H012——不带"
            f"-v 版本后缀，那是卡 id 不是谱系；T85/K1：主检验 N 的 lineage"
            f"成分按 ledger.lineage_n_dedup(谱系) 取数），收到 "
            f"{continues_family!r}")

    literature = _validate_literature(data.get("literature"), source)
    declared = None
    if data.get("declared") is not None:
        declared = _validate_declared(data["declared"], source)
    prob_declared = None
    if data.get("prob") is not None:
        prob_declared = _validate_prob(data["prob"], source, tuple(symbols))
    if status in RESEARCH_STATUSES and declared is None             and mechanism_class != "price_levels":
        raise CandidateError(
            f"{source}：status={status} 的研究候选必须带 [declared] 数值声明"
            "（每年事件/交易数、每笔折算后毛收益、事件窗 bar 数"
            "（window_bars，T84/K4）、入出场小时、计费夜数、折算后预期"
            "夏普——预筛的计算输入；折算依据写在 "
            "[literature].expected_effect，不得取自本项目样本内，铁律 17）")
    if mechanism_class == "price_levels" and status in RESEARCH_STATUSES:
        if declared is not None:
            raise CandidateError(
                f"{source}：price_levels 候选不带 [declared]（H 线口径），"
                "概率模式的事前声明在 [prob]（T76/蓝图 P 线附录 A）")
        if prob_declared is None:
            raise CandidateError(
                f"{source}：price_levels 候选必须带 [prob] 段"
                "（situations/hypotheses/bracket/level_grid/approach/"
                "breakout_atr/retest_window_bars/expected_delta_p——"
                "T76/蓝图 P 线附录 A）")
    return Candidate(
        id=cid, title=title, mechanism_class=mechanism_class, status=status,
        symbols=tuple(symbols), holding=holding, mechanism=mechanism,
        literature=literature, source_card=source_card, notes=notes,
        observed_in_sample=observed_in_sample,
        continues_family=continues_family,
        declared=declared, prob_declared=prob_declared)


def load_candidate(path) -> Candidate:
    """读候选登记（tomllib）+ 附录 C 校验；文件名主干必须与 id 一致。"""
    p = Path(path)
    if not p.is_file():
        raise CandidateError(f"候选登记文件不存在：{p}")
    try:
        with open(p, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise CandidateError(f"{p}：TOML 解析失败：{e}") from e
    cand = validate_candidate(data, source=str(p))
    if p.stem != cand.id:
        raise CandidateError(
            f"{p}：文件名主干 {p.stem!r} 与候选 id {cand.id!r} 不一致"
            f"（附录 C 约定文件名为 {cand.id}.toml）")
    return Candidate(
        id=cand.id, title=cand.title, mechanism_class=cand.mechanism_class,
        status=cand.status, symbols=cand.symbols, holding=cand.holding,
        mechanism=cand.mechanism, literature=cand.literature,
        source_card=cand.source_card, notes=cand.notes,
        observed_in_sample=cand.observed_in_sample,
        continues_family=cand.continues_family,
        declared=cand.declared, prob_declared=cand.prob_declared,
        path=str(p))


# ---------------------------------------------------------------- 覆盖年数

def _span_years(first: pd.Timestamp, last: pd.Timestamp) -> float:
    return ((last - first).total_seconds()
            / (_DAYS_PER_YEAR * 86400.0))


# T94/R14-L3：长于 30 天的内部空洞才切开（周末/节假日/换日空档远小于
# 此；EURUSD 主库 27 天服务器缺口也不属于"年数级"空洞，不切）
_GAP_SPLIT = pd.Timedelta(days=30)


def _observed_gaps(times) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """bar 时刻序列里 >30 天的相邻间隔 → (末根现存 bar, 空洞后首根 bar)。

    差分用 pandas Timedelta 比较（单位安全：pandas 3 的 DatetimeIndex
    底层分辨率随来源可为 us/ns，asi8 原始整数不可直接比 ns 阈值）。
    """
    t = pd.DatetimeIndex(times)
    if len(t) < 2:
        return []
    diffs = pd.Series(t).diff()
    idx = np.nonzero((diffs > _GAP_SPLIT).to_numpy())[0]
    return [(t[i - 1], t[i]) for i in idx]


def _manifest_gap_intervals(manifest: dict
                            ) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """ext manifest 里的空洞依据（T94/R14-L3）：

    - ``yearly_verification`` 中 used=False 的年份 → [Y-01-01, Y+1-01-01)
      （histdata 逐年闸门拒用的整年，数据不在场或未启用都如实切开）；
    - ``known_gaps``（若有）→ 登记过的长缺口。
    """
    out: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    yv = manifest.get("yearly_verification")
    if isinstance(yv, dict):
        for y, rec in yv.items():
            if isinstance(rec, dict) and rec.get("used") is False:
                try:
                    y0 = int(str(y))
                except (TypeError, ValueError):
                    continue
                out.append((pd.Timestamp(y0, 1, 1),
                            pd.Timestamp(y0 + 1, 1, 1)))
    kg = manifest.get("known_gaps")
    if isinstance(kg, list):
        for g in kg:
            if isinstance(g, dict) and g.get("start") and g.get("end"):
                try:
                    s, e = pd.Timestamp(g["start"]), pd.Timestamp(g["end"])
                except ValueError:
                    continue
                if e > s:
                    out.append((s, e))
    return out


def _split_span(first: pd.Timestamp, last: pd.Timestamp,
                gaps: list[tuple[pd.Timestamp, pd.Timestamp]]
                ) -> tuple[list[tuple[pd.Timestamp, pd.Timestamp]],
                           list[tuple[pd.Timestamp, pd.Timestamp]]]:
    """[first, last] 在长于 30 天的空洞处切开（T94/R14-L3）。

    空洞与段边界相交时按截断处理（段首/段尾本就不计空洞外的时间）。
    返回 (切出的连续子区间, 实际切开的空洞区间)——空洞边界随段记录
    写进 precheck JSON，切分依据可复核。
    """
    kept = [(first, last)]
    used: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for s, e in sorted(set(gaps)):
        s = max(pd.Timestamp(s), first)
        e = min(pd.Timestamp(e), last)
        if e - s <= _GAP_SPLIT:
            continue
        nxt: list[tuple[pd.Timestamp, pd.Timestamp]] = []
        for a, b in kept:
            if e <= a or s >= b:
                nxt.append((a, b))
                continue
            if a < s:
                nxt.append((a, s))
            if e < b:
                nxt.append((e, b))
        if nxt != kept:
            used.append((s, e))
        kept = nxt
        if not kept:
            break
    return kept, used


def _segments_for_span(source: str, first: pd.Timestamp, last: pd.Timestamp,
                       gaps: list[tuple[pd.Timestamp, pd.Timestamp]]
                       ) -> list[dict]:
    """一个序列（主段或一个 ext 序列）的段记录：在空洞处切开后逐段给出。"""
    spans, used = _split_span(first, last, gaps)
    gap_note = [{"start": s.isoformat(), "end": e.isoformat()}
                for s, e in used]
    out = []
    for a, b in spans:
        seg = {"source": source, "first": a.isoformat(),
               "last": b.isoformat(), "years": _span_years(a, b),
               "counted": True}
        if gap_note:
            seg["gaps"] = gap_note
            seg["note"] = (f"内部空洞切分（T94/R14-L3：{len(used)} 处 "
                           ">30 天空洞，边界见 gaps）")
        out.append(seg)
    return out


def coverage_segments(data_root, symbols, timeframes) -> dict[str, list[dict]]:
    """研究库按 品种×周期 的覆盖分段（T80/G3）——主段（bars/，MT5 导出）
    + 已批准外部序列（{root}/ext/<来源>/<品种>/<周期>/）。

    每段：{"source", "first", "last", "years", "counted"}。counted=False
    的段**不计入**年数（未批准来源在场也如实列出——D36 白名单外的数据
    不得进研究口径）。主段优先读序列 meta.json 水位（first_time /
    last_closed_time），缺 meta 时读 parquet 的 time 列；ext 段经
    mt5data.ext.load_ext_series 读取（manifest 完整性 fail-fast），来源
    批准状态查 mt5data.ext.approvals.SOURCE_APPROVALS。没有数据的序列
    不出现在结果里（调用方按缺失处理）。不同来源之间的口径差异只记录
    （source 标识），不折算。

    T94/R14-L3：每个序列在**长于 30 天的内部空洞**处切开后再进入并集
    （空洞依据：parquet 实测相邻 bar 间隔、meta/manifest 的 known_gaps、
    ext manifest yearly_verification 未启用年份）——首尾跨度会把空洞虚
    计成年数（审核 L3：histdata EURUSD 2013→2022 缺 2014/2019 两年仍按
    9.99 年计，把 C002 的检验力抬过 50% 门槛）。切开的段带 gaps 边界。
    """
    from mt5data.ext.approvals import SOURCE_APPROVALS
    from mt5data.ext.core import ExtDataError, load_ext_series

    root = Path(data_root)
    out: dict[str, list[dict]] = {}
    for sym in symbols:
        for tf in timeframes:
            tf_u = tf.upper()
            key = f"{sym}/{tf_u}"
            segs: list[dict] = []
            first = last = None
            known: list[tuple[pd.Timestamp, pd.Timestamp]] = []
            meta_p = root / "bars" / str(sym) / tf_u / "meta.json"
            if meta_p.is_file():
                try:
                    wm = json.loads(meta_p.read_text(encoding="utf-8"))
                    first = pd.Timestamp(wm["first_time"])
                    last = pd.Timestamp(wm["last_closed_time"])
                except (ValueError, KeyError, TypeError):
                    first = last = None
                raw_kg = wm.get("known_gaps") if isinstance(wm, dict) else None
                if isinstance(raw_kg, list):
                    for g in raw_kg:
                        if isinstance(g, dict) and g.get("start") \
                                and g.get("end"):
                            try:
                                s = pd.Timestamp(g["start"])
                                e = pd.Timestamp(g["end"])
                            except ValueError:
                                continue
                            if e > s:
                                known.append((s, e))
            bars = load_bars(str(root), str(sym), tf_u)
            observed: list[tuple[pd.Timestamp, pd.Timestamp]] = []
            if bars is not None and len(bars) >= 2:
                t = pd.to_datetime(bars["time"])
                observed = _observed_gaps(t)
                if first is None or last is None or last <= first:
                    first, last = t.iloc[0], t.iloc[-1]
            if first is not None and last is not None and last > first:
                segs.extend(_segments_for_span(
                    "mt5", first, last, observed + known))
            ext_root = root / "ext"
            if ext_root.is_dir():
                for src_dir in sorted(ext_root.iterdir()):
                    if not src_dir.is_dir():
                        continue
                    approved = bool(
                        SOURCE_APPROVALS.get(src_dir.name, {})
                        .get("approved"))
                    seg_dir = src_dir / str(sym) / tf_u
                    if not (seg_dir / "data.parquet").is_file():
                        continue
                    if approved:
                        try:
                            loaded = load_ext_series(
                                str(ext_root), src_dir.name, str(sym), tf_u)
                        except ExtDataError as e:
                            segs.append({
                                "source": src_dir.name, "counted": False,
                                "years": 0.0,
                                "note": f"序列不可读（{e}）——不计入"})
                            continue
                        if loaded is None:
                            continue
                        df, _manifest = loaded
                        t = pd.to_datetime(df["time"])
                        e_first, e_last = t.iloc[0], t.iloc[-1]
                        if e_last > e_first:
                            gaps = (_observed_gaps(t)
                                    + _manifest_gap_intervals(_manifest))
                            segs.extend(_segments_for_span(
                                src_dir.name, e_first, e_last, gaps))
                    else:
                        # 未批准来源（如 stooq/yahoo，批准表
                        # mt5data.ext.approvals；histdata 已按 R13 §5
                        # 有条件批准）：在场、如实列出，但不计入年数
                        try:
                            t = pd.to_datetime(pd.read_parquet(
                                seg_dir / "data.parquet")["time"])
                            segs.append({
                                "source": src_dir.name,
                                "first": t.iloc[0].isoformat(),
                                "last": t.iloc[-1].isoformat(),
                                "years": _span_years(t.iloc[0], t.iloc[-1]),
                                "counted": False,
                                "note": "未批准来源（D36 白名单外）——"
                                        "不计入研究年数"})
                        except Exception:
                            segs.append({
                                "source": src_dir.name, "counted": False,
                                "years": 0.0,
                                "note": "未批准来源且不可读——不计入"})
            if segs:
                out[key] = segs
    return out


def merged_coverage_years(segments: list[dict]) -> float:
    """计入段的**日历区间并集**年数（T80/G3 审核修正：重叠不双计）。

    独立审核发现：直接把各段年数相加会在段重叠时虚增覆盖（真实库
    USTEC：fred 39.8 年完全包住 mt5 4.3 年 → 相加 44.1 年、并集 39.8
    年；BTCUSD 部分重叠虚增 40%，足以把 waiting_data 翻成 pass）。按
    计划 T80"按序列取最长连续覆盖"的意图，计入段按 [first, last] 做区间
    并集：相接/重叠的段合成一段（取 min(first)..max(last) 的连续跨度），
    不相接的分段各自计入、跨度累加——缺口段自然被排除在并集之外。
    counted=False 的段（未批准来源）不参与。

    T94/R14-L3：段在产出侧已在 >30 天内部空洞处切开（coverage_segments
    逐序列切分），并集前不再含首尾跨度虚计的空洞（histdata EURUSD 缺
    2014/2019 两年按 8 年计、不是 9.99 年）。
    """
    intervals = sorted(
        (pd.Timestamp(s["first"]), pd.Timestamp(s["last"]))
        for s in segments if s.get("counted"))
    merged: list[list[pd.Timestamp]] = []
    for a, b in intervals:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return float(sum((b - a).total_seconds() for a, b in merged)
                 / (_DAYS_PER_YEAR * 86400.0))


def coverage_years(data_root, symbols, timeframes) -> dict[str, float]:
    """研究库按 品种×周期 的实际覆盖年数（T80/G3：主段 + 已批准外部序列
    的**日历区间并集**——重叠不双计；未批准来源不计；coverage_segments
    逐段口径、merged_coverage_years 合并口径）。

    键为 "SYMBOL/TF"（precheck 的 research_years 口径）；与实测密度年化
    配套（report.observed_bars_per_year 同一思想）：年化夏普的分母年数
    取跨度，检验力（Lo 近似）才与台账里的夏普同口径。
    """
    return {key: merged_coverage_years(segs) for key, segs in
            coverage_segments(data_root, symbols, timeframes).items()}


# ------------------------------------------------- 无条件收益波动（T80/G2）

def unconditional_volatility(data_root, symbol: str, timeframe: str, *,
                             window_bars: int = 1,
                             entry_hour_ny: int | None = None) -> float | None:
    """品种在事件窗长度上的无条件收益波动 σ（相对价格，随机时点口径）。

    T84/K4：σ = **同长度窗口**收益的标准差——window_bars 根持仓周期 bar
    的滚动窗口收益全体（ddof=1），与 T66 的 σ_w 共用同一个函数
    （mt5quant.validation.events.window_volatility；随机时点抽样的总体
    极限：随机等权抽取入场时点、持有 window_bars 根 bar 的收益分布，
    即品种本身的性质，与候选信号无关、不是样本内结果）。单根 bar 口径
    会少算 √h（C001 的 4 日窗差约一半、隐含夏普多算约一倍——K4）。

    entry_hour_ny：日内周期（< D1）按入场小时取**同一时段起点**的窗口
    （窗口起点 = 锚 bar 收盘时刻；时段异方差：定盘前一小时 ≠ 全天平均
    ——C002 的 K4 更正）；D1 及以上周期全天只有一种起点，不传（传了
    也按 None 处理）。用全体而非抽样的原因：确定性、无种子，precheck
    结果逐位可复算。

    序列缺失/数据不足/时段窗口不足 → None；恒价序列 → 0.0（隐含夏普
    无从折算，调用方按声明夏普计并如实记录）。
    """
    from mt5quant.validation.events import window_volatility

    bars = load_bars(str(data_root), str(symbol), timeframe.upper())
    if bars is None or len(bars) < 3:
        return None
    intraday = (TIMEFRAME_SECONDS[timeframe.upper()]
                < TIMEFRAME_SECONDS["D1"])
    try:
        return float(window_volatility(
            bars, int(window_bars),
            entry_hour_ny=(int(entry_hour_ny)
                           if intraday and entry_hour_ny is not None
                           else None)))
    except ValueError:
        return None


# ---------------------------------------------------------------- 预筛

def _contract_size(data_root, symbol: str) -> float:
    """{data_root}/specs/latest.json 的合约大小（与 run_backtest 同规：缺则报错）。"""
    p = Path(data_root) / "specs" / "latest.json"
    if not p.is_file():
        raise CandidateError(
            f"{symbol} 合约大小未知：缺 {p}——先在数据根侧跑 "
            "scripts/snapshot_specs.py（预筛的事前成本需要把每手金额折成"
            "相对价格）")
    snap = json.loads(p.read_text(encoding="utf-8"))
    spec = snap.get(symbol) if isinstance(snap, dict) else None
    cs = spec.get("contract_size") if isinstance(spec, dict) else None
    if not isinstance(cs, (int, float)) or not cs > 0:
        raise CandidateError(
            f"{symbol} 的快照缺 contract_size（{p}）——预筛的事前成本"
            "无从折算，重跑 scripts/snapshot_specs.py")
    return float(cs)


def _reference_price(data_root, symbol: str, timeframe: str) -> tuple[float, str]:
    """参考价：研究库该品种最后一根收盘价（金额→相对价格的折算基准）。

    优先取候选持仓周期；该周期缺序列时退回该品种任一可用周期（参考价
    只用于把每手金额成本折成相对价格，与周期无关）。返回 (价格, 所用周期)。
    """
    bars = load_bars(str(data_root), symbol, timeframe)
    tf_used = timeframe
    if bars is None or len(bars) == 0:
        sym_dir = Path(data_root) / "bars" / str(symbol)
        tf_used = ""
        if sym_dir.is_dir():
            for tdir in sorted(sym_dir.iterdir()):
                if tdir.is_dir():
                    bars = load_bars(str(data_root), symbol, tdir.name)
                    if bars is not None and len(bars) > 0:
                        tf_used = tdir.name.upper()
                        break
    if not tf_used or bars is None or len(bars) == 0:
        raise CandidateError(
            f"研究库无 {symbol} 任何周期的 K 线——参考价（事前成本的金额→"
            "相对价格折算基准）无从取；检查数据根或先导出研究库")
    return float(pd.to_numeric(bars["close"]).iloc[-1]), tf_used


def _ex_ante_cost_per_symbol(candidate, symbol, *, data_root, cost_model,
                             declared) -> dict:
    """单品种的来回成本（相对价格口径）与事前成本倍数。

    与引擎 A/G1 同一口径（铁律：成本模型只有一份语义）：
    - 点差 p90 是**来回点差全幅、每来回恰收一次**（多头计入场腿、空头计
      出场腿——方向未定时取入/出场两小时中较不利的一侧，保守）；
    - 滑点开、平两侧各扣一次；
    - 佣金为每手来回佣金，除以 1 手合约价值折成相对价格；
    - 隔夜费取 long/short 每夜费率中更不利的一侧 × 声明计费夜数（保守，
      蓝图 §6.2 条件 3"按快照隔夜费保守计入"）。
    spread_p90/slippage 恒返回价格单位（相对模型内部已 ×price），统一按
    参考价折成相对价格。
    """
    cs = _contract_size(data_root, symbol)
    ref, ref_tf = _reference_price(data_root, symbol, candidate.holding)

    def _frac(price_units: float) -> float:
        """价格单位 → 相对价格（spread_p90/slippage 两种口径都返回价格单位）。"""
        return price_units / ref

    spr_e = cost_model.spread_p90(symbol, declared.entry_hour_ny, price=ref)
    spr_x = cost_model.spread_p90(symbol, declared.exit_hour_ny, price=ref)
    slip_e = cost_model.slippage(symbol, declared.entry_hour_ny, price=ref)
    slip_x = cost_model.slippage(symbol, declared.exit_hour_ny, price=ref)
    spr_frac = _frac(spr_e if spr_e >= spr_x else spr_x)   # 单次收费，取不利侧
    notional_per_lot = cs * ref            # 1 手的合约价值（账户货币）
    commission = cost_model.commission_per_lot(symbol) / notional_per_lot
    overnight = 0.0
    if declared.overnight_nights > 0:
        swap = getattr(cost_model, "swap", None)
        if swap is None:
            raise CandidateError(
                f"{symbol} 的成本模型缺隔夜费规则（swap）——持仓过夜的候选"
                "按快照隔夜费保守计入成本（蓝图 §6.2 条件 3），不能静默按 0")
        rule = swap.rule(symbol)
        worst = max(abs(float(rule.long_per_day)),
                    abs(float(rule.short_per_day)))
        overnight = declared.overnight_nights * worst / notional_per_lot
    cost = (spr_frac + _frac(slip_e) + _frac(slip_x)
            + commission + overnight)
    return {
        "reference_price": ref, "reference_timeframe": ref_tf,
        "contract_size": cs,
        "spread_round_trip": spr_frac,
        "spread_entry_frac": _frac(spr_e), "spread_exit_frac": _frac(spr_x),
        "slippage_entry": _frac(slip_e), "slippage_exit": _frac(slip_x),
        "commission": commission, "overnight": overnight,
        "round_trip_cost": cost,
        "multiple": declared.gross_return_per_event / cost,
    }


def precheck(candidate: Candidate, *, data_root, cost_model,
             research_years: dict[str, float],
             active_mechanisms: set[str] | frozenset[str] | None = None,
             research_coverage_segments: dict[str, list[dict]] | None = None
             ) -> PrecheckResult:
    """预筛五条件（蓝图 §6.2）→ pass / fail / waiting_data 与理由。

    - data_root：数据根（合约大小与参考价取自 specs/K 线）；
    - cost_model：成本模型 v3（Costs 聚合体；预筛只信缓存文件模型）；
    - research_years：品种×周期覆盖年数（coverage_years 的产出，键
      "SYMBOL/TF"，T80/G3 起含已批准外部序列）——检验力用它，不用常数；
    - active_mechanisms：组合内已有活跃谱系的机制类别集合（可选；调用方
      扫候选登记 status∈{in_round, screened_pass} 得到）——机制空缺是
      排序类理由，不阻塞（决定只有三值）；
    - research_coverage_segments：coverage_segments 的产出（可选；T80/G3
      逐段来源与年数随 power 检查写进 precheck JSON——不同来源口径差异
      只记录不折算）。
    """
    if candidate.status in BOOKKEEPING_STATUSES:
        what = ("撤回登记" if candidate.status == cards.WITHDRAWN
                else "标定样品")
        return PrecheckResult(
            candidate_id=candidate.id, decision="fail",
            reasons=(f"status={candidate.status}：{what}（T61/D30 簿记），"
                     "不是研究候选，不进入预筛与轮次",),
            checks={}, events_per_year=None)

    declared = candidate.declared
    assert declared is not None          # 装载层已保证研究状态必带
    checks: dict[str, dict] = {}
    reasons: list[str] = []

    # 条件 1：文献（结构由装载器保证；此处复核，供绕过装载器直接构造者）
    lit = candidate.literature
    lit_ok = bool(lit.original) and bool(lit.search_log.strip()) \
        and bool(lit.expected_effect.strip())
    checks["literature"] = {
        "passed": lit_ok,
        "original_n": len(lit.original),
        "replications_n": len(lit.replications),
        "rebuttals_n": len(lit.rebuttals),
        "note": ("无复现——预期按发表后 −58% 折算（McLean & Pontiff 2016），"
                 "折算依据须写在 expected_effect"
                 if not lit.replications else "复现与折算依据见 expected_effect"),
    }
    if not lit_ok:
        reasons.append("文献条件不满足：original/search_log/expected_effect "
                       "有缺（预筛条件 1）")

    # 条件 2：持仓周期 ≥ H1
    h_seconds = TIMEFRAME_SECONDS[candidate.holding]
    min_seconds = TIMEFRAME_SECONDS[_HOLDING_MIN]
    holding_ok = h_seconds >= min_seconds
    checks["holding"] = {
        "passed": holding_ok, "holding": candidate.holding,
        "seconds": int(h_seconds), "min": _HOLDING_MIN,
        "min_seconds": int(min_seconds)}
    if not holding_ok:
        reasons.append(f"持仓周期 {candidate.holding} < {_HOLDING_MIN}"
                       "（预筛条件 2：信号周期低于 H1 的候选不做）")

    # 条件 3：事前成本倍数 ≥ 3（按品种算，取最紧的品种）
    per_symbol = {sym: _ex_ante_cost_per_symbol(
        candidate, sym, data_root=data_root, cost_model=cost_model,
        declared=declared) for sym in candidate.symbols}
    multiple = min(d["multiple"] for d in per_symbol.values())
    worst_symbol = min(per_symbol,
                       key=lambda s: per_symbol[s]["multiple"])
    cost_ok = multiple >= _COST_MULTIPLE_MIN
    checks["ex_ante_cost"] = {
        "passed": cost_ok, "multiple": multiple,
        "min": _COST_MULTIPLE_MIN,
        "gross_return_per_event": declared.gross_return_per_event,
        "binding_symbol": worst_symbol, "per_symbol": per_symbol}
    if not cost_ok:
        reasons.append(
            f"事前成本倍数 {multiple:.2f} < {_COST_MULTIPLE_MIN:g}"
            f"（{worst_symbol}：折算后每笔毛收益 "
            f"{declared.gross_return_per_event:g} ÷ 来回成本 "
            f"{per_symbol[worst_symbol]['round_trip_cost']:g}，预筛条件 3）")

    # 条件 4：检验力（单次检验；铁律 18——不足进"待数据"，不是 fail）。
    # T80/G2：夏普取声明与隐含中较小者——隐含夏普 =（每事件毛收益 − 事前
    # 来回成本）÷ σ × √每年事件数；σ 是品种性质（随机时点口径，与信号
    # 无关），乐观的夏普声明过不了"毛收益−成本按波动折算"这一关。
    # T84/K4：σ 按声明的 window_bars 事件窗取（与 T66 σ_w 同一函数），
    # 日内周期再按 entry_hour_ny 取同一时段起点——单根 bar 口径会少算
    # √h、时段平均会盖掉定盘前一小时这类时段异方差。
    years_detail = {f"{s}/{candidate.holding}":
                    float(research_years.get(f"{s}/{candidate.holding}", 0.0))
                    for s in candidate.symbols}
    years = min(years_detail.values())
    missing = [k for k, v in years_detail.items() if v <= 0.0]
    sigma_by_symbol: dict[str, float | None] = {}
    implied_by_symbol: dict[str, float | None] = {}
    for s in candidate.symbols:
        sigma = unconditional_volatility(
            data_root, s, candidate.holding,
            window_bars=declared.window_bars,
            entry_hour_ny=declared.entry_hour_ny)
        sigma_by_symbol[s] = sigma
        if sigma is None or sigma <= 0.0:
            implied_by_symbol[s] = None      # σ 缺失/恒价 → 无从折算
        else:
            net_per_event = (declared.gross_return_per_event
                             - per_symbol[s]["round_trip_cost"])
            implied_by_symbol[s] = (net_per_event / sigma
                                    * math.sqrt(declared.events_per_year))
    known = [v for v in implied_by_symbol.values() if v is not None]
    implied_worst = min(known) if known else None
    sharpe_used = (declared.expected_sharpe if implied_worst is None
                   else min(declared.expected_sharpe, implied_worst))
    if years <= 0.0:
        pw = 0.0
    else:
        pw = power.power_one_sided(sharpe_used, years,
                                   alpha=_POWER_ALPHA,
                                   n_tests=_POWER_N_TESTS)
    power_ok = pw >= _POWER_MIN
    checks["power"] = {
        "passed": power_ok, "power": float(pw), "min": _POWER_MIN,
        "years": years, "expected_sharpe": declared.expected_sharpe,
        "implied_sharpe": (None if implied_worst is None
                           else float(implied_worst)),
        "sharpe_used": float(sharpe_used),
        "implied_sharpe_by_symbol": implied_by_symbol,
        "sigma_by_symbol": sigma_by_symbol,
        "sigma_window_bars": declared.window_bars,
        "sigma_entry_hour_ny": (
            declared.entry_hour_ny
            if TIMEFRAME_SECONDS[candidate.holding]
            < TIMEFRAME_SECONDS["D1"] else None),
        "sigma_method": (
            "无条件收益波动 σ：候选声明 window_bars 根持仓周期 bar 的滚动"
            "窗口收益全体（随机时点抽样的总体极限，ddof=1，与 T66 σ_w 同一"
            "函数 validation.events.window_volatility——T84/K4：单根 bar 口"
            "径少算 √h）；日内周期按 entry_hour_ny 取同一时段起点（窗口"
            "起点=锚 bar 收盘时刻）；σ 是品种性质，与候选信号无关、不取自"
            "本项目样本内（T80/G2）。隐含夏普 =（毛收益−事前来回成本）÷σ"
            "×√每年事件数，检验力取 min(声明, 隐含)"),
        "alpha": _POWER_ALPHA, "n_tests": _POWER_N_TESTS,
        "years_detail": years_detail,
        "min_detectable_sharpe": (
            power.min_detectable_sharpe(years, alpha=_POWER_ALPHA,
                                        n_tests=_POWER_N_TESTS)
            if years > 0.0 else None)}
    if research_coverage_segments is not None:
        # T80/G3：逐段列来源与年数（未批准来源 counted=False 如实列出）
        # + 合并后总覆盖（区间并集，从逐段独立可复算）与口径注明
        checks["power"]["coverage_segments"] = {
            k: v for k, v in research_coverage_segments.items()
            if k in years_detail}
        checks["power"]["coverage_merged_years"] = {
            k: merged_coverage_years(v)
            for k, v in research_coverage_segments.items()
            if k in years_detail}
        checks["power"]["coverage_merge_method"] = (
            "计入段日历区间并集：相接/重叠的段合成连续跨度（min(first).."
            "max(last)），不相接的分段各自计入——重叠不双计（T80/G3 审核"
            "修正：相加会把完全包含/部分重叠的覆盖虚增，如 fred/USTEC "
            "39.8 年包住 mt5 4.3 年 → 合并 39.8 而非 44.1）；每段在产出侧"
            "已在 >30 天内部空洞处切开（T94/R14-L3：实测间隔+known_gaps+"
            "manifest 未启用年份，边界随段的 gaps 字段）")
    if not power_ok:
        if missing:
            reasons.append(
                f"研究库无覆盖 {missing}——检验力无从计算 → 待数据"
                "（铁律 18：延长研究段（T67）后重检）")
        elif implied_worst is not None \
                and implied_worst < declared.expected_sharpe:
            reasons.append(
                f"检验力 {pw:.0%} < {_POWER_MIN:.0%}（{years:.2f} 年、"
                f"隐含夏普 {implied_worst:.2f} < 声明 "
                f"{declared.expected_sharpe:g}——按较小者计，T80/G2："
                "毛收益−成本按品种波动 σ 折算到不了声明的夏普）→ 待数据"
                "（铁律 18：检验力不足不开卡）")
        else:
            reasons.append(
                f"检验力 {pw:.0%} < {_POWER_MIN:.0%}（{years:.2f} 年、折算"
                f"夏普 {sharpe_used:g}）→ 待数据（铁律 18："
                "检验力不足不开卡，延长研究段（T67）后重检）")

    # 条件 5：机制空缺（排序类理由，不阻塞）
    active = set(active_mechanisms or ())
    if candidate.mechanism_class in active:
        gap_note = (f"机制类别 {candidate.mechanism_class} 已有活跃谱系——"
                    "排后（蓝图 §6.2 条件 5：同类排在后面，排序类理由不阻塞）")
    else:
        gap_note = (f"机制空缺：{candidate.mechanism_class} 是组合里还没有的"
                    "类别（蓝图 §6.2 条件 5）")
    checks["mechanism_gap"] = {"blocking": False, "note": gap_note,
                               "mechanism_class": candidate.mechanism_class,
                               "active_mechanisms": sorted(active)}
    reasons.append(gap_note)

    # 判定块（K10/R13 还原 0d92448 之前写法）：`not cost_ok` 判 fail——
    # 成本链路返回 numpy 布尔值（np.False_ is False 为假）或成本无法计算
    # （None）时，`cost_ok is False` 会漏判/改判 waiting_data；`not`
    # 对两者都成立（成本不过/算不出都不是"待数据"）。P 线 precheck_prob
    # 的 cost_ok=None（无触发计数）语义不同，保留其三分支写法。
    if not lit_ok or not holding_ok or not cost_ok:
        decision = "fail"
    elif not power_ok:
        decision = "waiting_data"
    else:
        decision = "pass"
    return PrecheckResult(
        candidate_id=candidate.id, decision=decision,
        reasons=tuple(reasons), checks=checks,
        events_per_year=declared.events_per_year)




# ------------------------------------------------------- P 线预筛（T76/D45）

_PROB_BASE_RATE = 0.40        # 蓝图 P 线 §3.4：基准赢率口径（止盈 2R/止损 1R）

# ----------------------------- T75 实测样本量规则（T76 收尾接线，2026-10-29 批准）
# 标定报告 docs/research/prob-mode-calibration-2026-10-29.md §2.5/§3（月内置换
# 方法获批；基准赢率≈0.34 实测口径——解析表用 0.40，实测更保守）的单假设
# 实测锚点（单位=触发日数，单假设/单侧 5%/50% 检验力）：
#   Δp=0.02 → ">4,000"（开区间：n=4000 检验力仅 34%，精确下界未测出）；
#   Δp=0.03 → ≈2,000（53%）；Δp=0.05 → ≈1,000（70%）
# 规则（报告 §3 + 计划 T76 修订 v2）：
# - dp < 0.02：None——低于标定最小档，未测出（恒 waiting_data，不外推）；
# - 0.02 ≤ dp ≤ 0.03：4000@0.02 ↔ 2000@0.03 线性插值，open_ended=True
#   （0.02 端继承开区间语义：判 pass 需 days **>** n_required）；
# - 0.03 < dp ≤ 0.05：2000@0.03 ↔ 1000@0.05 线性插值（闭区间，≥ 即过）；
# - dp > 0.05：沿 0.03→0.05 段斜率**外推**（非 clamp），max(值, 0) 截断
#   （dp≳0.07 后外推到 0、解析 D52 主导——由调用方的 max 取严兜底，如实注明）。
# 假设数维度（审核推荐 z² 换算，任意 n_hyp ≥ 1；即 T75 报告 §3 的 4/12 列
# 推导式——(z₄/z₁)²≈1.856、(z₁₂/z₁)²≈2.572，z 取 power._z_alpha(0.05, n_hyp)
# 与解析 n_needed_proportion 同一取位约定）：
#   n_required_measured(n_hyp, dp) = 单假设值(dp) × (z_α(n_hyp)/z_α(1))²
# 实现按推导式而非报告取整表（3700/5100/7400 等——相差 ≤3%），常量注释写明。
# 数值自检（tests/test_prob_precheck.py::test_measured_rule_numeric_self_checks）：
#   4 假设 Δp=0.03 → 2000×1.856 = 3712；2 假设 Δp=0.04 → 单假设 1500×
#   (1.960/1.645)² = 2130；12 假设 Δp=0.05 → 1000×2.572 = 2572。

# 单假设实测锚点：(Δp, n_required)（T75 报告 §3 表的"1 假设（实测）"列）
_MEASURED_ANCHORS = ((0.02, 4000.0), (0.03, 2000.0), (0.05, 1000.0))


def measured_n_required(n_hyp: int, dp: float) -> tuple[int | None, bool]:
    """T75 实测样本量规则（单位=触发日数）→ (n_required, open_ended)。

    **P-03 注记（T98 审计 2026-10-29；T100 重跑 2026-10-29 更新）**：
    锚点含义 = **名义**触发日数（有放回按日抽样后再按日聚合，名义 n
    并非独立的实际触发日数——日聚合把同日多触发/重复抽样坍缩，实际
    有效日数小于名义 n）。修正代码（P-02/P-03/P-12）重跑
    `t75-formal-v2` 已完成：非成簇项与旧运行逐值复现一致；成簇负对照
    （P-02 新设计：real 与 null 均独立整周平移）w7 档误报率 11% > 5%
    ——按预写判定标准**未采纳**，锚点维持本表旧值（名义口径，对日数
    消费方偏保守——实测信息天花板 ~1,732 实际日）。信息性实际日数
    推导与升级处置选项见
    `docs/research/prob-mode-calibration-2026-10-29-v2.md`。

    - n_required=None：dp < 0.02 未测出——该假设**恒 waiting_data**，
      不随触发日数增加改变（调用方见 None 即判不过，不与解析取 max）；
    - open_ended=True（0.02 ≤ dp ≤ 0.03）：判 pass 需 days **>** n_required
      （0.02 端 ">4,000" 锚点的开区间语义随插值段继承）；其余档位 ≥ 即过。
    取严（n_required = max(解析 D52, 实测)）由调用方 precheck_prob 做，
    单假设值 × (z_α(n_hyp)/z_α(1))² 在本函数内完成。
    """
    if isinstance(n_hyp, bool) or not isinstance(n_hyp, int) or n_hyp < 1:
        raise ValueError(f"n_hyp 必须是 ≥1 整数，收到 {n_hyp!r}")
    if not (isinstance(dp, (int, float)) and math.isfinite(dp) and dp > 0.0):
        raise ValueError(f"dp 必须为有限正数，收到 {dp!r}")
    (lo_dp, lo_n), (mid_dp, mid_n), (hi_dp, hi_n) = _MEASURED_ANCHORS
    if dp < lo_dp:
        return None, False                  # 未测出：恒 waiting_data
    if dp <= mid_dp:
        a_dp, a_n, b_dp, b_n = lo_dp, lo_n, mid_dp, mid_n
        open_ended = True                   # [0.02, 0.03]：开区间档
    else:
        # (0.03, +∞)：0.03→0.05 段线性插值；>0.05 沿同一斜率外推
        a_dp, a_n, b_dp, b_n = mid_dp, mid_n, hi_dp, hi_n
        open_ended = False
    single = a_n + (dp - a_dp) * (b_n - a_n) / (b_dp - a_dp)
    if dp > hi_dp:                          # 外推区：非负截断 max(值, 0)
        single = max(single, 0.0)
    z1 = power._z_alpha(_POWER_ALPHA, 1)
    z_h = power._z_alpha(_POWER_ALPHA, n_hyp)
    return math.ceil(single * (z_h / z1) ** 2 - 1e-9), open_ended


def precheck_prob(candidate: Candidate, *, data_root, cost_model, counted,
                  split, active_mechanisms=None) -> PrecheckResult:
    """概率模式预筛五条件（计划 T76；蓝图 v4 §6.2 在概率模式上的具体化）。

    输入：
    - counted：计数工具（mt5quant.prob.counting）的发现段子集（时间 <
      split），列含 symbol/situation/action/side/time/ny_hour/atr/close；
      None = 无计数（检验力无从算 → waiting_data）；
    - split：发现段/验证段分界（bt segment propose 的产出，D50）。

    条件（与 H 线 precheck 平行；只做事前计算，不看任何结局）：
    1. 文献：同 H 线；
    2. 决策周期 ≥ H1（candidate.holding）；
    3. 事前成本（审核二 Q1/Q2）：**逐品种**检查 ΔEV = Δp×(tp_R+1) ≥
       3×来回成本(R)——每个品种都要满足（等价于按成本最高的品种判断）。
       成本 = 触发纽约小时的（点差 + 双腿滑点 + 佣金/合约大小）÷
       (sl_atr×ATR) 按触发均值（佣金 commission_per_lot/cs 为价格单位）；
       触发小时分布来自计数（不看结果）；
    4. 检验力（审核二 Q3/Q6，D52）：样本单位为发现段内的**触发日数**
       （同一天多次触发记 1）；Bonferroni 条数 = 候选登记的假设条数；
       **逐条判定**——不足的假设判 waiting_data 并列出（开轮时移出），
       候选至少一条通过才算通过；样本量取解析 D52 与 **T75 实测规则**
       （measured_n_required，月内置换标定 2026-10-29 批准）的**更严者**：
       实测未覆盖（Δp<0.02）→ 恒 waiting_data；开区间档（0.02≤Δp≤0.03）
       判 pass 需 days > n_required（见 checks["power_prob"]）；
    5. 机制空缺：price_levels 是新类别（D45）——排序类理由，不阻塞。
    """
    if candidate.status in BOOKKEEPING_STATUSES:
        return PrecheckResult(
            candidate_id=candidate.id, decision="fail",
            reasons=(f"status={candidate.status}：簿记登记不是研究候选，"
                     "不进入预筛与轮次",),
            checks={}, events_per_year=None)
    pdecl = candidate.prob_declared
    assert pdecl is not None        # price_levels 装载层已保证带 [prob]
    # T97 情景点差（先决 2）：era 判定读 bars meta.json 的 first_time
    from mt5quant.prob.gaps import mt5_first_time

    checks: dict[str, dict] = {}
    reasons: list[str] = []

    # 条件 1：文献（与 H 线同一判据）
    lit = candidate.literature
    lit_ok = bool(lit.original) and bool(lit.search_log.strip()) \
        and bool(lit.expected_effect.strip())
    checks["literature"] = {
        "passed": lit_ok, "original_n": len(lit.original),
        "replications_n": len(lit.replications),
        "rebuttals_n": len(lit.rebuttals)}
    if not lit_ok:
        reasons.append("文献条件不满足：original/search_log/expected_effect "
                       "有缺（预筛条件 1）")

    # 条件 2：决策周期 ≥ H1
    h_seconds = TIMEFRAME_SECONDS[candidate.holding]
    holding_ok = h_seconds >= TIMEFRAME_SECONDS[_HOLDING_MIN]
    checks["holding"] = {"passed": holding_ok, "holding": candidate.holding,
                         "min": _HOLDING_MIN}
    if not holding_ok:
        reasons.append(f"决策周期 {candidate.holding} < {_HOLDING_MIN}"
                       "（预筛条件 2）")

    tp_r = pdecl.bracket["tp_atr"] / pdecl.bracket["sl_atr"]
    edge_ev_r = pdecl.expected_delta_p * (tp_r + 1.0)

    # 条件 3：事前成本（R 口径；审核二 Q1：逐品种检查，每个品种都要满足，
    # 等价于按成本最高的品种判断；Q2：佣金 = commission_per_lot / cs 为价格
    # 单位，再除以 (sl_atr×ATR) 换成 R）。每个条件都给出结果，不短路。
    # T97 情景点差（先决 2）：候选声明 cost_scenario_mult 时按双档计算——
    # era 判定 = 触发时刻 < {data_root}/bars/{品种}/{周期}/meta.json 的
    # first_time → ext 段（precheck 内部读 meta；缺 meta → 无 ext 触发）；
    # ext 触发的点差分别按 1x / 保守档 mult[1] / 参考档计算，
    # cost_r_by_symbol（绑定 passed）用保守档，1x 与参考档（若有）并列
    # 进 checks。缺省（cost_scenario_mult=None）与 T97 之前逐位相同。
    scenario_mult = (float(pdecl.cost_scenario_mult[1])
                     if pdecl.cost_scenario_mult is not None else None)
    scenario_ref = (float(pdecl.cost_scenario_reference)
                    if pdecl.cost_scenario_reference is not None else None)
    per_symbol: dict[str, dict] = {}
    for sym in candidate.symbols:
        rows = None
        if counted is not None:
            rows = counted[counted["symbol"] == sym]
        if rows is None or len(rows) == 0:
            per_symbol[sym] = {"n": 0, "cost_r": None}
            continue
        cs = _contract_size(data_root, sym)
        comm_price = cost_model.commission_per_lot(sym) / cs
        first = (mt5_first_time(data_root, sym, candidate.holding)
                 if scenario_mult is not None else None)
        times = (pd.to_datetime(rows["time"]) if first is not None else None)

        def _cost_r(ext_spread_mult: float) -> float:
            """ext 行点差 ×ext_spread_mult（1.0 = 无情景），均值 R 口径。"""
            vals = []
            for k, r in enumerate(rows.itertuples(index=False)):
                hour, price = int(r.ny_hour), float(r.close)
                spr = cost_model.spread_p90(sym, hour, price=price)
                slip = cost_model.slippage(sym, hour, price=price)
                m = (ext_spread_mult
                     if times is not None and times.iloc[k] < first else 1.0)
                r_price = pdecl.bracket["sl_atr"] * float(r.atr)
                vals.append((m * spr + 2.0 * slip + comm_price) / r_price)
            return float(np.mean(vals))

        entry = {"n": int(len(rows)), "cost_r": _cost_r(1.0)}
        if scenario_mult is not None:
            entry["cost_r_1x"] = entry["cost_r"]
            entry["cost_r"] = _cost_r(scenario_mult)     # 保守档绑定
            entry["ext_n"] = (int((times < first).sum())
                              if times is not None else 0)
            if scenario_ref is not None:
                entry["cost_r_ref"] = _cost_r(scenario_ref)
        per_symbol[sym] = entry
    known = {s: d["cost_r"] for s, d in per_symbol.items()
             if d["cost_r"] is not None}
    cost_ok: bool | None
    if len(known) == len(candidate.symbols):
        # 逐品种判定（Q1）：任一品种 ΔEV < 3×cost → 不通过
        failing = sorted(s for s, v in known.items()
                         if edge_ev_r < _COST_MULTIPLE_MIN * v)
        cost_ok = not failing
        dearest = max(known, key=lambda s: known[s])
        checks["ex_ante_cost_prob"] = {
            "passed": cost_ok, "edge_ev_r": edge_ev_r,
            "expected_delta_p": pdecl.expected_delta_p, "tp_r": tp_r,
            "binding_symbol": dearest,
            "cost_r_by_symbol": {s: float(v) for s, v in known.items()},
            "failing_symbols": failing,
            "min": _COST_MULTIPLE_MIN,
            "formula": "逐品种：ΔEV_R = Δp×(tp_R+1) ≥ 3×来回成本(R) 须对每个"
                       "品种成立（等价于按成本最高品种判断，Q1）；成本 = 触发"
                       "纽约小时（点差+双腿滑点+佣金/合约大小）÷ (sl_atr×ATR)"
                       "的均值（Q2：佣金 commission_per_lot/cs 为价格单位）",
            "note": "已知限制：未计入隔夜费与止损越幅；基准赢率固定为 0.40"
                    "（比按止损止盈推算的 sl/(sl+tp)=1/3 保守）"}
        if scenario_mult is not None:
            # T97 情景点差（先决 2）：cost_r_by_symbol 已是保守档（绑定）；
            # 1x 基准档与非绑定参考档（若有）并列报告，era 规则写明可复核。
            checks["ex_ante_cost_prob"].update({
                "cost_r_by_symbol_1x": {s: float(per_symbol[s]["cost_r_1x"])
                                        for s in known},
                "cost_scenario_mult": [1.0, scenario_mult],
                "ext_trigger_n_by_symbol": {s: int(per_symbol[s]["ext_n"])
                                            for s in known},
                "era_rule": "ext 段触发 = 触发时刻 < {data_root}/bars/"
                            "{品种}/{周期}/meta.json 的 first_time（缺 "
                            "meta → 无 ext 触发）；ext 触发点差 × 情景档"
                            "（其余分量不缩放），判定绑定保守档",
            })
            if scenario_ref is not None:
                checks["ex_ante_cost_prob"]["cost_r_by_symbol_ref3"] = {
                    s: float(per_symbol[s]["cost_r_ref"]) for s in known}
                checks["ex_ante_cost_prob"]["cost_scenario_reference"] = \
                    scenario_ref
        for s in failing:
            reasons.append(
                f"事前成本：ΔEV {edge_ev_r:.4f}R < "
                f"{_COST_MULTIPLE_MIN:g}×{known[s]:.4f}R（{s}——逐品种检查，"
                "预筛条件 3：折算 Δp 覆盖不了该品种的 3 倍成本）")
    else:
        cost_ok = None      # 无计数 → 条件如实给出"无法计算"，不视为通过
        checks["ex_ante_cost_prob"] = {
            "passed": None, "edge_ev_r": edge_ev_r,
            "note": "无触发计数——成本无从按小时分布计算（与检验力同因）"}

    # 条件 4：检验力（审核二 Q3/Q6，D52）——样本单位为发现段内的**触发
    # 日数**（同一天多次触发记 1，与 P1 主检验的按日聚合一致）；Bonferroni
    # 条数 = 候选登记的假设条数；逐条判定：不足的假设机械判 waiting_data
    # 并列出（开轮时从本轮移除，不替换动作、不改方向），候选至少一条通过
    # 才算通过；开轮复核按全轮假设条数再算一遍（T77 用同一函数的全轮口径）。
    # T76 收尾接线：样本量 = max(解析 D52, T75 实测)——实测规则见
    # measured_n_required；实测 None（Δp<0.02 未测出）不参与 max，
    # 该假设恒 waiting_data；开区间档（0.02≤Δp≤0.03）须严格大于。
    n_hyp = len(pdecl.hypotheses)
    n_required = power.n_needed_proportion(
        _PROB_BASE_RATE, pdecl.expected_delta_p,
        alpha=_POWER_ALPHA, power=_POWER_MIN, n_tests=n_hyp)
    n_meas, open_ended = measured_n_required(n_hyp, pdecl.expected_delta_p)
    n_used = (None if n_meas is None
              else max(int(n_required), n_meas))
    per_hypothesis: dict[str, dict] = {}
    insufficient: list[str] = []
    if counted is None or len(counted) == 0:
        power_ok = False
    else:
        sub = pd.DataFrame({
            "situation": counted["situation"].to_numpy(),
            "action": counted["action"].to_numpy(),
            "day": pd.to_datetime(counted["time"]).dt.date.to_numpy()})
        for h in pdecl.hypotheses:
            sit, act = h.split(":")[0], h.split(":")[1]
            n_days = int(sub.loc[(sub["situation"] == sit)
                                 & (sub["action"] == act), "day"].nunique())
            if n_used is None:              # 实测未覆盖 → 恒 waiting_data
                ok_h = False
            elif open_ended:                # 开区间档：须 days > n_required
                ok_h = n_days > n_used
            else:
                ok_h = n_days >= n_used
            per_hypothesis[h] = {"trigger_days": n_days,
                                 "required_days": n_used,
                                 "passed": bool(ok_h)}
            if not ok_h:
                insufficient.append(h)
        power_ok = len(per_hypothesis) > len(insufficient)
    checks["power_prob"] = {
        "passed": power_ok,
        "unit": "trigger_days",
        "n_tests": n_hyp,
        "n_required_analytic": int(n_required),
        "n_required_measured": n_meas,   # None = T75 实测未覆盖（Δp<0.02）
        "n_required_used": n_used,       # max(解析, 实测)；None = 恒待数据
        "open_ended": open_ended,
        "rule_source": "t75-measured+z2",
        "per_hypothesis": per_hypothesis,
        "insufficient": insufficient,
        "passing_count": len(per_hypothesis) - len(insufficient),
        "split": str(split),
        "base_rate": _PROB_BASE_RATE,
        "alpha": _POWER_ALPHA,
        "t75_rule_pending": False,
        "note": ("无触发计数" if counted is None or len(counted) == 0 else
                 "逐条判定（D52）：样本单位为发现段（time < split）内该假设"
                 "（局面×动作，跨品种/方向合并）的触发日数；Bonferroni 条数"
                 "=候选登记假设数；样本量取解析 D52 与 T75 实测规则"
                 "（rule_source=t75-measured+z2：单假设锚点 4000@0.02（开）/"
                 "2000@0.03/1000@0.05 插值、>0.05 沿段斜率外推 max(0,·)，"
                 "假设数按 (z_α(n)/z_α(1))² 换算）的更严者——实测未覆盖"
                 "（Δp<0.02）恒待数据，开区间档（0.02≤Δp≤0.03）判 pass 需 "
                 "days > n_required；不足的假设判 waiting_data（开轮时移出，"
                 "不替换不改向），候选至少一条通过才算通过；开轮复核用同一"
                 "函数按全轮假设条数（T77）")}
    if counted is not None and len(counted) > 0:
        for h in insufficient:
            d = per_hypothesis[h]
            if d["required_days"] is None:
                reasons.append(
                    f"检验力：假设 {h} 的 T75 实测规则未覆盖"
                    f"（Δp={pdecl.expected_delta_p:g} < 标定下界 0.02——"
                    "开区间下界未测出，不外推）→ 该假设恒待数据"
                    "（铁律 18；延长标定覆盖前不随触发日数增加改变）")
            else:
                reasons.append(
                    f"检验力：假设 {h} 发现段触发日数 {d['trigger_days']} < "
                    f"样本量要求 {d['required_days']}（解析 {int(n_required)} / "
                    f"T75 实测 {n_meas}，取严；按 {n_hyp} 条假设 "
                    "Bonferroni）→ 该假设待数据（铁律 18；开轮时从本轮移除，"
                    "不替换动作、不改方向）")
    elif not power_ok:
        reasons.append(
            "检验力：无触发计数 → 待数据（计数经 bt candidate check 自动"
            "执行）")

    # 条件 5：机制空缺（排序类，不阻塞）
    active = set(active_mechanisms or ())
    if candidate.mechanism_class in active:
        gap_note = (f"机制类别 {candidate.mechanism_class} 已有活跃谱系——排后"
                    "（蓝图 §6.2 条件 5）")
    else:
        gap_note = (f"机制空缺：{candidate.mechanism_class} 是组合里还没有的"
                    "类别（蓝图 §6.2 条件 5；D45 新类别）")
    checks["mechanism_gap"] = {"blocking": False, "note": gap_note,
                               "mechanism_class": candidate.mechanism_class,
                               "active_mechanisms": sorted(active)}
    reasons.append(gap_note)

    if not lit_ok or not holding_ok or cost_ok is False:
        decision = "fail"
    elif cost_ok is None or not power_ok:
        decision = "waiting_data"
    else:
        decision = "pass"
    return PrecheckResult(
        candidate_id=candidate.id, decision=decision, reasons=tuple(reasons),
        checks=checks, events_per_year=None)


def propose_split(data_root, symbols, timeframe) -> pd.Timestamp:
    """发现段/验证段分界提案（T76，D50）：各品种计入段合并（区间并集）后
    的**共同覆盖**里最长一段的中点，取整到月初。确定性：同一库同一结果。

    P1 与一切设计选择只用发现段（< split），P2 只在验证段（≥ split）评估
    （蓝图 P 线 §3.1）；分界在第一个 P 线轮次预注册时写死（T77）。
    """
    segs = coverage_segments(data_root, symbols, [timeframe])
    merged_by_symbol: list[list[list[pd.Timestamp]]] = []
    for sym in symbols:
        key = f"{sym}/{timeframe.upper()}"
        if key not in segs:
            raise ValueError(
                f"研究库无 {key} 覆盖——分界提案需要全部品种的共同覆盖")
        intervals = sorted(
            (pd.Timestamp(s["first"]), pd.Timestamp(s["last"]))
            for s in segs[key] if s.get("counted"))
        if not intervals:
            raise ValueError(f"{key} 没有计入段（未批准来源不算覆盖）")
        merged: list[list[pd.Timestamp]] = []
        for a, b in intervals:
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        merged_by_symbol.append(merged)

    def _intersect(a: list[list[pd.Timestamp]],
                   b: list[list[pd.Timestamp]]) -> list[list[pd.Timestamp]]:
        out, i, j = [], 0, 0
        while i < len(a) and j < len(b):
            lo = max(a[i][0], b[j][0])
            hi = min(a[i][1], b[j][1])
            if lo < hi:
                out.append([lo, hi])
            if a[i][1] <= b[j][1]:
                i += 1
            else:
                j += 1
        return out

    common = merged_by_symbol[0]
    for merged in merged_by_symbol[1:]:
        common = _intersect(common, merged)
        if not common:
            raise ValueError(
                f"{symbols} 在 {timeframe} 上没有共同覆盖段——分界无从提案")
    longest = max(common, key=lambda ab: (ab[1] - ab[0]).total_seconds())
    mid = longest[0] + (longest[1] - longest[0]) / 2
    return mid.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
