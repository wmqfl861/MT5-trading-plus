"""假设卡：研究假设的"预测先行"登记（模板见 docs/blueprint.md 附录 A）。

铁律（先卡后测）：
- 讲不出机制的假设不测；预期（expected）先写死，再和结果对照；
- 卡在第一次回测前提交到 git，提交时间是"预测先于测量"的证据；
- 卡的参数 ≤4 个、网格笛卡尔积 ≤100 组（与 strategies.base 同限）。

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
- 该卡（按 card_id）在台账中已有运行，且 committed_at 晚于首次运行时间 →
  拒绝：说明卡文件在试验开始后被改动过（哪怕是升版本），预测已非"先行"。
  迭代假设请开**新卡**（新 id，旧卡如实留在台账里计入试验次数）。
"""
from __future__ import annotations

import math
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
_TOP_KEYS = ("id", "version", "symbols", "timeframe", "session", "phenomenon",
             "mechanism", "should_fail_when", "falsified_if",
             "graveyard_checked")


class CardError(ValueError):
    """卡内容不符合模式（缺字段/参数越界/TOML 坏/文件名与 id 不一致）。"""


class CardNotCommittedError(RuntimeError):
    """卡尚未提交到 git——先卡后测被破坏（未提交）。"""


class CardLateCommitError(RuntimeError):
    """卡的提交时间晚于该卡首次运行——先卡后测被破坏（事后改动）。"""


@dataclass(frozen=True)
class Card:
    """一张已通过模式校验的假设卡（字段见附录 A）。"""

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


def _require_str(data: dict, key: str, *, nonempty: bool, source: str) -> str:
    v = data.get(key)
    if not isinstance(v, str):
        raise CardError(f"{source}：字段 {key!r} 必须是字符串，收到 {v!r}")
    if nonempty and not v.strip():
        raise CardError(f"{source}：字段 {key!r} 不能为空")
    return v


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
        rules=_validate_table(data.get("rules"), _RULES_KEYS, "rules", source),
        params=_validate_params(data.get("params", {}), source),
        expected=_validate_table(data.get("expected"), _EXPECTED_KEYS,
                                 "expected", source),
    )


def load_card(path) -> Card:
    """读卡（tomllib）+ 模式校验；文件名主干必须与卡 id 一致（H012.toml）。"""
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
        graveyard_checked=card.graveyard_checked, rules=card.rules,
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


def ensure_card_precedes_run(card_id: str, committed_at: datetime | None,
                             first_run_at: datetime | None) -> None:
    """先卡后测判定（纯函数；git 与台账交互由调用方完成后注入结果）。

    - committed_at 为 None → CardNotCommittedError；
    - first_run_at 为 None（该卡还没跑过）→ 只要已提交即可；
    - committed_at > first_run_at → CardLateCommitError（卡在试验开始后被改）；
    - 两个时间必须同为 aware 或同为 naive，混用直接 ValueError。
    """
    if committed_at is None:
        raise CardNotCommittedError(
            f"卡 {card_id} 尚未提交到 git（先卡后测：预测必须先于测量登记）——"
            "先 git commit 卡文件，再运行回测")
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
            "改动过，先卡后测被破坏；迭代请开新卡（新 id），旧卡留在台账里"
            "如实计入试验次数")
