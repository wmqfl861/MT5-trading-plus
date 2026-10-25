"""scripts/reconcile_ab.py 的对齐与容差逻辑：合成 bars + ticks 手算对账。

结构性预期（T10 计划约定，真机实测前先钉进回归）：
- tick 复现 bar OHLC 且点差/滑点模型与 tick 一致 → 两层差异 ≈ 0；
- 点差模型 ≠ 实测点差 → 差异集中在非 Bid 腿价格，净利差 = −Δ点差×手数×
  合约大小，且 |净利差| ≤ 1 个点差的默认容差内（结构正确而非缺陷）；
- 信号类成交（入场与 signal 出场）时间必须完全一致；收盘价类出场
  （time_stop/week_close/end_of_data）相差不足一根 bar 属结构内差异。
"""
import importlib.util
import json
import sys
from datetime import timedelta
from pathlib import Path

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
    B 层只消费 commission_per_lot / overnight_fee。"""

    def __init__(self, half=S_MODEL, slip=SLIP_MODEL, commission=COMM,
                 overnight=ONIGHT):
        self.half, self.slip = half, slip
        self.commission, self.overnight = commission, overnight

    def spread_p90(self, symbol, hour_utc, *, price=None):
        return self.half

    def slippage(self, symbol, hour_utc, *, price=None):
        return self.slip

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


def trades_frame(rows):
    cols = ["entry_time", "exit_time", "side", "volume_lots", "entry_price",
            "exit_price", "gross", "net", "spread_cost", "exit_reason"]
    return pd.DataFrame([dict(zip(cols, r)) for r in rows], columns=cols)


# --------------------------------------------- 默认容差常量（计划的回归锚点）

def test_default_tolerances_match_plan():
    mod = _load_script()
    assert mod.TOL_ENTRY_TIME_MS == 2_000     # 信号类时间容差 2s（真机实测 1ms 到达抖动）
    assert mod.TOL_EXIT_TIME_SIGNAL_MS == 2_000
    assert mod.NET_TOL_SPREADS == 1.0          # 每笔净利差 ≤ 1 个点差


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
    assert row["entry_price_diff"] == pytest.approx(0.0, abs=1e-9)
    assert row["exit_price_diff"] == pytest.approx(0.0, abs=1e-9)
    assert row["net_diff"] == pytest.approx(0.0, abs=1e-9)
    # end_of_data 收盘价类出场：B 在收盘前最后一笔 tick，差 tf−1ms（< 1 bar）
    assert row["exit_dt_ms"] == pytest.approx(3_600_000 - 1)
    assert row["exit_time_ok"] and row["row_ok"]


def test_spread_model_mismatch_diff_structure():
    """tick 实际点差 0.7 vs A 层模型 0.5：差异结构正确且在默认容差内。"""
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
    assert row["net_tol"] == pytest.approx(0.7)            # 1 × max(两侧 spread_cost)
    assert row["net_ok"] and row["row_ok"]
    assert rep.ok
    stats = rep.summary["net_diff"]
    assert stats["count"] == 1
    assert stats["mean"] == pytest.approx(-0.2)
    assert stats["within_tol"] == 1


def test_entry_time_skew_beyond_tolerance_fails():
    """bar1 开盘 tick 缺位 5 分钟：B 入场时间晚 5 分钟 → 入场时间违规。"""
    mod = _load_script()
    df = base4()
    ticks = ohlc_ticks(df, S_MODEL)
    assert ticks[4][0] == msc("2026-01-05 11:00:00")       # bar1 开盘 tick
    ticks[4] = (msc("2026-01-05 11:05:00"), ticks[4][1], ticks[4][2])
    rep = mod.reconcile(ScriptStrategy([1], fill=1), df, make_loader(ticks),
                        FakeCosts(), symbol="XAUUSD", params={}, timeframe="H1",
                        contract_size=1.0, slippage_price=SLIP_MODEL)
    assert not rep.ok
    row = rep.rows.iloc[0]
    assert row["match"] == "paired"                        # 仍配对（1 bar 内）
    assert row["entry_dt_ms"] == 5 * 60 * 1000
    assert not row["entry_time_ok"]
    assert rep.summary["violations"]["entry_time"] == 1


def test_signal_exit_time_must_be_exact():
    """signal 出场时间超容差即违规：skew 1 根 bar（远超 2s）必须被抓。"""
    mod = _load_script()
    df = base4()
    ticks = ohlc_ticks(df, S_MODEL)
    # bar2 开盘 tick（=bar1 收盘后的第一笔，signal 反转成交点）推迟 3 分钟
    assert ticks[8][0] == msc("2026-01-05 12:00:00")
    ticks[8] = (msc("2026-01-05 12:03:00"), ticks[8][1], ticks[8][2])
    rep = mod.reconcile(ScriptStrategy([1, -1], fill=-1), df, make_loader(ticks),
                        FakeCosts(), symbol="XAUUSD", params={}, timeframe="H1",
                        contract_size=1.0, slippage_price=SLIP_MODEL)
    assert not rep.ok
    first = rep.rows.iloc[0]
    assert first["exit_reason_a"] == first["exit_reason_b"] == "signal"
    assert first["exit_dt_ms"] == 3 * 60 * 1000
    assert not first["exit_time_ok"]


# --------------------------------------------- align_trades：贪心对齐

def test_align_trades_pairs_and_flags_unmatched():
    mod = _load_script()
    ta = trades_frame([
        ("2026-01-05 10:00", "2026-01-05 11:00", "long", 1.0, 100.0, 101.0,
         1.0, 0.5, 0.5, "signal"),
        ("2026-01-05 12:00", "2026-01-05 13:00", "short", 1.0, 101.0, 100.0,
         -1.0, -0.5, 0.5, "signal"),
    ])
    tb = trades_frame([
        ("2026-01-05 10:05", "2026-01-05 11:02", "long", 1.0, 100.0, 101.0,
         1.0, 0.4, 0.5, "signal"),
        ("2026-01-05 15:00", "2026-01-05 16:00", "long", 1.0, 99.0, 100.0,
         1.0, 0.2, 0.5, "end_of_data"),
    ])
    pairs = mod.align_trades(ta, tb, max_entry_skew_ms=3_600_000)
    assert pairs == [(0, 0), (1, None), (None, 1)]


def test_align_trades_never_pairs_opposite_sides():
    mod = _load_script()
    ta = trades_frame([("2026-01-05 10:00", "2026-01-05 11:00", "long",
                        1.0, 100.0, 101.0, 1.0, 0.5, 0.5, "signal")])
    tb = trades_frame([("2026-01-05 10:00", "2026-01-05 11:00", "short",
                        1.0, 101.0, 100.0, -1.0, -0.5, 0.5, "signal")])
    assert mod.align_trades(ta, tb, max_entry_skew_ms=3_600_000) == [
        (0, None), (None, 0)]


def test_unmatched_trades_make_report_fail():
    mod = _load_script()
    rows = mod.build_diff_rows(
        trades_frame([("2026-01-05 10:00", "2026-01-05 11:00", "long", 1.0,
                       100.0, 101.0, 1.0, 0.5, 0.5, "signal")]),
        trades_frame([("2026-01-05 14:00", "2026-01-05 15:00", "long", 1.0,
                       100.0, 101.0, 1.0, 0.5, 0.5, "signal")]),
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
        trades_frame([("2026-01-05 10:00", "2026-01-05 11:00", "long", 1.0,
                       100.0, 99.0, -1.0, -0.5, 0.5, "stop_gap")]),
        trades_frame([("2026-01-05 10:00", "2026-01-05 11:00", "long", 1.0,
                       100.0, 99.0, -1.0, -0.5, 0.5, "stop")]),
        [(0, 0)], tf_seconds=3600)
    assert rows.iloc[0]["exit_reason_a"] == "stop_gap"
    assert rows.iloc[0]["exit_reason_b"] == "stop"
    assert rows.iloc[0]["reason_ok"]


# --------------------------------------------- 净利差分布汇总

def test_summarize_net_diffs_hand_values():
    mod = _load_script()
    rows = pd.DataFrame({
        "net_diff": [1.0, -2.0, 3.0, -4.0],
        "net_tol": [1.0, 1.0, 1.0, 1.0],
        "entry_time_ok": [True] * 4,
        "exit_time_ok": [True] * 4,
        "reason_ok": [True] * 4,
        "net_ok": [True, False, False, False],
        "row_ok": [True, False, False, False],
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

@pytest.fixture
def data_root(tmp_path):
    """30 根 H1 上涨 bars + OHLC 复现 ticks + 合约大小快照。"""
    from mt5data.store import save_bars
    from mt5data.ticks import save_ticks, TICK_COLUMNS

    root = tmp_path / "data"
    root.mkdir()
    df = flat_bars(pd.Timestamp("2026-01-05 00:00"), 30, close=100.0)
    for i in range(1, 30):
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
    specs.mkdir()
    (specs / "latest.json").write_text(
        json.dumps({"XAUUSD": {"contract_size": 100.0}}), encoding="utf-8")
    return root


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
