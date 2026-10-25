"""scripts/run_backtest.py 的金额口径装配：合约大小必须有来源，绝不静默按 1。"""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_backtest.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("run_backtest_script", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_contract_size_override_wins(tmp_path):
    mod = _load_script()
    assert mod.resolve_contract_size(str(tmp_path), "XAUUSD", 100.0) == 100.0


def test_contract_size_read_from_specs_snapshot(tmp_path):
    mod = _load_script()
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "latest.json").write_text(
        json.dumps({"EURUSD": {"contract_size": 100000.0}}), encoding="utf-8")
    assert mod.resolve_contract_size(str(tmp_path), "EURUSD", None) == 100000.0


def test_contract_size_missing_is_an_error_not_a_default(tmp_path):
    mod = _load_script()
    with pytest.raises(ValueError, match="snapshot_specs"):
        mod.resolve_contract_size(str(tmp_path), "XAUUSD", None)
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "latest.json").write_text(json.dumps({"BTCUSD": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="合约大小未知"):
        mod.resolve_contract_size(str(tmp_path), "BTCUSD", None)


def test_contract_size_override_must_be_positive(tmp_path):
    mod = _load_script()
    with pytest.raises(ValueError, match="contract-size"):
        mod.resolve_contract_size(str(tmp_path), "XAUUSD", 0.0)


# ------------------------------------------------- T20：sessions.toml 接线

def _write_ticks(root, symbol="XAUUSD"):
    """单小时 tick（2026-10-19 12:00 UTC = 纽约 8 点，EDT）。"""
    import numpy as np
    import pandas as pd
    from mt5data.ticks import save_ticks

    save_ticks(str(root), symbol, pd.DataFrame({
        "time_msc": np.asarray(
            [pd.Timestamp(f"2026-10-19 12:{m:02d}:00").value // 10**6
             for m in (0, 10, 20, 30, 40, 50)], dtype="int64"),
        "bid": [2400.0] * 6,
        "ask": [2400.3] * 6,
        "last": [2400.15] * 6,
        "volume": [0.0] * 6,
        "flags": [2] * 6,
    }))
    return root


def test_sessions_sha256_stable_and_recorded(tmp_path):
    mod = _load_script()
    p = tmp_path / "sessions.toml"
    p.write_bytes(b"version = 1\n")   # 字节写入：不随平台换行翻译漂移
    import hashlib
    sha = mod.sessions_sha256(str(p))
    assert sha == hashlib.sha256(b"version = 1\n").hexdigest()
    # 仓库默认配置存在且可读
    repo_sha = mod.sessions_sha256(str(mod.SESSIONS_PATH))
    assert len(repo_sha) == 64


def test_build_costs_annotates_sessions_sha_and_missing_hours(tmp_path):
    from mt5data.sessions import load_session_policy

    mod = _load_script()
    root = _write_ticks(tmp_path / "data")
    sha = mod.sessions_sha256(str(mod.SESSIONS_PATH))

    costs, ver = mod.build_costs(str(root), "XAUUSD", 0.1, allow_zero_swap=True,
                                 sessions_sha=sha)
    assert f"sess[{sha[:8]}]" in ver
    # tick 只覆盖纽约 8 点 → 23 个缺统计小时 → 标注进 cost_version（forbid 策略）
    assert "miss" in ver
    assert costs.spread.missing_hours("XAUUSD") == tuple(
        h for h in range(24) if h != 8)

    # 全小时覆盖（不缺）→ 无 miss 标注
    import numpy as np
    import pandas as pd
    from mt5data.ticks import save_ticks
    save_ticks(str(root), "XAUUSD", pd.DataFrame({
        "time_msc": np.asarray(
            [pd.Timestamp(f"2026-10-{19 + h // 24} {h % 24:02d}:00:00").value // 10**6
             for h in range(24)], dtype="int64"),
        "bid": [2400.0] * 24,
        "ask": [2400.3] * 24,
        "last": [2400.15] * 24,
        "volume": [0.0] * 24,
        "flags": [2] * 24,
    }))
    costs2, ver2 = mod.build_costs(str(root), "XAUUSD", 0.1, allow_zero_swap=True,
                                   sessions_sha=sha)
    assert "miss" not in ver2
    assert costs2.spread.missing_hours("XAUUSD") == ()


def test_entry_policy_for_combines_missing_hours(tmp_path):
    from mt5data.sessions import load_session_policy

    mod = _load_script()
    root = _write_ticks(tmp_path / "data")
    costs, _ = mod.build_costs(str(root), "XAUUSD", 0.1, allow_zero_swap=True)
    base = load_session_policy(str(mod.SESSIONS_PATH))
    pol = mod.entry_policy_for(costs, "XAUUSD", base)
    # 缺统计小时（纽约 7 点 = 11:00 UTC EDT）禁入；换日窗照常
    from datetime import datetime
    assert not pol.entry_allowed("XAUUSD", datetime(2026, 10, 19, 11, 0))
    assert pol.entry_allowed("XAUUSD", datetime(2026, 10, 19, 12, 0))
    assert not pol.entry_allowed("XAUUSD", datetime(2026, 10, 19, 21, 10))  # 换日窗
    # 原策略不被改动
    assert base.entry_allowed("XAUUSD", datetime(2026, 10, 19, 11, 0))


def test_entry_policy_for_zero_costs_returns_base(tmp_path):
    mod = _load_script()
    from mt5data.sessions import load_session_policy
    base = load_session_policy(str(mod.SESSIONS_PATH))
    assert mod.entry_policy_for(mod.ZeroCosts(), "XAUUSD", base) is base


def test_missing_hour_forbid_defers_engine_entry_end_to_end(tmp_path):
    """T20 全链：点差模型缺统计的小时禁入（不抛错）——引擎把开仓推迟到
    下一个有统计的小时；出场照常（缺统计小时取全天最大 p90 的保守成本）。"""
    import numpy as np
    import pandas as pd
    from mt5data.sessions import load_session_policy
    from mt5data.store import _COLUMNS
    from mt5quant.backtest.engine_a import run_backtest
    from mt5quant.strategies import BaseStrategy
    from mt5quant.strategies.base import ExitRules

    mod = _load_script()
    root = _write_ticks(tmp_path / "data")     # 只覆盖纽约 8 点（12:00 UTC，EDT）

    class AlwaysLong(BaseStrategy):
        exit_rules = ExitRules()

        def generate_signals(self, bars, params):
            return pd.Series(1, index=bars.index, dtype="int64")

    costs, _ = mod.build_costs(str(root), "XAUUSD", 0.0, allow_zero_swap=True)
    # bar 开盘 2026-10-19 10/11/12/13 UTC = 纽约 6/7/8/9 点；只有 8 点有统计
    bars = pd.DataFrame({
        "time": pd.to_datetime(["2026-10-19 10:00", "2026-10-19 11:00",
                                "2026-10-19 12:00", "2026-10-19 13:00"]),
        "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0,
        "tick_volume": 10, "spread": 5, "real_volume": 0,
    })[_COLUMNS]
    base = load_session_policy(str(mod.SESSIONS_PATH))
    pol = mod.entry_policy_for(costs, "XAUUSD", base)
    res = run_backtest(AlwaysLong(), bars, costs, symbol="XAUUSD", params={},
                       contract_size=100.0, timeframe="H1", session_policy=pol)
    assert len(res.trades) == 1                    # 不抛错，且只有一笔
    t = res.trades.iloc[0]
    assert t["entry_time"] == pd.Timestamp("2026-10-19 12:00")  # 纽约 8 点（有统计）
    # 对照：无策略时 bar1（纽约 7 点，缺统计）照常入场
    res0 = run_backtest(AlwaysLong(), bars, costs, symbol="XAUUSD", params={},
                        contract_size=100.0, timeframe="H1")
    assert res0.trades.iloc[0]["entry_time"] == pd.Timestamp("2026-10-19 11:00")
