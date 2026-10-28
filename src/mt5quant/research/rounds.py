"""研究轮次预注册（蓝图 v4 附录 D；铁律 16；D32）。

先定规则再开卡（铁律 16）：每一轮研究在看到任何结果之前提交
`research/rounds/R#.toml`（候选 ≤3 个新谱系、试验预算、锁箱用量、推进
规则、结束条件、批准人、每个候选的评估方式），**文件已提交且工作区
干净**后 `bt round open R#` 才把该轮写进台账 rounds 表（带哈希链）；
`bt` 拒绝运行不属于已开启轮次的卡（scripts/bt.py 的运行约束接线）。

装载（附录 D 模式，fail-fast）：
- 必填字段齐全且类型正确：id（R#）/ opened（ISO 日期）/ candidates
  （1..3 个 C###，去重）/ trial_budget（≥1）/ lockbox_uses_max（≥1）/
  dsr_trial_scope（family | project | rounds_lineage——T85/D34 冻结后
  gates.toml 的口径）/ advance_rule / close_rule /
  approved_by（非空）；
- [evaluation] 每个候选一条（mode + primary 非空），键与 candidates
  一一对应——评估方式是预注册内容的一部分，缺了就不可信；
- 未知顶层字段 / [evaluation] 未知键 → RoundError（拼写错误会静默丢
  约束，全部拒绝）；文件名主干必须与 id 一致（R1.toml ↔ id="R1"）。

D16（2026-10-27 已决定，D49）：本锁箱最多 3 次 G5、每个研究轮次最多
1 次、H 线与 P 线共用；用尽或新增满 12 个月数据后退役。开启轮次时
`lockbox_uses_max` 不得超过 D16 剩余预算——本模块把总预算写死为常量
D16_LOCKBOX_USES_TOTAL=3（任务口径：当前建议值 3，待用户最终确认后
可调；调整是管理员动作，不是改预注册）。"每轮最多 1 次"同样出自 D16，
运行约束按轮次申报的 lockbox_uses_max 执行（申报值 ≤ 剩余预算才可开）。

screened_pass 口径（T62 衔接）：候选的预筛结论以**台账 candidate_checks
的最新 decision** 为准（pass = screened_pass）——台账带哈希链，事后
改写留痕；候选 TOML 的 status 是登记材料，不作为运行约束的数据源。
"""
from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

__all__ = [
    "RoundError", "Round", "MAX_ROUND_CANDIDATES", "DSR_TRIAL_SCOPES",
    "D16_LOCKBOX_USES_TOTAL", "load_round", "candidate_screened_pass",
]

# 附录 D：每轮最多 3 个新谱系（蓝图 §6.2：迭代在轮次内登记并计入预算）
MAX_ROUND_CANDIDATES = 3
# T64/D33 的试验计数口径（G4 的 N 取数范围；缺省 family。T85/D34 冻结后
# config/gates.toml 显式写 rounds_lineage——T96/R14：枚举与冻结口径对齐）
DSR_TRIAL_SCOPES = ("family", "project", "rounds_lineage")

# D16（D49，2026-10-27）：本锁箱最多 3 次 G5（H 线与 P 线共用）。
# 任务口径：当前建议值写死 3，待用户决策后可调（管理员改常量 + 重开新
# 锁箱时重新核定；不改已开启轮次的申报值——预注册一次性）。
D16_LOCKBOX_USES_TOTAL = 3

_ROUND_ID = re.compile(r"^R\d{1,4}$")
_CANDIDATE_ID = re.compile(r"^C\d{3,}$")
_TOP_KEYS = ("id", "opened", "candidates", "trial_budget", "lockbox_uses_max",
             "dsr_trial_scope", "advance_rule", "close_rule", "approved_by")
_EVAL_KEYS = ("mode", "primary")
_HYPOTHESIS_RE = re.compile(r"^S[123]:A[1-4]:[A-Za-z][A-Za-z0-9_>]*$")


class RoundError(ValueError):
    """轮次预注册不符合附录 D 模式（缺字段/类型错/枚举外/超上限）。"""


@dataclass(frozen=True)
class Round:
    """一份已通过附录 D 模式校验的轮次预注册。"""

    id: str
    opened: str                        # 计划开启日期（ISO，YYYY-MM-DD）
    candidates: tuple[str, ...]
    trial_budget: int                  # 本轮最多去重试验次数
    lockbox_uses_max: int              # 本轮最多 G5 次数（受 D16 总预算约束）
    dsr_trial_scope: str               # family | project | rounds_lineage
                                        # （T64/D33；T85/D34 冻结 =
                                        # rounds_lineage）
    advance_rule: str                  # 哪些结果算过；过了不再在本轮挑选
    close_rule: str                    # 结束条件
    approved_by: str
    evaluation: dict = field(default_factory=dict)   # C### -> {mode, primary}
    segments: dict | None = None      # T73A：{"split": "YYYY-MM-01"}（D50）
    hypotheses: dict = field(default_factory=dict)   # T73A：C### -> 假设数组
    path: str = ""

    def content_sha256(self) -> str:
        """预注册文件内容的 sha256（开启时绑定进台账 rounds 行——内容与
        git 提交一一对应，事后改文件即可对出）。"""
        if not self.path:
            raise RoundError("Round 未带文件路径，无从计算内容哈希")
        return hashlib.sha256(
            Path(self.path).read_bytes()).hexdigest()


def _require_str(data: dict, key: str, source: str) -> str:
    v = data.get(key)
    if not isinstance(v, str):
        raise RoundError(f"{source}：字段 {key!r} 必须是字符串，收到 {v!r}")
    if not v.strip():
        raise RoundError(f"{source}：字段 {key!r} 不能为空（附录 D）")
    return v


def _require_int(data: dict, key: str, source: str, *, minimum: int,
                 maximum: int | None = None,
                 maximum_msg: str | None = None) -> int:
    v = data.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        raise RoundError(
            f"{source}：字段 {key!r} 必须是整数，收到 {v!r}")
    if v < minimum:
        raise RoundError(
            f"{source}：字段 {key!r} 必须 ≥{minimum}，收到 {v!r}")
    if maximum is not None and v > maximum:
        raise RoundError(maximum_msg or (
            f"{source}：字段 {key!r} 必须 ≤{maximum}，收到 {v!r}"))
    return v


def _validate_evaluation(raw: object, candidates: tuple[str, ...],
                         source: str) -> dict:
    """[evaluation]：每个候选恰一条（mode + primary 非空），键与候选
    名单一一对应——多出（不在名单内）或缺失（候选无评估方式）都拒绝。"""
    if not isinstance(raw, dict):
        raise RoundError(
            f"{source}：[evaluation] 表缺失或不是表（附录 D：每个候选的"
            "评估方式与主统计量是预注册内容，缺了不可信）")
    unknown = sorted(set(raw) - set(candidates))
    if unknown:
        raise RoundError(
            f"{source}：[evaluation] 有不在候选名单内的键 {unknown}"
            f"（candidates={list(candidates)}）")
    missing = [c for c in candidates if c not in raw]
    if missing:
        raise RoundError(
            f"{source}：[evaluation] 缺候选 {missing} 的评估方式"
            "（每个候选必须声明 mode 与 primary）")
    out: dict = {}
    for cid, ev in raw.items():
        if not isinstance(ev, dict):
            raise RoundError(
                f"{source}：[evaluation].{cid} 必须是表"
                f'（形如 {cid} = {{ mode = "event", primary = "…" }}），'
                f"收到 {ev!r}")
        bad = sorted(set(ev) - set(_EVAL_KEYS))
        if bad:
            raise RoundError(
                f"{source}：[evaluation].{cid} 有未知键 {bad}，"
                f"合法键 {list(_EVAL_KEYS)}")
        item = {}
        for k in _EVAL_KEYS:
            v = ev.get(k)
            if not isinstance(v, str) or not v.strip():
                raise RoundError(
                    f"{source}：[evaluation].{cid}.{k} 必须是非空字符串，"
                    f"收到 {v!r}")
            item[k] = v
        out[cid] = item
    return out


def validate_round(data: dict, *, source: str) -> Round:
    """附录 D 模式校验（fail-fast）。"""
    if not isinstance(data, dict):
        raise RoundError(f"{source}：轮次预注册必须是 TOML 表")
    unknown = sorted(set(data) - set(_TOP_KEYS)
                     - {"evaluation", "segments", "hypotheses"})
    if unknown:
        raise RoundError(
            f"{source}：未知顶层字段 {unknown}（附录 D 字段齐了才可信，"
            f"拼写错误静默丢约束——全部拒绝）；合法字段 {list(_TOP_KEYS)}"
            " + [evaluation]")

    rid = _require_str(data, "id", source)
    if not _ROUND_ID.match(rid):
        raise RoundError(
            f"{source}：id 必须形如 R1/R12（R + 数字），收到 {rid!r}")

    opened = _require_str(data, "opened", source)
    try:
        date.fromisoformat(opened)
    except ValueError as e:
        raise RoundError(
            f"{source}：opened 必须是 ISO 日期（YYYY-MM-DD），"
            f"收到 {opened!r}") from e

    raw_cands = data.get("candidates")
    if (not isinstance(raw_cands, list) or not raw_cands
            or not all(isinstance(c, str) and c.strip()
                       for c in raw_cands)):
        raise RoundError(
            f"{source}：candidates 必须是非空字符串数组，收到 {raw_cands!r}")
    for c in raw_cands:
        if not _CANDIDATE_ID.match(c):
            raise RoundError(
                f"{source}：候选 id 必须形如 C001（C + 至少三位数字），"
                f"收到 {c!r}")
    if len(set(raw_cands)) != len(raw_cands):
        raise RoundError(
            f"{source}：candidates 有重复 {raw_cands!r}（同一候选在一轮内"
            "只登记一次）")
    if len(raw_cands) > MAX_ROUND_CANDIDATES:
        raise RoundError(
            f"{source}：候选 {len(raw_cands)} 个超过上限 "
            f"{MAX_ROUND_CANDIDATES}（附录 D：每轮最多 3 个新谱系）")
    candidates = tuple(raw_cands)

    scope = _require_str(data, "dsr_trial_scope", source)
    if scope not in DSR_TRIAL_SCOPES:
        raise RoundError(
            f"{source}：dsr_trial_scope {scope!r} 不在枚举 "
            f"{list(DSR_TRIAL_SCOPES)} 内（T64/D33：G4 试验计数的取数范围）")

    evaluation = _validate_evaluation(data.get("evaluation"), candidates,
                                      source)
    all_prob = all(ev["mode"] == "probability"
                   for ev in evaluation.values())

    # T73A：lockbox_uses_max = 0（P1 不用锁箱）只对全部候选都是 probability
    # 模式的轮次放行；上限 1 不变（D16：每个研究轮次最多 1 次 G5）
    lockbox_uses_max = _require_int(
        data, "lockbox_uses_max", source, minimum=0, maximum=1,
        maximum_msg=f"{source}：lockbox_uses_max 超过 1——D16 机器强制"
        "（用户 2026-10-28 授权）：每个研究轮次最多 1 次 G5")
    if lockbox_uses_max == 0 and not all_prob:
        raise RoundError(
            f"{source}：lockbox_uses_max = 0 只在全部候选的 [evaluation] 都是 "
            'mode = "probability" 时允许（P1 不用锁箱；H 线/混合轮次仍按 D16'
            " 申报 ≥1）")

    # T73A/D50：[segments].split（概率轮次的发现段/验证段分界，月初）
    segments = data.get("segments")
    if segments is not None:
        if not isinstance(segments, dict) or set(segments) != {"split"}:
            raise RoundError(
                f"{source}：[segments] 必须且只含 split（YYYY-MM-01，月初；"
                "D50），收到 " + repr(sorted(segments) if isinstance(
                    segments, dict) else segments))
        split = segments["split"]
        try:
            from datetime import date as _d
            y, m, dd = (int(x) for x in split.split("-"))
            ok = _d(y, m, dd) and dd == 1 and len(split) == 10
        except (TypeError, ValueError):
            ok = False
        if not ok:
            raise RoundError(
                f"{source}：[segments].split 必须是月初的 ISO 日期"
                f"（YYYY-MM-01，D50），收到 {split!r}")

    # T73A（蓝图 P 线附录 A）：[hypotheses]——概率模式候选必填（Holm 假设
    # 族），每条形如 S#:A#:fade>base；非概率候选不得出现。与候选登记的
    # 交叉校验（每条都在候选登记的假设里）在 bt round open 做（那里装载
    # 候选登记）。
    hyp_raw = data.get("hypotheses", {})
    if not isinstance(hyp_raw, dict):
        raise RoundError(f"{source}：[hypotheses] 必须是表（C### -> 假设数组）")
    hypotheses: dict[str, tuple[str, ...]] = {}
    unknown_h = sorted(set(hyp_raw) - set(candidates))
    if unknown_h:
        raise RoundError(
            f"{source}：[hypotheses] 有不在候选名单内的键 {unknown_h}")
    for cid, ev in evaluation.items():
        listed = hyp_raw.get(cid)
        if ev["mode"] == "probability":
            if not listed:
                raise RoundError(
                    f"{source}：概率模式候选 {cid} 缺 [hypotheses]（本轮要"
                    "检验的假设族，Holm 校正的对象——蓝图 P 线附录 A）")
            for h in listed:
                if not isinstance(h, str) or not _HYPOTHESIS_RE.match(h):
                    raise RoundError(
                        f"{source}：[hypotheses].{cid} 的条目 {h!r} 必须形如 "
                        "'S1:A3:fade>base'（局面:动作:方向）")
            if len(set(listed)) != len(listed):
                raise RoundError(
                    f"{source}：[hypotheses].{cid} 有重复条目 {listed!r}"
                    "（Holm 家族里每条假设只出现一次）")
            hypotheses[cid] = tuple(listed)
        elif listed:
            raise RoundError(
                f"{source}：[hypotheses].{cid} 不允许——非概率候选（mode="
                f"{ev['mode']!r}）不带假设族")

    return Round(
        id=rid, opened=opened, candidates=candidates,
        trial_budget=_require_int(data, "trial_budget", source, minimum=1),
        lockbox_uses_max=lockbox_uses_max,
        dsr_trial_scope=scope,
        advance_rule=_require_str(data, "advance_rule", source),
        close_rule=_require_str(data, "close_rule", source),
        approved_by=_require_str(data, "approved_by", source),
        evaluation=evaluation, segments=segments, hypotheses=hypotheses)


def load_round(path) -> Round:
    """读轮次预注册（tomllib）+ 附录 D 校验；文件名主干必须与 id 一致。"""
    p = Path(path)
    if not p.is_file():
        raise RoundError(f"轮次预注册文件不存在：{p}")
    try:
        with open(p, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise RoundError(f"{p}：TOML 解析失败：{e}") from e
    r = validate_round(data, source=str(p))
    if p.stem != r.id:
        raise RoundError(
            f"{p}：文件名主干 {p.stem!r} 与轮次 id {r.id!r} 不一致"
            f"（附录 D 约定文件名为 {r.id}.toml）")
    return Round(id=r.id, opened=r.opened, candidates=r.candidates,
                 trial_budget=r.trial_budget,
                 lockbox_uses_max=r.lockbox_uses_max,
                 dsr_trial_scope=r.dsr_trial_scope,
                 advance_rule=r.advance_rule, close_rule=r.close_rule,
                 approved_by=r.approved_by, evaluation=r.evaluation,
                 segments=r.segments, hypotheses=r.hypotheses,
                 path=str(p))


def candidate_screened_pass(ledger, candidate_id: str) -> bool:
    """候选是否 screened_pass：台账 candidate_checks 的**最新** decision
    == pass（T62 的预筛结论表，带哈希链；重检翻案以最新为准；无记录 →
    未过）。ledger 为 mt5quant.research.ledger.Ledger（鸭子类型即可）。"""
    rows = ledger.candidate_checks(candidate_id)
    if not rows:
        return False
    return rows[-1]["decision"] == "pass"
