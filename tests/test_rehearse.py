"""T153/D67（第十六轮 H16-4；第十八轮施工）：bt round rehearse 彩排命令。

覆盖（任务钉死）：
1. 合成轮次上彩排端到端跑通（柱级模式 + 事件模式 + 概率模式开轮段）；
2. **台账无污染**：彩排前后正式 ledger 的 chain_meta 头哈希与行数
   逐位不变（彩排全部写入落 data/rehearse 沙盒影子台账）；
3. 报告生成（JSON 原始产物留 data/ 侧沙盒 + 人读 md 落 report 目录）；
4. 已在正式台账开启的轮次拒绝彩排（D67 是**开轮前**彩排）；
5. 安慰剂确定性（预写规则：同轮次同序列；不同轮次不同序列）。

T153 审核残留修复（docs/reviews/2026-11-13-t153-review.md F-1..F-5）：
- F-1 formal_round_status 读不到 → 拒绝彩排（fail-closed，沙盒前零写入）；
- F-2 no_approved_by 措辞与影子 rounds 行继承字段一致（断言=实际行为）；
- F-3 夹具带 candidate_sha256（H-08 绑定真实路径）与 precheck_source/
  calendar_coverage（F1 登记交叉核对真实路径）；
- F-4 沙盒缺省彩排成功后自动清理（护栏：只删自含沙盒目录）；失败/被拒
  不清理；--keep-sandbox 保留；
- F-5 覆盖边界文档化（--allow-unbounded-data 豁免 + 混合 mode 只彩排
  首候选）并各钉边界测试。

事件模式链（screen+gate V3）经过裁决代码指纹（verdict_code_sha）的
干净树检查（T188/D75）——工作树 src/mt5quant、src/mt5data、scripts/
bt.py 有未提交改动时 gate 阶段会被 fail-closed 拒绝；事件端到端用例
因此在已提交的工作树上运行（与既有 tests/test_fomc_calendar_engine.py
的端到端用例同一前提）。
"""
import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from mt5quant.research import rehearse
from mt5quant.research import rounds as rnd
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

ROUND_MIXED = """\
id = "R10"
opened = "2026-11-13"
candidates = ["C1001", "C1002"]
trial_budget = 40
lockbox_uses_max = 1
dsr_trial_scope = "rounds_lineage"
advance_rule = "柱级候选按 G3、事件候选按 [v3.event] 随机化参照"
close_rule = "预算用尽或全部候选有结论即结束"
approved_by = "彩排测试"

[evaluation]
C1001 = { mode = "bar", primary = "net_profit" }
C1002 = { mode = "event", primary = "randomization", side_rule = "static_long" }
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
                 rounds=(), keep_sandbox=True, extra=()):
    """CLI 端到端辅助。缺省带 --keep-sandbox（既有用例检查沙盒内容）；
    F-4 的缺省清理行为由专门用例以不带该 flag 的裸调用验证。"""
    rounds_dir = write_round(tmp_path, round_body, name)
    formal = make_formal_ledger(tmp_path, rounds=rounds)
    argv = [
        "round", "rehearse", name,
        "--rounds-dir", str(rounds_dir),
        "--sandbox-root", str(tmp_path / "rehearse"),
        "--formal-ledger", str(formal),
        "--report-dir", str(tmp_path / "reports"),
        "--bars", bars]
    if keep_sandbox:
        argv.append("--keep-sandbox")
    rc = mod.main([*argv, *extra])
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


def test_formal_round_status_fail_closed_on_unreadable(tmp_path, capsys):
    """F-1：正式台账文件存在但读不到（损坏字节）→ 按"无法确认轮次
    状态"拒绝彩排（fail-closed），不再视为未开启放行；拒绝发生在沙盒
    创建之前（零写入）；heads 快照兜底同样 fail-closed；文件不存在
    仍是 None（无台账可查）。"""
    bad = tmp_path / "formal.sqlite"
    bad.write_bytes(b"this is not a sqlite database at all" * 8)
    with pytest.raises(rehearse.RehearseError, match="无法确认"):
        rehearse.formal_round_status(bad, "R8")
    with pytest.raises(rehearse.RehearseError):        # heads 兜底不变
        rehearse.formal_chain_heads(bad)
    assert rehearse.formal_round_status(
        tmp_path / "absent.sqlite", "R8") is None
    mod = load_bt()
    rounds_dir = write_round(tmp_path, ROUND_BAR, "R8")
    rc = mod.main(["round", "rehearse", "R8", "--rounds-dir", str(rounds_dir),
                   "--sandbox-root", str(tmp_path / "rehearse"),
                   "--formal-ledger", str(bad),
                   "--report-dir", str(tmp_path / "reports")])
    assert rc == 1
    assert "无法确认" in capsys.readouterr().out
    assert not (tmp_path / "rehearse").exists()        # 拒绝时零写入


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
    # F-2：no_approved_by 措辞与实际行为一致——影子 rounds 行的
    # approved_by 是预注册必填字段（"彩排测试"）的继承值（沙盒内随
    # 真实 round open 代码路径写入，非彩排新增批准）；正式台账零 rounds
    # 行（上方 counts_now 已断言）。
    assert "继承值" in report["red_lines"]["no_approved_by"]
    assert "不写正式台账任何行" in report["red_lines"]["no_approved_by"]
    assert "继承值" in md
    # 影子行身份可区分：actor 带 bt-rehearse- 前缀（事后审计可拣出）
    shadow = _shadow_ledger(sandbox_root, "R8")
    con = _ro(shadow)
    try:
        actors = {r[0] for r in con.execute(
            "SELECT DISTINCT actor FROM runs")}
        approved = con.execute(
            "SELECT approved_by FROM rounds WHERE round_id='R8'"
        ).fetchone()[0]
        check = json.loads(con.execute(
            "SELECT result_json FROM candidate_checks"
            " WHERE candidate_id='C801'").fetchone()[0])
    finally:
        con.close()
    assert actors and all(a.startswith("bt-rehearse-") for a in actors)
    assert approved == "彩排测试"
    # F-3：夹具预筛行带 candidate_sha256（= 沙盒候选登记当前字节摘要）
    # ——H-08"候选内容与预筛结论一致"走真实绑定路径（见专门用例）
    cand_file = shadow.parent / "repo" / "candidates" / "C801.toml"
    assert check["candidate_sha256"] == hashlib.sha256(
        cand_file.read_bytes()).hexdigest()


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
        # F-3/F1：夹具预筛行带登记载荷（precheck_source.calendar_sha256
        # + calendar_coverage）——V3 关卡 details 的 precheck_binding 走
        # 真实比对路径（不再 no_registered_precheck）
        payload = json.loads(con.execute(
            "SELECT result_json FROM candidate_checks"
            " WHERE candidate_id='C901'").fetchone()[0])
        v3 = json.loads(con.execute(
            "SELECT g.details_json FROM gates g JOIN runs r"
            " ON g.run_id=r.run_id WHERE r.card_id='H901' AND g.gate='V3'"
            " ORDER BY g.run_id DESC LIMIT 1").fetchone()[0])
    finally:
        con.close()
    assert row and row[0] and len(row[0]) == 64
    assert lockbox == 0
    assert payload["precheck_source"]["calendar_sha256"] \
        == report["placebo"]["fomc_sha256"]
    cov = payload["calendar_coverage"]
    assert cov["era_start"] == "1994-09-27" and cov["window_events"] >= 1
    assert cov["dropped_no_bar"] == [] and cov["dropped_no_anchor"] == []
    binding = v3["fomc_calendar"]["XAUUSD"]["precheck_binding"]
    assert binding["calendar_sha256_match"] is True
    assert binding["coverage_match"] is True
    assert "no_registered_precheck" not in binding
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


# ------------------------------------------------- F-3：H-08 绑定真实路径

def test_fixture_covers_h08_candidate_binding(tmp_path):
    """F-3/H-08：夹具预筛行带 candidate_sha256——真实绑定核对路径被
    覆盖（不再是"旧记录未绑定"兼容路径）：内容一致 → 核对通过；
    预筛通过后改动候选登记 → 核对报错（H-08）。"""
    mod = load_bt()
    d = write_round(tmp_path, ROUND_BAR, "R8")
    r = rnd.load_round(d / "R8.toml")
    cdir = tmp_path / "repo" / "candidates"
    cdir.mkdir(parents=True)
    (cdir / "C801.toml").write_text(
        rehearse._candidate_toml("C801", r, "bar", None), encoding="utf-8")
    led_path = tmp_path / "shadow.sqlite"
    rehearse._seed_shadow_checks(led_path, r, 2.0, cdir, None)
    with Ledger(led_path) as led:
        payload = json.loads(led.conn.execute(
            "SELECT result_json FROM candidate_checks"
            " WHERE candidate_id='C801'").fetchone()[0])
        assert payload["candidate_sha256"] == hashlib.sha256(
            (cdir / "C801.toml").read_bytes()).hexdigest()
        assert mod._candidate_frozen_check(led, "C801", cdir) is None
    (cdir / "C801.toml").write_text("# tampered after precheck\n",
                                    encoding="utf-8")
    with Ledger(led_path) as led:
        err = mod._candidate_frozen_check(led, "C801", cdir)
    assert err and "H-08" in err


# ------------------------------------------------- F-4：沙盒自动清理

def test_rehearse_default_cleans_sandbox_on_pass(tmp_path, capsys):
    """F-4：缺省（不带 --keep-sandbox）彩排成功后自动清理自含沙盒目录
    ——md 报告（A1 材料）保留；sandbox_root 只剩空目录（只删沙盒本身、
    绝不递归上级）；正式台账零污染照旧。"""
    mod = load_bt()
    rounds_dir = write_round(tmp_path, ROUND_BAR, "R8")
    formal = make_formal_ledger(tmp_path)
    rc = mod.main([
        "round", "rehearse", "R8",
        "--rounds-dir", str(rounds_dir),
        "--sandbox-root", str(tmp_path / "rehearse"),
        "--formal-ledger", str(formal),
        "--report-dir", str(tmp_path / "reports"),
        "--bars", "300"])
    assert rc == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "沙盒已自动清理" in out
    root = tmp_path / "rehearse"
    assert root.is_dir() and list(root.iterdir()) == []   # 沙盒已清、根保留
    mds = list((tmp_path / "reports").glob("rehearse-R8-*.md"))
    assert len(mds) == 1                                  # md 报告保留
    assert "已按缺省自动清理" in mds[0].read_text("utf-8")
    _, counts_now = _formal_state(formal)                 # 零污染照旧
    assert counts_now["runs"] == 0 and counts_now["rounds"] == 0


def test_rehearse_failure_keeps_sandbox(tmp_path, capsys):
    """F-4：机械失败（阶段超时，stage-timeout 极小）不清理沙盒——排查
    需要；报告如实 fail。"""
    mod = load_bt()
    rc, formal, sandbox_root, report_dir = run_rehearse(
        mod, tmp_path, ROUND_BAR, "R8", bars="300",
        keep_sandbox=False, extra=["--stage-timeout", "0.001"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "超时" in out and "沙盒保留" in out
    report = _latest_report(sandbox_root, "R8")
    assert report["verdict"] == "fail"
    assert report["sandbox_cleaned"] is False
    assert _shadow_ledger(sandbox_root, "R8").is_file()   # 沙盒保留


def test_cleanup_sandbox_guard_never_deletes_outside(tmp_path):
    """F-4 防误删护栏：只删 sandbox_root **直接子目录**中名字 == 本次
    <R#>-<时刻> tag 且含夹具形状（repo/ + store/）的目录；tag 不符/
    形状不符/sandbox_root 本身一律不动。"""
    root = tmp_path / "rehearse"
    tag = "R8-20260101T000000Z"
    sb = root / tag
    (sb / "repo").mkdir(parents=True)
    (sb / "store").mkdir()
    assert rehearse._cleanup_sandbox(sb, root, tag) is True
    assert not sb.exists() and root.is_dir()
    # tag 不符 → 不删（目录重建后再试）
    (sb / "repo").mkdir(parents=True)
    (sb / "store").mkdir()
    assert rehearse._cleanup_sandbox(sb, root, "R8-20260101T000001Z") is False
    assert sb.is_dir()
    # 缺夹具形状（无 repo/store）→ 不删
    bare = root / "R8-20260101T000002Z"
    bare.mkdir()
    assert rehearse._cleanup_sandbox(bare, root, bare.name) is False
    assert bare.is_dir()
    # sandbox_root 本身（parent 不是 root）→ 不删
    assert rehearse._cleanup_sandbox(root, root, root.name) is False
    assert root.is_dir() and bare.is_dir() and sb.is_dir()


# ------------------------------------------------- F-5：覆盖边界文档化+钉死

def test_rehearse_coverage_boundary_documented(capsys, monkeypatch):
    """F-5：覆盖边界在两处写清——rehearse.py 模块 docstring 与 bt 子命令
    help：① screen/gate 带 --allow-unbounded-data（真实运行不带，裸库
    拒绝类故障不在彩排暴露面）；② 多候选混合 mode 轮只彩排首候选。"""
    monkeypatch.setenv("COLUMNS", "200")    # 防 argparse 折行截断 flag 字面量
    doc = rehearse.__doc__
    assert "--allow-unbounded-data" in doc
    assert "不在彩排暴露面内" in doc
    assert "混合" in doc and "首候选" in doc
    mod = load_bt()
    with pytest.raises(SystemExit) as ei:
        mod.main(["round", "rehearse", "--help"])
    assert ei.value.code in (0, None)
    out = capsys.readouterr().out
    assert "--allow-unbounded-data" in out
    assert "混合" in out and "首候选" in out


def test_rehearse_mixed_mode_round_only_first_candidate(tmp_path, capsys):
    """F-5（行为钉死）：混合 mode 轮（首候选 bar、次候选 event）v1 只
    彩排首候选 mode 的命令形状——screen/gate 走柱级 G0–G4、不带事件
    绑定（事件候选的命令形状不在本次彩排面内，报告 candidates_all 全列
    自明口径）；同时钉死 screen/gate 带 --allow-unbounded-data（合成库
    无上界语义的必要豁免——真实运行不带此 flag）。"""
    mod = load_bt()
    rc, formal, sandbox_root, report_dir = run_rehearse(
        mod, tmp_path, ROUND_MIXED, "R10", bars="300")
    out = capsys.readouterr().out
    assert rc == 0, out
    report = _latest_report(sandbox_root, "R10")
    assert report["verdict"] == "pass"
    assert report["rehearsed_mode"] == "bar"
    assert report["rehearsed_candidate"] == "C1001"
    assert report["candidates_all"] == ["C1001", "C1002"]
    by = {s["stage"]: s for s in report["stages"]}
    for name in ("screen", "gate"):
        argv = " ".join(by[name]["argv"])
        assert "--allow-unbounded-data" in argv   # F-5①：豁免如实落 argv
        assert "--event-kind" not in argv         # F-5②：事件形状未彩排
    gates_run = {g["gate"] for g in report["gate_rows"]}
    assert gates_run >= {"G0", "G4"} and "V3" not in gates_run
    md = list(report_dir.glob("rehearse-R10-*.md"))[0].read_text("utf-8")
    assert "v1 只彩排首候选" in md
    assert report["pollution"]["unchanged"] is True
