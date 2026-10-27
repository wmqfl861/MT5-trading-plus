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
