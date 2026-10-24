"""A 层回测引擎（逐 bar 循环）：信号 bar 收盘 → 下一根 bar 开盘调仓，bid 基准成本模型。

货币口径（2026-10-25 修复，C1）：
- 价格单位与账户货币严格区分。contract_size = 每手每 1.0 价格变动对应的
  账户货币盈亏（报价货币 = 账户货币时即合约大小：XAUUSD 100、EURUSD
  100000、BTCUSD/USTEC 以经纪商规格为准，见 data/specs/latest.json）。
- gross / spread_cost / slippage_cost = 价格差 × 手数 × contract_size（账户货币）；
  commission = 每手来回佣金 × 手数；swap = 每手隔夜费 × 手数（账户货币）；
  net = gross − commission + swap；equity 与初始资金同为账户货币。

成交与成本模型（全部钉死，测试逐项手算对账）：
- bar 价格即 bid；ask = bid + 点差。costs.spread_p90 即点差全幅（p90），
  每个来回恰计一次，挂在"非 bid 一侧"的那条腿上（多头入场/空头出场）。
- 成本接口 v2（R1，A-1.2）：spread_p90/slippage 均带 keyword-only price
  （该笔成交的 bid 基准价）。相对点差模型（(ask−bid)/mid 按小时分位）按
  price 缩放——缺 price 抛 ValueError；绝对模型忽略 price（行为与旧版逐位
  一致）。价格基准：开/平仓 = 该笔成交的 bid 基准价（开盘调仓=下一根 open；
  time_stop/week_close/end_of_data=该根 close）；空头 SL/TP 的 Ask 触发
  判断 = 当根 high（SL）/low（TP），开盘跳空检查 = 开盘价；逐 bar 估值 =
  当根 close。
- 执行价（p 为 bid 口径的成交基准价，h 为执行 bar 开盘时刻的小时，
  spr=costs.spread_p90(symbol,h,price=p)、slip=costs.slippage(symbol,h,price=p)）：
      多头 entry = p + spr + slip    多头 exit = p − slip
      空头 entry = p − slip           空头 exit = p + spr + slip
  → 价差与滑点已含在成交价内；Trade.slippage_cost/spread_cost 仅作报告
    拆分，计算 net 时不再重复扣减。（相对模型下空头保护性出场的触发基准价
    与成交基准价不同，exit 与「价位+slip」之间存在二阶小量残差；绝对模型
    两者严格一致。）
- 成交基准价 p：信号调仓 = 下一根 bar 的 open；time_stop / week_close /
  end_of_data = 该 bar 的 close；SL/TP 见下。
- SL/TP 价位 = 入场成交价 ± 系数 × ATR14。ATR 取**信号 bar**（决策时点
  已收盘）的值；入场 bar 自身的 ATR 未收盘不可知。ATR 为 NaN（回看不足）
  → 不设 SL/TP，r_multiple 记 NaN。
- SL/TP 触发口径与 MT5 一致（H2 修复）：多头按 Bid 触发（bar 的 low/high），
  空头按 Ask 触发（bar 的 high/low + spr，spr 的价格基准见成本接口 v2 条）。
  成交：多头 exit = 价位 − slip；
  空头 exit = 价位 + slip（价位本身就是 Ask 口径，不再另加点差）。
- 跳空穿价（H1 修复）：入场之后的 bar 若**开盘**已越过 SL（多头 open ≤ SL；
  空头 open+spr ≥ SL），按开盘价成交（exit_reason="stop_gap"，比止损价更差）；
  开盘已越过 TP 时按 TP 价成交（exit_reason="take_profit_gap"，保守不计跳空收益）。
- 同一根 bar 盘中同时触及 SL 与 TP → 止损优先，按止损价成交。
- bar 内处理顺序：开盘调仓（同 bar 反向先平后开）→ 开盘跳空检查 → SL → TP →
  time_stop（到期 bar 收盘价出）→ week_close（周五收盘价出）；
  入场当根 bar 同样适用 SL/TP 盘中检查（入场即开盘价，不存在跳空）。
- 保护性出场后的再入场（R7，ExitRules.reentry，A-1.3）：保护性出场指
  stop / stop_gap / take_profit / take_profit_gap / time_stop / week_close
  （signal 出场不算）。reentry="immediate"（默认）＝目标仓位语义——信号仍
  指向该方向时下一根开盘重入；reentry="after_signal_change"＝信号离开该
  方向（变 0 或反向）之前禁止同向再入场。
- Trade 时间口径（R9，A-1.3）：exit_time = 出场 bar 的开盘时刻（原义不变，
  供对齐/分组）；exit_fill_time = 出场成交时刻——按收盘价出场
  （time_stop / week_close / end_of_data）为该 bar 收盘时刻（bar 时间+周期），
  其余出场 = bar 时间。隔夜费按 (entry_time, exit_fill_time] 计。
- week_close_exit 只允许 ≤ H1 的周期（R8，A-1.3）：按 bar 收盘时刻判定周五
  收盘，D1/H4 会提前一整根平仓（D1 在周四收盘强平），更高周期运行前抛
  ValueError。
- 隔夜费：整段 (entry_time, exit_fill_time] 交给 overnight_fee 按计费时刻数夜
  （三倍日由成本模型内部计算），结果 × 手数。
- 爆仓标记（R16，A-1.3）：不模拟逐笔保证金。逐 bar 收盘权益 ≤ 0 时引擎停止
  开新仓（已有持仓的各类出场照常执行），BacktestResult.ruined=True 且
  ruined_at 记首次发生 bar 的时间（标记粘滞，权益回升不清除）。equity 序列
  如实记录负值；收益率等指标的解释由报告层结合 ruined 标记判断。
- equity = 逐 bar 收盘市值：初始资金 + 已实现净利 + 持仓浮盈；多头按收盘
  Bid 估值，空头按收盘 Ask（close + spr，spr 基准价=当根 close）估值，
  与各自的平仓口径一致；
  未实现隔夜费不计，出场时一次入账。
- 数据末尾仍持仓 → 按末根 bar 收盘价强平，exit_reason="end_of_data"。
- 时间口径：bar.time 是经纪商服务器时间（库内约定贴 UTC 标签）；
  Exness 服务器为 GMT+0，故与 UTC 一致。换用非 GMT+0 经纪商前必须先
  处理时区（见 docs/blueprint.md「时间约定」）。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields
from datetime import datetime, time as dtime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from mt5data.store import TIMEFRAME_SECONDS
from mt5quant.strategies.base import REENTRY_MODES, ExitRules

NY_TZ = "America/New_York"   # 周五当地收盘（17:00）换算用的时区
_WEEK_CLOSE_LOCAL = dtime(17, 0)
ENGINE_VERSION = "A-1.3"     # 2026-10-25 T7：R7 reentry / R8 week_close 限 ≤H1 /
                             # R9 exit_fill_time / R16 爆仓标记

# 按收盘价出场的 reason：成交时刻 = bar 收盘（bar 时间 + 周期）（R9）
CLOSE_PRICE_REASONS = ("time_stop", "week_close", "end_of_data")
# 保护性出场（R7）：这些 reason 触发 after_signal_change 的同向禁入
PROTECTIVE_REASONS = ("stop", "stop_gap", "take_profit", "take_profit_gap",
                      "time_stop", "week_close")


class Costs(Protocol):
    """成本模型结构化接口（v2，R1）——四个方法名与 mt5quant.costs 的产出逐字一致。

    spread_p90/slippage 的 keyword-only price = 该笔成交的 bid 基准价：
    相对点差模型必须拿到 price（缺失抛 ValueError）；绝对模型忽略。
    """

    def spread_p90(self, symbol: str, hour_utc: int,
                   *, price: float | None = None) -> float:
        """该小时的点差全幅 p90（价格单位，每个来回恰计一次）。

        相对模型返回 rel_p90×price（缺 price 抛 ValueError）；绝对模型忽略 price。
        """
        ...

    def slippage(self, symbol: str, hour_utc: int,
                 *, price: float | None = None) -> float:
        """该 UTC 小时的滑点幅值（价格单位，恒为正、不利方向）；price 同上。"""
        ...

    def commission_per_lot(self, symbol: str) -> float:
        """每手来回佣金（计价货币）。"""
        ...

    def overnight_fee(self, symbol: str, side: str,
                      from_ts: datetime, to_ts: datetime) -> float:
        """持仓 [from_ts, to_ts] 的隔夜费（计价货币/手，恒 ≤0）。"""
        ...


@dataclass(frozen=True)
class Trade:
    """单笔往返。价格字段为价格单位；金额字段（gross 起）一律为账户货币。"""

    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    side: str            # "long" | "short"
    volume_lots: float
    entry_price: float   # 实际成交价（已含点差/滑点）
    exit_price: float
    gross: float         # (exit−entry)×方向×手数×contract_size——已含价差与滑点损耗
    commission: float    # 来回佣金（每手来回 × 手数）
    slippage_cost: float  # 双边滑点金额（拆分用，已含在 gross 内）
    spread_cost: float   # 该来回的价差成本金额（拆分用，已含在 gross 内）
    swap: float          # 隔夜费（每手 × 手数，≤0）
    net: float           # gross − commission + swap
    r_multiple: float    # net / 初始风险金额（SL 距离；无 SL 时按 1×ATR）
    bars_held: int
    exit_reason: str     # signal|stop|stop_gap|take_profit|take_profit_gap|
                         # time_stop|week_close|end_of_data
    exit_fill_time: pd.Timestamp | None = None   # 出场成交时刻（R9）：
                         # 按收盘价出场（CLOSE_PRICE_REASONS）= bar 时间+周期，
                         # 其余 = bar 时间（与 exit_time 相同）。引擎恒写入；
                         # None 仅出现在引擎之外手工构造的合成交易里。


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.Series
    bars: pd.DataFrame
    params: dict
    symbol: str
    timeframe: str
    contract_size: float | None = None   # 金额换算口径（写进 manifest）
    volume_lots: float | None = None
    initial_capital: float | None = None
    ruined: bool = False                 # 权益 ≤ 0 过（R16）：此后不再开新仓
    ruined_at: pd.Timestamp | None = None  # 首次权益 ≤ 0 的 bar 时间；未发生为 None


def _wilder(values: pd.Series, period: int) -> pd.Series:
    """Wilder 平滑：种子=前 period 个有效值的均值，其后按 (n-1)/n 递推。

    与 mt5mcp.indicators._wilder 同法（本地实现，本模块不 import mt5mcp）：
    把种子写入第 period 个有效位置、抹掉其前的原始值，再用
    ewm(alpha=1/period, adjust=False, ignore_na=True) 从种子起递推。
    """
    vals = values.astype("float64").copy()
    valid = vals.dropna()
    if len(valid) < period:
        return vals * float("nan")  # 数据不足，全部 NaN
    seed_idx = valid.index[period - 1]
    seed = valid.iloc[:period].mean()
    for i in range(period):
        vals[valid.index[i]] = float("nan")
    vals[seed_idx] = float(seed)
    return vals.ewm(alpha=1 / period, adjust=False, ignore_na=True).mean()


def atr_series(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR（Wilder）：TR = max(high−low, |high−prev_close|, |low−prev_close|)。

    种子=前 period 个 TR 的均值；与 mt5mcp.indicators.atr 数值一致
    （注意 MT5 内置 iATR 是简单平均，口径不同，见 mt5mcp.indicators 说明）。
    """
    highs = bars["high"].astype("float64")
    lows = bars["low"].astype("float64")
    closes = bars["close"].astype("float64")
    prev_close = closes.shift(1)
    tr = pd.concat(
        [highs - lows, (highs - prev_close).abs(), (lows - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return _wilder(tr, period)


def _validate_bars(bars: pd.DataFrame) -> None:
    missing = {"time", "open", "high", "low", "close"} - set(bars.columns)
    if missing:
        raise ValueError(f"bars 缺列：{sorted(missing)}")
    if len(bars) == 0:
        raise ValueError("bars 为空")
    t = pd.to_datetime(bars["time"])
    if not t.is_monotonic_increasing or bool(t.duplicated().any()):
        raise ValueError("bars.time 必须严格升序且无重复")


def _validate_signals(sig: pd.Series, bars: pd.DataFrame) -> pd.Series:
    if not isinstance(sig, pd.Series):
        raise ValueError("generate_signals 必须返回 pd.Series")
    if len(sig) != len(bars) or not sig.index.equals(bars.index):
        raise ValueError("信号长度或 index 必须与 bars 一致")
    bad = set(pd.unique(sig)) - {-1, 0, 1}
    if bad:
        raise ValueError(f"信号取值必须在 {{-1,0,1}} 内，出现：{sorted(bad, key=str)}")
    return sig


def _validate_rules(rules: ExitRules) -> None:
    if rules.stop_loss_atr is not None and not rules.stop_loss_atr > 0:
        raise ValueError(f"stop_loss_atr 必须为正：{rules.stop_loss_atr!r}")
    if rules.take_profit_atr is not None and not rules.take_profit_atr > 0:
        raise ValueError(f"take_profit_atr 必须为正：{rules.take_profit_atr!r}")
    if rules.time_stop_bars is not None and rules.time_stop_bars < 1:
        raise ValueError(f"time_stop_bars 必须 ≥1：{rules.time_stop_bars!r}")
    # ExitRules 构造时已校验；这里兜住绕过 dataclass 的鸭子类型规则对象
    if rules.reentry not in REENTRY_MODES:
        raise ValueError(
            f"reentry 必须是 {REENTRY_MODES} 之一，收到 {rules.reentry!r}")


def _week_close_bars(times: list, tf_seconds: int) -> frozenset:
    """标记"周五当地 17:00 收盘前最后一根已收盘 bar"的位置。

    bar j 入选当且仅当 close_j <= 本周五当地 17:00 < close_{j+1}
    （close = time + 周期秒数；DST 由 zoneinfo 处理）。
    """
    ny, utc = ZoneInfo(NY_TZ), ZoneInfo("UTC")
    closes_ts = [t + pd.Timedelta(seconds=tf_seconds) for t in times]
    out = set()
    for j in range(len(times) - 1):
        c, nxt = closes_ts[j], closes_ts[j + 1]
        local = c.replace(tzinfo=utc).astimezone(ny)
        days = 4 - local.weekday()  # 周一(0)→+4 … 周五(4)→0；周六→−1、周日→−2
        fri_local = datetime.combine(
            local.date() + timedelta(days=days), _WEEK_CLOSE_LOCAL, tzinfo=ny)
        cutoff = fri_local.astimezone(utc).replace(tzinfo=None)
        if c <= cutoff < nxt:
            out.add(j)
    return frozenset(out)


def _validate_contract_size(contract_size) -> float:
    if isinstance(contract_size, bool) or not isinstance(contract_size, (int, float)):
        raise ValueError(f"contract_size 必须是正数，收到 {contract_size!r}")
    cs = float(contract_size)
    if not math.isfinite(cs) or cs <= 0:
        raise ValueError(f"contract_size 必须是有限正数，收到 {contract_size!r}")
    return cs


def run_backtest(strategy, bars: pd.DataFrame, costs, *, symbol: str, params: dict,
                 contract_size: float,
                 volume_lots: float = 1.0, initial_capital: float = 10_000.0,
                 timeframe: str | None = None) -> BacktestResult:
    """跑一次 A 层回测。

    contract_size 必填（无默认值，防止静默按 1 折算）：每手每 1.0 价格变动的
    账户货币盈亏；报价货币 = 账户货币时即品种合约大小
    （data/specs/latest.json 的 contract_size）。
    timeframe 未显式给出时取 strategy.required_data.timeframe；必须能对应
    TIMEFRAME_SECONDS 的周期（Trade.exit_fill_time 需要 bar 周期算收盘时刻）。
    week_close_exit=True 另要求周期 ≤ H1（R8）。
    """
    cs = _validate_contract_size(contract_size)
    if not (isinstance(volume_lots, (int, float)) and not isinstance(volume_lots, bool)
            and math.isfinite(volume_lots) and volume_lots > 0):
        raise ValueError(f"volume_lots 必须是有限正数，收到 {volume_lots!r}")
    if not (isinstance(initial_capital, (int, float)) and math.isfinite(initial_capital)
            and initial_capital > 0):
        raise ValueError(f"initial_capital 必须是有限正数，收到 {initial_capital!r}")
    if timeframe is None:
        timeframe = strategy.required_data.timeframe
    tf_norm = timeframe.upper() if timeframe else ""
    tf_seconds = TIMEFRAME_SECONDS.get(tf_norm)
    if tf_seconds is None:
        raise ValueError(
            f"timeframe 必须是 TIMEFRAME_SECONDS 之一（exit_fill_time 需要 bar "
            f"周期计算收盘时刻），得到 {timeframe!r}")
    tf_delta = pd.Timedelta(seconds=tf_seconds)
    params = strategy.validate_params(dict(params))
    _validate_bars(bars)
    rules: ExitRules = strategy.exit_rules or ExitRules()
    _validate_rules(rules)
    sig = _validate_signals(strategy.generate_signals(bars, params), bars)
    sig_arr = sig.to_numpy()
    atr_arr = atr_series(bars).to_numpy()

    n = len(bars)
    times = [pd.Timestamp(t) for t in bars["time"]]
    opens = bars["open"].to_numpy(dtype="float64")
    highs = bars["high"].to_numpy(dtype="float64")
    lows = bars["low"].to_numpy(dtype="float64")
    closes = bars["close"].to_numpy(dtype="float64")

    week_close_idx: frozenset = frozenset()
    if rules.week_close_exit:
        if tf_seconds > TIMEFRAME_SECONDS["H1"]:
            raise ValueError(
                f"week_close_exit 只允许 ≤ H1 的周期：按 bar 收盘时刻判定周五"
                f"收盘，D1/H4 会提前一整根平仓（D1 在周四收盘强平），得到 "
                f"{timeframe!r}")
        week_close_idx = _week_close_bars(times, tf_seconds)

    trades: list[Trade] = []
    state: dict | None = None   # 持仓状态；None = 空仓
    realized = 0.0
    equity_vals = [0.0] * n
    # R7：after_signal_change 模式下保护性出场后的禁入方向（±1；0=无禁入）。
    # immediate 模式不设禁入（保持目标仓位语义）。
    blocked_dir = 0
    # R16：权益 ≤ 0 后不再开新仓；ruined 粘滞，ruined_at 记首次发生 bar。
    ruined = False
    ruined_at: pd.Timestamp | None = None

    def _close(j: int, base_price: float, reason: str) -> None:
        """平仓。base_price 为 bid 口径基准价（空头出场另加 spr 变成 Ask）。"""
        nonlocal state, realized, blocked_dir
        assert state is not None
        spr = costs.spread_p90(symbol, times[j].hour, price=base_price)
        slip = costs.slippage(symbol, times[j].hour, price=base_price)
        if state["dir"] == 1:
            exit_price = base_price - slip
        else:
            exit_price = base_price + spr + slip
        lots = state["lots"]
        gross = (exit_price - state["entry_price"]) * state["dir"] * lots * cs
        commission = costs.commission_per_lot(symbol) * lots
        # 成交时刻（R9）：按收盘价出场 = bar 收盘（bar 时间+周期），其余 = bar
        # 时间。隔夜费按 (entry_time, exit_fill_time] 计——恰跨计费时刻的那根
        # bar 的收盘也计入；overnight_fee 是"每手"口径，必须 × 手数
        exit_fill = (times[j] + tf_delta if reason in CLOSE_PRICE_REASONS
                     else times[j])
        swap = costs.overnight_fee(
            symbol, state["side"], state["entry_time"], exit_fill) * lots
        net = gross - commission + swap
        spread_charged = state["spread_charged"]
        if spread_charged is None:        # 空头：价差计在出场腿
            spread_charged = spr
        trades.append(Trade(
            entry_time=state["entry_time"], exit_time=times[j],
            side=state["side"], volume_lots=lots,
            entry_price=state["entry_price"], exit_price=exit_price,
            gross=gross, commission=commission,
            slippage_cost=(state["entry_slip"] + slip) * lots * cs,
            spread_cost=spread_charged * lots * cs, swap=swap, net=net,
            r_multiple=_r_multiple(net, state), bars_held=j - state["entry_bar"],
            exit_reason=reason, exit_fill_time=exit_fill))
        realized += net
        if (rules.reentry == "after_signal_change"
                and reason in PROTECTIVE_REASONS):   # R7：保护性出场 → 禁同向再入场
            blocked_dir = state["dir"]
        state = None

    def _open(j: int, desired: int) -> None:
        nonlocal state
        spr = costs.spread_p90(symbol, times[j].hour, price=opens[j])
        slip = costs.slippage(symbol, times[j].hour, price=opens[j])
        base = opens[j]
        if desired == 1:
            entry_price = base + spr + slip
            spread_charged: float | None = spr  # 多头：价差计在入场腿
        else:
            entry_price = base - slip
            spread_charged = None
        sig_bar = j - 1                       # SL/TP 用信号 bar 的 ATR（已收盘）
        entry_atr = float(atr_arr[sig_bar])
        if math.isnan(entry_atr):
            entry_atr = float("nan")
        sl = tp = None
        if not math.isnan(entry_atr):
            if rules.stop_loss_atr is not None:
                sl = (entry_price - rules.stop_loss_atr * entry_atr if desired == 1
                      else entry_price + rules.stop_loss_atr * entry_atr)
            if rules.take_profit_atr is not None:
                tp = (entry_price + rules.take_profit_atr * entry_atr if desired == 1
                      else entry_price - rules.take_profit_atr * entry_atr)
        state = {
            "dir": desired, "side": "long" if desired == 1 else "short",
            "lots": volume_lots, "entry_price": entry_price, "entry_time": times[j],
            "entry_bar": j, "entry_slip": slip, "spread_charged": spread_charged,
            "entry_atr": entry_atr, "sl_k": rules.stop_loss_atr, "sl": sl, "tp": tp,
        }

    def _r_multiple(net: float, st: dict) -> float:
        atr_v = st["entry_atr"]
        if math.isnan(atr_v) or atr_v <= 0:
            return float("nan")
        k = st["sl_k"] if st["sl_k"] is not None else 1.0
        return net / (k * atr_v * st["lots"] * cs)   # 初始风险金额（账户货币）

    def _protective_exit(j: int) -> None:
        """开盘跳空 → 盘中 SL → 盘中 TP。多头按 Bid、空头按 Ask 触发（MT5 口径）。

        空头 Ask = Bid + spr；触发判断的 spr 价格基准＝当根 high（SL）/
        low（TP），开盘跳空检查＝开盘价（成本接口 v2，相对模型下随基准价
        缩放；绝对模型与旧版逐位一致）。
        """
        sl, tp = state["sl"], state["tp"]
        if sl is None and tp is None:
            return
        gap_check = j > state["entry_bar"]   # 入场当根以开盘价入场，不存在跳空
        if state["dir"] == 1:
            if gap_check and sl is not None and opens[j] <= sl:
                _close(j, opens[j], "stop_gap")          # 跳空穿止损：按开盘价（更差）
            elif gap_check and tp is not None and opens[j] >= tp:
                _close(j, tp, "take_profit_gap")         # 保守：按 TP 价，不计跳空收益
            elif sl is not None and lows[j] <= sl:       # 同 bar 双触：止损优先
                _close(j, sl, "stop")
            elif tp is not None and highs[j] >= tp:
                _close(j, tp, "take_profit")
            return
        if gap_check and (sl is not None or tp is not None):
            spr_open = costs.spread_p90(symbol, times[j].hour, price=opens[j])
            if sl is not None and opens[j] + spr_open >= sl:
                _close(j, opens[j], "stop_gap")          # exit = open + spr + slip
            elif tp is not None and opens[j] + spr_open <= tp:
                _close(j, tp - spr_open, "take_profit_gap")  # exit = tp + slip
        if state is not None and sl is not None:
            spr_high = costs.spread_p90(symbol, times[j].hour, price=highs[j])
            if highs[j] + spr_high >= sl:
                _close(j, sl - spr_high, "stop")         # exit = sl + slip
        if state is not None and tp is not None:
            spr_low = costs.spread_p90(symbol, times[j].hour, price=lows[j])
            if lows[j] + spr_low <= tp:
                _close(j, tp - spr_low, "take_profit")   # exit = tp + slip

    for j in range(n):
        # 1) 开盘调仓：信号来自 bar j-1 的收盘
        if j > 0:
            if blocked_dir and int(sig_arr[j - 1]) != blocked_dir:
                blocked_dir = 0   # R7：信号已离开禁入方向（变 0 或反向）→ 解禁
            if int(sig_arr[j - 1]) != (state["dir"] if state else 0):
                desired = int(sig_arr[j - 1])
                if state is not None:
                    _close(j, opens[j], "signal")
                if desired != 0 and not ruined and desired != blocked_dir:
                    _open(j, desired)   # 爆仓（R16）或同向禁入（R7）时不开新仓
        # 2) 持仓检查（入场当根同样适用盘中 SL/TP）
        if state is not None:
            _protective_exit(j)
        if state is not None:
            if (rules.time_stop_bars is not None
                    and j - state["entry_bar"] >= rules.time_stop_bars):
                _close(j, closes[j], "time_stop")
            elif j in week_close_idx:
                _close(j, closes[j], "week_close")
        # 3) 收盘市值：多头按 Bid、空头按 Ask（与平仓口径一致）
        unreal = 0.0
        if state is not None:
            mark = closes[j]
            if state["dir"] == -1:
                mark = closes[j] + costs.spread_p90(symbol, times[j].hour,
                                                    price=closes[j])
            unreal = (mark - state["entry_price"]) * state["dir"] * state["lots"] * cs
        equity_vals[j] = initial_capital + realized + unreal
        if not ruined and equity_vals[j] <= 0:   # R16：爆仓（粘滞标记）
            ruined = True
            ruined_at = times[j]

    if state is not None:  # 数据末尾强平
        _close(n - 1, closes[n - 1], "end_of_data")
        equity_vals[n - 1] = initial_capital + realized
        if not ruined and equity_vals[n - 1] <= 0:
            ruined = True
            ruined_at = times[n - 1]

    trades_df = pd.DataFrame(
        [asdict(t) for t in trades],
        columns=[f.name for f in fields(Trade)],
    )
    equity = pd.Series(equity_vals, index=bars.index, name="equity")
    return BacktestResult(trades=trades_df, equity=equity, bars=bars,
                          params=params, symbol=symbol, timeframe=timeframe,
                          contract_size=cs, volume_lots=float(volume_lots),
                          initial_capital=float(initial_capital),
                          ruined=ruined, ruined_at=ruined_at)
