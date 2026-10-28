"""T83（R13 K5/K6）：研究路径隔夜费口径——全部失败测试先行。

- K5 参考价接线：bt screen/gate、gate_calibration（assemble_experiment 与
  run_event_phase）调 build_costs 须传 swap_ref_price（研究库该品种最后
  收盘，与 CLI 主路径同一约定）→ cost_version 带 relref。对账路径
  （reconcile_ab / reconcile_mt5_tester）**有意**保持绝对口径（与 MT5
  策略测试器同口径），由守护测试钉住。
- K6 模拟盘实测不进研究成本：calibrated.json 带 account_kind（demo|real，
  calibrate_costs 从网关账户 trade_mode 写入）；build_costs 只认 real 的
  实测隔夜费，demo 实测只落报告、cost_version 注明"demo 实测未采用"。
- account-profile.toml：只有用户确认过的 swap_free 条目生效，缺省 charged，
  文件不存在 = 全 charged。
- 预筛双口径：持仓过夜候选的 precheck JSON 同时给 charged 与 swap_free
  两口径成本倍数与检验力；决定按 account-profile 取；swap_free 过而
  charged 不过 → swap_dependent=true。
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from mt5data.store import _COLUMNS
from mt5quant.costs import SpreadModel
from mt5quant.costs.spread import SpreadStat
from mt5quant.research.candidates import load_candidate, precheck

REPO_ROOT = Path(__file__).resolve().parents[1]
PAST = pd.Timestamp("2026-01-01", tz="UTC")

SYMBOL = "USTEC"
# 点数模式快照（与 data/specs 实测同构）：-633.8 点 × point 0.01 × 合约 1
# → −6.338 USD/手/夜（校准于 ≈25097 价位，T82 口径）
SWAP_LONG_POINTS = -633.8
PER_LOT_NIGHT = -6.338
USTEC_REF = 25097.0
EARLY_1986 = 131.0


def load_script(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "scripts" / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_bt():
    return load_script("bt_script_t83", "bt.py")


def load_gc():
    return load_script("gate_calibration_t83", "gate_calibration.py")


# ---------------------------------------------------------------- 装夹具

def d1_bars(closes, start="1986-01-06", spread=2, tf="D"):
    """K 线（1986-01-06 是周一）：收盘价序列给定，其余字段定值。"""
    times = pd.date_range(start, periods=len(closes), freq=tf)
    df = pd.DataFrame({
        "time": times, "open": [c - 0.5 for c in closes],
        "high": [c + 0.5 for c in closes], "low": [c - 1.0 for c in closes],
        "close": closes, "tick_volume": 10, "spread": spread, "real_volume": 0,
    })[_COLUMNS]
    return df


def write_research_store(root: Path, bars: pd.DataFrame, *,
                         calibrated: dict | None = None, tf: str = "D1"):
    """研究库样式数据根：bars + specs（点数模式隔夜费）+ 点差缓存（相对模式）。

    calibrated 给定时写 costs/calibrated.json（可含 account_kind）。
    """
    sdir = root / "bars" / SYMBOL / tf
    sdir.mkdir(parents=True)
    bars.to_parquet(sdir / "data.parquet")
    (sdir / "meta.json").write_text(json.dumps({
        "symbol": SYMBOL, "timeframe": tf, "rows": len(bars),
        "first_time": bars["time"].iloc[0].isoformat(),
        "last_closed_time": bars["time"].iloc[-1].isoformat()}),
        encoding="utf-8")
    specs = root / "specs"
    specs.mkdir(exist_ok=True)
    (specs / "latest.json").write_text(json.dumps({
        SYMBOL: {"swap_mode": 1, "swap_long": SWAP_LONG_POINTS,
                 "swap_short": 0.0, "point": 0.01, "contract_size": 1.0,
                 "swap_rollover3days": 5, "volume_min": 0.01,
                 "volume_step": 0.01, "volume_max": 200.0,
                 "currency_profit": "USD", "currency_margin": "USD"}}),
        encoding="utf-8")
    stats = {h: SpreadStat(median=0.0002, p90=0.0002, p99=0.0002,
                           n=1000, n_days=20) for h in range(24)}
    model = SpreadModel({SYMBOL: stats}, {SYMBOL: 0.0002}, mode="relative")
    import run_backtest as rb   # bt/gc 装载后 scripts 目录已在 sys.path
    rb.save_spread_model_cache(
        rb.spread_model_cache_path(str(root)), model,
        {SYMBOL: {"rows": 0, "first_msc": 0, "last_msc": 0}})
    if calibrated is not None:
        cdir = root / "costs"
        cdir.mkdir(exist_ok=True)
        (cdir / "calibrated.json").write_text(
            json.dumps(calibrated), encoding="utf-8")
    return root


CARD_D1 = """\
id = "H099"
version = 1
symbols = ["USTEC"]
timeframe = "D1"
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

CARD_H1 = CARD_D1.replace('timeframe = "D1"', 'timeframe = "H1"')


def write_card(tmp_path, body=CARD_D1):
    d = tmp_path / "cards"
    d.mkdir(exist_ok=True)
    (d / "H099.toml").write_text(body, encoding="utf-8")
    return d


def patch_bt_git(monkeypatch, mod):
    """注入卡文件 git 状态与策略指纹（研究运行 e2e 的两问）。"""
    import datetime as _dt
    committed = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
    monkeypatch.setattr(mod.cards, "committed_at",
                        lambda path, runner=None: committed)
    monkeypatch.setattr(mod.cards, "worktree_clean",
                        lambda path, runner=None: True)
    monkeypatch.setattr(mod.cards, "ensure_strategy_clean",
                        lambda ref, root, **kw: None)
    monkeypatch.setattr(mod.cards, "strategy_fingerprint",
                        lambda ref, root, **kw: "a" * 64)


CANDIDATE_TOML = """\
id = "C001"
title = "月末效应（USTEC，T83 双口径用例）"
mechanism_class = "flow_calendar"
status = "registered"
symbols = ["USTEC"]
holding = "D1"
mechanism = "月末与月初的固定资金流（工资、养老金、再平衡）"

[literature]
original = ["McConnell & Xu (2008), FAJ 64(2)"]
replications = []
rebuttals = []
search_log = "SSRN / Google Scholar：month-end effect equity；2026-10-27"
published_effect = "1926–2005 美股只在月末前后获得风险补偿（原文表 2）"
expected_effect = "按发表后衰减 −58% 折算（McLean & Pontiff 2016），每事件毛收益 0.15%（文献折算，不取自本项目样本内）"

[declared]
events_per_year = 12
gross_return_per_event = 0.0015
entry_hour_ny = 16
exit_hour_ny = 10
overnight_nights = 3
window_bars = 5
expected_sharpe = 3.0
"""


def write_candidate(tmp_path, body=CANDIDATE_TOML, name="C001.toml"):
    d = tmp_path / "candidates"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(body, encoding="utf-8")
    return p


def dual_basis_store(tmp_path):
    """双口径用研究库：≈6 年 D1 交替收盘（±0.2% 价位；window_bars=5 的
    交替窗收益互相抵消 → 事件窗 σ≈0.002，两口径的隐含夏普（0.87 与 2.18）
    都不触及声明夏普 3.0 的截顶——检验力随口径真实变化；swap_free 口径
    检验力 ≥50%、charged 口径成本倍数 <3——决定由口径翻转）。"""
    n = int(6.05 * 365.25)
    closes = [USTEC_REF * (1.0 + (0.002 if i % 2 else -0.002))
              for i in range(n)]
    return write_research_store(tmp_path / "data_research",
                                d1_bars(closes, start="2020-01-06"))


# ------------------------------------------------- K5：参考价接线（bt 路径）

def stairs_1986_h1(cycles=5, plateau=60, tail=72):
    """1986 反例序列（H1）：131 起的台阶形拉升-回落（每 240 根一个来回，
    唐奇安 10 日突破入场 / 回落出场），末段跳到今日价位 25097——序列最后
    收盘即参考价。早年价位 ~131 → 25097 的 1/192。"""
    closes: list[float] = []
    for c in range(cycles):
        L = EARLY_1986 + 6.0 * c
        for lvl in (L, L + 2.0, L + 4.0, L + 2.5):
            closes.extend([lvl] * plateau)
    closes.extend([USTEC_REF] * tail)
    return d1_bars(closes, start="1986-01-06", tf="h")


DONCHIAN = ("mt5quant.strategies.library."
            "b1_donchian_breakout:DonchianBreakout")
DONCHIAN_PARAMS = '{"entry_days": 10, "exit_days": 2}'


def test_bt_gate_1986_counterexample_not_ruined(tmp_path, monkeypatch):
    """K5 失败测试：经 bt gate 的 1986 反例不再爆仓。

    合成研究库（H1 台阶序列，早年 ~131、末段 25097）：唐奇安突破策略
    100 手、本金 1 万，早年段每笔持仓 ≈7 夜——
    - 旧绝对口径：每笔 swap ≈ −6.338×100×7 ≈ −4400，第 3 笔后权益 ≤0
      （T65 §5.4 实测 6 笔爆仓的复算，本用例内直接跑旧口径作对照）；
    - 相对口径（bt gate 传 swap_ref_price=序列最后收盘）：早年每笔
      ≈ −6.338×(131/25097)×100×7 ≈ −25，权益全程 >0；台账
      cost_model_version 带 relref。
    """
    mod = load_bt()
    bars = stairs_1986_h1()
    root = write_research_store(tmp_path / "data_research", bars, tf="H1")
    cards_dir = write_card(tmp_path, CARD_H1)
    ledger = tmp_path / "ledger.sqlite"
    patch_bt_git(monkeypatch, mod)
    monkeypatch.chdir(tmp_path)          # 缺省数据根 = cwd 下 data_research
    rc = mod.main(
        ["gate", "--card", "H099", "--cards-dir", str(cards_dir),
         "--strategy", DONCHIAN, "--costs", "auto", "--gates", "G0",
         "--params", DONCHIAN_PARAMS,
         "--volume-lots", "100", "--initial-capital", "10000",
         "--ledger", str(ledger)])
    assert ledger.is_file(), "基准运行须已入台账"
    from mt5quant.research.ledger import Ledger
    with Ledger(ledger) as led:
        row = led.conn.execute(
            "SELECT cost_model_version, metrics_json FROM runs").fetchone()
    assert row is not None
    # K5 核心：研究路径点数模式折相对口径 → relref 入串（旧绝对口径无此标记）
    assert "relref[" in row["cost_model_version"]
    summary = json.loads(row["metrics_json"])["metrics"]
    assert summary["trade_count"] >= 3
    assert summary["ruined"] is False, "1986 反例：相对口径下权益不得爆掉"
    assert 0.0 < abs(summary["total_swap"]) < 5000.0   # 旧口径 ≈ −13310 且爆仓
    # 反例为真（同一序列、同一策略，旧绝对口径直接跑引擎 → 爆仓）
    import run_backtest as rb
    from mt5quant.backtest.engine_a import run_backtest
    from mt5quant.strategies.library.b1_donchian_breakout import \
        DonchianBreakout
    costs_old, _ = rb.build_costs(str(root), SYMBOL, 0.0,
                                  allow_zero_swap=True, research_store=True)
    res_old = run_backtest(
        DonchianBreakout(), rb.load_series(str(root), SYMBOL, "H1", None),
        costs_old, symbol=SYMBOL,
        params=json.loads(DONCHIAN_PARAMS), timeframe="H1",
        contract_size=1.0, volume_lots=100.0, initial_capital=10_000.0)
    assert res_old.ruined is True
    assert res_old.trades["swap"].sum() < -10_000.0
    assert rc in (0, 1)                  # 关卡结论不影响本用例（读台账为凭）


def test_bt_screen_passes_swap_ref_price(tmp_path, monkeypatch):
    """K5 失败测试：bt screen --costs auto 的台账 cost_model_version 带
    relref（点数模式隔夜费按参考价折相对口径）。"""
    mod = load_bt()
    closes = [USTEC_REF] * 12
    root = write_research_store(tmp_path / "data_research", d1_bars(closes))
    cards_dir = write_card(tmp_path)
    ledger = tmp_path / "ledger.sqlite"
    patch_bt_git(monkeypatch, mod)
    monkeypatch.chdir(tmp_path)
    assert mod.main(
        ["screen", "--card", "H099", "--cards-dir", str(cards_dir),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "auto", "--volume-lots", "0.01",
         "--ledger", str(ledger)]) == 0
    from mt5quant.research.ledger import Ledger
    with Ledger(ledger) as led:
        ver = led.conn.execute(
            "SELECT cost_model_version FROM runs").fetchone()[0]
    assert "relref[" in ver


def test_merged_costs_ref_price_keeps_precheck_numbers_identical(tmp_path):
    """K5 失败测试（C001/C002 数字逐位不变的合成镜像）：_merged_costs 传
    swap_ref_prices 后 cost_version 变（relref 入串），但预筛的事前成本与
    检验力**逐位不变**——预筛本来就按自己的参考价折算（点数费率换算出的
    绝对 USD/手/夜不随 ref 变）。"""
    mod = load_bt()
    root = dual_basis_store(tmp_path)
    last_close = float(pd.read_parquet(
        root / "bars" / SYMBOL / "D1" / "data.parquet")["close"].iloc[-1])
    costs_ref, ver_ref = mod._merged_costs(
        str(root), [SYMBOL], spread_fraction=0.1, commission=0.0,
        research_mode=True, swap_ref_prices={SYMBOL: last_close})
    costs_abs, ver_abs = mod._merged_costs(
        str(root), [SYMBOL], spread_fraction=0.1, commission=0.0,
        research_mode=True)
    assert "relref[" in ver_ref and "relref[" not in ver_abs
    assert ver_ref != ver_abs
    cand_path = write_candidate(tmp_path)
    c = load_candidate(cand_path)
    years = {f"{SYMBOL}/D1": 6.0}
    r_ref = precheck(c, data_root=str(root), cost_model=costs_ref,
                     research_years=years, active_mechanisms=set())
    r_abs = precheck(c, data_root=str(root), cost_model=costs_abs,
                     research_years=years, active_mechanisms=set())
    assert r_ref.checks["ex_ante_cost"] == r_abs.checks["ex_ante_cost"]
    assert r_ref.checks["power"] == r_abs.checks["power"]


def test_reconciliation_paths_keep_absolute_swap_basis():
    """K5 守护：对账路径（reconcile_ab / reconcile_mt5_tester）不传
    swap_ref_price——与 MT5 策略测试器同口径（测试器按快照点数的绝对额
    计费），对账必须与被对照物同一口径，否则差异里有口径差、不是执行差。"""
    for name in ("reconcile_ab.py", "reconcile_mt5_tester.py"):
        src = (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "swap_ref_price" not in src, name


# --------------------------------------------- K5：gate_calibration 接线

def test_gate_calibration_assemble_passes_swap_ref(tmp_path, monkeypatch):
    """K5 失败测试：assemble_experiment（负/正对照臂的成本装配）的
    cost_version 带 relref——研究段 K 线最后收盘折相对口径。"""
    gc = load_gc()
    root = write_research_store(tmp_path / "data_research",
                                d1_bars([USTEC_REF] * 12))
    monkeypatch.chdir(tmp_path)
    out = gc.assemble_experiment("data_research", [f"{SYMBOL} D1"])
    assert "relref[" in out[f"{SYMBOL} D1"]["cost_version"]


def test_run_event_phase_passes_swap_ref(tmp_path, monkeypatch):
    """K5 失败测试：run_event_phase 调 build_costs 传 swap_ref_price =
    事件臂 K 线最后收盘（研究库/已批准外部序列同源）。事件标定本体打桩，
    只验装配接线的参考价。"""
    gc = load_gc()
    bars = d1_bars([USTEC_REF] * 12, start="2020-01-06")
    write_research_store(tmp_path / "data_research", bars)
    import run_backtest as rb
    captured = {}
    real = rb.build_costs

    def spy(data_root, symbol, spread_fraction, commission, **kw):
        captured[symbol] = kw.get("swap_ref_price")
        return real(data_root, symbol, spread_fraction, commission, **kw)

    monkeypatch.setattr(rb, "build_costs", spy)
    monkeypatch.setattr(gc, "load_event_bars",
                        lambda *a, **k: (bars, "research"))
    monkeypatch.setattr(
        gc, "event_mode_calibration",
        lambda bars_, **kw: {"kind": kw["kind"], "n_events": 1,
                             "first": "2020-01-06", "last": "2020-01-17",
                             "bars_source": "research", "cost_version": "stub"})
    args = SimpleNamespace(event_levels="0.1", event_seeds=2,
                           event_test_sims=50, event_rand_sims=19,
                           quick=True, event_symbols=f"{SYMBOL} D1",
                           data_root="data_research", max_bars=None,
                           event_ext_source=None, event_kind="month_end")
    monkeypatch.chdir(tmp_path)
    res = gc.run_event_phase(args, log=lambda *a, **k: None)
    assert res["meta"]["phase"] == "event"
    assert captured[SYMBOL] == pytest.approx(
        float(bars["close"].iloc[-1]))


# ------------------------------------------------ K6：demo 实测不进研究成本

def test_calibrated_demo_free_of_swap_not_adopted(tmp_path):
    """K6 失败测试：demo 账户的实测 free_of_swap=true（与费率）不改变研究
    成本——隔夜费回落快照推算，cost_version 注明 demo 实测未采用。"""
    import run_backtest as rb
    calibrated = {
        "version": 1, "account_kind": "demo",
        "symbols": {SYMBOL: {
            "swap": {"free_of_swap": True,
                     "long": {"per_lot_per_night": -99.9},
                     "short": {"per_lot_per_night": -99.9}}}}}
    root = write_research_store(tmp_path / "data_research",
                                d1_bars([USTEC_REF] * 12),
                                calibrated=calibrated)
    costs, ver = rb.build_costs(str(root), SYMBOL, 0.0,
                                allow_zero_swap=True, research_store=True,
                                swap_ref_price=USTEC_REF)
    rule = costs.swap.rule(SYMBOL)
    # 快照推算：-633.8 点 × 0.01 × 1.0 = -6.338/手/夜（不被 demo 实测覆盖）
    assert rule.long_per_day == pytest.approx(PER_LOT_NIGHT)
    assert rule.short_per_day == pytest.approx(0.0)
    assert "swap0-calibrated" not in ver and "swap-cal" not in ver
    assert "demo-measured-not-adopted" in ver


def test_calibrated_real_account_measured_swap_adopted(tmp_path):
    """K6 对照（合法路径保持）：account_kind=real 的实测费率照常优先于快照。"""
    import run_backtest as rb
    calibrated = {
        "version": 1, "account_kind": "real",
        "symbols": {SYMBOL: {
            "swap": {"free_of_swap": None,
                     "long": {"per_lot_per_night": -7.7},
                     "short": {"per_lot_per_night": 0.0}}}}}
    root = write_research_store(tmp_path / "data_research",
                                d1_bars([USTEC_REF] * 12),
                                calibrated=calibrated)
    costs, ver = rb.build_costs(str(root), SYMBOL, 0.0,
                                allow_zero_swap=True, research_store=True,
                                swap_ref_price=USTEC_REF)
    rule = costs.swap.rule(SYMBOL)
    assert rule.long_per_day == pytest.approx(-7.7)
    assert "swap-cal" in ver
    assert "demo-measured-not-adopted" not in ver


def test_calibrated_missing_account_kind_treated_as_not_real(tmp_path):
    """K6 边界：旧 calibrated.json（无 account_kind）的实测隔夜费同样不采用
    （无从证明来自实盘——校准工具历史上只连模拟盘）。"""
    import run_backtest as rb
    calibrated = {
        "version": 1,
        "symbols": {SYMBOL: {
            "swap": {"free_of_swap": True,
                     "long": {"per_lot_per_night": -99.9},
                     "short": {"per_lot_per_night": -99.9}}}}}
    root = write_research_store(tmp_path / "data_research",
                                d1_bars([USTEC_REF] * 12),
                                calibrated=calibrated)
    costs, ver = rb.build_costs(str(root), SYMBOL, 0.0,
                                allow_zero_swap=True, research_store=True,
                                swap_ref_price=USTEC_REF)
    assert costs.swap.rule(SYMBOL).long_per_day == pytest.approx(PER_LOT_NIGHT)
    assert "demo-measured-not-adopted" in ver


# ------------------------------------------------- account-profile.toml

PROFILE_CONFIRMED = """\
# T83 测试用账户隔夜费画像
[account]
kind = "real"
confirmed_by = "用户"
confirmed_on = "2026-10-27"

[symbols.USTEC]
status = "swap_free"
confirmed_by = "用户"
confirmed_on = "2026-10-27"
evidence = "实盘账户条款页存档"

[symbols.XAUUSD]
status = "swap_free"
confirmed_by = ""
confirmed_on = ""
evidence = ""

[symbols.EURUSD]
status = "charged"
confirmed_by = "用户"
confirmed_on = "2026-10-27"
evidence = "实盘实测收费"

[symbols.BTCUSD]
status = "unknown"
confirmed_by = ""
confirmed_on = ""
evidence = ""
"""


def test_account_profile_missing_file_all_charged(tmp_path, monkeypatch):
    """T83 失败测试：文件不存在 = 全 charged（装载器返回空生效集）。"""
    mod = load_bt()
    monkeypatch.setattr(mod, "ACCOUNT_PROFILE_PATH",
                        tmp_path / "不存在.toml")
    assert mod.load_account_profile() == {}
    assert mod.load_account_profile(tmp_path / "另一处.toml") == {}


def test_account_profile_only_confirmed_swap_free_effective(tmp_path):
    """T83 失败测试：只有用户确认过的 swap_free 条目生效；charged/unknown/
    未填确认人的 swap_free 一律按 charged（缺省保守）。"""
    mod = load_bt()
    p = tmp_path / "account-profile.toml"
    p.write_text(PROFILE_CONFIRMED, encoding="utf-8")
    assert mod.load_account_profile(p) == {SYMBOL: "swap_free"}


def test_account_profile_invalid_status_rejected(tmp_path):
    mod = load_bt()
    p = tmp_path / "account-profile.toml"
    p.write_text(PROFILE_CONFIRMED.replace('status = "swap_free"\nconfirmed_by = "用户"',
                                            'status = "maybe"\nconfirmed_by = "用户"', 1),
                 encoding="utf-8")
    with pytest.raises(ValueError, match="status"):
        mod.load_account_profile(p)


def test_repo_account_profile_template_defaults_all_charged():
    """T83：仓库模板存在（不含账号/服务器名）。2026-10-29 用户确认四品种
    swap_free（隔夜费按 0 计）——装载返回确认过的映射。"""
    p = REPO_ROOT / "config" / "account-profile.toml"
    assert p.is_file()
    src = p.read_text(encoding="utf-8")
    assert "login" not in src and "server" not in src
    mod = load_bt()
    assert mod.load_account_profile(p) == {
        "XAUUSD": "swap_free", "EURUSD": "swap_free",
        "BTCUSD": "swap_free", "USTEC": "swap_free"}


# ----------------------------------------------------- 预筛双口径

def test_candidate_check_dual_basis_and_decision_flip(tmp_path, monkeypatch,
                                                      capsys):
    """T83 失败测试：持仓过夜候选的 precheck JSON 双口径数字 + 决定按
    account-profile 状态取 + swap_dependent 标记。

    构造：隔夜费使 charged 口径成本倍数 <3（fail）、swap_free 口径 ≥3
    （pass）；缺省（全 charged）决定 = fail；用户确认 swap_free 后决定
    翻转为 pass，swap_dependent=true 一并登记。
    """
    mod = load_bt()
    root = dual_basis_store(tmp_path)
    cand_path = write_candidate(tmp_path)
    # 第一段：缺省无画像文件 → 全 charged → 决定 fail（成本倍数 <3）
    monkeypatch.setattr(mod, "ACCOUNT_PROFILE_PATH",
                        tmp_path / "profile.toml")     # 不存在
    assert mod.main(["candidate", "check", "C001",
                     "--candidates-dir", str(cand_path.parent),
                     "--data-root", str(root),
                     "--ledger", str(tmp_path / "l1.sqlite")]) == 1
    payload = json.loads((cand_path.parent / "C001.precheck.json")
                         .read_text(encoding="utf-8"))
    assert payload["decision"] == "fail"
    dual = payload["swap_basis"]
    assert dual["applicable"] is True
    assert dual["decision_basis"] == "charged"
    assert dual["account_profile"] == {SYMBOL: "charged"}
    charged, free = dual["charged"], dual["swap_free"]
    assert charged["multiple"] < 3.0 <= free["multiple"]
    assert charged["power"] < free["power"]          # 净收益差 → 检验力差
    assert dual["swap_dependent"] is True            # free 过而 charged 不过
    # 主判定块 = charged 口径（缺省），逐位与单口径预筛一致
    assert payload["checks"]["ex_ante_cost"]["multiple"] == charged["multiple"]

    # 第二段：用户确认 USTEC swap_free → 决定按 swap_free 口径取 → 翻转 pass
    (tmp_path / "profile.toml").write_text(PROFILE_CONFIRMED, encoding="utf-8")
    assert mod.main(["candidate", "check", "C001",
                     "--candidates-dir", str(cand_path.parent),
                     "--data-root", str(root),
                     "--ledger", str(tmp_path / "l2.sqlite")]) == 0
    payload2 = json.loads((cand_path.parent / "C001.precheck.json")
                          .read_text(encoding="utf-8"))
    assert payload2["decision"] == "pass"
    assert payload2["swap_basis"]["decision_basis"] == "swap_free"
    assert payload2["swap_basis"]["swap_dependent"] is True
    assert payload2["swap_basis"]["charged"]["multiple"] == charged["multiple"]
    # 决定口径入成本身份（swapfree 标记，台账可区分）
    assert "swapfree" in payload2["cost_model_version"].lower()
    assert any("swap_dependent" in r for r in payload2["reasons"])
    out = capsys.readouterr().out
    assert "swap_dependent" in out


def test_candidate_check_no_overnight_dual_not_applicable(tmp_path,
                                                          monkeypatch):
    """T83 失败测试：不过夜候选（overnight_nights=0）双口径不适用——
    隔夜费恒 0，两口径无差别，不产生 swap_basis 数字。"""
    mod = load_bt()
    root = dual_basis_store(tmp_path)
    body = CANDIDATE_TOML.replace("overnight_nights = 3",
                                  "overnight_nights = 0")
    cand_path = write_candidate(tmp_path, body)
    monkeypatch.setattr(mod, "ACCOUNT_PROFILE_PATH",
                        tmp_path / "profile.toml")
    mod.main(["candidate", "check", "C001",
              "--candidates-dir", str(cand_path.parent),
              "--data-root", str(root),
              "--ledger", str(tmp_path / "l.sqlite")])
    payload = json.loads((cand_path.parent / "C001.precheck.json")
                         .read_text(encoding="utf-8"))
    assert payload["swap_basis"]["applicable"] is False
    assert "charged" not in payload["swap_basis"]
