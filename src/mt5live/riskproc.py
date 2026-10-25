"""mt5live.riskproc（T14/P7）：独立风控进程——风控规则（纯函数）与主循环。

铁律（docs/blueprint.md §4 第 10 条）：**风控进程独立于策略进程**。本模块
只做检查与指令输出，绝不直接下单；全平/停机通过 data/live/ 下的文件传递：

- ``data/live/heartbeat-<magic>.json``——每个策略运行器各写一个（T24/M9；
  旧版单文件 heartbeat.json 兼容读取）；风控读全部登记策略的心跳，
  任一超时/缺失 → 置该文件 runner_alive=false 并写 halt 文件（fail-closed：
  运行器未启动也拒绝交易）。
- ``data/live/killSwitch``——一键停机：文件存在即 halt（不自动平仓，平仓由
  人工决定）；恢复 = 删除该文件与 halt.json。
- ``data/live/halt.json``——``{"reason": str, "ts": iso}``，存在即停机；
  首次原因优先（已存在不覆盖），恢复须人工删除。
- ``data/live/commands/flatten.json``（T32/D21 编号协议，读写一律经
  ``mt5live.commands``）——``{"command": "flatten_all", "id": <uuid4>.hex,
  "reason": str, "ts": iso}``；执行器消费后写引用编号的
  ``commands/flatten.ack-<magic>``；风控进程在**所有登记 magic**（心跳/
  expected 文件名）的 ack 编号都对应当前指令后，把指令归档到
  ``commands/archive/`` 并删除这些 ack（旧编号的 ack 不算数——C3 的根因；
  指令读不出 → 不归档并告警）。策略退役用 ``scripts/live_retire.py``
  移走登记文件，使其不再计入仲裁。

规则（check，按序裁决，第一条违规即返回）：
1. 日亏损上限：当日已实现 + 浮动 ≤ -daily_loss_cap_usd → 拒单且
   flatten_all+halt（指令文件由 RiskProcess/主循环落盘）；
2. 停机状态（state.halted / halt 文件 / killSwitch 文件）→ 拒单；
3. 品种白名单；4. 单笔最大手数；5. 每品种最大持仓数（账户内该品种全部
   持仓，不分 magic——风控保护整个账户）；6. 组合美元暴露上限（手数 ×
   contract_size × 现价的 USD 名义，全部品种合并计算）；7. 每分钟最大
   下单数（60 秒滑窗，恰 60 秒前的单不计入）；8. 禁止开仓时段：
   SessionPolicy（config/sessions.toml，T20 唯一定义源）——换日 17:00
   America/New_York 前后配置分钟数（夏令时 21:00 / 冬令时 22:00 GMT，
   DST 感知），BTCUSD 等豁免品种、EURUSD 扩展窗同在该文件；回测引擎
   （engine_a/engine_b）与这里读同一份配置（铁律 v3 第 11 条）。

check 只裁决**开仓**请求；平仓任何时候都不经此门（禁止时段也允许平仓）。
缺数据（品种无现价/合约大小）→ 拒单（reason 前缀 missing_data），
绝不静默放行。
"""
from __future__ import annotations

import json
import math
import os
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from mt5data.sessions import DEFAULT_SESSIONS_PATH, SessionPolicy, load_session_policy
from mt5live.commands import (
    ALERT_COMMAND_UNREADABLE,
    COMMANDS_SUBDIR,
    CommandUnreadableError,
    FLATTEN_FILE,
    archive_if_all_acked,
    write_flatten,
)
from mt5live.fileio import write_json_atomic
from mt5live.monitor import (
    ALERT_HEARTBEAT_TIMEOUT,
    ALERT_RISK_FLATTEN,
    ALERT_RISK_HALT,
    TRADE_ENTRY_KINDS,
    append_alert,
    heartbeat_files,
    heartbeat_status,
    iso_utc,
    mark_heartbeat_stale,
    naive_utc,
    read_heartbeat_path,
)

FREQUENCY_WINDOW_S = 60.0        # 下单频率滑窗宽度

HALT_FILE = "halt.json"
KILL_SWITCH_FILE = "killSwitch"
# 全平指令/确认文件的编号协议与归档实现见 mt5live.commands（T32/D21）；
# COMMANDS_SUBDIR / FLATTEN_FILE 自那里导入（拼路径用）。

DEFAULT_HEARTBEAT_TIMEOUT_S = 120.0


class RiskConfigError(RuntimeError):
    """风控配置文件缺失 / 键不合法 / 类型错误——拒绝启动。"""


def _repo_sessions_path() -> str:
    """仓库 config/sessions.toml 的绝对路径（本文件在 src/mt5live/ 下）。"""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "config", "sessions.toml")


def load_default_session_policy(path: str | None = None) -> SessionPolicy:
    """加载默认禁入策略：显式路径 > 仓库 config/sessions.toml > cwd 相对默认。"""
    for cand in (path, _repo_sessions_path(), DEFAULT_SESSIONS_PATH):
        if cand and os.path.exists(cand):
            return load_session_policy(cand)
    raise RiskConfigError(
        f"找不到 config/sessions.toml（尝试 {_repo_sessions_path()} 与 "
        f"{DEFAULT_SESSIONS_PATH}）——T20 起禁入时段的唯一定义源，必须存在")


# ---- 配置 ----
# T20：禁止开仓时段已迁 config/sessions.toml（SessionPolicy，单一定义源），
# risk.toml 不再有 crypto_symbols / [[forbidden]]——出现即按未知键拒绝。

@dataclass(frozen=True)
class RiskConfig:
    """风控参数（字段与 risk.toml 一一对应；金额均为账户货币 USD）。"""

    version: int
    symbols: tuple[str, ...]              # 品种白名单
    max_lots_per_order: float             # 单笔最大手数
    max_positions_per_symbol: int         # 每品种最大持仓数
    max_usd_exposure: float               # 组合美元暴露上限（USD 名义）
    max_orders_per_minute: int            # 每分钟最大下单数（60 秒滑窗）
    daily_loss_cap_usd: float             # 日亏损上限（正数；≤ -cap 触发全平）

    def __post_init__(self):
        if not self.symbols:
            raise ValueError("symbols 白名单不能为空")
        if not math.isfinite(self.max_lots_per_order) or self.max_lots_per_order <= 0:
            raise ValueError(f"max_lots_per_order 须为正数：{self.max_lots_per_order}")
        if self.max_positions_per_symbol < 1:
            raise ValueError(f"max_positions_per_symbol 须 ≥ 1：{self.max_positions_per_symbol}")
        if not math.isfinite(self.max_usd_exposure) or self.max_usd_exposure <= 0:
            raise ValueError(f"max_usd_exposure 须为正数：{self.max_usd_exposure}")
        if self.max_orders_per_minute < 1:
            raise ValueError(f"max_orders_per_minute 须 ≥ 1：{self.max_orders_per_minute}")
        if not math.isfinite(self.daily_loss_cap_usd) or self.daily_loss_cap_usd <= 0:
            raise ValueError(f"daily_loss_cap_usd 须为正数：{self.daily_loss_cap_usd}")


def _as_int(value, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RiskConfigError(f"{key} 须为整数，收到 {value!r}")
    return value


def _as_float(value, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RiskConfigError(f"{key} 须为数值，收到 {value!r}")
    return float(value)


def _as_symbols(value, key: str) -> tuple[str, ...]:
    if (not isinstance(value, list) or not value
            or not all(isinstance(s, str) and s for s in value)):
        raise RiskConfigError(f"{key} 须为非空字符串数组，收到 {value!r}")
    return tuple(value)


def load_risk_config(path: str) -> RiskConfig:
    """加载 risk.toml（严格校验：缺键/未知键/类型不符 → RiskConfigError）。"""
    if not os.path.exists(path):
        raise RiskConfigError(f"风控配置不存在：{path}")
    with open(path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise RiskConfigError(f"{path} 不是合法 TOML：{e}") from e

    def require(key: str):
        if key not in data:
            raise RiskConfigError(f"风控配置缺键：{key}（{path}）")
        return data.pop(key)

    kw = {
        "version": _as_int(require("version"), "version"),
        "symbols": _as_symbols(require("symbols"), "symbols"),
        "max_lots_per_order": _as_float(
            require("max_lots_per_order"), "max_lots_per_order"),
        "max_positions_per_symbol": _as_int(
            require("max_positions_per_symbol"), "max_positions_per_symbol"),
        "max_usd_exposure": _as_float(
            require("max_usd_exposure"), "max_usd_exposure"),
        "max_orders_per_minute": _as_int(
            require("max_orders_per_minute"), "max_orders_per_minute"),
        "daily_loss_cap_usd": _as_float(
            require("daily_loss_cap_usd"), "daily_loss_cap_usd"),
    }
    if data:
        raise RiskConfigError(
            f"风控配置未知键：{sorted(data)}（{path}）。禁止开仓时段自 T20 起"
            "定义在 config/sessions.toml（SessionPolicy），risk.toml 不再"
            "有 crypto_symbols / [[forbidden]]")
    try:
        return RiskConfig(**kw)
    except ValueError as e:
        raise RiskConfigError(f"{path} 风控配置不合法：{e}") from e


# ---- 检查器的输入输出 ----

@dataclass(frozen=True)
class OrderRequest:
    """开仓请求（执行器在下单前送风控裁决）。"""

    symbol: str
    side: str            # "buy"/"sell"
    volume_lots: float
    magic: int


@dataclass(frozen=True)
class RiskState:
    """检查器所需账户快照（调用方从账户与本地记录组装）。"""

    positions: tuple                      # mt5gw.models.Position（账户全部持仓）
    contract_sizes: Mapping[str, float]   # symbol → 合约大小
    prices: Mapping[str, float]           # symbol → 现价（USD 名义换算）
    recent_order_times: tuple = ()        # 近期下单时刻（naive UTC）
    today_realized: float = 0.0           # 当日已实现盈亏（USD）
    today_floating: float = 0.0           # 当日浮动盈亏（USD）
    halted: bool = False                  # 已处于停机状态


@dataclass(frozen=True)
class RiskDecision:
    """裁决结果：allowed + reason；flatten_all/halt 为指令标志。"""

    allowed: bool
    reason: str
    flatten_all: bool = False   # 日亏触发：要求全平
    halt: bool = False          # 触发后停机（拒绝后续一切开仓）


@dataclass(frozen=True)
class AccountSnapshot:
    """主循环轮询账户得到的快照（当日 = UTC 日，00:00 起）。"""

    currency: str
    positions: tuple
    deals: tuple


# ---- 纯函数检查器 ----

def is_forbidden_open_time(symbol: str, now: datetime,
                           session_policy: SessionPolicy) -> bool:
    """禁止开仓时段判定（T20）：SessionPolicy.entry_allowed 的取反。

    窗口随 DST 移动（换日 17:00 纽约 = 夏 21:00 / 冬 22:00 GMT），
    与回测引擎共用 config/sessions.toml（单一定义源）。
    """
    return not session_policy.entry_allowed(symbol, naive_utc(now))


def _net_sign(side: str) -> float:
    return 1.0 if side == "buy" else -1.0


def usd_notional(symbol: str, volume_lots: float, price: float,
                 contract_size: float) -> float:
    """USD 名义 = 手数 × 合约大小 × 现价（品种以 USD 计价的近似，见模块 docstring）。"""
    return volume_lots * contract_size * price


def _held_usd_exposure(state: RiskState) -> tuple[float, list[str]]:
    total = 0.0
    missing: list[str] = []
    for p in state.positions:
        cs = state.contract_sizes.get(p.symbol)
        if cs is None or not math.isfinite(cs) or cs <= 0:
            missing.append(f"{p.symbol} 缺 contract_size")
            continue
        price = state.prices.get(p.symbol)
        if price is None or not math.isfinite(price) or price <= 0:
            missing.append(f"{p.symbol} 缺 price（现价）")
            continue
        total += usd_notional(p.symbol, p.volume, price, cs)
    return total, missing


def check(order_request: OrderRequest, state: RiskState, config: RiskConfig,
          now: datetime, session_policy: SessionPolicy) -> RiskDecision:
    """纯函数风控检查器（规则与顺序见模块 docstring；now 为 naive UTC）。"""
    # 1. 日亏损上限（最高优先：先于一切规则，触发即全平+停机）
    total = state.today_realized + state.today_floating
    if total <= -config.daily_loss_cap_usd:
        return RiskDecision(
            False,
            f"daily_loss_cap: 当日盈亏 {total:,.2f} USD ≤ "
            f"-{config.daily_loss_cap_usd:,.2f}，触发全平与停机",
            flatten_all=True, halt=True)
    # 2. 停机状态
    if state.halted:
        return RiskDecision(
            False,
            "halted: 风控已停机（killSwitch/心跳超时/日亏触发），"
            "恢复须人工清理 halt.json", halt=True)
    # 3. 品种白名单
    if order_request.symbol not in config.symbols:
        return RiskDecision(
            False, f"symbol_not_whitelisted: {order_request.symbol} "
            f"不在白名单 {list(config.symbols)}")
    # 4. 单笔手数
    v = float(order_request.volume_lots)
    if not math.isfinite(v) or v <= 0:
        return RiskDecision(
            False, f"invalid_volume: 手数须为正数，收到 {order_request.volume_lots!r}")
    if v > config.max_lots_per_order:
        return RiskDecision(
            False, f"max_lots_per_order: {v} 手 > {config.max_lots_per_order} 手")
    # 5. 每品种最大持仓数（账户内该品种全部持仓 + 本笔）
    held = [p for p in state.positions if p.symbol == order_request.symbol]
    if len(held) + 1 > config.max_positions_per_symbol:
        return RiskDecision(
            False, f"max_positions_per_symbol: {order_request.symbol} "
            f"已有 {len(held)} 个持仓，上限 {config.max_positions_per_symbol}")
    # 6. 组合美元暴露（现有持仓 + 本笔；缺数据 → 拒单不放行）
    existing, missing = _held_usd_exposure(state)
    if missing:
        return RiskDecision(False, "missing_data: " + "、".join(missing))
    cs = state.contract_sizes.get(order_request.symbol)
    if cs is None or not math.isfinite(cs) or cs <= 0:
        return RiskDecision(
            False, f"missing_data: {order_request.symbol} 缺 contract_size")
    price = state.prices.get(order_request.symbol)
    if price is None or not math.isfinite(price) or price <= 0:
        return RiskDecision(
            False, f"missing_data: {order_request.symbol} 缺 price（现价）")
    addition = usd_notional(order_request.symbol, v, price, cs)
    if existing + addition > config.max_usd_exposure:
        return RiskDecision(
            False, f"max_usd_exposure: {existing + addition:,.0f} USD > "
            f"{config.max_usd_exposure:,.0f} USD"
            f"（现有 {existing:,.0f} + 本笔 {addition:,.0f}）")
    # 7. 每分钟最大下单数（60 秒滑窗；恰 60 秒前不计入，未来时刻保守计入）
    n = naive_utc(now)
    recent = [t for t in state.recent_order_times
              if (n - t).total_seconds() < FREQUENCY_WINDOW_S]
    if len(recent) + 1 > config.max_orders_per_minute:
        return RiskDecision(
            False, f"max_orders_per_minute: 1 分钟内已提交 {len(recent)} 笔，"
            f"加本笔超上限 {config.max_orders_per_minute} 笔")
    # 8. 禁止开仓时段（config/sessions.toml 的换日窗，DST 感知；只禁开仓）
    if is_forbidden_open_time(order_request.symbol, n, session_policy):
        return RiskDecision(
            False, f"forbidden_session: {order_request.symbol} UTC "
            f"{n:%H:%M} 处于禁止开仓时段（config/sessions.toml 换日窗，"
            f"平仓不受限）")
    return RiskDecision(True, "ok")


def daily_pnl(deals, positions) -> tuple[float, float]:
    """当日已实现 + 浮动盈亏（USD）。

    已实现 = 当日全部成交流水的 profit+commission+swap+fee（只计交易类
    entry，出入金绝不计入）；浮动 = 持仓 profit + swap。
    「当日」按 UTC 自然日计算（00:00 UTC 换日），与交易日（纽约 17:00 换日）
    不同——刻意选择：口径简单，且与回测年化（按 UTC bar 时间）一致（L2）。
    """
    realized = sum(d.profit + d.commission + d.swap + d.fee
                   for d in deals if d.entry in TRADE_ENTRY_KINDS)
    floating = sum(p.profit + p.swap for p in positions)
    return float(realized), float(floating)


# ---- 风控进程（文件级停机 + 主循环） ----

@dataclass(frozen=True)
class CycleResult:
    heartbeat_ok: bool
    heartbeat_reason: str
    halt_written: bool        # 本轮新写入 halt.json
    halt_active: bool         # 本轮结束时 halt.json 存在
    flatten_written: bool     # 本轮新写入 flatten 指令
    daily_loss_breached: bool
    flatten_archived: bool = False   # 本轮把 flatten 指令归档（全员已确认）


def registered_magics(live_dir: str) -> set[int]:
    """登记在案的 magic 集合（T24/M9）：每策略心跳 heartbeat-<magic>.json 与
    执行器目标持仓 expected-<magic>.json 的文件名并集——风控进程据此检查
    所有策略的心跳、确认所有执行器都消费了 flatten 指令。"""
    out: set[int] = set()
    try:
        names = os.listdir(live_dir)
    except OSError:
        return out
    for name in names:
        for prefix in ("heartbeat-", "expected-"):
            if name.startswith(prefix) and name.endswith(".json"):
                body = name[len(prefix):-len(".json")]
                try:
                    out.add(int(body))
                except ValueError:
                    continue
    return out


class RiskProcess:
    """绑定配置与 live 目录的风控进程（检查器 + 周期逻辑，不做任何下单）。"""


class RiskProcess:
    """绑定配置与 live 目录的风控进程（检查器 + 周期逻辑，不做任何下单）。"""

    def __init__(self, config: RiskConfig, *,
                 live_dir: str = os.path.join("data", "live"),
                 heartbeat_timeout_s: float = DEFAULT_HEARTBEAT_TIMEOUT_S,
                 session_policy: SessionPolicy | None = None):
        self.config = config
        self.live_dir = str(live_dir)
        self.heartbeat_timeout_s = float(heartbeat_timeout_s)
        # T20：禁入窗唯一来源 config/sessions.toml；缺省从仓库路径加载
        self.session_policy: SessionPolicy = (
            session_policy if session_policy is not None
            else load_default_session_policy())
        self.halt_path = os.path.join(self.live_dir, HALT_FILE)
        self.kill_switch_path = os.path.join(self.live_dir, KILL_SWITCH_FILE)
        self.commands_dir = os.path.join(self.live_dir, COMMANDS_SUBDIR)
        self.flatten_path = os.path.join(self.commands_dir, FLATTEN_FILE)
        self._hb_alerted = False  # 心跳告警去重（恢复后重置，重启重新告警一次）

    # -- 下单前检查（执行器调用；文件级停机优先于纯函数规则） --

    def check_order(self, order_request: OrderRequest, state: RiskState,
                    now: datetime | None = None) -> RiskDecision:
        n = naive_utc(now if now is not None
                      else datetime.now(timezone.utc))
        if os.path.exists(self.kill_switch_path):
            self._write_halt("kill_switch", n)
            return RiskDecision(
                False, f"kill_switch: {self.kill_switch_path} 存在，一键停机",
                halt=True)
        if os.path.exists(self.halt_path):
            reason = self._read_halt_reason()
            return RiskDecision(False, f"halted: {reason}", halt=True)
        return check(order_request, state, self.config, n, self.session_policy)

    def trigger_flatten_and_halt(self, reason: str,
                                 now: datetime | None = None) -> None:
        """落实全平 + 停机的文件级后果（T24：RiskGate 在执行器路径上调用——
        日亏在开仓检查时被发现也要立即写 flatten.json/halt.json，不等
        风控主循环的下一轮）。已存在的不覆盖（首次原因优先）。"""
        n = naive_utc(now if now is not None
                      else datetime.now(timezone.utc))
        self._write_flatten(reason, n)
        self._write_halt(reason, n)

    # -- 主循环单轮 --

    def run_cycle(self, snapshot: AccountSnapshot | None,
                  now: datetime | None = None) -> CycleResult:
        """一轮：心跳（全部登记策略）→ killSwitch → 账户日亏 → flatten 归档。"""
        n = naive_utc(now if now is not None
                      else datetime.now(timezone.utc))
        if snapshot is not None and snapshot.currency != "USD":
            raise ValueError(
                f"账户货币须为 USD（美元暴露/日亏口径），收到 {snapshot.currency!r}")

        halt_written = False
        flatten_written = False
        daily_breached = False

        # 1) 心跳（T24/M9）：每策略一个 heartbeat-<magic>.json，任一超时/缺失
        #    → 全局停机（fail-closed）；运行器自报停止是干净退出，不停机。
        #    无任何心跳文件时维持旧语义（missing → 停机）。
        worst = None          # (path, status)；优先级 timeout/missing > runner_stopped
        for path in heartbeat_files(self.live_dir):
            hb = read_heartbeat_path(path)
            st = heartbeat_status(hb, n, timeout_s=self.heartbeat_timeout_s)
            if st.ok:
                continue
            if worst is None or worst[1].reason == "runner_stopped":
                worst = (path, st)
        if worst is None and not heartbeat_files(self.live_dir):
            worst = (None, heartbeat_status(None, n,
                                            timeout_s=self.heartbeat_timeout_s))
        if worst is not None:
            path, st = worst
            if st.reason in ("timeout", "missing"):
                if path is not None:
                    hb = read_heartbeat_path(path)
                    if hb is not None:
                        mark_heartbeat_stale(path, hb)
                halt_written |= self._write_halt(
                    "heartbeat_timeout" if st.reason == "timeout"
                    else "heartbeat_missing", n)
                if not self._hb_alerted:
                    who = (f"（{os.path.basename(path)}）" if path is not None
                           else "（无心跳文件）")
                    age = (f"最后心跳 {st.age_s:.0f} 秒前，" if st.age_s is not None
                           else "")
                    append_alert(self.live_dir, ALERT_HEARTBEAT_TIMEOUT,
                                 f"运行器心跳 {st.reason}{who}{age}，已全局停机", n)
                    self._hb_alerted = True
        else:
            self._hb_alerted = False

        heartbeat_ok = worst is None
        heartbeat_reason = worst[1].reason if worst is not None else "ok"

        # 2) 一键停机文件
        if os.path.exists(self.kill_switch_path):
            halt_written |= self._write_halt("kill_switch", n)

        # 3) 账户日亏 → 全平指令 + 停机
        if snapshot is not None:
            realized, floating = daily_pnl(snapshot.deals, snapshot.positions)
            if realized + floating <= -self.config.daily_loss_cap_usd:
                daily_breached = True
                flatten_written |= self._write_flatten("daily_loss_cap", n)
                halt_written |= self._write_halt("daily_loss_cap", n)

        # 4) flatten 指令归档（T32/D21 编号协议）：所有登记 magic 的
        #    ack.id == 当前指令 id 才归档并删除 ack（旧编号不算数——C3），
        #    防执行器漏消费、也防旧 ack 吞掉新指令。
        flatten_archived = False
        if os.path.exists(self.flatten_path):
            flatten_archived = self._archive_flatten_if_acked(n)

        return CycleResult(
            heartbeat_ok=heartbeat_ok, heartbeat_reason=heartbeat_reason,
            halt_written=halt_written, halt_active=os.path.exists(self.halt_path),
            flatten_written=flatten_written, daily_loss_breached=daily_breached,
            flatten_archived=flatten_archived)

    def _archive_flatten_if_acked(self, now: datetime) -> bool:
        """T32/D21：所有登记 magic 的 ack 编号对应当前指令 → 归档并删 ack
        （mt5live.commands.archive_if_all_acked）；指令读不出 → 告警、
        不归档（执行器按有指令处理，等人工清理）。"""
        magics = registered_magics(self.live_dir)
        try:
            return archive_if_all_acked(self.live_dir, magics, now)
        except CommandUnreadableError as e:
            append_alert(
                self.live_dir, ALERT_COMMAND_UNREADABLE,
                f"全平指令读不出，暂不归档（{e}）——执行器按有指令处理；"
                "人工核对 commands/flatten.json", now)
            return False

    # -- 指令文件 --

    def _write_halt(self, reason: str, now: datetime) -> bool:
        """写 halt.json；已存在则保留首次原因不覆盖。返回是否本次写入。"""
        if os.path.exists(self.halt_path):
            return False
        os.makedirs(self.live_dir, exist_ok=True)
        write_json_atomic(self.halt_path, {"reason": reason, "ts": iso_utc(now)})
        append_alert(self.live_dir, ALERT_RISK_HALT, f"风控停机：{reason}", now)
        return True

    def _write_flatten(self, reason: str, now: datetime) -> bool:
        """写全平指令（T32/D21 编号协议，mt5live.commands.write_flatten）；
        已存在（执行器未消费）→ 不重写（首次原因优先）。返回是否本次写入。

        已有指令读不出 → 不覆盖、写告警（执行器仍会按"有指令"处理）。
        """
        existed = os.path.exists(self.flatten_path)
        try:
            write_flatten(self.live_dir, reason=reason, now=now)
        except CommandUnreadableError as e:
            append_alert(
                self.live_dir, ALERT_COMMAND_UNREADABLE,
                f"全平指令读不出，保留原文件不重写（{e}）——人工核对 "
                "commands/flatten.json", now)
            return False
        if existed:
            return False
        append_alert(self.live_dir, ALERT_RISK_FLATTEN,
                     f"全平指令：{reason}", now)
        return True

    def _read_halt_reason(self) -> str:
        try:
            with open(self.halt_path, encoding="utf-8") as f:
                data = json.load(f)
            return str(data.get("reason", "halt 文件存在"))
        except (OSError, ValueError):
            return "halt 文件存在"
