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
    # 覆盖阈值降到 1（tick 只有 1 天样本）：有统计小时放行、缺统计小时禁入
    # （min_days=20 的缺覆盖禁入见 test_entry_policy_min_days_forbids_thin）
    import dataclasses as dc
    thin = dc.replace(base, min_days_per_hour=1)
    pol = mod.entry_policy_for(costs, "XAUUSD", thin)
    # 缺统计小时（纽约 7 点 = 11:00 UTC EDT）禁入；换日窗照常
    from datetime import datetime
    assert not pol.entry_allowed("XAUUSD", datetime(2026, 10, 19, 11, 0))
    assert pol.entry_allowed("XAUUSD", datetime(2026, 10, 19, 12, 0))
    assert not pol.entry_allowed("XAUUSD", datetime(2026, 10, 19, 21, 10))  # 换日窗
    # 原策略不被改动
    assert thin.entry_allowed("XAUUSD", datetime(2026, 10, 19, 11, 0))


def test_entry_policy_min_days_forbids_thin_coverage(tmp_path):
    """R6/C4 失败测试：有统计但 n_days < min_days_per_hour 的小时按缺统计
    禁入（此前 entry_policy_for 只看小时有无统计——一天样本的小时在配置
    要求 20 天时仍放行）。"""
    from datetime import datetime

    from mt5data.sessions import load_session_policy

    mod = _load_script()
    root = _write_ticks(tmp_path / "data")     # 只覆盖纽约 8 点，1 天样本
    costs, _ = mod.build_costs(str(root), "XAUUSD", 0.1, allow_zero_swap=True)
    base = load_session_policy(str(mod.SESSIONS_PATH))   # min_days_per_hour=20
    pol = mod.entry_policy_for(costs, "XAUUSD", base)
    # 纽约 8 点（12:00 UTC，EDT）有统计但 n_days=1 < 20 → 禁入
    assert not pol.entry_allowed("XAUUSD", datetime(2026, 10, 19, 12, 0))
    # 阈值降到 1 → 同一小时放行（缺统计小时照旧禁入）
    import dataclasses as dc
    pol1 = mod.entry_policy_for(
        costs, "XAUUSD", dc.replace(base, min_days_per_hour=1))
    assert pol1.entry_allowed("XAUUSD", datetime(2026, 10, 19, 12, 0))
    assert not pol1.entry_allowed("XAUUSD", datetime(2026, 10, 19, 11, 0))


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
    # 覆盖阈值降到 1（tick 只有 1 天样本；min_days=20 的禁入口径见
    # test_entry_policy_min_days_forbids_thin）
    import dataclasses as dc
    pol = mod.entry_policy_for(costs, "XAUUSD",
                               dc.replace(base, min_days_per_hour=1))
    res = run_backtest(AlwaysLong(), bars, costs, symbol="XAUUSD", params={},
                       contract_size=100.0, timeframe="H1", session_policy=pol, volume_lots=1.0)
    assert len(res.trades) == 1                    # 不抛错，且只有一笔
    t = res.trades.iloc[0]
    assert t["entry_time"] == pd.Timestamp("2026-10-19 12:00")  # 纽约 8 点（有统计）
    # 对照：无策略时 bar1（纽约 7 点，缺统计）照常入场
    res0 = run_backtest(AlwaysLong(), bars, costs, symbol="XAUUSD", params={},
                        contract_size=100.0, timeframe="H1", volume_lots=1.0)
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
        "--contract-size", "100", "--allow-zero-swap", "--risk-per-trade", "0"])
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
    specs = root / "specs"
    specs.mkdir(parents=True, exist_ok=True)
    (specs / "latest.json").write_text(
        json.dumps({"XAUUSD": {"volume_min": 0.01, "volume_step": 0.01,
                               "volume_max": 200.0}}), encoding="utf-8")
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


# ------------------------------------------------- T23（M2）：定手数口径

import pytest as _pytest  # noqa: E402


def test_resolve_lots_mode_semantics():
    """CLI 手数语义矩阵（见 run_backtest.resolve_lots_mode 注释）：
    缺省=风险 0.005；显式 --volume-lots=固定；--risk-per-trade 0=固定 1.0；
    两者同显式（风险>0）=拒绝；非法值拒绝。"""
    mod = _load_script()
    lots, sizing, info = mod.resolve_lots_mode(None, None)
    assert lots is None and sizing.risk_fraction == 0.005
    assert info == {"mode": "risk", "risk_fraction": 0.005,
                    "basis": "initial", "fallback_stop_atr": 1.0}
    lots, sizing, info = mod.resolve_lots_mode(None, 2.0)
    assert (lots, sizing, info["mode"]) == (2.0, None, "fixed")
    lots, sizing, info = mod.resolve_lots_mode(0.006, None)
    assert sizing.risk_fraction == 0.006 and info["mode"] == "risk"
    lots, sizing, info = mod.resolve_lots_mode(0, None)
    assert (lots, sizing, info["mode"]) == (1.0, None, "fixed")
    with _pytest.raises(ValueError, match="互斥"):
        mod.resolve_lots_mode(0.006, 2.0)
    with _pytest.raises(ValueError, match="volume-lots"):
        mod.resolve_lots_mode(None, 0.0)
    with _pytest.raises(ValueError, match="risk-per-trade"):
        mod.resolve_lots_mode(-0.1, None)


def _cli_bars(n=20):
    """恒定波幅 H1（TR=1.0 → ATR14 从 bar13 起恒 1.0），末根收 105（高点
    105.5 覆盖，保持 OHLC 合法；末根的 TR 尖峰发生在唯一入场之后，无影响）。"""
    import pandas as pd
    times = pd.date_range("2026-01-05 10:00", periods=n, freq="h")
    df = pd.DataFrame({
        "time": times, "open": 100.0, "high": 100.5, "low": 99.5,
        "close": 100.0, "tick_volume": 10, "spread": 5, "real_volume": 0,
    })
    df.loc[n - 1, ["close", "high"]] = [105.0, 105.5]
    return df


def _run_cli(mod, tmp_path, monkeypatch, root, extra):
    from mt5data.store import save_bars
    save_bars(str(root), "XAUUSD", "H1", _cli_bars())
    monkeypatch.chdir(tmp_path)
    argv = ["run_backtest.py", "--strategy",
            "mt5quant.strategies.library.buy_hold:BuyHold",
            "--symbol", "XAUUSD", "--timeframe", "H1", "--params", "{}",
            "--data-root", str(root), "--out", str(tmp_path / "out"),
            "--costs", "zero", "--contract-size", "100"] + list(extra)
    monkeypatch.setattr("sys.argv", argv)
    return mod.main()


def test_cli_default_is_risk_sizing_with_manifest_record(tmp_path, monkeypatch):
    """缺省（不给 --risk-per-trade/--volume-lots）= 风险定手数 0.005：
    ATR 可得后入场 0.50 手（50/(1.0×100)）；ATR 回看不足的尝试计入
    skipped_zero_lots；manifest.extra.sizing 记录方式。"""
    mod = _load_script()
    root = tmp_path / "data"
    root.mkdir()
    specs = root / "specs"
    specs.mkdir(parents=True)
    (specs / "latest.json").write_text(json.dumps(
        {"XAUUSD": {"volume_min": 0.01, "volume_step": 0.01,
                    "volume_max": 200.0}}), encoding="utf-8")
    assert _run_cli(mod, tmp_path, monkeypatch, root, []) == 0
    run_dir = sorted((tmp_path / "out").iterdir())[-1]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["extra"]["sizing"] == {
        "mode": "risk", "risk_fraction": 0.005, "basis": "initial",
        "fallback_stop_atr": 1.0, "vol_min": 0.01, "vol_step": 0.01,
        "vol_max": 200.0}
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["trade_count"] == 1
    # BuyHold 首根 bar 信号为 0 → bar2..13 共 12 次尝试（sig_bar 1..12 无 ATR）
    assert summary["skipped_zero_lots"] == 12
    # 手数如实进入交易：入场 bar14 open=100，末根 close=105（零成本），
    # 0.50 手 × 合约 100 → 净利恰为 250（手数不对这个数就对不上）
    assert summary["net_profit"] == 250.0


def test_cli_volume_lots_explicit_is_fixed(tmp_path, monkeypatch):
    mod = _load_script()
    root = tmp_path / "data"
    root.mkdir()
    assert _run_cli(mod, tmp_path, monkeypatch, root,
                    ["--volume-lots", "0.02"]) == 0
    run_dir = sorted((tmp_path / "out").iterdir())[-1]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["extra"]["sizing"] == {"mode": "fixed", "volume_lots": 0.02}
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["trade_count"] == 1 and summary["skipped_zero_lots"] == 0


def test_cli_risk_zero_is_fixed_one_lot(tmp_path, monkeypatch):
    mod = _load_script()
    root = tmp_path / "data"
    root.mkdir()
    assert _run_cli(mod, tmp_path, monkeypatch, root,
                    ["--risk-per-trade", "0"]) == 0
    run_dir = sorted((tmp_path / "out").iterdir())[-1]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    sizing = manifest["extra"]["sizing"]
    assert sizing["mode"] == "fixed" and sizing["volume_lots"] == 1.0


def test_cli_risk_and_volume_conflict_rejected(tmp_path, monkeypatch, capsys):
    mod = _load_script()
    root = tmp_path / "data"
    root.mkdir()
    rc = _run_cli(mod, tmp_path, monkeypatch, root,
                  ["--risk-per-trade", "0.006", "--volume-lots", "1.0"])
    assert rc == 1
    assert "互斥" in capsys.readouterr().out


def test_cli_risk_mode_requires_volume_bounds_in_specs(tmp_path, monkeypatch,
                                                       capsys):
    """风险定手数缺快照 volume 字段 → 报错指路 snapshot_specs（不静默假设）。"""
    mod = _load_script()
    root = tmp_path / "data"
    root.mkdir()
    specs = root / "specs"
    specs.mkdir(parents=True)
    (specs / "latest.json").write_text(
        json.dumps({"XAUUSD": {"contract_size": 100.0}}), encoding="utf-8")
    rc = _run_cli(mod, tmp_path, monkeypatch, root, [])
    assert rc == 1
    out = capsys.readouterr().out
    assert "volume_min" in out or "snapshot_specs" in out


# ------------------------------------------------- T27（H3）+ T38（H4/D24）

# T38/D24 缺省切换：研究侧 CLI 的 --data-root 缺省从 data 改为 data_research；
# 读主库须显式 --main-store；读主库锁箱期再加 --ops-ack。


def test_resolve_data_root_semantics():
    """数据根解析矩阵（T38/D24）：
    显式 --data-root 永远优先；--main-store → data；缺省（及保留兼容的
    --research-store）→ data_research；--main-store 与 --research-store
    同时给出 → 拒绝（口径二义）。"""
    mod = _load_script()
    # 显式 --data-root 优先于一切 flag
    assert mod.resolve_data_root("some/root", False, False) == "some/root"
    assert mod.resolve_data_root("some/root", True, False) == "some/root"
    assert mod.resolve_data_root("some/root", False, True) == "some/root"
    # 缺省（T38 翻转：不再是 data）
    assert mod.resolve_data_root(None, False, False) == "data_research"
    # --research-store（T27 兼容别名）与缺省同一指向
    assert mod.resolve_data_root(None, True, False) == "data_research"
    # --main-store → 主库
    assert mod.resolve_data_root(None, False, True) == "data"
    # 两个 store flag 互斥
    with pytest.raises(ValueError, match="互斥"):
        mod.resolve_data_root(None, True, True)


def test_classify_store_three_kinds():
    """数据根三分类（T38/D24）：research（data_research 字面或仓库内绝对
    路径）/ main（data 字面或仓库内绝对路径）/ custom（其余，测试与临时
    数据根）。"""
    mod = _load_script()
    assert mod.classify_store("data_research") == "research"
    assert mod.classify_store(str(mod.REPO_ROOT / "data_research")) == "research"
    assert mod.classify_store("data") == "main"
    assert mod.classify_store(str(mod.REPO_ROOT / "data")) == "main"
    assert mod.classify_store(str(_tmp_root())) == "custom"
    assert mod.classify_store("some/root") == "custom"


def _tmp_root():
    """不属于仓库两库的任意路径（classify_store 的 custom 用例）。"""
    from pathlib import Path
    import tempfile
    return Path(tempfile.gettempdir()) / "mt5-t38-custom-root"


def _research_store(tmp_path):
    """可直接回测的研究库：bars + specs + 成本缓存（无 tick——研究账户拒读）。
    缓存在旁边的 build/ 目录用真 tick 构建，再只复制缓存文件过来。"""
    import shutil

    import pandas as pd
    from mt5data.store import _COLUMNS, save_bars

    build = tmp_path / "build"
    _full_hour_ticks(build)                      # 建 tick → 建缓存
    rb = _load_script()
    from mt5quant.costs import SpreadModel
    from mt5data.ticks import load_ticks, tick_watermark
    ticks = load_ticks(str(build), "XAUUSD",
                       columns=["time_msc", "bid", "ask"])
    model = SpreadModel.from_ticks("XAUUSD", ticks, mode="relative")
    rb.save_spread_model_cache(
        rb.spread_model_cache_path(str(build)), model,
        {"XAUUSD": tick_watermark(str(build), "XAUUSD")})

    root = tmp_path / "data_research"
    times = pd.date_range("2026-01-05 10:00", periods=20, freq="h")
    save_bars(str(root), "XAUUSD", "H1", pd.DataFrame({
        "time": times, "open": 100.0, "high": 100.5, "low": 99.5,
        "close": 100.0, "tick_volume": 10, "spread": 5, "real_volume": 0,
    })[_COLUMNS])
    specs = root / "specs"
    specs.mkdir(parents=True)
    (specs / "latest.json").write_text(json.dumps(
        {"XAUUSD": {"contract_size": 100.0, "volume_min": 0.01,
                    "volume_step": 0.01, "volume_max": 200.0}}),
        encoding="utf-8")
    costs = root / "costs"
    costs.mkdir()
    shutil.copyfile(rb.spread_model_cache_path(str(build)),
                    str(costs / "spread_model.json"))
    assert not (root / "ticks").exists()         # 研究库无 tick，有意为之
    return root


def test_cli_research_store_switch_uses_data_research(tmp_path, monkeypatch,
                                                      capsys):
    """--research-store：--data-root 缺省切换为 data_research（cwd 相对），
    成本经缓存文件装配（无 tick），manifest 记录 research_store 与 data_root。
    cwd 下只有 data_research 没有 data——成功即证明读的是研究库。"""
    mod = _load_script()
    root = _research_store(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", [
        "run_backtest.py", "--strategy",
        "mt5quant.strategies.library.buy_hold:BuyHold",
        "--symbol", "XAUUSD", "--timeframe", "H1", "--params", "{}",
        "--research-store", "--out", str(tmp_path / "out"),
        "--volume-lots", "0.01", "--allow-zero-swap"])
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "研究库" in out and "缓存" in out
    run_dir = sorted((tmp_path / "out").iterdir())[-1]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["extra"]["research_store"] is True
    assert "data_research" in manifest["extra"]["data_root"].replace("\\", "/")


def test_cli_default_reads_research_store_without_flags(tmp_path, monkeypatch,
                                                        capsys):
    """T38/D24 翻转（原 T27 对照用例反转）：不带任何 flag 时 --data-root
    缺省即 data_research——cwd 下只有 data_research 没有 data/，成功即证明
    缺省读的是研究库；manifest 记 research_store 模式。"""
    mod = _load_script()
    _research_store(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", [
        "run_backtest.py", "--strategy",
        "mt5quant.strategies.library.buy_hold:BuyHold",
        "--symbol", "XAUUSD", "--timeframe", "H1", "--params", "{}",
        "--volume-lots", "0.01", "--allow-zero-swap",
        "--out", str(tmp_path / "out")])
    assert mod.main() == 0, capsys.readouterr().out
    assert "研究库" in capsys.readouterr().out
    run_dir = sorted((tmp_path / "out").iterdir())[-1]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["extra"]["research_store"] is True
    assert "data_research" in manifest["extra"]["data_root"].replace("\\", "/")


def test_cli_main_store_requires_explicit_flag(tmp_path, monkeypatch, capsys):
    """T38/D24：--data-root 指到主库（字面 data 或仓库内绝对路径）而未给
    --main-store → 拒绝并指路（缺省只读研究库，主库是显式豁免）。"""
    mod = _load_script()
    _research_store(tmp_path)
    monkeypatch.chdir(tmp_path)
    base = ["run_backtest.py", "--strategy",
            "mt5quant.strategies.library.buy_hold:BuyHold",
            "--symbol", "XAUUSD", "--timeframe", "H1", "--params", "{}",
            "--volume-lots", "0.01", "--allow-zero-swap",
            "--out", str(tmp_path / "out")]
    # 字面 "data"
    monkeypatch.setattr("sys.argv", base + ["--data-root", "data"])
    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "须显式 --main-store" in out and "主库" in out
    # 仓库内绝对路径同样拦（绕不开：换个写法指同一目录不算显式豁免）
    monkeypatch.setattr("sys.argv",
                        base + ["--data-root", str(mod.REPO_ROOT / "data")])
    assert mod.main() == 1
    assert "须显式 --main-store" in capsys.readouterr().out
    # 给了 --main-store → 守卫放行（cwd 无 data/，后续因缺 specs/数据失败，
    # 但不再是守卫的拒绝）
    monkeypatch.setattr("sys.argv", base + ["--data-root", "data",
                                            "--main-store"])
    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "须显式 --main-store" not in out
    assert "主库模式" in out                      # 已进入主库模式（显式豁免）
    assert "合约大小未知" in out or "无可用数据" in out


def test_cli_missing_research_store_points_to_export(tmp_path, monkeypatch,
                                                     capsys):
    """T38/D24：缺省（研究库模式）下 data_research 不存在 → 报错指路
    export_research_store（绝不静默回退主库）。"""
    mod = _load_script()
    monkeypatch.chdir(tmp_path)            # cwd 空无一物：无 data 也无 data_research
    monkeypatch.setattr("sys.argv", [
        "run_backtest.py", "--strategy",
        "mt5quant.strategies.library.buy_hold:BuyHold",
        "--symbol", "XAUUSD", "--timeframe", "H1", "--params", "{}",
        "--volume-lots", "0.01", "--allow-zero-swap",
        "--out", str(tmp_path / "out")])
    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "data_research" in out and "export_research_store" in out


def _main_store_with_lockbox(tmp_path):
    """cwd 相对的主库样式夹具：tmp/data 下 bars + lockbox.json（v1 序列键，
    cutoff 落在 bars 中段）。"""
    from mt5data.lockbox import write_config
    from mt5data.store import save_bars

    root = tmp_path / "data"
    save_bars(str(root), "XAUUSD", "H1", _cli_bars())
    write_config(str(root), {"XAUUSD/H1": "2026-01-06T00:00:00"})
    return root


def test_cli_main_store_ops_ack_gates_lockbox_period(tmp_path, monkeypatch,
                                                     capsys):
    """T38/D24：读主库（--main-store）的锁箱期（--end 越过 cutoff）必须显式
    --ops-ack——不给 → LockboxError（沿用"显式请求不做静默截断"）；给了 →
    放行并在 manifest 记 main_store / ops_ack（审计线索）。"""
    import pandas as pd

    mod = _load_script()
    _main_store_with_lockbox(tmp_path)
    monkeypatch.chdir(tmp_path)
    base = ["run_backtest.py", "--strategy",
            "mt5quant.strategies.library.buy_hold:BuyHold",
            "--symbol", "XAUUSD", "--timeframe", "H1", "--params", "{}",
            "--costs", "zero",
            "--volume-lots", "0.01", "--allow-zero-swap",
            "--contract-size", "100",
            "--main-store", "--end", "2026-01-06 06:00",
            "--out", str(tmp_path / "out")]
    # 1) 不带 --ops-ack：越过 cutoff → 拒绝（锁箱边界如实报出）
    monkeypatch.setattr("sys.argv", list(base))
    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "锁箱" in out
    assert not (tmp_path / "out").exists() or not list(
        (tmp_path / "out").iterdir())
    # 2) 带 --ops-ack：运维豁免放行，完整读到锁箱段
    monkeypatch.setattr("sys.argv", base + ["--ops-ack"])
    assert mod.main() == 0, capsys.readouterr().out
    run_dir = sorted((tmp_path / "out").iterdir())[-1]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["extra"]["main_store"] is True
    assert manifest["extra"]["ops_ack"] is True


def test_cli_research_store_missing_cache_points_to_export(tmp_path,
                                                           monkeypatch,
                                                           capsys):
    mod = _load_script()
    root = _research_store(tmp_path)
    (root / "costs" / "spread_model.json").unlink()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", [
        "run_backtest.py", "--strategy",
        "mt5quant.strategies.library.buy_hold:BuyHold",
        "--symbol", "XAUUSD", "--timeframe", "H1", "--params", "{}",
        "--research-store", "--out", str(tmp_path / "out"),
        "--volume-lots", "0.01", "--allow-zero-swap"])
    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "export_research_store" in out


def test_cli_build_cost_model_refused_under_research_store(tmp_path,
                                                           monkeypatch,
                                                           capsys):
    """研究库无 tick 可建模型：--build-cost-model + --research-store 拒绝。"""
    mod = _load_script()
    _research_store(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", [
        "run_backtest.py", "--strategy",
        "mt5quant.strategies.library.buy_hold:BuyHold",
        "--symbol", "XAUUSD", "--timeframe", "H1", "--params", "{}",
        "--research-store", "--build-cost-model",
        "--out", str(tmp_path / "out"),
        "--volume-lots", "0.01", "--allow-zero-swap"])
    assert mod.main() == 1
    assert "研究库" in capsys.readouterr().out


# ------------------------------------------------- R6（C4+I2）：越幅接线与
# 成本版本身份

def _write_overshoot_cache(mod, root, symbol="XAUUSD", *, p50_by_hour=None):
    """v2 缓存：点差模型 + 越幅表（build_cost_model --overshoot 的产物形状：
    version 2 / sampling / overshoot / overshoot_sha256，两个 sha 均自洽）。"""
    import os

    from mt5data.ticks import load_ticks, tick_watermark
    from mt5quant.costs import SpreadModel
    from mt5quant.costs.overshoot import OvershootStat, StopOvershootModel

    ticks = load_ticks(str(root), symbol, columns=["time_msc", "bid", "ask"])
    model = SpreadModel.from_ticks(symbol, ticks, mode="relative")
    path = mod.spread_model_cache_path(str(root))
    doc = mod.save_spread_model_cache(path, model,
                                      {symbol: tick_watermark(str(root), symbol)})
    doc["version"] = 2
    doc["sampling"] = model.sampling
    om = StopOvershootModel({symbol: {
        h: OvershootStat(p50=v, p90=v * 4.0, n=5, n_days=25)
        for h, v in (p50_by_hour
                     or {h: 0.001 for h in range(24)}).items()}})
    doc["overshoot"] = om.to_dict()
    doc["overshoot_sha256"] = mod.canonical_json_sha256(doc["overshoot"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return doc


def test_build_costs_injects_overshoot_from_cache(tmp_path):
    """R6/C4 失败测试：v2 缓存带越幅表 → build_costs 注入 Costs（引擎
    stop_overshoot 非零），版本串带越幅 sha；此前 Costs 漏传 overshoot_model，
    正式回测的越幅恒为 0。"""
    mod = _load_script()
    root = _full_hour_ticks(tmp_path / "data")
    doc = _write_overshoot_cache(mod, root, p50_by_hour={
        h: (0.002 if h == 8 else 0.001) for h in range(24)})
    costs, ver = mod.build_costs(str(root), "XAUUSD", 0.0, allow_zero_swap=True)
    assert f"ov[{doc['overshoot_sha256'][:8]}]" in ver
    assert "no-overshoot" not in ver
    assert costs.stop_overshoot("XAUUSD", 8, price=2000.0) == pytest.approx(
        0.002 * 2000.0)
    assert costs.stop_overshoot("XAUUSD", 9, price=2000.0) == pytest.approx(
        0.001 * 2000.0)


def test_build_costs_without_overshoot_keeps_a15_and_annotates(tmp_path):
    """无越幅键（v1 缓存 / 直建）→ overshoot_model=None 保持 A-1.5 行为，
    版本串如实标注 no-overshoot。"""
    mod = _load_script()
    root = _full_hour_ticks(tmp_path / "data")
    _build_cache(mod, root)                    # v1 缓存（save_spread_model_cache）
    costs, ver = mod.build_costs(str(root), "XAUUSD", 0.0, allow_zero_swap=True)
    assert "no-overshoot" in ver and "ov[" not in ver
    assert costs.stop_overshoot("XAUUSD", 8, price=2000.0) == 0.0


def test_build_costs_legacy_adjacent_overshoot_not_injected(tmp_path):
    """缓存里的越幅表是旧 version 1（只有 adjacent_jump 口径，无引擎用的
    stop_level 统计）→ 不注入（注入会让引擎 stop 出场直接抛错）、不报错，
    版本串标 no-overshoot 并打印重建指路。"""
    mod = _load_script()
    root = _full_hour_ticks(tmp_path / "data")
    from mt5quant.costs.spread import BUCKET_TZ

    legacy = {"version": 1, "bucket_tz": BUCKET_TZ,
              "per_symbol": {"XAUUSD": {"8": {"p50": 0.001, "p90": 0.004,
                                              "n": 5, "n_days": 25}}}}
    p = mod.spread_model_cache_path(str(root))
    doc = json.loads(Path(p).read_text(encoding="utf-8")) \
        if Path(p).exists() else {}
    if not doc:
        _build_cache(mod, root)
        doc = json.loads(Path(p).read_text(encoding="utf-8"))
    doc["version"] = 2
    doc["sampling"] = doc["model"].get("sampling", "bar_open_m1")
    doc["overshoot"] = legacy
    doc["overshoot_sha256"] = mod.canonical_json_sha256(legacy)
    Path(p).write_text(json.dumps(doc), encoding="utf-8")
    costs, ver = mod.build_costs(str(root), "XAUUSD", 0.0, allow_zero_swap=True)
    assert "no-overshoot" in ver and "ov[" not in ver
    assert costs.stop_overshoot("XAUUSD", 8, price=2000.0) == 0.0


def test_build_costs_overshoot_sha_tamper_rejected(tmp_path):
    """越幅表内容与 overshoot_sha256 不一致 → 拒绝加载（绝不静默跳过）。"""
    mod = _load_script()
    root = _full_hour_ticks(tmp_path / "data")
    _write_overshoot_cache(mod, root)
    p = mod.spread_model_cache_path(str(root))
    doc = json.loads(Path(p).read_text(encoding="utf-8"))
    doc["overshoot"]["per_symbol"]["XAUUSD"]["8"]["p50"] = 0.5   # 改内容不动 sha
    Path(p).write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match="overshoot_sha256"):
        mod.build_costs(str(root), "XAUUSD", 0.0, allow_zero_swap=True)


def test_engine_stop_exit_includes_overshoot_via_build_costs(tmp_path):
    """R6/C4 失败测试（端到端经 build_costs）：含越幅表的缓存装配出的成本
    → 引擎 reason="stop" 的出场价比无越幅时恰好多扣该纽约小时越幅 p50×SL
    （slip=0：无越幅的 stop 出场价就是 SL 价位）。"""
    import dataclasses as dc

    import pandas as pd

    from mt5quant.backtest.engine_a import run_backtest
    from mt5quant.strategies import BaseStrategy
    from mt5quant.strategies.base import ExitRules

    class LongAfterWarmup(BaseStrategy):
        exit_rules = ExitRules(stop_loss_atr=1.5)

        def generate_signals(self, bars, params):
            sig = [0] * 14 + [1] * (len(bars) - 14)
            return pd.Series(sig, index=bars.index, dtype="int64")

    mod = _load_script()
    root = _full_hour_ticks(tmp_path / "data")
    _write_overshoot_cache(mod, root, p50_by_hour={
        h: (0.002 if h == 8 else 0.001) for h in range(24)})
    costs, _ver = mod.build_costs(str(root), "XAUUSD", 0.0, allow_zero_swap=True)
    plain = dc.replace(costs, overshoot_model=None)

    # 17 根 H1（2026-10-18 20:00 起）：bar0..14 TR=1（ATR14=1.0）；信号从
    # bar14 转 1 → bar15 开盘入场（SL≈98.5）；bar16（2026-10-19 12:00 UTC =
    # 纽约 8 点）盘中下穿 SL → reason="stop"
    times = pd.date_range("2026-10-18 20:00", periods=17, freq="h")
    bars = pd.DataFrame({
        "time": times, "open": 100.0, "high": 100.5, "low": 99.5,
        "close": 100.0, "tick_volume": 10, "spread": 5, "real_volume": 0,
    })
    bars.loc[15, ["high", "low", "close"]] = [100.2, 99.8, 99.9]
    bars.loc[16, ["open", "high", "low", "close"]] = [99.0, 99.2, 97.0, 97.5]

    def run(c):
        res = run_backtest(LongAfterWarmup(), bars, c, symbol="XAUUSD",
                           params={}, contract_size=100.0, timeframe="H1",
                           volume_lots=1.0)
        assert len(res.trades) == 1
        return res.trades.iloc[0]

    t_plain, t_ov = run(plain), run(costs)
    assert t_plain["exit_reason"] == "stop"
    assert t_ov["exit_reason"] == "stop"
    sl = float(t_plain["exit_price"])          # slip=0、无越幅 → 恰为 SL 价位
    # 出场 bar（2026-10-19 12:00 UTC）= 纽约 8 点：夹具里该小时 p50=0.002，
    # 越幅 = 0.002 × SL 价位（独立于被测对象计算，不从 costs 反推）
    expected = 0.002 * sl
    assert t_ov["exit_price"] == pytest.approx(sl - expected)


def test_cost_version_binds_cache_content_sampling_and_overshoot(tmp_path):
    """R6/I2 失败测试：缓存内容（合法改 + 更新 sha）变化 → 版本串必须变化
    ——点差内容、越幅表内容、采样口径任何一处不同都是不同的成本身份。"""
    mod = _load_script()
    root = _full_hour_ticks(tmp_path / "data")
    _write_overshoot_cache(mod, root)
    _, ver1 = mod.build_costs(str(root), "XAUUSD", 0.0, allow_zero_swap=True)
    assert "/bar_open_m1#" in ver1          # 采样口径进版本串

    def _patch(mutate):
        p = mod.spread_model_cache_path(str(root))
        doc = json.loads(Path(p).read_text(encoding="utf-8"))
        mutate(doc)
        doc["model_sha256"] = mod.canonical_json_sha256(doc["model"])
        if "overshoot" in doc:
            doc["overshoot_sha256"] = mod.canonical_json_sha256(
                doc["overshoot"])
        Path(p).write_text(json.dumps(doc), encoding="utf-8")

    # 1) 点差模型内容变（p90 0.3/2400 → 0.5）→ 版本串变
    _patch(lambda d: d["model"]["per_symbol"]["XAUUSD"]["8"].update(p90=0.5))
    _, ver2 = mod.build_costs(str(root), "XAUUSD", 0.0, allow_zero_swap=True)
    assert ver2 != ver1
    # 2) 只变越幅表（点差不动）→ 版本串也变
    _patch(lambda d: d["overshoot"]["per_symbol"]["XAUUSD"]["8"].update(
        p50=0.123, p90=0.5))
    _, ver3 = mod.build_costs(str(root), "XAUUSD", 0.0, allow_zero_swap=True)
    assert ver3 != ver2 and ver3 != ver1
    # 3) 采样口径变（同统计、bar_open_m1 → tick）→ 版本串变
    _patch(lambda d: d["model"].update(sampling="tick"))
    _, ver4 = mod.build_costs(str(root), "XAUUSD", 0.0, allow_zero_swap=True)
    assert ver4 != ver3 and "/tick#" in ver4


def test_cost_version_cache_hit_equals_direct_build(tmp_path):
    """缓存命中与同内容直建的**点差身份**一致（sha 取自模型负载本身，不是
    缓存文件——同一份统计两种来路同一 sha/采样标注）；越幅表只存在于缓存，
    直建路径如实标 no-overshoot（与 I2 的身份区分不冲突）。"""
    mod = _load_script()
    root = _full_hour_ticks(tmp_path / "data")
    _write_overshoot_cache(mod, root)
    cached, ver_c = mod.build_costs(str(root), "XAUUSD", 0.0,
                                    allow_zero_swap=True)
    import os

    p = mod.spread_model_cache_path(str(root))
    os.remove(p)                       # 删缓存 → 直建路径（水位已无缓存可比）
    direct, ver_d = mod.build_costs(str(root), "XAUUSD", 0.0,
                                    allow_zero_swap=True)
    sha8 = mod.canonical_json_sha256(cached.spread.to_dict())[:8]
    assert f"/bar_open_m1#{sha8}" in ver_c
    assert f"/bar_open_m1#{sha8}" in ver_d       # 同一统计同一身份段
    assert direct.spread.stat(8).p90 == cached.spread.stat(8).p90
    # 直建无缓存 → 无越幅表可挂，标注 no-overshoot（缓存路径是 ov[...]）
    assert "no-overshoot" in ver_d and "ov[" not in ver_d
    assert "ov[" in ver_c


# ----------------------------------------------- R7/I2 残留：swap 费率身份

def _write_calibrated_swap(root, long_rate, short_rate=-0.5):
    """calibrated.json + specs 快照（calibrated 三倍日来源）——swap 实测费率路径。"""
    import os

    costs = os.path.join(str(root), "costs")
    os.makedirs(costs, exist_ok=True)
    Path(costs, "calibrated.json").write_text(json.dumps({
        "version": 1, "symbols": {"XAUUSD": {
            "swap": {"free_of_swap": None,
                     "long": {"per_lot_per_night": long_rate},
                     "short": {"per_lot_per_night": short_rate}}}}}),
        encoding="utf-8")
    specs = os.path.join(str(root), "specs")
    os.makedirs(specs, exist_ok=True)
    Path(specs, "latest.json").write_text(json.dumps({
        "XAUUSD": {"swap_rollover3days": 3, "contract_size": 100}}),
        encoding="utf-8")


def test_cost_version_binds_actual_swap_rates_calibrated(tmp_path):
    """R7/I2 失败测试：calibrated 实测每夜费率 −1 → −10（实际费用随之
    变化），cost_model_version 必须变化，且带 swap 规则摘要 sha8——此前
    版本串完全相同，swap 费率不构成成本身份。"""
    mod = _load_script()
    root = _full_hour_ticks(tmp_path / "data")
    _write_calibrated_swap(root, -1.0)
    _, ver1 = mod.build_costs(str(root), "XAUUSD", 0.0, allow_zero_swap=True)
    assert "swap-cal" in ver1
    assert "swap-cal" in ver1 and "[" in ver1.split("swap-cal", 1)[1]
    _write_calibrated_swap(root, -10.0)     # 只改费率
    _, ver2 = mod.build_costs(str(root), "XAUUSD", 0.0, allow_zero_swap=True)
    assert ver2 != ver1


def test_cost_version_binds_actual_swap_rates_snapshot(tmp_path):
    """快照推算路径同理：swap_long 费率变 → 版本串变（费率身份不分来路）。"""
    import os

    mod = _load_script()

    def _snapshot_root(swap_long):
        root = _full_hour_ticks(tmp_path / f"d{swap_long}" / "data")
        specs = os.path.join(str(root), "specs")
        os.makedirs(specs, exist_ok=True)
        Path(specs, "latest.json").write_text(json.dumps({
            "XAUUSD": {"swap_mode": 2, "swap_long": swap_long,
                       "swap_short": -2.0, "swap_rollover3days": 3,
                       "contract_size": 100, "currency_profit": "USD"}}),
            encoding="utf-8")
        return root

    root1 = _snapshot_root(-1.0)
    _, ver1 = mod.build_costs(str(root1), "XAUUSD", 0.0, allow_zero_swap=True)
    assert "swap-snapshot" in ver1
    root2 = _snapshot_root(-10.0)
    _, ver2 = mod.build_costs(str(root2), "XAUUSD", 0.0, allow_zero_swap=True)
    assert ver2 != ver1
