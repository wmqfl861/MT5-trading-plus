"""T62/D31（铁律 17、18）：候选登记装载（蓝图附录 C）与预筛五条件。

- 装载：附录 C 字段齐全才可加载（search_log 必须非空；[precheck] 段是
  工具产物，候选 TOML 里见到即拒）；
- 预筛（precheck，蓝图 §6.2 五条硬条件）：文献 / 持仓 ≥ H1 / 事前成本
  倍数 ≥ 3（成本模型 v3 + 快照隔夜费保守计入）/ 检验力 ≥ 50%（不足 →
  waiting_data，铁律 18"待数据"队列）/ 机制空缺（排序类理由，不阻塞）；
- 台账：candidate_checks 表带哈希链（沿用 T37/R6/C5 机制）；
- CLI：bt candidate check 读候选 → precheck → 写 C###.precheck.json +
  台账 → 打印结论。
"""
import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from mt5quant.costs import (
    CommissionModel, Costs, SlippageModel, SpreadModel, SwapModel, SwapRule,
)
from mt5quant.costs.spread import SpreadStat
from mt5quant.research import candidates
from mt5quant.research.candidates import (
    CandidateError, load_candidate, precheck,
)
from mt5quant.research.ledger import Ledger

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "bt.py"
SYMBOL = "TESTUSD"


def load_bt():
    spec = importlib.util.spec_from_file_location("bt_script_candidate",
                                                  SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- 装夹具

CANDIDATE_TOML = """\
id = "C001"
title = "月末效应（TESTUSD）"
mechanism_class = "flow_calendar"
status = "registered"
symbols = ["TESTUSD"]
holding = "D1"
mechanism = "月末与月初的固定资金流（工资、养老金、再平衡）"

[literature]
original = ["McConnell & Xu (2008), FAJ 64(2)"]
replications = []
rebuttals = []
search_log = "SSRN / Google Scholar：month-end effect equity；2026-10-27"
published_effect = "1926–2005 美股只在月末前后获得风险补偿（原文表 2）"
expected_effect = "按发表后衰减 −58% 折算（McLean & Pontiff 2016），折算后每事件毛收益 0.30%（依据为文献折算，不取自本项目样本内）"

[declared]
events_per_year = 12
gross_return_per_event = 0.003
window_bars = 4
entry_hour_ny = 16
exit_hour_ny = 10
overnight_nights = 1
expected_sharpe = 1.0
"""


def write_candidate(tmp_path, body=CANDIDATE_TOML, name="C001.toml"):
    d = tmp_path / "candidates"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(body, encoding="utf-8")
    return p


def make_costs(sym=SYMBOL, *, rel_spread=0.0002, spread_fraction=0.1,
               commission=6.0, long_swap=-5.0, short_swap=-1.0):
    stats = {h: SpreadStat(median=rel_spread, p90=rel_spread,
                           p99=rel_spread, n=1000, n_days=20)
             for h in range(24)}
    spread = SpreadModel({sym: stats}, {sym: rel_spread}, mode="relative")
    return Costs(
        spread=spread,
        slippage_model=SlippageModel(spread, spread_fraction=spread_fraction),
        commission=CommissionModel(default_per_lot=commission),
        swap=SwapModel(per_symbol={sym: SwapRule(
            long_per_day=long_swap, short_per_day=short_swap)}))


def make_data_root(tmp_path, *, years=6.05, price=2000.0, contract=100.0):
    """预筛用的最小数据根：specs 快照 + D1/H1 K 线（含 meta 水位）。"""
    root = tmp_path / "data"
    root.mkdir(parents=True, exist_ok=True)
    (root / "specs").mkdir(exist_ok=True)
    (root / "specs" / "latest.json").write_text(json.dumps(
        {SYMBOL: {"contract_size": contract, "volume_min": 0.01,
                  "volume_step": 0.01, "volume_max": 100.0}}),
        encoding="utf-8")
    for tf, freq in (("D1", "D"), ("H1", "h")):
        n = int(years * 365.25) if tf == "D1" else int(years * 365.25 * 24)
        times = pd.date_range(end="2026-01-01", periods=n, freq=freq)
        df = pd.DataFrame({"time": times, "open": price, "high": price,
                           "low": price, "close": price, "tick_volume": 1,
                           "spread": 2, "real_volume": 0})
        sdir = root / "bars" / SYMBOL / tf
        sdir.mkdir(parents=True)
        df.to_parquet(sdir / "data.parquet")
        (sdir / "meta.json").write_text(json.dumps({
            "symbol": SYMBOL, "timeframe": tf, "rows": n,
            "first_time": times[0].isoformat(),
            "last_closed_time": times[-1].isoformat()}), encoding="utf-8")
    return root


# 事前成本手算（与引擎 A/G1 同口径：点差是来回全幅、每来回恰收一次；
# 滑点双边；contract=100、ref=2000、点差 0.0002、滑点 0.1×点差、
# 佣金 6/手、隔夜 1 夜×max(|−5|,|−1|)=5/手）：
#   成本 = 0.0002（点差一次，取入/出场较不利侧） + 2×0.00002（滑点）
#        + 6/(100×2000) + 5/(100×2000)
#        = 0.0002 + 0.00004 + 0.00003 + 0.000025 = 0.000295
#   毛利 0.003 → 倍数 ≈ 10.17 ≥ 3；毛利 0.0008 → ≈ 2.71 < 3
EXPECTED_COST = 0.000295
YEARS_D1 = 6.05            # 覆盖年数（研究库实际跨度）


# ---------------------------------------------------------------- 装载

def test_load_full_candidate(tmp_path):
    c = load_candidate(write_candidate(tmp_path))
    assert c.id == "C001"
    assert c.title == "月末效应（TESTUSD）"
    assert c.mechanism_class == "flow_calendar"
    assert c.status == "registered"
    assert c.symbols == ("TESTUSD",)
    assert c.holding == "D1"
    assert c.literature.search_log.startswith("SSRN")
    assert c.literature.original == ("McConnell & Xu (2008), FAJ 64(2)",)
    assert c.literature.replications == ()
    assert c.literature.rebuttals == ()
    assert c.declared.events_per_year == 12
    assert c.declared.gross_return_per_event == pytest.approx(0.003)
    assert c.declared.expected_sharpe == pytest.approx(1.0)
    assert c.source_card == ""


def test_load_rejects_empty_search_log(tmp_path):
    body = CANDIDATE_TOML.replace(
        'search_log = "SSRN / Google Scholar：month-end effect equity；'
        '2026-10-27"',
        'search_log = ""')
    p = write_candidate(tmp_path, body)
    with pytest.raises(CandidateError, match="search_log"):
        load_candidate(p)


def test_load_rejects_missing_required_field(tmp_path):
    for drop in ('title = "月末效应（TESTUSD）"\n',
                 'mechanism_class = "flow_calendar"\n',
                 'status = "registered"\n',
                 'mechanism = "月末与月初的固定资金流（工资、养老金、再平衡）"\n',
                 'holding = "D1"\n',
                 'symbols = ["TESTUSD"]\n'):
        p = write_candidate(tmp_path, CANDIDATE_TOML.replace(drop, ""))
        with pytest.raises(CandidateError):
            load_candidate(p)


def test_load_rejects_precheck_section(tmp_path):
    """[precheck] 是工具产物（bt candidate check 生成 json），候选不手填。"""
    body = CANDIDATE_TOML + '\n[precheck]\ndecision = "pass"\n'
    p = write_candidate(tmp_path, body)
    with pytest.raises(CandidateError, match="precheck"):
        load_candidate(p)


def test_load_rejects_bad_enum_types(tmp_path):
    cases = {
        "mechanism_class": 'mechanism_class = "momentum"',   # 不在七类枚举
        "status": 'status = "alive"',
        "holding": 'holding = "H7"',
    }
    for key, line in cases.items():
        body = CANDIDATE_TOML
        for old in body.splitlines():
            if old.startswith(key + " "):
                body = body.replace(old, line)
                break
        p = write_candidate(tmp_path, body)
        with pytest.raises(CandidateError, match=key):
            load_candidate(p)


def test_load_rejects_unknown_top_key_and_filename_mismatch(tmp_path):
    p = write_candidate(tmp_path, CANDIDATE_TOML + 'surprise = 1\n',
                        name="C001.toml")
    with pytest.raises(CandidateError, match="surprise"):
        load_candidate(p)
    p2 = write_candidate(tmp_path, CANDIDATE_TOML, name="C002.toml")
    with pytest.raises(CandidateError, match="C002"):
        load_candidate(p2)


def test_load_rejects_research_status_without_declared(tmp_path):
    body = CANDIDATE_TOML.split("[declared]")[0]
    p = write_candidate(tmp_path, body)
    with pytest.raises(CandidateError, match="declared"):
        load_candidate(p)


def test_load_rejects_bad_declared_values(tmp_path):
    for bad in ("events_per_year = 0",
                "gross_return_per_event = 0.0",
                "entry_hour_ny = 24",
                "exit_hour_ny = -1",
                "overnight_nights = -1",
                "expected_sharpe = -0.5"):
        good = [l for l in CANDIDATE_TOML.splitlines()
                if l.startswith(bad.split(" =")[0] + " ")][0]
        p = write_candidate(tmp_path, CANDIDATE_TOML.replace(good, bad))
        with pytest.raises(CandidateError):
            load_candidate(p)


def test_load_rejects_declared_without_window_bars(tmp_path):
    """T84/K4 失败测试：[declared].window_bars（事件窗持仓 bar 数）必填
    ——σ 按事件窗长度取（K4：单根 bar 口径少算 √h、隐含夏普多算 √h），
    缺字段的登记装载即报错（fail-fast，不静默按 1 根算）。"""
    body = CANDIDATE_TOML.replace("window_bars = 4\n", "")
    with pytest.raises(CandidateError, match="window_bars"):
        load_candidate(write_candidate(tmp_path, body))


@pytest.mark.parametrize("bad", ["window_bars = 0", "window_bars = -1",
                                 "window_bars = 1.5", "window_bars = true",
                                 'window_bars = "4"'])
def test_load_rejects_bad_window_bars(tmp_path, bad):
    """window_bars 必须是 ≥1 的整数（bar 数）。"""
    body = CANDIDATE_TOML.replace("window_bars = 4", bad)
    with pytest.raises(CandidateError, match="window_bars"):
        load_candidate(write_candidate(tmp_path, body))


def test_load_optional_observed_in_sample(tmp_path):
    """T84/K3 失败测试：可选顶层字段 observed_in_sample——候选机制在
    本项目样本内被看过时如实登记来源（C001 = "T66 §3.2"）；缺省空串
    兼容（未看过的候选不带）；非字符串拒绝。"""
    body = CANDIDATE_TOML.replace(
        'mechanism = "月末与月初的固定资金流（工资、养老金、再平衡）"',
        'mechanism = "月末与月初的固定资金流（工资、养老金、再平衡）"\n'
        'observed_in_sample = "T66 §3.2"')
    c = load_candidate(write_candidate(tmp_path, body))
    assert c.observed_in_sample == "T66 §3.2"
    assert load_candidate(write_candidate(tmp_path)).observed_in_sample == ""
    bad = body.replace('observed_in_sample = "T66 §3.2"',
                       "observed_in_sample = 3")
    with pytest.raises(CandidateError, match="observed_in_sample"):
        load_candidate(write_candidate(tmp_path, bad))


def test_repo_research_candidates_declare_window_bars():
    """T84/K4、K8、K3：仓库研究候选 C001/C002 带 window_bars（σ 按事件窗
    取）；C001 的 K8 事件窗 = 4 根 D1（第 −2 日纽约 16:00 收盘 → 第 +3 日
    16:00 收盘）、样本内观察如实登记、screened_fail 理由改按目标市场
    发表后证据（Han, Han & Tian 2025：效应 2001 年后消失）。"""
    c1 = load_candidate(REPO_ROOT / "research" / "candidates" / "C001.toml")
    assert c1.status == "screened_fail"
    assert c1.declared.window_bars == 4
    assert c1.observed_in_sample.strip()
    raw = (REPO_ROOT / "research" / "candidates" / "C001.toml").read_text(
        encoding="utf-8")
    assert "2001" in raw and "Han" in raw        # 发表后消失理由（R13 §4.3）
    c2 = load_candidate(REPO_ROOT / "research" / "candidates" / "C002.toml")
    assert c2.declared.window_bars == 1          # 定盘前 1 小时 = 1 根 H1


def test_load_literature_required_keys(tmp_path):
    """original 必须非空；published_effect / expected_effect 必须有据（非空）。"""
    for bad, key in [
        ('original = []', "original"),
        ('published_effect = ""', "published_effect"),
        ('expected_effect = ""', "expected_effect"),
    ]:
        good = [l for l in CANDIDATE_TOML.splitlines()
                if l.startswith(key + " ")][0]
        p = write_candidate(tmp_path, CANDIDATE_TOML.replace(good, bad))
        with pytest.raises(CandidateError, match=key):
            load_candidate(p)


def test_load_specimen_without_declared_is_compatible(tmp_path):
    """T61 的 specimen/withdrawn 登记不带数值声明——装载兼容（precheck 短路）。"""
    body = """\
id = "C950"
title = "标定样品登记（装载兼容）"
mechanism_class = "risk_premium"
status = "specimen"
symbols = ["TESTUSD"]
holding = "H1"
source_card = "H001"
mechanism = "持有风险溢价；只作对照线"

[literature]
original = ["不适用——标定样品（T61/D30），非研究候选"]
replications = []
rebuttals = []
search_log = "不适用——标定样品登记，不进入研究轮次，无文献检索"
published_effect = "不适用——样品不承载预期效应声明（蓝图 v4 §6.1）"
expected_effect = "不适用——样品结果只作测量证据，不作为方向依据"
"""
    c = load_candidate(write_candidate(tmp_path, body, name="C950.toml"))
    assert c.status == "specimen"
    assert c.declared is None


@pytest.mark.parametrize("cid,status", [
    ("C900", "specimen"), ("C901", "specimen"), ("C902", "specimen"),
    ("C903", "specimen"), ("C904", "specimen"), ("C905", "specimen"),
    ("C906", "specimen"), ("C907", "withdrawn"), ("C908", "withdrawn"),
    ("C909", "withdrawn"),
])
def test_t61_registrations_load_with_full_loader(cid, status):
    """C900–C909 迁移到附录 C 完整装载：字段补齐、状态不变。"""
    c = load_candidate(REPO_ROOT / "research" / "candidates" / f"{cid}.toml")
    assert c.id == cid
    assert c.status == status


# ---------------------------------------------------------------- 预筛

def test_precheck_pass(tmp_path):
    c = load_candidate(write_candidate(tmp_path))
    root = make_data_root(tmp_path)
    res = precheck(c, data_root=root, cost_model=make_costs(),
                   research_years={f"{SYMBOL}/D1": YEARS_D1})
    assert res.decision == "pass"
    cost = res.checks["ex_ante_cost"]
    assert cost["multiple"] == pytest.approx(0.003 / EXPECTED_COST)
    assert cost["multiple"] >= 3.0
    assert cost["per_symbol"][SYMBOL]["reference_price"] \
        == pytest.approx(2000.0)
    assert cost["per_symbol"][SYMBOL]["overnight"] == pytest.approx(0.000025)
    assert res.checks["power"]["power"] == pytest.approx(
        candidates.power.power_one_sided(1.0, YEARS_D1))
    assert res.checks["power"]["years"] == pytest.approx(YEARS_D1)
    assert res.checks["holding"]["passed"] is True
    assert res.checks["literature"]["passed"] is True
    assert res.checks["mechanism_gap"]["blocking"] is False


def test_precheck_holding_below_h1_fails(tmp_path):
    body = CANDIDATE_TOML.replace('holding = "D1"', 'holding = "M15"')
    c = load_candidate(write_candidate(tmp_path, body))
    res = precheck(c, data_root=make_data_root(tmp_path),
                   cost_model=make_costs(),
                   research_years={f"{SYMBOL}/M15": YEARS_D1})
    assert res.decision == "fail"
    assert res.checks["holding"]["passed"] is False
    assert any("H1" in r for r in res.reasons)


def test_precheck_cost_multiple_below_3_fails(tmp_path):
    body = CANDIDATE_TOML.replace("gross_return_per_event = 0.003",
                                  "gross_return_per_event = 0.0008")
    c = load_candidate(write_candidate(tmp_path, body))
    res = precheck(c, data_root=make_data_root(tmp_path),
                   cost_model=make_costs(),
                   research_years={f"{SYMBOL}/D1": YEARS_D1})
    assert res.decision == "fail"
    assert res.checks["ex_ante_cost"]["passed"] is False
    assert res.checks["ex_ante_cost"]["multiple"] \
        == pytest.approx(0.0008 / EXPECTED_COST)
    assert any("成本" in r for r in res.reasons)


def test_precheck_low_power_waits_for_data(tmp_path):
    body = CANDIDATE_TOML.replace("expected_sharpe = 1.0",
                                  "expected_sharpe = 0.5")
    c = load_candidate(write_candidate(tmp_path, body))
    res = precheck(c, data_root=make_data_root(tmp_path),
                   cost_model=make_costs(),
                   research_years={f"{SYMBOL}/D1": YEARS_D1})
    assert res.decision == "waiting_data"
    assert res.checks["power"]["passed"] is False
    assert res.checks["power"]["power"] < 0.5
    # 其余硬条件仍逐条给出（成本、持仓、文献都是过的）
    assert res.checks["ex_ante_cost"]["passed"] is True
    assert res.checks["holding"]["passed"] is True


def test_precheck_no_research_coverage_waits_for_data(tmp_path):
    c = load_candidate(write_candidate(tmp_path))
    res = precheck(c, data_root=make_data_root(tmp_path),
                   cost_model=make_costs(), research_years={})
    assert res.decision == "waiting_data"
    assert res.checks["power"]["years"] == 0.0
    assert any("覆盖" in r for r in res.reasons)


def test_precheck_mechanism_gap_is_note_not_block(tmp_path):
    c = load_candidate(write_candidate(tmp_path))
    res = precheck(c, data_root=make_data_root(tmp_path),
                   cost_model=make_costs(),
                   research_years={f"{SYMBOL}/D1": YEARS_D1},
                   active_mechanisms={"flow_calendar"})
    assert res.decision == "pass"          # 排序类理由不阻塞
    assert any("排后" in r for r in res.reasons)


def test_precheck_specimen_short_circuits_to_fail(tmp_path):
    body = """\
id = "C950"
title = "标定样品登记"
mechanism_class = "risk_premium"
status = "specimen"
symbols = ["TESTUSD"]
holding = "H1"
source_card = "H001"
mechanism = "持有风险溢价"

[literature]
original = ["不适用——标定样品，非研究候选"]
replications = []
rebuttals = []
search_log = "不适用——标定样品登记，无文献检索"
published_effect = "不适用"
expected_effect = "不适用"
"""
    c = load_candidate(write_candidate(tmp_path, body, name="C950.toml"))
    res = precheck(c, data_root=make_data_root(tmp_path),
                   cost_model=make_costs(), research_years={})
    assert res.decision == "fail"
    assert any("specimen" in r for r in res.reasons)


# ---------------------------------------------------------------- 覆盖年数

def test_coverage_years_from_research_store_layout(tmp_path):
    root = make_data_root(tmp_path)
    cov = candidates.coverage_years(root, [SYMBOL], ["D1"])
    assert cov == {f"{SYMBOL}/D1": pytest.approx(YEARS_D1, abs=0.02)}
    assert candidates.coverage_years(root, ["NOPE"], ["D1"]) == {}


def test_coverage_years_falls_back_to_parquet_without_meta(tmp_path):
    root = make_data_root(tmp_path)
    (root / "bars" / SYMBOL / "D1" / "meta.json").unlink()
    cov = candidates.coverage_years(root, [SYMBOL], ["D1"])
    assert cov == {f"{SYMBOL}/D1": pytest.approx(YEARS_D1, abs=0.02)}


# ---------------------------------------------------------------- 台账链

def test_candidate_check_recorded_in_hash_chain(tmp_path):
    with Ledger(tmp_path / "ledger.sqlite") as led:
        rid = led.record_candidate_check(
            "C001", decision="pass",
            result={"candidate_id": "C001", "decision": "pass",
                    "checks": {"power": {"power": 0.64}}})
        assert rid >= 1
        assert led.verify_chain() == []
        rows = led.candidate_checks("C001")
        assert len(rows) == 1
        assert rows[0]["decision"] == "pass"
        assert json.loads(rows[0]["result_json"])["decision"] == "pass"
        # 决策枚举外的值拒绝
        with pytest.raises(ValueError):
            led.record_candidate_check("C001", decision="maybe", result={})


def test_candidate_check_chain_detects_tamper(tmp_path):
    p = tmp_path / "ledger.sqlite"
    with Ledger(p) as led:
        led.record_candidate_check("C001", decision="fail", result={"x": 1})
    with Ledger(p) as led:   # 绕过写方法改内容 → 链上报出
        led.conn.execute("UPDATE candidate_checks SET decision='pass'"
                         " WHERE candidate_id='C001'")
        led.conn.commit()
        problems = led.verify_chain()
    assert any("candidate_checks" in s for s in problems)


def test_candidate_check_after_runs_chain_intact(tmp_path):
    """与既有 runs/gates/lockbox_uses 链共存：两条链都完好。"""
    with Ledger(tmp_path / "ledger.sqlite") as led:
        led.record_run(card_id="H099", card_version=1, strategy="s",
                       params={}, symbol="XAUUSD", timeframe="H1",
                       data_first=None, data_last=None, data_sha256="d",
                       engine_version="e", cost_model_version="c",
                       gates_hash="", metrics={"ok_metric": 1}, actor="bt")
        led.record_candidate_check("C001", decision="pass", result={})
        assert led.verify_chain() == []
        # 删 candidate_checks 行 → 删行被快照比对报出
        led.conn.execute("DELETE FROM candidate_checks")
        led.conn.commit()
        problems = led.verify_chain()
    assert any("candidate_checks" in s and "删" in s for s in problems)


# ---------------------------------------------------------------- CLI

def make_cli_root(tmp_path):
    """CLI 端到端用的研究库样式数据根：K 线 + specs + 成本缓存 + 校准。"""
    root = make_data_root(tmp_path)
    spec = json.loads((root / "specs" / "latest.json").read_text(
        encoding="utf-8"))
    spec[SYMBOL].update({"swap_mode": 4, "swap_long": -5.0,
                         "swap_short": -1.0, "swap_rollover3days": 3,
                         "point": 0.01})
    (root / "specs" / "latest.json").write_text(json.dumps(spec),
                                                encoding="utf-8")
    stats = {h: SpreadStat(median=0.0002, p90=0.0002, p99=0.0002,
                           n=1000, n_days=20) for h in range(24)}
    model = SpreadModel({SYMBOL: stats}, {SYMBOL: 0.0002}, mode="relative")
    import run_backtest as rb   # bt 装载时已把 scripts 目录挂上 sys.path
    rb.save_spread_model_cache(
        rb.spread_model_cache_path(str(root)), model,
        {SYMBOL: {"rows": 0, "first_msc": 0, "last_msc": 0}})
    (root / "costs" / "calibrated.json").write_text(json.dumps({
        "account_kind": "real",     # T83/K6：隔夜费实测只认 real 账户
        "symbols": {SYMBOL: {
            "commission": {"round_trip_per_lot": 6.0},
            "swap": {"free_of_swap": None,
                     "long": {"per_lot_per_night": -5.0},
                     "short": {"per_lot_per_night": -1.0}}}}}),
        encoding="utf-8")
    return root


def test_bt_candidate_check_pass_writes_json_and_ledger(tmp_path, capsys):
    mod = load_bt()
    cand = write_candidate(tmp_path)
    root = make_cli_root(tmp_path)
    ledger = tmp_path / "ledger.sqlite"
    rc = mod.main(["candidate", "check", "C001",
                   "--candidates-dir", str(cand.parent),
                   "--data-root", str(root), "--ledger", str(ledger)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "pass" in out
    payload = json.loads((cand.parent / "C001.precheck.json")
                         .read_text(encoding="utf-8"))
    assert payload["decision"] == "pass"
    assert payload["candidate_id"] == "C001"
    assert payload["checks"]["ex_ante_cost"]["multiple"] \
        == pytest.approx(0.003 / EXPECTED_COST)
    with Ledger(ledger) as led:
        rows = led.candidate_checks("C001")
        assert len(rows) == 1
        assert rows[0]["decision"] == "pass"
        assert led.verify_chain() == []


def test_bt_candidate_check_fail_and_waiting_exit_codes(tmp_path, capsys):
    mod = load_bt()
    root = make_cli_root(tmp_path)
    # 成本倍数 < 3 → fail（退出码 1）
    body = CANDIDATE_TOML.replace("gross_return_per_event = 0.003",
                                  "gross_return_per_event = 0.0008")
    cand = write_candidate(tmp_path, body)
    rc = mod.main(["candidate", "check", "C001",
                   "--candidates-dir", str(cand.parent),
                   "--data-root", str(root),
                   "--ledger", str(tmp_path / "l1.sqlite")])
    assert rc == 1
    assert json.loads((cand.parent / "C001.precheck.json")
                      .read_text(encoding="utf-8"))["decision"] == "fail"
    # 检验力 < 50% → waiting_data（退出码 2）
    body = CANDIDATE_TOML.replace("expected_sharpe = 1.0",
                                  "expected_sharpe = 0.5")
    body = body.replace('id = "C001"', 'id = "C003"')
    cand2 = write_candidate(tmp_path, body, name="C003.toml")
    rc = mod.main(["candidate", "check", "C003",
                   "--candidates-dir", str(cand2.parent),
                   "--data-root", str(root),
                   "--ledger", str(tmp_path / "l2.sqlite")])
    assert rc == 2
    assert json.loads((cand2.parent / "C003.precheck.json")
                      .read_text(encoding="utf-8"))["decision"] \
        == "waiting_data"


def test_bt_candidate_check_empty_search_log_rejected(tmp_path, capsys):
    mod = load_bt()
    body = CANDIDATE_TOML.replace(
        'search_log = "SSRN / Google Scholar：month-end effect equity；'
        '2026-10-27"', 'search_log = ""')
    cand = write_candidate(tmp_path, body)
    root = make_cli_root(tmp_path)
    ledger = tmp_path / "ledger.sqlite"
    rc = mod.main(["candidate", "check", "C001",
                   "--candidates-dir", str(cand.parent),
                   "--data-root", str(root), "--ledger", str(ledger)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "search_log" in out
    assert not (cand.parent / "C001.precheck.json").exists()
    assert not ledger.exists()


def test_bt_candidate_check_repo_c900_specimen(tmp_path, capsys):
    """C900（标定样品）走 CLI：结论 fail（非研究候选），如实入台账。

    用仓库文件的临时副本跑——预筛结果 json 写在候选旁边，不污染仓库目录。
    """
    mod = load_bt()
    root = make_cli_root(tmp_path)
    cdir = tmp_path / "candidates"
    cdir.mkdir()
    (cdir / "C900.toml").write_text(
        (REPO_ROOT / "research" / "candidates" / "C900.toml")
        .read_text(encoding="utf-8"), encoding="utf-8")
    rc = mod.main(["candidate", "check", "C900",
                   "--candidates-dir", str(cdir),
                   "--data-root", str(root),
                   "--ledger", str(tmp_path / "l.sqlite")])
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "specimen" in out


# ================================================ T80/G2、G3：隐含夏普与外部年数

def make_volatile_data_root(tmp_path, *, lo=2000.0, hi=2100.0):
    """D1 收盘交替 lo/hi（研究年数不变、σ 可控>0）——隐含夏普的取数场景。"""
    root = make_data_root(tmp_path)
    p = root / "bars" / SYMBOL / "D1" / "data.parquet"
    df = pd.read_parquet(p)
    df["close"] = [lo if i % 2 == 0 else hi for i in range(len(df))]
    df.to_parquet(p)
    return root


def test_unconditional_volatility_hand_computed(tmp_path):
    """T80/G2 失败测试：σ = 候选持仓周期 bar 的相邻收盘相对收益全体
    （随机时点抽样的总体极限，品种性质）——小序列手算闭式核对；恒价
    序列 σ=0；缺序列 None。"""
    import math as _math

    root = tmp_path / "d"
    d = root / "bars" / SYMBOL / "D1"
    d.mkdir(parents=True)
    n = 8                                    # 7 个收益：4×(+1/10)、3×(−1/11)
    times = pd.date_range("2026-01-01", periods=n, freq="D")
    closes = [100.0, 110.0] * 4
    pd.DataFrame({"time": times, "open": closes, "high": closes,
                  "low": closes, "close": closes, "tick_volume": 1,
                  "spread": 0, "real_volume": 0}).to_parquet(
        d / "data.parquet")
    sigma = candidates.unconditional_volatility(root, SYMBOL, "D1")
    # 手算：均值 1/55；样本方差 (4×(9/110)² + 3×(12/110)²)/6 = 756/72600
    assert sigma == pytest.approx(_math.sqrt(756.0 / 72600.0))
    # 恒价序列（现行夹具形态）→ σ=0（隐含夏普无从折算，按声明夏普计）
    root2 = make_data_root(tmp_path, price=2000.0)
    assert candidates.unconditional_volatility(root2, SYMBOL, "D1") == 0.0
    assert candidates.unconditional_volatility(
        tmp_path / "nope", SYMBOL, "D1") is None


def test_precheck_power_uses_min_of_declared_and_implied_sharpe(tmp_path):
    """T80/G2 失败测试 1：声明夏普 1.5、但（毛收益−事前来回成本）÷σ×
    √事件数 折算的隐含夏普 ≈0.2 → 检验力按隐含（较小者）算（waiting_data）；
    两个夏普与 σ 口径都写进 precheck JSON。"""
    import math as _math

    body = CANDIDATE_TOML.replace("expected_sharpe = 1.0",
                                  "expected_sharpe = 1.5"
                                  ).replace("window_bars = 4", "window_bars = 1")
    c = load_candidate(write_candidate(tmp_path, body))
    root = make_volatile_data_root(tmp_path)
    sigma = candidates.unconditional_volatility(root, SYMBOL, "D1")
    assert sigma > 0.04
    implied = (0.003 - EXPECTED_COST) / sigma * _math.sqrt(12.0)
    assert implied < 0.5                        # 构造：隐含远低于声明 1.5
    res = precheck(c, data_root=root, cost_model=make_costs(),
                   research_years={f"{SYMBOL}/D1": YEARS_D1})
    pw = res.checks["power"]
    assert pw["expected_sharpe"] == pytest.approx(1.5)
    assert pw["sigma_by_symbol"][SYMBOL] == pytest.approx(sigma)
    assert pw["implied_sharpe_by_symbol"][SYMBOL] == pytest.approx(implied)
    assert pw["implied_sharpe"] == pytest.approx(implied)
    assert pw["sharpe_used"] == pytest.approx(implied)   # min(声明, 隐含)
    assert pw["power"] == pytest.approx(
        candidates.power.power_one_sided(implied, YEARS_D1))
    assert res.decision == "waiting_data"
    assert any("隐含" in r for r in res.reasons)
    assert "随机时点" in pw["sigma_method"]


def test_precheck_power_uses_declared_when_sigma_zero(tmp_path):
    """T80/G2：σ=0（恒价序列/序列不可用）→ 隐含夏普记 None、按声明夏普
    计算（现行结果逐位不变——test_precheck_pass 的语义由此钉住）。"""
    c = load_candidate(write_candidate(tmp_path))
    root = make_data_root(tmp_path)             # 恒价 → σ=0
    res = precheck(c, data_root=root, cost_model=make_costs(),
                   research_years={f"{SYMBOL}/D1": YEARS_D1})
    pw = res.checks["power"]
    assert pw["implied_sharpe"] is None
    assert pw["sigma_by_symbol"][SYMBOL] == 0.0
    assert pw["sharpe_used"] == pytest.approx(1.0)
    assert pw["power"] == pytest.approx(
        candidates.power.power_one_sided(1.0, YEARS_D1))
    assert res.decision == "pass"


# ================================================ T84/K4：σ 按事件窗与时段取

def make_rw_data_root(tmp_path, *, n=3000, vol=0.01, s0=2000.0):
    """独立同分布随机游走 D1 数据根（σ>0 且窗长口径可手算：σ_h≈σ_1·√h）。"""
    rng = np.random.default_rng(20261028)
    closes = s0 * np.cumprod(1.0 + rng.normal(0.0, vol, n))
    root = tmp_path / "data"
    root.mkdir(parents=True, exist_ok=True)
    (root / "specs").mkdir(exist_ok=True)
    (root / "specs" / "latest.json").write_text(json.dumps(
        {SYMBOL: {"contract_size": 100.0, "volume_min": 0.01,
                  "volume_step": 0.01, "volume_max": 100.0}}),
        encoding="utf-8")
    times = pd.date_range(end="2026-01-01", periods=n, freq="D")
    df = pd.DataFrame({"time": times, "open": closes, "high": closes,
                       "low": closes, "close": closes, "tick_volume": 1,
                       "spread": 2, "real_volume": 0})
    sdir = root / "bars" / SYMBOL / "D1"
    sdir.mkdir(parents=True)
    df.to_parquet(sdir / "data.parquet")
    (sdir / "meta.json").write_text(json.dumps({
        "symbol": SYMBOL, "timeframe": "D1", "rows": n,
        "first_time": times[0].isoformat(),
        "last_closed_time": times[-1].isoformat()}), encoding="utf-8")
    return root


def test_precheck_sigma_uses_declared_window_bars(tmp_path):
    """T84/K4 失败测试：隐含夏普的 σ 按声明的 window_bars 事件窗取（与
    T66 σ_w 同一函数）——独立同分布合成库上 4 日窗 σ ≈ 2×单根 D1、隐含
    夏普 ≈ 单根口径的一半；预筛检验力仍按**单次**判定（蓝图 §6.2 条件
    4，n_tests=1——α/N 只用于事件模式样本量规则与开轮复核）。"""
    import math as _math

    root = make_rw_data_root(tmp_path)
    years = 3000 / 365.25
    res = {}
    for cid, wb in (("C001", 1), ("C004", 4)):
        body = (CANDIDATE_TOML
                .replace('id = "C001"', f'id = "{cid}"')
                .replace("window_bars = 4", f"window_bars = {wb}"))
        c = load_candidate(write_candidate(tmp_path, body, name=f"{cid}.toml"))
        res[wb] = precheck(c, data_root=root, cost_model=make_costs(),
                           research_years={f"{SYMBOL}/D1": years})
    s1 = res[1].checks["power"]["sigma_by_symbol"][SYMBOL]
    s4 = res[4].checks["power"]["sigma_by_symbol"][SYMBOL]
    assert s1 == pytest.approx(0.01, rel=0.1)
    assert s4 == pytest.approx(2.0 * s1, rel=0.1), \
        f"4 日窗 σ {s4:.4f} 应 ≈ 2×单根 D1 σ（{2.0 * s1:.4f}）"
    assert res[4].checks["power"]["sigma_window_bars"] == 4
    assert res[4].checks["power"]["n_tests"] == 1     # 预筛仍按单次判定
    imp1 = res[1].checks["power"]["implied_sharpe"]
    imp4 = res[4].checks["power"]["implied_sharpe"]
    assert imp4 == pytest.approx(imp1 / 2.0, rel=0.1)
    # 手算对账：隐含夏普 =（毛收益 − 事前来回成本）÷σ_4 × √12
    # （成本按参考价折算——随机游走库的参考价≠2000，从 checks 里取实测值）
    cost = (res[4].checks["ex_ante_cost"]["per_symbol"][SYMBOL]
            ["round_trip_cost"])
    assert imp4 == pytest.approx(
        (0.003 - cost) / s4 * _math.sqrt(12.0))


def test_precheck_sigma_intraday_uses_entry_hour_window(tmp_path):
    """T84/K4 失败测试：日内候选的 σ 按 entry_hour_ny 取**同一时段起点**
    的窗口（窗口起点=锚 bar 收盘时刻=入场成交时点）——合成时段异方差
    （纽约 10→11 点窗波动 3 倍于其余小时）下，定盘前一时段的 σ 显著
    高于全天平均（C002 的 K4 更正口径）。"""
    rng = np.random.default_rng(20261028)
    days = pd.date_range("2021-01-04", periods=40, freq="B")  # EST 无 DST 切换
    n = len(days) * 24
    vols = np.full(n, 0.0004)
    times = pd.DatetimeIndex(
        [d + pd.Timedelta(hours=h) for d in days for h in range(24)])
    vols[np.asarray([t.hour for t in times]) == 15] = 0.0012  # 纽约 10 点窗
    closes = 1.10 * np.cumprod(1.0 + rng.normal(0.0, 1.0, n) * vols)
    root = tmp_path / "data"
    root.mkdir(parents=True)
    (root / "specs").mkdir()
    (root / "specs" / "latest.json").write_text(json.dumps(
        {SYMBOL: {"contract_size": 100.0, "volume_min": 0.01,
                  "volume_step": 0.01, "volume_max": 100.0}}),
        encoding="utf-8")
    df = pd.DataFrame({"time": times, "open": closes, "high": closes,
                       "low": closes, "close": closes, "tick_volume": 1,
                       "spread": 2, "real_volume": 0})
    sdir = root / "bars" / SYMBOL / "H1"
    sdir.mkdir(parents=True)
    df.to_parquet(sdir / "data.parquet")

    body = (CANDIDATE_TOML
            .replace('holding = "D1"', 'holding = "H1"')
            .replace("entry_hour_ny = 16", "entry_hour_ny = 10")
            .replace("exit_hour_ny = 10", "exit_hour_ny = 11")
            .replace("window_bars = 4", "window_bars = 1"))
    c = load_candidate(write_candidate(tmp_path, body))
    res = precheck(c, data_root=root, cost_model=make_costs(),
                   research_years={f"{SYMBOL}/H1": 0.15})
    sigma_hour = res.checks["power"]["sigma_by_symbol"][SYMBOL]
    sigma_all = candidates.unconditional_volatility(root, SYMBOL, "H1")
    assert sigma_hour == pytest.approx(0.0012, rel=0.25)
    assert sigma_hour > 1.5 * sigma_all, \
        f"定盘前一小时 σ {sigma_hour:.5f} 应显著高于全天平均 {sigma_all:.5f}"
    assert res.checks["power"]["sigma_entry_hour_ny"] == 10


def test_precheck_cost_condition_numpy_false_or_none_fails(
        tmp_path, monkeypatch):
    """R13/K10 失败测试：成本条件为 numpy 布尔值 False 或 None 时判 fail
    ——0d92448 把判定块写成 `cost_ok is False`，np.False_ is False 为假
    （不通过被漏判）、None 落到 waiting_data；恢复 `not cost_ok` 写法后
    两者都判 fail（成本不过/算不出都不是"待数据"，K10）。"""
    c = load_candidate(write_candidate(tmp_path))
    root = make_data_root(tmp_path)
    years = {f"{SYMBOL}/D1": YEARS_D1}
    base = {"reference_price": 2000.0, "reference_timeframe": "D1",
            "contract_size": 100.0, "spread_round_trip": 0.0002,
            "spread_entry_frac": 0.0002, "spread_exit_frac": 0.0002,
            "slippage_entry": 0.00002, "slippage_exit": 0.00002,
            "commission": 0.00003, "overnight": 0.000025,
            "round_trip_cost": 0.000295, "multiple": 0.5}
    # 1) multiple 为 np.float64（成本链路返回 numpy 标量）→ cost_ok=np.False_
    fake_np = dict(base, multiple=np.float64(0.5))
    monkeypatch.setattr(candidates, "_ex_ante_cost_per_symbol",
                        lambda *a, **k: fake_np)
    res = precheck(c, data_root=root, cost_model=make_costs(),
                   research_years=years)
    assert res.decision == "fail", \
        f"np.False_ 的成本不通过须判 fail（不是 {res.decision}）"

    # 2) 成本无法计算（与门槛的比较结果为 None）→ cost_ok=None → fail
    #    （__format__ 让理由行的 {multiple:.2f} 仍可渲染——只测判定块，
    #     不让格式化在判定之前把异常抛出来）
    class _Uncomputable:
        def __ge__(self, other):
            return None

        def __format__(self, spec):
            return format(0.5, spec)

    fake_none = dict(base, multiple=_Uncomputable())
    monkeypatch.setattr(candidates, "_ex_ante_cost_per_symbol",
                        lambda *a, **k: fake_none)
    res2 = precheck(c, data_root=root, cost_model=make_costs(),
                    research_years=years)
    assert res2.decision == "fail", \
        f"成本无法计算（None）须判 fail（不是 {res2.decision}）"


def _write_ext(root, source, symbol, tf, *, first, days):
    """研究库 ext 布局 {root}/ext/<来源>/<品种>/<周期>/ 的最小序列。"""
    d = root / "ext" / source / symbol / tf
    d.mkdir(parents=True)
    times = pd.date_range(first, periods=days, freq="D")
    df = pd.DataFrame({"time": times, "open": 100.0, "high": 100.0,
                       "low": 100.0, "close": 100.0, "tick_volume": 1,
                       "spread": 0, "real_volume": 0})
    df.to_parquet(d / "data.parquet")
    manifest = {
        "source": source, "source_url": "https://example.test/x",
        "symbol": symbol, "timeframe": tf, "price_side": "mid",
        "original_timezone": "UTC", "dst_rule": "无", "terms_note": "测试",
        "downloaded_at_utc": "2026-10-27T00:00:00+00:00",
        "source_files": [{"name": "x.csv", "sha256": "0" * 64}],
        "converter_version": 1, "rows": days,
        "first_time": times[0].isoformat(),
        "last_time": times[-1].isoformat()}
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return d


def test_coverage_years_merges_approved_ext_segments(tmp_path):
    """T80/G3 失败测试 2：批准 ext 序列（fred）计入研究年数、逐段列来源
    与年数；未批准来源在场也不计。ext 段与主段**不相接**
    （并集=分段相加，本用例）；重叠并集口径见下一个用例。
    T87 起未批准示例改用 stooq（histdata 已按 R13 §5 有条件批准）。"""
    root = make_data_root(tmp_path)
    _write_ext(root, "fred", SYMBOL, "D1", first="2010-01-05", days=3500)
    _write_ext(root, "stooq", SYMBOL, "D1", first="1990-01-01", days=7300)
    key = f"{SYMBOL}/D1"
    cov = candidates.coverage_years(root, [SYMBOL], ["D1"])
    ext_years = 3499 / 365.25                 # 不相交接的 ext 段自身跨度
    assert cov[key] == pytest.approx(YEARS_D1 + ext_years, abs=0.05)
    segs = candidates.coverage_segments(root, [SYMBOL], ["D1"])
    by_src = {s["source"]: s for s in segs[key]}
    assert {"mt5", "fred"} <= set(by_src)
    assert by_src["mt5"]["counted"] is True
    assert by_src["fred"]["counted"] is True
    assert by_src["fred"]["years"] == pytest.approx(ext_years, abs=0.01)
    assert "fred" == by_src["fred"]["source"]          # 逐段列来源
    # 未批准来源：在场、可读，但不计入年数（白名单外）
    assert "stooq" in by_src
    assert by_src["stooq"]["counted"] is False
    assert by_src["stooq"]["years"] == pytest.approx(7300 / 365.25,
                                                     abs=0.01)
    # 没有外部序列时与现行一致（只有主段）
    root2 = make_data_root(tmp_path / "plain")
    cov2 = candidates.coverage_years(root2, [SYMBOL], ["D1"])
    assert cov2[key] == pytest.approx(YEARS_D1, abs=0.02)
    assert [s["source"] for s in
            candidates.coverage_segments(root2, [SYMBOL], ["D1"])[key]] \
        == ["mt5"]


def test_coverage_years_unions_overlapping_segments(tmp_path):
    """T80/G3 审核修正（Medium-High）失败测试：计入段做**日历区间并集**，
    重叠不双计——直接相加会把完全包含/部分重叠的覆盖虚增（真实库
    USTEC：fred 39.8 年完全包住 mt5 4.3 年 → 合并 39.8 而非 44.1；
    BTCUSD 部分重叠虚增 40%，足以把 waiting_data 翻成 pass）。

    - 场景 1：mt5 段完全落入 fred 段 → 合并年数 = fred 段年数；
    - 场景 2：部分重叠 → 合并 = [min(first), max(last)] 的连续并集跨度。
    """
    key = f"{SYMBOL}/D1"
    # 场景 1：fred 2015-01-01 起约 12 年（完全包住主段 ~2019-12..2026-01）
    root = make_data_root(tmp_path)
    _write_ext(root, "fred", SYMBOL, "D1", first="2015-01-01", days=4400)
    segs = candidates.coverage_segments(root, [SYMBOL], ["D1"])
    by_src = {s["source"]: s for s in segs[key]}
    assert by_src["fred"]["counted"] and by_src["mt5"]["counted"]
    # 主段确实完全落入 fred 段（夹具自检）
    assert pd.Timestamp(by_src["fred"]["first"]) \
        <= pd.Timestamp(by_src["mt5"]["first"])
    assert pd.Timestamp(by_src["mt5"]["last"]) \
        <= pd.Timestamp(by_src["fred"]["last"])
    cov = candidates.coverage_years(root, [SYMBOL], ["D1"])
    assert cov[key] == pytest.approx(by_src["fred"]["years"], abs=1e-9)
    assert cov[key] < by_src["fred"]["years"] + by_src["mt5"]["years"] - 1.0
    # 场景 2：fred 2010 年起约 12 年（止于 ~2022，与主段部分重叠）
    root2 = make_data_root(tmp_path / "part")
    _write_ext(root2, "fred", SYMBOL, "D1", first="2010-01-05", days=4383)
    segs2 = candidates.coverage_segments(root2, [SYMBOL], ["D1"])
    b2 = {s["source"]: s for s in segs2[key]}
    union_span = ((pd.Timestamp(b2["mt5"]["last"])
                   - pd.Timestamp(b2["fred"]["first"])).total_seconds()
                  / 365.25 / 86400.0)
    cov2 = candidates.coverage_years(root2, [SYMBOL], ["D1"])
    assert cov2[key] == pytest.approx(union_span, abs=1e-9)
    assert cov2[key] < b2["mt5"]["years"] + b2["fred"]["years"]  # 不双计


def test_precheck_json_carries_coverage_segments(tmp_path):
    """T80/G3：precheck JSON 的 power 检查逐段列出覆盖来源与年数，另带
    合并后总覆盖与合并口径说明（并集，重叠不双计）；隐含夏普字段同在。
    本用例 ext 段与主段不相接（并集=分段相加）。"""
    root = make_volatile_data_root(tmp_path)
    _write_ext(root, "fred", SYMBOL, "D1", first="2010-01-05", days=3500)
    segs = candidates.coverage_segments(root, [SYMBOL], ["D1"])
    years = {k: sum(s["years"] for s in v if s.get("counted"))
             for k, v in segs.items()}       # 不相接 → 并集 = 分段相加
    c = load_candidate(write_candidate(tmp_path))
    res = precheck(c, data_root=root, cost_model=make_costs(),
                   research_years=years,
                   research_coverage_segments=segs)
    pw = res.checks["power"]
    assert pw["years"] == pytest.approx(years[f"{SYMBOL}/D1"])
    srcs = {s["source"]: s["counted"]
            for s in pw["coverage_segments"][f"{SYMBOL}/D1"]}
    assert srcs == {"mt5": True, "fred": True}
    # 合并后总覆盖（按并集口径从逐段重算，与 years 同一值）+ 口径注明
    assert pw["coverage_merged_years"][f"{SYMBOL}/D1"] \
        == pytest.approx(years[f"{SYMBOL}/D1"])
    assert "并集" in pw["coverage_merge_method"]
    assert "implied_sharpe" in pw and "sigma_method" in pw
