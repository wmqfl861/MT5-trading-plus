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


# ------------------------------------------------- T28（M8）：点差模型缓存


def _full_hour_ticks(root, symbol="XAUUSD"):
    """覆盖纽约全 24 小时的 tick（每小时 2 条）。"""
    import numpy as np
    import pandas as pd
    from mt5data.ticks import save_ticks

    save_ticks(str(root), symbol, pd.DataFrame({
        "time_msc": np.asarray(
            [pd.Timestamp(f"2026-10-{19 + h // 24} {h % 24:02d}:00:00").value // 10**6
             for h in range(24) for _ in range(2)], dtype="int64"),
        "bid": [2400.0] * 48,
        "ask": [2400.3] * 48,
        "last": [2400.15] * 48,
        "volume": [0.0] * 48,
        "flags": [2] * 48,
    }))
    return root


def _build_cache(mod, root, symbol="XAUUSD", mode="relative"):
    from mt5data.ticks import tick_watermark
    from mt5quant.costs import SpreadModel
    from mt5data.ticks import load_ticks

    ticks = load_ticks(str(root), symbol,
                       columns=["time_msc", "bid", "ask"])
    model = SpreadModel.from_ticks(symbol, ticks, mode=mode)
    return mod.save_spread_model_cache(
        mod.spread_model_cache_path(str(root)), model,
        {symbol: tick_watermark(str(root), symbol)})


def test_build_costs_prefers_cache_and_never_loads_ticks(tmp_path, monkeypatch):
    """缓存命中：build_costs 绝不加载原始 tick（load_ticks 被调即失败），
    统计与直建逐位一致（同一份 tick 构建）。"""
    mod = _load_script()
    root = _full_hour_ticks(tmp_path / "data")
    direct_costs, direct_ver = mod.build_costs(str(root), "XAUUSD", 0.1,
                                               allow_zero_swap=True)
    _build_cache(mod, root)

    def _no_ticks(*a, **k):
        raise AssertionError("缓存命中时不得加载原始 tick（M8）")

    monkeypatch.setattr(mod, "load_ticks", _no_ticks)
    costs, ver = mod.build_costs(str(root), "XAUUSD", 0.1, allow_zero_swap=True)
    assert ver == direct_ver                      # 版本串不因缓存而变
    for h in range(24):
        assert costs.spread.stat(h).p90 == direct_costs.spread.stat(h).p90
        assert costs.spread.stat(h).n == direct_costs.spread.stat(h).n


def test_build_costs_cache_watermark_mismatch_requires_rebuild(tmp_path):
    """tick 水位变化（新 tick 落库）而模型未重建 → 报错提示重建，
    绝不静默用旧模型。"""
    import numpy as np
    import pandas as pd
    from mt5data.ticks import save_ticks

    mod = _load_script()
    root = _full_hour_ticks(tmp_path / "data")
    _build_cache(mod, root)
    # 追加新 tick → 水位 rows/last_msc 变化
    save_ticks(str(root), "XAUUSD", pd.DataFrame({
        "time_msc": np.asarray(
            [pd.Timestamp("2026-10-21 03:00:00").value // 10**6], dtype="int64"),
        "bid": [2400.0], "ask": [2400.4], "last": [2400.2],
        "volume": [0.0], "flags": [2],
    }))
    with pytest.raises(ValueError, match="build_cost_model"):
        mod.build_costs(str(root), "XAUUSD", 0.1, allow_zero_swap=True)


def test_build_costs_cache_sha_tamper_rejected(tmp_path):
    """缓存内容与 model_sha256 不一致（手改/写坏）→ 拒绝加载。"""
    mod = _load_script()
    root = _full_hour_ticks(tmp_path / "data")
    _build_cache(mod, root)
    p = mod.spread_model_cache_path(str(root))
    doc = json.loads(Path(p).read_text(encoding="utf-8"))
    hours = doc["model"]["per_symbol"]["XAUUSD"]
    first = sorted(hours)[0]
    hours[first]["p90"] = 0.0                     # 改内容不动 sha
    Path(p).write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match="sha256"):
        mod.build_costs(str(root), "XAUUSD", 0.1, allow_zero_swap=True)


def test_build_costs_cache_mode_or_symbol_mismatch_rejected(tmp_path):
    """口径不一致（缓存 absolute、请求 relative）或品种不在缓存 → 报错重建。"""
    mod = _load_script()
    root = _full_hour_ticks(tmp_path / "data")
    _build_cache(mod, root, mode="absolute")
    with pytest.raises(ValueError, match="absolute"):
        mod.build_costs(str(root), "XAUUSD", 0.1, allow_zero_swap=True,
                        spread_mode="relative")
    _build_cache(mod, root, mode="relative")
    with pytest.raises(ValueError, match="EURUSD"):
        mod.build_costs(str(root), "EURUSD", 0.1, allow_zero_swap=True)


def test_cli_missing_cache_errors_with_guidance(tmp_path, monkeypatch, capsys):
    """CLI --costs auto 缺缓存 → 明确报错并指路（不静默从 3.7GB tick 直建）。"""
    import numpy as np
    import pandas as pd
    from mt5data.store import save_bars

    mod = _load_script()
    root = tmp_path / "data"
    _full_hour_ticks(root)
    bars = pd.DataFrame({
        "time": pd.to_datetime(["2026-10-19 10:00", "2026-10-19 11:00"]),
        "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0,
        "tick_volume": 10, "spread": 5, "real_volume": 0,
    })
    save_bars(str(root), "XAUUSD", "H1", bars)
    monkeypatch.chdir(tmp_path)                    # 时钟校验读 cwd 的 config.json
    monkeypatch.setattr("sys.argv", [
        "run_backtest.py", "--strategy",
        "mt5quant.strategies.library.buy_hold:BuyHold",
        "--symbol", "XAUUSD", "--timeframe", "H1", "--params", "{}",
        "--data-root", str(root), "--out", str(tmp_path / "out"),
        "--contract-size", "100", "--allow-zero-swap"])
    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "spread_model.json" in out and "build_cost_model" in out
    assert "--build-cost-model" in out


def test_cli_build_cost_model_flag_builds_cache_and_runs(tmp_path, monkeypatch,
                                                         capsys):
    """--build-cost-model：本次运行内（重）建缓存再走缓存装配，回测成功；
    缓存文件带水位与 sha，事后可被 build_costs 直接命中。"""
    import numpy as np
    import pandas as pd
    from mt5data.store import save_bars

    mod = _load_script()
    root = tmp_path / "data"
    _full_hour_ticks(root)
    bars = pd.DataFrame({
        "time": pd.to_datetime(["2026-10-19 10:00", "2026-10-19 11:00",
                                "2026-10-19 12:00"]),
        "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0,
        "tick_volume": 10, "spread": 5, "real_volume": 0,
    })
    save_bars(str(root), "XAUUSD", "H1", bars)
    monkeypatch.syspath_prepend(str(SCRIPT_PATH.parent))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", [
        "run_backtest.py", "--strategy",
        "mt5quant.strategies.library.buy_hold:BuyHold",
        "--symbol", "XAUUSD", "--timeframe", "H1", "--params", "{}",
        "--data-root", str(root), "--out", str(tmp_path / "out"),
        "--contract-size", "100", "--allow-zero-swap", "--build-cost-model"])
    assert mod.main() == 0
    cache = root / "costs" / "spread_model.json"
    assert cache.exists()
    doc = json.loads(cache.read_text(encoding="utf-8"))
    assert doc["watermarks"]["XAUUSD"]["rows"] == 48
    # 二次运行不再直建（水位未变），照样成功
    monkeypatch.setattr("sys.argv", [
        "run_backtest.py", "--strategy",
        "mt5quant.strategies.library.buy_hold:BuyHold",
        "--symbol", "XAUUSD", "--timeframe", "H1", "--params", "{}",
        "--data-root", str(root), "--out", str(tmp_path / "out"),
        "--contract-size", "100", "--allow-zero-swap"])
    assert mod.main() == 0
    assert "缓存" in capsys.readouterr().out
