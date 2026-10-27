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
   meta 水位/parquet 实测，不用常数）上按折算后预期夏普算单次检验
   （mt5quant.research.power，与 round11 探针逐位一致）；不足 →
   waiting_data（铁律 18"待数据"队列），不是 fail；
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

import pandas as pd

from mt5data.store import TIMEFRAME_SECONDS, load_bars
from mt5quant.research import cards
from mt5quant.research import power

__all__ = [
    "CandidateError", "Candidate", "Literature", "Declared",
    "PrecheckResult", "MECHANISM_CLASSES", "RESEARCH_STATUSES",
    "BOOKKEEPING_STATUSES", "PRECHECK_DECISIONS", "load_candidate",
    "coverage_years", "precheck",
]

# 附录 C：机制类别七分类（蓝图 v4 §6.2 预筛条件 5）
MECHANISM_CLASSES = ("trend", "reversal", "flow_calendar", "risk_premium",
                     "volatility", "cross_asset", "event")

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
             "holding", "mechanism", "source_card", "notes")
_LIT_KEYS = ("original", "replications", "rebuttals", "search_log",
             "published_effect", "expected_effect")
_DECLARED_KEYS = ("events_per_year", "gross_return_per_event",
                  "entry_hour_ny", "exit_hour_ny", "overnight_nights",
                  "expected_sharpe")

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
    entry_hour_ny: int              # 入场成交的纽约本地小时（成本取该小时实测）
    exit_hour_ny: int               # 出场成交的纽约本地小时
    overnight_nights: float         # 每笔/每事件预期计费夜数（0=不过夜；保守申报）
    expected_sharpe: float          # 折算后预期年化夏普（检验力用）


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
    declared: Declared | None = None
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

    return Declared(
        events_per_year=_num("events_per_year", positive=True),
        gross_return_per_event=_num("gross_return_per_event", positive=True),
        entry_hour_ny=_hour("entry_hour_ny"),
        exit_hour_ny=_hour("exit_hour_ny"),
        overnight_nights=_num("overnight_nights", positive=False),
        expected_sharpe=_num("expected_sharpe", positive=False),
    )


def validate_candidate(data: dict, *, source: str) -> Candidate:
    """附录 C 模式校验（fail-fast）。"""
    if not isinstance(data, dict):
        raise CandidateError(f"{source}：候选登记必须是 TOML 表")
    unknown = sorted(set(data) - set(_TOP_KEYS) - {"literature", "declared"})
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

    literature = _validate_literature(data.get("literature"), source)
    declared = None
    if data.get("declared") is not None:
        declared = _validate_declared(data["declared"], source)
    if status in RESEARCH_STATUSES and declared is None:
        raise CandidateError(
            f"{source}：status={status} 的研究候选必须带 [declared] 数值声明"
            "（每年事件/交易数、每笔折算后毛收益、入出场小时、计费夜数、"
            "折算后预期夏普——预筛的计算输入；折算依据写在 "
            "[literature].expected_effect，不得取自本项目样本内，铁律 17）")
    return Candidate(
        id=cid, title=title, mechanism_class=mechanism_class, status=status,
        symbols=tuple(symbols), holding=holding, mechanism=mechanism,
        literature=literature, source_card=source_card, notes=notes,
        declared=declared)


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
        declared=cand.declared, path=str(p))


# ---------------------------------------------------------------- 覆盖年数

def coverage_years(data_root, symbols, timeframes) -> dict[str, float]:
    """研究库按 品种×周期 的实际覆盖年数（(last−first)/365.25 天）。

    键为 "SYMBOL/TF"（precheck 的 research_years 口径）；优先读序列
    meta.json 水位（first_time / last_closed_time），缺 meta 时读 parquet
    的 time 列。没有的序列不出现在结果里（调用方按缺失处理）。

    与实测密度年化配套（report.observed_bars_per_year 同一思想）：年化
    夏普的分母年数取跨度，检验力（Lo 近似）才与台账里的夏普同口径。
    """
    root = Path(data_root)
    out: dict[str, float] = {}
    for sym in symbols:
        for tf in timeframes:
            key = f"{sym}/{tf.upper()}"
            first = last = None
            meta_p = root / "bars" / str(sym) / tf.upper() / "meta.json"
            if meta_p.is_file():
                try:
                    wm = json.loads(meta_p.read_text(encoding="utf-8"))
                    first = pd.Timestamp(wm["first_time"])
                    last = pd.Timestamp(wm["last_closed_time"])
                except (ValueError, KeyError, TypeError):
                    first = last = None
            if first is None or last is None or last <= first:
                bars = load_bars(str(root), str(sym), tf.upper())
                if bars is None or len(bars) < 2:
                    continue
                t = pd.to_datetime(bars["time"])
                first, last = t.iloc[0], t.iloc[-1]
            if last > first:
                out[key] = ((last - first).total_seconds()
                            / (_DAYS_PER_YEAR * 86400.0))
    return out


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
             active_mechanisms: set[str] | frozenset[str] | None = None
             ) -> PrecheckResult:
    """预筛五条件（蓝图 §6.2）→ pass / fail / waiting_data 与理由。

    - data_root：数据根（合约大小与参考价取自 specs/K 线）；
    - cost_model：成本模型 v3（Costs 聚合体；预筛只信缓存文件模型）；
    - research_years：品种×周期覆盖年数（coverage_years 的产出，键
      "SYMBOL/TF"）——检验力用它，不用常数；
    - active_mechanisms：组合内已有活跃谱系的机制类别集合（可选；调用方
      扫候选登记 status∈{in_round, screened_pass} 得到）——机制空缺是
      排序类理由，不阻塞（决定只有三值）。
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

    # 条件 4：检验力（单次检验；铁律 18——不足进"待数据"，不是 fail）
    years_detail = {f"{s}/{candidate.holding}":
                    float(research_years.get(f"{s}/{candidate.holding}", 0.0))
                    for s in candidate.symbols}
    years = min(years_detail.values())
    missing = [k for k, v in years_detail.items() if v <= 0.0]
    if years <= 0.0:
        pw = 0.0
    else:
        pw = power.power_one_sided(declared.expected_sharpe, years,
                                   alpha=_POWER_ALPHA,
                                   n_tests=_POWER_N_TESTS)
    power_ok = pw >= _POWER_MIN
    checks["power"] = {
        "passed": power_ok, "power": float(pw), "min": _POWER_MIN,
        "years": years, "expected_sharpe": declared.expected_sharpe,
        "alpha": _POWER_ALPHA, "n_tests": _POWER_N_TESTS,
        "years_detail": years_detail,
        "min_detectable_sharpe": (
            power.min_detectable_sharpe(years, alpha=_POWER_ALPHA,
                                        n_tests=_POWER_N_TESTS)
            if years > 0.0 else None)}
    if not power_ok:
        if missing:
            reasons.append(
                f"研究库无覆盖 {missing}——检验力无从计算 → 待数据"
                "（铁律 18：延长研究段（T67）后重检）")
        else:
            reasons.append(
                f"检验力 {pw:.0%} < {_POWER_MIN:.0%}（{years:.2f} 年、折算"
                f"夏普 {declared.expected_sharpe:g}）→ 待数据（铁律 18："
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
