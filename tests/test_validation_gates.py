"""关卡配置与 G1/G4/G5 + `bt gate` 命令行。

计划失败测试落点：
- G5 第二次请求直接拒绝（test_run_g5_second_use_refused）；
- 改动 config/gates.toml 后拒绝运行（test_tampered_gates_toml_refuses）。
"""
import dataclasses
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
    # 只替换 [g1] 节头（D34 冻结后文件头注释含 "[g1]–[g5]" 字样）
    assert "\n[g1]" in body
    p.write_text(body.replace("\n[g1]", "\nstray_key = 1\n\n[g1]", 1),
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
    """带一条当前通过的完整试验记录（G3+V3，H-02 起的 G5 前置）。"""
    led = Ledger(tmp_path / "l.sqlite")
    rid = led.record_run(
        card_id=card_id, card_version=1, strategy="s:Donchian", params={},
        symbol="XAUUSD", timeframe="H1", data_first="2024-01-01",
        data_last="2026-06-30", data_sha256="a" * 64,
        engine_version="A-1.3", cost_model_version="zero-v0",
        gates_hash="", metrics={"sharpe": 1.0}, actor="test")
    led.record_gate(rid, "G3", True, {})
    led.record_gate(rid, "V3", True, {})
    return led


def lockbox_trial(**over):
    """与 g3_ledger 登记的试验身份一致的 LockboxTrial（H-02 绑定）。"""
    kw = dict(params={}, data_sha256="a" * 64, cost_model_version="zero-v0",
              strategy_sha="legacy", gates_hash="")
    kw.update(over)
    return vg.LockboxTrial(**kw)


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
        vg.begin_lockbox(led, _Card("H777"), approved_by="user",
                         trial=lockbox_trial())
        assert led.lockbox_used("H777") is True
        assert led.lockbox_status("H777") == "started"
        with pytest.raises(vg.LockboxAlreadyUsedError, match="H777"):
            vg.begin_lockbox(led, _Card("H777"), approved_by="user",
                             trial=lockbox_trial())


def test_g5_begin_then_run_finishes_started_row(tmp_path, g5_cfg):
    """编排协议：begin_lockbox（started）→ 评估 → run_g5 写终态（done）。"""
    with g3_ledger(tmp_path) as led:
        vg.begin_lockbox(led, _Card("H777"), approved_by="user",
                         trial=lockbox_trial())
        out = run_g5(led, 2.0, cfg=g5_cfg)
        assert out["passed"] is True
        assert led.lockbox_status("H777") == "done"
        row = led.conn.execute("SELECT * FROM lockbox_uses").fetchone()
        assert json.loads(row["result_json"])["passed"] is True


# ------------------------------------------- H-02：G5 准入绑定当前通过试验

CUT = "2026-04-01T00:00:00"


def test_h02_begin_lockbox_requires_trial_binding(tmp_path, g5_cfg):
    """H-02 失败测试 1：不带 LockboxTrial 的 begin_lockbox 一律拒绝——
    该卡任意历史 G3 通过不再授权（历史成功参数/旧成本/旧代码不授权当前
    试验），且不产生 started、不扣预算。"""
    with g3_ledger(tmp_path, card_id="H880") as led:
        with pytest.raises(vg.LockboxPrerequisiteError, match="H-02"):
            vg.begin_lockbox(led, _Card("H880"), approved_by="user",
                             cutoff=CUT)
        assert led.lockbox_used("H880") is False
        assert led.lockbox_budget(CUT)["used"] == 0


def test_h02_current_v3_failure_blocks_g5(tmp_path, g5_cfg):
    """H-02 失败测试 2：历史 G3/V3 成功 + 同试验身份的当前 V3 失败 →
    G5 拒（已见 V3 失败不进入 G5），不扣预算、不烧一次性。"""
    with g3_ledger(tmp_path, card_id="H881") as led:
        # 同试验身份再跑一次，V3 未通过（当前判决 = 最新 V3 行）
        rid2 = led.record_run(
            card_id="H881", card_version=1, strategy="s:Donchian", params={},
            symbol="XAUUSD", timeframe="H1", data_first="2024-01-01",
            data_last="2026-06-30", data_sha256="a" * 64,
            engine_version="A-1.3", cost_model_version="zero-v0",
            gates_hash="", metrics={"sharpe": 1.0}, actor="test")
        led.record_gate(rid2, "G3", True, {})
        led.record_gate(rid2, "V3", False,
                        {"failed_vetoes": ["primary"]})
        with pytest.raises(vg.LockboxPrerequisiteError, match="V3"):
            vg.begin_lockbox(led, _Card("H881"), approved_by="user",
                             cutoff=CUT, trial=lockbox_trial())
        assert led.lockbox_used("H881") is False
        assert led.lockbox_budget(CUT)["used"] == 0


def test_h02_identity_changes_rejected(tmp_path, g5_cfg):
    """H-02 失败测试 3：参数/成本/代码指纹/冻结关卡版本任一变化 → 该
    历史成功不授权当前试验（无匹配的当前 G3 记录 → 拒）。"""
    with g3_ledger(tmp_path, card_id="H882") as led:
        for over, what in (
                (dict(params={"entry_days": 7}), "参数"),
                (dict(cost_model_version="cost-v9"), "成本"),
                (dict(strategy_sha="b" * 64), "代码指纹"),
                (dict(gates_hash="c" * 64), "关卡版本")):
            with pytest.raises(vg.LockboxPrerequisiteError,
                               match="没有 G3 关卡记录") as ei:
                vg.begin_lockbox(led, _Card("H882"), approved_by="user",
                                 cutoff=CUT, trial=lockbox_trial(**over))
            assert what in str(ei.value)
            assert led.lockbox_used("H882") is False
        assert led.lockbox_budget(CUT)["used"] == 0


def test_h02_current_g3_failure_blocks_g5(tmp_path, g5_cfg):
    """H-02：当前试验身份下最新 G3 行未通过 → 拒（历史 G3 通过不抵消）。"""
    with g3_ledger(tmp_path, card_id="H883") as led:
        rid2 = led.record_run(
            card_id="H883", card_version=1, strategy="s:Donchian", params={},
            symbol="XAUUSD", timeframe="H1", data_first="2024-01-01",
            data_last="2026-06-30", data_sha256="a" * 64,
            engine_version="A-1.3", cost_model_version="zero-v0",
            gates_hash="", metrics=None, error="ValueError: 爆了",
            actor="test")
        led.record_gate(rid2, "G3", False, {"reason": "样本外夏普 ≤ 0"})
        with pytest.raises(vg.LockboxPrerequisiteError, match="G3"):
            vg.begin_lockbox(led, _Card("H883"), approved_by="user",
                             cutoff=CUT, trial=lockbox_trial())
        assert led.lockbox_used("H883") is False


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
    # T23：含手数上下限（bt 默认按风险定手数，装配需 volume_min/step/max）
    (specs / "latest.json").write_text(
        json.dumps({"XAUUSD": {"contract_size": 100.0, "volume_min": 0.01,
                               "volume_step": 0.01, "volume_max": 200.0}}),
        encoding="utf-8")
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


def patch_as_main_store(monkeypatch, mod):
    """R6/C6：G5 只认主库（--main-store）——生产 classify_store 只认
    data/REPO_ROOT·data；测试的 tmp 数据根按"主库布局"使用（lockbox.json
    + bars + specs 俱全），须显式伪装为主库并带 --main-store。"""
    monkeypatch.setattr(mod, "classify_store", lambda p: "main")


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
    # R6/C6：G5 一律要求 --main-store（tmp 根按主库布局伪装）
    patch_as_main_store(monkeypatch, mod)
    rc = mod.main(gate_argv(root, cards_dir, ledger, "G5", "--main-store"))
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
        json.dumps({"XAUUSD": {"contract_size": 100.0, "volume_min": 0.01,
                               "volume_step": 0.01, "volume_max": 200.0}}),
        encoding="utf-8")
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
    crlf.write_bytes(vg.DEFAULT_GATES_TOML.read_bytes()
                     .replace(b'\r\n', b'\n').replace(b'\n', b'\r\n'))
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


def g5_kwargs(mod, bars, *, data_root="data", trial=None):
    from mt5quant.backtest.report import data_sha256
    spec = buy_hold_spec(mod)
    if trial is None:                      # H-02：编排必须带试验绑定
        trial = vg.LockboxTrial(
            params={}, data_sha256=data_sha256(bars),
            cost_model_version="zero-v0", strategy_sha="legacy",
            gates_hash="")
    return dict(strategy=spec.strategy, spec=spec, params={}, bars=bars,
                costs=mod.ZeroCosts(), session_policy=None,
                data_root=data_root, approved_by="user", trial=trial,
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
        led.record_gate(rid, "V3", True, {})
    real_begin = mod.vgate.begin_lockbox

    def _begin(led_, card, *, approved_by, cutoff=None, **kw):
        calls.append("begin")
        real_begin(led_, card, approved_by=approved_by, cutoff=cutoff, **kw)

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


def test_h02_orchestrate_v3_failure_never_reads_lockbox(tmp_path,
                                                         monkeypatch):
    """H-02（编排级）：同试验身份当前 V3 失败 → begin_lockbox 拒绝，锁箱
    loader 零调用、不扣预算（历史 G3/V3 成功不授权当前失败试验）。"""
    mod = load_bt()
    from mt5quant.backtest.report import data_sha256
    bars = ramp_bars(40)
    calls = []
    monkeypatch.setattr(mod.vgate, "load_lockbox_window",
                        lambda *a, **k: calls.append(1))
    ledger_path = tmp_path / "l.sqlite"
    segments = [{"train_range": [0, 20], "test_range": [20, 30],
                 "params": {}},
                {"train_range": [20, 30], "test_range": [30, 40],
                 "params": {}}]
    with Ledger(ledger_path) as led:
        rid1 = led.record_run(
            card_id="H001", card_version=1, strategy=BUY_HOLD, params={},
            symbol="XAUUSD", timeframe="H1", data_first="", data_last="",
            data_sha256=data_sha256(bars), engine_version="A-1.3",
            cost_model_version="zero-v0", gates_hash="",
            metrics={"sharpe": 1.0}, actor="test")
        led.record_gate(rid1, "G3", True, {"segments": segments})
        led.record_gate(rid1, "V3", True, {})
        rid2 = led.record_run(
            card_id="H001", card_version=1, strategy=BUY_HOLD, params={},
            symbol="XAUUSD", timeframe="H1", data_first="", data_last="",
            data_sha256=data_sha256(bars), engine_version="A-1.3",
            cost_model_version="zero-v0", gates_hash="",
            metrics=None, error="ValueError: 主检验未过", actor="test")
        led.record_gate(rid2, "G3", True, {"segments": segments})
        led.record_gate(rid2, "V3", False, {"failed_vetoes": ["primary"]})
    with Ledger(ledger_path) as led:
        with pytest.raises(vg.LockboxPrerequisiteError, match="V3"):
            mod._g5_orchestrate(led, h001_card(mod), **g5_kwargs(mod, bars))
        assert calls == []                     # 锁箱 loader 零调用
        assert led.lockbox_used("H001") is False
        assert led.conn.execute(
            "SELECT COUNT(*) n FROM lockbox_uses").fetchone()["n"] == 0


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
    # R6/C6：G5 只认主库——tmp 根按主库布局伪装并显式 --main-store
    patch_as_main_store(monkeypatch, mod)
    from mt5quant.backtest.report import data_sha256
    # H-02：预登记的授权试验必须与本次 G5 请求同一试验身份——数据哈希、
    # 成本版本、策略代码指纹与冻结门槛 sha 全部对上，且 G3/V3 当前通过
    research = bars[bars["time"] < cutoff].reset_index(drop=True)
    with Ledger(ledger) as led:
        rid = led.record_run(
            card_id="H700", card_version=1, strategy=BUY_HOLD, params={},
            symbol="XAUUSD", timeframe="H1",
            data_first=str(research["time"].iloc[0]),
            data_last=str(research["time"].iloc[-1]),
            data_sha256=data_sha256(research), engine_version="A-1.3",
            cost_model_version="zero-v0", gates_hash=mod._gates_hash(),
            metrics={"sharpe": 1.0, "trade_count": 1}, actor="test",
            strategy_sha=mod.cards.strategy_fingerprint(
                BUY_HOLD, mod.STRATEGY_FP_ROOT))
        led.record_gate(rid, "G3", True, {
            "segments": [{"train_range": [0, 100], "test_range": [100, 150],
                          "params": {}},
                         {"train_range": [100, 150], "test_range": [150, 200],
                          "params": {}}]})
        led.record_gate(rid, "V3", True, {})
    rc = mod.main(gate_argv(root, cards_dir, ledger, "G5", "--main-store",
                            "--approve", "--approved-by", "user",
                            "--params", "{}"))
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


# ================================================ T64/D33：dsr_trial_scope 键

def test_g4_dsr_trial_scope_absent_defaults_family(tmp_path):
    """T64/D33：dsr_trial_scope 缺键 → 缺省 family（v2 形态文件的冻结
    语义，缺省口径结果与现行逐位一致）。

    D34 冻结语义变更（2026-10-29，如实更新）：仓库 gates.toml 已是 v3，
    显式写 dsr_trial_scope = "rounds_lineage"（L1，见
    test_t85_freeze_l1）——缺省加载不再 family；本测试改对"缺键变体"
    验证缺省值不变。"""
    body = vg.DEFAULT_GATES_TOML.read_text(encoding="utf-8")
    assert 'dsr_trial_scope = "rounds_lineage"' in body   # 冻结文件显式写出
    p = tmp_path / "gates-noscope.toml"
    p.write_text(body.replace('dsr_trial_scope = "rounds_lineage"',
                              "# （缺键变体：dsr_trial_scope 缺省）"),
                 encoding="utf-8")
    cfg, _ = vg.load_gates(p, frozen_sha=vg.gates_sha256(p))
    assert cfg.dsr_trial_scope == "family"          # 缺键缺省（T64 语义）


def test_g4_dsr_trial_scope_scopes_parse(tmp_path):
    """[g4].dsr_trial_scope 三个合法枚举（family | project |
    rounds_lineage）都可解析；非法枚举拒绝（D34 冻结值 = rounds_lineage）。"""
    body = vg.DEFAULT_GATES_TOML.read_text(encoding="utf-8")

    def variant(val: str, name: str) -> Path:
        p = tmp_path / name
        p.write_text(body.replace('dsr_trial_scope = "rounds_lineage"',
                                  f'dsr_trial_scope = "{val}"'),
                     encoding="utf-8")
        return p

    for val, name in (("family", "gates-family.toml"),
                      ("project", "gates-project.toml"),
                      ("rounds_lineage", "gates-rounds.toml")):
        p = variant(val, name)
        cfg, _ = vg.load_gates(p, frozen_sha=vg.gates_sha256(p))
        assert cfg.dsr_trial_scope == val
        assert cfg.dsr_min_probability == 0.95
    # 枚举外拒绝
    p3 = variant("global", "gates-bad.toml")
    with pytest.raises(vg.GatesConfigError, match="dsr_trial_scope"):
        vg.load_gates(p3, frozen_sha=vg.gates_sha256(p3))


# ================================================ T65/D34：关卡 v3（suite="v3"）

def test_d34_freeze_collapses_dual_track_single_fingerprint(tmp_path):
    """原 T65 失败测试 1"v3 路径不影响现行 gates.toml 冻结校验"（原名
    test_v3_path_leaves_frozen_v2_bit_for_bit）——D34 冻结（2026-10-29，
    R14 §2 批准）后前提反转，如实更名与更新为收束守护：
    gates-v3.toml 内容已替换 gates.toml，单一冻结对象与指纹——
    - 缺省与显式 suite="v3" 加载同一文件、同一 sha（FROZEN_GATES_SHA256，
      原 FROZEN_GATES_V3_SHA256 合一移除）；
    - suite="v2" 是冻结前历史口径：仓库文件缺省路径上拒绝（须显式 path
      注入 v2 形态文件）；
    - 篡改拒绝照常（单文件单指纹）。
    """
    cfg, sha = vg.load_gates()                        # 缺省 = v3 语义
    assert vg.DEFAULT_GATES_TOML.name == "gates.toml"
    assert sha == vg.FROZEN_GATES_SHA256
    assert type(cfg) is vg.GatesV3Config and cfg.version == 3
    explicit, explicit_sha = vg.load_gates(suite="v3")
    assert explicit_sha == sha and explicit == cfg    # 缺省=显式 v3，不留状态
    assert not hasattr(vg, "FROZEN_GATES_V3_SHA256")  # 指纹合一（收束）
    assert not hasattr(vg, "DEFAULT_GATES_V3_TOML")
    # suite="v2" 只保留给显式 path 的历史（v2 形态）文件
    with pytest.raises(vg.GatesConfigError, match="v2"):
        vg.load_gates(suite="v2")
    # 篡改拒绝（冻结对象 = 替换后的 gates.toml）
    bad = tmp_path / "gates.toml"
    bad.write_bytes(vg.DEFAULT_GATES_TOML.read_bytes() + b"\n# x\n")
    with pytest.raises(vg.GatesConfigError, match="sha256"):
        vg.load_gates(bad)
    assert vg.load_gates()[1] == sha                  # 守护原样


def test_v3_toml_schema_validation(tmp_path):
    """gates.toml（D34 冻结后即 v3 内容）的 [v3] 节与 version 校验：非法
    枚举/范围/类型/未知键拒绝；v2 解析路径读到 [v3] 节按未知顶层键拒绝
    （v3 键不进 v2 语义——suite="v2" 显式注入）。"""
    body = vg.DEFAULT_GATES_TOML.read_text(encoding="utf-8")

    def variant(*repl: tuple[str, str]) -> Path:
        t = body
        for old, new in repl:
            assert old in t, old
            t = t.replace(old, new)
        p = tmp_path / "gates.toml"
        p.write_text(t, encoding="utf-8")
        return p

    def load(p: Path):
        return vg.load_gates(p, frozen_sha=vg.gates_sha256(p), suite="v3")

    with pytest.raises(vg.GatesConfigError, match="primary_test"):
        load(variant(('primary_test = "dsr_v0"',
                      'primary_test = "crystal"')))
    with pytest.raises(vg.GatesConfigError, match="n_min"):
        load(variant(("n_min = 2", "n_min = 0")))     # L2：下限键 ≥1
    with pytest.raises(vg.GatesConfigError, match="alpha"):
        load(variant(("alpha = 0.05", "alpha = 1.5")))
    with pytest.raises(vg.GatesConfigError, match="bootstrap_sims"):
        load(variant(("bootstrap_sims = 1000", "bootstrap_sims = 1")))
    with pytest.raises(vg.GatesConfigError, match="未知"):
        load(variant(("\n[g1]", "\nstray = 1\n\n[g1]")))
    with pytest.raises(vg.GatesConfigError, match="未知"):
        load(variant(("cost_scale_veto = 1.5", "cost_scale_veto = 1.5\n"
                                             "oracle = true")))
    with pytest.raises(vg.GatesConfigError, match="version"):
        load(variant(("version = 3", "version = 2")))
    # v2 解析路径读 v3 文件（带 [v3] 节）→ 未知顶层键 v3 拒绝
    with pytest.raises(vg.GatesConfigError, match="v3"):
        vg.load_gates(variant(), frozen_sha=vg.gates_sha256(variant()),
                      suite="v2")


def test_v3_diagnostic_failures_do_not_change_verdict():
    """T65 失败测试 2：诊断项失败不改变通过结论——否决层全过即通过，
    诊断层（参数邻域/按年分段/去最好 5%/G1 样本内线）只报告。"""
    ok = {"passed": True}
    out = vg.evaluate_v3(
        g0=ok, primary={"passed": True}, cost_scale=ok, delay=ok, buyhold=ok,
        diagnostics={"param_plateau": {"passed": False, "fraction": 0.2},
                     "yearly_segments": {"passed": False},
                     "drop_best_5pct": {"passed": False},
                     "g1_trade_count": {"passed": False}})
    assert out["passed"] is True
    assert out["failed_vetoes"] == []
    assert out["diagnostics"] == {"param_plateau": False,
                                  "yearly_segments": False,
                                  "drop_best_5pct": False,
                                  "g1_trade_count": False}


def test_v3_any_veto_failure_fails_the_suite():
    """T65 失败测试 3：否决项失败即不通过——G0 / 主检验 / 成本 ×1.5 /
    延迟一根 / 多头相对买入持有超额，任一失败整 suites 不通过。"""
    ok = {"passed": True}
    bad = {"passed": False}
    cases = {
        "G0": dict(g0=bad, primary=ok, cost_scale=ok, delay=ok, buyhold=ok),
        "primary": dict(g0=ok, primary=bad, cost_scale=ok, delay=ok,
                        buyhold=ok),
        "cost_scale_x15": dict(g0=ok, primary=ok, cost_scale=bad, delay=ok,
                               buyhold=ok),
        "delay_1_bar": dict(g0=ok, primary=ok, cost_scale=ok, delay=bad,
                            buyhold=ok),
        "buyhold_excess": dict(g0=ok, primary=ok, cost_scale=ok, delay=ok,
                               buyhold=bad),
    }
    for name, kw in cases.items():
        out = vg.evaluate_v3(**kw, diagnostics={"param_plateau": ok})
        assert out["passed"] is False, name
        assert out["failed_vetoes"] == [name]
    # 全过 → 通过
    assert vg.evaluate_v3(g0=ok, primary=ok, cost_scale=ok, delay=ok,
                          buyhold=ok)["passed"] is True


def test_v3_primary_tests_dsr_and_year_block():
    """主检验两个备选（蓝图附录 B v3）：
    - 方案甲：WF 拼接 OOS 收益的 PSR/DSR（N=项目级，实验 n_trials=1）；
    - 方案乙：同一收益序列按年块自助的单侧 p（Bonferroni 按项目级 N）。
    """
    cfg = vg.load_gates(suite="v3")[0]
    rng = np.random.default_rng(7)
    good = pd.Series(0.0012 + rng.normal(0.0, 0.0008, 6000))
    noise = pd.Series(rng.normal(0.0, 0.004, 6000))
    a_good = vg.v3_primary_dsr(good, n_trials=1, trial_sharpes=None, cfg=cfg)
    a_noise = vg.v3_primary_dsr(noise, n_trials=1, trial_sharpes=None,
                                cfg=cfg)
    assert a_good["passed"] is True and a_good["probability"] >= 0.95
    assert a_noise["passed"] is False and a_noise["probability"] < 0.95
    assert a_noise["trial_scope"] == "rounds_lineage" \
        and a_noise["test"] == "dsr"   # D34 冻结口径（原 project，如实更新）
    # OOS 无方差（常数）→ fail-closed 概率 0
    flat = vg.v3_primary_dsr(pd.Series([0.0] * 50), n_trials=1,
                             trial_sharpes=None, cfg=cfg)
    assert flat["passed"] is False and flat["probability"] == 0.0

    # 方案乙：多年正漂移（每年均值>0）→ p 小过；零漂移 → 不过的概率语义
    hours = pd.date_range("2016-01-01", periods=6000, freq="6h")
    b_good = pd.Series(0.0004 + rng.normal(0.0, 0.004, 6000),
                       index=pd.DatetimeIndex(hours))
    b_zero = pd.Series(rng.normal(0.0, 0.004, 6000),
                       index=pd.DatetimeIndex(hours))
    out_good = vg.v3_primary_year_block(b_good, n_trials=1, cfg=cfg, seed=11)
    out_zero = vg.v3_primary_year_block(b_zero, n_trials=1, cfg=cfg, seed=11)
    assert out_good["test"] == "year_block"
    assert out_good["threshold"] == pytest.approx(cfg.alpha)
    assert out_good["n_blocks"] >= 2
    assert out_good["passed"] is True
    assert out_zero["passed"] is False
    # Bonferroni：N=40 → 阈值收紧 40 倍（项目级试验数的口径）
    n40 = vg.v3_primary_year_block(b_good, n_trials=40, cfg=cfg, seed=11)
    assert n40["threshold"] == pytest.approx(cfg.alpha / 40)
    assert n40["bonferroni_n"] == 40
    # 年块 <2 → fail-closed（不炸、passed=False 带 reason）
    one_year = pd.Series(0.001 + rng.normal(0.0, 0.004, 500),
                         index=pd.DatetimeIndex(
                             pd.date_range("2016-01-01", periods=500,
                                           freq="h")))
    few = vg.v3_primary_year_block(one_year, n_trials=1, cfg=cfg, seed=11)
    assert few["passed"] is False and "年块" in few["reason"]


def test_v3_run_primary_test_dispatch():
    """run_primary_test 按 [v3].primary_test 分派（D34 冻结后 = "dsr_v0"，
    原为 "dsr"——如实更新）；year_block 仍可显式配置分派；未知值（不可达，
    schema 已挡）fail-closed。N_eff 下限见 test_t85_freeze_l2。"""
    cfg = vg.load_gates(suite="v3")[0]
    rng = np.random.default_rng(3)
    rets = pd.Series(0.001 + rng.normal(0.0, 0.001, 4000),
                     index=pd.DatetimeIndex(
                         pd.date_range("2016-01-01", periods=4000,
                                       freq="6h")))
    via = vg.run_primary_test(rets, n_trials=3, cfg=cfg, seed=5)
    assert via["test"] == "dsr_v0"
    assert via == vg.v3_primary_dsr_null_variance(rets, n_trials=3, cfg=cfg)
    cfg_b = dataclasses.replace(cfg, primary_test="year_block")
    via_yb = vg.run_primary_test(rets, n_trials=3, cfg=cfg_b, seed=5)
    assert via_yb["test"] == "year_block"
    direct = vg.v3_primary_year_block(rets, n_trials=3, cfg=cfg_b, seed=5)
    assert via_yb["p_value"] == direct["p_value"]
    cfg_bad = dataclasses.replace(cfg, primary_test="crystal")
    with pytest.raises(vg.GatesConfigError, match="primary_test"):
        vg.run_primary_test(rets, n_trials=3, cfg=cfg_bad, seed=5)


def _det_returns(n=6000, per_bar_sr=0.0285, std=0.004, seed=17):
    """确定性收益：样本均值/标准差精确设定 → 每根 bar 夏普 = per_bar_sr。"""
    rng = np.random.default_rng(seed)
    eps = rng.normal(0.0, 1.0, n)
    eps = (eps - eps.mean()) / eps.std(ddof=1) * std
    return pd.Series(eps + per_bar_sr * std)


def test_v3_primary_dsr_null_variance_v0():
    """T85/§4.1 方差口径（预写于结果之前）：方案甲 V = V₀ = 1/(n−1)（零假设
    下每根 bar 夏普估计的抽样方差，n = OOS bar 数），不用台账经验方差。

    - null_variance_trial_sharpes(v) 的 ddof=1 样本方差恰为 v（确定性两点集，
      与 N 无关——trial_sharpes 在 DSR 里只进方差）；
    - N=1 时 SR₀=0 → 甲@N=1 与现行 PSR(0) 概率逐位相同（现行 primary.dsr
      键语义不变的对照锚点）；
    - N 抬高 → SR₀ 抬高 → 同一收益概率单调不增；
    - 恒定序列 fail-closed。"""
    cfg = vg.load_gates(suite="v3")[0]
    ts = vg.null_variance_trial_sharpes(0.0002)
    assert float(np.var(ts, ddof=1)) == pytest.approx(0.0002, rel=1e-12)
    rets = _det_returns()
    a1 = vg.v3_primary_dsr_null_variance(rets, n_trials=1, cfg=cfg)
    p0 = vg.v3_primary_dsr(rets, n_trials=1, trial_sharpes=None, cfg=cfg)
    assert a1["probability"] == pytest.approx(p0["probability"])
    assert a1["v0"] == pytest.approx(1.0 / (len(rets) - 1))
    assert a1["test"] == "dsr_v0"
    probs = [vg.v3_primary_dsr_null_variance(rets, n_trials=n, cfg=cfg)
             ["probability"] for n in (1, 2, 6, 24)]
    assert probs == sorted(probs, reverse=True)   # N↑ → SR₀↑ → 概率↓
    # 零均值噪声 → 任何 N 都不过
    noise = _det_returns(per_bar_sr=0.0)
    for n in (1, 6, 24):
        out = vg.v3_primary_dsr_null_variance(noise, n_trials=n, cfg=cfg)
        assert out["passed"] is False
    # 恒定序列（无方差）→ fail-closed 概率 0 + reason
    flat = vg.v3_primary_dsr_null_variance(pd.Series([0.001] * 50),
                                           n_trials=6, cfg=cfg)
    assert flat["passed"] is False and flat["probability"] == 0.0
    assert flat.get("reason")


def test_v3_primary_psr_bonferroni():
    """T85/§4.1 方案丙（预写于结果之前）：PSR(0) ≥ 1 − α/N（Bonferroni）。

    - 同一 OOS 收益：概率与 N 无关（就是 PSR(0)），阈值随 N 收紧；
    - 边界样本（PSR≈0.986）N=1/2 过、N=6/24 不过——丙比甲（DSR 先扣
      SR₀ 再叠 95% 置信）宽，这是两方案差异的机制；
    - 噪声不过；恒定序列 fail-closed。"""
    cfg = vg.load_gates(suite="v3")[0]
    rets = _det_returns()
    base = vg.v3_primary_dsr(rets, n_trials=1, trial_sharpes=None, cfg=cfg)
    for n in (1, 2, 6, 24):
        out = vg.v3_primary_psr_bonferroni(rets, n_trials=n, cfg=cfg)
        assert out["test"] == "psr0_bonferroni"
        assert out["threshold"] == pytest.approx(1.0 - cfg.alpha / n)
        assert out["bonferroni_n"] == n
        assert out["probability"] == pytest.approx(base["probability"])
    verdicts = {n: vg.v3_primary_psr_bonferroni(rets, n_trials=n, cfg=cfg)
                ["passed"] for n in (1, 2, 6, 24)}
    assert verdicts == {1: True, 2: True, 6: False, 24: False}
    noise = _det_returns(per_bar_sr=0.0)
    assert vg.v3_primary_psr_bonferroni(noise, n_trials=1,
                                        cfg=cfg)["passed"] is False
    flat = vg.v3_primary_psr_bonferroni(pd.Series([0.001] * 50), n_trials=6,
                                        cfg=cfg)
    assert flat["passed"] is False and flat.get("reason")


def test_v3_event_branch_config():
    """T85/§4.2：事件模式分支写进门槛文件（D35 修改三条全落地）——
    主检验=随机化参照 p ≤ α/N、随机化次数下限 max(1999, ⌈20N/α⌉)、
    否决=成本 ×1.5 净均值 > 0、不适用买入持有超额与延迟一根、四项诊断；
    [v3] 原键语义逐位不变；D34 冻结：primary_test="dsr_v0"、n_min=2，
    冻结指纹合一（FROZEN_GATES_SHA256）。"""
    cfg, sha = vg.load_gates(suite="v3")
    assert sha == vg.FROZEN_GATES_SHA256
    assert cfg.primary_test == "dsr_v0" and cfg.n_min == 2
    assert cfg.alpha == pytest.approx(0.05)
    assert cfg.cost_scale_veto == pytest.approx(1.5)
    assert cfg.delay_bars == 1 and cfg.buyhold_excess_veto is True
    ev = cfg.event
    assert ev.primary_test == "randomization"
    assert ev.draws_min_base == 1999
    assert ev.draws_per_test_coeff == pytest.approx(20.0)
    assert ev.buyhold_excess_veto is False      # D35 修改 2：不适用
    assert ev.delay_bars_veto is False          # D35 修改 2：不适用
    assert set(ev.diagnostics) == {"year_block_vs_zero", "window_shift_1_bar",
                                   "segmented_means", "known_gaps"}
    # 随机化次数下限：max(1999, ⌈20N/α⌉)——p 分辨率足以判到 α/N
    assert vg.v3_event_draws_min(1, cfg) == 1999
    assert vg.v3_event_draws_min(2, cfg) == 1999      # ⌈800⌉ < 1999
    assert vg.v3_event_draws_min(6, cfg) == 2400
    assert vg.v3_event_draws_min(24, cfg) == 9600


def test_v3_event_branch_schema_rejects(tmp_path):
    """[v3.event] 未知键 / 非法 primary_test / 非法诊断名 → 拒绝加载。"""
    raw = (Path(vg.DEFAULT_GATES_TOML).read_text(encoding="utf-8")
           + '\nunknown_event_key = 1\n')
    p = tmp_path / "gates-v3-bad.toml"
    p.write_text(raw, encoding="utf-8")
    with pytest.raises(vg.GatesConfigError, match="v3.event"):
        vg.load_gates(p, frozen_sha=vg.gates_sha256(p), suite="v3")
    bad_test = (Path(vg.DEFAULT_GATES_TOML).read_text(encoding="utf-8")
                .replace('primary_test = "randomization"',
                         'primary_test = "year_block"'))
    p2 = tmp_path / "gates-v3-bad2.toml"
    p2.write_text(bad_test, encoding="utf-8")
    with pytest.raises(vg.GatesConfigError, match="randomization"):
        vg.load_gates(p2, frozen_sha=vg.gates_sha256(p2), suite="v3")


# ================================ T85 冻结（R14 §2 四项条件，2026-10-29）
# 第十四轮审核 §2：批准关卡 v3 冻结（方案甲），冻结提交同一提交满足四项
# 条件并配失败测试（每条件至少一个先红）。台账替身沿用
# test_research_ledger.py 的 record 口径（七元组去重）。

def _freeze_ledger_record(led, run_n=1, *, card_id="H401", symbol="XAUUSD",
                          created_utc=None, round_id=None, actor="test"):
    return led.record_run(
        card_id=card_id, card_version=1,
        strategy="mt5quant.strategies.library.buy_hold:BuyHold",
        params={"run": run_n}, symbol=symbol, timeframe="H1",
        data_first="2024-01-01T00:00:00", data_last="2026-06-30T23:00:00",
        data_sha256="deadbeef" * 8, engine_version="A-1.1",
        cost_model_version="zero-v0", gates_hash="",
        metrics={"net_profit": 123.4, "trade_count": 1},
        actor=actor, created_utc=created_utc, round_id=round_id)


def _freeze_ledger(tmp_path):
    """台账替身：轮次内两谱系各 2 次研究试验 + H401 轮次外历史 3 次
    （延续谱系）→ primary_test_n("H401") 门槛 N = 4 + 3 = 7。"""
    led = Ledger(tmp_path / "freeze-l.sqlite")
    t = PAST
    for fam, sym in (("H401", "XAUUSD"), ("H402", "EURUSD")):
        for i in (1, 2):
            _freeze_ledger_record(led, run_n=i, card_id=fam, symbol=sym,
                                  created_utc=t, round_id="R1")
    for i in (10, 11, 12):
        _freeze_ledger_record(led, run_n=i, card_id="H401", created_utc=t)
    return led


def test_t85_freeze_l1_primary_dsr_v0_rounds_lineage(tmp_path):
    """R14 §2 条件 1（失败测试先行）：冻结配置的主检验与 N 口径真正指向
    方案甲 V₀ 工作点与新 N 口径——
    - [v3] primary_test = "dsr_v0"（V3_PRIMARY_TESTS 含该值），
      run_primary_test 分派到 v3_primary_dsr_null_variance；
    - [g4] dsr_trial_scope = "rounds_lineage"：N 取
      ledger.primary_test_n(continues_family) 的门槛部分；
    - 台账经验方差（trial_sharpes）只进诊断：传入不改变主检验结果；
    - 按冻结配置走 run_primary_test 与 v3_primary_dsr_null_variance 直调
      逐位相同（dict 全等）。"""
    cfg = vg.load_gates(suite="v3")[0]
    assert "dsr_v0" in vg.V3_PRIMARY_TESTS
    assert cfg.primary_test == "dsr_v0"
    assert cfg.dsr_trial_scope == "rounds_lineage"
    led = _freeze_ledger(tmp_path)
    n_info = led.primary_test_n("H401")
    led.close()
    assert n_info["project_n_dedup_rounds"] == 4
    assert n_info["lineage_n_dedup"] == 3
    assert n_info["n"] == 7                     # 门槛 N = 轮次内 + 延续谱系
    rets = _det_returns()
    via = vg.run_primary_test(rets, n_trials=n_info["n"], seed=5, cfg=cfg,
                              trial_sharpes=[0.9, 0.7, 1.3, 0.2])  # 经验方差
    direct = vg.v3_primary_dsr_null_variance(rets, n_trials=n_info["n"],
                                             cfg=cfg)
    assert via == direct                        # 逐位相同（经验方差不进）
    assert via["test"] == "dsr_v0"
    assert via["n_trials"] == n_info["n"]       # N 取自 primary_test_n
    assert via["trial_scope"] == "rounds_lineage"
    assert via["v0"] == pytest.approx(1.0 / (len(rets) - 1))   # V₀ 口径


def test_t85_freeze_l2_n_min_floor():
    """R14 §2 条件 2（失败测试先行）：L2——[v3] n_min = 2，主检验
    N_eff = max(N, n_min)（N=1 → N_eff=2）；事件模式分支不受影响
    （随机化检验在 N=1 时也是精确的，n_min 不进随机化次数下限）。"""
    cfg = vg.load_gates(suite="v3")[0]
    assert cfg.n_min == 2
    assert vg.v3_n_eff(1, cfg) == 2             # N=1 → N_eff=2
    assert vg.v3_n_eff(2, cfg) == 2
    assert vg.v3_n_eff(7, cfg) == 7             # N ≥ n_min 原样
    rets = _det_returns()
    via = vg.run_primary_test(rets, n_trials=1, seed=5, cfg=cfg)
    assert via["n_trials"] == 2                 # 下限生效
    assert via == vg.v3_primary_dsr_null_variance(rets, n_trials=2, cfg=cfg)
    # 事件分支不受 n_min 影响：随机化次数下限按 raw N 计算（N=1 → 1999）
    assert vg.v3_event_draws_min(1, cfg) == 1999
    assert vg.v3_event_draws_min(6, cfg) == 2400
    assert cfg.event.primary_test == "randomization"


def test_t85_freeze_c3_t65_report_superseded_annotation():
    """R14 §2 条件 3（失败测试先行）：T65 报告开头注明其数字按 T83 之前
    的成本口径计算、已被 T85 工作点补充标定取代。"""
    doc = (REPO_ROOT / "docs" / "research"
           / "gates-v3-proposal-2026-10-28.md").read_text(encoding="utf-8")
    head = doc[:doc.index("## 0.")]             # 结论摘要之前的开头
    assert "T83" in head and "取代" in head
    assert "gates-v3-operating-point-2026-10-28" in head


def test_t85_freeze_c4_gates_toml_replaced_single_fingerprint(tmp_path):
    """R14 §2 条件 4（失败测试先行，提案 §8）：gates-v3.toml 内容替换
    gates.toml，冻结指纹合一，v2/v3 双轨收束——
    - 缺省加载即 v3 语义（GatesV3Config、version=3、primary_test="dsr_v0"）；
    - FROZEN_GATES_SHA256 = 替换后 gates.toml 的 sha（与 v3 指纹合一）；
    - gates-v3.toml 随替换退役（不再存在第二来源）；
    - 篡改拒绝照常。"""
    cfg, sha = vg.load_gates()                  # 缺省 = v3 语义（收束）
    assert type(cfg) is vg.GatesV3Config
    assert cfg.version == 3
    assert cfg.primary_test == "dsr_v0" and cfg.n_min == 2
    assert cfg.dsr_trial_scope == "rounds_lineage"
    assert sha == vg.FROZEN_GATES_SHA256 == vg.gates_sha256(
        vg.DEFAULT_GATES_TOML)
    assert not (REPO_ROOT / "config" / "gates-v3.toml").exists()
    bad = tmp_path / "gates.toml"
    bad.write_bytes(vg.DEFAULT_GATES_TOML.read_bytes() + b"\n# x\n")
    with pytest.raises(vg.GatesConfigError, match="sha256"):
        vg.load_gates(bad)


def test_t85_freeze_c4_blueprint_d34_sync():
    """R14 §2 条件 4（失败测试先行）：蓝图附录 B 与 D34 记录同步——
    附录 B 的现行表换为 v3 冻结（方案甲 dsr_v0、rounds_lineage、n_min=2、
    新冻结指纹）；D34 决策记录注明冻结已执行、"冻结后按方案甲"。"""
    bp = (REPO_ROOT / "docs" / "blueprint.md").read_text(encoding="utf-8")
    assert "冻结后按方案甲" in bp                       # D34 决策记录
    appendix_b = bp[bp.index("## 附录 B"):bp.index("## 附录 C")]
    assert "冻结后按方案甲" in appendix_b               # 现行表 = v3
    assert "dsr_v0" in appendix_b
    assert "rounds_lineage" in appendix_b
    assert "n_min = 2" in appendix_b
    assert vg.FROZEN_GATES_SHA256[:8] in appendix_b     # 新冻结指纹入册


def test_h02_same_invocation_flow_binds_current_run(tmp_path, g5_cfg):
    """H-02：同次 G3/V3+G5 流程——关卡结果先入账（bt gate 即时记录），
    begin_lockbox 绑定**本次运行**的当前判决：当前运行无 G3/V3 行 → 拒；
    同一 run_id 上 G3/V3 通过后 → 占位成功（started）。

    （对照流程"仅请求 G5"由 test_gate_cli_g5_orchestration_started_then_done
    端到端覆盖：授权来自同身份的历史通过运行。）"""
    with g3_ledger(tmp_path, card_id="H885") as led:      # 历史完整通过
        # 当前运行的基准行（无任何关卡行）
        rid_now = led.record_run(
            card_id="H885", card_version=1, strategy="s:Donchian", params={},
            symbol="XAUUSD", timeframe="H1", data_first="2024-01-01",
            data_last="2026-06-30", data_sha256="a" * 64,
            engine_version="A-1.3", cost_model_version="zero-v0",
            gates_hash="", metrics={"sharpe": 1.0}, actor="test")
        # 当前运行 V3 失败先行入账（bt gate 即时记录）→ 已见失败不进 G5
        led.record_gate(rid_now, "V3", False, {"failed_vetoes": ["primary"]})
        with pytest.raises(vg.LockboxPrerequisiteError, match="V3"):
            vg.begin_lockbox(led, _Card("H885"), approved_by="user",
                             trial=lockbox_trial())
        assert led.lockbox_used("H885") is False
        # 同一 run_id 上 G3 通过 + V3 重跑通过（关卡重录语义）→ 绑定成立
        led.record_gate(rid_now, "G3", True, {})
        led.record_gate(rid_now, "V3", True, {})
        vg.begin_lockbox(led, _Card("H885"), approved_by="user",
                         trial=lockbox_trial())
        assert led.lockbox_status("H885") == "started"
