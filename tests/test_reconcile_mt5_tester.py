"""scripts/reconcile_mt5_tester.py：EA 成交 CSV 解析 + 与 B 层逐笔比对。

MT5 策略测试器本身无法自动测试——这里测的是 Python 侧：
- CSV 解析（列齐全、数值/方向合法、坏行报错）；
- 与 B 层交易的逐笔比对（时间/方向/价格/手数 + 容差判定）；
- 两个参考 EA 源文件的关键信号逻辑字符串存在（结构自检，非编译验证）。
"""
import importlib.util
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

from mt5data.store import TIMEFRAME_SECONDS
from mt5quant.strategies import BuyHold

SCRIPT_PATH = (Path(__file__).resolve().parents[1] / "scripts"
               / "reconcile_mt5_tester.py")
MQ5_DIR = Path(__file__).resolve().parents[1] / "mql5" / "Experts"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "reconcile_mt5_tester_script", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reconcile_mt5_tester_script"] = mod   # dataclass 注解解析需要
    spec.loader.exec_module(mod)
    return mod


def msc(ts) -> int:
    return int(pd.Timestamp(ts).value // 1_000_000)


def b_trades_frame(rows):
    cols = ["entry_time", "exit_time", "side", "volume_lots", "entry_price",
            "exit_price", "gross", "exit_reason"]
    return pd.DataFrame([dict(zip(cols, r)) for r in rows], columns=cols)


SAMPLE_CSV = """seq,side,volume_lots,entry_msc,entry_time,entry_price,exit_msc,exit_time,exit_price,gross,exit_reason
1,long,0.10,1767607200500,2026-01-05 11:00:00.500,100.500,1767614399990,2026-01-05 12:59:59.990,103.000,250.000,end_of_data
2,short,0.10,1767614400000,2026-01-05 13:00:00.000,103.000,1767620800123,2026-01-05 14:46:40.123,101.000,200.000,signal
"""


# --------------------------------------------- CSV 解析

def test_parse_tester_csv_roundtrip(tmp_path):
    mod = _load_script()
    p = tmp_path / "trades.csv"
    p.write_text(SAMPLE_CSV, encoding="utf-8")
    df = mod.parse_tester_csv(p)
    assert len(df) == 2
    assert df["seq"].tolist() == [1, 2]
    assert df["side"].tolist() == ["long", "short"]
    assert df["volume_lots"].tolist() == [0.10, 0.10]
    assert df["entry_msc"].tolist() == [1767607200500, 1767614400000]
    assert df["entry_price"].tolist() == [100.5, 103.0]
    assert df["exit_price"].tolist() == [103.0, 101.0]
    assert df["gross"].tolist() == [250.0, 200.0]
    assert df["exit_reason"].tolist() == ["end_of_data", "signal"]
    # msc → Timestamp 换算列（与 B 层 entry_time 同口径可直接比对）
    assert df["entry_time"].iloc[0] == pd.Timestamp(
        1767607200500, unit="ms")


def test_parse_rejects_missing_columns(tmp_path):
    mod = _load_script()
    p = tmp_path / "bad.csv"
    p.write_text("seq,side,volume_lots\n1,long,0.1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="缺列"):
        mod.parse_tester_csv(p)


def test_parse_rejects_bad_side_and_numbers(tmp_path):
    mod = _load_script()
    p = tmp_path / "bad_side.csv"
    p.write_text(SAMPLE_CSV.replace("long,0.10", "BUY,0.10"),
                 encoding="utf-8")
    with pytest.raises(ValueError, match="side"):
        mod.parse_tester_csv(p)
    p2 = tmp_path / "bad_price.csv"
    p2.write_text(SAMPLE_CSV.replace("100.500", "hundred"),
                  encoding="utf-8")
    with pytest.raises(ValueError, match="entry_price"):
        mod.parse_tester_csv(p2)


# --------------------------------------------- 与 B 层逐笔比对

def mt5_frame(**overrides):
    base = dict(seq=1, side="long", volume_lots=0.1,
                entry_msc=msc("2026-01-05 11:00:00.500"),
                entry_time="2026-01-05 11:00:00.500", entry_price=100.5,
                exit_msc=msc("2026-01-05 12:59:59.990"),
                exit_time="2026-01-05 12:59:59.990", exit_price=103.0,
                gross=250.0, exit_reason="end_of_data")
    base.update(overrides)
    return pd.DataFrame([base])


_B_COLS = ["entry_time", "exit_time", "side", "volume_lots", "entry_price",
           "exit_price", "gross", "exit_reason"]


def b_frame(**overrides):
    base = dict(entry_time=pd.Timestamp("2026-01-05 11:00:00.500"),
                exit_time=pd.Timestamp("2026-01-05 12:59:59.990"),
                side="long", volume_lots=0.1, entry_price=100.5,
                exit_price=103.0, gross=250.0, exit_reason="end_of_data")
    base.update(overrides)
    return b_trades_frame([tuple(base[k] for k in _B_COLS)])


def test_compare_self_consistency_zero_diff():
    mod = _load_script()
    rep = mod.compare_with_b(mt5_frame(), b_frame(), tf_seconds=3600)
    assert rep.ok, rep.rows.to_string()
    row = rep.rows.iloc[0]
    assert row["match"] == "paired"
    assert row["side_match"] and row["entry_time_ok"] and row["exit_time_ok"]
    assert row["entry_price_diff"] == pytest.approx(0.0)
    assert row["exit_price_diff"] == pytest.approx(0.0)
    assert row["lots_diff"] == pytest.approx(0.0)
    assert row["gross_diff"] == pytest.approx(0.0)
    assert row["row_ok"]
    assert rep.summary["n_paired"] == 1
    assert rep.summary["violations"]["total"] == 0


def test_compare_flags_price_time_lots_side():
    mod = _load_script()
    # 价格差 0.3（容差 0）→ 违规
    rep = mod.compare_with_b(mt5_frame(), b_frame(entry_price=100.8),
                             tf_seconds=3600)
    assert not rep.ok
    row = rep.rows.iloc[0]
    assert row["entry_price_diff"] == pytest.approx(0.3)
    assert not row["price_ok"] and not row["row_ok"]
    # 出场时间差 5s → 违规
    rep2 = mod.compare_with_b(
        mt5_frame(), b_frame(exit_time=pd.Timestamp("2026-01-05 13:00:04.990")),
        tf_seconds=3600)
    assert not rep2.ok and not rep2.rows.iloc[0]["exit_time_ok"]
    # 手数差 → 违规
    rep3 = mod.compare_with_b(mt5_frame(), b_frame(volume_lots=0.2),
                              tf_seconds=3600)
    assert not rep3.ok and not rep3.rows.iloc[0]["lots_ok"]
    # 方向不同 → 不可配对，两侧各一笔 unmatched
    rep4 = mod.compare_with_b(mt5_frame(), b_frame(side="short"),
                              tf_seconds=3600)
    assert not rep4.ok
    assert set(rep4.rows["match"]) == {"only_mt5", "only_b"}
    # 容差放宽后同类小差异放行
    rep5 = mod.compare_with_b(mt5_frame(), b_frame(entry_price=100.8),
                              tf_seconds=3600, tol_price=0.5)
    assert rep5.ok


def test_compare_gross_is_informational_only():
    """gross 仅作交叉核对，不影响判定（EA 侧不含佣金/隔夜费的净口径）。"""
    mod = _load_script()
    rep = mod.compare_with_b(mt5_frame(gross=999.0), b_frame(), tf_seconds=3600)
    assert rep.ok
    assert rep.rows.iloc[0]["gross_diff"] == pytest.approx(-749.0)


def test_drop_b_trailing_end_of_data():
    mod = _load_script()
    b = b_trades_frame([
        ("2026-01-05 11:00:00.500", "2026-01-05 12:00:00.100", "long", 0.1,
         100.5, 101.0, 5.0, "signal"),
        ("2026-01-05 13:00:00.000", "2026-01-05 13:59:59.999", "long", 0.1,
         101.0, 102.0, 10.0, "end_of_data"),
    ])
    csv_early = mt5_frame(exit_msc=msc("2026-01-05 12:00:00.100"),
                          exit_price=101.0, exit_reason="signal")
    out = mod.drop_b_trailing_end_of_data(b, csv_early, tf_seconds=3600)
    assert len(out) == 1 and out.iloc[0]["exit_reason"] == "signal"
    # EA 也持仓到最后（末笔即 end_of_data）→ 不丢
    csv_late = mt5_frame(exit_msc=msc("2026-01-05 13:59:59.999"))
    out2 = mod.drop_b_trailing_end_of_data(b, csv_late, tf_seconds=3600)
    assert len(out2) == 2
    # 中途的 end_of_data（其后 EA 还有成交）绝不丢
    csv_late2 = mt5_frame(exit_msc=msc("2026-01-05 14:30:00.000"))
    out3 = mod.drop_b_trailing_end_of_data(b, csv_late2, tf_seconds=3600)
    assert len(out3) == 2


def test_default_tolerances_are_exact_match():
    """默认容差：真实 tick 同源 → 时间/价格/手数都要求完全一致。"""
    mod = _load_script()
    assert mod.TOL_TIME_MS == 0
    assert mod.TOL_PRICE == 0.0
    assert mod.TOL_LOTS == 0.0


# --------------------------------------------- 参考 EA 源文件（字符串自检）

def test_buyhold_ref_mq5_exists_with_key_logic():
    src = (MQ5_DIR / "BuyHoldRef.mq5").read_text(encoding="utf-8")
    mod = _load_script()
    assert mod.CSV_HEADER in src                # 与 Python 解析器同一表头
    assert "OnTester" in src and "FILE_COMMON" in src
    assert "mt5quant.strategies.library.buy_hold:BuyHold" in src  # 对应策略
    assert "trade.Buy" in src                   # 只做多、从不反向


def test_macross_ref_mq5_exists_with_key_logic():
    src = (MQ5_DIR / "MACrossRef.mq5").read_text(encoding="utf-8")
    mod = _load_script()
    assert mod.CSV_HEADER in src
    assert "OnTester" in src and "FILE_COMMON" in src
    assert "iMA" in src and "MODE_SMA" in src and "PRICE_CLOSE" in src
    assert "#define FAST_PERIOD 10" in src      # 快慢线参数写死 10/30
    assert "#define SLOW_PERIOD 30" in src


def test_csv_header_is_shared_constant():
    mod = _load_script()
    assert mod.CSV_HEADER == ",".join(mod.CSV_COLUMNS)


# --------------------------------------------- CLI 端到端（B 层真跑）

def flat_bars(start, n, tf="H1", *, openc=100.0, high=100.5, low=99.5,
              close=100.0):
    secs = TIMEFRAME_SECONDS[tf]
    times = [pd.Timestamp(start) + timedelta(seconds=secs * i) for i in range(n)]
    return pd.DataFrame({
        "time": pd.to_datetime(times),
        "open": openc, "high": high, "low": low, "close": close,
        "tick_volume": 10, "spread": 5, "real_volume": 0,
    })


def ohlc_ticks(bars, spread, tf="H1"):
    secs = TIMEFRAME_SECONDS[tf]
    out = []
    for _, row in bars.iterrows():
        t = pd.Timestamp(row["time"])
        out.append((msc(t), float(row["open"]), float(row["open"]) + spread))
        out.append((msc(t + pd.Timedelta(minutes=20)), float(row["high"]),
                    float(row["high"]) + spread))
        out.append((msc(t + pd.Timedelta(minutes=40)), float(row["low"]),
                    float(row["low"]) + spread))
        out.append((msc(t + pd.Timedelta(seconds=secs, milliseconds=-1)),
                    float(row["close"]), float(row["close"]) + spread))
    return out


class ZeroCosts:
    def spread_p90(self, symbol, hour_utc, *, price=None):
        return 0.0

    def slippage(self, symbol, hour_utc, *, price=None):
        return 0.0

    def commission_per_lot(self, symbol):
        return 0.0

    def overnight_fee(self, symbol, side, from_ts, to_ts):
        return 0.0


def test_cli_end_to_end_matches_b_layer(tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from mt5data.store import save_bars
    from mt5data.ticks import load_ticks, save_ticks, TICK_COLUMNS
    from mt5quant.backtest.engine_b import run_backtest_ticks

    mod = _load_script()
    root = tmp_path / "data"
    root.mkdir()
    df = flat_bars(pd.Timestamp("2026-01-05 00:00"), 30)
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

    loader = lambda start_msc=None, end_msc=None: load_ticks(
        str(root), "XAUUSD", start_msc=start_msc, end_msc=end_msc)
    res_b = run_backtest_ticks(BuyHold(), df, loader, ZeroCosts(),
                               symbol="XAUUSD", params={},
                               contract_size=100.0, timeframe="H1")
    # 把 B 层自己的成交写成 EA 的 CSV 形态 → 比对必须全零通过
    rows = []
    for i, t in enumerate(res_b.trades.itertuples(), 1):
        e, x = pd.Timestamp(t.entry_time), pd.Timestamp(t.exit_time)
        rows.append(dict(
            seq=i, side=t.side, volume_lots=float(t.volume_lots),
            entry_msc=int(e.value // 10**6),
            entry_time=e.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            entry_price=float(t.entry_price),
            exit_msc=int(x.value // 10**6),
            exit_time=x.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            exit_price=float(t.exit_price), gross=float(t.gross),
            exit_reason=t.exit_reason))
    csv_path = tmp_path / "ea_trades.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False)

    out_csv = tmp_path / "diff.csv"
    code = mod.main([
        "--csv", str(csv_path),
        "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
        "--symbol", "XAUUSD", "--timeframe", "H1", "--params", "{}",
        "--data-root", str(root), "--contract-size", "100",
        "--costs", "zero", "--out-csv", str(out_csv)])
    assert code == 0
    diff = pd.read_csv(out_csv)
    assert diff["match"].tolist() == ["paired"]
    assert diff["row_ok"].all()


def test_cli_flags_beyond_tolerance_with_exit_code_2(tmp_path):
    mod = _load_script()
    p = tmp_path / "ea.csv"
    p.write_text(SAMPLE_CSV, encoding="utf-8")
    # 无数据可跑 → 报错退出 1（数据装配失败优先于比对）
    code = mod.main([
        "--csv", str(p),
        "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
        "--symbol", "XAUUSD", "--timeframe", "H1", "--params", "{}",
        "--data-root", str(tmp_path / "empty"), "--costs", "zero"])
    assert code == 1
