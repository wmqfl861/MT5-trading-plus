"""假设卡：研究假设的"预测先行"登记（模板见 docs/blueprint.md 附录 A v2）。

铁律（先卡后测）：
- 讲不出机制的假设不测；预期（expected）先写死，再和结果对照；
- 卡在第一次回测前提交到 git，且提交时**该文件**在工作区必须干净
  （提交时间即"预测先于测量"的证据）；
- 卡的参数 ≤4 个、网格笛卡尔积 ≤100 组（与 strategies.base 同限）。

谱系（T21d/H4，附录 A v2）：
- 卡字段新增 family / parent / strategy / selection_rule；
- 版本迭代 = 同一谱系**新建文件** H###-v<n>.toml（id 含版本后缀），
  旧版本文件不得再改——DSR 的试验次数按谱系（family）去重累计；
- 旧卡（H001-H007）缺字段：family = family_of(id)（无后缀时 = id）、
  strategy = None（运行必须由 --strategy 给出，见 Card.migration_hint）。

轮次归属（T63/D32，铁律 16）：卡可带 round / candidate 两个可选字段
（成对出现：所属研究轮次 R# 与该轮名单内的候选 C###）。台账一旦有
轮次（Ledger.rounds_any），不带这两个字段的卡拒绝运行——bt 拒绝运行
不属于已提交轮次的卡；轮次制之前的旧卡（两字段都缺省）在 rounds 表
为空的台账上照常运行（runs.round_id=NULL=轮次制之前）。

committed_at 语义（钉死）：
- `git log -1 --format=%cI -- <path>`，即**最后一次**改动该文件的提交时间
  （aware datetime，带作者本地时区偏移）；
- 输出为空（文件未被 git 跟踪/从未提交）→ None；
- git 返回非零（不在仓库内、路径在仓库外、仓库损坏）、命令超时 → None
  （无法确定提交时间，调用方按"未提交"处理 → 拒绝运行）；
- 只有 git 可执行缺失（FileNotFoundError）如实抛出；
- %cI 输出无法解析为 ISO 时间 → 如实抛 ValueError（git 行为异常，值得暴露）。

先卡后测的判定（ensure_card_precedes_run，纯函数，git 交互可注入）：
- committed_at 为 None → 拒绝（卡未提交）；
- worktree_clean=False（`git status --porcelain -- <卡文件>` 非空，探针 P6：
  改了没提交）→ 拒绝：预测内容与登记内容可能不一致；
- 该卡（按 card_id）在台账中已有运行，且 committed_at 晚于首次运行时间 →
  拒绝：说明卡文件在试验开始后被改动过。按**卡文件**（id+version）判定：
  新版本文件（H012-v2）有自己的首次运行，不受旧版本运行影响。
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from mt5data.store import TIMEFRAME_SECONDS

MAX_PARAMS = 4            # 与 mt5quant.strategies.base 同限
MAX_GRID_POINTS = 100

_RULES_KEYS = ("entry", "exit", "stop")
_EXPECTED_KEYS = ("trades_per_year", "win_rate", "payoff_ratio")
_PROB_EXPECTED_KEYS = ("delta_p", "triggers_per_year")   # 蓝图 P 线附录 A
_TOP_KEYS = ("id", "family", "version", "parent", "strategy",
             "selection_rule", "round", "candidate", "symbols", "timeframe",
             "session", "phenomenon", "mechanism", "should_fail_when",
             "falsified_if", "graveyard_checked",
             "evaluation_mode", "primary_statistic", "segment")
_EVALUATION_MODES = ("standard", "probability")
_SEGMENTS = ("discovery", "validation")
_P_CARD_ID = re.compile(r"^P\d+-v\d+$")

_ROUND_ID_RE = re.compile(r"^R\d{1,4}$")
_CANDIDATE_ID_RE = re.compile(r"^C\d{3,}$")

_VERSION_SUFFIX = re.compile(r"-v(\d+)$")
_LEGACY_MIGRATION_HINT = (
    "卡 {cid} 为附录 A v1 旧卡（缺 strategy 字段）：运行必须显式 --strategy；"
    "建议迁移到 v2 卡——新建 {fam}-v{n}.toml 补齐 family/parent/strategy/"
    "selection_rule 后提交，旧卡文件不改")


class CardError(ValueError):
    """卡内容不符合模式（缺字段/参数越界/TOML 坏/文件名与 id 不一致）。"""


class CardNotCommittedError(RuntimeError):
    """卡尚未提交到 git——先卡后测被破坏（未提交）。"""


class CardLateCommitError(RuntimeError):
    """卡的提交时间晚于该卡首次运行——先卡后测被破坏（事后改动）。"""


class CardDirtyWorktreeError(RuntimeError):
    """卡文件在工作区有未提交改动（探针 P6）——先卡后测被破坏。"""


class CardStrategyMismatchError(RuntimeError):
    """传入 --strategy 与卡上绑定的 strategy 不一致——拒绝运行（H4）。"""


def family_of(card_id: str) -> str:
    """卡谱系：去掉版本后缀（H012-v2 → H012；无后缀 → 原样）。

    台账的 N（DSR 试验次数）按谱系去重累计（T21c/H4）。
    """
    return _VERSION_SUFFIX.sub("", card_id)


@dataclass(frozen=True)
class Card:
    """一张已通过模式校验的假设卡（字段见附录 A v2）。"""

    id: str
    version: int
    symbols: tuple[str, ...]
    timeframe: str
    session: str
    phenomenon: str
    mechanism: str
    should_fail_when: str
    falsified_if: str
    graveyard_checked: tuple[str, ...]
    family: str = ""
    parent: str = ""
    strategy: str | None = None
    selection_rule: str = "max_net_profit"
    round: str = ""                     # T63/D32：所属研究轮次（R#；空=未入轮）
    candidate: str = ""                 # T63/D32：轮次名单内的候选（C###；空=未入轮）
    evaluation_mode: str = "standard"   # T73A：standard | probability（P 线）
    primary_statistic: str = ""         # T73A：probability 卡恒 delta_p_one_sided
    segment: str = ""                   # T73A：discovery | validation（D50）
    prob: dict | None = None            # T73A：[prob] 段（仅 probability 卡）
    rules: dict = field(default_factory=dict)     # entry/exit/stop -> str
    params: dict = field(default_factory=dict)    # 名 -> 网格 tuple（标量）
    expected: dict = field(default_factory=dict)  # 三项预期 -> str
    path: str = ""

    def param_grid(self) -> list[dict]:
        """参数网格的笛卡尔积（组合 ≤100 已在装载时校验）；无参数 → [{}]。"""
        combos: list[dict] = [{}]
        for name, grid in self.params.items():
            combos = [{**c, name: v} for c in combos for v in grid]
        return combos

    @property
    def is_legacy(self) -> bool:
        """附录 A v1 旧卡：未绑定 strategy（必须由 --strategy 给出）。"""
        return self.strategy is None

    @property
    def migration_hint(self) -> str:
        """旧卡迁移提示（打印给研究侧；v2 卡返回空串）。"""
        if not self.is_legacy:
            return ""
        return _LEGACY_MIGRATION_HINT.format(
            cid=self.id, fam=family_of(self.id), n=self.version + 1)


def _require_str(data: dict, key: str, *, nonempty: bool, source: str) -> str:
    v = data.get(key)
    if not isinstance(v, str):
        raise CardError(f"{source}：字段 {key!r} 必须是字符串，收到 {v!r}")
    if nonempty and not v.strip():
        raise CardError(f"{source}：字段 {key!r} 不能为空")
    return v


def _no_whitespace(v: str, key: str, source: str) -> str:
    if any(ch.isspace() for ch in v):
        raise CardError(
            f"{source}：字段 {key!r} 不能含空白字符：{v!r}")
    return v


def _validate_lineage(data: dict, cid: str, version: int,
                      source: str) -> tuple[str, str, str | None, str]:
    """校验 family / parent / strategy / selection_rule（附录 A v2，可缺省）。

    缺省值（旧卡兼容）：family = family_of(id)、parent = ""、
    strategy = None（必须由 --strategy 给出，见 Card.migration_hint）、
    selection_rule = "max_net_profit"。
    """
    raw_family = data.get("family")
    if raw_family is None or (isinstance(raw_family, str)
                              and not raw_family.strip()):
        family = family_of(cid)
    else:
        if not isinstance(raw_family, str):
            raise CardError(
                f"{source}：family 必须是字符串，收到 {raw_family!r}")
        family = _no_whitespace(raw_family, "family", source)
    if cid != family and not cid.startswith(family + "-"):
        raise CardError(
            f"{source}：id {cid!r} 必须等于 family 或以 {family!r}+'-' 开头"
            "（附录 A v2：同一谱系共用 family，DSR 的 N 按谱系累计）")

    suffix = _VERSION_SUFFIX.search(cid)
    if suffix and int(suffix.group(1)) != version:
        raise CardError(
            f"{source}：id 后缀 {suffix.group(0)!r} 与 version={version} 不一致"
            "（文件名主干 = id，版本后缀必须对上）")

    raw_parent = data.get("parent")
    parent = "" if raw_parent is None else raw_parent
    if not isinstance(parent, str):
        raise CardError(f"{source}：parent 必须是字符串，收到 {parent!r}")
    if version == 1 and parent.strip():
        raise CardError(
            f"{source}：version 1 的 parent 必须为空（首代无上一版本），"
            f"收到 {parent!r}")
    if version >= 2 and not parent.strip():
        raise CardError(
            f"{source}：version ≥2 必须给出 parent（上一版本的卡 id，附录 A）")
    if parent.strip():
        _no_whitespace(parent, "parent", source)
        if parent == cid:
            raise CardError(
                f"{source}：parent 不能是卡自身（{cid!r}）")

    raw_strategy = data.get("strategy")
    strategy: str | None
    if raw_strategy is None or (isinstance(raw_strategy, str)
                                and not raw_strategy.strip()):
        strategy = None
    else:
        if not isinstance(raw_strategy, str):
            raise CardError(
                f"{source}：strategy 必须是字符串，收到 {raw_strategy!r}")
        strategy = _no_whitespace(raw_strategy, "strategy", source)
        mod, sep, cls = strategy.partition(":")
        if not sep or not mod or not cls:
            raise CardError(
                f"{source}：strategy 必须形如 module:Class"
                f"（如 mt5quant.strategies.library.buy_hold:BuyHold），"
                f"收到 {strategy!r}")

    raw_rule = data.get("selection_rule")
    if raw_rule is None or (isinstance(raw_rule, str)
                            and not raw_rule.strip()):
        selection_rule = "max_net_profit"
    else:
        if not isinstance(raw_rule, str):
            raise CardError(
                f"{source}：selection_rule 必须是字符串，收到 {raw_rule!r}")
        selection_rule = _no_whitespace(raw_rule, "selection_rule", source)
    return family, parent, strategy, selection_rule


def _validate_round_membership(data: dict,
                               source: str) -> tuple[str, str]:
    """校验 round / candidate（T63/D32，铁律 16）：可选但**成对**出现。

    - 两者都缺省 → ("", "")（轮次制之前的旧卡；能否运行由台账 rounds
      表是否为空决定，见 bt 的运行约束）；
    - 只写一个 → CardError（说了轮次不说候选，或反之，运行约束无从
      校验）；
    - round 必须形如 R1/R12，candidate 必须形如 C001（与候选登记同式）；
      候选是否真的在该轮名单内由 bt 运行时对台账 rounds 行校验（装载
      层不读台账）。
    """
    raw_round = data.get("round")
    raw_cand = data.get("candidate")
    if raw_round is None and raw_cand is None:
        return "", ""
    if raw_round is None or raw_cand is None:
        missing = "round" if raw_round is None else "candidate"
        raise CardError(
            f"{source}：round 与 candidate 必须成对出现（缺 {missing!r}）——"
            "轮次内的卡同时声明所属轮次与该轮名单内的候选；轮次制之前"
            "的旧卡两者都不写")
    for key, pattern in (("round", _ROUND_ID_RE),
                         ("candidate", _CANDIDATE_ID_RE)):
        v = data.get(key)
        if not isinstance(v, str) or not pattern.match(v):
            raise CardError(
                f"{source}：字段 {key!r} 必须形如 "
                + ("R1/R12（R+数字）" if key == "round"
                   else "C001（C+至少三位数字）")
                + f"，收到 {v!r}")
    return raw_round, raw_cand


def _validate_params(raw: object, source: str) -> dict:
    if not isinstance(raw, dict):
        raise CardError(f"{source}：[params] 必须是表（可为空表）")
    if len(raw) > MAX_PARAMS:
        raise CardError(
            f"{source}：参数 {len(raw)} 个超过上限 {MAX_PARAMS}（附录 A）")
    out: dict = {}
    for name, grid in raw.items():
        if not isinstance(grid, list) or not grid:
            raise CardError(
                f"{source}：参数 {name!r} 的网格必须是非空数组，收到 {grid!r}")
        for v in grid:
            # 与 ParamSpec 的三类一致：int/float/bool（bool 不是 int 的误放行）
            if isinstance(v, bool) or isinstance(v, (int, float)):
                continue
            raise CardError(
                f"{source}：参数 {name!r} 网格值 {v!r} 必须是数值或布尔")
        out[name] = tuple(grid)
    points = math.prod(len(g) for g in out.values()) if out else 1
    if points > MAX_GRID_POINTS:
        raise CardError(
            f"{source}：网格组合 {points} 超过上限 {MAX_GRID_POINTS}（附录 A）")
    return out


def _validate_table(raw: object, keys: tuple[str, ...], table: str,
                    source: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise CardError(f"{source}：[{table}] 表缺失或不是表")
    unknown = sorted(set(raw) - set(keys))
    if unknown:
        raise CardError(f"{source}：[{table}] 有未知键 {unknown}，合法键 {list(keys)}")
    out = {}
    for k in keys:
        v = raw.get(k)
        if not isinstance(v, str) or not v.strip():
            raise CardError(f"{source}：[{table}].{k} 必须是非空字符串，收到 {v!r}")
        out[k] = v
    return out


def validate_card(data: dict, *, source: str) -> Card:
    """模式校验（fail-fast）：字段齐全且类型正确、参数 ≤4、网格 ≤100。"""
    if not isinstance(data, dict):
        raise CardError(f"{source}：卡内容必须是 TOML 表")
    unknown = sorted(set(data) - set(_TOP_KEYS)
                     - {"rules", "params", "expected", "prob"})
    if unknown:
        raise CardError(
            f"{source}：未知顶层字段 {unknown}（拼写错误会静默丢约束，全部拒绝）；"
            f"合法字段 {list(_TOP_KEYS)}")

    cid = _require_str(data, "id", nonempty=True, source=source)
    if any(ch.isspace() for ch in cid):
        raise CardError(f"{source}：id 不能含空白字符：{cid!r}")

    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise CardError(
            f"{source}：version 必须是 ≥1 的整数（每加一个过滤条件 +1），"
            f"收到 {version!r}")

    family, parent, strategy, selection_rule = _validate_lineage(
        data, cid, version, source)
    round_id, candidate_id = _validate_round_membership(data, source)

    symbols = data.get("symbols")
    if (not isinstance(symbols, list) or not symbols
            or not all(isinstance(s, str) and s.strip() for s in symbols)):
        raise CardError(
            f"{source}：symbols 必须是非空字符串数组，收到 {symbols!r}")

    timeframe = _require_str(data, "timeframe", nonempty=True, source=source)
    if timeframe.upper() not in TIMEFRAME_SECONDS:
        raise CardError(
            f"{source}：timeframe {timeframe!r} 不在 TIMEFRAME_SECONDS 内"
            "（M1/H1/D1 等）")
    # 周期统一大写，台账与引擎口径一致
    timeframe = timeframe.upper()

    # T73A：评估模式与 P 卡专属字段（蓝图 P 线附录 A）
    evaluation_mode = data.get("evaluation_mode", "standard")
    if evaluation_mode not in _EVALUATION_MODES:
        raise CardError(
            f"{source}：evaluation_mode {evaluation_mode!r} 不在枚举 "
            f"{list(_EVALUATION_MODES)} 内（T73A）")
    primary_statistic = data.get("primary_statistic", "")
    segment = data.get("segment", "")
    prob_raw = data.get("prob")
    if evaluation_mode == "probability":
        if not _P_CARD_ID.match(cid):
            raise CardError(
                f"{source}：probability 卡的 id 必须形如 P###-v#（蓝图 P 线"
                f" §5/D45），收到 {cid!r}")
        if primary_statistic != "delta_p_one_sided":
            raise CardError(
                f"{source}：probability 卡的 primary_statistic 恒为 "
                f'"delta_p_one_sided"（P1 主检验），收到 {primary_statistic!r}')
        if segment not in _SEGMENTS:
            raise CardError(
                f"{source}：probability 卡必须声明 segment ∈ "
                f"{list(_SEGMENTS)}（D50：发现段/验证段），收到 {segment!r}")
        if prob_raw is None:
            raise CardError(
                f"{source}：probability 卡必须带 [prob] 段（局面/假设/止损"
                "止盈/网格/接近参数/预期 Δp——蓝图 P 线附录 A）")
        if data.get("params"):
            raise CardError(
                f"{source}：probability 卡不开参数网格（P1 建议不设网格，"
                "蓝图 P 线附录 A）")
        from mt5quant.research.candidates import CandidateError, _validate_prob
        try:
            prob = dict(vars(_validate_prob(prob_raw, source, tuple(symbols))))
        except CandidateError as e:
            raise CardError(str(e)) from e
    else:
        # 审核三 D1：P 卡（P###-v#）在 standard 模式（P2 决策卡）下允许
        # segment（validation）与 [prob]——蓝图附录 A：P2 的卡
        # evaluation_mode="standard"、segment="validation"；H 线卡不带。
        is_p_card = _P_CARD_ID.match(cid) is not None
        if prob_raw is not None and not is_p_card:
            raise CardError(
                f"{source}：[prob] 段只允许出现在 P###-v# 卡（probability "
                "的 P1 卡或 standard+validation 的 P2 决策卡——审核三 D1）")
        if segment and not is_p_card:
            raise CardError(
                f"{source}：segment 只对 P###-v# 卡有意义（D50），"
                f"收到 {segment!r}")
        if segment and segment not in _SEGMENTS:
            raise CardError(
                f"{source}：segment 必须是 {list(_SEGMENTS)} 之一（D50），"
                f"收到 {segment!r}")
        prob = None
        if prob_raw is not None:
            if segment != "validation":
                raise CardError(
                    f"{source}：standard P 卡带 [prob] 时 segment 必须是 "
                    f'"validation"（P2 决策卡——审核三 D1/蓝图附录 A），'
                    f"收到 {segment!r}")
            if primary_statistic and primary_statistic != "max_net_profit":
                raise CardError(
                    f"{source}：standard P 卡的 primary_statistic 按关卡 v3 "
                    f'主检验（缺省 max_net_profit），收到 {primary_statistic!r}')
            from mt5quant.research.candidates import (
                CandidateError, _validate_prob,
            )
            try:
                prob = dict(vars(_validate_prob(prob_raw, source,
                                                tuple(symbols))))
            except CandidateError as e:
                raise CardError(str(e)) from e

    graveyard = data.get("graveyard_checked")
    if not isinstance(graveyard, list) or not all(
            isinstance(g, str) for g in graveyard):
        raise CardError(
            f"{source}：graveyard_checked 必须是字符串数组（可为空），"
            f"收到 {graveyard!r}")

    return Card(
        id=cid, version=version, symbols=tuple(symbols),
        timeframe=timeframe,
        session=_require_str(data, "session", nonempty=False, source=source),
        phenomenon=_require_str(data, "phenomenon", nonempty=True, source=source),
        mechanism=_require_str(data, "mechanism", nonempty=True, source=source),
        should_fail_when=_require_str(data, "should_fail_when",
                                      nonempty=True, source=source),
        falsified_if=_require_str(data, "falsified_if",
                                  nonempty=True, source=source),
        graveyard_checked=tuple(graveyard),
        family=family, parent=parent, strategy=strategy,
        selection_rule=selection_rule,
        round=round_id, candidate=candidate_id,
        evaluation_mode=evaluation_mode,
        primary_statistic=primary_statistic, segment=segment, prob=prob,
        rules=_validate_table(data.get("rules"), _RULES_KEYS, "rules", source),
        params=_validate_params(data.get("params", {}), source),
        expected=_validate_table(
            data.get("expected"),
            _PROB_EXPECTED_KEYS if (evaluation_mode == "probability"
                                    or prob is not None) else _EXPECTED_KEYS,
            "expected", source),
    )


def load_card(path) -> Card:
    """读卡（tomllib）+ 模式校验；文件名主干必须与卡 id 一致（H012-v2.toml）。"""
    p = Path(path)
    if not p.is_file():
        raise CardError(f"卡文件不存在：{p}")
    try:
        with open(p, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise CardError(f"{p}：TOML 解析失败：{e}") from e
    card = validate_card(data, source=str(p))
    if p.stem != card.id:
        raise CardError(
            f"{p}：文件名主干 {p.stem!r} 与卡 id {card.id!r} 不一致"
            f"（附录 A 约定文件名为 {card.id}.toml）")
    return Card(
        id=card.id, version=card.version, symbols=card.symbols,
        timeframe=card.timeframe, session=card.session,
        phenomenon=card.phenomenon, mechanism=card.mechanism,
        should_fail_when=card.should_fail_when, falsified_if=card.falsified_if,
        graveyard_checked=card.graveyard_checked, family=card.family,
        parent=card.parent, strategy=card.strategy,
        selection_rule=card.selection_rule,
        round=card.round, candidate=card.candidate,
        evaluation_mode=card.evaluation_mode,
        primary_statistic=card.primary_statistic, segment=card.segment,
        prob=card.prob,
        rules=card.rules, params=card.params, expected=card.expected,
        path=str(p))


def committed_at(path, *, runner=None) -> datetime | None:
    """卡文件最后一次提交时间（语义见模块 docstring）。

    runner 为注入点（测试用）：runner(args, cwd) -> 带 returncode/stdout
    的对象；缺省用 subprocess.run（cwd=卡所在目录，超时 10s）。
    路径规范化为绝对（H-09）：cwd=p.parent 时传相对完整路径给 git 相当于
    重复目录前缀（cards/H097.toml 在 cards/ 下被解析成 cards/cards/…），
    已提交文件会被误判未提交。绝对输入路径字符串保持不变。
    """
    p = Path(os.path.abspath(str(path)))

    def _default_runner(args, cwd):
        return subprocess.run(args, cwd=cwd, capture_output=True,
                              text=True, timeout=10)

    run = runner or _default_runner
    try:
        proc = run(["git", "log", "-1", "--format=%cI", "--", str(p)],
                   cwd=str(p.parent))
    except FileNotFoundError:      # git 都没有——如实抛
        raise
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    if not out:
        return None               # 未跟踪/从未提交
    return datetime.fromisoformat(out)


def worktree_clean(path, *, runner=None) -> bool:
    """该卡文件在 git 工作区是否干净（`git status --porcelain -- <path>` 为空）。

    T21d/探针 P6：卡文件改了没提交时 committed_at 不变（仍是旧提交时间），
    只查提交时间拦不住——必须同时要求工作区干净。
    - 输出非空（改动/未跟踪/暂存）→ False；
    - git 返回非零（不在仓库、仓库损坏）或超时 → False（无法证明干净，
      fail-closed 从严拒绝）；
    - 只有 git 可执行缺失（FileNotFoundError）如实抛（与 committed_at 同口径）。
    路径规范化为绝对（H-09，与 committed_at 同因）：相对完整路径在
    cwd=p.parent 下重复前缀，已修改文件会被误判干净。
    runner 为注入点（测试用）。
    """
    p = Path(os.path.abspath(str(path)))

    def _default_runner(args, cwd):
        return subprocess.run(args, cwd=cwd, capture_output=True,
                              text=True, timeout=10)

    run = runner or _default_runner
    try:
        proc = run(["git", "status", "--porcelain", "--", str(p)],
                   cwd=str(p.parent))
    except FileNotFoundError:      # git 都没有——如实抛
        raise
    except subprocess.TimeoutExpired:
        return False
    if proc.returncode != 0:
        return False
    return not (proc.stdout or "").strip()


def ensure_card_precedes_run(card_id: str, committed_at: datetime | None,
                             first_run_at: datetime | None, *,
                             worktree_clean: bool | None = None) -> None:
    """先卡后测判定（纯函数；git 与台账交互由调用方完成后注入结果）。

    - committed_at 为 None → CardNotCommittedError；
    - worktree_clean=False（git status --porcelain 非空，探针 P6）→
      CardDirtyWorktreeError；None = 未检查（向后兼容旧调用）；
    - first_run_at 为 None（该卡文件还没跑过）→ 只要已提交且干净即可；
    - committed_at > first_run_at → CardLateCommitError（卡在试验开始后被
      改动过）；按卡文件（id+version）判定——新版本文件有自己的首跑；
    - 两个时间必须同为 aware 或同为 naive，混用直接 ValueError。
    """
    if committed_at is None:
        raise CardNotCommittedError(
            f"卡 {card_id} 尚未提交到 git（先卡后测：预测必须先于测量登记）——"
            "先 git commit 卡文件，再运行回测")
    if worktree_clean is False:
        raise CardDirtyWorktreeError(
            f"卡 {card_id} 文件在工作区有未提交改动（git status --porcelain "
            "非空）——先卡后测被破坏；提交（或还原）该文件后再运行。"
            "版本迭代请新建 H###-v<n>.toml 新文件，旧卡文件不得再改")
    if first_run_at is None:
        return
    if (committed_at.tzinfo is None) != (first_run_at.tzinfo is None):
        raise ValueError(
            "committed_at 与 first_run_at 必须同为 aware 或同为 naive"
            f"（收到 {committed_at.isoformat()} / {first_run_at.isoformat()}）")
    if committed_at > first_run_at:
        raise CardLateCommitError(
            f"卡 {card_id} 的最后提交时间（{committed_at.isoformat()}）晚于"
            f"该卡首次运行（{first_run_at.isoformat()}）——卡在试验开始后被"
            "改动过，先卡后测被破坏；版本迭代请新建 H###-v<n>.toml 新文件，"
            "旧卡留在台账里如实计入谱系的试验次数")


def ensure_strategy_matches(card: Card, strategy_ref: str) -> None:
    """卡上绑定的策略与传入 --strategy 一致才放行（T21d/H4，函数形式提供；
    bt.py 接线由 T21e 完成）。

    - 旧卡（strategy=None，附录 A v1）不限制：策略必须由 --strategy 给出；
    - v2 卡绑定了 strategy：不一致 → CardStrategyMismatchError
      （防止任意策略记到卡名下、谱系的 N 被别的研究污染）。
    """
    if card.strategy is None:
        return
    if card.strategy != strategy_ref:
        raise CardStrategyMismatchError(
            f"卡 {card.id} 绑定策略 {card.strategy}，与传入 --strategy "
            f"{strategy_ref!r} 不一致——拒绝运行（卡谱系只统计绑定策略的"
            "试验；旧卡请在迁移到 v2 时补 strategy 字段）")


# ------------------------------------------- 候选登记（T61/D30；T62 迁移附录 C）

# 附录 C 的 status 枚举 + T61 的 specimen（标定样品：用于校准关卡/成本/
# 引擎/实盘链路，不作研究候选，蓝图 v4 §6.1）
CANDIDATE_STATUSES = ("registered", "screened_pass", "screened_fail",
                      "waiting_data", "withdrawn", "in_round", "specimen")

# 撤回登记（status=withdrawn）约束的卡：bt screen/gate 一律拒绝
WITHDRAWN = "withdrawn"
# 标定样品登记（status=specimen）约束的卡：须 --specimen 且 actor=calibration
SPECIMEN = "specimen"


def candidate_statuses_by_card(candidates_dir=None) -> dict[str, str]:
    """扫 research/candidates/*.toml，返回 source_card -> status 映射。

    T61/D30 的**最小实现**（只认 id/status/source_card 三个字段；T62 迁移
    到蓝图附录 C 的完整装载器——mechanism_class/literature 等届时再校验）：

    - 目录不存在 → 空映射（未登记任何候选的仓库/测试 tmp 不受影响）；
    - 无 source_card 的登记（普通研究候选，如 C001 月末效应）不参与映射、
      不报错——它们不约束任何已有卡；
    - status 不在 CANDIDATE_STATUSES 枚举内 → CardError（fail-fast：登记
      表是撤回/样品运行约束的数据源，坏文件不能静默跳过）；
    - 同一 source_card 出现不同 status → CardError（状态冲突须人工裁决，
      不能随机取一个）。
    """
    if candidates_dir is None:
        candidates_dir = (Path(__file__).resolve().parents[3]
                          / "research" / "candidates")
    d = Path(candidates_dir)
    if not d.is_dir():
        return {}
    out: dict[str, str] = {}
    for p in sorted(d.glob("*.toml")):
        try:
            with open(p, "rb") as f:
                data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise CardError(f"{p}：TOML 解析失败：{e}") from e
        if not isinstance(data, dict):
            raise CardError(f"{p}：候选登记必须是 TOML 表")
        card = data.get("source_card")
        if not isinstance(card, str) or not card.strip():
            continue                    # 普通研究候选：不约束已有卡
        status = data.get("status")
        if status not in CANDIDATE_STATUSES:
            raise CardError(
                f"{p}：status {status!r} 不在枚举 {list(CANDIDATE_STATUSES)} "
                "内（登记表是撤回/样品运行约束的数据源，坏文件不能静默跳过）")
        known = out.get(card)
        if known is not None and known != status:
            raise CardError(
                f"{p}：卡 {card} 的登记状态冲突——已是 {known!r}，本文件写 "
                f"{status!r}；须人工裁决后改登记，不能随机取一个")
        out[card] = status
    return out


# ------------------------------------------------------- 代码指纹（T37/H3/D23）

_LIBRARY_PKG = "mt5quant.strategies.library"
_PROJECT_PKG = "mt5quant"                 # H-12：相对导入解析的项目包边界


class StrategyDirtyError(RuntimeError):
    """策略代码未提交或工作区不干净（D23：试验身份含代码指纹，
    策略文件须已提交且干净）——拒绝运行。"""


def _module_file(name: str) -> Path | None:
    """按模块名定位 .py 文件（不执行模块；找不到/非源文件 → None）。"""
    import importlib.util

    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, AttributeError, ValueError):
        return None
    if spec is None or not spec.origin or not spec.origin.endswith(".py"):
        return None
    return Path(spec.origin).resolve()


def _package_parts(module_name: str, *, is_pkg: bool) -> tuple[str, ...]:
    """模块的 `__package__`（真实 package）——普通模块去末段（a.b.c →
    a.b）；包（__init__.py）取自身（a.b → a.b）。H-12 相对导入解析用。"""
    parts = tuple(module_name.split("."))
    return parts if is_pkg else parts[:-1]


def _imports_within(path: Path, library_dir: Path, module_name: str | None = None,
                    project_root: Path | None = None
                    ) -> list[tuple[Path, str]]:
    """path 源码里 import 的、落在指纹覆盖范围内的模块文件，连同模块名
    返回 [(文件, 模块名)]（H-12 起带名——闭包继续走查时，相对导入要按
    **该文件自己的 package** 解析，不能沿用固定前缀）。

    认两类形式：
    - 绝对 import（`import mt5quant…[.x]`、`from mt5quant…[.x] import y`）：
      library 前缀照旧（T37 口径），**X-05（2026-10-30 审核）起项目包
      mt5quant 内的绝对导入同样进指纹**——真实 P 策略的
      `from mt5quant.prob.labels import …` / `tables` / `decision` 是
      实际评估依赖，改它们必须改变 strategy_sha（旧实现只认 library
      前缀，三个概率核心文件被漏掉——审核探针 dependencies 的反例）。
      真库外（pandas/numpy/mt5data 等）不进指纹，由 git_commit +
      engine_version 覆盖（T37 口径不变）。
    - 相对 import（H-12）：按导入文件的**真实 package**（module_name 的
      `__package__` 语义，importlib 定位文件、模块名逐级上溯）解析——
      旧实现把 level 1/2 锚死 library 根、level≥3 直接丢弃，库外文件的
      相对导入会解析到错误前缀，漏掉 mt5quant.prob 等库外概率核心。
      解析结果落在 library 内（旧行为）**或**项目 mt5quant 包内
      （project_root 之下——相对导入物理上不可能越出顶层包，天然只有
      项目代码）→ 进指纹。

    `from X import y` 的 y 存在同名子模块文件才算（属性导出不展开
    __init__——否则任何库策略的指纹都会经 __init__ 蔓延到整个策略库）。
    module_name/project_root 缺省（旧调用方/测试探针）时无法解析相对
    导入——保守跳过（与旧实现的收录范围一致，不误扩）。
    """
    import ast

    lib = _LIBRARY_PKG
    proj = _PROJECT_PKG
    targets: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def _in_scope(f: Path | None) -> bool:
        if f is None:
            return False
        if library_dir is not None and library_dir in f.parents:
            return True
        return (project_root is not None
                and project_root in f.parents)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                n = alias.name
                if n == lib or n.startswith(lib + "."):
                    targets.add(n)
                elif (proj is not None and (n == proj
                                            or n.startswith(proj + "."))
                      and _in_scope(_module_file(n))):
                    # X-05：项目内绝对 import（mt5quant.prob.labels 等）
                    targets.add(n)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
                if base == lib:
                    for alias in node.names:
                        cand = f"{lib}.{alias.name}"
                        if _module_file(cand) is not None:
                            targets.add(cand)
                elif base.startswith(lib + "."):
                    targets.add(base)
                elif (proj is not None
                      and (base == proj or base.startswith(proj + "."))):
                    # X-05：`from mt5quant.prob.labels import x` —— base
                    # 模块及其 import 的同名子模块都在项目内才收录
                    cands = [base] + [f"{base}.{a.name}" for a in node.names]
                    for cand in cands:
                        if cand in targets or cand == lib \
                                or cand.startswith(lib + "."):
                            continue        # library 分支已处理/去重
                        if _in_scope(_module_file(cand)):
                            targets.add(cand)
            elif module_name:
                # H-12：真实 package 解析——anchor 上溯 level−1 级
                anchor = _package_parts(
                    module_name, is_pkg=(path.name == "__init__.py"))
                if len(anchor) < node.level - 1:
                    continue        # 相对层级越出顶层包（import 系统会报错）
                anchor = anchor[: len(anchor) - (node.level - 1)]
                extra = tuple(node.module.split(".")) if node.module else ()
                base_parts = anchor + extra
                base = ".".join(base_parts)
                if not base:
                    continue
                cands = [base] + [f"{base}.{a.name}" for a in node.names]
                for cand in cands:
                    if cand in targets:
                        continue
                    if _in_scope(_module_file(cand)):
                        targets.add(cand)
    out: list[tuple[Path, str]] = []
    for t in sorted(targets):
        f = _module_file(t)
        if f is not None and _in_scope(f):
            out.append((f, t))
    return out


def strategy_files(strategy_ref: str) -> list[Path]:
    """指纹覆盖的文件集：策略类所在模块文件 + 它（传递）import 的库内
    模块与**相对/绝对导入解析到的项目内模块**（T37/H3/D23 + H-12 +
    X-05——台账 runs.strategy_sha 的哈希对象；真库外依赖（pandas、numpy
    等）不进指纹，由 git_commit + engine_version 覆盖）。

    X-05（2026-10-30 审核）：绝对导入同样按**项目包边界**收录——真实
    P 策略 `from mt5quant.prob.labels import …` 的概率核心（labels/
    tables/decision）是实际评估依赖，落在闭包里才进指纹（旧实现只认
    library 前缀，漏掉三个概率核心文件——审核探针 dependencies）。

    策略模块通过 importlib 定位（不执行）；找不到模块 → ValueError
    （fail-fast：试验身份必须可计算，不能静默退化为不含代码的指纹）。
    """
    mod_name, sep, cls = strategy_ref.partition(":")
    if not sep or not mod_name or not cls:
        raise ValueError(
            f"strategy_ref 必须形如 module:Class，收到 {strategy_ref!r}")
    main = _module_file(mod_name)
    if main is None:
        raise ValueError(
            f"找不到策略模块 {mod_name!r}（strategy_ref={strategy_ref!r}）"
            "——代码指纹（D23）无从计算，拒绝运行")
    lib_init = _module_file(_LIBRARY_PKG)
    library_dir = lib_init.parent if lib_init is not None else None
    proj_init = _module_file(_PROJECT_PKG)          # H-12：项目包根
    project_root = proj_init.parent if proj_init is not None else None
    seen: set[Path] = set()
    files: list[Path] = []
    queue: list[tuple[Path, str]] = [(main, mod_name)]
    while queue:
        f, name = queue.pop(0)
        rf = f.resolve()
        if rf in seen:
            continue
        seen.add(rf)
        files.append(rf)
        if library_dir is None:
            continue
        for dep, dep_name in _imports_within(rf, library_dir, name,
                                             project_root):
            if dep.resolve() not in seen:
                queue.append((dep, dep_name))
    return files


def _rel_to(repo: Path, f: Path) -> str:
    try:
        return f.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return f.as_posix()          # 仓库外：绝对路径如实入指纹


def strategy_fingerprint(strategy_ref: str, repo_root=None, *,
                         runner=None, files=None) -> str:
    """策略代码指纹（D23）：覆盖文件逐个 `git hash-object`，按路径排序拼
    "relpath:blob_hash\\n"，整体 sha256。

    - 排序保证与发现次序无关；relpath 保证改名/挪位置即变指纹；
    - runner 为注入点（测试）；files 为注入文件集（测试/复算——正常调用
      由 strategy_files 解析）；
    - git 返回非零 → RuntimeError（指纹算不出就不允许运行，fail-fast）。
    """
    repo = Path(repo_root) if repo_root is not None else Path.cwd()
    fl = sorted((Path(f) for f in files) if files is not None
                else strategy_files(strategy_ref),
                key=lambda f: f.as_posix())
    if not fl:
        raise ValueError("指纹文件集为空——strategy_files 未解析出任何文件")

    def _default_runner(args, cwd):
        return subprocess.run(args, cwd=cwd, capture_output=True,
                              text=True, timeout=10)

    run = runner or _default_runner
    args = ["git", "hash-object"] + [str(f) for f in fl]
    proc = run(args, cwd=str(repo))
    if proc.returncode != 0:
        raise RuntimeError(
            f"git hash-object 失败（rc={proc.returncode}）："
            f"{(proc.stderr or '').strip() or (proc.stdout or '').strip()}"
            "——代码指纹（D23）算不出，拒绝运行")
    hashes = (proc.stdout or "").strip().splitlines()
    if len(hashes) != len(fl):
        raise RuntimeError(
            f"git hash-object 输出 {len(hashes)} 个哈希，与文件数 "
            f"{len(fl)} 不符——代码指纹（D23）无从计算")
    canonical = "".join(f"{_rel_to(repo, f)}:{h}\n"
                        for f, h in zip(fl, hashes))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ensure_strategy_clean(strategy_ref: str, repo_root=None, *,
                          runner=None, files=None) -> None:
    """指纹覆盖的每个文件都已提交且工作区干净，否则 StrategyDirtyError
    （D23；先卡后测此前只查卡文件，H3 指出策略代码可以改了重跑不涨 N）。

    与 committed_at/worktree_clean 同口径：git 返回非零（不在仓库/仓库
    损坏/超时后按 fail-closed）→ 拒绝；只有 git 可执行缺失如实抛
    FileNotFoundError。runner/files 为注入点（测试）。
    """
    repo = Path(repo_root) if repo_root is not None else Path.cwd()
    fl = [Path(f) for f in files] if files is not None \
        else strategy_files(strategy_ref)
    if not fl:
        raise ValueError("指纹文件集为空——无从检查干净与否")

    def _default_runner(args, cwd):
        return subprocess.run(args, cwd=cwd, capture_output=True,
                              text=True, timeout=10)

    run = runner or _default_runner
    for f in fl:
        rel = _rel_to(repo, f)
        proc = run(["git", "log", "-1", "--format=%cI", "--", str(f)],
                   cwd=str(repo))
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            raise StrategyDirtyError(
                f"策略文件 {rel} 未提交到 git（D23：策略文件须已提交且"
                "干净——试验身份含代码指纹）——先 commit 再运行")
        proc = run(["git", "status", "--porcelain", "--", str(f)],
                   cwd=str(repo))
        if proc.returncode != 0 or (proc.stdout or "").strip():
            raise StrategyDirtyError(
                f"策略文件 {rel} 在工作区有未提交改动（D23）——先提交"
                "（或还原）后再运行；改策略代码请配合新建卡版本"
                " H###-v<n>.toml，旧卡版本不改代码")
