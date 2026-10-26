"""scripts/reconcile_ab.py 的机制核对与净利差拆解：合成 bars + ticks 手算对账。

T40（H6 / 铁律 14）撤回 T26 的结果宽容差（120s、max(1 点差, 1×初始风险)、
6 根 bar），改为按机制核对：
- 入场 / signal 出场：B 层成交 = 就绪时刻（bar 边界 + latency）后第一笔 tick
  （误差 ≤1ms）；边界后 >2s 才有首笔 tick 的行单列"静默边界"，不算违规；
- 保护性出场：B 层成交 tick = 按正确一侧（多头 Bid、空头 Ask）第一笔穿过
  价位的 tick（同 bar 先止损后止盈，更早满足任一条件者即应出场）；
- 收盘价类出场：B 层成交 = 该 bar 收盘边界前最后一笔 tick，且落在出场 bar
  区间内（零容差，机制语义）；
- 净利差拆解：net_diff = 点差分量 + 止损越幅分量（T36）+ 其余；
  |其余| > 1 个点差判违规。

结构性预期（tick 复现 bar OHLC 且点差/滑点模型与 tick 一致）仍为：两层
差异 ≈ 0；点差模型 ≠ 实测点差时差异全部落在点差分量，"其余"≈ 0。
"""
import importlib.util
import json
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from mt5data.store import TIMEFRAME_SECONDS
from mt5quant.strategies import BaseStrategy, BuyHold, ExitRules

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "reconcile_ab.py"

COMM, ONIGHT = 1.0, -2.0
S_MODEL, SLIP_MODEL = 0.5, 0.1


def _load_script():
    spec = importlib.util.spec_from_file_location("reconcile_ab_script", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reconcile_ab_script"] = mod   # dataclass 注解解析需要
    spec.loader.exec_module(mod)
    return mod


class FakeCosts:
    """A/B 通吃：spread_p90/slippage 供 A 层调用（带 price= 兼容成本接口 v2），
    B 层只消费 commission_per_lot / overnight_fee。overshoot 模拟 T36 的
    stop_overshoot（价格单位定值，忽略 price 基准）。"""

    def __init__(self, half=S_MODEL, slip=SLIP_MODEL, commission=COMM,
                 overnight=ONIGHT, overshoot=None):
        self.half, self.slip = half, slip
        self.commission, self.overnight = commission, overnight
        self.overshoot = overshoot

    def spread_p90(self, symbol, hour_utc, *, price=None):
        return self.half

    def slippage(self, symbol, hour_utc, *, price=None):
        return self.slip

    def stop_overshoot(self, symbol, hour_ny, *, price=None, level="p50"):
        return self.overshoot if self.overshoot is not None else 0.0

    def commission_per_lot(self, symbol):
        return self.commission

    def overnight_fee(self, symbol, side, from_ts, to_ts):
        f, t = pd.Timestamp(from_ts), pd.Timestamp(to_ts)
        if t <= f:
            return 0.0
        return self.overnight * (t.normalize() - f.normalize()).days


class ScriptStrategy(BaseStrategy):
    """按位置取脚本信号（fill 补齐）。仅测试用：参数模式未声明。"""

    def __init__(self, script, fill=0, exit_rules=None):
        self.script = list(script)
        self.fill = fill
        self.exit_rules = exit_rules or ExitRules()

    def generate_signals(self, bars, params):
        vals = [self.script[i] if i < len(self.script) else self.fill
                for i in range(len(bars))]
        return pd.Series(vals, index=bars.index, dtype="int64")


def flat_bars(start, n, tf="H1", *, openc=100.0, high=100.5, low=99.5,
              close=100.0):
    secs = TIMEFRAME_SECONDS[tf]
    times = [pd.Timestamp(start) + timedelta(seconds=secs * i) for i in range(n)]
    return pd.DataFrame({
        "time": pd.to_datetime(times),
        "open": openc, "high": high, "low": low, "close": close,
        "tick_volume": 10, "spread": 5, "real_volume": 0,
    })


def base4():
    """4 根 H1（周一 2026-01-05 10:00 起），末根收盘 103。"""
    df = flat_bars(pd.Timestamp("2026-01-05 10:00"), 4)
    df.loc[3, "close"] = 103.0
    return df


def msc(ts) -> int:
    return int(pd.Timestamp(ts).value // 1_000_000)


def tick(ts, bid, ask):
    return (msc(ts), float(bid), float(ask))


def ohlc_ticks(bars, spread, tf="H1"):
    """每根 bar 四笔 tick 恰好复现其 OHLC（bid=价、ask=bid+spread）。"""
    secs = TIMEFRAME_SECONDS[tf]
    out = []
    for _, row in bars.iterrows():
        t = pd.Timestamp(row["time"])
        out.append(tick(t, row["open"], row["open"] + spread))
        out.append(tick(t + pd.Timedelta(minutes=20), row["high"],
                        row["high"] + spread))
        out.append(tick(t + pd.Timedelta(minutes=40), row["low"],
                        row["low"] + spread))
        out.append(tick(t + pd.Timedelta(seconds=secs, milliseconds=-1),
                        row["close"], row["close"] + spread))
    return out


def make_loader(ticks):
    def load(start_msc=None, end_msc=None):
        rows = [t for t in ticks
                if (start_msc is None or t[0] >= start_msc)
                and (end_msc is None or t[0] <= end_msc)]
        return pd.DataFrame(rows, columns=["time_msc", "bid", "ask"])
    return load


TRADE_COLS = ["entry_time", "exit_time", "exit_fill_time", "side",
              "volume_lots", "entry_price", "exit_price", "gross", "net",
              "commission", "slippage_cost", "spread_cost", "swap",
              "exit_reason"]


def trades_frame(rows):
    """手造交易帧：缺省字段补 None/0（机制核对只用其中一部分）。"""
    out = []
    for r in rows:
        d = dict.fromkeys(TRADE_COLS)
        d.update(zip(TRADE_COLS, r))
        out.append(d)
    return pd.DataFrame(out, columns=TRADE_COLS)


def make_ctx(mod, ticks, *, rules=None, atr=None, bar_times=None,
             tf_seconds=3600, latency_ms=0, slippage_price=0.0, costs=None,
             symbol="XAUUSD", contract_size=1.0):
    """构造 MechanismContext：bars 时刻→ms，ATR 缺省全 NaN（无保护性出场）。"""
    n = len(bar_times) if bar_times is not None else 0
    return mod.MechanismContext(
        probe=mod.TickProbe(make_loader(ticks)),
        rules=rules or ExitRules(),
        atr=np.asarray(atr if atr is not None else [np.nan] * n),
        bar_open_ms=[msc(t) for t in (bar_times or [])],
        tf_seconds=tf_seconds, latency_ms=latency_ms,
        slippage_price=slippage_price, costs=costs, symbol=symbol,
        contract_size=contract_size)


# --------------------------------------------- 容差常量锚点（铁律 14：按机制）

def test_default_tolerances_match_plan():
    """T40 锚点：撤回 120s / 1R / 6bar 的结果宽容差，容差只按机制设定。"""
    mod = _load_script()
    assert mod.TOL_FILL_TIME_MS == 1          # B 成交=边界后首笔 tick，逐位一致
    assert mod.SILENT_BOUNDARY_MS == 2_000    # >2s 无 tick → 静默边界（单列）
    assert mod.NET_REST_TOL_SPREADS == 1.0    # 净利"其余"分量 ≤ 1 个点差
    # T26 引入的结果宽容差常量必须不复存在（防回归）
    for gone in ("TOL_ENTRY_TIME_MS", "TOL_EXIT_TIME_SIGNAL_MS",
                 "NET_TOL_RISK_MULTIPLE", "EXIT_TIME_PROTECTIVE_BARS",
                 "EXIT_TIME_OTHER_BARS"):
        assert not hasattr(mod, gone), gone


def test_module_doc_states_lockbox_exemption_and_direct_load():
    mod = _load_script()
    from mt5data.store import load_bars
    assert "锁箱" in mod.__doc__ and "豁免" in mod.__doc__
    assert mod.load_bars is load_bars   # 直接读全量（运维对账），不走 research_bars
    assert not hasattr(mod, "research_bars")


# --------------------------------------------- A/B 全链路：tick 复现 OHLC

def test_parity_ohlc_ticks_all_diffs_near_zero():
    mod = _load_script()
    df = base4()
    rep = mod.reconcile(ScriptStrategy([1], fill=1), df,
                        make_loader(ohlc_ticks(df, S_MODEL)), FakeCosts(),
                        symbol="XAUUSD", params={}, timeframe="H1",
                        contract_size=1.0, slippage_price=SLIP_MODEL)
    assert rep.ok, rep.rows.to_string()
    assert rep.summary["n_paired"] == 1
    assert rep.summary["n_only_a"] == rep.summary["n_only_b"] == 0
    row = rep.rows.iloc[0]
    assert row["match"] == "paired"
    assert row["entry_dt_ms"] == 0                       # 信号类入场时间完全一致
    assert row["entry_gap_ms"] == 0                      # B=边界后首笔 tick
    assert row["entry_time_ok"]
    assert row["entry_price_diff"] == pytest.approx(0.0, abs=1e-9)
    assert row["exit_price_diff"] == pytest.approx(0.0, abs=1e-9)
    assert row["net_diff"] == pytest.approx(0.0, abs=1e-9)
    assert row["net_spread_comp"] == pytest.approx(0.0, abs=1e-9)
    assert row["net_overshoot_comp"] == pytest.approx(0.0, abs=1e-9)
    assert row["net_rest"] == pytest.approx(0.0, abs=1e-9)
    # end_of_data 收盘价类出场：B 在收盘前最后一笔 tick，差 tf−1ms（结构内差异）
    assert row["exit_dt_ms"] == pytest.approx(3_600_000 - 1)
    assert row["exit_time_ok"] and row["row_ok"]


def test_spread_model_mismatch_diff_structure():
    """tick 实际点差 0.7 vs A 层模型 0.5：差异全部落进点差分量，"其余"≈0。"""
    mod = _load_script()
    df = base4()
    rep = mod.reconcile(ScriptStrategy([1], fill=1), df,
                        make_loader(ohlc_ticks(df, 0.7)), FakeCosts(),
                        symbol="XAUUSD", params={}, timeframe="H1",
                        contract_size=1.0, slippage_price=SLIP_MODEL)
    assert rep.summary["n_paired"] == 1
    row = rep.rows.iloc[0]
    assert row["entry_price_diff"] == pytest.approx(0.2)   # 非 Bid 腿（多头入场）
    assert row["exit_price_diff"] == pytest.approx(0.0)    # Bid 腿不受影响
    assert row["net_diff"] == pytest.approx(-0.2)
    assert row["net_spread_comp"] == pytest.approx(-0.2)   # −(实际−模型)点差成本
    assert row["net_overshoot_comp"] == pytest.approx(0.0)
    assert row["net_rest"] == pytest.approx(0.0, abs=1e-9)
    assert row["net_tol"] == pytest.approx(0.7)            # 1 × max(两侧 spread_cost)
    assert row["net_ok"] and row["row_ok"]
    assert rep.ok
    stats = rep.summary["net_diff"]
    assert stats["count"] == 1
    assert stats["mean"] == pytest.approx(-0.2)
    assert stats["within_tol"] == 1


# --------------------------------------------- 静默边界（计划失败测试 2）

def test_silent_boundary_entry_not_a_violation():
    """边界后 5 分钟才有首笔 tick：B 按该 tick 成交是对的（机制如此）——
    单列"静默边界"，不判违规（T40 撤回 120s 宽容差的替代口径）。"""
    mod = _load_script()
    df = base4()
    ticks = ohlc_ticks(df, S_MODEL)
    assert ticks[4][0] == msc("2026-01-05 11:00:00")       # bar1 开盘 tick
    ticks[4] = (msc("2026-01-05 11:05:00"), ticks[4][1], ticks[4][2])
    rep = mod.reconcile(ScriptStrategy([1], fill=1), df, make_loader(ticks),
                        FakeCosts(), symbol="XAUUSD", params={}, timeframe="H1",
                        contract_size=1.0, slippage_price=SLIP_MODEL)
    row = rep.rows.iloc[0]
    assert row["match"] == "paired"                        # 仍配对（1 bar 内）
    assert row["entry_dt_ms"] == 5 * 60 * 1000             # B 晚 5 分钟（如实记录）
    assert row["entry_boundary_gap_ms"] == 5 * 60 * 1000   # 边界→首笔 tick 5 分钟
    assert bool(row["entry_silent"])                       # >2s → 静默边界
    assert row["entry_time_ok"]                            # B=首笔 tick：机制正确
    assert row["row_ok"]
    assert rep.summary["violations"]["entry_time"] == 0
    assert rep.summary["n_silent_entry"] == 1
    assert rep.ok                                          # 不算违规


def test_signal_exit_silent_boundary_not_a_violation():
    """signal 出场边界后 3 分钟才有首笔 tick（反手单同 tick 成交）：静默、不违规。"""
    mod = _load_script()
    df = base4()
    ticks = ohlc_ticks(df, S_MODEL)
    # bar2 开盘 tick（=bar1 收盘后的第一笔，signal 反转成交点）推迟 3 分钟
    assert ticks[8][0] == msc("2026-01-05 12:00:00")
    ticks[8] = (msc("2026-01-05 12:03:00"), ticks[8][1], ticks[8][2])
    rep = mod.reconcile(ScriptStrategy([1, -1], fill=-1), df, make_loader(ticks),
                        FakeCosts(), symbol="XAUUSD", params={}, timeframe="H1",
                        contract_size=1.0, slippage_price=SLIP_MODEL)
    assert rep.ok, rep.rows.to_string()
    first, second = rep.rows.iloc[0], rep.rows.iloc[1]
    assert first["exit_reason_a"] == first["exit_reason_b"] == "signal"
    assert first["exit_dt_ms"] == 3 * 60 * 1000            # B 晚 3 分钟（如实记录）
    assert bool(first["exit_silent"])
    assert first["exit_time_ok"]                           # B=边界后首笔 tick
    assert bool(second["entry_silent"])                    # 反手腿同一笔迟到 tick
    assert rep.summary["violations"]["exit_time"] == 0
    assert rep.summary["n_silent_exit"] == 1


# --------------------------------------------- 计划失败测试 1：空头止损按 Bid 触发

def _short_stop_setup():
    """20 根平 H1（ATR14=1）；bar14 收盘出 −1 信号，bar15 开盘入空
    （A=99.9、B=99.9，SL=入场+1×ATR=100.9，Ask 侧触发）。

    tick：t15 开盘（入场）；+20m Ask 恰触 SL（正确 B 在此止损）；
    +40m Bid 越过 SL（错误实现按 Bid 触发才在此止损）；+59:59.999 收盘。"""
    bars = flat_bars(pd.Timestamp("2026-01-05 10:00"), 20)
    t15 = pd.Timestamp("2026-01-05 10:00") + timedelta(hours=15)
    ticks = [
        tick(t15, 100.0, 100.5),                                  # 入场 tick
        tick(t15 + timedelta(minutes=20), 100.4, 100.9),          # Ask 首穿 SL
        tick(t15 + timedelta(minutes=40), 100.95, 101.0),         # Bid 越过 SL
        tick(t15 + timedelta(minutes=59, seconds=59, milliseconds=999),
             100.0, 100.5),
    ]
    return bars, t15, ticks


def test_short_stop_bid_trigger_flagged_old_1r_tolerance_missed_it():
    """计划失败测试（T40）：人为把 B 层空头止损改成按 Bid 触发 → 判违规。

    改法（测试里构造）：包装 run_backtest_ticks，把真实的空头 stop 行从
    "+20m Ask 首穿 SL"改写到"+40m Bid 越过 SL"的 tick（成交价=该 tick
    Ask+滑点）。该错误退出比正确退出差 0.1 价格单位：
    - 旧容差测不出：|净利差| 0.1 ≤ max(1×点差成本 0.5, 1×初始风险 1.0)；
    - 新机制核对抓得住：+20m 的 Ask 已穿过 SL，B 却在更晚的 tick 成交——
      不是"正确一侧第一笔穿过价位的 tick"。
    """
    mod = _load_script()
    bars, t15, ticks = _short_stop_setup()
    real_engine = mod.run_backtest_ticks
    t_bad = msc(t15 + timedelta(minutes=40))

    def bid_trigger_engine(strategy, bars_, loader, costs_, **kw):
        res = real_engine(strategy, bars_, loader, costs_, **kw)
        assert res.trades["exit_reason"].tolist() == ["stop"]
        r = res.trades.iloc[0]
        assert r["exit_time"] == pd.Timestamp(
            t15 + timedelta(minutes=20))                    # 真实 B：Ask 首穿处
        # —— 人为改成按 Bid 触发：在 Bid 越过 SL 的那笔 tick 上成交 ——
        exit_price_bad = 101.0 + SLIP_MODEL                 # 该 tick Ask + 滑点
        gross_bad = (exit_price_bad - r["entry_price"]) * -1.0  # 空头 dir=−1
        net_bad = gross_bad - float(r["commission"]) + float(r["swap"])
        res.trades.loc[res.trades.index[0], [
            "exit_time", "exit_fill_time", "exit_price", "gross", "net",
            "spread_cost"]] = [
            pd.Timestamp(t15 + timedelta(minutes=40)),
            pd.Timestamp(t15 + timedelta(minutes=40)),
            exit_price_bad, gross_bad, net_bad, (101.0 - 100.95) * 1.0]
        return res

    mod.run_backtest_ticks = bid_trigger_engine
    try:
        rep = mod.reconcile(
            ScriptStrategy([0] * 14 + [-1], fill=0,
                           exit_rules=ExitRules(stop_loss_atr=1.0)),
            bars, make_loader(ticks), FakeCosts(),
            symbol="XAUUSD", params={}, timeframe="H1",
            contract_size=1.0, slippage_price=SLIP_MODEL)
    finally:
        mod.run_backtest_ticks = real_engine
    assert rep.summary["n_paired"] == 1
    row = rep.rows.iloc[0]
    # 旧容差的反事实：|净利差| 仍在 max(1 点差, 1×初始风险) 内
    risk_amt = abs(float(row["net_a"]) / -2.1)              # r_multiple_a=−2.1
    assert abs(row["net_diff"]) <= max(row["net_tol"], 1.0 * risk_amt)
    # 新机制核对：更早的 Ask 穿价存在 → 不是第一笔穿价 tick
    assert not row["exit_time_ok"]
    assert row["exit_note"] == "earlier_cross"
    assert not row["row_ok"]
    assert rep.summary["violations"]["exit_time"] == 1
    assert not rep.ok


def test_short_stop_correct_first_ask_cross_passes():
    """对照：真实 B（Ask 首穿处成交）在同样数据上全部核对通过。"""
    mod = _load_script()
    bars, t15, ticks = _short_stop_setup()
    rep = mod.reconcile(
        ScriptStrategy([0] * 14 + [-1], fill=0,
                       exit_rules=ExitRules(stop_loss_atr=1.0)),
        bars, make_loader(ticks), FakeCosts(),
        symbol="XAUUSD", params={}, timeframe="H1",
        contract_size=1.0, slippage_price=SLIP_MODEL)
    assert rep.ok, rep.rows.to_string()
    row = rep.rows.iloc[0]
    assert row["exit_check"] == "first_cross"
    assert row["exit_time_ok"]
    # 空头非跳空保护性出场：点差不进净利（A 预减 spr 再加回 / B 按触发侧成交），
    # 实际越幅 0（恰触）与模型 0 → 拆解后"其余"≈0
    assert row["net_spread_comp"] == pytest.approx(0.0, abs=1e-9)
    assert row["net_rest"] == pytest.approx(0.0, abs=1e-9)


def test_short_stop_same_ms_follow_on_quote_no_false_alarm():
    """I6（第七轮复审）失败测试：正确止损样例追加**同毫秒后续报价**——
    B 仍正确在该毫秒首笔穿价 tick 成交（引擎逐 tick 消费，先到先成交），
    对账不得误报 no_trigger_at_fill。

    旧实现取"该毫秒最后一笔 tick"核对触发条件：追加的未穿价报价排在
    成交 tick 之后 → 触发条件在最后一笔上不成立 → 正确成交被判违规。
    tick 身份应为 (time_msc, 行内序号)。"""
    mod = _load_script()
    bars, t15, ticks = _short_stop_setup()
    t_stop = t15 + timedelta(minutes=20)      # B 成交 tick（Ask 首穿 SL）
    ticks.insert(2, tick(t_stop, 100.0, 100.7))   # 同毫秒后续报价：Ask 未穿
    rep = mod.reconcile(
        ScriptStrategy([0] * 14 + [-1], fill=0,
                       exit_rules=ExitRules(stop_loss_atr=1.0)),
        bars, make_loader(ticks), FakeCosts(),
        symbol="XAUUSD", params={}, timeframe="H1",
        contract_size=1.0, slippage_price=SLIP_MODEL)
    assert rep.ok, rep.rows.to_string()
    row = rep.rows.iloc[0]
    assert row["exit_check"] == "first_cross"
    assert row["exit_time_ok"]
    assert row["exit_note"] == ""
    assert rep.summary["violations"]["exit_time"] == 0


# --------------------------------------------- 机制核对（ctx 级手造）

def _ctx_rows(mod, trades_a, trades_b, ctx, pairs=None):
    ta, tb = trades_frame(trades_a), trades_frame(trades_b)
    pairs = pairs or [(i, i) for i in range(min(len(ta), len(tb)))]
    return mod.build_diff_rows(ta, tb, pairs, tf_seconds=3600, ctx=ctx)


def test_entry_not_at_first_tick_is_violation():
    """B 入场在边界后第二笔 tick（非首笔）→ 入场时刻违规。"""
    mod = _load_script()
    ticks = [tick("2026-01-05 11:00", 100.0, 100.5),
             tick("2026-01-05 11:20", 100.0, 100.5),
             tick("2026-01-05 13:59:59.999", 100.0, 100.5)]
    bar_times = [f"2026-01-05 {h:02d}:00" for h in (10, 11, 12, 13)]
    ctx = make_ctx(mod, ticks, bar_times=bar_times)
    rows = _ctx_rows(
        mod,
        [("2026-01-05 11:00", "2026-01-05 13:00", "2026-01-05 14:00", "long",
          1.0, 100.5, 100.0, -0.5, -0.5, 0.0, 0.0, 0.5, 0.0, "end_of_data")],
        [("2026-01-05 11:20", "2026-01-05 13:59:59.999",
          "2026-01-05 13:59:59.999", "long",
          1.0, 100.5, 100.0, -0.5, -0.5, 0.0, 0.0, 0.5, 0.0, "end_of_data")],
        ctx)
    row = rows.iloc[0]
    assert row["entry_gap_ms"] == 20 * 60 * 1000
    assert not row["entry_time_ok"]
    assert row["entry_note"] == "not_first_tick"
    assert not row["row_ok"]
    assert row["exit_time_ok"]                  # 出场仍是边界前最后一笔 tick


def test_close_exit_must_be_last_tick_inside_exit_bar():
    """收盘价类出场：B 成交必须是出场 bar 收盘边界前最后一笔 tick。"""
    mod = _load_script()
    bar_times = [f"2026-01-05 {h:02d}:00" for h in (10, 11, 12, 13)]
    base = ("2026-01-05 11:00", "2026-01-05 13:00", "2026-01-05 14:00",
            "long", 1.0, 100.5, 100.0, -0.5, -0.5, 0.0, 0.0, 0.5, 0.0,
            "end_of_data")
    # (a) B 出场后、收盘边界前还有 tick → not_last_tick
    ticks_a = [tick("2026-01-05 11:00", 100.0, 100.5),
               tick("2026-01-05 13:59:59.998", 100.0, 100.5),
               tick("2026-01-05 13:59:59.999", 100.0, 100.5)]
    ctx = make_ctx(mod, ticks_a, bar_times=bar_times)
    rows = _ctx_rows(
        mod, [base],
        [("2026-01-05 11:00", "2026-01-05 13:59:59.998",
          "2026-01-05 13:59:59.998") + base[3:]], ctx)
    assert rows.iloc[0]["exit_note"] == "not_last_tick"
    assert not rows.iloc[0]["exit_time_ok"]
    # (b) B 出场晚于收盘边界（下一根 bar 内）→ outside_exit_bar
    ticks_b = [tick("2026-01-05 11:00", 100.0, 100.5),
               tick("2026-01-05 13:59:59.999", 100.0, 100.5)]
    ctx = make_ctx(mod, ticks_b, bar_times=bar_times)
    rows = _ctx_rows(
        mod, [base],
        [("2026-01-05 11:00", "2026-01-05 14:00:00.000",
          "2026-01-05 14:00:00.000") + base[3:]], ctx)
    assert rows.iloc[0]["exit_note"] == "outside_exit_bar"


def test_protective_exit_wrong_side_no_trigger_flagged():
    """B 的出场 tick 上正确一侧并未穿价（按错误一侧触发）→ no_trigger_at_fill。"""
    mod = _load_script()
    t15 = pd.Timestamp("2026-01-06 01:00")
    bar_times = [pd.Timestamp("2026-01-05 10:00") + timedelta(hours=i)
                 for i in range(20)]
    atr = [np.nan] * 14 + [1.0] * 6
    # SL=100.9（Ask 侧）：出场 tick 的 Ask=100.8 未穿，Bid=100.95 只是"错误一侧"
    ticks = [tick(t15, 100.0, 100.5),
             tick(t15 + timedelta(minutes=30), 100.95, 100.8),
             tick(t15 + timedelta(minutes=59, seconds=59, milliseconds=999),
                  100.0, 100.5)]
    ctx = make_ctx(mod, ticks, rules=ExitRules(stop_loss_atr=1.0), atr=atr,
                   bar_times=bar_times, slippage_price=0.1)
    short_a = (t15, t15, t15, "short", 1.0, 99.9, 101.0, -1.1, -1.1, 0.0,
               0.0, 0.5, 0.0, "stop")
    short_b = (t15, t15 + timedelta(minutes=30), t15 + timedelta(minutes=30),
               "short", 1.0, 99.9, 100.9, -1.0, -1.0, 0.0, 0.0, 0.05, 0.0,
               "stop")
    rows = _ctx_rows(mod, [short_a], [short_b], ctx)
    row = rows.iloc[0]
    assert row["exit_check"] == "first_cross"
    assert row["exit_note"] == "no_trigger_at_fill"
    assert not row["exit_time_ok"]
    assert not row["row_ok"]


# --------------------------------------------- 净利差三分量拆解（手造）

def _stop_decomp_rows(mod, *, ov_model, cross_bid, costs=None):
    """多头 stop：A 入场 100.6（开 100+点差 0.5+滑点 0.1）、SL=99.6、
    出场=SL−滑点−模型越幅；B 入场 100.6、SL=99.6、出场=穿价 tick−滑点，
    实际越幅=cross_bid 距 SL。"""
    t15 = pd.Timestamp("2026-01-06 01:00")
    bar_times = [pd.Timestamp("2026-01-05 10:00") + timedelta(hours=i)
                 for i in range(20)]
    atr = [np.nan] * 14 + [1.0] * 6
    ticks = [tick(t15, 100.0, 100.5),
             tick(t15 + timedelta(minutes=30), cross_bid, cross_bid + 0.5),
             tick(t15 + timedelta(minutes=59, seconds=59, milliseconds=999),
                  100.0, 100.5)]
    ctx = make_ctx(mod, ticks, rules=ExitRules(stop_loss_atr=1.0), atr=atr,
                   bar_times=bar_times, slippage_price=0.1,
                   costs=costs or FakeCosts(overshoot=ov_model))
    level = 100.6 - 1.0                                     # SL=99.6
    exit_a = level - SLIP_MODEL - ov_model
    exit_b = cross_bid - SLIP_MODEL
    net_a = (exit_a - 100.6) * 1.0
    net_b = (exit_b - 100.6) * 1.0
    a = (t15, t15, t15, "long", 1.0, 100.6, exit_a, net_a, net_a, 0.0,
         0.0, 0.5, 0.0, "stop")
    b = (t15, t15 + timedelta(minutes=30), t15 + timedelta(minutes=30),
         "long", 1.0, 100.6, exit_b, net_b, net_b, 0.0, 0.0, 0.5, 0.0, "stop")
    rows = _ctx_rows(mod, [a], [b], ctx)
    assert rows.iloc[0]["exit_time_ok"]                      # 穿价语义本身正确
    return rows.iloc[0], level


def test_net_decomposition_attributes_overshoot():
    """实际越幅 1.2 >> 模型 p50 0.05：净利差全部落进越幅分量，"其余"≈0。
    旧的"1 点差"净利容差会误判违规（|Δnet|=1.15 > 0.5），新拆解不冤枉。"""
    mod = _load_script()
    level = 99.6                                           # SL=100.6−1×ATR
    row, _ = _stop_decomp_rows(mod, ov_model=0.05, cross_bid=level - 1.2)
    assert row["net_diff"] == pytest.approx(-1.15)
    assert abs(row["net_diff"]) > row["net_tol"]            # 旧口径必违规
    assert row["net_spread_comp"] == pytest.approx(0.0, abs=1e-9)
    assert row["net_overshoot_comp"] == pytest.approx(0.05 - 1.2)
    assert row["net_rest"] == pytest.approx(0.0, abs=1e-9)
    assert row["net_ok"]


def test_net_rest_over_one_spread_is_violation():
    """拆解后"其余"超 1 个点差 → 判违规（点差/越幅解释不掉的部分）。

    构造：机制全部正确（同一穿价 tick 成交、成交价一致），但 B 的 net
    凭空多亏 1.5（例如佣金/隔夜费装配分叉）——点差、越幅分量均为 0，
    残差全部落进"其余"。"""
    mod = _load_script()
    t15 = pd.Timestamp("2026-01-06 01:00")
    bar_times = [pd.Timestamp("2026-01-05 10:00") + timedelta(hours=i)
                 for i in range(20)]
    atr = [np.nan] * 14 + [1.0] * 6
    # 穿价 tick：bid 99.4 穿过 SL=99.6，B 按 99.4−0.1=99.3 成交
    ticks = [tick(t15, 100.0, 100.5),
             tick(t15 + timedelta(minutes=30), 99.4, 99.9),
             tick(t15 + timedelta(minutes=59, seconds=59, milliseconds=999),
                  100.0, 100.5)]
    ctx = make_ctx(mod, ticks, rules=ExitRules(stop_loss_atr=1.0), atr=atr,
                   bar_times=bar_times, slippage_price=0.1,
                   costs=FakeCosts(overshoot=0.0))
    a = (t15, t15, t15, "long", 1.0, 100.6, 99.5, -1.1, -1.1, 0.0,
         0.0, 0.5, 0.0, "stop")
    b = (t15, t15 + timedelta(minutes=30), t15 + timedelta(minutes=30),
         "long", 1.0, 100.6, 99.3, -2.6, -2.6, 0.0, 0.0, 0.5, 0.0, "stop")
    rows = _ctx_rows(mod, [a], [b], ctx)
    row = rows.iloc[0]
    assert row["exit_time_ok"]                     # 机制核对（tick/成交价）全对
    assert row["net_spread_comp"] == pytest.approx(0.0, abs=1e-9)
    # B 的穿价 tick 越幅 0.2（bid 99.4 vs SL 99.6），模型 0 → 越幅分量 −0.2
    assert row["net_overshoot_comp"] == pytest.approx(-0.2, abs=1e-9)
    assert row["net_rest"] == pytest.approx(-1.3, abs=1e-9)
    assert abs(row["net_rest"]) > row["net_tol"]
    assert not row["net_ok"]
    assert not row["row_ok"]


def test_protective_fill_price_mismatch_is_violation():
    """B 在正确的穿价 tick 成交但成交价不等于触发侧价±滑点 → 违规
    （成交价坏不会被越幅分量吞掉）。"""
    mod = _load_script()
    t15 = pd.Timestamp("2026-01-06 01:00")
    bar_times = [pd.Timestamp("2026-01-05 10:00") + timedelta(hours=i)
                 for i in range(20)]
    atr = [np.nan] * 14 + [1.0] * 6
    ticks = [tick(t15, 100.0, 100.5),
             tick(t15 + timedelta(minutes=30), 99.4, 99.9),
             tick(t15 + timedelta(minutes=59, seconds=59, milliseconds=999),
                  100.0, 100.5)]
    ctx = make_ctx(mod, ticks, rules=ExitRules(stop_loss_atr=1.0), atr=atr,
                   bar_times=bar_times, slippage_price=0.1,
                   costs=FakeCosts(overshoot=0.0))
    a = (t15, t15, t15, "long", 1.0, 100.6, 99.5, -1.1, -1.1, 0.0,
         0.0, 0.5, 0.0, "stop")
    b_exit = 97.8                       # 应为穿价 tick bid−滑点 = 99.3
    b = (t15, t15 + timedelta(minutes=30), t15 + timedelta(minutes=30),
         "long", 1.0, 100.6, b_exit, b_exit - 100.6, b_exit - 100.6, 0.0,
         0.0, 0.5, 0.0, "stop")
    rows = _ctx_rows(mod, [a], [b], ctx)
    row = rows.iloc[0]
    assert row["exit_note"] == "fill_price_mismatch"
    assert not row["exit_time_ok"]
    assert not row["row_ok"]


def test_same_ms_exit_first_crossing_tick_price_is_the_reference():
    """I6（第七轮复审）失败测试：出场毫秒内多笔 tick 都穿价——成交价必须
    与**首笔**穿价 tick 的触发侧价±滑点一致（引擎语义即首笔；多笔同价取
    首笔）。在后续同毫秒 tick 的价格上成交 → fill_price_mismatch。

    旧实现与"该毫秒最后一笔 tick"比对：错误地按第二笔成交反而核对通过、
    按首笔正确成交反被误判（两个方向都错）。"""
    mod = _load_script()
    t15 = pd.Timestamp("2026-01-06 01:00")
    t30 = t15 + timedelta(minutes=30)
    bar_times = [pd.Timestamp("2026-01-05 10:00") + timedelta(hours=i)
                 for i in range(20)]
    atr = [np.nan] * 14 + [1.0] * 6
    # SL=99.6（多头 Bid 侧）：t30 两笔同毫秒 tick 均穿价（bid 99.4 / 99.55）
    ticks = [tick(t15, 100.0, 100.5),
             tick(t30, 99.4, 99.9),
             tick(t30, 99.55, 100.05),
             tick(t15 + timedelta(minutes=59, seconds=59, milliseconds=999),
                  100.0, 100.5)]
    ctx = make_ctx(mod, ticks, rules=ExitRules(stop_loss_atr=1.0), atr=atr,
                   bar_times=bar_times, slippage_price=0.1,
                   costs=FakeCosts(overshoot=0.0))
    a = (t15, t15, t15, "long", 1.0, 100.6, 99.5, -1.1, -1.1, 0.0,
         0.0, 0.5, 0.0, "stop")
    # B 按第二笔穿价 tick（99.55−0.1）而非首笔（99.4−0.1）成交 → 违规
    b_bad = (t15, t30, t30, "long", 1.0, 100.6, 99.45, -1.15, -1.15, 0.0,
             0.0, 0.5, 0.0, "stop")
    rows = _ctx_rows(mod, [a], [b_bad], ctx)
    row = rows.iloc[0]
    assert row["exit_note"] == "fill_price_mismatch"
    assert not row["exit_time_ok"]
    # 同数据：B 按首笔穿价 tick 成交（99.4−0.1=99.3）→ 通过
    b_ok = (t15, t30, t30, "long", 1.0, 100.6, 99.3, -1.3, -1.3, 0.0,
            0.0, 0.5, 0.0, "stop")
    rows = _ctx_rows(mod, [a], [b_ok], ctx)
    assert rows.iloc[0]["exit_time_ok"]
    assert rows.iloc[0]["exit_note"] == ""


def test_entry_ms_ticks_after_entry_fill_checked_for_earlier_cross():
    """I6（第七轮复审）失败测试：入场毫秒不整段跳过——引擎语义是"入场
    成交 tick 自身不触发 SL/TP，**下一笔 tick 起**逐 tick 监控"，同毫秒内
    排在入场 tick 之后的报价同样参与判定。

    旧实现 searchsorted(side=right) 跳过入场毫秒全部 tick：入场同毫秒后续
    tick 已穿价、B 却拖到 +30m 才出场时，earlier_cross 检不出来。"""
    mod = _load_script()
    t15 = pd.Timestamp("2026-01-06 01:00")
    t30 = t15 + timedelta(minutes=30)
    bar_times = [pd.Timestamp("2026-01-05 10:00") + timedelta(hours=i)
                 for i in range(20)]
    atr = [np.nan] * 14 + [1.0] * 6
    # 空头：入场 99.9（bid100.0−滑点0.1）、SL=99.9+1×ATR=100.9（Ask 侧）。
    # 入场 tick 之后同毫秒第二笔 Ask=100.95 已穿 SL —— 引擎应在该笔止损
    ticks = [tick(t15, 100.0, 100.5),
             tick(t15, 100.05, 100.95),
             tick(t30, 100.1, 101.0)]
    ctx = make_ctx(mod, ticks, rules=ExitRules(stop_loss_atr=1.0), atr=atr,
                   bar_times=bar_times, slippage_price=0.1)
    short_a = (t15, t15, t15, "short", 1.0, 99.9, 101.0, -1.1, -1.1, 0.0,
               0.0, 0.5, 0.0, "stop")
    # B 拖到 +30m 才止损 → 入场同毫秒的穿价 tick 必须被判 earlier_cross
    b_late = (t15, t30, t30, "short", 1.0, 99.9, 101.1, -1.2, -1.2, 0.0,
              0.0, 0.1, 0.0, "stop")
    rows = _ctx_rows(mod, [short_a], [b_late], ctx)
    assert rows.iloc[0]["exit_note"] == "earlier_cross"
    assert not rows.iloc[0]["exit_time_ok"]
    # 对照：入场/出场同毫秒，B 在穿价那笔（Ask100.95+0.1）成交 → 通过
    b_same = (t15, t15, t15, "short", 1.0, 99.9, 101.05, -1.15, -1.15, 0.0,
              0.0, 0.05, 0.0, "stop")
    rows = _ctx_rows(mod, [short_a], [b_same], ctx)
    assert rows.iloc[0]["exit_time_ok"]
    assert rows.iloc[0]["exit_note"] == ""


def test_short_protective_spread_difference_not_in_rest():
    """空头非跳空保护性出场：A 的成交价"预减 spr 再加回"、B 按触发侧价格成交
    ——实际点差与模型点差的差不进净利。拆解：点差分量=0，其余≈0。"""
    mod = _load_script()
    t15 = pd.Timestamp("2026-01-06 01:00")
    bar_times = [pd.Timestamp("2026-01-05 10:00") + timedelta(hours=i)
                 for i in range(20)]
    atr = [np.nan] * 14 + [1.0] * 6
    # B 出场 tick 实际点差 2.5（bid 98.45 / ask 100.95），模型点差 0.5
    ticks = [tick(t15, 100.0, 100.5),
             tick(t15 + timedelta(minutes=30), 98.45, 100.95),
             tick(t15 + timedelta(minutes=59, seconds=59, milliseconds=999),
                  100.0, 100.5)]
    ctx = make_ctx(mod, ticks, rules=ExitRules(stop_loss_atr=1.0), atr=atr,
                   bar_times=bar_times, slippage_price=0.1,
                   costs=FakeCosts(overshoot=0.0))
    # A：入场 99.9、SL=100.9、base=SL−spr=100.4 → 出场=100.4+0.5+0.1=101.0
    a = (t15, t15, t15, "short", 1.0, 99.9, 101.0, -1.1, -1.1, 0.0,
         0.0, 0.5, 0.0, "stop")
    # B：出场=触发侧 Ask 100.95+0.1=101.05（实际越幅 0.05）
    b = (t15, t15 + timedelta(minutes=30), t15 + timedelta(minutes=30),
         "short", 1.0, 99.9, 101.05, -1.15, -1.15, 0.0, 0.0, 2.5, 0.0,
         "stop")
    rows = _ctx_rows(mod, [a], [b], ctx)
    row = rows.iloc[0]
    assert row["exit_time_ok"]
    assert row["net_spread_comp"] == pytest.approx(0.0, abs=1e-9)
    assert row["net_overshoot_comp"] == pytest.approx(-0.05)
    assert row["net_rest"] == pytest.approx(0.0, abs=1e-9)
    assert row["net_ok"]


# --------------------------------------------- align_trades：贪心对齐

def test_align_trades_pairs_and_flags_unmatched():
    mod = _load_script()
    ta = trades_frame([
        ("2026-01-05 10:00", "2026-01-05 11:00", "2026-01-05 11:00", "long",
         1.0, 100.0, 101.0, 1.0, 0.5, 0.0, 0.0, 0.5, 0.0, "signal"),
        ("2026-01-05 12:00", "2026-01-05 13:00", "2026-01-05 13:00", "short",
         1.0, 101.0, 100.0, -1.0, -0.5, 0.0, 0.0, 0.5, 0.0, "signal"),
    ])
    tb = trades_frame([
        ("2026-01-05 10:05", "2026-01-05 11:02", "2026-01-05 11:02", "long",
         1.0, 100.0, 101.0, 1.0, 0.4, 0.0, 0.0, 0.5, 0.0, "signal"),
        ("2026-01-05 15:00", "2026-01-05 16:00", "2026-01-05 16:00", "long",
         1.0, 99.0, 100.0, 1.0, 0.2, 0.0, 0.0, 0.5, 0.0, "end_of_data"),
    ])
    pairs = mod.align_trades(ta, tb, max_entry_skew_ms=3_600_000)
    assert pairs == [(0, 0), (1, None), (None, 1)]


def test_align_trades_never_pairs_opposite_sides():
    mod = _load_script()
    ta = trades_frame([("2026-01-05 10:00", "2026-01-05 11:00",
                        "2026-01-05 11:00", "long", 1.0, 100.0, 101.0, 1.0,
                        0.5, 0.0, 0.0, 0.5, 0.0, "signal")])
    tb = trades_frame([("2026-01-05 10:00", "2026-01-05 11:00",
                        "2026-01-05 11:00", "short", 1.0, 101.0, 100.0, -1.0,
                        -0.5, 0.0, 0.0, 0.5, 0.0, "signal")])
    assert mod.align_trades(ta, tb, max_entry_skew_ms=3_600_000) == [
        (0, None), (None, 0)]


def test_unmatched_trades_make_report_fail():
    mod = _load_script()
    rows = mod.build_diff_rows(
        trades_frame([("2026-01-05 10:00", "2026-01-05 11:00",
                       "2026-01-05 11:00", "long", 1.0, 100.0, 101.0, 1.0,
                       0.5, 0.0, 0.0, 0.5, 0.0, "signal")]),
        trades_frame([("2026-01-05 14:00", "2026-01-05 15:00",
                       "2026-01-05 15:00", "long", 1.0, 100.0, 101.0, 1.0,
                       0.5, 0.0, 0.0, 0.5, 0.0, "signal")]),
        [(0, None), (None, 0)], tf_seconds=3600)
    assert len(rows) == 2
    assert set(rows["match"]) == {"only_a", "only_b"}
    assert not rows["row_ok"].all()
    summary = mod.summarize_net_diffs(rows)
    assert summary["n_only_a"] == 1 and summary["n_only_b"] == 1


def test_gap_reasons_compare_equal_to_plain_stop():
    """A 层 stop_gap/take_profit_gap 与 B 层 stop/take_profit 视为同因。"""
    mod = _load_script()
    rows = mod.build_diff_rows(
        trades_frame([("2026-01-05 10:00", "2026-01-05 11:00",
                       "2026-01-05 11:00", "long", 1.0, 100.0, 99.0, -1.0,
                       -0.5, 0.0, 0.0, 0.5, 0.0, "stop_gap")]),
        trades_frame([("2026-01-05 10:00", "2026-01-05 11:00",
                       "2026-01-05 11:00", "long", 1.0, 100.0, 99.0, -1.0,
                       -0.5, 0.0, 0.0, 0.5, 0.0, "stop")]),
        [(0, 0)], tf_seconds=3600)
    assert rows.iloc[0]["exit_reason_a"] == "stop_gap"
    assert rows.iloc[0]["exit_reason_b"] == "stop"
    assert rows.iloc[0]["reason_ok"]


# --------------------------------------------- 净利差分布汇总

def test_summarize_net_diffs_hand_values():
    mod = _load_script()
    rows = pd.DataFrame({
        "net_diff": [1.0, -2.0, 3.0, -4.0],
        "net_rest": [1.0, -2.0, 3.0, -4.0],
        "net_tol": [1.0, 1.0, 1.0, 1.0],
        "entry_time_ok": [True] * 4,
        "exit_time_ok": [True] * 4,
        "reason_ok": [True] * 4,
        "net_ok": [True, False, False, False],
        "row_ok": [True, False, False, False],
        "entry_silent": [False] * 4,
        "exit_silent": [False] * 4,
        "match": ["paired"] * 4,
    })
    stats = mod.summarize_net_diffs(rows)
    nd = stats["net_diff"]
    assert nd["count"] == 4
    assert nd["mean"] == pytest.approx(-0.5)
    assert nd["median"] == pytest.approx(-0.5)
    assert nd["min"] == pytest.approx(-4.0)
    assert nd["max"] == pytest.approx(3.0)
    assert nd["p95_abs"] == pytest.approx(3.85)
    assert nd["within_tol"] == 1
    assert stats["n_silent_entry"] == 0 and stats["n_silent_exit"] == 0


# --------------------------------------------- tick 覆盖期窗口裁剪

def hour_bars(start, n):
    times = [pd.Timestamp(start) + timedelta(hours=i) for i in range(n)]
    return pd.DataFrame({
        "time": pd.to_datetime(times), "open": 100.0, "high": 100.5,
        "low": 99.5, "close": 100.0, "tick_volume": 10, "spread": 5,
        "real_volume": 0,
    })


def test_bars_in_tick_window_trims_to_coverage():
    mod = _load_script()
    df = hour_bars("2026-01-05 08:00", 11)               # 08:00 … 18:00
    out = mod.bars_in_tick_window(
        df, first_msc=msc("2026-01-05 09:30"),
        last_msc=msc("2026-01-05 17:30"), tf_seconds=3600)
    assert list(out["time"]) == [pd.Timestamp(f"2026-01-05 {h:02d}:00")
                                 for h in range(9, 17)]
    # 显式 start/end 与覆盖期取交集
    out2 = mod.bars_in_tick_window(
        df, first_msc=msc("2026-01-05 09:30"),
        last_msc=msc("2026-01-05 17:30"), tf_seconds=3600,
        start=pd.Timestamp("2026-01-05 12:00"),
        end=pd.Timestamp("2026-01-05 20:00"))
    assert out2["time"].iloc[0] == pd.Timestamp("2026-01-05 11:00")
    assert out2["time"].iloc[-1] == pd.Timestamp("2026-01-05 16:00")


def test_bars_in_tick_window_too_few_bars_is_error():
    mod = _load_script()
    df = hour_bars("2026-01-05 08:00", 3)
    with pytest.raises(ValueError, match="不足"):
        mod.bars_in_tick_window(df, first_msc=msc("2026-01-05 09:30"),
                                last_msc=msc("2026-01-05 09:45"),
                                tf_seconds=3600)


# --------------------------------------------- CLI 端到端（真实成本装配）

def _make_data_root(tmp_path, n_bars=30):
    from mt5data.store import save_bars
    from mt5data.ticks import save_ticks, TICK_COLUMNS

    root = tmp_path / "data"
    root.mkdir(exist_ok=True)
    df = flat_bars(pd.Timestamp("2026-01-05 00:00"), n_bars, close=100.0)
    for i in range(1, n_bars):
        df.loc[i, ["open", "high", "low", "close"]] = [100.0 + i, 100.9 + i,
                                                       99.6 + i, 100.5 + i]
    save_bars(str(root), "XAUUSD", "H1", df)
    ticks = ohlc_ticks(df, 0.5)
    tdf = pd.DataFrame(ticks, columns=["time_msc", "bid", "ask"])
    tdf["last"] = 0.0
    tdf["volume"] = 0.0
    tdf["flags"] = 0
    save_ticks(str(root), "XAUUSD", tdf[TICK_COLUMNS])
    specs = root / "specs"
    specs.mkdir(exist_ok=True)
    (specs / "latest.json").write_text(
        json.dumps({"XAUUSD": {"contract_size": 100.0}}), encoding="utf-8")
    return root


@pytest.fixture
def data_root(tmp_path):
    return _make_data_root(tmp_path)


def test_cli_end_to_end_ok(tmp_path, data_root, monkeypatch):
    mod = _load_script()
    out = tmp_path / "reports"
    code = mod.main([
        "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
        "--symbol", "XAUUSD", "--timeframe", "H1", "--params", "{}",
        "--data-root", str(data_root), "--volume-lots", "0.01",
        "--slippage-price", "0.05", "--allow-zero-swap",
        "--out", str(out)])
    assert code == 0
    run_dirs = list(out.iterdir())
    assert len(run_dirs) == 1
    files = {p.name for p in run_dirs[0].iterdir()}
    assert {"diff.csv", "summary.json", "trades_a.csv", "trades_b.csv"} <= files
    with open(run_dirs[0] / "summary.json", encoding="utf-8") as f:
        summary = json.load(f)
    assert summary["ok"] is True
    assert summary["n_paired"] == 1
    assert summary["n_only_a"] == summary["n_only_b"] == 0
    assert summary["engine_b_version"].startswith("B-")
    # T40：容差表按机制（无 120s / 1R / 6bar）
    assert summary["tolerances"]["fill_time_ms"] == 1
    assert summary["tolerances"]["silent_boundary_ms"] == 2_000
    assert summary["tolerances"]["net_rest_tol_spreads"] == 1.0


def test_cli_quick_trims_window(tmp_path, monkeypatch):
    """--quick：对账窗裁到 tick 覆盖期末端 QUICK_WINDOW_DAYS 天（单组快验）。"""
    mod = _load_script()
    root = _make_data_root(tmp_path, n_bars=24 * 6)        # 6 天 H1
    out = tmp_path / "reports"
    code = mod.main([
        "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
        "--symbol", "XAUUSD", "--timeframe", "H1", "--params", "{}",
        "--data-root", str(root), "--volume-lots", "0.01",
        "--slippage-price", "0.05", "--allow-zero-swap", "--quick",
        "--out", str(out)])
    assert code == 0
    with open(next(out.iterdir()) / "summary.json", encoding="utf-8") as f:
        summary = json.load(f)
    assert summary["meta"]["quick"] is True
    # 6 天窗被裁到末端 3 天：起点从 01-05 推到 ~01-07 23:00，终点不变
    assert summary["meta"]["bars_first"] >= "2026-01-07 23:00"
    assert summary["meta"]["bars_last"] == "2026-01-10 23:00:00"


def test_cli_missing_ticks_is_error(tmp_path, data_root, monkeypatch):
    import shutil
    shutil.rmtree(data_root / "ticks" / "XAUUSD")
    mod = _load_script()
    code = mod.main([
        "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
        "--symbol", "XAUUSD", "--timeframe", "H1", "--params", "{}",
        "--data-root", str(data_root), "--allow-zero-swap",
        "--out", str(tmp_path / "reports")])
    assert code == 1


def test_buyhold_reference_is_untouched():
    """reconcile_ab 不改策略库：BuyHold 信号仍恒 1。"""
    bars = base4()
    sig = BuyHold().generate_signals(bars, {})
    assert set(sig) == {1}


# --------------------------------------------- T27（H3）：--ops-ack 闸门

def _add_lockbox(root, cutoff="2026-01-05T06:00:00"):
    from mt5data.lockbox import write_global_cutoff
    write_global_cutoff(str(root), pd.Timestamp(cutoff), note="测试")


def test_cli_requires_ops_ack_when_window_hits_lockbox(tmp_path, data_root,
                                                       capsys):
    """计划失败测试（T27）：对账窗触到锁箱期而无 --ops-ack → 拒绝并说明，
    不读 bars、不写报告。"""
    _add_lockbox(data_root)                    # cutoff 落在 tick 窗中段
    mod = _load_script()
    code = mod.main([
        "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
        "--symbol", "XAUUSD", "--timeframe", "H1", "--params", "{}",
        "--data-root", str(data_root), "--volume-lots", "0.01",
        "--slippage-price", "0.05", "--allow-zero-swap",
        "--out", str(tmp_path / "reports")])
    assert code == 1
    out = capsys.readouterr().out
    assert "--ops-ack" in out and "锁箱" in out
    assert not list((tmp_path / "reports").iterdir()) \
        if (tmp_path / "reports").exists() else True


def test_cli_ops_ack_records_ledger_row_actor_ops(tmp_path, data_root):
    """--ops-ack：照常对账，并在台账记一行 actor=ops（供审计）。"""
    from mt5quant.research.ledger import Ledger

    _add_lockbox(data_root)
    mod = _load_script()
    code = mod.main([
        "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
        "--symbol", "XAUUSD", "--timeframe", "H1", "--params", "{}",
        "--data-root", str(data_root), "--volume-lots", "0.01",
        "--slippage-price", "0.05", "--allow-zero-swap",
        "--ops-ack",
        "--out", str(tmp_path / "reports")])
    assert code == 0
    ledger_path = data_root / "research" / "ledger.sqlite"
    assert ledger_path.is_file()
    with Ledger(ledger_path, create=False) as led:
        row = led.conn.execute(
            "SELECT card_id, actor, metrics_json FROM runs").fetchone()
    assert row["actor"] == "ops"
    assert row["card_id"].startswith("OPS-")
    assert json.loads(row["metrics_json"])["ok"] is True


def test_cli_window_before_cutoff_needs_no_ack(tmp_path, data_root):
    """对账窗整体早于 cutoff（历史窗对账）→ 不需要 --ops-ack。"""
    _add_lockbox(data_root, cutoff="2030-01-01T00:00:00")
    mod = _load_script()
    code = mod.main([
        "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
        "--symbol", "XAUUSD", "--timeframe", "H1", "--params", "{}",
        "--data-root", str(data_root), "--volume-lots", "0.01",
        "--slippage-price", "0.05", "--allow-zero-swap",
        "--out", str(tmp_path / "reports")])
    assert code == 0
    # 无 ops 行（没读锁箱期数据，不需要豁免登记）
    assert not (data_root / "research" / "ledger.sqlite").exists()
