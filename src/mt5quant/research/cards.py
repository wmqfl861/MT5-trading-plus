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

import math
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
_TOP_KEYS = ("id", "family", "version", "parent", "strategy",
             "selection_rule", "symbols", "timeframe", "session", "phenomenon",
             "mechanism", "should_fail_when", "falsified_if",
             "graveyard_checked")

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
    unknown = sorted(set(data) - set(_TOP_KEYS) - {"rules", "params", "expected"})
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
        rules=_validate_table(data.get("rules"), _RULES_KEYS, "rules", source),
        params=_validate_params(data.get("params", {}), source),
        expected=_validate_table(data.get("expected"), _EXPECTED_KEYS,
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
        selection_rule=card.selection_rule, rules=card.rules,
        params=card.params, expected=card.expected, path=str(p))


def committed_at(path, *, runner=None) -> datetime | None:
    """卡文件最后一次提交时间（语义见模块 docstring）。

    runner 为注入点（测试用）：runner(args, cwd) -> 带 returncode/stdout
    的对象；缺省用 subprocess.run（cwd=卡所在目录，超时 10s）。
    """
    p = Path(path)

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
    runner 为注入点（测试用）。
    """
    p = Path(path)

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
