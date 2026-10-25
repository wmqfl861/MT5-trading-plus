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
