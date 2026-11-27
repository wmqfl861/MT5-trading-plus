"""信号运行器：bar 收盘后触发，用与回测同一份策略代码计算目标仓位与 SL/TP。

口径（docs/blueprint.md §4 与 T14）：
- 时间网格：按 TIMEFRAME_SECONDS 的秒数对 Unix 纪元取整，bar 收盘后满
  trigger_delay_s（容差，等定时刷新进程把已收盘 bar 落盘）才决策；
  墙钟处于 bar 中段时绝不会把正在形成的 bar 当作已收盘（双保险：网格 +
  逐根校验 time+周期 ≤ 墙钟）。
- 只读已收盘 bar；计算窗口 = max(3×lookback_bars, 1000) 根（可配置
  compute_window_bars，T25/M3——窗口不变性问题上实盘侧的兜底：喂远大于
  声明 lookback 的窗口，EMA 类递推统计量与回测全史更接近；H1 计算很便宜）。
  config.lookback_bars 仍是决策的**下限**（低于策略声明 required_data.
  lookback_bars 拒绝启动；已收盘不足 lookback_bars 报"窗口不足"），
  实际喂给策略的是 min(compute_window_bars, 已收盘根数)，
  Decision.bars_used 如实记录（blueprint §4 第 5 条：实盘信号必须与全史
  一致——窗口不变性由 G0 把关，运行器侧尽量给足窗口）。
  数据源两条路（T24/C2）：实盘/真终端干跑用 TerminalBarsSource（进程内
  直接向终端取最近 N 根已收盘 bar，休市=报价陈旧→静默跳过）；parquet
  （store.load_bars）只用于 `--dry-run --offline`——**锁箱（lockbox）豁免**
  ——锁箱约束的是研究侧防前视，实盘决策本来就该用最新已收盘数据，不适用；
  与定时刷新进程（唯一写者）之间只存在原子文件读，无写冲突（R14）。
- 触发网格只有一个（T220）：decide() 用一次 _due_close(now) 选出收盘网格 B，
  按 [B − 周期, B) 判断，并把 B 交给加载器（认这个参数的加载器用它，不认的
  照旧按三个位置参数调用）。运行器和取数源因此对着同一个 B：重试跨过下一
  网格时，下一根 bar 不拿来处置旧的网格。trigger_delay_s 只决定何时开始
  评估，不加到 bar 上。
- 换日停报段内的缺口跳过（T220，只走 TerminalBarsSource 这一路）：报价是
  新的、期望的 bar 重试后仍然没有时，只有所缺的一整段都落在已核实的换日
  停报段里（sessions.toml 的换日锚点起 post_minutes 分钟，限 XAUUSD），且
  一分钟 bar 反证不了那段有行情，才按 RolloverGapSkipError 跳过本网格点
  ——只消费这一个 B，不产生决策。其余一切情形照旧抛"数据滞后"。
- 信号语义与 engine_a 相同：s[i] 为 bar i 收盘时的目标仓位 ∈ {-1,0,1}，
  取窗口末位（= 刚收盘那根）。
- SL/TP 价位口径与 engine_a 相同：入场成交价 ± k × ATR14（ATR 取**信号
  bar**的值，engine_a.atr_series 同源算法；ATR 为 NaN → 不设）。决策时点
  尚无成交价，Decision.sl/tp 为按报价参考价（多用 Ask、空用 Bid）的
  **建议价**；执行器下单后按实际成交价重算并修正（见 executor）。
- Decision 供独立风控进程（T14b）裁决与对账；心跳文件 write_heartbeat
  每轮更新，风控进程按 updated_at_utc 判活。
"""
from __future__ import annotations

import hashlib
import inspect
import math
import os
import time as _time
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from mt5data.store import TIMEFRAME_SECONDS, drop_unclosed, load_bars
from mt5live.fileio import write_json_atomic
from mt5live.monitor import ALERT_HEARTBEAT_WRITE_ERROR, append_alert
from mt5quant.backtest.engine_a import atr_series
from mt5quant.strategies.base import ExitRules

ATR_PERIOD = 14   # 与 engine_a 默认一致（Wilder ATR）

# T25（M3）：计算窗口缺省值——远大于声明的 lookback（H1 计算很便宜）
COMPUTE_WINDOW_MIN_BARS = 1000          # 下限：至少 1000 根
COMPUTE_WINDOW_LOOKBACK_MULTIPLE = 3    # 倍数：3 × lookback_bars

# ---- T220：换日停报段内的缺口跳过（只走终端取数这一路） ----

# 已核实"换日时刻起 post_minutes 分钟没有报价"的品种。只列 XAUUSD：依据是
# 这家券商的 XAUUSD（docs/ops/phase-f-log.md 2026-11-27 条 §3.2，三个夜晚的
# 对照）。别的品种、带后缀的同名品种都不适用——要放行须先有该品种的时段依据。
ROLLOVER_GAP_SYMBOLS = frozenset({"XAUUSD"})
ALERT_ROLLOVER_GAP_SKIP = "ROLLOVER_GAP_SKIP"   # 换日停报段缺口跳过（记录类）

# 一分钟反证最多取多少根（缺口本身不超过停报段长度，正常远用不到这个上限）
_ROLLOVER_GAP_M1_MAX_BARS = 2000

# 内核字节锁（H-05 同 magic 单实例；与 mt5live.fileio 同款选择——
# Windows msvcrt.locking / POSIX flock，互斥由内核强制）
try:
    import msvcrt as _msvcrt
except ImportError:            # pragma: no cover - 非 Windows 回退
    _msvcrt = None
    import fcntl as _fcntl


class MarketClosedError(RuntimeError):
    """市场休市（最新报价陈旧超过阈值）——静默跳过本网格点，不是故障。

    TerminalBarsSource.closed_bars 返回 None 时由 terminal_bars_loader
    抛出；SignalRunner.decide 捕获后返回 None（周末、每日维护时段都走
    这条路，不报"数据滞后"）。
    """


@dataclass(frozen=True)
class RolloverGap:
    """换日停报段里的一次缺口跳过（T220；日志与告警用）。时间一律 naive UTC。"""

    symbol: str
    timeframe: str
    close_time: datetime                 # 本次评估的收盘网格 B（只消费这一个）
    expected_open: datetime              # 触发网格期望的 bar 开盘时刻（= B − 周期）
    latest_bar: datetime                 # 上一根可信的已收盘 bar 开盘时刻
    quote_time: datetime                 # 判定时的最新报价时刻
    window: tuple[datetime, datetime]    # 命中的停报段 [换日时刻, 换日时刻 + post_minutes)


class RolloverGapSkipError(RuntimeError):
    """触发网格期望的 bar 因券商换日停报而不存在——跳过本网格点，不是故障。

    与 MarketClosedError 同样处置（SignalRunner.decide 记下 _last_close、返回
    None），成因不同：报价是新的，只是停报段里没有行情、成不了 bar。只有
    TerminalBarsSource 在本模块的全部核实条件都成立时才抛；任何一条拿不准
    都照旧抛"数据滞后"（ValueError）。
    """

    def __init__(self, gap: RolloverGap):
        super().__init__(
            f"换日停报段缺口：{gap.symbol} {gap.timeframe} 网格 "
            f"{gap.close_time.isoformat()} 期望的 bar "
            f"{gap.expected_open.isoformat()} 不存在（最新已收盘 bar "
            f"{gap.latest_bar.isoformat()}，报价 {gap.quote_time.isoformat()}），"
            f"停报段 [{gap.window[0].isoformat()}, {gap.window[1].isoformat()}) "
            "内没有一分钟 bar——跳过本网格点")
        self.gap = gap


def _accepts_keyword(func, name: str) -> bool:
    """加载器是否接收 name 这个关键字参数（不认的照旧按三个位置参数调用）。"""
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):        # 无法内省的可调用对象：按不认处理
        return False
    for param in params.values():
        if param.kind is param.VAR_KEYWORD:
            return True
        if (param.name == name and param.kind in (
                param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY)):
            return True
    return False


class MagicInstanceLock:
    """同账户/同 magic 的运行器所有权锁（H-05：两份运行器不得并行）。

    1 字节内核锁（Windows msvcrt.locking / POSIX flock），锁文件路径由
    调用方给定（H2-01/1C 起实跑用 :func:`runner_lock_path` 的**身份键**
    路径——``%LOCALAPPDATA%\\mt5-trading-plus\\locks\\runner-<hash>.lock``，
    跨数据目录互斥；影子盘沿用数据目录内 ``runner-<magic>.lock``）：
    互斥由内核仲裁（两个进程、乃至同进程两个 fd 一视同仁），持有进程
    崩溃/退出自动释放——不存在残锁，也不允许"凭 mtime 接管"。acquire
    非阻塞：他人持有立即返回 False，调用方（scripts/live_runner）据此
    拒绝第二份运行器启动。锁文件常驻磁盘（不删除——"释放即删"本身是
    竞态源），内容为持有者 pid（排查用）。
    """

    def __init__(self, path: str):
        self.path = path
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        """当前是否持有锁。"""
        return self._fd is not None

    def acquire(self) -> bool:
        """尝试持锁：成功 True；已被他人持有（或内核拒绝）立即 False。"""
        if self._fd is not None:
            return True
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        fd = os.open(self.path,
                     os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0))
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if _msvcrt is not None:
                _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
            else:                      # pragma: no cover - 非 Windows 回退
                _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        try:                           # 持锁后写持有者 pid（排查用；覆盖式）
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
        except OSError:
            pass                       # 内容仅排查用：写不进不影响互斥语义
        return True

    def release(self) -> None:
        """释放（只解自己 fd 上的锁；未持有时为空操作）。"""
        if self._fd is None:
            return
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
            if _msvcrt is not None:
                _msvcrt.locking(self._fd, _msvcrt.LK_UNLCK, 1)
            else:                      # pragma: no cover - 非 Windows 回退
                _fcntl.flock(self._fd, _fcntl.LOCK_UN)
        except OSError:
            pass                       # 退出路径：尽力解锁，失败交内核回收
        finally:
            os.close(self._fd)
            self._fd = None


# ---- H2-01（1C，R6）：跨数据目录的运行器身份锁 ----
# 审计 H2-01：锁随 live_dir 定位（data/live/runner-<magic>.lock）不足以
# 证明不同数据目录下的同账户同 magic 互斥——两份运行器各持各的目录锁
# 并行，重复下单、互写状态。锁键改按**账户身份**（server+login+magic，
# login 跨券商可撞号必须带上 server），锁文件放 OS 状态目录（所有数据
# 目录共享）。网关连接前读不到 server/login：先取 magic-only 临时锁防
# 双启动竞速，连接后升级身份锁并释放临时锁。

def instance_lock_key(server: str, login: int, magic: int) -> str:
    """身份锁键：sha256(server|login|magic)[:16]（16 hex，文件名安全）。"""
    return hashlib.sha256(
        f"{server}|{login}|{magic}".encode("utf-8")).hexdigest()[:16]


def magic_lock_key(magic: int) -> str:
    """连接前临时锁键（magic-only，与任何身份键不撞）。"""
    return hashlib.sha256(
        f"magic-only|{magic}".encode("utf-8")).hexdigest()[:16]


def runner_locks_dir() -> str:
    """锁文件目录：Windows ``%LOCALAPPDATA%\\mt5-trading-plus\\locks``（R6：
    OS 状态目录，跨数据目录共享）；非 Windows 退回 XDG 数据目录。"""
    base = os.environ.get("LOCALAPPDATA")
    if not base:                              # pragma: no cover - 非 Windows
        base = os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "mt5-trading-plus", "locks")


def runner_lock_path(key: str) -> str:
    """键 → 锁文件全路径（runner-<key>.lock）。"""
    return os.path.join(runner_locks_dir(), f"runner-{key}.lock")


def default_compute_window_bars(lookback_bars: int) -> int:
    """计算窗口缺省值：max(3×lookback_bars, 1000)（T25/M3）。"""
    return max(COMPUTE_WINDOW_LOOKBACK_MULTIPLE * int(lookback_bars),
               COMPUTE_WINDOW_MIN_BARS)

_EPS_VOLUME = 1e-9


def _to_epoch(dt: datetime) -> int:
    if dt.tzinfo is not None:
        raise ValueError(
            f"只接受贴 UTC 标签的 naive datetime（库内约定），收到 tzinfo={dt.tzinfo}")
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def _from_epoch(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class Decision:
    """一次决策的完整记录（时间、bar 收盘时刻、信号、现价、建议订单）。

    风控进程接口约定（T14b）：decision_id = "{symbol}|{TF}|{bar_close_iso}"
    全局确定（同 bar 重算不变），执行器据此幂等去重；volume_lots=None 表示
    手数待风控进程裁决（执行器对非零信号拒绝无手数开仓）。
    """

    decision_id: str
    decided_at: datetime            # 决策墙钟（naive，贴 UTC 标签）
    bar_time: datetime              # 信号 bar 开盘时刻
    bar_close_time: datetime        # 信号 bar 收盘时刻（= 触发网格点）
    signal: int                     # -1/0/1，bar 收盘时的目标仓位
    target_position: int            # = signal（目标仓位语义）
    volume_lots: float | None       # 配置固定手数；None = 风控进程定
    quote_bid: float | None         # 决策时点报价（None = 无行情源）
    quote_ask: float | None
    atr: float | None               # 信号 bar ATR（Wilder14，窗口内计算；不足 → None）
    sl_atr: float | None            # k 值（strategy.exit_rules）
    tp_atr: float | None
    sl: float | None                # 建议止损价（报价参考价口径，执行器按成交价修正）
    tp: float | None
    bars_used: int                  # 实际喂给策略的窗口根数
                                    # （= min(compute_window_bars, 已收盘根数)，T25）

    def to_dict(self) -> dict:
        """心跳/日志用的 JSON 兼容视图。"""

        def _iso(v):
            return v.isoformat() if isinstance(v, datetime) else v
        return {
            "decision_id": self.decision_id,
            "decided_at": _iso(self.decided_at),
            "bar_time": _iso(self.bar_time),
            "bar_close_time": _iso(self.bar_close_time),
            "signal": self.signal,
            "target_position": self.target_position,
            "volume_lots": self.volume_lots,
            "quote_bid": self.quote_bid,
            "quote_ask": self.quote_ask,
            "atr": self.atr,
            "sl_atr": self.sl_atr,
            "tp_atr": self.tp_atr,
            "sl": self.sl,
            "tp": self.tp,
            "bars_used": self.bars_used,
        }


class SignalRunner:
    """每根 bar 收盘（+容差）后产出一个 Decision；未到点返回 None。"""

    def __init__(self, config, strategy, *, bars_loader=load_bars,
                 quote_fn=None, clock=None, atr_func=atr_series,
                 atr_period: int = ATR_PERIOD,
                 compute_window_bars: int | None = None):
        """
        - bars_loader(root, symbol, timeframe) -> DataFrame | None：默认
          store.load_bars 直读（锁箱豁免，见模块 docstring）；可以另收一个
          关键字参数 close_time（T220：运行器选定的收盘网格 B，naive UTC），
          认它的加载器按 B 判断，不认的照旧按三个位置参数调用；
        - quote_fn(symbol) -> (bid, ask)：现价来源（实盘接 gw.market.quote；
          None = 无行情源，Decision 报价与建议 SL/TP 为 None）；
        - clock() -> naive UTC datetime：默认墙钟 UTC。
        - compute_window_bars（T25/M3）：每次决策喂给策略的窗口长度，缺省
          max(3×config.lookback_bars, 1000)；不得小于 config.lookback_bars
          （后者是 required_data.lookback_bars 的下限承载，只作下限校验）。
          已收盘不足计算窗口时喂全部已收盘 bar（仍须 ≥ lookback_bars）。
        """
        from mt5live.config import LiveConfig  # 局部导入避免环

        if not isinstance(config, LiveConfig):
            raise TypeError(f"config 必须是 LiveConfig，收到 {type(config).__name__}")
        required = getattr(strategy.required_data, "lookback_bars", 0) or 0
        if config.lookback_bars < required:
            raise ValueError(
                f"config.lookback_bars={config.lookback_bars} 小于策略声明 "
                f"required_data.lookback_bars={required}（窗口不足会让策略/"
                f"ATR 与回测口径不一致）")
        if compute_window_bars is None:
            compute_window_bars = default_compute_window_bars(config.lookback_bars)
        if (not isinstance(compute_window_bars, int)
                or isinstance(compute_window_bars, bool)
                or compute_window_bars < 1):
            raise ValueError(
                f"compute_window_bars 必须是 ≥1 的整数，收到 {compute_window_bars!r}")
        if compute_window_bars < config.lookback_bars:
            raise ValueError(
                f"compute_window_bars={compute_window_bars} 不得小于 "
                f"config.lookback_bars={config.lookback_bars}（lookback_bars 是"
                f" 策略声明回看的下限，计算窗口只能更大，见 T25/M3）")
        declared_tf = getattr(strategy.required_data, "timeframe", "") or ""
        if declared_tf and declared_tf.upper() != config.timeframe.upper():
            raise ValueError(
                f"策略声明 timeframe={declared_tf!r} 与配置 {config.timeframe!r} 不符")
        self.config = config
        self.strategy = strategy
        self.compute_window_bars = compute_window_bars
        self._loader = bars_loader
        self._quote_fn = quote_fn
        self._clock = clock or (lambda: datetime.now(timezone.utc)
                                .replace(tzinfo=None))
        self._atr_func = atr_func
        self._atr_period = atr_period
        self._params = strategy.validate_params(dict(config.params))
        self._tf_seconds = TIMEFRAME_SECONDS[config.timeframe.upper()]
        self._last_close: datetime | None = None   # 已决策过的最新网格点
        self.market_closed_skips = 0               # 休市静默跳过的网格点数（日志用）
        self._last_market_closed: str | None = None
        # T220：换日停报段内的缺口跳过（最近一次的记录；调用方据此记日志/告警）
        self.gap_skips = 0
        self.last_gap_skip: RolloverGap | None = None
        # 加载器认不认 close_time（T220：把运行器选定的网格 B 交给取数源）
        self._loader_takes_close_time = _accepts_keyword(bars_loader, "close_time")

    # ---- 触发网格 ----

    def _due_close(self, now: datetime) -> datetime | None:
        """最近一个"收盘已满容差秒数"的网格点；未满容差返回 None。"""
        epoch = _to_epoch(now)
        tf = self._tf_seconds
        boundary = epoch - epoch % tf       # 最近网格点 = 上一根 bar 的收盘时刻
        if epoch - boundary < self.config.trigger_delay_s:
            return None
        return _from_epoch(boundary)

    # ---- 决策 ----

    def decide(self, now: datetime | None = None) -> Decision | None:
        """到期则对刚收盘的 bar 产出 Decision；未到点/本 bar 已决策返回 None。

        fail-fast：库内无数据、滞后于网格、窗口不足、策略返回非法信号都抛错
        （由调用方决定重试/告警），绝不带病下单。两个例外由这里消费、返回
        None：休市（MarketClosedError）和换日停报段内的缺口（RolloverGapSkipError，
        T220）——都只消费本次选定的那一个网格点。
        """
        if now is None:
            now = self._clock()
        close_time = self._due_close(now)
        if close_time is None or close_time == self._last_close:
            return None
        cfg = self.config
        tf = self._tf_seconds
        try:
            if self._loader_takes_close_time:
                # T220：两层对着同一个 B——取数源按运行器选定的网格判断，
                # 重试跨过下一网格时不得拿下一根 bar 处置旧的网格
                bars = self._loader(cfg.data_root, cfg.symbol, cfg.timeframe,
                                    close_time=close_time)
            else:
                bars = self._loader(cfg.data_root, cfg.symbol, cfg.timeframe)
        except MarketClosedError as exc:
            # 休市（周末/每日维护，报价陈旧）：静默跳过本网格点（计数的
            # 日志由调用方负责），绝不当"数据滞后"报错
            self._last_close = close_time     # 该网格点已处置，不再重试
            self.market_closed_skips += 1
            self._last_market_closed = str(exc)
            return None
        except RolloverGapSkipError as exc:
            # T220：期望的 bar 因换日停报不存在——只消费这一个网格点，
            # 不产生决策；日志与记录类告警由调用方按 last_gap_skip 写
            self._last_close = close_time
            self.gap_skips += 1
            self.last_gap_skip = exc.gap
            return None
        if bars is None or len(bars) == 0:
            raise ValueError(
                f"无可用 K 线：{cfg.symbol} {cfg.timeframe}（{cfg.data_root} 下"
                f"无数据——先由定时刷新进程写入）")
        closed = bars[pd.to_datetime(bars["time"]) + pd.Timedelta(seconds=tf)
                      <= pd.Timestamp(now)]
        if len(closed) == 0:
            raise ValueError(
                f"无已收盘 K 线：{cfg.symbol} {cfg.timeframe}（墙钟 {now.isoformat()}）")
        expected_open = pd.Timestamp(close_time) - pd.Timedelta(seconds=tf)
        latest = pd.Timestamp(closed["time"].iloc[-1])
        if latest != expected_open:
            raise ValueError(
                f"数据滞后：{cfg.symbol} {cfg.timeframe} 库内最新已收盘 bar "
                f"{latest.isoformat()}，触发网格期望 {expected_open.isoformat()}"
                f"（收盘 {pd.Timestamp(close_time).isoformat()}）——等刷新进程落盘")
        if len(closed) < cfg.lookback_bars:
            raise ValueError(
                f"窗口不足：需要 lookback_bars={cfg.lookback_bars} 根，"
                f"已收盘仅 {len(closed)} 根（先回补历史数据）")
        # T25/M3：喂计算窗口（远大于声明 lookback）；不足时喂全部已收盘 bar
        window = closed.iloc[-self.compute_window_bars:]

        sig = self._generate(window)
        signal = int(sig.iloc[-1])
        atr = self._window_atr(window)
        rules: ExitRules = self.strategy.exit_rules or ExitRules()
        k_sl = rules.stop_loss_atr
        k_tp = rules.take_profit_atr

        bid = ask = None
        sl = tp = None
        if self._quote_fn is not None:
            bid, ask = self._quote_fn(cfg.symbol)
            ref = ask if signal == 1 else bid   # 多头 Ask 入场、空头 Bid 入场
            if signal != 0 and ref is not None and atr is not None:
                if k_sl is not None:
                    sl = ref - k_sl * atr if signal == 1 else ref + k_sl * atr
                if k_tp is not None:
                    tp = ref + k_tp * atr if signal == 1 else ref - k_tp * atr

        decision = Decision(
            decision_id=f"{cfg.symbol}|{cfg.timeframe.upper()}"
                        f"|{close_time.isoformat()}",
            decided_at=now,
            bar_time=expected_open.to_pydatetime(),
            bar_close_time=close_time,
            signal=signal,
            target_position=signal,
            volume_lots=cfg.volume_lots,
            quote_bid=bid, quote_ask=ask,
            atr=atr, sl_atr=k_sl, tp_atr=k_tp, sl=sl, tp=tp,
            bars_used=len(window))
        self._last_close = close_time
        return decision

    # ---- 内部 ----

    def _generate(self, window: pd.DataFrame) -> pd.Series:
        sig = self.strategy.generate_signals(window, self._params)
        if not isinstance(sig, pd.Series):
            raise ValueError("generate_signals 必须返回 pd.Series")
        if len(sig) != len(window) or not sig.index.equals(window.index):
            raise ValueError("信号长度或 index 必须与窗口 bars 一致")
        bad = set(pd.unique(sig)) - {-1, 0, 1}
        if bad:
            raise ValueError(
                f"信号取值必须在 {{-1,0,1}} 内，出现：{sorted(bad, key=str)}")
        return sig

    def _window_atr(self, window: pd.DataFrame) -> float | None:
        """信号 bar 的 ATR（engine_a.atr_series 同源，窗口内计算）。

        Wilder ATR 随窗口收敛：窗口 ≥ 5×周期 时与全史差异可忽略；
        不足（NaN）→ None（不设 SL/TP，与引擎"回看不足不设"一致）。
        """
        value = float(self._atr_func(window, self._atr_period).iloc[-1])
        return None if math.isnan(value) else value


# ---- 终端行情源（T24/C2：运行器进程内直接向终端取最近 N 根已收盘 bar） ----

class TerminalBarsSource:
    """在运行器进程内向终端取最近 N 根已收盘 bar（mt5gw market 服务）。

    - 休市判定：最新报价时刻距今超过 stale_quote_s（默认 120s）→ 返回
      None（周末、每日维护时段——调用方静默跳过并记日志，不是故障）；
    - 收盘判定沿用 store.drop_unclosed（末行收盘 ⇔ time+周期 ≤ 墙钟），
      多取 1 根把正在形成的 bar 剔掉；
    - 有行情但等不到预期 bar（终端/行情服务滞后）：在 max_wait_s 内以
      poll_interval_s 重试，仍没有 → ValueError（"数据滞后"——真故障，
      由运行器记录 last_error，绝不静默跳过）；
    - 唯一的例外（T220）：调用方给了收盘网格 B（close_time）、报价是新的、
      所缺的一整段落在已核实的换日停报段里、一分钟 bar 反证不了那段有行情
      → 抛 RolloverGapSkipError（调用方只消费这一个网格点，不产生决策）。
      需要 session_policy；没有装配就永远不走这个例外。
    """

    def __init__(self, market, *, stale_quote_s: float = 120.0,
                 max_wait_s: float = 10.0, poll_interval_s: float = 0.5,
                 clock=None, session_policy=None):
        if stale_quote_s <= 0 or max_wait_s < 0 or poll_interval_s <= 0:
            raise ValueError(
                f"stale_quote_s/max_wait_s/poll_interval_s 须为正（max_wait_s "
                f"可为 0），收到 {stale_quote_s}/{max_wait_s}/{poll_interval_s}")
        self._market = market
        self._stale_quote_s = float(stale_quote_s)
        self._max_wait_s = float(max_wait_s)
        self._poll_interval_s = float(poll_interval_s)
        self._clock = clock or (lambda: datetime.now(timezone.utc)
                                .replace(tzinfo=None))
        self._session_policy = session_policy     # None = 不启用缺口跳过

    def closed_bars(self, symbol: str, timeframe: str, n: int,
                    *, close_time: datetime | None = None) -> pd.DataFrame | None:
        """最近 n 根已收盘 bar；休市返回 None；等不到期望的 bar 抛错。

        close_time（T220）：运行器选定的收盘网格 B（naive UTC）。给了就对
        着这个 B 判断 [B − 周期, B)——重试跨过下一网格时，下一根 bar 不算数，
        也不拿来处置旧的网格；缺口整体落在已核实的换日停报段里时抛
        RolloverGapSkipError。不给（直接调用）则沿用取数源自己的时钟选网格，
        也永远不走缺口跳过。
        """
        now = self._clock()
        quote = self._market.quote(symbol)
        if (now - quote.time).total_seconds() > self._stale_quote_s:
            return None                       # 休市：静默跳过的信号
        tf = TIMEFRAME_SECONDS[timeframe.upper()]
        if close_time is None:
            epoch = int(now.replace(tzinfo=timezone.utc).timestamp())
            boundary = epoch - epoch % tf     # 最近网格点（= 应已收盘的收盘时刻）
            as_of = now
        else:
            boundary = int(close_time.replace(tzinfo=timezone.utc).timestamp())
            as_of = close_time               # 只认收盘不晚于 B 的 bar
        expected_open = boundary - tf
        expected_ts = pd.Timestamp(expected_open, unit="s")
        boundary_ts = pd.Timestamp(boundary, unit="s")
        # 重试预算按"次数"封顶（≈ max_wait_s / poll_interval_s 轮）：注入的
        # clock 可以是冻结的模拟钟，不能拿它当日程表的死线。
        attempts = max(1, int(self._max_wait_s / self._poll_interval_s) + 1)
        closed = pd.DataFrame()
        for attempt in range(attempts):
            rows = self._market.bars(symbol, timeframe, n + 1)
            df = pd.DataFrame([{
                "time": b.time, "open": b.open, "high": b.high,
                "low": b.low, "close": b.close, "tick_volume": b.tick_volume,
                "spread": b.spread, "real_volume": b.real_volume,
            } for b in rows])
            if close_time is not None and len(df):
                # 开盘不早于 B 的是下一根 bar（重试跨过网格后终端已经交出
                # 它）：不属于本次评估的网格，不得拿来处置旧的网格
                df = df[pd.to_datetime(df["time"]) < boundary_ts]
            closed = drop_unclosed(df, timeframe, as_of)
            if len(closed) and pd.Timestamp(closed["time"].iloc[-1]) >= expected_ts:
                return closed
            if attempt < attempts - 1:
                _time.sleep(self._poll_interval_s)
        latest = (pd.Timestamp(closed["time"].iloc[-1]).isoformat()
                  if len(closed) else "无")
        if close_time is not None and len(closed):
            gap = self._rollover_gap(symbol, timeframe, closed, boundary,
                                     expected_ts)
            if gap is not None:
                raise RolloverGapSkipError(gap)
        raise ValueError(
            f"数据滞后：{symbol} {timeframe} 终端最新已收盘 bar "
            f"{latest}，触发网格期望 {expected_ts.isoformat()}"
            f"——已重试 {self._max_wait_s:.1f}s 仍无（行情服务/终端滞后）")

    def _rollover_gap(self, symbol: str, timeframe: str, closed: pd.DataFrame,
                      boundary: int, expected_ts: pd.Timestamp
                      ) -> RolloverGap | None:
        """T220：期望的 bar 缺失是不是因为换日停报（是 → 缺口记录；否 → None）。

        逐条都要成立；任何一条拿不准都返回 None，调用方照旧抛"数据滞后"：
        1. 品种在 ROLLOVER_GAP_SYMBOLS 里，且装配了 sessions 策略；
        2. 从上一根可信的已收盘 bar 的收盘时刻到 B 这一整段落在停报段
           [换日时刻, 换日时刻 + post_minutes) 里（B 等于窗尾也算在内）；
        3. 此刻重新读的报价与墙钟相差不超过 stale_quote_s——不陈旧，也不
           超前（券商时钟与本机有秒级偏差是常态，按同一个阈值容忍）；报价
           时刻不早于 B。报价落在所缺区间里，它本身就是有行情的证据；
        4. 反证：最近的一分钟 bar 里，[上一根 bar 收盘, B) 一根都没有，并且
           历史覆盖到了上一根可信 bar。取不到、为空或没覆盖到，都算无法判断。
        """
        policy = self._session_policy
        if policy is None or symbol not in ROLLOVER_GAP_SYMBOLS:
            return None
        tf = TIMEFRAME_SECONDS[timeframe.upper()]
        latest_ts = pd.Timestamp(closed["time"].iloc[-1])
        gap_start = latest_ts + pd.Timedelta(seconds=tf)   # 上一根 bar 的收盘时刻
        close_time = _from_epoch(boundary)
        try:
            local_day = close_time.replace(tzinfo=timezone.utc).astimezone(
                ZoneInfo(policy.rollover_tz)).date()
            window = policy.rollover_blackout(local_day)
        except Exception:
            return None                       # 时区/配置算不出停报段：不算核实
        if not (window[0] <= gap_start.to_pydatetime()
                and close_time <= window[1]):
            return None                       # 缺口伸进了正常交易段，或不在停报段里
        try:
            now = self._clock()
            quote = self._market.quote(symbol)
        except Exception:
            return None                       # 报价读不到：不算核实
        if (abs((now - quote.time).total_seconds()) > self._stale_quote_s
                or quote.time < close_time):
            return None
        # 一分钟 bar 从"现在"往前数：B 之后的分钟也占根数（重试跨网格时最多
        # 一整个周期），取少了会看不到缺口里的 bar，所以按到缺口起点的时长取
        minutes = int((now - gap_start.to_pydatetime()).total_seconds() // 60) + 2
        try:
            m1 = self._market.bars(
                symbol, "M1", max(1, min(minutes, _ROLLOVER_GAP_M1_MAX_BARS)))
            if not m1:
                return None                   # 返回空证明不了没有行情（刚重连的终端会返回空）
            m1_open = pd.to_datetime(pd.Series([b.time for b in m1]))
        except Exception:
            return None                       # 接口失败或数据读不了：不算核实
        if m1_open.max() < latest_ts:
            return None                       # 一分钟历史没覆盖到上一根可信 bar：无法判断
        if ((m1_open >= gap_start) & (m1_open < pd.Timestamp(close_time))).any():
            return None                       # 所缺区间里有行情：照旧"数据滞后"
        return RolloverGap(
            symbol=symbol, timeframe=timeframe.upper(),
            close_time=close_time, expected_open=expected_ts.to_pydatetime(),
            latest_bar=latest_ts.to_pydatetime(), quote_time=quote.time,
            window=window)


def terminal_bars_loader(source: TerminalBarsSource, *, n: int):
    """把 TerminalBarsSource 适配成 SignalRunner 的 bars_loader 签名。

    closed_bars 返回 None（休市）→ 抛 MarketClosedError（runner 捕获后
    静默跳过本网格点）。收 close_time（T220：运行器选定的收盘网格 B）并
    交给取数源；换日停报段里的缺口由取数源抛 RolloverGapSkipError，这里
    原样上抛，runner 捕获后同样只消费本网格点。
    """
    def load(root, symbol, timeframe, close_time=None):
        df = source.closed_bars(symbol, timeframe, n, close_time=close_time)
        if df is None:
            raise MarketClosedError(
                f"{symbol} 休市（最新报价陈旧超过阈值），跳过本网格点")
        return df
    return load


# ---- 心跳文件（供独立风控进程判活；格式是 T14b 的对接契约） ----

def default_heartbeat_path(config) -> str:
    """每策略一个心跳文件（T24/M9）：{data_root}/live/heartbeat-<magic>.json。"""
    return config.heartbeat_path or os.path.join(
        config.data_root, "live", f"heartbeat-{config.magic}.json")


def write_heartbeat(path: str, *, config, decision: Decision | None = None,
                    error: str | None = None, dry_run: bool = False,
                    pid: int | None = None,
                    now: datetime | None = None) -> None:
    """原子写心跳 JSON（tmp + fsync + os.replace 带重试，T33 经 fileio）。

    契约（风控/监控进程 T14b 消费，落点 {data_root}/live/heartbeat-<magic>.json，
    T24/M9 起每策略一个文件）：
    - 兼容字段（monitor.read_heartbeat_path 必需）：runner_alive（运行器每轮写
      True；风控进程判活超时会改写为 False）、ts（ISO-8601 带 +00:00）；
    - 扩展字段：pid / updated_at_utc / symbol / timeframe / strategy /
      magic / risk_check_enabled / dry_run / last_decision / last_error；
    - 判活：ts（或 updated_at_utc）距今超过阈值（monitor 默认 120s）→ 告警；
    - last_decision：最近一次决策（decision_id/bar_close_time/signal/
      volume_lots 等，null = 尚无决策）；
    - last_error：最近一轮异常或风控拒绝原因（null = 正常）；
    - dry_run=true 表示未下单（离线干跑或只打印决策）；
    - now：显式时间戳（测试注入模拟时钟）；缺省用真实墙钟。
    """
    ts_dt = (now if now is not None
             else datetime.now(timezone.utc)).replace(tzinfo=timezone.utc)
    ts = ts_dt.isoformat(timespec="milliseconds")
    write_json_atomic(path, {
        "runner_alive": True,        # T14b monitor.read_heartbeat_path 的必需字段
        "ts": ts,                    # 同上；风控进程超时会整文件改写
        "pid": pid if pid is not None else os.getpid(),
        "deployment_sha": os.environ.get("MT5_DEPLOYMENT_SHA", ""),
        "updated_at_utc": ts,
        "symbol": config.symbol,
        "timeframe": config.timeframe,
        "strategy": config.strategy,
        "magic": config.magic,
        "risk_check_enabled": config.risk_check_enabled,
        "dry_run": dry_run,
        "last_decision": decision.to_dict() if decision is not None else None,
        "last_error": str(error) if error else None,
    })


# 心跳写入失败连续多少次后写告警（T33/M1）
HEARTBEAT_FAILURE_ALERT_THRESHOLD = 3


def _default_log(message: str) -> None:
    print(message, flush=True)


class HeartbeatWriter:
    """心跳写入的容错包装（T33/M1）：写失败不退出，下一轮重试。

    旧实现的写心跳在主循环里裸调用——Windows 下目标文件被风控进程占着
    句柄时 os.replace 抛 PermissionError 直接把运行器打崩，随后心跳超时、
    全局停机。语义改为：

    - 瞬时冲突由 fileio.write_json_atomic 内部重试自愈（调用方无感）；
    - 重试预算仍失败（OSError）：记日志、返回 False，**不抛**——下一轮
      再写；连续 ``max_consecutive_failures``（缺省 3）次失败写一条告警
      （HEARTBEAT_WRITE_ERROR，每段连续失败只告警一次，成功即复位）；
    - 告警通道本身写不进去时同样吞掉 OSError（日志已记，别让告警打崩）。
    """

    def __init__(self, path: str, *, config, alert_dir: str | None = None,
                 max_consecutive_failures: int = HEARTBEAT_FAILURE_ALERT_THRESHOLD,
                 log=_default_log):
        self._path = path
        self._config = config
        self._alert_dir = alert_dir
        self._max = int(max_consecutive_failures)
        self._log = log
        self._consecutive = 0
        self._alerted = False

    @property
    def consecutive_failures(self) -> int:
        """当前连续失败计数（成功即归零）。"""
        return self._consecutive

    def write(self, *, decision: Decision | None = None,
              error: str | None = None, dry_run: bool = False,
              now: datetime | None = None, pid: int | None = None) -> bool:
        """写一次心跳：成功 True；失败记日志 False（绝不抛 OSError）。"""
        try:
            write_heartbeat(self._path, config=self._config, decision=decision,
                            error=error, dry_run=dry_run, pid=pid, now=now)
        except OSError as e:
            self._consecutive += 1
            self._log(
                f"心跳写入失败（连续 {self._consecutive} 次）："
                f"{type(e).__name__}: {e}——记日志下一轮重试，不退出"
                f"（{self._path}）")
            if self._consecutive >= self._max:
                self._alert_streak()
            return False
        self._consecutive = 0
        self._alerted = False
        return True

    def _alert_streak(self) -> None:
        """连续失败达到阈值：告警一次（本段内不重复；成功后复位）。"""
        if self._alerted or not self._alert_dir:
            return
        self._alerted = True
        try:
            append_alert(
                self._alert_dir, ALERT_HEARTBEAT_WRITE_ERROR,
                f"心跳写入连续失败 {self._consecutive} 次（{self._path}）——"
                "运行器仍在轮询重试；人工检查 live 目录权限/磁盘", None)
        except OSError as e:      # 告警通道同样不可用：日志已记，不打崩
            self._log(f"告警写入同样失败：{type(e).__name__}: {e}")
