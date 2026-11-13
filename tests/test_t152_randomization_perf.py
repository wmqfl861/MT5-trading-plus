"""T152 随机化检验性能优化：新旧实现逐位等价（对拍）测试。

第十六轮定义（docs/handoff/2026-11-08-round16-plan.md §T152、
docs/reviews/2026-11-07-review-round16.md H16-5）：只许优化实现速度，
同种子下 p 值、零分布均值（及全部输出）必须**逐位相同**；draws 数、
随机化参考分布构造、p 值口径、种子派生不得变。本文件把该契约钉进 CI：

- 旧实现 = 分支基点 fdf842a（T152 施工前）的 events.py，经
  ``git show`` 原文装载（与 test_t112 的 pin 测试同一模式）；
- 冻结输入各两型——R3 形（fomc/day_close、ext+mt5 混合来源标签、
  相对点差+滑点+佣金+隔夜费的真实 Costs 聚合体、static_long 声明侧）
  与 R1 形（london_fix/bar_open、无来源标签列、swap 计费、隐式 long）；
- 断言：event_returns 逐事件 DataFrame 逐位相同（check_exact）、
  randomization_test 全字典逐位相同、逐 draw 零分布统计量逐位相同
  （含 NaN 位置）、mtd_sign 声明侧（anchor 记忆化改动面）在 anchor
  网格上输出/异常逐位相同；
- 另钉 randomize_event_times 源码与基点恒等（采样器本体零改动——
  它另有 b4eeddb 的既有 pin，见 test_t112_research_inputs）。
"""
from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from mt5quant.costs import Costs, SpreadModel, SpreadStat
from mt5quant.costs.commission import CommissionModel
from mt5quant.costs.slippage import SlippageModel
from mt5quant.costs.swap import SwapModel, SwapRule
from mt5quant.validation import events as ev
from mt5quant.validation.stats import rng_seed

REPO = Path(__file__).resolve().parents[1]
# T152 施工前基点（本分支 t152-randperf-wt 的分叉提交）
BASE = "fdf842a"


# ---------------------------------------------------------------- 旧实现装载

def load_old_events(tmp_path: Path):
    src = subprocess.run(
        ["git", "-C", str(REPO), "show",
         f"{BASE}:src/mt5quant/validation/events.py"],
        check=True, capture_output=True).stdout
    path = tmp_path / "events_pre_t152.py"
    path.write_bytes(src)
    spec = importlib.util.spec_from_file_location("events_pre_t152", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # dataclass 装饰器需要模块已注册
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- 冻结输入

def _stats(p90: float) -> SpreadStat:
    return SpreadStat(median=p90 / 2, p90=p90, p99=p90 * 2, n=5000, n_days=90)


def real_costs(symbol: str, *, hours=(17, 21), p90s=(0.00011, 0.00007),
               commission: float = 3.0,
               swap: SwapModel | None = None) -> Costs:
    """真实成本聚合体（相对点差 ×price、滑点按 p90、佣金、隔夜费）。"""
    spread = SpreadModel({symbol: {h: _stats(p) for h, p in zip(hours, p90s)}},
                         {symbol: min(p90s)}, mode="relative")
    return Costs(spread=spread,
                 slippage_model=SlippageModel(spread, spread_fraction=0.1),
                 commission=CommissionModel(default_per_lot=commission),
                 swap=swap if swap is not None else SwapModel(per_symbol={
                     symbol: SwapRule(long_per_day=-2.0, short_per_day=-1.0,
                                      triple_weekdays=frozenset({2}))}))


def r3_shaped_input():
    """R3 形冻结输入：D1 close-only 序列（ext 前段 + mt5 尾段来源标签）、
    1 日持有窗 day_close 事件、static_long 声明侧（与 C005 主检验同形）。"""
    days = pd.bdate_range("1986-01-02", "2021-05-14")
    rng = np.random.default_rng(20211105)
    closes = 400.0 * np.exp(np.cumsum(rng.normal(0, 0.011, len(days))))
    split = len(days) - 40
    bars = pd.DataFrame({
        "time": days,
        "open": closes,                       # close-only 段开=收（R3 等价锚）
        "close": closes,
        "_research_source": ["ext:fred:USTEC:D1:frozen"] * split
                            + ["mt5"] * (len(days) - split)})
    ann = days[np.arange(30, len(days) - 2, 43)]      # 冻结公告日网格
    events = [ev.EventWindow(
        kind="fomc_calendar", label=f"fomc_calendar:{d.date().isoformat()}",
        entry_time=days[np.searchsorted(days.to_numpy(), d.to_datetime64()) - 1],
        exit_time=d, price_timepoint="day_close") for d in ann]
    side_fn = ev.declared_event_side("static_long", signal_bars=bars,
                                     signal_timeframe="D1", threshold=0.0)
    return (bars, days, events, side_fn, real_costs("USTEC"),
            rng_seed("H102"), 1.0)


def r1_shaped_input():
    """R1 形冻结输入：H1 序列（无来源标签列）、london_fix 月末事件、
    隐式 long（非 strict 侧）、swap 计费（与 C002/H101 主检验同形）。"""
    times = pd.date_range("2021-05-17", "2021-11-30 23:00", freq="h")
    rng = np.random.default_rng(20210517)
    close = 1.20 * np.exp(np.cumsum(rng.normal(0, 0.0006, len(times))))
    bars = pd.DataFrame({"time": times, "open": close * 1.0001, "close": close})
    days = pd.DatetimeIndex(sorted(set(times.normalize())))
    anchors = ev.month_end_events(days, last_n=1, first_n=1)
    events = ev.london_fix_events(
        pd.DatetimeIndex([e.entry_time for e in anchors]))
    return (bars, days, events, (lambda event: "long"),
            real_costs("EURUSD", hours=(2, 15, 16),
                       p90s=(0.00009, 0.00013, 0.00010)),
            rng_seed("H101"), 100000.0)


def mtd_sign_fixture():
    """mtd_sign 声明侧夹具（T112 同形）：信号序列 + 显式日历 + 逐月方向。"""
    signal = pd.DataFrame({
        "time": pd.to_datetime(["2024-01-30 21:00", "2024-01-31 21:00",
                                "2024-02-28 21:00", "2024-02-29 21:00",
                                "2024-03-28 21:00"]),
        "close": [100., 200., 110., 9999., 90.]})
    cal = {"version": 1, "complete_through": "2024-03-31",
           "sessions": [{"day": str(d.date()),
                         "close_time": str(d + pd.Timedelta(hours=22))}
                        for d in pd.bdate_range("2024-01-01", "2024-03-31")]}
    return signal, cal


def _draws(evmod, bars, days, events, *, side_fn, costs, symbol,
           contract_size, sims, seed):
    """复刻 randomization_test 的 draw 环（同种子派生），返回逐 draw 均值。"""
    master = np.random.default_rng(int(seed))
    child_seeds = master.integers(0, 2 ** 63 - 1, size=int(sims))
    out = np.empty(int(sims), dtype="float64")
    for k, cs in enumerate(child_seeds):
        rand = evmod.randomize_event_times(events, days, seed=int(cs))
        frame = evmod.event_returns(bars, rand, side_fn=side_fn,
                                    costs=costs, symbol=symbol,
                                    contract_size=contract_size)
        out[k] = (float(pd.to_numeric(frame["net"]).mean())
                  if len(frame) else np.nan)
    return out


# ---------------------------------------------------------------- 对拍测试

@pytest.mark.parametrize("shape", ["r3", "r1"])
def test_t152_randomization_old_new_bitwise(tmp_path, shape):
    """T152 等价：新旧 randomization_test / event_returns / 逐 draw 零分布
    在冻结输入上逐位相同（round16 定义验收）。"""
    old = load_old_events(tmp_path)
    bars, days, events, side_fn, costs, seed, size = (
        r3_shaped_input() if shape == "r3" else r1_shaped_input())
    sims = 12

    fo = old.event_returns(bars, events, side_fn=side_fn, costs=costs,
                           symbol=costs.spread.symbols[0],
                           contract_size=size)
    fn = ev.event_returns(bars, events, side_fn=side_fn, costs=costs,
                          symbol=costs.spread.symbols[0], contract_size=size)
    pd.testing.assert_frame_equal(fo, fn, check_exact=True)
    assert fo.attrs["dropped"] == fn.attrs["dropped"]

    ro = old.randomization_test(bars, events, side_fn=side_fn, costs=costs,
                                symbol=costs.spread.symbols[0],
                                trading_days=days, sims=sims, seed=seed,
                                contract_size=size)
    rn = ev.randomization_test(bars, events, side_fn=side_fn, costs=costs,
                               symbol=costs.spread.symbols[0],
                               trading_days=days, sims=sims, seed=seed,
                               contract_size=size)
    assert ro == rn, f"{shape}: 新旧 randomization_test 输出不一致"
    assert rn["sims"] == sims and rn["seed"] == int(seed)
    assert rn["n_events"] == ro["n_events"]

    do = _draws(old, bars, days, events, side_fn=side_fn, costs=costs,
                symbol=costs.spread.symbols[0], contract_size=size,
                sims=sims, seed=seed)
    dn = _draws(ev, bars, days, events, side_fn=side_fn, costs=costs,
                symbol=costs.spread.symbols[0], contract_size=size,
                sims=sims, seed=seed)
    assert np.array_equal(do, dn, equal_nan=True), \
        f"{shape}: 逐 draw 零分布统计量不一致"


def test_t152_mtd_sign_anchor_cache_bitwise(tmp_path):
    """T152 等价（anchor 记忆化改动面）：新旧 DeclaredEventSide 在 anchor
    网格（可解 / 不可解 / 重复调用触发缓存）上输出与异常逐位相同。"""
    old = load_old_events(tmp_path)
    signal, cal = mtd_sign_fixture()
    side_new = ev.declared_event_side("mtd_sign", signal_bars=signal,
                                      signal_timeframe="H1", calendar=cal,
                                      threshold=0.0)
    side_old = old.declared_event_side("mtd_sign", signal_bars=signal,
                                       signal_timeframe="H1", calendar=cal,
                                       threshold=0.0)
    probes = ["2024-02-29 15:00", "2024-03-29 15:00", "2024-02-29 15:00",
              "2024-03-28 16:00", "2024-02-29 15:00", "2024-03-29 15:00",
              "2024-02-20 15:00", "2024-04-01 15:00"]
    for ts in probes:
        out_n = out_o = exc_n = exc_o = None
        window = ev.EventWindow("london_fix", "x", pd.Timestamp(ts),
                                pd.Timestamp(ts) + pd.Timedelta(hours=1))
        try:
            out_n = (side_new.anchor_indices(pd.Timestamp(ts)),
                     side_new(window))
        except Exception as e:                                   # noqa: BLE001
            exc_n = type(e).__name__
        try:
            out_o = (side_old.anchor_indices(pd.Timestamp(ts)),
                     side_old(window))
        except Exception as e:                                   # noqa: BLE001
            exc_o = type(e).__name__
        assert exc_n == exc_o, f"{ts}: 异常类型不一致 {exc_n} vs {exc_o}"
        assert out_n == out_o, f"{ts}: anchor/side 输出不一致"


def test_t152_randomize_event_times_source_unchanged():
    """T152 红线自钉：采样器 randomize_event_times 源码与基点 fdf842a
    恒等（draw 构造零改动；另有 b4eeddb pin 见 test_t112_research_inputs）。
    randomization_test 不在钉内——其逐 draw 循环正是 T152 的优化面
    （bars 预计算/隔夜费记忆化），等价性由上面的对拍测试承担。"""
    def func_src(text, name):
        tree = ast.parse(text)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return ast.get_source_segment(text, node)
        raise AssertionError(f"{name} not found")
    old_src = subprocess.run(
        ["git", "-C", str(REPO), "show",
         f"{BASE}:src/mt5quant/validation/events.py"],
        check=True, capture_output=True).stdout.decode("utf8")
    new_src = (REPO / "src" / "mt5quant" / "validation" / "events.py"
               ).read_text(encoding="utf8")
    assert func_src(old_src, "randomize_event_times") == \
        func_src(new_src, "randomize_event_times")


def test_t152_memos_are_pure_caches():
    """T152：_normalize_days/_day_strata 记忆化是纯缓存——同对象同结果、
    不同对象（等值）同结果、清除后仍同结果（值不因缓存而变）。"""
    days = pd.bdate_range("2020-01-02", periods=400)
    a1 = ev._normalize_days(days)
    a2 = ev._normalize_days(days)               # 缓存命中
    a3 = ev._normalize_days(pd.DatetimeIndex(days.to_numpy()))  # 等值新对象
    ev._DAYS_NORM_MEMO.clear()
    a4 = ev._normalize_days(days)
    assert a1.equals(a2) and a1.equals(a3) and a1.equals(a4)
    s1 = ev._day_strata(days)
    s2 = ev._day_strata(days)
    ev._DAYS_STRATA_MEMO.clear()
    s3 = ev._day_strata(days)
    for x, y in ((s1, s2), (s1, s3)):
        assert np.array_equal(x[0], y[0]) and np.array_equal(x[1], y[1])
