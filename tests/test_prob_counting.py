"""T76 触发计数：只数触发不看结果；静态证明不 import 标签模块。"""
import inspect
from pathlib import Path

import pandas as pd
import pytest

from mt5quant.backtest.engine_a import atr_series
from mt5quant.prob import counting
from mt5quant.strategies.library.prob.actions import ActionParams
from mt5quant.strategies.library.prob.situations import Approach

APPROACH = Approach(lookback_bars=6, min_distance_atr=5.0, touch_atr=0.3)
PARAMS = ActionParams(breakout_atr=2.0, retest_window_bars=8)
PLAT = (1989.5, 1990.0, 1989.0, 1989.5)


def make_bars(rows, *, start="2026-03-02", tf="H1"):
    from mt5data.store import _COLUMNS, TIMEFRAME_SECONDS
    secs = TIMEFRAME_SECONDS[tf]
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df.insert(0, "time",
              pd.date_range(start, periods=len(rows), freq=f"{secs}s"))
    df["tick_volume"], df["spread"], df["real_volume"] = 10, 5, 0
    return df[_COLUMNS]


def s1_bars():
    rows = [PLAT] * 26
    rows.append((1992.0, 1999.9, 1991.0, 1997.0))          # anchor
    rows.append((1997.0, 1998.0, 1996.0, 1997.5))
    rows.append((1998.0, 2005.5, 1998.0, 2005.0))          # A1
    rows += [PLAT] * 4
    return make_bars(rows)


def s1_bars_gap_after(hole_after, jump_days=31):
    """同 s1_bars 形态，但 index hole_after→hole_after+1 的时间轴跳
    jump_days 天（>30 天 → gap）。"""
    from mt5data.store import _COLUMNS
    rows = s1_bars().drop(columns=["time"])
    times = [x if i <= hole_after else x + pd.Timedelta(days=jump_days)
             for i, x in enumerate(
                 pd.date_range("2026-03-02", periods=len(rows), freq="1h"))]
    df = rows.copy()
    df.insert(0, "time", pd.DatetimeIndex(pd.to_datetime(times)))
    return df[_COLUMNS]


def _count(bars, **over):
    kw = dict(symbol="XAUUSD", situation="S1", approach=APPROACH,
              action_params=PARAMS, grid_step=50.0)
    kw.update(over)
    return counting.count_triggers(bars, **kw)


def test_count_output_has_no_outcome_columns():
    bars = s1_bars()
    out = counting.count_triggers(bars, symbol="XAUUSD", situation="S1",
                                  approach=APPROACH, action_params=PARAMS,
                                  grid_step=50.0)
    assert len(out) >= 1
    forbidden = {"outcome", "net_r", "exit_reason", "entry_time", "exit_time"}
    assert forbidden.isdisjoint(set(out.columns))
    assert {"time", "situation", "action", "side", "level", "kind",
            "ny_hour", "atr", "close", "symbol"} <= set(out.columns)
    # 计数值与动作库一致（A3@anchor、A1@28）
    got = sorted(zip(out["action"], out["bar"]))
    assert ("A1", 28) in got and ("A3", 26) in got


def test_count_module_does_not_import_labels():
    """静态检查（AST）：计数模块的 import 语句不引入 prob.labels /
    label_triggers（不泄漏结局信息的结构保证；docstring 提及不算）。"""
    import ast
    src = Path(inspect.getfile(counting)).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "prob.labels" not in alias.name
                assert "label_triggers" not in alias.name
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert "prob.labels" not in mod
            for alias in node.names:
                assert alias.name != "label_triggers"
    assert not hasattr(counting, "label_triggers")


def test_count_ny_hour_matches_time():
    bars = s1_bars()
    out = counting.count_triggers(bars, symbol="XAUUSD", situation="S1",
                                  approach=APPROACH, action_params=PARAMS,
                                  grid_step=50.0)
    for r in out.itertuples(index=False):
        expected = (pd.Timestamp(r.time).tz_localize("UTC")
                    .tz_convert("America/New_York").hour)
        assert int(r.ny_hour) == int(expected)


def test_count_s2_and_s3_dispatch():
    """S2/S3 经通用发现器计数（无需 grid_step）。"""
    rows = [PLAT] * 30
    bars = make_bars(rows)
    with pytest.raises(ValueError):
        counting.count_triggers(bars, symbol="XAUUSD", situation="S9",
                                approach=APPROACH, action_params=PARAMS)
    for sit in ("S2", "S3"):
        out = counting.count_triggers(bars, symbol="XAUUSD", situation=sit,
                                      approach=APPROACH, action_params=PARAMS)
        assert len(out) == 0        # 恒定价格无事件，空表也带 symbol 列


def test_triggers_before_filters_by_split():
    bars = s1_bars()
    out = counting.count_triggers(bars, symbol="XAUUSD", situation="S1",
                                  approach=APPROACH, action_params=PARAMS,
                                  grid_step=50.0)
    split = pd.Timestamp("2026-03-03")          # 序列从 03-02 起
    disc = counting.triggers_before(out, split)
    assert len(disc) < len(out)
    assert (pd.to_datetime(disc["time"]) < split).all()
    # 全在分界前 → 全保留
    assert len(counting.triggers_before(out, pd.Timestamp("2026-04-01"))) == len(out)


# ------------------------------------------------- P-09 共同可用集合（T98）

class TestCountTriggerEligibility:
    """count_triggers 经 gaps.eligibility_mask 过滤（可选三元组：预热/
    持有/洞宽；缺省不启用，行为不变）。"""

    def test_default_kwargs_keep_behavior(self):
        bars = s1_bars_gap_after(30)
        plain = _count(bars)
        assert {26, 28} <= set(plain["bar"].astype(int))
        # 只给 gap_spans（旧行为路径）：洞在触发后、回看窗不跨 → 全保留
        t = list(pd.to_datetime(bars["time"]))
        assert _count(bars, gap_spans=[(t[30], t[31])]).equals(plain)

    def test_feature_warmup_drops_early_triggers(self):
        bars = s1_bars()
        plain = _count(bars)
        plain_bars = set(plain["bar"].astype(int))
        assert {26, 28, 29} <= plain_bars            # A3@26、A1@28、A4@29
        warm = _count(bars, feature_warmup=27)
        assert set(warm["bar"].astype(int)) == plain_bars - {26}
        assert len(_count(bars, feature_warmup=30)) == 0

    def test_hold_window_crossing_gap_drops_trigger(self):
        """洞 (t30,t31)：hold_bars=3 → A3@26 的持有窗 [26,29] 干净保留、
        A1@28 的 [28,31] 跨洞剔除；hold_bars=5 → 两个都跨洞。"""
        bars = s1_bars_gap_after(30)
        t = list(pd.to_datetime(bars["time"]))
        kw = dict(gap_spans=[(t[30], t[31])], feature_warmup=0)
        assert set(_count(bars, hold_bars=3, **kw)["bar"].astype(int)) == {26}
        assert len(_count(bars, hold_bars=5, **kw)) == 0
