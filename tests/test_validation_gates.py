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
    assert cfg.walkforward_min_oos_ratio == 0.50
    assert cfg.dsr_min_probability == 0.95


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


# ---------------------------------------------------------------- G5

MC_SHARPES = [float(x) for x in np.linspace(-2.0, 2.0, 101)]   # p5≈−1.8, p95≈1.8


def test_g5_requires_approval(tmp_path):
    with Ledger(tmp_path / "l.sqlite") as led:
        with pytest.raises(vg.GateApprovalError, match="approve"):
            vg.run_g5(led, "H777", approved_by=None,
                      metrics={"expectancy": 5.0, "sharpe": 1.0},
                      mc_sharpes=MC_SHARPES, cfg=cfg)
        assert led.lockbox_used("H777") is False   # 未批准不烧一次性


def test_g5_first_use_passes_and_records(tmp_path, cfg):
    with Ledger(tmp_path / "l.sqlite") as led:
        out = vg.run_g5(led, "H777", approved_by="user",
                        metrics={"expectancy": 5.0, "sharpe": 1.0},
                        mc_sharpes=MC_SHARPES, cfg=cfg)
        assert out["passed"] is True
        assert led.lockbox_used("H777") is True
        row = led.conn.execute("SELECT * FROM lockbox_uses").fetchone()
        assert row["approved_by"] == "user"
        assert json.loads(row["result_json"])["passed"] is True


def test_g5_second_use_refused(tmp_path, cfg):
    """计划失败测试 4：锁箱一次性——第二次请求直接拒绝。"""
    with Ledger(tmp_path / "l.sqlite") as led:
        vg.run_g5(led, "H777", approved_by="user",
                  metrics={"expectancy": 5.0, "sharpe": 1.0},
                  mc_sharpes=MC_SHARPES, cfg=cfg)
        with pytest.raises(vg.LockboxAlreadyUsedError, match="H777"):
            vg.run_g5(led, "H777", approved_by="user",
                      metrics={"expectancy": 5.0, "sharpe": 1.0},
                      mc_sharpes=MC_SHARPES, cfg=cfg)
        n = led.conn.execute("SELECT COUNT(*) n FROM lockbox_uses").fetchone()["n"]
        assert n == 1


def test_g5_failed_metrics_still_burn_the_shot(tmp_path, cfg):
    with Ledger(tmp_path / "l.sqlite") as led:
        out = vg.run_g5(led, "H888", approved_by="user",
                        metrics={"expectancy": -5.0, "sharpe": 5.0},
                        mc_sharpes=MC_SHARPES, cfg=cfg)
        assert out["passed"] is False          # 期望为负 + 夏普越出区间
        assert led.lockbox_used("H888") is True


def test_g5_sharpe_outside_mc_interval_fails(tmp_path, cfg):
    with Ledger(tmp_path / "l.sqlite") as led:
        out = vg.run_g5(led, "H889", approved_by="user",
                        metrics={"expectancy": 5.0, "sharpe": 9.9},
                        mc_sharpes=MC_SHARPES, cfg=cfg)
        assert out["checks"]["mc_interval"]["passed"] is False


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


def write_card(tmp_path):
    d = tmp_path / "cards"
    d.mkdir(exist_ok=True)
    (d / "H700.toml").write_text(CARD_BODY, encoding="utf-8")
    return d


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
    monkeypatch.setattr(mod.cards, "committed_at",
                        lambda path, runner=None: PAST)
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
    monkeypatch.setattr(mod.cards, "committed_at",
                        lambda path, runner=None: PAST)
    rc = mod.main(gate_argv(root, cards_dir, ledger, "G0"))
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
    monkeypatch.setattr(mod.cards, "committed_at",
                        lambda path, runner=None: PAST)
    rc = mod.main(gate_argv(root, cards_dir, ledger, "G1"))
    assert rc == 1                                # 1 笔交易 < H1 门槛 150
    with Ledger(ledger) as led:
        rows = led.conn.execute("SELECT gate, passed FROM gates").fetchall()
        assert rows[0]["gate"] == "G1" and rows[0]["passed"] == 0


def test_gate_cli_g5_requires_approve_flag(tmp_path, monkeypatch, capsys):
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)
    ledger = tmp_path / "l.sqlite"
    monkeypatch.setattr(mod.cards, "committed_at",
                        lambda path, runner=None: PAST)
    rc = mod.main(gate_argv(root, cards_dir, ledger, "G5"))
    assert rc == 1
    assert "--approve" in capsys.readouterr().out
    with Ledger(ledger) as led:
        assert led.conn.execute(
            "SELECT COUNT(*) n FROM lockbox_uses").fetchone()["n"] == 0
        assert led.stats("H700")["total_runs"] == 0   # 预检拒绝：未跑任何运行
