"""关卡配置与 G1/G4/G5 + `bt gate` 命令行。

计划失败测试落点：
- G5 第二次请求直接拒绝（test_run_g5_second_use_refused）；
- 改动 config/gates.toml 后拒绝运行（test_tampered_gates_toml_refuses）。
"""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from mt5quant.research.ledger import Ledger
from mt5quant.validation import gates as vg
from mt5quant.validation import stats as vstats

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "bt.py"
REPO_ROOT = Path(__file__).resolve().parents[1]
PAST = datetime(2026, 1, 1, tzinfo=timezone.utc)


def load_bt():
    spec = importlib.util.spec_from_file_location("bt_script_gate", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- 门槛配置

def test_load_gates_repo_file_matches_frozen_sha():
    cfg, sha = vg.load_gates()
    assert sha == vg.FROZEN_GATES_SHA256
    assert len(sha) == 64
    # 附录 B 逐条入库
    assert cfg.trade_count_min_intraday == 300
    assert cfg.trade_count_min_hourly == 150
    assert cfg.gross_cost_multiple_min == 3.0
    assert cfg.profit_factor_min == 1.2
    assert cfg.cost_scale_pass == 1.5
    assert cfg.cost_scale_record == 2.0
    assert cfg.plateau_min_fraction == 0.70
    assert cfg.yearly_min_fraction == 0.70
    assert cfg.yearly_max_concentration == 0.50
    assert cfg.random_entry_percentile == 95.0
    assert cfg.drop_best_fraction == 0.05
    assert cfg.cross_symbol_min == 2
    # T21b/H5：G3 改日历月窗口
    assert cfg.train_months_hourly == 24
    assert cfg.test_months_hourly == 6
    assert cfg.train_months_intraday == 12
    assert cfg.test_months_intraday == 3
    assert cfg.min_oos_ratio == 0.50
    assert cfg.dsr_min_probability == 0.95
    # T21a/C3：G5 单侧判定新键
    assert cfg.lockbox_sharpe_min_percentile == 5.0
    assert cfg.lockbox_warn_percentile == 95.0
    assert cfg.lockbox_min_trades_ratio == 0.50
    assert cfg.lockbox_bootstrap_sims == 2000


def test_tampered_gates_toml_refuses_run(tmp_path):
    """计划失败测试 5：gates.toml 内容改动 → sha256 与冻结值不符 → 拒绝。"""
    p = tmp_path / "gates.toml"
    p.write_bytes(vg.DEFAULT_GATES_TOML.read_bytes() + b"\n# tampered\n")
    with pytest.raises(vg.GatesConfigError, match="sha256"):
        vg.load_gates(p)


def test_missing_gates_file_is_error(tmp_path):
    with pytest.raises(vg.GatesConfigError):
        vg.load_gates(tmp_path / "nope.toml")


def test_unknown_key_rejected(tmp_path):
    p = tmp_path / "gates.toml"
    body = vg.DEFAULT_GATES_TOML.read_text(encoding="utf-8")
    p.write_text(body.replace("[g1]", "stray_key = 1\n\n[g1]"),
                 encoding="utf-8")
    with pytest.raises(vg.GatesConfigError, match="未知"):
        vg.load_gates(p, frozen_sha=vg.gates_sha256(p))


def test_load_gates_accepts_matching_frozen_sha(tmp_path):
    p = tmp_path / "gates.toml"
    p.write_bytes(vg.DEFAULT_GATES_TOML.read_bytes())
    cfg, sha = vg.load_gates(p, frozen_sha=vg.gates_sha256(p))
    assert cfg.dsr_min_probability == 0.95 and sha == vg.gates_sha256(p)


def test_trade_count_min_by_timeframe():
    cfg, _ = vg.load_gates()
    assert vg.trade_count_min("M1", cfg) == 300    # 未列出，从严按日内档
    assert vg.trade_count_min("M5", cfg) == 300
    assert vg.trade_count_min("M15", cfg) == 300
    assert vg.trade_count_min("M30", cfg) == 300
    assert vg.trade_count_min("H1", cfg) == 150
    assert vg.trade_count_min("H4", cfg) == 150
    assert vg.trade_count_min("D1", cfg) == 150


# ---------------------------------------------------------------- G1

def g_row(net, commission=2.0, when="2024-05-01 12:00"):
    """gross = net + commission（点差/滑点/隔夜为 0）。"""
    t = pd.Timestamp(when)
    return {"entry_time": t, "exit_time": t + pd.Timedelta(hours=2),
            "side": "long", "volume_lots": 1.0, "entry_price": 100.0,
            "exit_price": 100.0 + net + commission, "gross": net + commission,
            "commission": commission, "slippage_cost": 0.0, "spread_cost": 0.0,
            "swap": 0.0, "net": net, "r_multiple": float("nan"),
            "bars_held": 2, "exit_reason": "signal"}


def g_trades(nets, **kw):
    cols = list(g_row(0.0).keys())
    return pd.DataFrame([g_row(n, **kw) for n in nets], columns=cols)


@pytest.fixture(scope="module")
def cfg():
    return vg.load_gates()[0]


def test_g1_passes_on_healthy_trades(cfg):
    nets = [20.0] * 130 + [-10.0] * 20
    out = vg.run_g1(g_trades(nets), timeframe="H1", cfg=cfg)
    assert out["passed"] is True
    assert out["checks"]["trade_count"]["passed"] is True
    assert out["checks"]["net_profit_positive"]["passed"] is True
    assert out["checks"]["profit_factor"]["passed"] is True


def test_g1_trade_count_boundaries(cfg):
    assert vg.run_g1(g_trades([10.0] * 150), timeframe="H1", cfg=cfg)["passed"] is True
    assert vg.run_g1(g_trades([10.0] * 149), timeframe="H1", cfg=cfg)["passed"] is False
    assert vg.run_g1(g_trades([10.0] * 300), timeframe="M15", cfg=cfg)["passed"] is True
    assert vg.run_g1(g_trades([10.0] * 299), timeframe="M15", cfg=cfg)["passed"] is False


def test_g1_negative_net_fails(cfg):
    out = vg.run_g1(g_trades([10.0] * 200 + [-30.0] * 200), timeframe="H1", cfg=cfg)
    assert out["passed"] is False


def test_g1_gross_cost_multiple(cfg):
    # 每笔毛利 12、来回成本 2 → 6 倍 ≥ 3 通过；毛利 3、成本 2 → 1.5 倍不通过
    assert vg.run_g1(g_trades([10.0] * 160), timeframe="H1", cfg=cfg)["passed"] is True
    out = vg.run_g1(g_trades([1.0] * 160), timeframe="H1", cfg=cfg)
    assert out["passed"] is False
    assert out["checks"]["gross_cost_multiple"]["passed"] is False


def test_g1_profit_factor_floor(cfg):
    # PF = 220/200 = 1.1 < 1.2
    nets = [11.0] * 20 + [-10.0] * 20
    out = vg.run_g1(g_trades(nets), timeframe="H1", cfg=cfg)
    assert out["checks"]["profit_factor"]["passed"] is False
    assert out["passed"] is False


def test_g1_zero_cost_is_pass_with_note(cfg):
    out = vg.run_g1(g_trades([10.0] * 160, commission=0.0), timeframe="H1",
                    cfg=cfg)
    assert out["checks"]["gross_cost_multiple"]["passed"] is True
    assert out["checks"]["gross_cost_multiple"].get("note")


# ---------------------------------------------------------------- G4

def test_g4_overfit_noise_fails(cfg):
    """计划失败测试 2（G4 部分）：噪声上挑最优 → DSR 概率 < 0.95。"""
    rng = np.random.default_rng(21)
    trial_sharpes, best = [], None
    for _ in range(20):
        r = pd.Series(rng.normal(0.0, 0.004, 2000))
        trial_sharpes.append(vstats.sharpe_per_interval(r))
        if best is None or trial_sharpes[-1] > best[0]:
            best = (trial_sharpes[-1], r)
    equity = 10_000 * (1 + best[1]).cumprod()
    mdd = float((equity / equity.cummax() - 1.0).min())
    out = vg.run_g4(best[1], n_trials=20, trial_sharpes=trial_sharpes,
                    actual_max_drawdown=mdd, cfg=cfg, seed=7)
    assert out["checks"]["deflated_sharpe"]["passed"] is False
    assert out["checks"]["deflated_sharpe"]["probability"] < 0.95
    assert out["passed"] is False


def test_g4_strong_trend_passes(cfg):
    rng = np.random.default_rng(6)
    r = pd.Series(0.0012 + rng.normal(0.0, 0.0008, 5000))
    equity = 10_000 * (1 + r).cumprod()
    mdd = float((equity / equity.cummax() - 1.0).min())
    out = vg.run_g4(r, n_trials=1, trial_sharpes=None,
                    actual_max_drawdown=mdd, cfg=cfg, seed=7)
    assert out["checks"]["deflated_sharpe"]["probability"] > 0.95
    assert out["passed"] is True


def test_g4_flat_returns_fail_closed(cfg):
    r = pd.Series(0.0, index=range(50))
    out = vg.run_g4(r, n_trials=1, trial_sharpes=None,
                    actual_max_drawdown=0.0, cfg=cfg, seed=7)
    assert out["passed"] is False
    assert out["checks"]["deflated_sharpe"]["probability"] == 0.0


def test_g4_accepts_block_len_for_mc_drawdown(cfg):
    """T21c/H6：G4 的回撤蒙特卡洛按块自助（块长 = 一个交易日的 bar 数）。"""
    rng = np.random.default_rng(6)
    r = pd.Series(0.0012 + rng.normal(0.0, 0.0008, 3000))
    equity = 10_000 * (1 + r).cumprod()
    mdd = float((equity / equity.cummax() - 1.0).min())
    out = vg.run_g4(r, n_trials=1, trial_sharpes=None,
                    actual_max_drawdown=mdd, cfg=cfg, seed=7, block_len=24)
    assert out["passed"] is True
    assert out["checks"]["mc_drawdown"]["block_len"] == 24


# ---------------------------------------------------------------- G5（T21a/C3 重写）

class _Card:
    """run_g5 的卡替身：只需要 id 与 timeframe。"""

    def __init__(self, card_id, timeframe="H1"):
        self.id = card_id
        self.timeframe = timeframe


def g3_ledger(tmp_path, card_id="H777"):
    """带一条 G3 通过记录的台账（未过 G3 的卡不允许跑 G5）。"""
    led = Ledger(tmp_path / "l.sqlite")
    rid = led.record_run(
        card_id=card_id, card_version=1, strategy="s:Donchian", params={},
        symbol="XAUUSD", timeframe="H1", data_first="2024-01-01",
        data_last="2026-06-30", data_sha256="a" * 64,
        engine_version="A-1.3", cost_model_version="zero-v0",
        gates_hash="", metrics={"sharpe": 1.0}, actor="test")
    led.record_gate(rid, "G3", True, {})
    return led


def ref_returns(n=30_000, drift=7.0e-5, vol=0.004, seed=5):
    """参照收益（G3 拼接的样本外收益替身）：实测年化夏普约 2（bpy=6000），
    对 11,250 根窗口块自助后 p5≈0.77、p95≈3.3（与计划的示例口径一致）。"""
    rng = np.random.default_rng(seed)
    return pd.Series(drift + rng.normal(0.0, vol, n))


REF = ref_returns()
WINDOW = 11_250        # 锁箱长度（bar 数）：H1 ≈ 22.5 个月
BPY = 6_000.0          # 与 lockbox_result 的年化口径一致


def lb_metrics(sharpe, *, expectancy=5.0, trades=40, bars=WINDOW):
    return {"expectancy": expectancy, "sharpe": sharpe,
            "trade_count": trades, "bar_count": bars,
            "annualization_bars_per_year": BPY}


@pytest.fixture(scope="module", name="g5_cfg")
def g5_cfg_fixture():
    return vg.load_gates()[0]


@pytest.fixture(scope="module")
def ref_percentiles(g5_cfg):
    """与 run_g5 同参数预计算参照分布分位（用例前提自检）。"""
    from mt5quant.validation import stats as vs
    boot = vs.block_bootstrap_sharpes(
        REF, window_len=WINDOW, block_len=24,
        sims=g5_cfg.lockbox_bootstrap_sims, seed=vs.rng_seed("H777"),
        bars_per_year=BPY)
    p5 = float(np.percentile(boot, g5_cfg.lockbox_sharpe_min_percentile))
    p95 = float(np.percentile(boot, g5_cfg.lockbox_warn_percentile))
    return p5, p95


def run_g5(led, sharpe, *, trades=40, expected=40.0, expectancy=5.0,
           approved_by="user", cfg=None, card_id="H777"):
    return vg.run_g5(
        led, _Card(card_id), lockbox_result=lb_metrics(
            sharpe, expectancy=expectancy, trades=trades),
        reference_returns=REF, expected_trades=expected,
        approved_by=approved_by, cfg=cfg, bars_per_day=24)


def test_g5_reference_distribution_premise(ref_percentiles):
    """用例前提：参照分布 p5 ≈ 0.8（夏普 2.0 应过、0.0 应拒、5.0 应告警）。"""
    p5, p95 = ref_percentiles
    assert 0.0 < p5 < 1.5
    assert 2.0 < p95 < 4.5


def test_g5_advantage_maintained_passes(tmp_path, g5_cfg, ref_percentiles):
    """保持优势（锁箱夏普 2.0 ≥ 参照 p5≈0.8）→ 通过（旧逻辑会拒绝，C3）。"""
    with g3_ledger(tmp_path) as led:
        out = run_g5(led, 2.0, cfg=g5_cfg)
        assert out["passed"] is True
        assert "warning" not in out
        assert led.lockbox_used("H777") is True
        assert led.lockbox_status("H777") == "done"
        row = led.conn.execute("SELECT * FROM lockbox_uses").fetchone()
        assert row["approved_by"] == "user"
        assert json.loads(row["result_json"])["passed"] is True


def test_g5_degenerated_to_random_fails(tmp_path, g5_cfg):
    """退化为随机（夏普 0.0 < 参照 p5）→ 不通过（旧逻辑会放行，C3）。"""
    with g3_ledger(tmp_path) as led:
        out = run_g5(led, 0.0, cfg=g5_cfg)
        assert out["checks"]["sharpe_vs_reference"]["passed"] is False
        assert out["passed"] is False
        assert led.lockbox_used("H777") is True      # 看过一眼即消耗


def test_g5_suspiciously_good_passes_with_warning(tmp_path, g5_cfg):
    """夏普 5.0 > 参照 p95：通过但带告警（复核数据与前视），不判失败。"""
    with g3_ledger(tmp_path) as led:
        out = run_g5(led, 5.0, cfg=g5_cfg)
        assert out["passed"] is True
        assert "复核" in out["warning"]
        assert out["checks"]["sharpe_vs_reference"]["passed"] is True


def test_g5_negative_expectancy_fails(tmp_path, g5_cfg):
    with g3_ledger(tmp_path) as led:
        out = run_g5(led, 2.0, expectancy=-1.0, cfg=g5_cfg)
        assert out["checks"]["expectation_positive"]["passed"] is False
        assert out["passed"] is False


def test_g5_min_trades_ratio(tmp_path, g5_cfg):
    """交易数 < 预期 × 50% → 不通过（20 笔 < 0.5×50）。"""
    with g3_ledger(tmp_path) as led:
        out = run_g5(led, 2.0, trades=20, expected=50.0, cfg=g5_cfg)
        check = out["checks"]["trade_count"]
        assert check["passed"] is False
        assert check["min_ratio"] == 0.50


def test_g5_requires_approval(tmp_path, g5_cfg):
    with g3_ledger(tmp_path) as led:
        with pytest.raises(vg.GateApprovalError, match="approve"):
            run_g5(led, 2.0, approved_by="", cfg=g5_cfg)
        with pytest.raises(vg.GateApprovalError, match="approve"):
            vg.begin_lockbox(led, _Card("H777"), approved_by="")
        assert led.lockbox_used("H777") is False   # 未批准不烧一次性


def test_g5_refuses_without_g3(tmp_path, g5_cfg):
    """未过 G3 的卡不允许跑 G5（参照分布来自 G3 的样本外收益）。"""
    with Ledger(tmp_path / "l.sqlite") as led:
        with pytest.raises(vg.LockboxPrerequisiteError, match="G3"):
            run_g5(led, 2.0, cfg=g5_cfg)
        with pytest.raises(vg.LockboxPrerequisiteError, match="G3"):
            vg.begin_lockbox(led, _Card("H777"), approved_by="user")
        assert led.lockbox_used("H777") is False   # 前置不满足：不烧一次性


def test_g5_missing_reference_returns_is_error(tmp_path, g5_cfg):
    with g3_ledger(tmp_path) as led:
        with pytest.raises(ValueError, match="样本外"):
            vg.run_g5(led, _Card("H777"), lockbox_result=lb_metrics(2.0),
                      reference_returns=None, expected_trades=40.0,
                      approved_by="user", cfg=g5_cfg, bars_per_day=24)


def test_g5_second_use_refused(tmp_path, g5_cfg):
    """锁箱一次性——第二次请求直接拒绝。"""
    with g3_ledger(tmp_path) as led:
        run_g5(led, 2.0, cfg=g5_cfg)
        with pytest.raises(vg.LockboxAlreadyUsedError, match="H777"):
            run_g5(led, 2.0, cfg=g5_cfg)
        n = led.conn.execute("SELECT COUNT(*) n FROM lockbox_uses").fetchone()["n"]
        assert n == 1
        assert led.lockbox_status("H777") == "done"


def test_g5_started_row_blocks_rerun(tmp_path, g5_cfg):
    """计划失败测试（T21a）：started 后崩溃不能重跑。

    begin_lockbox 在读锁箱数据之前写 started；此后该卡锁箱视为已用，
    崩溃重跑在 begin_lockbox 就被拒（UNIQUE + lockbox_used 双保险）。
    """
    with g3_ledger(tmp_path) as led:
        vg.begin_lockbox(led, _Card("H777"), approved_by="user")
        assert led.lockbox_used("H777") is True
        assert led.lockbox_status("H777") == "started"
        with pytest.raises(vg.LockboxAlreadyUsedError, match="H777"):
            vg.begin_lockbox(led, _Card("H777"), approved_by="user")


def test_g5_begin_then_run_finishes_started_row(tmp_path, g5_cfg):
    """编排协议：begin_lockbox（started）→ 评估 → run_g5 写终态（done）。"""
    with g3_ledger(tmp_path) as led:
        vg.begin_lockbox(led, _Card("H777"), approved_by="user")
        out = run_g5(led, 2.0, cfg=g5_cfg)
        assert out["passed"] is True
        assert led.lockbox_status("H777") == "done"
        row = led.conn.execute("SELECT * FROM lockbox_uses").fetchone()
        assert json.loads(row["result_json"])["passed"] is True


# ------------------------------------------------- 锁箱运行窗（预热段）

def lockbox_root(tmp_path, n=2000, cutoff_bar=1800):
    """带 lockbox.json 的数据根：cutoff 在第 cutoff_bar 根 bar 的时刻。"""
    root = tmp_path / "data"
    sdir = root / "bars" / "XAUUSD" / "H1"
    sdir.mkdir(parents=True)
    # 确定性上行 ramps（每根 +0.75~0.85，加微小正弦）：B1 必然持续突破
    closes = np.array([100.0 + 0.8 * i + 0.05 * np.sin(i) for i in range(n)])
    opens = np.concatenate(([closes[0]], closes[:-1]))
    times = pd.date_range("2025-01-01", periods=n, freq="1h")
    pd.DataFrame({
        "time": times, "open": opens,
        "high": np.maximum(opens, closes) + 0.2,
        "low": np.minimum(opens, closes) - 0.2,
        "close": closes,
        "tick_volume": 1, "spread": 1, "real_volume": 0,
    }).to_parquet(sdir / "data.parquet")
    cutoff = times[cutoff_bar]
    (root / "lockbox.json").write_text(json.dumps({
        "version": 2,
        "global_cutoff": cutoff.isoformat(),
        "created_utc": "2026-06-01T00:00:00",
        "note": "test"}), encoding="utf-8")
    bars = pd.read_parquet(sdir / "data.parquet")
    return root, bars, cutoff


def test_load_lockbox_window_starts_lookback_before_cutoff(tmp_path):
    """锁箱运行从 cutoff 前 lookback_bars 根起步（C3：无预热段信号不一致）。"""
    from mt5quant.strategies.library.b1_donchian_breakout import DonchianBreakout
    root, bars, cutoff = lockbox_root(tmp_path, n=2000, cutoff_bar=1800)
    lookback = DonchianBreakout.required_data.lookback_bars
    seg, got_cutoff = vg.load_lockbox_window(root, "XAUUSD", "H1",
                                             lookback_bars=lookback)
    assert got_cutoff == cutoff
    assert len(seg) == 200 + lookback            # 200 根锁箱 + 预热
    assert seg["time"].iloc[0] == bars["time"].iloc[1800 - lookback]
    assert seg["time"].iloc[lookback] == cutoff  # 预热段后第一根 = cutoff

    # B1 构造：带预热段重算的信号（cutoff 之后）与全史一致；
    # 没有预热段（直接从 cutoff 切）时信号与全史不同。
    params = {"entry_days": 10, "exit_days": 2}
    strat = DonchianBreakout()
    full = strat.generate_signals(bars, params).to_numpy()
    warm = strat.generate_signals(seg, params).to_numpy()
    no_warm = strat.generate_signals(bars.iloc[1800:], params).to_numpy()
    assert np.array_equal(warm[-200:], full[1800:])
    assert not np.array_equal(no_warm, full[1800:])


def test_load_lockbox_window_rejects_bad_lookback(tmp_path):
    root, _, _ = lockbox_root(tmp_path)
    with pytest.raises(ValueError):
        vg.load_lockbox_window(root, "XAUUSD", "H1", lookback_bars=-1)


# ---------------------------------------------------------------- 报告渲染

def test_render_gate_report_markdown():
    md = vg.render_gate_report(
        "H777", {"k": 2},
        {"G0": {"passed": True, "checks": {"truncation_violations": []}},
         "G1": {"passed": False, "checks": {"trade_count": {"passed": False,
                                                            "trades": 12}}}})
    assert "H777" in md and "G0" in md and "G1" in md
    assert "✓" in md and "✗" in md


# ---------------------------------------------------------------- bt gate CLI

def h1_bars(n=6):
    times = pd.date_range("2026-01-05 10:00", periods=n, freq="h")
    return pd.DataFrame({
        "time": times, "open": 100.0, "high": 100.5, "low": 99.5,
        "close": [100.0, 100.0, 101.0, 102.0, 103.0, 104.0][:n],
        "tick_volume": 10, "spread": 5, "real_volume": 0,
    })


def make_data_root(tmp_path):
    root = tmp_path / "data"
    sdir = root / "bars" / "XAUUSD" / "H1"
    sdir.mkdir(parents=True)
    h1_bars().to_parquet(sdir / "data.parquet")
    specs = root / "specs"
    specs.mkdir(parents=True)
    (specs / "latest.json").write_text(
        json.dumps({"XAUUSD": {"contract_size": 100.0}}), encoding="utf-8")
    return root


CARD_BODY = """\
id = "H700"
version = 1
symbols = ["XAUUSD"]
timeframe = "H1"
session = "全时段"
phenomenon = "黄金长期上行"
mechanism = "风险溢价"
should_fail_when = "长期通缩"
falsified_if = "样本内净利为负"
graveyard_checked = []

[rules]
entry = "第一根可交易 bar 买入"
exit = "数据末尾"
stop = "无"

[params]

[expected]
trades_per_year = "1"
win_rate = "50%+"
payoff_ratio = "不适用"
"""


def write_card(tmp_path, body=CARD_BODY, name="H700.toml"):
    d = tmp_path / "cards"
    d.mkdir(exist_ok=True)
    (d / name).write_text(body, encoding="utf-8")
    return d


def patch_card_git(monkeypatch, mod, *, committed=PAST, clean=True):
    """注入卡文件的 git 状态（T21d：committed_at + worktree_clean 两问）。"""
    monkeypatch.setattr(mod.cards, "committed_at",
                        lambda path, runner=None: committed)
    monkeypatch.setattr(mod.cards, "worktree_clean",
                        lambda path, runner=None: clean)


def gate_argv(root, cards_dir, ledger, gates, *extra):
    return ["gate", "--card", "H700", "--cards-dir", str(cards_dir),
            "--data-root", str(root), "--ledger", str(ledger),
            "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
            "--costs", "zero", "--gates", gates, *extra]


def test_gate_cli_refuses_tampered_frozen_sha(tmp_path, monkeypatch, capsys):
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)
    ledger = tmp_path / "l.sqlite"
    patch_card_git(monkeypatch, mod)
    monkeypatch.setattr("mt5quant.validation.gates.FROZEN_GATES_SHA256",
                        "0" * 64)
    rc = mod.main(gate_argv(root, cards_dir, ledger, "G0,G1"))
    assert rc == 1
    assert "sha256" in capsys.readouterr().out
    with Ledger(ledger) as led:
        assert led.stats("H700")["total_runs"] == 0   # 拒绝运行：无 runs


def test_gate_cli_g0_passes_and_writes_gates(tmp_path, monkeypatch, capsys):
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)
    ledger = tmp_path / "l.sqlite"
    patch_card_git(monkeypatch, mod)
    # T21e 起 --params 缺省 = auto（需先有 screen 运行）；这里显式给参数
    rc = mod.main(gate_argv(root, cards_dir, ledger, "G0",
                            "--params", "{}"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "G0" in out
    with Ledger(ledger) as led:
        rows = led.conn.execute(
            "SELECT r.run_id, r.gates_hash, g.gate, g.passed FROM runs r"
            " JOIN gates g ON g.run_id = r.run_id").fetchall()
        assert len(rows) == 1
        assert rows[0]["gate"] == "G0" and rows[0]["passed"] == 1
        assert len(rows[0]["gates_hash"]) == 64    # gates.toml sha 进运行


def test_gate_cli_g1_failure_exits_nonzero(tmp_path, monkeypatch, capsys):
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)
    ledger = tmp_path / "l.sqlite"
    patch_card_git(monkeypatch, mod)
    rc = mod.main(gate_argv(root, cards_dir, ledger, "G1",
                            "--params", "{}"))
    assert rc == 1                                # 1 笔交易 < H1 门槛 150
    with Ledger(ledger) as led:
        rows = led.conn.execute("SELECT gate, passed FROM gates").fetchall()
        assert rows[0]["gate"] == "G1" and rows[0]["passed"] == 0


def test_gate_cli_g5_requires_approve_flag(tmp_path, monkeypatch, capsys):
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)
    ledger = tmp_path / "l.sqlite"
    patch_card_git(monkeypatch, mod)
    rc = mod.main(gate_argv(root, cards_dir, ledger, "G5"))
    assert rc == 1
    assert "--approve" in capsys.readouterr().out
    with Ledger(ledger) as led:
        assert led.conn.execute(
            "SELECT COUNT(*) n FROM lockbox_uses").fetchone()["n"] == 0
        assert led.stats("H700")["total_runs"] == 0   # 预检拒绝：未跑任何运行


# ------------------------------------------------------- T21e 接线（P7/M5）

DONCHIAN = ("mt5quant.strategies.library.b1_donchian_breakout"
            ":DonchianBreakout")
BUY_HOLD = "mt5quant.strategies.library.buy_hold:BuyHold"

CARD_H701 = """\
id = "H701"
version = 1
strategy = "mt5quant.strategies.library.b1_donchian_breakout:DonchianBreakout"
symbols = ["XAUUSD"]
timeframe = "H1"
session = "全时段"
phenomenon = "趋势延续"
mechanism = "突破后动量"
should_fail_when = "区间市"
falsified_if = "样本内净利为负"
graveyard_checked = []

[rules]
entry = "N 日通道突破"
exit = "短通道反向"
stop = "ATR 止损（引擎）"

[params]
entry_days = [10]
exit_days = [2, 4]

[expected]
trades_per_year = "20"
win_rate = "40%+"
payoff_ratio = "2+"
"""


def ramp_bars(n=300, step=0.5, start="2026-01-05"):
    """确定性上行 H1 bars（唐奇安必然持续突破）。"""
    closes = [100.0 + step * i for i in range(n)]
    times = pd.date_range(start, periods=n, freq="1h")
    opens = [closes[0]] + closes[:-1]
    return pd.DataFrame({
        "time": times, "open": opens,
        "high": [max(o, c) + 0.2 for o, c in zip(opens, closes)],
        "low": [min(o, c) - 0.2 for o, c in zip(opens, closes)],
        "close": closes, "tick_volume": 1, "spread": 1, "real_volume": 0,
    })


def ramp_data_root(tmp_path, n=300, *, lockbox_cutoff_bar=None):
    root = tmp_path / "data"
    sdir = root / "bars" / "XAUUSD" / "H1"
    sdir.mkdir(parents=True)
    bars = ramp_bars(n)
    bars.to_parquet(sdir / "data.parquet")
    specs = root / "specs"
    specs.mkdir(parents=True)
    (specs / "latest.json").write_text(
        json.dumps({"XAUUSD": {"contract_size": 100.0}}), encoding="utf-8")
    cutoff = None
    if lockbox_cutoff_bar is not None:
        cutoff = bars["time"].iloc[lockbox_cutoff_bar]
        (root / "lockbox.json").write_text(json.dumps({
            "version": 2, "global_cutoff": cutoff.isoformat(),
            "created_utc": "2026-06-01T00:00:00", "note": "test"}),
            encoding="utf-8")
    return root, bars, cutoff


def test_screen_and_gate_record_same_gates_hash_crlf(tmp_path, monkeypatch):
    """P7 计划失败测试（端到端）：CRLF 检出的 gates.toml 下，
    `bt screen` 与 `bt gate` 记进 runs 的 gates_hash 是同一个值（LF 归一）。"""
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)
    cfg_root = tmp_path / "cfgroot"
    (cfg_root / "config").mkdir(parents=True)
    crlf = cfg_root / "config" / "gates.toml"
    crlf.write_bytes(vg.DEFAULT_GATES_TOML.read_bytes().replace(b"\n", b"\r\n"))
    assert b"\r\n" in crlf.read_bytes()           # 夹具自检：确为 CRLF
    monkeypatch.setattr(mod, "REPO_ROOT", cfg_root)
    monkeypatch.setattr(vg, "DEFAULT_GATES_TOML", crlf)
    patch_card_git(monkeypatch, mod)
    led_scr, led_gate = tmp_path / "scr.sqlite", tmp_path / "gt.sqlite"

    rc_s = mod.main(["screen", "--card", "H700", "--cards-dir", str(cards_dir),
                     "--data-root", str(root), "--ledger", str(led_scr),
                     "--strategy", BUY_HOLD, "--costs", "zero"])
    rc_g = mod.main(gate_argv(root, cards_dir, led_gate, "G0",
                              "--params", "{}"))
    assert (rc_s, rc_g) == (0, 0)
    with Ledger(led_scr) as a, Ledger(led_gate) as b:
        ha = a.conn.execute("SELECT gates_hash FROM runs").fetchone()[0]
        hb = b.conn.execute("SELECT gates_hash FROM runs").fetchone()[0]
    assert ha == hb == vg.FROZEN_GATES_SHA256


def record_screen_run(led, **kw):
    """按台账口径记一行 screen 运行（M5 选参测试用）。"""
    base = dict(card_id="H701", card_version=1, strategy=DONCHIAN,
                symbol="XAUUSD", timeframe="H1", data_first="", data_last="",
                data_sha256="a" * 64, engine_version="A-1.3",
                cost_model_version="zero-v0", gates_hash="",
                metrics=None, actor="bt-screen")
    base.update(kw)
    return led.record_run(**base)


def test_gate_params_auto_selects_max_net_profit_run(tmp_path, monkeypatch,
                                                     capsys):
    """M5 计划失败测试：--params 缺省（auto）按卡 selection_rule 从同
    data_sha256+cost_model_version 的 screen 运行选净利最高组合，
    并把候选数与选中 run_id 写进关卡 details 与报告。"""
    mod = load_bt()
    root, bars, _ = ramp_data_root(tmp_path, n=1800)
    cards_dir = write_card(tmp_path, CARD_H701, name="H701.toml")
    ledger = tmp_path / "l.sqlite"
    patch_card_git(monkeypatch, mod)
    from mt5quant.backtest.report import data_sha256
    dsha = data_sha256(bars)
    with Ledger(ledger) as led:
        r1 = record_screen_run(led, params={"entry_days": 10, "exit_days": 2},
                               data_sha256=dsha,
                               metrics={"net_profit": 10.0, "sharpe": 0.9})
        r2 = record_screen_run(led, params={"entry_days": 10, "exit_days": 4},
                               data_sha256=dsha,
                               metrics={"net_profit": 50.0, "sharpe": 0.4})
        record_screen_run(led, params={"entry_days": 10, "exit_days": 2},
                          data_sha256="f" * 64,     # 数据哈希不同 → 非候选
                          metrics={"net_profit": 999.0})
        n_runs_before = led.stats("H701")["total_runs"]

    rc = mod.main(["gate", "--card", "H701", "--cards-dir", str(cards_dir),
                   "--data-root", str(root), "--ledger", str(ledger),
                   "--strategy", DONCHIAN, "--costs", "zero", "--gates", "G0"])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"run#{r2}" in out and "候选" in out
    with Ledger(ledger) as led:
        assert led.stats("H701")["total_runs"] == n_runs_before + 1
        base = led.conn.execute(
            "SELECT params_json FROM runs ORDER BY run_id DESC").fetchone()
        assert json.loads(base["params_json"]) == {"entry_days": 10,
                                                   "exit_days": 4}
        det = json.loads(led.conn.execute(
            "SELECT details_json FROM gates WHERE gate='G0'"
        ).fetchone()["details_json"])
        sel = det["params_selection"]
        assert sel["selection"] == "auto"
        assert sel["rule"] == "max_net_profit"
        assert sel["candidates"] == 2              # 不同数据哈希的不算
        assert sel["selected_run_id"] == r2
        assert sel["params"] == {"entry_days": 10, "exit_days": 4}


def test_gate_params_manual_selection_recorded(tmp_path, monkeypatch):
    """M5：显式 --params → selection="manual"，同样写进 gates details。"""
    mod = load_bt()
    root, bars, _ = ramp_data_root(tmp_path, n=1800)
    cards_dir = write_card(tmp_path, CARD_H701, name="H701.toml")
    ledger = tmp_path / "l.sqlite"
    patch_card_git(monkeypatch, mod)
    from mt5quant.backtest.report import data_sha256
    with Ledger(ledger) as led:
        record_screen_run(led, params={"entry_days": 10, "exit_days": 2},
                          data_sha256=data_sha256(bars),
                          metrics={"net_profit": 10.0})
    rc = mod.main(["gate", "--card", "H701", "--cards-dir", str(cards_dir),
                   "--data-root", str(root), "--ledger", str(ledger),
                   "--strategy", DONCHIAN, "--costs", "zero", "--gates", "G0",
                   "--params", '{"entry_days": 10, "exit_days": 2}'])
    assert rc == 0
    with Ledger(ledger) as led:
        det = json.loads(led.conn.execute(
            "SELECT details_json FROM gates WHERE gate='G0'"
        ).fetchone()["details_json"])
        assert det["params_selection"]["selection"] == "manual"


def test_gate_params_auto_unknown_selection_rule_rejected(tmp_path,
                                                          monkeypatch,
                                                          capsys):
    """M5：卡上 selection_rule 非法 → 拒绝（不产生任何运行）。"""
    mod = load_bt()
    root, _, _ = ramp_data_root(tmp_path, n=1800)
    body = CARD_H701.replace("graveyard_checked = []",
                             'graveyard_checked = []\n'
                             'selection_rule = "max_moon_phase"')
    cards_dir = write_card(tmp_path, body, name="H701.toml")
    ledger = tmp_path / "l.sqlite"
    patch_card_git(monkeypatch, mod)
    rc = mod.main(["gate", "--card", "H701", "--cards-dir", str(cards_dir),
                   "--data-root", str(root), "--ledger", str(ledger),
                   "--strategy", DONCHIAN, "--costs", "zero", "--gates", "G0"])
    assert rc == 1
    assert "selection_rule" in capsys.readouterr().out
    with Ledger(ledger) as led:
        assert led.stats("H701")["total_runs"] == 0


def test_gate_params_auto_without_candidates_rejected(tmp_path, monkeypatch,
                                                      capsys):
    """M5：auto 找不到候选（没跑过 screen）→ 拒绝并提示先跑 screen。"""
    mod = load_bt()
    root, _, _ = ramp_data_root(tmp_path, n=1800)
    cards_dir = write_card(tmp_path, CARD_H701, name="H701.toml")
    ledger = tmp_path / "l.sqlite"
    patch_card_git(monkeypatch, mod)
    rc = mod.main(["gate", "--card", "H701", "--cards-dir", str(cards_dir),
                   "--data-root", str(root), "--ledger", str(ledger),
                   "--strategy", DONCHIAN, "--costs", "zero", "--gates", "G0"])
    assert rc == 1
    assert "screen" in capsys.readouterr().out
    with Ledger(ledger) as led:
        assert led.stats("H701")["total_runs"] == 0


# ------------------------------------------------- G5 编排（T21a 协议接线）

def h001_card(mod):
    return mod.cards.load_card(REPO_ROOT / "research" / "cards" / "H001.toml")


def buy_hold_spec(mod):
    from mt5quant.strategies import BuyHold
    return mod.BacktestSpec(strategy=BuyHold(), symbol="XAUUSD",
                            timeframe="H1", contract_size=1.0)


def g5_kwargs(mod, bars, *, data_root="data"):
    spec = buy_hold_spec(mod)
    return dict(strategy=spec.strategy, spec=spec, params={}, bars=bars,
                costs=mod.ZeroCosts(), session_policy=None,
                data_root=data_root, approved_by="user",
                cfg=vg.load_gates()[0],
                base_summary={"trade_count": 1})


def test_g5_orchestrate_never_reads_lockbox_before_begin(tmp_path,
                                                         monkeypatch):
    """T21e 计划失败测试（函数级）：未过 G3（begin_lockbox 会拒）时
    绝不读锁箱数据，且一次性机会未消耗。"""
    mod = load_bt()
    calls = []
    monkeypatch.setattr(mod.vgate, "load_lockbox_window",
                        lambda *a, **k: calls.append(1))
    bars = ramp_bars(40)
    led = Ledger(tmp_path / "l.sqlite")            # 无任何 G3 记录
    with pytest.raises(ValueError, match="G3"):
        mod._g5_orchestrate(led, h001_card(mod), **g5_kwargs(mod, bars))
    assert calls == []                             # 锁箱未被读
    assert led.lockbox_used("H001") is False       # 一次性未消耗


def test_g5_orchestrate_reference_failure_does_not_consume(tmp_path):
    """G3 记录没有 segments（重建参照失败）→ 拒绝且不消耗一次性。"""
    mod = load_bt()
    bars = ramp_bars(40)
    with g3_ledger(tmp_path, card_id="H001") as led:   # G3 通过但 details={}
        with pytest.raises(ValueError, match="G3"):
            mod._g5_orchestrate(led, h001_card(mod), **g5_kwargs(mod, bars))
        assert led.lockbox_used("H001") is False


def test_g5_orchestrate_protocol_order(tmp_path, monkeypatch):
    """T21e（函数级）：编排顺序 = begin_lockbox（写 started）→ 读锁箱窗 →
    回测 → run_g5（写终态）；读锁箱数据之前必有 started 行。"""
    mod = load_bt()
    from mt5quant.backtest.report import data_sha256
    calls = []
    bars = ramp_bars(40)
    ledger_path = tmp_path / "l.sqlite"
    with Ledger(ledger_path) as led:
        rid = led.record_run(
            card_id="H001", card_version=1, strategy=BUY_HOLD, params={},
            symbol="XAUUSD", timeframe="H1", data_first="", data_last="",
            data_sha256=data_sha256(bars), engine_version="A-1.3",
            cost_model_version="zero-v0", gates_hash="",
            metrics={"sharpe": 1.0}, actor="test")
        led.record_gate(rid, "G3", True, {
            "segments": [{"train_range": [0, 20], "test_range": [20, 30],
                          "params": {}},
                         {"train_range": [20, 30], "test_range": [30, 40],
                          "params": {}}]})
    real_begin = mod.vgate.begin_lockbox

    def _begin(led_, card, *, approved_by):
        calls.append("begin")
        real_begin(led_, card, approved_by=approved_by)

    window = ramp_bars(8)
    cutoff = window["time"].iloc[4]
    monkeypatch.setattr(mod.vgate, "begin_lockbox", _begin)
    monkeypatch.setattr(mod.vgate, "load_lockbox_window",
                        lambda *a, **k: calls.append("window")
                        or (window, cutoff))

    class _FakeResult:
        trades = pd.DataFrame({"entry_time": [window["time"].iloc[5]],
                               "net": [5.0]})
        equity = pd.Series([100.0, 101.0, 102.0, 103.0, 104.0, 105.0,
                            106.0, 107.0])

    monkeypatch.setattr(mod, "run_backtest",
                        lambda *a, **k: calls.append("backtest")
                        or _FakeResult())
    monkeypatch.setattr(mod.vgate, "run_g5",
                        lambda *a, **k: calls.append("run_g5")
                        or {"passed": True})
    with Ledger(ledger_path) as led:
        out = mod._g5_orchestrate(led, h001_card(mod),
                                  **g5_kwargs(mod, bars))
        assert out == {"passed": True}
        assert calls == ["begin", "window", "backtest", "run_g5"]
        assert led.lockbox_status("H001") == "started"   # run_g5 被替身顶替


def test_gate_cli_g5_orchestration_started_then_done(tmp_path, monkeypatch,
                                                     capsys):
    """T21e 端到端：gate --gates G5 --approve 走新协议——锁箱窗带预热、
    交易/收益只计 cutoff 后，started 行最终写成 done。"""
    mod = load_bt()
    root, bars, cutoff = ramp_data_root(tmp_path, n=260,
                                        lockbox_cutoff_bar=200)
    cards_dir = write_card(tmp_path)
    ledger = tmp_path / "l.sqlite"
    patch_card_git(monkeypatch, mod)
    from mt5quant.backtest.report import data_sha256
    research = bars[bars["time"] < cutoff].reset_index(drop=True)
    with Ledger(ledger) as led:
        rid = led.record_run(
            card_id="H700", card_version=1, strategy=BUY_HOLD, params={},
            symbol="XAUUSD", timeframe="H1",
            data_first=str(research["time"].iloc[0]),
            data_last=str(research["time"].iloc[-1]),
            data_sha256=data_sha256(research), engine_version="A-1.3",
            cost_model_version="zero-v0", gates_hash="",
            metrics={"sharpe": 1.0, "trade_count": 1}, actor="test")
        led.record_gate(rid, "G3", True, {
            "segments": [{"train_range": [0, 100], "test_range": [100, 150],
                          "params": {}},
                         {"train_range": [100, 150], "test_range": [150, 200],
                          "params": {}}]})
    rc = mod.main(gate_argv(root, cards_dir, ledger, "G5", "--approve",
                            "--approved-by", "user", "--params", "{}"))
    out = capsys.readouterr().out
    assert "G5" in out
    with Ledger(ledger) as led:
        row = led.conn.execute("SELECT * FROM lockbox_uses").fetchone()
        assert row["status"] == "done"
        assert row["approved_by"] == "user"
        result = json.loads(row["result_json"])
        assert isinstance(result["passed"], bool)
        assert result["metrics"]["bar_count"] == 60     # 只计 cutoff 后
        g5 = led.conn.execute(
            "SELECT g.passed, g.details_json FROM gates g"
            " JOIN runs r ON r.run_id = g.run_id WHERE g.gate='G5'").fetchone()
        assert g5 is not None
        det = json.loads(g5["details_json"])
        assert det["params_selection"]["selection"] == "manual"
        assert (1 if g5["passed"] else 0) == (1 if result["passed"] else 0)
