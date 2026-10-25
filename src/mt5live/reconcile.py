"""mt5live.reconcile（T14/P7 + T24）：expected vs actual 持仓对账。

对账口径：
- expected：T24 起每策略一个 ``data/live/expected-<magic>.json``（执行器
  每轮写入的目标持仓）；``load_expected_dir`` 合并全部 ``expected-*.json``
  （含旧版单文件 ``expected.json``），每 (symbol, magic) 一条净持仓
  （buy 正 / sell 负的净手数），缺项或 volume=0 表示该组合应为空仓；
  **完全未在任何 expected 文件中出现的 magic 视为不受管**（手动单等）——
  因此执行器每轮都写（空仓写 volume=0），否则该 magic 的实际持仓不参与
  对账；
- actual：账户实际持仓，**按 magic 过滤**——expected 中出现的 magic 才参与
  对账，手动单（magic=0）与其他策略的持仓不纳入；
- 差分三情形：``missing``（应有而无）、``unexpected``（有而不应）、
  ``mismatch``（净手数不一致，含方向相反）；浮点比较带容差（默认 1e-6 手）。

暂停策略指令：``data/live/pause.json``（``{"paused": bool, "reason": str|null,
"ts": iso, "diffs": [...]}``）——不一致时 paused=true，恢复一致时写回
paused=false；执行器闸门（mt5live.gate.RiskGate）读 paused 字段决定是否
暂停开仓。

时间约定：内存中 naive UTC，文件中 ISO 时间带 +00:00（同 mt5live.monitor）。
"""
from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from mt5gw.snapshot import write_json_atomic
from mt5live.monitor import iso_utc, naive_utc, parse_iso

EXPECTED_FILE = "expected.json"
EXPECTED_GLOB = "expected-*.json"   # T24：每策略一个目标持仓文件
PAUSE_FILE = "pause.json"
DEFAULT_VOLUME_TOLERANCE = 1e-6  # 手（volume_step 通常 0.01，纯浮点噪声容差）

_SIDES = ("buy", "sell")


class ExpectedStateError(RuntimeError):
    """预期持仓文件缺失 / 不是合法 JSON / 字段不合法——拒绝对账。"""


@dataclass(frozen=True)
class ExpectedPosition:
    """目标持仓：每 (symbol, magic) 一条，volume=0 表示空仓。"""

    symbol: str
    magic: int
    side: str  # "buy"/"sell"
    volume: float

    def __post_init__(self):
        if self.side not in _SIDES:
            raise ValueError(f"side 须为 buy/sell，收到 {self.side!r}")
        if not isinstance(self.magic, int) or isinstance(self.magic, bool):
            raise ValueError(f"magic 须为整数，收到 {self.magic!r}")
        if not isinstance(self.volume, (int, float)) or self.volume < 0:
            raise ValueError(f"volume 须为非负数，收到 {self.volume!r}")

    @property
    def net_volume(self) -> float:
        """有符号净手数（buy 正 / sell 负）。"""
        return float(self.volume if self.side == "buy" else -self.volume)


@dataclass(frozen=True)
class ExpectedState:
    ts: datetime
    positions: tuple[ExpectedPosition, ...]


def write_expected(path: str, ts: datetime, positions) -> None:
    """运行器/执行器写目标持仓（原子写；格式是对账双方的集成契约）。"""
    write_json_atomic(path, {
        "ts": iso_utc(ts),
        "positions": [
            {"symbol": p.symbol, "magic": p.magic, "side": p.side,
             "volume": float(p.volume)}
            for p in positions
        ],
    })


def load_expected(path: str) -> ExpectedState:
    """读目标持仓文件；缺失/不合法 → ExpectedStateError（如实抛错）。"""
    if not os.path.exists(path):
        raise ExpectedStateError(f"预期持仓文件不存在：{path}（运行器先启动？）")
    with open(path, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise ExpectedStateError(f"{path} 不是合法 JSON：{e}") from e
    if not isinstance(data, dict):
        raise ExpectedStateError(f"{path} 顶层须为对象，收到 {type(data).__name__}")
    if "ts" not in data or "positions" not in data:
        raise ExpectedStateError(f"{path} 缺 ts/positions 字段")
    try:
        ts = parse_iso(data["ts"])
    except (TypeError, ValueError) as e:
        raise ExpectedStateError(
            f"{path} ts 不是合法 ISO 时间：{data['ts']!r}") from e
    if not isinstance(data["positions"], list):
        raise ExpectedStateError(
            f"{path} positions 须为数组，收到 {type(data['positions']).__name__}")
    parsed = []
    for i, p in enumerate(data["positions"]):
        if not isinstance(p, dict):
            raise ExpectedStateError(f"{path} positions[{i}] 须为对象：{p!r}")
        for key in ("symbol", "magic", "side", "volume"):
            if key not in p:
                raise ExpectedStateError(f"{path} positions[{i}] 缺字段 {key}")
        try:
            parsed.append(ExpectedPosition(
                symbol=p["symbol"], magic=p["magic"], side=p["side"],
                volume=p["volume"]))
        except (ValueError, TypeError) as e:
            raise ExpectedStateError(f"{path} positions[{i}] 不合法：{e}") from e
    return ExpectedState(ts=ts, positions=tuple(parsed))


def load_expected_dir(live_dir: str) -> ExpectedState:
    """合并 live 目录下全部 expected 文件（T24：expected-<magic>.json 逐个
    + 旧版 expected.json），供对账进程一次读入所有受管 magic。

    目录里没有任何 expected 文件 → ExpectedStateError（运行器先启动）；
    单个文件损坏如实抛错（ExpectedStateError），绝不静默跳过该策略。
    """
    paths = sorted(glob.glob(os.path.join(live_dir, EXPECTED_GLOB)))
    legacy = os.path.join(live_dir, EXPECTED_FILE)
    if os.path.exists(legacy):
        paths.append(legacy)
    if not paths:
        raise ExpectedStateError(
            f"{live_dir} 下没有任何 expected 文件（expected-<magic>.json / "
            f"{EXPECTED_FILE}）——运行器/执行器先启动写出目标持仓")
    merged_ts: datetime | None = None
    merged: list[ExpectedPosition] = []
    for path in paths:
        state = load_expected(path)
        merged.extend(state.positions)
        if merged_ts is None or state.ts > merged_ts:
            merged_ts = state.ts
    return ExpectedState(ts=merged_ts, positions=tuple(merged))


@dataclass(frozen=True)
class ReconcileDiff:
    kind: str          # "missing" / "unexpected" / "mismatch"
    symbol: str
    magic: int
    expected_net: float  # 有符号净手数（buy 正 / sell 负）
    actual_net: float
    detail: str


@dataclass(frozen=True)
class ReconcileResult:
    ok: bool
    diffs: tuple[ReconcileDiff, ...]

    @property
    def pause(self) -> bool:
        """不一致 → 暂停策略。"""
        return bool(self.diffs)


def _actual_net_volume(p) -> float:
    return float(p.volume if p.side == "buy" else -p.volume)


def diff_positions(expected, actual, *,
                   volume_tolerance: float = DEFAULT_VOLUME_TOLERANCE,
                   ) -> ReconcileResult:
    """逐 (symbol, magic) 净额差分（expected 与 actual 均可多条同组合，净额合并）。"""
    exp_net: dict[tuple[str, int], float] = {}
    for e in expected:
        key = (e.symbol, e.magic)
        exp_net[key] = exp_net.get(key, 0.0) + e.net_volume
    managed_magics = {magic for _, magic in exp_net}

    act_net: dict[tuple[str, int], float] = {}
    for p in actual:
        if p.magic not in managed_magics:
            continue  # 手动单（magic=0）与未声明 magic 的持仓不参与本次对账
        key = (p.symbol, p.magic)
        act_net[key] = act_net.get(key, 0.0) + _actual_net_volume(p)

    diffs: list[ReconcileDiff] = []
    for key in sorted(set(exp_net) | set(act_net)):
        e = exp_net.get(key, 0.0)
        a = act_net.get(key, 0.0)
        if abs(e - a) <= volume_tolerance:
            continue
        symbol, magic = key
        if abs(e) <= volume_tolerance:
            kind = "unexpected"   # 有持仓而不应持有
        elif abs(a) <= volume_tolerance:
            kind = "missing"      # 应持有而账户没有
        else:
            kind = "mismatch"     # 净手数不一致（含方向相反）
        diffs.append(ReconcileDiff(
            kind=kind, symbol=symbol, magic=magic,
            expected_net=e, actual_net=a,
            detail=f"{kind}: {symbol} magic {magic} "
            f"期望净 {e:+.2f} 手，实际净 {a:+.2f} 手"))
    return ReconcileResult(ok=not diffs, diffs=tuple(diffs))


def write_pause(live_dir: str, *, paused: bool, reason: str | None,
                diffs=(), now: datetime | None = None) -> str:
    """维护 data/live/pause.json：不一致 → paused=true；恢复 → paused=false。"""
    n = naive_utc(now if now is not None else datetime.now(timezone.utc))
    data = {
        "paused": bool(paused),
        "reason": reason,
        "ts": iso_utc(n),
        "diffs": [
            {"kind": d.kind, "symbol": d.symbol, "magic": d.magic,
             "expected_net": d.expected_net, "actual_net": d.actual_net,
             "detail": d.detail}
            for d in diffs
        ],
    }
    path = os.path.join(live_dir, PAUSE_FILE)
    write_json_atomic(path, data)
    return path
