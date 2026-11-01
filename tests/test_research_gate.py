"""T107 模块 A：研究门禁四态 + 实验指纹六维 + 验收阈值快照（失败测试先行）。

计划：docs/superpowers/plans/2026-11-01-t107-p0-gates-p210.md 模块 A。
全部合成/现行登记数据，不读研究库、不看真实结局。
"""
import hashlib
import json
from pathlib import Path

import pytest

from mt5quant.research.gate import (
    ACCEPTANCE_CONSTANTS,
    GateResult,
    experiment_fingerprint,
    research_gate,
)
from mt5quant.research.candidates import sample_size_evidence
from mt5quant.research.ledger import Ledger
from mt5quant.validation import prob_mode
from mt5quant.validation.prob_mode import METHOD_REGISTRY, PRIMARY_METHOD

REPO = Path(__file__).resolve().parents[1]

STRATEGY_REF = ("mt5quant.strategies.library.prob.level_decision:"
                "LevelDecisionS1Demo")

# 指纹的基准六维（每次只动一维做敏感性）
BASE_FP = dict(
    strategy_ref=STRATEGY_REF,
    card="P001-v1",
    model_dict={"schema_version": 1, "prior_strength": 100},
    bars_meta={"generation": "synthetic-v1",
               "content_sha256": "a" * 64, "bars": 9000},
    cost_version="costs-v3-probe",
    sessions_sha="b" * 64,
    params={"hypothesis": "S1:A1:follow>base", "sims": 200},
    features=["atr_pct_2000", "trend_sma200"],
    evaluator_sha="p1-v1#" + "c" * 16,
)


def _fp(**over):
    kw = dict(BASE_FP)
    kw.update(over)
    return experiment_fingerprint(**kw)


def _adopted_registry(monkeypatch, method_id="probe-method",
                      status="adopted"):
    """临时给 METHOD_REGISTRY 塞一个 adopted=True 的条目（四态转移表用；
    当前仓库三方法全 adopted=False，PASS/INSUFFICIENT_DATA 只能这样构造）。"""
    entry = {"status": status, "adopted": True, "note": "探针条目"}
    monkeypatch.setitem(prob_mode.METHOD_REGISTRY, method_id, entry)
    return method_id


# ---------------------------------------------------------------- 四态

def test_not_run_unknown_method():
    """未登记方法 → NOT_RUN（fail-closed：登记缺失不是放行理由）。"""
    r = research_gate(method_id="no-such-method", dp=0.05, days=10 ** 6)
    assert r.status == "NOT_RUN"
    assert any("不在 METHOD_REGISTRY" in x for x in r.reasons)


def test_not_run_calibrating_takes_priority_over_fail(monkeypatch):
    """优先序：登记在册且 status=calibrating（哪怕 adopted=False）→
    NOT_RUN 先于 FAIL。"""
    monkeypatch.setitem(
        prob_mode.METHOD_REGISTRY, "probe-calibrating",
        {"status": "calibrating", "adopted": False, "note": "标定中"})
    r = research_gate(method_id="probe-calibrating", dp=0.05, days=10 ** 6)
    assert r.status == "NOT_RUN"
    assert any("calibrating" in x for x in r.reasons)


@pytest.mark.parametrize("method_id", sorted(METHOD_REGISTRY))
def test_current_registry_all_fail(method_id):
    """现行登记三方法（全部 adopted=False）→ FAIL；且**任何** dp/days
    组合都到不了 PASS（校准/批准失败必须阻断——放行通道不存在）。"""
    for dp, days in ((None, None), (0.05, None), (0.05, 0), (0.05, 10 ** 6),
                     (0.03, 10 ** 9), (0.5, 10 ** 9), (0.025, 10 ** 9)):
        r = research_gate(method_id=method_id, dp=dp, days=days)
        assert r.status == "FAIL", (dp, days, r.status)
    assert r.reasons and any("adopted=False" in x for x in r.reasons)


def test_current_registry_never_pass_sweep():
    """全 not_adopted → 永不 PASS（钉死：当前仓库不存在 PASS 输入）。"""
    for method_id in list(METHOD_REGISTRY) + [PRIMARY_METHOD]:
        for dp in (0.02, 0.025, 0.03, 0.05, 0.2, 0.5):
            for days in (0, 1, 1000, 4000, 10 ** 6):
                assert research_gate(
                    method_id=method_id, dp=dp, days=days).status != "PASS"


def test_fail_priority_over_insufficient_data():
    """优先序：adopted=False → FAIL 先于 INSUFFICIENT_DATA（dp/days 全缺
    也不改判）。"""
    r = research_gate(method_id=PRIMARY_METHOD, dp=None, days=None)
    assert r.status == "FAIL"


def test_calibration_ok_false_blocks_even_adopted(monkeypatch):
    """显式 calibration_ok=False → FAIL（即便方法 adopted=True、样本量
    充分）——校准失败必须阻断，不存在任何参数组合能从 FAIL 到 PASS。"""
    mid = _adopted_registry(monkeypatch)
    r = research_gate(method_id=mid, dp=0.05, days=10 ** 6,
                      calibration_ok=False)
    assert r.status == "FAIL"
    assert any("calibration_ok=False" in x for x in r.reasons)


def test_acceptance_snapshot_drift_blocks_adopted_method(monkeypatch):
    """即使方法已获批且样本充分，验收阈值漂移也必须阻断。"""
    mid = _adopted_registry(monkeypatch)
    monkeypatch.setattr(
        "mt5quant.research.gate.acceptance_snapshot",
        lambda: {"constants": {"lb_z": 2.0}, "expected": {"lb_z": 1.0},
                 "sources": {}, "drift": ["lb_z"]})
    r = research_gate(method_id=mid, dp=0.05, days=10 ** 6)
    assert r.status == "FAIL"
    assert any("阈值" in reason and "漂移" in reason for reason in r.reasons)
    assert r.acceptance["drift"] == ["lb_z"]


def test_acceptance_snapshot_unavailable_fails_closed(monkeypatch):
    """验收快照无法读取时，不得退回默认常量继续放行。"""
    mid = _adopted_registry(monkeypatch)
    def unavailable():
        raise OSError("snapshot source unavailable")
    monkeypatch.setattr("mt5quant.research.gate.acceptance_snapshot",
                        unavailable)
    r = research_gate(method_id=mid, dp=0.05, days=10 ** 6)
    assert r.status == "FAIL"
    assert "快照不可用" in r.reasons[0]


def test_insufficient_data_when_adopted_but_inputs_missing(monkeypatch):
    """adopted=True 但 dp/days 给不出 → INSUFFICIENT_DATA（注明原因）。"""
    mid = _adopted_registry(monkeypatch)
    r = research_gate(method_id=mid, dp=None, days=5000)
    assert r.status == "INSUFFICIENT_DATA"
    assert any("dp" in x for x in r.reasons)
    r = research_gate(method_id=mid, dp=0.05, days=None)
    assert r.status == "INSUFFICIENT_DATA"
    assert any("days" in x for x in r.reasons)


@pytest.mark.parametrize("dp", [0.01, 0.025])
def test_insufficient_data_uncalibrated_and_lower_bound(monkeypatch, dp):
    """adopted=True 但证据 ∈ {lower_bound_only, uncalibrated} → 恒
    INSUFFICIENT_DATA（days 再大也不放行——P2-08 同一语义）。"""
    mid = _adopted_registry(monkeypatch)
    for days in (0, 4000, 10 ** 6, 10 ** 9):
        r = research_gate(method_id=mid, dp=dp, days=days)
        assert r.status == "INSUFFICIENT_DATA", (dp, days)


def test_pass_only_when_adopted_calibrated_and_days_met(monkeypatch):
    """PASS = adopted=True 且 calibrated_sufficient 且 days 达标（唯一通道，
    现行登记不可达——此处用探针条目构造）。"""
    mid = _adopted_registry(monkeypatch)
    decision, state, _detail = sample_size_evidence(0.05, 10 ** 6, n_hyp=3)
    assert decision == "pass" and state == "calibrated_sufficient"
    r = research_gate(method_id=mid, dp=0.05, days=10 ** 6, n_hyp=3)
    assert r.status == "PASS"
    assert isinstance(r, GateResult)
    assert r.method_id == mid


def test_gate_boundary_is_sample_size_evidence_boundary(monkeypatch):
    """门槛来源=sample_size_evidence 同一判定，不另立数字：全档 days 扫描
    上 gate 的 PASS/INSUFFICIENT_DATA 与 evidence 的 pass/waiting_data
    逐点一致。"""
    mid = _adopted_registry(monkeypatch)
    for dp in (0.03, 0.04, 0.05, 0.2):
        for n_hyp in (1, 2, 3, 12):
            for days in (0, 1, 500, 1000, 1500, 2000, 2130, 2572, 3712,
                         5000, 10 ** 5):
                decision, state, _detail = sample_size_evidence(
                    dp, days, n_hyp=n_hyp)
                got = research_gate(method_id=mid, dp=dp, days=days,
                                    n_hyp=n_hyp).status
                want = "PASS" if decision == "pass" else "INSUFFICIENT_DATA"
                assert got == want, (dp, n_hyp, days, got, want, state)


def test_gate_result_carries_acceptance_snapshot(monkeypatch):
    """GateResult 携带 ACCEPTANCE_CONSTANTS 快照（消费方可断言未漂移）。"""
    r = research_gate(method_id=PRIMARY_METHOD)
    assert r.acceptance == ACCEPTANCE_CONSTANTS
    assert r.acceptance["constants"] == r.acceptance["expected"]
    assert r.acceptance["drift"] == []
    assert "策略引用" not in json.dumps(r.acceptance)  # 快照只是阈值+哈希


def test_describe_line_format():
    r = research_gate(method_id=PRIMARY_METHOD, dp=0.05, days=10 ** 6)
    line = r.describe()
    assert line.startswith("研究门禁：FAIL——")
    assert "METHOD_REGISTRY" in line


# ---------------------------------------------------------------- 快照

def test_acceptance_snapshot_matches_live_constants():
    """快照四常量 = 现行 POLICY 缺省与决策层类常量（lb_z/threshold_mult
    来自 DecisionPolicy 缺省；min_cell/prior_strength 来自
    LevelDecisionV1 类常量→ProbTable.fit 的实际入参）。"""
    from mt5quant.strategies.library.prob.level_decision import (
        LevelDecisionV1,
    )
    got = ACCEPTANCE_CONSTANTS["constants"]
    assert got == {
        "lb_z": LevelDecisionV1.POLICY.z,
        "threshold_mult": LevelDecisionV1.POLICY.threshold_mult,
        "min_cell": LevelDecisionV1.MIN_CELL,
        "prior_strength": LevelDecisionV1.PRIOR_STRENGTH,
    }
    assert got == {"lb_z": 1.0, "threshold_mult": 2.0,
                   "min_cell": 100, "prior_strength": 100}


def test_acceptance_snapshot_source_hashes():
    """快照携带各源文件 sha256，与磁盘现行内容一致（漂移即换哈希）。"""
    src = ACCEPTANCE_CONSTANTS["sources"]
    assert set(src) >= {
        "src/mt5quant/strategies/library/prob/level_decision.py",
        "src/mt5quant/prob/tables.py",
        "src/mt5quant/prob/decision.py"}
    from mt5quant.research import gate
    assert src == gate.EXPECTED_ACCEPTANCE_SOURCE_SHA256
    assert ACCEPTANCE_CONSTANTS["source_drift"] == []
    for rel, digest in src.items():
        p = REPO / rel
        assert p.is_file(), rel
        normalized = p.read_text(encoding="utf-8").replace("\r\n", "\n") \
            .replace("\r", "\n").encode("utf-8")
        assert digest == hashlib.sha256(normalized).hexdigest()


def test_acceptance_snapshot_detects_semantic_source_drift(monkeypatch):
    """EV 下界/概率估计语义文件变化，即使常量不变也必须触发漂移。"""
    from mt5quant.research import gate
    path = "src/mt5quant/prob/decision.py"
    monkeypatch.setitem(gate.EXPECTED_ACCEPTANCE_SOURCE_SHA256, path,
                        "0" * 64)
    snap = gate.acceptance_snapshot()
    assert path in snap["source_drift"]
    assert f"source:{path}" in snap["drift"]


# ---------------------------------------------------------------- 指纹

def test_fingerprint_shape_and_determinism():
    a = _fp()
    b = _fp()
    assert a["sha256"] == b["sha256"] and a["run_id"] == b["run_id"]
    assert a["run_id"] == a["sha256"][:16]
    assert len(a["sha256"]) == 64 and len(a["run_id"]) == 16
    assert set(a["components"]) == {"code", "data", "params", "features",
                                    "cost", "evaluator"}
    comp = a["components"]
    assert comp["code"]["strategy_ref"] == STRATEGY_REF
    assert comp["code"]["card"] == "P001-v1"
    # 闭包可得 → 列文件集（level_decision 模块与其项目内依赖）
    assert comp["code"]["files"], "闭包文件集不应为空"
    assert comp["evaluator"] == {"sha": BASE_FP["evaluator_sha"],
                                 "bound": True}


def test_fingerprint_sensitivity_code():
    base = _fp()["sha256"]
    # 策略引用变（另一模块的另一类）→ code 维变
    assert _fp(strategy_ref="mt5quant.prob.tables:ProbTable")["sha256"] \
        != base
    # 卡号变 → code 维变
    assert _fp(card="P002-v1")["sha256"] != base


def test_fingerprint_sensitivity_data_params_features_cost_evaluator():
    base = _fp()["sha256"]
    # data 维
    assert _fp(bars_meta={**BASE_FP["bars_meta"],
                          "content_sha256": "d" * 64})["sha256"] != base
    # params 维（params 与冻结模型都折进 params 组件）
    assert _fp(params={**BASE_FP["params"], "sims": 201})["sha256"] != base
    assert _fp(model_dict={**BASE_FP["model_dict"],
                           "prior_strength": 200})["sha256"] != base
    # features 维
    assert _fp(features=["trend_sma200"])["sha256"] != base
    # cost 维（cost_version 与 sessions_sha 都进 cost 组件）
    assert _fp(cost_version="costs-v3-probe-2")["sha256"] != base
    assert _fp(sessions_sha="e" * 64)["sha256"] != base
    # evaluator 维（None → 绑定，sha 必变）
    assert _fp(evaluator_sha=None)["sha256"] != base


def test_fingerprint_evaluator_unbound_is_explicit():
    """evaluator_sha=None → 组件显式标注'未绑定'，不冒充覆盖。"""
    comp = _fp(evaluator_sha=None)["components"]["evaluator"]
    assert comp["sha"] is None
    assert comp["bound"] is False
    assert "未绑定" in comp["note"]


def test_fingerprint_features_order_insensitive():
    """features 排序后入指纹：同集合不同顺序 → 同一 sha256。"""
    assert _fp(features=["trend_sma200", "atr_pct_2000"])["sha256"] \
        == _fp()["sha256"]


@pytest.mark.parametrize("over", [
    {"strategy_ref": ""},
    {"strategy_ref": None},
    {"card": ""},
    {"card": None},
    {"bars_meta": None},
    {"bars_meta": {}},
    {"params": None},
    {"params": {}},
    {"model_dict": None},
    {"features": None},
    {"features": []},
    {"cost_version": ""},
    {"sessions_sha": None},
])
def test_fingerprint_missing_required_component_rejected(over):
    """必填组件（code/data/params/features/cost）缺失 → ValueError，不
    静默空串（P2-07 教训）。"""
    with pytest.raises(ValueError):
        _fp(**over)


# ------------------------------------------------- round-report 接线（bt.py）

# 沿用现有 round-report 测试夹具（tests/test_prob_p1_cli.py 的
# r4_open/_run_round_report——fixture 连同其依赖 round_env 一并引入）
from tests.test_prob_p1_cli import (  # noqa: E402
    _record_r4_run,
    _run_round_report,
    r4_open,
    round_env,
)


def test_round_report_prints_research_gate_line(r4_open, monkeypatch):
    """bt prob round-report 在方法门标注旁输出 ResearchGate 四态一行
    （stdout + 报告 md 各一行；exit code 仍 0；既有标注不回归）。"""
    with Ledger(r4_open["ledger"]) as led:
        _record_r4_run(led, cid="C101", hyp="S1:A1:follow>base")
        _record_r4_run(led, cid="C102", hyp="S1:A2:follow>base")
    code, out = _run_round_report(r4_open, monkeypatch)
    assert code == 0, out
    assert "研究门禁：FAIL——" in out
    assert "METHOD_REGISTRY" in out
    reports = sorted((r4_open["tmp"] / "docs" / "research")
                     .glob("round-R4-p1-*.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    gate_lines = [ln for ln in text.splitlines()
                  if ln.startswith("研究门禁：")]
    assert len(gate_lines) == 1
    assert gate_lines[0].startswith("研究门禁：FAIL——")
    # 既有 P2-09 标注原样保留
    assert "方法未批准（成簇负对照失败）——本报告不构成推进依据" in text
    assert "不推进（方法阻塞）" in text
    assert "推进 0 / 2" in text
