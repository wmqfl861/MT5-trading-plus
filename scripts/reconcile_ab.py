"""reconcile_ab：A/B 双引擎逐笔对账（运维工具，T10；T40 口径收紧）。

同一策略、同一份 bars、同一 tick 覆盖期，分别跑 A 层（engine_a，逐 bar +
点差模型）与 B 层（engine_b，逐 tick 真实报价），逐笔对齐输出差异表
（入场/出场时间、价格、净利），并汇总每笔净利差的分布。

锁箱豁免声明（T27/H3 收口：显式 --ops-ack）：
- 研究路径（scripts/run_backtest.py / bt.py）必须经研究库或
  mt5data.lockbox.research_bars，看不到锁箱段；本脚本直读 store.load_bars
  全量（对账需要两个引擎吃同一份 bars，其中含锁箱期行）。
- 豁免理由：本脚本是运维对账工具，不产生任何研究结论——它只校验两个执行
  引擎在同一数据上的口径一致性，输出的是差异表与容差判定，不会进入试验
  台账或假设卡。A/B 两层喂的是同一份 bars，锁箱段数据不会单侧泄漏成
  "可研究的 alpha"。
- 收口（T27）：对账窗触到锁箱期时必须显式 --ops-ack（运维确认上述豁免
  理由），否则在读 bars 之前拒绝退出；给了 --ops-ack 的运行会在台账
  （{data-root}/research/ledger.sqlite）记一行 actor=ops 供事后审计。

对账口径（T40 / H6 / 铁律第 14 条：容差只能按机制设定，不因结果放宽）：
T26 引入的结果宽容差（入场/signal 出场 120s、保护性出场净利
max(1 点差, 1×初始风险)、保护性出场时间 6 根 bar）已全部撤回——它们让
"A 层止损按价位、B 层按穿价 tick"以外的分歧（例如 B 层用错触发侧、漏掉
首笔穿价 tick）测不出来。改为逐项按机制核对（需要读 tick：CLI 用
load_ticks 月分区按品种加载窗口内 tick，reconcile() 复用同一个 loader）：

1. **入场 / signal 出场**：B 层成交 = 就绪时刻（bar 边界 + latency_ms）
   之后第一笔 tick，误差 ≤ TOL_FILL_TIME_MS（1ms，B 实现语义如此，
   成交时刻即该 tick 的 time_msc）；A 层 = bar 开盘标签（机制参照）。
   边界后超过 SILENT_BOUNDARY_MS（2s）才有首笔 tick 的行单列"静默边界"
   （entry_silent / exit_silent），不算违规——B 在第一笔可用 tick 成交
   正是机制语义，差的是行情本身没有报价。
2. **保护性出场（stop / take_profit）**：B 层成交 tick = 按正确一侧
   （多头 Bid、空头 Ask）第一笔穿过价位的 tick——出场 tick 上必须已触发，
   且入场 tick 之后、出场 tick 之前不存在任何已穿价（stop 或 TP，任一）
   的 tick；A 层 = 价位（跳空时为开盘价，机制参照）。零容差。
3. **收盘价类出场（time_stop / week_close / end_of_data）**：B 层成交 =
   出场 bar 收盘边界（A 的 exit_fill_time）前最后一笔 tick，且落在出场
   bar 区间 [bar 开盘, 收盘) 内。零容差（B 实现语义即"prev tick"）。
4. **净利差拆解**：net_diff = net_spread_comp + net_overshoot_comp +
   net_rest，"其余"（net_rest）绝对值 > NET_REST_TOL_SPREADS（1）个
   点差成本判违规：
   - 点差分量 = 模型点差 vs 实际点差：多头挂入场腿、空头挂出场腿，取
     −(spread_cost_b − spread_cost_a)；空头非跳空 stop/take_profit 例外
     取 0——A 层成交价"预减 spr 再加回"（engine_a._protective_exit）、
     B 层按触发侧价格成交，实际点差不进净利（只影响触发早晚）；
   - 止损越幅分量（T36/A-1.6）＝(模型 p50 − B 实际越幅)×手数×合约大小，
     仅 reason=stop 的行；stop_gap 双方都按开盘/穿价 tick 成交、TP 不
     建模越幅，均取 0（其成交差如实落进"其余"）；
   - 其余 = 残差（滑点差、bid 与 bar 开盘/收盘之差、跳空止盈保守口径等）。
5. **配对与原因**：配对窗 1 根 bar（对齐用，不是容差；超窗即 only_a/
   only_b 违规）；exit_reason 一致（stop_gap/take_profit_gap 与
   stop/take_profit 视为同因）。

用法（Git Bash，四组全量对账见 docs/reports/reconciliation-v3-*.md）：
    .venv/Scripts/python.exe scripts/reconcile_ab.py \
        --strategy mt5quant.backtest.reference:MaCrossATR \
        --symbol XAUUSD --timeframe H1 \
        --params '{"fast":10,"slow":30,"stop_loss_atr":2.0,"take_profit_atr":3.0}' \
        --data-root data --ops-ack
    # 单组快验（--quick）：窗口裁到 tick 覆盖期末端 QUICK_WINDOW_DAYS 天
    .venv/Scripts/python.exe scripts/reconcile_ab.py … --quick --ops-ack

约定：
- 对账期 = tick 覆盖期 ∩ [--start, --end]：bars 只保留收盘时刻落在该窗口内
  的（两端含边界），保证 B 层所需 tick 在边界处可用；窗口内 bar 区间缺 tick
  时由 B 层 TickCoverageError fail-fast，绝不静默跳过。
- 成本装配复用 run_backtest.py（build_costs 等，import 不改）；T36 起若
  点差缓存带越幅表（version 2 的 overshoot 键），自动挂进 Costs——A-1.6
  的 stop 成交口径生效，拆解的越幅分量用同一模型。
- 退出码：0 = 全部在容差内；2 = 存在超容差差异（差异表已写出）；1 = 装配
  或引擎报错。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # 复用 run_backtest 的装配

from mt5data.lockbox import cutoff_for, load_config  # noqa: E402
from mt5data.store import TIMEFRAME_SECONDS, load_bars  # noqa: E402
from mt5data.ticks import load_ticks, read_ticks_watermark  # noqa: E402
from mt5quant.backtest.engine_a import (  # noqa: E402
    ENGINE_VERSION, NY_TZ, atr_series, run_backtest,
)
from mt5quant.backtest.engine_b import (  # noqa: E402
    ENGINE_VERSION_B, run_backtest_ticks,
)
from mt5quant.backtest.report import (  # noqa: E402
    data_sha256, make_run_dir,
)
from mt5quant.strategies import ExitRules  # noqa: E402
from mt5data.store import atomic_replace  # noqa: E402
from run_backtest import (  # noqa: E402
    ZeroCosts, build_costs, canonical_json_sha256, check_clock,
    load_strategy, resolve_contract_size, spread_model_cache_path,
)

# ------------------------------------------------ 容差常量（T40：按机制设定）
TOL_FILL_TIME_MS = 1          # 入场/signal 出场：B 成交=就绪后第一笔 tick 的
                              # time_msc（B 实现语义逐位一致，1ms 护毫舍入）
SILENT_BOUNDARY_MS = 2_000    # 边界后 >2s 才有首笔 tick → "静默边界"：
                              # 单列不判违规（机制上 B 就该在该 tick 成交）
NET_REST_TOL_SPREADS = 1.0    # 净利差拆解后"其余"分量 ≤ 1 个点差成本；
                              # 点差分量与越幅分量（机制已知差异）不设上限

QUICK_WINDOW_DAYS = 3         # --quick：对账窗裁到 tick 覆盖期末端天数

# A 层跳空变体与 B 层逐 tick 出场视为同因（B 层无 stop_gap 之分）
_REASON_EQUIV = {"stop_gap": "stop", "take_profit_gap": "take_profit"}
# 收盘价类出场（按 bar 收盘价成交）：B 在收盘前最后一笔 tick
_CLOSE_PRICE_REASONS = {"time_stop", "week_close", "end_of_data"}

MAX_PRINT_ROWS = 50


def _ms(ts) -> int:
    return int(pd.Timestamp(ts).value // 1_000_000)


def _series_ms(times: pd.Series) -> pd.Series:
    """时间列 → 毫秒 int64（显式经 ns 单位转换；pandas 3 的 datetime64[us]
    直接 astype('int64') 会得到微秒，绝不能裸转）。"""
    return (times.astype("datetime64[ns]").astype("int64") // 1_000_000)


def _f(row, col) -> float:
    """行字段安全取 float（缺列/None/NaN → nan）。"""
    try:
        v = float(row[col])
    except (KeyError, TypeError, ValueError):
        return float("nan")
    return v


# ------------------------------------------------ tick 探针（机制核对读 tick）

_EMPTY_TICKS = (np.empty(0, dtype="int64"), np.empty(0, dtype="float64"),
                np.empty(0, dtype="float64"))


class TickProbe:
    """按月分区拉取 tick 的小窗口探针（LRU 缓存最近若干个整月块）。

    机制核对只需要每个成交附近的小窗口；逐行直接调 ticks_loader 会反复
    读同一个月份分区（load_ticks 按月分区裁剪），这里把整月块读一次缓存，
    searchsorted 切窗口。与 B 层引擎共用同一个 loader、同一个月分区语义，
    看到的 tick 与引擎逐位一致。
    """

    def __init__(self, loader, max_month_blocks: int = 4):
        self._loader = loader
        self._blocks: OrderedDict[int, tuple] = OrderedDict()
        self._max = max(1, int(max_month_blocks))

    @staticmethod
    def _month_start(ms: int) -> tuple[int, int]:
        """毫秒时刻 → (所在月起点 ms, 下月起点 ms)。"""
        ts = pd.Timestamp(int(ms), unit="ms")
        m0 = pd.Timestamp(ts.year, ts.month, 1)
        m1 = m0 + pd.offsets.MonthBegin(1)
        return (int(m0.value // 1_000_000), int(m1.value // 1_000_000))

    def _month_block(self, month_ms: int) -> tuple:
        if month_ms in self._blocks:
            self._blocks.move_to_end(month_ms)
            return self._blocks[month_ms]
        _, nxt_ms = self._month_start(month_ms)
        df = self._loader(start_msc=month_ms, end_msc=nxt_ms - 1)
        if df is None or len(df) == 0:
            block = _EMPTY_TICKS
        else:
            missing = {"time_msc", "bid", "ask"} - set(df.columns)
            if missing:
                raise ValueError(
                    f"tick 帧缺列：{sorted(missing)}"
                    "（需 time_msc/bid/ask，参见 mt5data.ticks.TICK_COLUMNS）")
            block = (df["time_msc"].to_numpy(dtype="int64"),
                     df["bid"].to_numpy(dtype="float64"),
                     df["ask"].to_numpy(dtype="float64"))
        self._blocks[month_ms] = block
        if len(self._blocks) > self._max:
            self._blocks.popitem(last=False)   # LRU 淘汰最旧月块
        return block

    def window(self, start_msc: int, end_msc: int) -> tuple:
        """[start_msc, end_msc]（含两端）的 (msc, bid, ask) 三个 numpy 数组。"""
        s, e = int(start_msc), int(end_msc)
        if s > e:
            return _EMPTY_TICKS
        parts_m, parts_b, parts_a = [], [], []
        m = s
        while m <= e:
            m0, m1 = self._month_start(m)
            bmsc, bbid, bask = self._month_block(m0)
            if bmsc.size:
                lo = int(np.searchsorted(bmsc, m, side="left"))
                hi = int(np.searchsorted(bmsc, min(e, m1 - 1), side="right"))
                if hi > lo:
                    parts_m.append(bmsc[lo:hi])
                    parts_b.append(bbid[lo:hi])
                    parts_a.append(bask[lo:hi])
            if m1 > e:
                break
            m = m1
        if not parts_m:
            return _EMPTY_TICKS
        if len(parts_m) == 1:
            return (parts_m[0], parts_b[0], parts_a[0])
        return (np.concatenate(parts_m), np.concatenate(parts_b),
                np.concatenate(parts_a))


@dataclass
class MechanismContext:
    """机制核对所需的执行语义上下文（reconcile() 内部构造）。"""

    probe: TickProbe
    rules: ExitRules
    atr: np.ndarray                     # atr_series(bars)（与引擎同一函数）
    bar_open_ms: list[int]              # bars["time"] 的毫秒开盘时刻
    tf_seconds: int
    latency_ms: int = 0
    slippage_price: float = 0.0         # B 层每腿滑点（越幅回推用）
    costs: object = None                # 提供 stop_overshoot → A 的模型越幅
    symbol: str = ""
    contract_size: float = 1.0


# ------------------------------------------------ 逐笔对齐

def align_trades(trades_a: pd.DataFrame, trades_b: pd.DataFrame, *,
                 max_entry_skew_ms: int) -> list:
    """贪心对齐两层交易列表（均按时间升序），返回 [(i|None, j|None), ...]。

    规则：同方向且入场时间差 ≤ max_entry_skew_ms 才可配对；否则入场更早的
    一侧记为 unmatched 并前进——配对窗是"对齐手段"而非容差（成交 tick 总在
    信号 bar 区间内，正确实现不会超窗）；超窗的分歧以 only_a/only_b 违规
    浮出，不会吞成"已配对但合规"。
    """
    pairs: list = []
    i = j = 0
    na, nb = len(trades_a), len(trades_b)
    while i < na or j < nb:
        if i >= na:
            pairs.append((None, j)); j += 1
            continue
        if j >= nb:
            pairs.append((i, None)); i += 1
            continue
        ta = pd.Timestamp(trades_a["entry_time"].iloc[i])
        tb = pd.Timestamp(trades_b["entry_time"].iloc[j])
        skew = abs(_ms(tb) - _ms(ta))
        same_side = (str(trades_a["side"].iloc[i])
                     == str(trades_b["side"].iloc[j]))
        if same_side and skew <= max_entry_skew_ms:
            pairs.append((i, j)); i += 1; j += 1
        elif ta <= tb:
            pairs.append((i, None)); i += 1
        else:
            pairs.append((None, j)); j += 1
    return pairs


# ------------------------------------------------ 机制核对（逐项）

def _check_fill_at_first_tick(probe: TickProbe, boundary_ms: int,
                              ready_ms: int, fill_ms: int, *,
                              tol_fill_ms: int, silent_ms: int) -> dict:
    """入场 / signal 出场共用核对：B 成交=就绪时刻后第一笔 tick。

    返回 {first_tick_ms, gap_ms, boundary_gap_ms, silent, ok, note}。
    静默判定与违规判定独立：first_tick 距边界 >silent_ms → 静默（不判违规）；
    B 成交不是该首笔 tick（|fill−first| > tol）→ 违规（not_first_tick）。
    """
    out = {"first_tick_ms": None, "gap_ms": None, "boundary_gap_ms": None,
           "silent": False, "ok": False, "note": ""}
    if fill_ms < ready_ms:
        out["note"] = "before_ready"
        return out
    w = probe.window(ready_ms, fill_ms)     # B 的成交 tick 本身在窗口内
    if len(w[0]) == 0:
        out["note"] = "no_ticks"
        return out
    first = int(w[0][0])
    out["first_tick_ms"] = first
    out["gap_ms"] = fill_ms - first
    out["boundary_gap_ms"] = first - boundary_ms
    out["silent"] = (first - boundary_ms) > silent_ms
    out["ok"] = abs(fill_ms - first) <= tol_fill_ms
    if not out["ok"]:
        out["note"] = "not_first_tick"
    return out


def _check_close_exit(probe: TickProbe, bar_open_ms: int, close_ms: int,
                      fill_ms: int) -> dict:
    """收盘价类出场核对：B 成交=出场 bar 收盘边界前最后一笔 tick。

    零容差（机制语义）：成交必须落在 [bar 开盘, 收盘) 内，且成交之后、
    收盘边界之前没有任何 tick（若有更晚 tick，B 就该在那笔成交）。
    """
    out = {"ok": False, "note": ""}
    if not (bar_open_ms <= fill_ms < close_ms):
        out["note"] = "outside_exit_bar"
        return out
    w = probe.window(fill_ms + 1, close_ms - 1)
    if len(w[0]) != 0:
        out["note"] = "not_last_tick"
        return out
    out["ok"] = True
    return out


def _stop_levels(ctx: MechanismContext, entry_price: float, entry_ms: int,
                 is_long: bool) -> dict | None:
    """按引擎语义重算 SL/TP 价位（与 engine_b._open_at 同式：信号 bar 的
    ATR、成交价 ± 系数）。价位不可得（无规则/ATR 缺）→ None。"""
    bar_idx = bisect_right(ctx.bar_open_ms, entry_ms) - 1
    sig = bar_idx - 1
    if sig < 0 or sig >= len(ctx.atr):
        return None
    atr_v = float(ctx.atr[sig])
    if not math.isfinite(atr_v):
        return None
    out: dict = {}
    if ctx.rules.stop_loss_atr is not None:
        out["stop"] = (entry_price - ctx.rules.stop_loss_atr * atr_v if is_long
                       else entry_price + ctx.rules.stop_loss_atr * atr_v)
    if ctx.rules.take_profit_atr is not None:
        out["take_profit"] = (
            entry_price + ctx.rules.take_profit_atr * atr_v if is_long
            else entry_price - ctx.rules.take_profit_atr * atr_v)
    return out or None


def _cond_hit(level_name: str, level: float, is_long: bool,
              bid: np.ndarray, ask: np.ndarray) -> np.ndarray:
    """触发侧条件（与 engine_b 逐 tick 判定同谓词）：多头按 Bid、空头按 Ask。"""
    if level_name == "stop":
        return (bid <= level) if is_long else (ask >= level)
    return (bid >= level) if is_long else (ask <= level)


def _check_protective_exit(probe: TickProbe, ctx: MechanismContext,
                           b: pd.Series) -> dict:
    """保护性出场核对：B 成交 tick=正确一侧第一笔穿过价位的 tick。

    - 出场 tick 上该出场原因的触发条件必须成立（按错一侧触发的实现在此
      暴露为 no_trigger_at_fill）；
    - 成交价 = 该 tick 触发侧价格 ± slippage_price（多头 Bid−slip /
      空头 Ask+slip）——成交价坏在别处不会被越幅分量吞掉
      （fill_price_mismatch）；
    - 入场 tick 之后、出场 tick 之前不存在任何已穿价（SL 或 TP 任一）的
    tick——引擎逐 tick 先查止损后查止盈，更早穿价必然更早出场
    （earlier_cross）。
    """
    out = {"ok": False, "note": ""}
    is_long = str(b["side"]) == "long"
    entry_ms = _ms(b["entry_time"])
    exit_ms = _ms(b["exit_time"])
    levels = _stop_levels(ctx, float(b["entry_price"]), entry_ms, is_long)
    reason = str(b["exit_reason"])
    if levels is None or reason not in levels:
        out["note"] = "level_unknown"
        return out
    w = probe.window(entry_ms, exit_ms)
    if len(w[0]) == 0 or int(w[0][-1]) != exit_ms:
        out["note"] = "no_ticks"
        return out
    fill_bid, fill_ask = float(w[1][-1]), float(w[2][-1])
    if not bool(_cond_hit(reason, levels[reason], is_long,
                          np.array([fill_bid]), np.array([fill_ask]))[0]):
        out["note"] = "no_trigger_at_fill"
        return out
    expected = (fill_bid - float(ctx.slippage_price) if is_long
                else fill_ask + float(ctx.slippage_price))
    if abs(float(b["exit_price"]) - expected) > 1e-9 * max(1.0, abs(expected)):
        out["note"] = "fill_price_mismatch"
        return out
    j0 = int(np.searchsorted(w[0], entry_ms, side="right"))   # 入场 tick 不自触发
    if j0 < len(w[0]) - 1:
        bid_, ask_ = w[1][j0:-1], w[2][j0:-1]
        for name, lv in levels.items():
            if bool(_cond_hit(name, lv, is_long, bid_, ask_).any()):
                out["note"] = "earlier_cross"
                return out
    out["ok"] = True
    return out


# ------------------------------------------------ 净利差三分量拆解

def _ny_hour(ts) -> int:
    return int(pd.Timestamp(ts).tz_localize("UTC")
               .tz_convert(NY_TZ).hour)


def _decompose_net(a: pd.Series, b: pd.Series, reason_key: str,
                   reason_a: str, *, ctx: MechanismContext | None = None,
                   net_rest_tol_spreads: float = NET_REST_TOL_SPREADS) -> dict:
    """net_diff = 点差分量 + 止损越幅分量 + 其余（见模块 docstring 第 4 条）。

    返回 {net_diff, spread_comp, overshoot_comp, rest, tol, ok}；tol =
    net_rest_tol_spreads × max(两侧 spread_cost)（"1 个点差"的金额）。"""
    net_diff = _f(b, "net") - _f(a, "net")
    sc_a, sc_b = _f(a, "spread_cost"), _f(b, "spread_cost")
    is_long = str(a["side"]) == "long"
    if (not is_long) and reason_a in ("stop", "take_profit",
                                      "take_profit_gap"):
        # 空头非跳空保护性出场：A 预减 spr 再加回、B 按触发侧成交——
        # 实际点差不进净利（spread_cost 字段只是报告拆分），分量取 0
        spread_comp = 0.0
    elif math.isfinite(sc_a) and math.isfinite(sc_b):
        spread_comp = -(sc_b - sc_a)
    else:
        spread_comp = 0.0
    overshoot_comp = 0.0
    if reason_a == "stop" and ctx is not None:
        lots_cs = _f(b, "volume_lots") * float(ctx.contract_size)
        ov_actual = _b_stop_overshoot_actual(ctx, b)
        ov_model = _a_stop_overshoot_model(ctx, a, b)
        if math.isfinite(ov_actual) and math.isfinite(ov_model) \
                and math.isfinite(lots_cs) and lots_cs > 0:
            overshoot_comp = (ov_model - ov_actual) * lots_cs
    rest = net_diff - spread_comp - overshoot_comp
    if math.isfinite(sc_a) and math.isfinite(sc_b):
        tol = net_rest_tol_spreads * max(abs(sc_a), abs(sc_b))
    else:
        tol = 0.0
    return {"net_diff": net_diff, "spread_comp": spread_comp,
            "overshoot_comp": overshoot_comp, "rest": rest, "tol": tol,
            "ok": bool(math.isfinite(rest) and abs(rest) <= tol)}


def _b_stop_overshoot_actual(ctx: MechanismContext, b: pd.Series) -> float:
    """B 的实际止损越幅（价格单位）：触发侧价格越过价位的距离。

    exit_price = 触发侧价 ± slippage_price（多头 Bid−slip / 空头 Ask+slip），
    回推触发侧价；价位按 _stop_levels 重算（与引擎同式）。
    不可得 → nan。"""
    is_long = str(b["side"]) == "long"
    levels = _stop_levels(ctx, float(b["entry_price"]), _ms(b["entry_time"]),
                          is_long)
    if levels is None or "stop" not in levels:
        return float("nan")
    if is_long:
        trigger_px = float(b["exit_price"]) + float(ctx.slippage_price)
        return levels["stop"] - trigger_px
    trigger_px = float(b["exit_price"]) - float(ctx.slippage_price)
    return trigger_px - levels["stop"]


def _a_stop_overshoot_model(ctx: MechanismContext, a: pd.Series,
                            b: pd.Series) -> float:
    """A-1.6 加在 stop 成交价上的模型越幅（价格单位）。

    引擎查询 costs.stop_overshoot(symbol, 纽约小时, price=bid 基准价)；
    基准价 = A 的止损价位（多头）／价位−模型点差（空头，与引擎 _close 的
    base_price=sl−spr 同式，spr 取 spread_cost 折算）。模型未提供 → 0。
    """
    ov_fn = getattr(ctx.costs, "stop_overshoot", None) if ctx.costs else None
    if ov_fn is None:
        return 0.0
    is_long = str(a["side"]) == "long"
    levels = _stop_levels(ctx, float(a["entry_price"]), _ms(a["entry_time"]),
                          is_long)
    if levels is None or "stop" not in levels:
        return 0.0
    price = levels["stop"]
    if not is_long:
        lots_cs = _f(a, "volume_lots") * float(ctx.contract_size)
        sc_a = _f(a, "spread_cost")
        spr_a = sc_a / lots_cs if (math.isfinite(sc_a) and math.isfinite(lots_cs)
                                   and lots_cs > 0) else 0.0
        price = levels["stop"] - spr_a
    try:
        return float(ov_fn(ctx.symbol, _ny_hour(a["exit_time"]), price=price))
    except (TypeError, ValueError):
        return 0.0


# ------------------------------------------------ 逐笔差异表

_EMPTY_ROW_COLS = [
    "match", "side", "entry_time_a", "entry_time_b", "entry_dt_ms",
    "exit_time_a", "exit_time_b", "exit_dt_ms", "entry_price_a",
    "entry_price_b", "entry_price_diff", "exit_price_a", "exit_price_b",
    "exit_price_diff", "net_a", "net_b", "net_diff", "net_spread_comp",
    "net_overshoot_comp", "net_rest", "net_tol", "exit_reason_a",
    "exit_reason_b", "reason_ok", "entry_first_tick_ms", "entry_gap_ms",
    "entry_boundary_gap_ms", "entry_silent", "entry_time_ok", "entry_note",
    "exit_check", "exit_first_tick_ms", "exit_gap_ms", "exit_boundary_gap_ms",
    "exit_silent", "exit_time_ok", "exit_note", "net_ok", "row_ok",
]


def build_diff_rows(trades_a, trades_b, pairs, *, tf_seconds: int,
                    ctx: MechanismContext | None = None,
                    tol_fill_ms: int = TOL_FILL_TIME_MS,
                    silent_ms: int = SILENT_BOUNDARY_MS,
                    net_rest_tol_spreads: float = NET_REST_TOL_SPREADS
                    ) -> pd.DataFrame:
    """按配对结果构建逐笔差异表：结构差异 + 机制核对（ctx 给出时）+ 净利
    三分量拆解。unmatched 行（only_a/only_b）直接违规；无 ctx 时时间类
    核对退化为"未核对即通过"（供配对/结构单测；reconcile() 恒带 ctx）。"""
    rows = []
    for i, j in pairs:
        if i is None:
            rows.append(_row_one_side(trades_b.iloc[j], "only_b"))
            continue
        if j is None:
            rows.append(_row_one_side(trades_a.iloc[i], "only_a"))
            continue
        a, b = trades_a.iloc[i], trades_b.iloc[j]
        entry_dt = _ms(b["entry_time"]) - _ms(a["entry_time"])
        exit_dt = _ms(b["exit_time"]) - _ms(a["exit_time"])
        reason_a = str(a["exit_reason"])
        reason_b = str(b["exit_reason"])
        reason_ok = (_REASON_EQUIV.get(reason_a, reason_a)
                     == _REASON_EQUIV.get(reason_b, reason_b))
        reason_key = _REASON_EQUIV.get(reason_a, reason_a)

        # ---- 机制核对：入场 ----
        entry_boundary = _ms(a["entry_time"])      # A=bar 开盘标签=bar 边界
        entry_ready = entry_boundary + (ctx.latency_ms if ctx else 0)
        if ctx is not None:
            chk = _check_fill_at_first_tick(
                ctx.probe, entry_boundary, entry_ready, _ms(b["entry_time"]),
                tol_fill_ms=tol_fill_ms, silent_ms=silent_ms)
            entry_cols = {
                "entry_first_tick_ms": chk["first_tick_ms"],
                "entry_gap_ms": chk["gap_ms"],
                "entry_boundary_gap_ms": chk["boundary_gap_ms"],
                "entry_silent": bool(chk["silent"]),
                "entry_time_ok": bool(chk["ok"]),
                "entry_note": chk["note"],
            }
        else:
            entry_cols = {"entry_first_tick_ms": None, "entry_gap_ms": None,
                          "entry_boundary_gap_ms": None, "entry_silent": False,
                          "entry_time_ok": True, "entry_note": "no_tick_ctx"}

        # ---- 机制核对：出场（按出场类型）----
        if ctx is None:
            exit_cols = {"exit_check": "", "exit_first_tick_ms": None,
                         "exit_gap_ms": None, "exit_boundary_gap_ms": None,
                         "exit_silent": False, "exit_time_ok": True,
                         "exit_note": "no_tick_ctx"}
        elif reason_key == "signal":
            chk = _check_fill_at_first_tick(
                ctx.probe, _ms(a["exit_time"]),
                _ms(a["exit_time"]) + ctx.latency_ms, _ms(b["exit_time"]),
                tol_fill_ms=tol_fill_ms, silent_ms=silent_ms)
            exit_cols = {"exit_check": "first_tick",
                         "exit_first_tick_ms": chk["first_tick_ms"],
                         "exit_gap_ms": chk["gap_ms"],
                         "exit_boundary_gap_ms": chk["boundary_gap_ms"],
                         "exit_silent": bool(chk["silent"]),
                         "exit_time_ok": bool(chk["ok"]),
                         "exit_note": chk["note"]}
        elif reason_key in ("stop", "take_profit"):
            chk = _check_protective_exit(ctx.probe, ctx, b)
            exit_cols = {"exit_check": "first_cross",
                         "exit_first_tick_ms": None, "exit_gap_ms": None,
                         "exit_boundary_gap_ms": None, "exit_silent": False,
                         "exit_time_ok": bool(chk["ok"]),
                         "exit_note": chk["note"]}
        elif reason_key in _CLOSE_PRICE_REASONS:
            a_fill = a["exit_fill_time"]
            close_ms = (_ms(a_fill) if a_fill is not None
                        and not pd.isna(a_fill)
                        else _ms(a["exit_time"]) + tf_seconds * 1000)
            chk = _check_close_exit(ctx.probe, _ms(a["exit_time"]), close_ms,
                                    _ms(b["exit_time"]))
            exit_cols = {"exit_check": "last_tick",
                         "exit_first_tick_ms": None, "exit_gap_ms": None,
                         "exit_boundary_gap_ms": None, "exit_silent": False,
                         "exit_time_ok": bool(chk["ok"]),
                         "exit_note": chk["note"]}
        else:   # 引擎不会产出的原因：如实记违规，不冒充任何机制
            exit_cols = {"exit_check": "", "exit_first_tick_ms": None,
                         "exit_gap_ms": None, "exit_boundary_gap_ms": None,
                         "exit_silent": False, "exit_time_ok": False,
                         "exit_note": "reason_unknown"}

        # ---- 净利差三分量拆解 ----
        dec = _decompose_net(a, b, reason_key, reason_a, ctx=ctx,
                             net_rest_tol_spreads=net_rest_tol_spreads)
        row = {
            "match": "paired", "side": str(a["side"]),
            "entry_time_a": a["entry_time"], "entry_time_b": b["entry_time"],
            "entry_dt_ms": entry_dt,
            "exit_time_a": a["exit_time"], "exit_time_b": b["exit_time"],
            "exit_dt_ms": exit_dt,
            "entry_price_a": _f(a, "entry_price"),
            "entry_price_b": _f(b, "entry_price"),
            "entry_price_diff": _f(b, "entry_price") - _f(a, "entry_price"),
            "exit_price_a": _f(a, "exit_price"),
            "exit_price_b": _f(b, "exit_price"),
            "exit_price_diff": _f(b, "exit_price") - _f(a, "exit_price"),
            "net_a": _f(a, "net"), "net_b": _f(b, "net"),
            "net_diff": dec["net_diff"],
            "net_spread_comp": dec["spread_comp"],
            "net_overshoot_comp": dec["overshoot_comp"],
            "net_rest": dec["rest"], "net_tol": dec["tol"],
            "exit_reason_a": reason_a, "exit_reason_b": reason_b,
            "reason_ok": reason_ok, **entry_cols, **exit_cols,
            "net_ok": dec["ok"],
        }
        row["row_ok"] = bool(row["entry_time_ok"] and row["exit_time_ok"]
                             and reason_ok and row["net_ok"])
        rows.append(row)
    cols = list(rows[0].keys()) if rows else _EMPTY_ROW_COLS
    return pd.DataFrame(rows, columns=cols)


def _row_one_side(t, match: str) -> dict:
    """unmatched 行：该侧字段如实填，另一侧留空，row_ok=False。"""
    def _pick(col):
        v = t[col] if col in t.index else None
        return v
    return {
        "match": match, "side": str(t["side"]),
        "entry_time_a": _pick("entry_time") if match == "only_a" else pd.NaT,
        "entry_time_b": _pick("entry_time") if match == "only_b" else pd.NaT,
        "entry_dt_ms": None,
        "exit_time_a": _pick("exit_time") if match == "only_a" else pd.NaT,
        "exit_time_b": _pick("exit_time") if match == "only_b" else pd.NaT,
        "exit_dt_ms": None,
        "entry_price_a": _f(t, "entry_price") if match == "only_a" else None,
        "entry_price_b": _f(t, "entry_price") if match == "only_b" else None,
        "entry_price_diff": None,
        "exit_price_a": _f(t, "exit_price") if match == "only_a" else None,
        "exit_price_b": _f(t, "exit_price") if match == "only_b" else None,
        "exit_price_diff": None,
        "net_a": _f(t, "net") if match == "only_a" else None,
        "net_b": _f(t, "net") if match == "only_b" else None,
        "net_diff": None, "net_spread_comp": None,
        "net_overshoot_comp": None, "net_rest": None, "net_tol": None,
        "exit_reason_a": str(t["exit_reason"]) if match == "only_a" else None,
        "exit_reason_b": str(t["exit_reason"]) if match == "only_b" else None,
        "reason_ok": False,
        "entry_first_tick_ms": None, "entry_gap_ms": None,
        "entry_boundary_gap_ms": None, "entry_silent": False,
        "entry_time_ok": False, "entry_note": "unmatched",
        "exit_check": "", "exit_first_tick_ms": None, "exit_gap_ms": None,
        "exit_boundary_gap_ms": None, "exit_silent": False,
        "exit_time_ok": False, "exit_note": "unmatched",
        "net_ok": False, "row_ok": False,
    }


# ------------------------------------------------ 汇总

def summarize_net_diffs(rows: pd.DataFrame) -> dict:
    """差异表汇总：净利差与其三分量分布、静默边界计数、各类违规计数。"""
    paired = rows[rows["match"] == "paired"] if len(rows) else rows
    diffs = pd.to_numeric(paired["net_diff"], errors="coerce").dropna() \
        if "net_diff" in paired.columns else pd.Series(dtype="float64")
    if len(diffs):
        net_stats = {
            "count": int(len(diffs)),
            "mean": float(diffs.mean()),
            "median": float(diffs.median()),
            "min": float(diffs.min()),
            "max": float(diffs.max()),
            "mean_abs": float(diffs.abs().mean()),
            "p95_abs": float(diffs.abs().quantile(0.95)),
            "max_abs": float(diffs.abs().max()),
            "within_tol": int((paired["net_ok"].astype(bool)).sum()),
        }
    else:
        net_stats = {"count": 0, "mean": None, "median": None, "min": None,
                     "max": None, "mean_abs": None, "p95_abs": None,
                     "max_abs": None, "within_tol": 0}
    # 三分量（净利差 = 点差 + 越幅 + 其余）
    comp_stats = {}
    for key, col in (("spread", "net_spread_comp"),
                     ("overshoot", "net_overshoot_comp"),
                     ("rest", "net_rest")):
        if col in paired.columns:
            v = pd.to_numeric(paired[col], errors="coerce").dropna()
            comp_stats[key] = {
                "sum": float(v.sum()) if len(v) else 0.0,
                "mean": float(v.mean()) if len(v) else None,
                "abs_p95": float(v.abs().quantile(0.95)) if len(v) else None,
                "abs_max": float(v.abs().max()) if len(v) else None,
            }
        else:
            comp_stats[key] = {"sum": 0.0, "mean": None, "abs_p95": None,
                               "abs_max": None}
    n_only_a = int((rows["match"] == "only_a").sum()) if len(rows) else 0
    n_only_b = int((rows["match"] == "only_b").sum()) if len(rows) else 0
    # 违规只统计已配对行——unmatched 行的 *_ok 恒 False，单列一档避免重复计数
    sub = paired

    def _bad(col: str) -> int:
        if len(sub) == 0 or col not in sub.columns:
            return 0
        return int((~sub[col].astype(bool)).sum())

    def _flag(col: str) -> int:
        if len(sub) == 0 or col not in sub.columns:
            return 0
        return int(sub[col].astype(bool).sum())

    viol = {
        "entry_time": _bad("entry_time_ok"),
        "exit_time": _bad("exit_time_ok"),
        "reason": _bad("reason_ok"),
        "net": _bad("net_ok"),
        "unmatched": n_only_a + n_only_b,
    }
    viol["total"] = int(viol["entry_time"] + viol["exit_time"] + viol["reason"]
                        + viol["net"] + viol["unmatched"])
    return {
        "n_trades_a": int(rows["match"].isin(["paired", "only_a"]).sum()) if len(rows) else 0,
        "n_trades_b": int(rows["match"].isin(["paired", "only_b"]).sum()) if len(rows) else 0,
        "n_paired": int((rows["match"] == "paired").sum()) if len(rows) else 0,
        "n_only_a": n_only_a, "n_only_b": n_only_b,
        "net_diff": net_stats, "net_components": comp_stats,
        "n_silent_entry": _flag("entry_silent"),
        "n_silent_exit": (_flag("exit_silent")),
        "violations": viol,
        "ok": viol["total"] == 0,
    }


# ------------------------------------------------ 主对账入口

@dataclass
class ReconcileReport:
    ok: bool
    rows: pd.DataFrame
    summary: dict
    trades_a: pd.DataFrame
    trades_b: pd.DataFrame


def reconcile(strategy, bars: pd.DataFrame, ticks_loader, costs, *,
              symbol: str, params: dict, timeframe: str,
              contract_size: float, volume_lots: float = 1.0,
              initial_capital: float = 10_000.0,
              latency_ms: int = 0, slippage_price: float = 0.0,
              tol_fill_ms: int = TOL_FILL_TIME_MS,
              silent_boundary_ms: int = SILENT_BOUNDARY_MS,
              net_rest_tol_spreads: float = NET_REST_TOL_SPREADS
              ) -> ReconcileReport:
    """同一策略同一数据跑 A/B 两层并按机制对账；语义见模块 docstring。"""
    tf_seconds = TIMEFRAME_SECONDS[timeframe.upper()]
    common = dict(symbol=symbol, params=dict(params),
                  contract_size=contract_size, volume_lots=volume_lots,
                  initial_capital=initial_capital, timeframe=timeframe.upper())
    res_a = run_backtest(strategy, bars, costs, **common)
    res_b = run_backtest_ticks(strategy, bars, ticks_loader, costs,
                               latency_ms=latency_ms,
                               slippage_price=slippage_price, **common)
    ctx = MechanismContext(
        probe=TickProbe(ticks_loader),
        rules=getattr(strategy, "exit_rules", None) or ExitRules(),
        atr=atr_series(bars).to_numpy(),
        bar_open_ms=[_ms(t) for t in bars["time"]],
        tf_seconds=tf_seconds, latency_ms=latency_ms,
        slippage_price=slippage_price, costs=costs, symbol=symbol,
        contract_size=contract_size)
    pairs = align_trades(res_a.trades, res_b.trades,
                         max_entry_skew_ms=tf_seconds * 1000)
    rows = build_diff_rows(
        res_a.trades, res_b.trades, pairs, tf_seconds=tf_seconds, ctx=ctx,
        tol_fill_ms=tol_fill_ms, silent_ms=silent_boundary_ms,
        net_rest_tol_spreads=net_rest_tol_spreads)
    summary = summarize_net_diffs(rows)
    summary["engine_a_version"] = ENGINE_VERSION
    summary["engine_b_version"] = ENGINE_VERSION_B
    summary["tolerances"] = {
        "fill_time_ms": int(tol_fill_ms),
        "silent_boundary_ms": int(silent_boundary_ms),
        "net_rest_tol_spreads": float(net_rest_tol_spreads),
        "close_exit": "exact: last tick before bar close, inside exit bar",
        "protective_exit": ("exact: first crossing tick on trigger side "
                            "(long Bid / short Ask)"),
    }
    summary["ok"] = bool(summary["violations"]["total"] == 0)
    return ReconcileReport(ok=summary["ok"], rows=rows, summary=summary,
                           trades_a=res_a.trades, trades_b=res_b.trades)


# ------------------------------------------------ tick 覆盖期窗口

def bars_in_tick_window(bars: pd.DataFrame, *, first_msc: int, last_msc: int,
                        tf_seconds: int, start=None, end=None) -> pd.DataFrame:
    """把 bars 裁到对账窗口 = tick 覆盖期 ∩ [start, end]（naive UTC）。

    保留收盘时刻落在窗口内的 bar（close_ms ∈ [from_ms, to_ms+1ms]，+1ms 容纳
    "收盘前最后一笔 tick"在 close−1ms 的边界）；首根 bar 的盘中段可以部分
    早于窗口起点——引擎只从它的收盘时刻起消费 tick，且两层喂的是同一份
    bars，不影响对账。不足 2 根 → ValueError（跑不出任何往返）。
    """
    from_ms = int(first_msc) if start is None else max(int(first_msc), _ms(start))
    to_ms = int(last_msc) if end is None else min(int(last_msc), _ms(end))
    if from_ms > to_ms:
        raise ValueError(
            f"对账窗口为空：tick 覆盖 [{pd.Timestamp(first_msc, unit='ms')} … "
            f"{pd.Timestamp(last_msc, unit='ms')}] 与 --start/--end 无交集")
    close_ms = _series_ms(bars["time"]) + tf_seconds * 1000
    keep = (close_ms >= from_ms) & (close_ms <= to_ms + 1)
    out = bars[keep].reset_index(drop=True)
    if len(out) < 2:
        raise ValueError(
            f"对账窗口内 bars 不足（{len(out)} 根）：tick 覆盖 "
            f"{pd.Timestamp(from_ms, unit='ms')} … {pd.Timestamp(to_ms, unit='ms')}，"
            "请放宽 --start/--end 或回补 tick（scripts/download_ticks.py）")
    return out


# ------------------------------------------------ 输出

def _atomic_write_text(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    atomic_replace(tmp, path)


def write_outputs(out_dir: str, report: ReconcileReport, *,
                  meta: dict | None = None) -> dict:
    """写 diff.csv / trades_a.csv / trades_b.csv / summary.json（原子写）。"""
    os.makedirs(out_dir, exist_ok=True)
    summary = dict(report.summary)
    if meta is not None:
        summary["meta"] = meta
    paths = {
        "diff": os.path.join(out_dir, "diff.csv"),
        "trades_a": os.path.join(out_dir, "trades_a.csv"),
        "trades_b": os.path.join(out_dir, "trades_b.csv"),
        "summary": os.path.join(out_dir, "summary.json"),
    }
    report.rows.to_csv(paths["diff"], index=False)
    report.trades_a.to_csv(paths["trades_a"], index=False)
    report.trades_b.to_csv(paths["trades_b"], index=False)
    _atomic_write_text(paths["summary"], json.dumps(summary, ensure_ascii=False,
                                                    indent=2, default=str))
    return paths


def _print_report(report: ReconcileReport, paths: dict | None) -> None:
    s = report.summary
    nd = s["net_diff"]
    print(f"A={s['engine_a_version']}  B={s['engine_b_version']}  "
          f"配对 {s['n_paired']} / A 独有 {s['n_only_a']} / B 独有 {s['n_only_b']}  "
          f"静默边界 入场 {s.get('n_silent_entry', 0)} / "
          f"signal 出场 {s.get('n_silent_exit', 0)}（单列，不判违规）")
    if nd["count"]:
        comp = s.get("net_components", {})
        print(f"每笔净利差（B−A）：mean={nd['mean']:+.4f} median={nd['median']:+.4f} "
              f"min={nd['min']:+.4f} max={nd['max']:+.4f} "
              f"|diff| p95={nd['p95_abs']:.4f} 容差内 {nd['within_tol']}/{nd['count']}")
        print(f"三分量合计：点差 {comp.get('spread', {}).get('sum', 0):+.4f} / "
              f"止损越幅 {comp.get('overshoot', {}).get('sum', 0):+.4f} / "
              f"其余 {comp.get('rest', {}).get('sum', 0):+.4f}"
              f"（|其余| p95={comp.get('rest', {}).get('abs_p95') or 0:.4f}）")
    print(f"违规：{s['violations']}")
    show = report.rows.head(MAX_PRINT_ROWS)
    if len(report.rows) > MAX_PRINT_ROWS:
        print(f"（仅显示前 {MAX_PRINT_ROWS} 行，全量见 diff.csv）")
    cols = ["match", "side", "entry_gap_ms", "exit_check", "exit_note",
            "entry_price_diff", "exit_price_diff", "net_diff",
            "net_spread_comp", "net_overshoot_comp", "net_rest", "net_tol",
            "exit_reason_a", "exit_reason_b", "row_ok"]
    with pd.option_context("display.width", 240):
        print(show[cols].to_string(index=False))
    if paths:
        print(f"差异表：{paths['diff']}")
        print(f"汇总：{paths['summary']}")


# ------------------------------------------------ 锁箱闸门（T27/H3）

OPS_CARD_ID = "OPS-RECONCILE-AB"      # 台账登记用的"卡位"（非研究卡）


def window_touches_lockbox(to_ms: int, cutoff_ms: int, tf_seconds: int) -> bool:
    """对账窗是否可能包含锁箱行（row time ≥ cutoff）。

    保留的 bar 以收盘时刻落窗判定（bars_in_tick_window）：窗末端允许的
    最晚收盘 = to_ms + 1ms，其开盘 = to_ms + 1 − 周期。开盘 ≥ cutoff 的
    行属锁箱段——保守判定"可能包含"（真实 tick 库只覆盖最近数周，整段
    深锁箱期，此判定实际总是 True）。"""
    return to_ms + 1 >= cutoff_ms + tf_seconds * 1000


def _record_ops_run(ledger_path, *, args, symbol, tf, bars, cost_version,
                    summary) -> None:
    """--ops-ack 的审计登记：runs 表一行 actor=ops（T27/H3 收口）。"""
    from mt5quant.research.ledger import Ledger

    with Ledger(ledger_path) as led:
        led.record_run(
            card_id=OPS_CARD_ID, card_version=0, strategy=args.strategy,
            params=json.loads(args.params), symbol=symbol, timeframe=tf,
            data_first=pd.Timestamp(bars["time"].iloc[0]).isoformat(),
            data_last=pd.Timestamp(bars["time"].iloc[-1]).isoformat(),
            data_sha256=data_sha256(bars),
            engine_version=f"{ENGINE_VERSION}/{ENGINE_VERSION_B}",
            cost_model_version=cost_version, gates_hash="",
            metrics=summary, actor="ops")


# ------------------------------------------------ T36 越幅表挂载

def attach_overshoot(data_root: str, symbol: str, costs):
    """从点差缓存装载 T36 止损越幅表并挂进 Costs（A-1.6 stop 成交口径）。

    缓存 version 2 起带顶层 overshoot / overshoot_sha256 键（scripts/
    build_cost_model.py --overshoot 写入）；sha 不符 → 报错（缓存被改动），
    绝不静默跳过。缺表/品种不覆盖/成本对象无挂点 → 原样返回（状态写进
    meta，报告里注明"越幅分量按 0"）。
    """
    path = spread_model_cache_path(data_root)
    if not os.path.exists(path):
        return costs, "no-cache-file"
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if "overshoot" not in doc:
        return costs, "cache-v1(no-overshoot)"
    sha = doc.get("overshoot_sha256")
    if not sha or canonical_json_sha256(doc["overshoot"]) != sha:
        raise ValueError(
            f"点差缓存 {path} 的 overshoot_sha256 与越幅表内容不一致"
            f"（文件被改动或写坏）——请重建（scripts/build_cost_model.py "
            "--overshoot）")
    from mt5quant.costs.overshoot import StopOvershootModel
    om = StopOvershootModel.from_dict(doc["overshoot"])
    if symbol not in om.symbols:
        return costs, f"symbol-missing({symbol})"
    if not hasattr(costs, "stop_overshoot"):
        return costs, "costs-without-hook"
    try:
        return replace(costs, overshoot_model=om), "loaded"
    except TypeError:
        return costs, "not-a-dataclass"


# ------------------------------------------------ CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="A/B 双引擎逐笔对账（运维工具）")
    ap.add_argument("--strategy", required=True,
                    help="module:Class，如 mt5quant.backtest.reference:MaCrossATR")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--timeframe", required=True, help="M1/H1/D1 等")
    ap.add_argument("--params", default="{}", help="JSON 字符串")
    ap.add_argument("--start", default=None, help="对账窗口起点（naive UTC）")
    ap.add_argument("--end", default=None, help="对账窗口终点（naive UTC）")
    ap.add_argument("--data-root", default="data")
    ap.add_argument("--out", default="data/reports/reconciliation")
    ap.add_argument("--volume-lots", type=float, default=1.0)
    ap.add_argument("--initial-capital", type=float, default=10_000.0)
    ap.add_argument("--latency-ms", type=int, default=0)
    ap.add_argument("--slippage-price", type=float, default=0.0,
                    help="B 层每腿额外不利滑点（价格单位）；A 层用成本模型的滑点")
    ap.add_argument("--costs", choices=("auto", "zero"), default="auto")
    ap.add_argument("--spread-fraction", type=float, default=0.1)
    ap.add_argument("--spread-mode", choices=("relative", "absolute"),
                    default="relative")
    ap.add_argument("--commission", type=float, default=0.0)
    ap.add_argument("--allow-zero-swap", action="store_true")
    ap.add_argument("--contract-size", type=float, default=None)
    ap.add_argument("--quick", action="store_true",
                    help=f"单组快验：对账窗起点裁到 tick 覆盖期末端 "
                         f"{QUICK_WINDOW_DAYS} 天（与 --start 互斥时以 --start 为准）")
    ap.add_argument("--tol-fill-ms", type=int, default=TOL_FILL_TIME_MS,
                    help="入场/signal 出场：B 成交 vs 就绪后首笔 tick 的容差（ms）")
    ap.add_argument("--net-tol-spreads", type=float,
                    default=NET_REST_TOL_SPREADS,
                    help='净利差拆解后"其余"分量的容差（点差成本倍数）')
    ap.add_argument("--ops-ack", action="store_true",
                    help="运维确认锁箱豁免（对账窗触到锁箱期时必须显式给出，"
                         "否则拒绝运行；给出后本次运行在台账记一行 actor=ops）")
    ap.add_argument("--ledger", default=None,
                    help="台账路径；缺省 {data-root}/research/ledger.sqlite")
    args = ap.parse_args(argv)

    tf = args.timeframe.upper()
    if tf not in TIMEFRAME_SECONDS:
        print(f"✗ 未知周期：{args.timeframe}")
        return 1
    try:
        params = json.loads(args.params)
    except ValueError as e:
        print(f"✗ --params 不是合法 JSON：{e}")
        return 1
    try:
        strategy_cls = load_strategy(args.strategy)
        contract_size = resolve_contract_size(args.data_root, args.symbol,
                                              args.contract_size)
        check_clock(args.data_root)
        meta = read_ticks_watermark(args.data_root, args.symbol)
        first_msc = (meta or {}).get("first_msc")
        last_msc = (meta or {}).get("last_msc")
        if first_msc is None or last_msc is None:
            raise ValueError(
                f"{args.symbol} 无 tick 水位（data/ticks/{args.symbol}/meta.json），"
                "B 层无法回测；先跑 scripts/download_ticks.py")
        start = pd.Timestamp(args.start) if args.start else None
        end = pd.Timestamp(args.end) if args.end else None
        for label, ts in (("--start", start), ("--end", end)):
            if ts is not None and ts.tzinfo is not None:
                raise ValueError(f"{label} 只接受不带时区的 UTC 时间")
        if args.quick and start is None:
            start = pd.Timestamp(int(last_msc)
                                 - QUICK_WINDOW_DAYS * 86_400_000, unit="ms")
            print(f"— --quick：窗口起点裁到 {start}"
                  f"（tick 覆盖期末端 {QUICK_WINDOW_DAYS} 天）")
        # T27/H3：对账窗触到锁箱期 → 读 bars 之前先过 --ops-ack 闸门
        from_ms = int(first_msc) if start is None \
            else max(int(first_msc), _ms(start))
        to_ms = int(last_msc) if end is None else min(int(last_msc), _ms(end))
        tf_seconds = TIMEFRAME_SECONDS[tf]
        cutoff = cutoff_for(load_config(args.data_root), args.symbol, tf)
        ops_ack_recorded = False
        if cutoff is not None and window_touches_lockbox(
                to_ms, _ms(pd.Timestamp(cutoff)), tf_seconds):
            if not args.ops_ack:
                print(
                    f"✗ 对账窗 [{pd.Timestamp(from_ms, unit='ms')} … "
                    f"{pd.Timestamp(to_ms, unit='ms')}] 触到锁箱段"
                    f"（cutoff {pd.Timestamp(cutoff)}）——本脚本直读主库全量"
                    " bars（含锁箱期行），读锁箱期数据必须显式 --ops-ack："
                    "运维确认本次只做执行口径核对、不产生研究结论；"
                    "加 --ops-ack 会在台账记一行 actor=ops 供审计（T27/H3）")
                return 1
            ops_ack_recorded = True
        # 锁箱豁免（已 ack）：直读全量 bars（理由见模块 docstring）
        bars_all = load_bars(args.data_root, args.symbol, tf)
        if bars_all is None or len(bars_all) == 0:
            raise ValueError(
                f"无 K 线数据：{args.symbol} {tf}（先跑 scripts/download_bars.py）")
        bars = bars_in_tick_window(bars_all, first_msc=first_msc,
                                   last_msc=last_msc,
                                   tf_seconds=tf_seconds,
                                   start=start, end=end)
        if args.costs == "auto":
            costs, cost_version = build_costs(
                args.data_root, args.symbol, args.spread_fraction,
                args.commission, allow_zero_swap=args.allow_zero_swap,
                spread_mode=args.spread_mode)
        else:
            costs, cost_version = ZeroCosts(), "zero-v0"
        # T40/T36：缓存带越幅表 → 挂进 Costs（A-1.6 stop 成交口径 + 拆解用）
        costs, overshoot_status = attach_overshoot(
            args.data_root, args.symbol, costs)
        if overshoot_status == "loaded":
            print("✓ 止损越幅表已装入（A-1.6 stop 成交口径；拆解越幅分量用同表）")
        else:
            print(f"⚠ 无止损越幅表（{overshoot_status}）：A 层 stop 不加模型越幅，"
                  "净利拆解的越幅分量按 0（scripts/build_cost_model.py --overshoot）")
        ticks_loader = lambda start_msc, end_msc: load_ticks(  # noqa: E731
            args.data_root, args.symbol, start_msc=start_msc, end_msc=end_msc)
        report = reconcile(
            strategy_cls(), bars, ticks_loader, costs, symbol=args.symbol,
            params=params, timeframe=tf, contract_size=contract_size,
            volume_lots=args.volume_lots, initial_capital=args.initial_capital,
            latency_ms=args.latency_ms, slippage_price=args.slippage_price,
            tol_fill_ms=args.tol_fill_ms,
            net_rest_tol_spreads=args.net_tol_spreads)
        run_dir = make_run_dir(args.out, args.symbol, tf)
        paths = write_outputs(run_dir, report, meta={
            "symbol": args.symbol, "timeframe": tf,
            "strategy": args.strategy, "params": params,
            "bars_first": str(bars["time"].iloc[0]),
            "bars_last": str(bars["time"].iloc[-1]),
            "start": args.start, "end": args.end,
            "quick": bool(args.quick),
            "latency_ms": args.latency_ms,
            "slippage_price": args.slippage_price,
            "cost_version": cost_version,
            "overshoot": overshoot_status,
            "tick_first_msc": first_msc, "tick_last_msc": last_msc,
            "ops_ack": ops_ack_recorded,       # T27：本次是否走了运维豁免
            "lockbox_cutoff": (pd.Timestamp(cutoff).isoformat()
                               if cutoff is not None else None),
        })
        if ops_ack_recorded:
            ledger_path = args.ledger if args.ledger else os.path.join(
                args.data_root, "research", "ledger.sqlite")
            _record_ops_run(ledger_path, args=args, symbol=args.symbol,
                            tf=tf, bars=bars, cost_version=cost_version,
                            summary=report.summary)
            print(f"— 运维豁免已登记（actor=ops）：{ledger_path}")
    except Exception as e:  # fail-fast：任何错误如实报出
        print(f"✗ {type(e).__name__}: {e}")
        return 1

    print(f"对账 {args.symbol} {tf} bars={len(bars)} "
          f"窗口 [{bars['time'].iloc[0]} … {bars['time'].iloc[-1]}]")
    _print_report(report, paths)
    print("✓ 全部差异在容差内" if report.ok else
          "✗ 存在超容差差异（见上表；逐项归因请汇总进 "
          "docs/reports/reconciliation-v3-<日期>.md）")
    return 0 if report.ok else 2


if __name__ == "__main__":
    sys.exit(main())
