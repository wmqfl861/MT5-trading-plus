"""T153/D67（第十六轮 H16-4；第十八轮施工）：bt round rehearse 彩排命令。

覆盖（任务钉死）：
1. 合成轮次上彩排端到端跑通（柱级模式 + 事件模式 + 概率模式开轮段）；
2. **台账无污染**：彩排前后正式 ledger 的 chain_meta 头哈希与行数
   逐位不变（彩排全部写入落 data/rehearse 沙盒影子台账）；
3. 报告生成（JSON 原始产物留 data/ 侧沙盒 + 人读 md 落 report 目录）；
4. 已在正式台账开启的轮次拒绝彩排（D67 是**开轮前**彩排）；
5. 安慰剂确定性（预写规则：同轮次同序列；不同轮次不同序列）。

事件模式链（screen+gate V3）经过裁决代码指纹（verdict_code_sha）的
干净树检查（T188/D75）——工作树 src/mt5quant、src/mt5data、scripts/
bt.py 有未提交改动时 gate 阶段会被 fail-closed 拒绝；事件端到端用例
因此在已提交的工作树上运行（与既有 tests/test_fomc_calendar_engine.py
的端到端用例同一前提）。
"""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pandas as pd

from mt5quant.research import rehearse
from mt5quant.research.ledger import Ledger

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "bt.py"


def load_bt():
    spec = importlib.util.spec_from_file_location("bt_script_rehearse",
                                                  SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------- 轮次夹具

ROUND_EVENT = """\
id = "R9"
opened = "2026-11-13"
candidates = ["C901"]
trial_budget = 40
lockbox_uses_max = 1
dsr_trial_scope = "rounds_lineage"
advance_rule = "事件模式候选按 [v3.event] 随机化参照 p<=alpha/N"
close_rule = "预算用尽或全部候选有结论即结束"
approved_by = "彩排测试"

[evaluation]
C901 = { mode = "event", primary = "randomization", side_rule = "static_long" }
"""

ROUND_BAR = """\
id = "R8"
opened = "2026-11-13"
candidates = ["C801"]
trial_budget = 40
lockbox_uses_max = 1
dsr_trial_scope = "rounds_lineage"
advance_rule = "G3 通过且 DSR p>0.05 视为候选通过"
close_rule = "预算用尽或全部候选有结论即结束"
approved_by = "彩排测试"

[evaluation]
C801 = { mode = "bar", primary = "net_profit" }
"""

ROUND_PROB = """\
id = "R7"
opened = "2026-11-13"
candidates = ["C701"]
trial_budget = 40
lockbox_uses_max = 0
dsr_trial_scope = "rounds_lineage"
advance_rule = "概率模式按 Holm 假设族判定"
close_rule = "预算用尽或全部候选有结论即结束"
approved_by = "彩排测试"

[evaluation]
C701 = { mode = "probability", primary = "holm_family" }

[hypotheses]
C701 = ["S2:A1:fade>base"]
"""


def write_round(tmp_path, body, name):
    d = tmp_path / "rounds"
    d.mkdir(exist_ok=True)
    p = d / f"{name}.toml"
    p.write_text(body, encoding="utf-8")
    return d


def make_formal_ledger(tmp_path, *, rounds=()):
    """正式台账夹具：若干预筛行（可选拉开轮的 rounds 行）；返回路径。"""
    path = tmp_path / "formal.sqlite"
    with Ledger(path) as led:
        led.record_candidate_check(
            "C000", decision="pass",
            result={"candidate_id": "C000", "decision": "pass",
                    "checks": {"power": {"years": 2.0}}})
        for rid, cands in rounds:
            led.record_round_open(
                round_id=rid, candidates=cands, trial_budget=10,
                lockbox_uses_max=1, dsr_trial_scope="rounds_lineage",
                advance_rule="x", close_rule="y", approved_by="t",
                evaluation={c: {"mode": "event"} for c in cands},
                toml_sha256="0" * 64, git_commit="0" * 40)
    return path


def run_rehearse(mod, tmp_path, round_body, name, *, bars="300",
                 rounds=()):
    rounds_dir = write_round(tmp_path, round_body, name)
    formal = make_formal_ledger(tmp_path, rounds=rounds)
    rc = mod.main([
        "round", "rehearse", name,
        "--rounds-dir", str(rounds_dir),
        "--sandbox-root", str(tmp_path / "rehearse"),
        "--formal-ledger", str(formal),
        "--report-dir", str(tmp_path / "reports"),
        "--bars", bars])
    return rc, formal, tmp_path / "rehearse", tmp_path / "reports"


def _formal_state(path):
    """正式台账状态快照：chain_meta 头 + 各表行数（污染断言用）。"""
    con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        heads = {r[0]: [r[1], r[2], r[3]] for r in con.execute(
            "SELECT table_name, last_rowid, row_count, head_hash"
            " FROM chain_meta ORDER BY table_name")}
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("runs", "gates", "rounds", "candidate_checks",
                            "lockbox_uses", "trial_reservations",
                            "ledger_events")}
    finally:
        con.close()
    return heads, counts


def _latest_report(sandbox_root, rid):
    reports = sorted(sandbox_root.glob(f"{rid}-*/rehearsal-report.json"))
    assert reports, (f"沙盒里找不到彩排 JSON："
                     f"{sorted(p.name for p in sandbox_root.iterdir())}")
    return json.loads(reports[-1].read_text(encoding="utf-8"))


def _shadow_ledger(sandbox_root, rid) -> Path:
    paths = sorted(sandbox_root.glob(f"{rid}-*/ledger.sqlite"))
    assert paths, f"影子台账缺失：{sandbox_root}"
    return paths[-1]


def _ro(path):
    return sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)


# ------------------------------------------------------------ 安慰剂确定性

def test_placebo_deterministic_and_round_specific():
    a = rehearse.placebo_bars("R9", 100)
    b = rehearse.placebo_bars("R9", 100)
    c = rehearse.placebo_bars("R8", 100)
    pd.testing.assert_frame_equal(a, b)
    assert not a["close"].equals(c["close"])       # 不同轮次不同序列
    assert len(a) == 100 and a["close"].iloc[0] > 0
    # 事件日历平移：合成公告日落周四（真实 FOMC 公告日落周三——错开）
    doc = rehearse.placebo_fomc_doc(a)
    assert doc["event_definition"]["era_start"] == "1994-09-27"
    assert doc["meetings"]
    assert all(pd.Timestamp(m["announcement_date"]).weekday() == 3
               for m in doc["meetings"])


def test_formal_ledger_access_is_readonly(tmp_path):
    path = make_formal_ledger(tmp_path, rounds=(("R1", ["C002"]),))
    assert rehearse.formal_round_status(path, "R1") == "open"
    assert rehearse.formal_round_status(path, "R9") is None
    assert rehearse.formal_round_status(tmp_path / "absent.sqlite", "R1") \
        is None
    heads = rehearse.formal_chain_heads(path)
    assert "runs" in heads and "rounds" in heads
    # 只读访问不产生 journal/wal 等写痕迹
    assert not list(tmp_path.glob("formal.sqlite-*"))


def test_md_paths_rendered_repo_relative(tmp_path):
    """入库的 md 不带本机绝对路径（卫生测试 local_path 模式）——仓库内
    路径显示为仓库相对；仓库外（测试 tmp）原样。"""
    root = tmp_path / "repo"
    (root / "data" / "rehearse").mkdir(parents=True)
    disp = rehearse._display
    assert disp(str(root / "data" / "rehearse"), root) == "data/rehearse"
    assert disp("data/rehearse", root) == "data/rehearse"
    outside = tmp_path / "elsewhere"
    assert disp(str(outside), root) == str(outside)


# ------------------------------------------------------------ 柱级轮彩排

def test_rehearse_bar_round_end_to_end_no_pollution(tmp_path, capsys):
    """柱级轮：round open → screen → gate（G0–G4）→ ledger verify 全链
    机械跑通；正式台账头快照逐位不变；报告 JSON+md 生成。"""
    mod = load_bt()
    rc, formal, sandbox_root, report_dir = run_rehearse(
        mod, tmp_path, ROUND_BAR, "R8")
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "彩排结论：pass" in out
    report = _latest_report(sandbox_root, "R8")
    assert report["verdict"] == "pass"
    assert report["rehearsed_mode"] == "bar"
    by = {s["stage"]: s for s in report["stages"]}
    assert by["round_open"]["rc"] == 0
    assert by["screen"]["rc"] == 0
    assert by["gate"]["rc"] in (0, 1)          # 安慰剂上实质不过属预期
    assert by["ledger_verify"]["rc"] == 0
    assert all(s["seconds"] >= 0.0 for s in report["stages"])  # 逐阶段计时
    # 关卡足迹：影子台账有 gates 行且进报告（通过情况）
    assert report["gate_rows"] and all(
        set(g) == {"gate", "passed"} for g in report["gate_rows"])
    assert {g["gate"] for g in report["gate_rows"]} \
        >= {"G0", "G1", "G2", "G3", "G4"}
    # 影子台账：全部写入落沙盒；无锁箱行（G5 不彩排）
    counts = report["shadow_ledger_counts"]
    assert counts["rounds"] == 1 and counts["runs"] >= 2
    assert counts["gates"] >= 5 and counts["lockbox_uses"] == 0
    # 正式台账无污染：头快照 + 行数逐位不变
    assert report["pollution"]["unchanged"] is True
    heads_now, counts_now = _formal_state(formal)
    assert heads_now == report["pollution"]["heads_before"]
    assert counts_now["runs"] == 0 and counts_now["rounds"] == 0
    assert counts_now["lockbox_uses"] == 0
    # 报告：md（A1 材料）+ JSON（原始产物留 data/ 侧沙盒）
    mds = list(report_dir.glob("rehearse-R8-*.md"))
    assert len(mds) == 1
    md = mds[0].read_text(encoding="utf-8")
    assert "彩排报告 R8" in md and "耗时" in md
    assert "台账纪律" in md and "一致" in md          # 无污染断言可见
    assert "不彩排 G5" in md and "只读" in md          # 红线声明可见
    assert report["placebo"]["bars"] == 300
    assert set(report["red_lines"]) == {
        "offline_no_mt5", "no_real_candidate_returns", "no_approved_by",
        "gates_toml_readonly"}
    # 影子行身份可区分：actor 带 bt-rehearse- 前缀（事后审计可拣出）
    con = _ro(_shadow_ledger(sandbox_root, "R8"))
    try:
        actors = {r[0] for r in con.execute(
            "SELECT DISTINCT actor FROM runs")}
    finally:
        con.close()
    assert actors and all(a.startswith("bt-rehearse-") for a in actors)


# ------------------------------------------------------------ 事件轮彩排

def test_rehearse_event_round_end_to_end(tmp_path, capsys):
    """事件轮：安慰剂（平移日历）走 V3 事件分支——round open → screen
    → gate V3（随机化主检验）→ ledger verify；verdict_code_sha 随行
    （需干净判决路径树，见模块 docstring 的前提说明）。"""
    mod = load_bt()
    rc, formal, sandbox_root, report_dir = run_rehearse(
        mod, tmp_path, ROUND_EVENT, "R9", bars="400")
    out = capsys.readouterr().out
    assert rc == 0, out
    report = _latest_report(sandbox_root, "R9")
    assert report["verdict"] == "pass"
    assert report["rehearsed_mode"] == "event"
    by = {s["stage"]: s for s in report["stages"]}
    assert "--event-kind" in " ".join(by["gate"]["argv"])
    assert "--fomc-calendar-sha256" in " ".join(by["screen"]["argv"])
    assert "V3" in {g["gate"] for g in report["gate_rows"]}
    # 影子台账：事件 run 带 verdict 指纹 + 无锁箱消耗
    con = _ro(_shadow_ledger(sandbox_root, "R9"))
    try:
        row = con.execute(
            "SELECT verdict_code_sha FROM runs WHERE card_id='H901'"
            " ORDER BY run_id DESC LIMIT 1").fetchone()
        lockbox = con.execute(
            "SELECT COUNT(*) FROM lockbox_uses").fetchone()[0]
    finally:
        con.close()
    assert row and row[0] and len(row[0]) == 64
    assert lockbox == 0
    assert report["pollution"]["unchanged"] is True
    md = list(report_dir.glob("rehearse-R9-*.md"))[0].read_text("utf-8")
    assert "平移" in md                     # 安慰剂规则进人读报告


# ------------------------------------------------------------ 概率轮（开轮段）

def test_rehearse_probability_round_open_stage_only(tmp_path, capsys):
    """概率轮：开轮段（含 D52 假设族冻结的机械路径）彩排；fit/prob
    test 链 v1 不彩排（P 线休眠 D69）——screen/gate 跳过且如实记录。"""
    mod = load_bt()
    rc, formal, sandbox_root, report_dir = run_rehearse(
        mod, tmp_path, ROUND_PROB, "R7", bars="200")
    out = capsys.readouterr().out
    assert rc == 0, out
    report = _latest_report(sandbox_root, "R7")
    assert report["verdict"] == "pass"
    by = {s["stage"]: s for s in report["stages"]}
    assert by["round_open"]["rc"] == 0
    assert by["screen"].get("skipped") and by["gate"].get("skipped")
    assert "D69" in by["screen"]["skipped"]
    assert report["stage_status"]["screen"] == "skipped"
    assert report["stage_status"]["gate"] == "skipped"
    # 开轮机械：影子 rounds 行带冻结假设族（_prob_family_freeze 走通）
    con = _ro(_shadow_ledger(sandbox_root, "R7"))
    try:
        ev = json.loads(con.execute(
            "SELECT evaluation_json FROM rounds WHERE round_id='R7'"
        ).fetchone()[0])
    finally:
        con.close()
    frozen = ev.get("__frozen_hypotheses__") or {}
    assert frozen.get("hypotheses", {}).get("C701") == ["S2:A1:fade>base"]
    assert report["pollution"]["unchanged"] is True


# ------------------------------------------------------------ 已开启拒绝

def test_rehearse_refuses_round_already_open_in_formal(tmp_path, capsys):
    mod = load_bt()
    rc, formal, sandbox_root, _ = run_rehearse(
        mod, tmp_path, ROUND_EVENT, "R9",
        rounds=(("R9", ["C901"]),))
    assert rc == 1
    out = capsys.readouterr().out
    assert "开轮前" in out and "R9" in out
    assert not sandbox_root.exists()       # 拒绝时不建沙盒、零写入


def test_rehearse_invalid_preregistration_fails_fast(tmp_path, capsys):
    """预注册本身不合法（附录 D 校验失败）→ 无彩排可言，fail-fast。"""
    mod = load_bt()
    bad = ROUND_BAR.replace("trial_budget = 40", "trial_budget = 0")
    rounds_dir = write_round(tmp_path, bad, "R8")
    rc = mod.main(["round", "rehearse", "R8", "--rounds-dir",
                   str(rounds_dir), "--sandbox-root",
                   str(tmp_path / "rehearse"), "--formal-ledger",
                   str(tmp_path / "formal.sqlite"), "--report-dir",
                   str(tmp_path / "reports")])
    assert rc == 1
    assert "trial_budget" in capsys.readouterr().out
    assert not (tmp_path / "rehearse").exists()
