"""T26 多笔 A/B 对账参考策略：MACrossRef 同逻辑 SMA 交叉 + ATR 止损止盈。

【只用于执行口径对账，不是研究策略】本模块是 ops 参考策略（第四轮计划
T26 / 审核 H7）：多笔 A/B 对账（scripts/reconcile_ab.py）此前只有单笔
BuyHold（docs/reports/reconciliation-2026-10-25.md），止损/止盈、空头、
反手、after_signal_change 再入路径都没有对账证据。本策略补齐这些路径。
**禁止用于研究**：不进 mt5quant.strategies.library、不开研究卡、不进假设
谱系；对账读锁箱期数据须显式 --ops-ack（脚本在台账记 actor=ops 供审计）。

信号（与 mql5/Experts/MACrossRef.mq5 逐行对应，只用已收盘 bar）：

    fast = bars["close"].rolling(fast_n).mean()    # 收盘价 SMA（无前视）
    slow = bars["close"].rolling(slow_n).mean()
    signal = np.sign(fast - slow)；rolling NaN（预热不足）→ 0

- bar i 收盘产生目标仓位 ∈ {+1, 0, -1}，引擎在 bar i+1 开盘调仓（A 层
  次根开盘标签；B 层收盘后第一笔 tick——两层同一信号源、不同执行模型）；
- 金叉做多、死叉做空（多空都做）；fast == slow 时信号 0（np.sign(0)=0，
  与 EA 的 f[0]==s[0] 分支一致）；只看收盘价（扰动 high/low 不改信号）。

出场（引擎层，ExitRules——出场语义只有一份实现，引擎与实盘共用）：
- 反向交叉 = signal 出场（含反手"先平后开"）；
- stop_loss_atr / take_profit_atr = 入场成交价 ± 系数 × 信号 bar 的
  ATR14（Wilder，见 backtest.engine_a.atr_series；预热不足 ATR 为 NaN
  时不设 SL/TP，引擎语义）。

参数（4 个，恰好在上限内；fast/slow 驱动信号，SL/TP 见下）：
- fast/slow：走 params_schema。10/30 = MACrossRef.mq5 写死的口径
  （XAUUSD H1）；5/15 = 高换手变体（USTEC M5，保对账交易数）；
  2/4 = 手造测试的最小窗口。
- stop_loss_atr/take_profit_atr：**引擎只读 ExitRules**（类/实例属性），
  同时声明为参数是为了 --params / 台账 / manifest 里留档；两者不一致时
  generate_signals 直接抛 ValueError——参数副本与引擎规则禁止静默分叉。
  构造器可改（测试与变体用），缺省 2.0 / 3.0（让 stop / take_profit
  出场在对账窗口内实际发生）。

再入场变体：MaCrossATRAfterSignalChange = 同策略 + reentry=
"after_signal_change"（保护性出场后信号离开原方向前禁同向重入，R7），
用于对账 A/B 两层的 blocked_dir 状态机。

窗口不变性：rolling(slow_n) 只依赖最近 slow_n 根收盘价，故
LOOKBACK_BARS = max(slow 网格) = 30 时截窗重算与全史逐位一致
（G0 窗口不变性的依据；放大 slow 网格上界必须同步放大 LOOKBACK_BARS）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from mt5quant.strategies.base import BaseStrategy, ExitRules, ParamSpec, RequiredData

LOOKBACK_BARS = 30            # = max(slow 网格)（rolling 窗口；勿单独放大网格）

DEFAULT_STOP_LOSS_ATR = 2.0
DEFAULT_TAKE_PROFIT_ATR = 3.0


class MaCrossATR(BaseStrategy):
    """收盘价 SMA 快慢线交叉（多空都做）+ ATR 止损止盈（引擎层）。

    ops 参考策略：只用于执行口径对账（见模块 docstring），不进策略库。
    参数网格 fast(2,5,10) × slow(4,15,30) × stop_loss_atr(1,2,3) ×
    take_profit_atr(2,3,6) = 81 组 ≤ 100；实跑只用
    {fast:10, slow:30, stop_loss_atr:2.0, take_profit_atr:3.0}（XAUUSD H1）
    与 {fast:5, slow:15, ...}（USTEC M5 高换手变体）。
    """

    params_schema = (
        ParamSpec(name="fast", type="int", grid=(2, 5, 10)),
        ParamSpec(name="slow", type="int", grid=(4, 15, 30)),
        ParamSpec(name="stop_loss_atr", type="float", grid=(1.0, 2.0, 3.0)),
        ParamSpec(name="take_profit_atr", type="float", grid=(2.0, 3.0, 6.0)),
    )
    required_data = RequiredData(
        symbols=("XAUUSD", "USTEC"),
        timeframe="",            # 周期无关（H1/M5 都跑）；由调用方传入引擎
        lookback_bars=LOOKBACK_BARS,
    )
    exit_rules = ExitRules(stop_loss_atr=DEFAULT_STOP_LOSS_ATR,
                           take_profit_atr=DEFAULT_TAKE_PROFIT_ATR,
                           reentry="immediate")

    def __init__(self, stop_loss_atr: float = DEFAULT_STOP_LOSS_ATR,
                 take_profit_atr: float = DEFAULT_TAKE_PROFIT_ATR,
                 reentry: str = "immediate"):
        """实例级出场规则（遮蔽类属性；引擎读 strategy.exit_rules）。

        SL/TP 与 reentry 在此一定源（ExitRules 单一定义），测试与变体经
        构造器覆盖；CLI 无参构造得到缺省 2.0/3.0/immediate。
        """
        self.exit_rules = ExitRules(
            stop_loss_atr=float(stop_loss_atr),
            take_profit_atr=float(take_profit_atr),
            reentry=reentry)

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        p = self.validate_params(dict(params))
        if int(p["fast"]) >= int(p["slow"]):
            raise ValueError(
                f"fast({p['fast']}) 必须小于 slow({p['slow']})——快线不快于慢线"
                "没有交叉语义")
        # 引擎规则与参数副本一致（引擎只读 exit_rules；不一致 = 记录与执行
        # 分叉，立即报错，绝不静默取其一）
        for name, value in (("stop_loss_atr", self.exit_rules.stop_loss_atr),
                            ("take_profit_atr", self.exit_rules.take_profit_atr)):
            if float(p[name]) != float(value):
                raise ValueError(
                    f"参数 {name}={p[name]!r} 与引擎 ExitRules.{name}={value!r}"
                    " 不一致——引擎只读 ExitRules，请让 --params 与策略构造"
                    "（或类缺省）一致，禁止静默分叉")
        close = bars["close"].astype("float64")
        fast = close.rolling(int(p["fast"])).mean()
        slow = close.rolling(int(p["slow"])).mean()
        raw = fast - slow
        sig = np.where(raw.notna(), np.sign(raw), 0)
        return pd.Series(sig.astype("int64"), index=bars.index,
                         dtype="int64")


class MaCrossATRAfterSignalChange(MaCrossATR):
    """同策略，reentry='after_signal_change'（R7：保护性出场后信号离开
    原方向前禁同向再入场）——对账 A/B 两层 blocked_dir 状态机用。"""

    def __init__(self, stop_loss_atr: float = DEFAULT_STOP_LOSS_ATR,
                 take_profit_atr: float = DEFAULT_TAKE_PROFIT_ATR):
        super().__init__(stop_loss_atr=stop_loss_atr,
                         take_profit_atr=take_profit_atr,
                         reentry="after_signal_change")
