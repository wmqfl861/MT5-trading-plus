"""信号运行器：bar 收盘后触发，用与回测同一份策略代码计算目标仓位与 SL/TP。

口径（docs/blueprint.md §4 与 T14）：
- 时间网格：按 TIMEFRAME_SECONDS 的秒数对 Unix 纪元取整，bar 收盘后满
  trigger_delay_s（容差，等定时刷新进程把已收盘 bar 落盘）才决策；
  墙钟处于 bar 中段时绝不会把正在形成的 bar 当作已收盘（双保险：网格 +
  逐根校验 time+周期 ≤ 墙钟）。
- 只读已收盘 bar，窗口恒为配置的 lookback_bars 根（blueprint §4 第 5 条：
  实盘只用最近 lookback_bars 根，结果必须与全史一致——窗口不变性）。
  数据源经 store.load_bars 直读 parquet：**锁箱（lockbox）豁免**——锁箱
  约束的是研究侧防前视，实盘决策本来就该用最新已收盘数据，不适用；
  与定时刷新进程（唯一写者）之间只存在原子文件读，无写冲突（R14）。
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

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from mt5data.store import TIMEFRAME_SECONDS, load_bars
from mt5quant.backtest.engine_a import atr_series
from mt5quant.strategies.base import ExitRules

ATR_PERIOD = 14   # 与 engine_a 默认一致（Wilder ATR）

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
    bars_used: int                  # 实际喂给策略的窗口根数（= lookback_bars）

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
                 atr_period: int = ATR_PERIOD):
        """
        - bars_loader(root, symbol, timeframe) -> DataFrame | None：默认
          store.load_bars 直读（锁箱豁免，见模块 docstring）；
        - quote_fn(symbol) -> (bid, ask)：现价来源（实盘接 gw.market.quote；
          None = 无行情源，Decision 报价与建议 SL/TP 为 None）；
        - clock() -> naive UTC datetime：默认墙钟 UTC。
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
        declared_tf = getattr(strategy.required_data, "timeframe", "") or ""
        if declared_tf and declared_tf.upper() != config.timeframe.upper():
            raise ValueError(
                f"策略声明 timeframe={declared_tf!r} 与配置 {config.timeframe!r} 不符")
        self.config = config
        self.strategy = strategy
        self._loader = bars_loader
        self._quote_fn = quote_fn
        self._clock = clock or (lambda: datetime.now(timezone.utc)
                                .replace(tzinfo=None))
        self._atr_func = atr_func
        self._atr_period = atr_period
        self._params = strategy.validate_params(dict(config.params))
        self._tf_seconds = TIMEFRAME_SECONDS[config.timeframe.upper()]
        self._last_close: datetime | None = None   # 已决策过的最新网格点

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
        （由调用方决定重试/告警），绝不带病下单。
        """
        if now is None:
            now = self._clock()
        close_time = self._due_close(now)
        if close_time is None or close_time == self._last_close:
            return None
        cfg = self.config
        tf = self._tf_seconds
        bars = self._loader(cfg.data_root, cfg.symbol, cfg.timeframe)
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
        window = closed.iloc[-cfg.lookback_bars:]

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


# ---- 心跳文件（供独立风控进程判活；格式是 T14b 的对接契约）----

def default_heartbeat_path(config) -> str:
    return config.heartbeat_path or os.path.join(
        config.data_root, "live", "heartbeat.json")


def write_heartbeat(path: str, *, config, decision: Decision | None = None,
                    error: str | None = None, dry_run: bool = False,
                    pid: int | None = None) -> None:
    """原子写心跳 JSON（tmp + os.replace）。

    契约（风控/监控进程 T14b 消费，落点 {data_root}/live/heartbeat.json）：
    - 兼容字段（monitor.read_heartbeat 必需）：runner_alive（运行器每轮写
      True；风控进程判活超时会改写为 False）、ts（ISO-8601 带 +00:00）；
    - 扩展字段：pid / updated_at_utc / symbol / timeframe / strategy /
      magic / risk_check_enabled / dry_run / last_decision / last_error；
    - 判活：ts（或 updated_at_utc）距今超过阈值（monitor 默认 120s）→ 告警；
    - last_decision：最近一次决策（decision_id/bar_close_time/signal/
      volume_lots 等，null = 尚无决策）；volume_lots=null 且 signal≠0 时由
      风控进程裁决手数后交执行器；
    - last_error：最近一轮异常（null = 正常）；
    - dry_run=true 表示执行器未接入，心跳仅表明信号侧存活。
    """
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    payload = {
        "runner_alive": True,        # T14b monitor.read_heartbeat 的必需字段
        "ts": ts,                    # 同上；风控进程超时会整文件改写
        "pid": pid if pid is not None else os.getpid(),
        "updated_at_utc": ts,
        "symbol": config.symbol,
        "timeframe": config.timeframe,
        "strategy": config.strategy,
        "magic": config.magic,
        "risk_check_enabled": config.risk_check_enabled,
        "dry_run": dry_run,
        "last_decision": decision.to_dict() if decision is not None else None,
        "last_error": str(error) if error else None,
    }
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
