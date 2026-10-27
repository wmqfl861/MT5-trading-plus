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
   成本）÷ 品种在持仓长度上的无条件收益波动 σ × √每年事件数；σ 由工具
   从研究库算——随机时点抽样的总体极限 = 持仓周期 bar 的相邻收盘相对
   收益全体，品种性质、与候选信号无关）；σ=0 或序列不可用 → 隐含记
   None、按声明夏普计；不足 → waiting_data（铁律 18"待数据"队列），
   不是 fail；
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
    "CandidateError", "Candidate", "Literature", "Declared",
    "PrecheckResult", "MECHANISM_CLASSES", "RESEARCH_STATUSES",
    "BOOKKEEPING_STATUSES", "PRECHECK_DECISIONS", "load_candidate",
    "coverage_years", "coverage_segments", "unconditional_volatility",
    "precheck",
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

def _span_years(first: pd.Timestamp, last: pd.Timestamp) -> float:
    return ((last - first).total_seconds()
            / (_DAYS_PER_YEAR * 86400.0))


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
            meta_p = root / "bars" / str(sym) / tf_u / "meta.json"
            if meta_p.is_file():
                try:
                    wm = json.loads(meta_p.read_text(encoding="utf-8"))
                    first = pd.Timestamp(wm["first_time"])
                    last = pd.Timestamp(wm["last_closed_time"])
                except (ValueError, KeyError, TypeError):
                    first = last = None
            if first is None or last is None or last <= first:
                bars = load_bars(str(root), str(sym), tf_u)
                if bars is not None and len(bars) >= 2:
                    t = pd.to_datetime(bars["time"])
                    first, last = t.iloc[0], t.iloc[-1]
            if first is not None and last is not None and last > first:
                segs.append({"source": "mt5",
                             "first": first.isoformat(),
                             "last": last.isoformat(),
                             "years": _span_years(first, last),
                             "counted": True})
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
                            segs.append({
                                "source": src_dir.name,
                                "first": e_first.isoformat(),
                                "last": e_last.isoformat(),
                                "years": _span_years(e_first, e_last),
                                "counted": True})
                    else:
                        # 未批准来源（如 histdata，D36"条款不明的不用"）：
                        # 在场、如实列出，但不计入年数
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


def coverage_years(data_root, symbols, timeframes) -> dict[str, float]:
    """研究库按 品种×周期 的实际覆盖年数（T80/G3：主段 + 已批准外部序列
    各段年数之和；未批准来源不计——coverage_segments 逐段口径）。

    键为 "SYMBOL/TF"（precheck 的 research_years 口径）；与实测密度年化
    配套（report.observed_bars_per_year 同一思想）：年化夏普的分母年数
    取跨度，检验力（Lo 近似）才与台账里的夏普同口径。
    """
    return {key: float(sum(s["years"] for s in segs if s.get("counted")))
            for key, segs in coverage_segments(
                data_root, symbols, timeframes).items()}


# ------------------------------------------------- 无条件收益波动（T80/G2）

def unconditional_volatility(data_root, symbol: str,
                             timeframe: str) -> float | None:
    """品种在持仓长度上的无条件收益波动 σ（相对价格，随机时点口径）。

    σ = 该品种**持仓周期** bar 的相邻收盘相对收益全体的样本标准差
    （ddof=1）——随机时点抽样的总体极限：随机等权抽取入场时点、持有
    一个周期长度的收益分布，即品种本身的性质（与候选信号无关、不是
    策略样本内结果，第十二轮 G2）。用全体而非抽样的原因：确定性、无
    种子，precheck 结果逐位可复算。

    序列缺失 → None；数据不足（<3 根）→ None；恒价序列 → 0.0（隐含
    夏普无从折算，调用方按声明夏普计并如实记录）。
    """
    bars = load_bars(str(data_root), str(symbol), timeframe.upper())
    if bars is None or len(bars) < 3:
        return None
    closes = pd.to_numeric(bars["close"]).to_numpy(dtype="float64")
    rets = closes[1:] / closes[:-1] - 1.0
    return float(np.std(rets, ddof=1))


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
    years_detail = {f"{s}/{candidate.holding}":
                    float(research_years.get(f"{s}/{candidate.holding}", 0.0))
                    for s in candidate.symbols}
    years = min(years_detail.values())
    missing = [k for k, v in years_detail.items() if v <= 0.0]
    sigma_by_symbol: dict[str, float | None] = {}
    implied_by_symbol: dict[str, float | None] = {}
    for s in candidate.symbols:
        sigma = unconditional_volatility(data_root, s, candidate.holding)
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
        "sigma_method": (
            "无条件收益波动 σ：候选持仓周期 bar 的相邻收盘相对收益全体"
            "（随机时点抽样的总体极限，ddof=1）——品种性质，与候选信号"
            "无关、不取自本项目样本内（T80/G2）；隐含夏普 =（毛收益−事前"
            "来回成本）÷σ×√每年事件数，检验力取 min(声明, 隐含)"),
        "alpha": _POWER_ALPHA, "n_tests": _POWER_N_TESTS,
        "years_detail": years_detail,
        "min_detectable_sharpe": (
            power.min_detectable_sharpe(years, alpha=_POWER_ALPHA,
                                        n_tests=_POWER_N_TESTS)
            if years > 0.0 else None)}
    if research_coverage_segments is not None:
        # T80/G3：逐段列来源与年数（未批准来源 counted=False 如实列出）
        checks["power"]["coverage_segments"] = {
            k: v for k, v in research_coverage_segments.items()
            if k in years_detail}
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
