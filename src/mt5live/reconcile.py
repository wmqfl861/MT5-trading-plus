"""mt5live.reconcile（T14/P7 + T24 + T30/D19/D20）：意图 vs actual 持仓对账。

对账口径（T30 起为"意图"语义，修复 C1/P8——服务器端 SL/TP 出场后对账
死锁）：
- 意图：每策略一个 ``data/live/expected-<magic>.json``（文件名沿用 T24，
  风控进程的登记扫描不变），内容为一条 :class:`Intent`：
  - 执行器**下单前**写目标持仓（``status="pending"``，cause=decision_id）；
  - **订单返回后**写实际净持仓（``"confirmed"``；部分成交如实记成交量）；
  - 服务器端保护性出场（reason ∈ {SL,TP,SO}，执行器按 position_id 查
    当日成交识别）→ 意图改为实际净持仓（``"external_exit:<deal>"``）；
  - 人工确认（``scripts/live_ack_external.py``）→ ``"ack_external"``；
  - 兼容读 T24 及以前的旧格式（``{"ts", "positions": [...]}``，逐条记成
    confirmed、cause="legacy"）与旧版单文件 ``expected.json``。
- 实际：账户持仓，**按 magic 过滤**——意图中出现的 magic 才参与对账，
  手动单（magic=0）与未登记策略的持仓不纳入；
- 差分三情形：``missing``（应有而无）、``unexpected``（有而不应）、
  ``mismatch``（净手数不一致，含方向相反）；浮点比较带容差（默认 1e-6 手）；
- **宽限**：``status="pending"`` 且 ``now - ts < grace_s``（默认 60 秒）的
  magic 本轮整体不判——对账进程恰在"已决定、订单未回"的空档里运行时
  不误报（C1 的短暂误报场景）。

暂停指令（T30/D20：按策略隔离）：对账进程只写 ``pause-<magic>.json``；
全局 ``pause.json`` 只留给人工（写入路径保留 ``magic=None``）。闸门
（mt5live.gate.RiskGate）同时读两者，任一 paused 即拒绝开仓。

时间约定：内存中 naive UTC，文件中 ISO 时间带 +00:00（同 mt5live.monitor）。
"""
from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from mt5live.fileio import write_json_atomic
from mt5live.monitor import iso_utc, naive_utc, parse_iso

EXPECTED_FILE = "expected.json"          # 旧版单文件（只兼容读）
EXPECTED_GLOB = "expected-*.json"       # T24 起每策略一个；T30 起内容为意图
INTENT_FILE_FMT = "expected-{magic}.json"   # T30：意图文件名（沿用 T24 前缀，
#   风控进程 registered_magics 的登记扫描不变）
PAUSE_FILE = "pause.json"               # 全局暂停（只留给人工）
PAUSE_MAGIC_FMT = "pause-{magic}.json"  # T30/D20：对账进程按策略写
DEFAULT_VOLUME_TOLERANCE = 1e-6   # 手（volume_step 通常 0.01，纯浮点噪声容差）
DEFAULT_GRACE_S = 60.0            # pending 宽限（秒）：下单在途不判

STATUS_PENDING = "pending"        # 已决定、订单未回
STATUS_CONFIRMED = "confirmed"    # 订单已回 / 出场已解释 / 人工已确认
_STATUSES = (STATUS_PENDING, STATUS_CONFIRMED)

_SIDES = ("buy", "sell")


class ExpectedStateError(RuntimeError):
    """意图文件缺失 / 不是合法 JSON / 字段不合法——拒绝对账。"""


@dataclass(frozen=True)
class Intent:
    """目标持仓意图（T30/D19）：每策略一个 expected-<magic>.json，一条。

    side/volume 描述目标净持仓（volume=0 表示空仓）；status 区分
    "pending"（已决定、订单未回，对账给 grace_s 宽限）与 "confirmed"；
    cause 记这条意图的来历：decision_id | "flatten:<id>" |
    "external_exit:<deal_ticket>" | "ack_external" | "legacy"（旧格式兼容读）
    | "initial"（首次登记）。
    """

    symbol: str
    magic: int
    side: str                # "buy"/"sell"
    volume: float            # 目标净手数（≥0）
    status: str              # "pending"/"confirmed"
    cause: str
    ts: datetime             # naive UTC

    def __post_init__(self):
        if self.side not in _SIDES:
            raise ValueError(f"side 须为 buy/sell，收到 {self.side!r}")
        if self.status not in _STATUSES:
            raise ValueError(f"status 须为 {'/'.join(_STATUSES)}，"
                             f"收到 {self.status!r}")
        if not isinstance(self.magic, int) or isinstance(self.magic, bool):
            raise ValueError(f"magic 须为整数，收到 {self.magic!r}")
        if not isinstance(self.volume, (int, float)) or self.volume < 0:
            raise ValueError(f"volume 须为非负数，收到 {self.volume!r}")
        if not isinstance(self.cause, str) or not self.cause:
            raise ValueError(f"cause 须为非空字符串，收到 {self.cause!r}")

    @property
    def net_volume(self) -> float:
        """有符号净手数（buy 正 / sell 负）。"""
        return float(self.volume if self.side == "buy" else -self.volume)


def write_intent(path: str, intent: Intent) -> None:
    """写意图文件（原子写；格式是执行器与对账进程的集成契约）。"""
    write_json_atomic(path, {
        "symbol": intent.symbol,
        "magic": intent.magic,
        "side": intent.side,
        "volume": float(intent.volume),
        "status": intent.status,
        "cause": intent.cause,
        "ts": iso_utc(intent.ts),
    })


def load_intents(live_dir: str) -> list[Intent]:
    """读 live 目录下全部意图文件：expected-<magic>.json 逐个 + 旧版
    expected.json（兼容）。单文件可以是新格式（单意图对象）或旧格式
    （``{"ts", "positions": [...]}``，T24 及以前）。

    目录里没有任何文件 → ExpectedStateError（运行器先启动）；单个文件
    损坏如实抛错（ExpectedStateError），绝不静默跳过该策略。
    """
    paths = sorted(glob.glob(os.path.join(live_dir, EXPECTED_GLOB)))
    legacy = os.path.join(live_dir, EXPECTED_FILE)
    if os.path.exists(legacy):
        paths.append(legacy)
    if not paths:
        raise ExpectedStateError(
            f"{live_dir} 下没有任何意图文件（expected-<magic>.json / "
            f"{EXPECTED_FILE}）——运行器/执行器先启动写出目标持仓")
    out: list[Intent] = []
    for path in paths:
        out.extend(_load_intent_file(path))
    return out


def _load_intent_file(path: str) -> list[Intent]:
    if not os.path.exists(path):
        raise ExpectedStateError(f"意图文件不存在：{path}（运行器先启动？）")
    with open(path, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise ExpectedStateError(f"{path} 不是合法 JSON：{e}") from e
    if not isinstance(data, dict):
        raise ExpectedStateError(
            f"{path} 顶层须为对象，收到 {type(data).__name__}")
    if "positions" in data:
        return _load_legacy(path, data)      # T24 及以前的旧格式
    try:
        return [Intent(
            symbol=data["symbol"], magic=data["magic"], side=data["side"],
            volume=data["volume"], status=data["status"], cause=data["cause"],
            ts=parse_iso(data["ts"]))]
    except KeyError as e:
        raise ExpectedStateError(f"{path} 缺字段 {e}") from e
    except (TypeError, ValueError) as e:
        raise ExpectedStateError(f"{path} 意图不合法：{e}") from e


def _load_legacy(path: str, data: dict) -> list[Intent]:
    """旧格式（T24 及以前）：{"ts": iso, "positions": [{symbol, magic,
    side, volume}, ...]}——每条记成 confirmed 意图（cause="legacy"）。"""
    try:
        ts = parse_iso(data["ts"])
    except (KeyError, TypeError, ValueError) as e:
        raise ExpectedStateError(
            f"{path} ts 不是合法 ISO 时间：{data.get('ts')!r}") from e
    positions = data["positions"]
    if not isinstance(positions, list):
        raise ExpectedStateError(
            f"{path} positions 须为数组，收到 {type(positions).__name__}")
    out = []
    for i, p in enumerate(positions):
        if not isinstance(p, dict):
            raise ExpectedStateError(f"{path} positions[{i}] 须为对象：{p!r}")
        for key in ("symbol", "magic", "side", "volume"):
            if key not in p:
                raise ExpectedStateError(f"{path} positions[{i}] 缺字段 {key}")
        try:
            out.append(Intent(
                symbol=p["symbol"], magic=p["magic"], side=p["side"],
                volume=p["volume"], status=STATUS_CONFIRMED, cause="legacy",
                ts=ts))
        except (ValueError, TypeError) as e:
            raise ExpectedStateError(f"{path} positions[{i}] 不合法：{e}") from e
    return out


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


def diff_positions(intents, actual, *, now: datetime,
                   grace_s: float = DEFAULT_GRACE_S,
                   volume_tolerance: float = DEFAULT_VOLUME_TOLERANCE,
                   ) -> ReconcileResult:
    """逐 (symbol, magic) 净额差分（意图与实际均可多条同组合，净额合并）。

    ``status="pending"`` 且 ``now - ts < grace_s`` 的 magic 本轮**整体不判**
    （下单在途的宽限——"已决定、订单未回"的空档里对账不误报）；其余
    （confirmed、超时未回的 pending）按目标净额比对。
    """
    now = naive_utc(now)
    in_flight: set[int] = set()
    for i in intents:
        if (getattr(i, "status", None) == STATUS_PENDING
                and (now - naive_utc(i.ts)).total_seconds() < grace_s):
            in_flight.add(i.magic)

    exp_net: dict[tuple[str, int], float] = {}
    for i in intents:
        if i.magic in in_flight:
            continue                      # 本轮不判：整个 magic 跳过
        key = (i.symbol, i.magic)
        exp_net[key] = exp_net.get(key, 0.0) + i.net_volume
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


def write_pause(live_dir: str, magic: int | None, *, paused: bool,
                reason: str | None, diffs=(),
                now: datetime | None = None) -> str:
    """维护暂停指令（T30/D20：按策略隔离）。

    magic 非 None → ``pause-<magic>.json``（对账进程的写路径：不一致 →
    paused=true，恢复 → paused=false）；magic=None → 全局 ``pause.json``
    （只留给人工）。返回写出的路径。
    """
    n = naive_utc(now if now is not None else datetime.now(timezone.utc))
    data = {
        "magic": magic,
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
    name = (PAUSE_FILE if magic is None
            else PAUSE_MAGIC_FMT.format(magic=magic))
    path = os.path.join(live_dir, name)
    write_json_atomic(path, data)
    return path
