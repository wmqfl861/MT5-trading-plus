"""T63/D32（铁律 16）：研究轮次预注册——装载、开启、运行约束、关闭。

七个失败测试（红→绿，任务钉死）：
1. 未提交的轮次文件不能开启（含工作区脏文件变体）；
2. 候选非 screened_pass（台账 candidate_checks 最新 decision ≠ pass）不能开启；
3. 卡不在轮次内被拒（轮次未开启 / 候选不在名单 / 候选预筛非 pass）；
4. 累计去重试验数超 trial_budget 拒；
5. 本轮 G5 次数超 lockbox_uses_max 拒；
6. 关闭轮次后再运行拒；
7. rounds 记录在台账哈希链内（篡改/删行检出，写入 fail-closed）。

装载器（附录 D）：缺必填 / 超 3 候选 / 非法字段（未知键、枚举外、空串、
文件名与 id 不一致）→ RoundError。runs.round_id：新运行记录轮次，
旧运行无 round_id 兼容（NULL = 轮次制之前，含 pre-T63 旧库迁移）。
"""
import importlib.util
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from mt5data.store import _COLUMNS
from mt5quant.research import cards as cards_mod
from mt5quant.research import rounds as rounds_mod
from mt5quant.research.ledger import Ledger, LedgerTamperedError
from mt5quant.research.rounds import RoundError, load_round

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "bt.py"
REPO_ROOT = Path(__file__).resolve().parents[1]
PAST = datetime(2026, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)


def load_bt():
    spec = importlib.util.spec_from_file_location("bt_script_rounds", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- 装夹具

ROUND_TOML = """\
id = "R1"
opened = "2026-10-27"
candidates = ["C001"]
trial_budget = 40
lockbox_uses_max = 1
dsr_trial_scope = "project"
advance_rule = "G3 通过且 DSR p>0.05 视为候选通过；通过后本轮不再挑选"
close_rule = "预算用尽或全部候选有结论即结束"
approved_by = "用户A"

[evaluation]
C001 = { mode = "event", primary = "event_mean_return_one_sided" }
"""

CARD_BODY = """\
id = "H100"
version = 1
symbols = ["XAUUSD"]
timeframe = "H1"
session = "全时段"
phenomenon = "月末资金流"
mechanism = "固定日历资金流"
should_fail_when = "月末效应消失"
falsified_if = "样本内净利为负"
graveyard_checked = []

[rules]
entry = "月末最后一根 bar 收盘买入"
exit = "月初第三根 bar 收盘卖出"
stop = "无"

[params]

[expected]
trades_per_year = "12"
win_rate = "50%+"
payoff_ratio = "1.0+"
"""

CARD_IN_ROUND = CARD_BODY.replace(
    'id = "H100"\n',
    'id = "H100"\nround = "R1"\ncandidate = "C001"\n')


def write_round(tmp_path, body=ROUND_TOML, name="R1.toml"):
    d = tmp_path / "rounds"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(body, encoding="utf-8")
    return d


def write_card(tmp_path, body=CARD_IN_ROUND, name="H100.toml"):
    d = tmp_path / "cards"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(body, encoding="utf-8")
    return d


def patch_git(monkeypatch, mod, *, card=(PAST, True), rnd=(PAST, True)):
    """注入卡文件与轮次文件各自的 git 状态（committed_at / worktree_clean）。
    值为 (committed|None, clean)。按路径所在目录（cards/ vs rounds/）区分。"""

    def committed_at(path, runner=None):
        s = str(path).replace("\\", "/")
        return rnd[0] if "/rounds/" in s else card[0]

    def worktree_clean(path, runner=None):
        s = str(path).replace("\\", "/")
        return rnd[1] if "/rounds/" in s else card[1]

    monkeypatch.setattr(mod.cards, "committed_at", committed_at)
    monkeypatch.setattr(mod.cards, "worktree_clean", worktree_clean)


def patch_strategy_fp(monkeypatch, mod, sha="f" * 64):
    monkeypatch.setattr(mod.cards, "ensure_strategy_clean",
                        lambda ref, root, **kw: None)
    monkeypatch.setattr(mod.cards, "strategy_fingerprint",
                        lambda ref, root, **kw: sha)


def h1_bars(n=6):
    times = pd.date_range("2026-01-05 10:00", periods=n, freq="h")
    closes = [100.0, 100.0, 101.0, 102.0, 103.0, 104.0][:n]
    # H-13：high/low 随 close 抬高（旧固定 100.5 在 close>100.5 后非法）
    opens = [100.0] * n
    return pd.DataFrame({
        "time": times, "open": opens,
        "high": [max(o, c) + 0.5 for o, c in zip(opens, closes)],
        "low": [min(o, c) - 0.5 for o, c in zip(opens, closes)],
        "close": closes, "tick_volume": 10, "spread": 5, "real_volume": 0,
    })[_COLUMNS]


def make_data_root(root, *, n=6):
    sdir = root / "bars" / "XAUUSD" / "H1"
    sdir.mkdir(parents=True, exist_ok=True)
    h1_bars(n).to_parquet(sdir / "data.parquet")
    specs = root / "specs"
    specs.mkdir(parents=True, exist_ok=True)
    specs.joinpath("latest.json").write_text(
        json.dumps({"XAUUSD": {"contract_size": 100.0, "volume_min": 0.01,
                               "volume_step": 0.01, "volume_max": 200.0}}),
        encoding="utf-8")
    return root


def record_check(ledger, cid, decision, *, at=None):
    """往 candidate_checks 写一条（rowid 顺序即时间顺序，最新在最后）。"""
    return ledger.record_candidate_check(
        cid, decision=decision,
        result={"candidate_id": cid, "decision": decision, "at":
                (at or NOW).isoformat()})


def open_round(mod, tmp_path, ledger_path, monkeypatch, *, body=ROUND_TOML,
               name="R1", checks=(("C001", "pass"),), git=(PAST, True)):
    """装轮次文件 + 台账里给候选写预筛结论 + 注入 git 状态后 bt round open。"""
    rdir = write_round(tmp_path, body, f"{name}.toml")
    with Ledger(ledger_path) as led:
        for cid, decision in checks:
            record_check(led, cid, decision)
    patch_git(monkeypatch, mod, rnd=git)
    return mod.main(["round", "open", name,
                     "--rounds-dir", str(rdir), "--ledger", str(ledger_path)])


# ------------------------------------------------------- 装载器（附录 D）

def test_load_round_valid(tmp_path):
    d = write_round(tmp_path)
    r = load_round(d / "R1.toml")
    assert r.id == "R1"
    assert r.candidates == ("C001",)
    assert r.trial_budget == 40
    assert r.lockbox_uses_max == 1
    assert r.dsr_trial_scope == "project"
    assert r.approved_by == "用户A"
    assert r.evaluation["C001"] == {
        "mode": "event", "primary": "event_mean_return_one_sided"}
    assert len(r.content_sha256()) == 64


@pytest.mark.parametrize("mutate, frag", [
    ("del id", 'del data["id"]\n'),
    ("empty approved_by", 'data["approved_by"] = "  "\n'),
    ("no advance_rule", 'del data["advance_rule"]\n'),
    ("bad scope", 'data["dsr_trial_scope"] = "global"\n'),
    ("zero budget", 'data["trial_budget"] = 0\n'),
    ("zero lockbox max", 'data["lockbox_uses_max"] = 0\n'),
    ("bad opened date", 'data["opened"] = "not-a-date"\n'),
])
def test_load_round_rejects_missing_or_invalid(tmp_path, mutate, frag):
    d = write_round(tmp_path)
    p = d / "R1.toml"
    data = p.read_text(encoding="utf-8")
    # 简易改写：按 TOML 行重组再由 tomllib 解析后注入破坏
    import tomllib
    parsed = tomllib.loads(data)
    exec(frag, {"data": parsed})
    lines = []
    for k, v in parsed.items():
        if k == "evaluation":
            continue
        lines.append(f"{k} = {json.dumps(v, ensure_ascii=False)}")
    lines.append("[evaluation]")
    for cid, ev in parsed["evaluation"].items():
        lines.append(f"{cid} = {{ mode = {json.dumps(ev['mode'])}, "
                     f"primary = {json.dumps(ev['primary'])} }}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(RoundError):
        load_round(p)


def test_load_round_rejects_over_three_candidates(tmp_path):
    body = ROUND_TOML.replace(
        'candidates = ["C001"]',
        'candidates = ["C001", "C002", "C003", "C004"]').replace(
        "[evaluation]\nC001 = { mode = \"event\", "
        "primary = \"event_mean_return_one_sided\" }",
        "[evaluation]\n" + "\n".join(
            f'C00{i} = {{ mode = "event", primary = "x" }}'
            for i in (1, 2, 3, 4)))
    d = write_round(tmp_path, body)
    with pytest.raises(RoundError, match="3"):
        load_round(d / "R1.toml")


def test_load_round_rejects_unknown_field(tmp_path):
    # 未知顶层字段必须放在 [evaluation] 之前（TOML 追加会落进表内）
    body = ROUND_TOML.replace('approved_by = "用户A"',
                              'approved_by = "用户A"\nextra_field = "拼写错误"')
    d = write_round(tmp_path, body)
    with pytest.raises(RoundError, match="未知"):
        load_round(d / "R1.toml")


def test_load_round_rejects_missing_evaluation_entry(tmp_path):
    body = ROUND_TOML.replace(
        'candidates = ["C001"]', 'candidates = ["C001", "C002"]').replace(
        'C001 = { mode = "event", primary = "event_mean_return_one_sided" }',
        'C001 = { mode = "event", primary = "x" }')
    d = write_round(tmp_path, body)
    with pytest.raises(RoundError, match="evaluation"):
        load_round(d / "R1.toml")


def test_load_round_rejects_filename_mismatch(tmp_path):
    d = write_round(tmp_path, name="R2.toml")
    with pytest.raises(RoundError, match="文件名"):
        load_round(d / "R2.toml")


# ----------------------------------------------------- 开启前置条件（1、2）

def test_open_rejects_uncommitted_round_file(tmp_path, monkeypatch, capsys):
    """失败测试 1：未提交的轮次文件不能开启——铁律 16：轮次预注册必须在
    看到任何结果之前提交（committed_at=None → 拒绝），不写 rounds 行。"""
    mod = load_bt()
    ledger_path = tmp_path / "ledger.sqlite"
    rc = open_round(mod, tmp_path, ledger_path, monkeypatch,
                    git=(None, True))
    assert rc == 1
    out = capsys.readouterr().out
    assert "未提交" in out
    with Ledger(ledger_path) as led:
        assert led.conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0] == 0


def test_open_rejects_dirty_worktree_round_file(tmp_path, monkeypatch, capsys):
    """失败测试 1 变体：文件已提交但工作区有未提交改动 → 拒绝。"""
    mod = load_bt()
    ledger_path = tmp_path / "ledger.sqlite"
    rc = open_round(mod, tmp_path, ledger_path, monkeypatch,
                    git=(PAST, False))
    assert rc == 1
    assert "未提交" in capsys.readouterr().out
    with Ledger(ledger_path) as led:
        assert led.conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0] == 0


def test_open_rejects_candidate_not_screened_pass(tmp_path, monkeypatch,
                                                  capsys):
    """失败测试 2：候选最新预筛结论不是 pass（screened_pass）→ 不能开启。
    台账无该候选的预筛记录同样视为未过。"""
    mod = load_bt()
    ledger_path = tmp_path / "ledger.sqlite"
    rc = open_round(mod, tmp_path, ledger_path, monkeypatch,
                    checks=(("C001", "fail"),))
    assert rc == 1
    out = capsys.readouterr().out
    assert "C001" in out and "screened_pass" in out
    with Ledger(ledger_path) as led:
        assert led.conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0] == 0
    # 无预筛记录同样拒绝
    ledger2 = tmp_path / "ledger2.sqlite"
    rc = open_round(mod, tmp_path, ledger2, monkeypatch, checks=())
    assert rc == 1
    assert "C001" in capsys.readouterr().out


def test_open_rejects_lockbox_uses_max_over_d16_remaining(
        tmp_path, monkeypatch, capsys):
    """D16：本锁箱总预算 3 次（写死常量，待用户决策后可调）——
    lockbox_uses_max 超过剩余预算 → 拒绝开启。"""
    mod = load_bt()
    ledger_path = tmp_path / "ledger.sqlite"
    with Ledger(ledger_path) as led:
        for i in range(3):                    # D16 总预算 3 次已用尽
            led.record_lockbox_start(f"H00{i}", approved_by="u")
    rc = open_round(mod, tmp_path, ledger_path, monkeypatch)
    assert rc == 1
    out = capsys.readouterr().out
    assert "D16" in out or "锁箱" in out
    with Ledger(ledger_path) as led:
        assert led.conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0] == 0
    # 申报 4 次（>3）即使预算未动也拒绝
    ledger2 = tmp_path / "ledger2.sqlite"
    body = ROUND_TOML.replace("lockbox_uses_max = 1", "lockbox_uses_max = 4")
    rc = open_round(mod, tmp_path, ledger2, monkeypatch, body=body)
    assert rc == 1
    assert "D16" in capsys.readouterr().out


def test_open_records_round_row_in_chain(tmp_path, monkeypatch, capsys):
    """开启写台账 rounds 表（带哈希链），状态 open；重复开启同 id 拒绝。"""
    mod = load_bt()
    ledger_path = tmp_path / "ledger.sqlite"
    rc = open_round(mod, tmp_path, ledger_path, monkeypatch)
    assert rc == 0, capsys.readouterr().out
    with Ledger(ledger_path) as led:
        rows_ = led.conn.execute("SELECT * FROM rounds").fetchall()
        assert len(rows_) == 1
        row = rows_[0]
        assert row["round_id"] == "R1"
        assert row["status"] == "open"
        assert json.loads(row["candidates_json"]) == ["C001"]
        assert row["trial_budget"] == 40
        assert row["lockbox_uses_max"] == 1
        assert row["approved_by"] == "用户A"
        assert row["toml_sha256"]
        assert led.verify_chain() == []
    # 幂等性：同 id 再开 → 拒绝（预注册是一次性动作）
    rc = open_round(mod, tmp_path, ledger_path, monkeypatch)
    assert rc == 1
    assert "已开启" in capsys.readouterr().out
    with Ledger(ledger_path) as led:
        assert led.conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0] == 1


# ------------------------------------------------------- 运行约束（3–6）

def run_screen(mod, cards_dir, root, ledger_path, *, card="H100"):
    return mod.main(
        ["screen", "--card", card, "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--volume-lots", "0.01"])


def test_screen_rejects_card_in_unopened_round(tmp_path, monkeypatch, capsys):
    """失败测试 3a：卡的 round 字段指向未开启（未 bt round open）的轮次 → 拒。"""
    mod = load_bt()
    root = make_data_root(tmp_path / "data")
    cards_dir = write_card(tmp_path)
    ledger_path = tmp_path / "ledger.sqlite"
    patch_git(monkeypatch, mod)
    patch_strategy_fp(monkeypatch, mod)
    rc = run_screen(mod, cards_dir, root, ledger_path)
    assert rc == 1
    out = capsys.readouterr().out
    assert "R1" in out and "未开启" in out
    with Ledger(ledger_path) as led:
        assert led.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_screen_rejects_card_candidate_not_in_round(tmp_path, monkeypatch,
                                                    capsys):
    """失败测试 3b：卡的候选不在该轮 candidates 名单内 → 拒。"""
    mod = load_bt()
    root = make_data_root(tmp_path / "data")
    cards_dir = write_card(tmp_path, CARD_IN_ROUND.replace(
        'candidate = "C001"', 'candidate = "C002"'))
    ledger_path = tmp_path / "ledger.sqlite"
    assert open_round(mod, tmp_path, ledger_path, monkeypatch) == 0
    patch_strategy_fp(monkeypatch, mod)
    rc = run_screen(mod, cards_dir, root, ledger_path)
    assert rc == 1
    out = capsys.readouterr().out
    assert "C002" in out and "不在" in out
    with Ledger(ledger_path) as led:
        assert led.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_screen_rejects_card_candidate_not_screened_pass(tmp_path, monkeypatch,
                                                         capsys):
    """失败测试 3c：开启后候选的最新预筛结论翻为 fail → 该轮的卡拒绝运行
    （screened_pass 是轮次内运行的持续前置）。"""
    mod = load_bt()
    root = make_data_root(tmp_path / "data")
    cards_dir = write_card(tmp_path)
    ledger_path = tmp_path / "ledger.sqlite"
    assert open_round(mod, tmp_path, ledger_path, monkeypatch) == 0
    with Ledger(ledger_path) as led:        # 开启后翻案：最新结论 = fail
        record_check(led, "C001", "fail")
    patch_strategy_fp(monkeypatch, mod)
    rc = run_screen(mod, cards_dir, root, ledger_path)
    assert rc == 1
    out = capsys.readouterr().out
    assert "C001" in out and "pass" in out
    with Ledger(ledger_path) as led:
        assert led.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_screen_rejects_roundless_card_once_rounds_exist(tmp_path, monkeypatch,
                                                         capsys):
    """铁律 16：台账一旦有轮次，不带 round/candidate 字段的卡拒绝运行
    （轮次制之前——rounds 表为空——的旧卡不受影响，见下一条）。"""
    mod = load_bt()
    root = make_data_root(tmp_path / "data")
    cards_dir = write_card(tmp_path, CARD_BODY)      # 不带 round/candidate
    ledger_path = tmp_path / "ledger.sqlite"
    assert open_round(mod, tmp_path, ledger_path, monkeypatch) == 0
    patch_strategy_fp(monkeypatch, mod)
    rc = run_screen(mod, cards_dir, root, ledger_path)
    assert rc == 1
    out = capsys.readouterr().out
    assert "轮次" in out
    with Ledger(ledger_path) as led:
        assert led.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_screen_records_round_id_on_runs(tmp_path, monkeypatch, capsys):
    """轮次内的卡正常运行：runs.round_id 记轮次 id；无轮次时期（rounds 表
    空）旧卡照常运行且 round_id 为 NULL（NULL=轮次制之前）。"""
    mod = load_bt()
    root = make_data_root(tmp_path / "data")
    cards_dir = write_card(tmp_path)
    ledger_path = tmp_path / "ledger.sqlite"
    assert open_round(mod, tmp_path, ledger_path, monkeypatch) == 0
    patch_strategy_fp(monkeypatch, mod)
    rc = run_screen(mod, cards_dir, root, ledger_path)
    assert rc == 0, capsys.readouterr().out
    with Ledger(ledger_path) as led:
        rid = led.conn.execute(
            "SELECT round_id FROM runs").fetchone()["round_id"]
    assert rid == "R1"
    # 轮次制之前：新台账（rounds 空）+ 不带 round 字段的卡 → 照常运行
    legacy_ledger = tmp_path / "legacy.sqlite"
    legacy_cards = write_card(tmp_path, CARD_BODY, "H200.toml")
    legacy_cards.joinpath("H200.toml").write_text(
        CARD_BODY.replace('id = "H100"', 'id = "H200"'), encoding="utf-8")
    rc = run_screen(mod, legacy_cards, root, legacy_ledger, card="H200")
    assert rc == 0, capsys.readouterr().out
    with Ledger(legacy_ledger) as led:
        assert led.conn.execute(
            "SELECT round_id FROM runs").fetchone()["round_id"] is None


def test_screen_rejects_over_trial_budget(tmp_path, monkeypatch, capsys):
    """失败测试 4：累计去重试验数（本轮成功运行按七元组去重）达到
    trial_budget 后 → 拒绝新试验（不记 runs、退出码 1）。"""
    mod = load_bt()
    root1 = make_data_root(tmp_path / "d1")
    root2 = make_data_root(tmp_path / "d2")
    bars = h1_bars(6)
    bars["close"] = bars["close"] + 1.0        # 不同数据 → 不同 data_sha256
    bars.to_parquet(root2 / "bars" / "XAUUSD" / "H1" / "data.parquet")
    cards_dir = write_card(tmp_path)
    ledger_path = tmp_path / "ledger.sqlite"
    body = ROUND_TOML.replace("trial_budget = 40", "trial_budget = 1")
    assert open_round(mod, tmp_path, ledger_path, monkeypatch,
                      body=body) == 0
    patch_strategy_fp(monkeypatch, mod)
    rc = run_screen(mod, cards_dir, root1, ledger_path)
    assert rc == 0, capsys.readouterr().out
    rc = run_screen(mod, cards_dir, root2, ledger_path)   # 第 2 个试验超预算
    assert rc == 1
    out = capsys.readouterr().out
    assert "trial_budget" in out
    with Ledger(ledger_path) as led:
        n = led.conn.execute(
            "SELECT COUNT(*) FROM runs WHERE round_id='R1'").fetchone()[0]
    assert n == 1                              # 被拒的尝试不产生 runs 行


def test_gate_rejects_over_lockbox_uses_max(tmp_path, monkeypatch, capsys):
    """失败测试 5：本轮 G5 次数达 lockbox_uses_max → 拒（计数查台账：
    该轮内跑过的卡的 lockbox_uses 行数）。"""
    mod = load_bt()
    cards_dir = write_card(tmp_path)
    ledger_path = tmp_path / "ledger.sqlite"
    assert open_round(mod, tmp_path, ledger_path, monkeypatch) == 0
    with Ledger(ledger_path) as led:        # 同轮另一张卡已消耗一次 G5
        led.record_run(card_id="H101", card_version=1, strategy="s:X",
                       params={}, symbol="XAUUSD", timeframe="H1",
                       data_first="2024-01-01", data_last="2026-01-01",
                       data_sha256="a" * 64, engine_version="A-1",
                       cost_model_version="zero-v0", gates_hash="",
                       metrics={"net": 1.0}, actor="test", round_id="R1")
        led.record_lockbox_use("H101", approved_by="u", result={})
    patch_strategy_fp(monkeypatch, mod)
    rc = mod.main(
        ["gate", "--card", "H100", "--cards-dir", str(cards_dir),
         "--data-root", "data", "--main-store",
         "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--volume-lots", "0.01",
         "--gates", "G5", "--approve", "--approved-by", "user",
         "--params", "{}"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "lockbox_uses_max" in out and "R1" in out
    with Ledger(ledger_path) as led:
        assert led.conn.execute(
            "SELECT COUNT(*) FROM runs WHERE card_id='H100'"
        ).fetchone()[0] == 0


def test_close_then_run_rejected(tmp_path, monkeypatch, capsys):
    """失败测试 6：bt round close 后该轮的卡不能再 screen（gate 同理）；
    关闭生成 docs/research/round-R1-<日期>.md 骨架（--out 注入临时路径）。"""
    mod = load_bt()
    root = make_data_root(tmp_path / "data")
    cards_dir = write_card(tmp_path)
    ledger_path = tmp_path / "ledger.sqlite"
    rdir = write_round(tmp_path)
    with Ledger(ledger_path) as led:
        record_check(led, "C001", "pass")
        # 一成功一失败：报告须两者都列
        led.record_run(card_id="H100", card_version=1, strategy="s:X",
                       params={}, symbol="XAUUSD", timeframe="H1",
                       data_first="2024-01-01", data_last="2026-01-01",
                       data_sha256="a" * 64, engine_version="A-1",
                       cost_model_version="zero-v0", gates_hash="",
                       metrics={"net": 1.0}, actor="test", round_id="R1")
        led.record_run(card_id="H100", card_version=1, strategy="s:X",
                       params={}, symbol="XAUUSD", timeframe="H1",
                       data_first="2024-01-01", data_last="2026-01-01",
                       data_sha256="a" * 64, engine_version="A-1",
                       cost_model_version="zero-v0", gates_hash="",
                       metrics=None, error="ValueError: 爆了",
                       actor="test", round_id="R1")
    patch_git(monkeypatch, mod)
    assert mod.main(["round", "open", "R1", "--rounds-dir", str(rdir),
                     "--ledger", str(ledger_path)]) == 0
    report = tmp_path / "round-R1.md"
    rc = mod.main(["round", "close", "R1",
                   "--ledger", str(ledger_path), "--out", str(report)])
    assert rc == 0, capsys.readouterr().out
    text = report.read_text(encoding="utf-8")
    assert "R1" in text and "advance_rule" in text
    assert "G3 通过且 DSR" in text            # 推进规则原文
    assert "C001" in text
    assert "爆了" in text                     # 失败运行如实列入
    with Ledger(ledger_path) as led:
        assert led.round("R1")["status"] == "closed"
        assert led.verify_chain() == []
    # 关闭后再运行 → 拒
    patch_strategy_fp(monkeypatch, mod)
    rc = run_screen(mod, cards_dir, root, ledger_path)
    assert rc == 1
    out = capsys.readouterr().out
    assert "关闭" in out and "R1" in out
    with Ledger(ledger_path) as led:
        assert led.conn.execute(
            "SELECT COUNT(*) FROM runs WHERE card_id='H100'"
        ).fetchone()[0] == 2
    # 再关一次 → 拒
    rc = mod.main(["round", "close", "R1",
                   "--ledger", str(ledger_path), "--out", str(report)])
    assert rc == 1


def test_round_status_command(tmp_path, monkeypatch, capsys):
    mod = load_bt()
    ledger_path = tmp_path / "ledger.sqlite"
    assert open_round(mod, tmp_path, ledger_path, monkeypatch) == 0
    rc = mod.main(["round", "status", "R1", "--ledger", str(ledger_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "R1" in out and "open" in out and "C001" in out
    rc = mod.main(["round", "status", "R9", "--ledger", str(ledger_path)])
    assert rc == 1


def test_round_open_power_recheck_at_round_n(tmp_path, monkeypatch, capsys):
    """T84 失败测试：bt round open 按本轮 N（候选数，Bonferroni α/N）
    复核每个候选的检验力——只报告不阻塞；轮次报告骨架带"开轮检验力
    复核"一节，用**开轮时点最新**的预筛行（开轮后的新预筛不改开轮
    复核）。预筛本身仍按单次判定（蓝图 §6.2 条件 4）。"""
    from mt5quant.research import power

    mod = load_bt()
    ledger_path = tmp_path / "ledger.sqlite"
    body = (ROUND_TOML
            .replace('candidates = ["C001"]', 'candidates = ["C001", "C002"]')
            .replace(
                'C001 = { mode = "event", primary = "event_mean_return_one_sided" }',
                'C001 = { mode = "event", primary = "event_mean_return_one_sided" }\n'
                'C002 = { mode = "event", primary = "event_mean_return_one_sided" }'))
    rdir = write_round(tmp_path, body)
    # 单次检验力 59%（sharpe_used 0.65、10 年）≥ 50% 过预筛；α/N（N=2）
    # 复核后 46% < 50% —— 只报告，不阻塞开轮
    with Ledger(ledger_path) as led:
        for cid in ("C001", "C002"):
            led.record_candidate_check(cid, decision="pass", result={
                "candidate_id": cid, "decision": "pass",
                "checks": {"power": {"power": 0.588, "sharpe_used": 0.65,
                                     "years": 10.0, "n_tests": 1}}})
    patch_git(monkeypatch, mod)
    rc = mod.main(["round", "open", "R1", "--rounds-dir", str(rdir),
                   "--ledger", str(ledger_path)])
    assert rc == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "检验力复核" in out and "N=2" in out and "只报告" in out
    p_single = power.power_one_sided(0.65, 10.0, n_tests=1)
    p_at_n = power.power_one_sided(0.65, 10.0, n_tests=2)
    assert p_single >= 0.5 > p_at_n
    assert f"{p_at_n:.0%}" in out
    # 开轮后的新预筛（数据延长后重筛）不改开轮时点的复核
    with Ledger(ledger_path) as led:
        led.record_candidate_check("C001", decision="pass", result={
            "candidate_id": "C001", "decision": "pass",
            "checks": {"power": {"power": 0.99, "sharpe_used": 3.0,
                                 "years": 10.0, "n_tests": 1}}})
    report = tmp_path / "round-R1.md"
    rc = mod.main(["round", "close", "R1", "--ledger", str(ledger_path),
                   "--out", str(report)])
    assert rc == 0, capsys.readouterr().out
    text = report.read_text(encoding="utf-8")
    assert "开轮检验力复核" in text and "α/N" in text and "只报告" in text
    assert f"{p_single:.0%}" in text and f"{p_at_n:.0%}" in text
    p_late = power.power_one_sided(3.0, 10.0, n_tests=2)
    assert f"{p_late:.0%}" not in text, "开轮后的新预筛不得改开轮复核数字"


# ----------------------------------------------- rounds 表入哈希链（7）

def test_round_rows_in_hash_chain_tamper_detected(tmp_path, monkeypatch):
    """失败测试 7：rounds 记录在台账哈希链内——改写 trial_budget / 删行
    都被 verify_chain 报出，且事后写入 fail-closed（LedgerTamperedError）。"""
    mod = load_bt()
    ledger_path = tmp_path / "ledger.sqlite"
    assert open_round(mod, tmp_path, ledger_path, monkeypatch) == 0
    with Ledger(ledger_path) as led:
        assert led.verify_chain() == []
        with led.conn:   # 直连 sqlite 改预算（洗预算的典型改法）
            led.conn.execute("UPDATE rounds SET trial_budget=999")
        problems = led.verify_chain()
        assert any("rounds" in p and ("篡改" in p or "不符" in p)
                   for p in problems)
        with pytest.raises(LedgerTamperedError):
            led.record_run(card_id="H100", card_version=1, strategy="s:X",
                           params={}, symbol="XAUUSD", timeframe="H1",
                           data_first=None, data_last=None,
                           data_sha256="a" * 64, engine_version="A-1",
                           cost_model_version="zero-v0", gates_hash="",
                           metrics={"net": 1.0}, actor="test")
    # 删行（整轮抹掉重开）同样报出
    ledger2 = tmp_path / "ledger2.sqlite"
    assert open_round(mod, tmp_path, ledger2, monkeypatch,
                      name="R1") == 0
    with Ledger(ledger2) as led:
        with led.conn:
            led.conn.execute("DELETE FROM rounds")
        assert any("rounds" in p for p in led.verify_chain())


# ------------------------------------------------- runs.round_id 迁移与兼容

def test_record_run_round_id_roundtrip(tmp_path):
    with Ledger(tmp_path / "l.sqlite") as led:
        rid = led.record_run(card_id="H100", card_version=1, strategy="s:X",
                             params={}, symbol="XAUUSD", timeframe="H1",
                             data_first=None, data_last=None,
                             data_sha256="a" * 64, engine_version="A-1",
                             cost_model_version="zero-v0", gates_hash="",
                             metrics={"net": 1.0}, actor="test",
                             round_id="R1")
        v = led.conn.execute(
            "SELECT round_id FROM runs WHERE run_id=?",
            (rid,)).fetchone()["round_id"]
        assert v == "R1"
        assert led.verify_chain() == []
        assert led.round_n_dedup("R1") == 1
        assert led.round_n_dedup("R2") == 0


_PRE_T63_RUNS = """
CREATE TABLE runs(
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id TEXT NOT NULL, card_version INTEGER NOT NULL,
  strategy TEXT NOT NULL, params_json TEXT NOT NULL,
  symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
  data_first TEXT, data_last TEXT, data_sha256 TEXT NOT NULL,
  strategy_sha TEXT NOT NULL DEFAULT 'legacy',
  engine_version TEXT NOT NULL, cost_model_version TEXT NOT NULL,
  gates_hash TEXT NOT NULL DEFAULT '', metrics_json TEXT NOT NULL,
  created_utc TEXT NOT NULL, actor TEXT NOT NULL,
  prev_hash TEXT NOT NULL DEFAULT '', row_hash TEXT NOT NULL DEFAULT ''
);
"""


def test_pre_t63_ledger_migrates_round_id_and_rechains(tmp_path):
    """旧库（有哈希链、无 round_id 列）打开即迁移：补列（旧行 NULL=
    轮次制之前）、runs 链按新内容重算一次、verify 通过、可开轮次。"""
    p = tmp_path / "old.sqlite"
    conn = sqlite3.connect(str(p))
    conn.executescript(_PRE_T63_RUNS)
    conn.execute(
        "INSERT INTO runs(card_id, card_version, strategy, params_json,"
        " symbol, timeframe, data_sha256, strategy_sha, engine_version,"
        " cost_model_version, gates_hash, metrics_json, created_utc, actor,"
        " prev_hash, row_hash)"
        " VALUES ('H001',1,'s:BuyHold','{}','XAUUSD','H1',?,'x','A-1.1',"
        "'zero-v0','',?,'2026-10-01T00:00:00+00:00','bt','0','0')",
        ("a" * 64, json.dumps({"ok": True, "error": None,
                               "metrics": {"n": 1}})))
    conn.commit()
    conn.close()
    with Ledger(p) as led:
        cols = {r["name"] for r in led.conn.execute(
            "PRAGMA table_info(runs)")}
        assert "round_id" in cols
        assert led.verify_chain() == []          # 迁移重算后链自洽
        old = led.conn.execute(
            "SELECT round_id FROM runs WHERE actor='bt'").fetchone()
        assert old["round_id"] is None           # 旧行 = 轮次制之前
        names = {r["name"] for r in led.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "rounds" in names
        led.record_run(card_id="H100", card_version=1, strategy="s:X",
                       params={}, symbol="XAUUSD", timeframe="H1",
                       data_first=None, data_last=None, data_sha256="a" * 64,
                       engine_version="A-1", cost_model_version="zero-v0",
                       gates_hash="", metrics={"net": 1.0}, actor="test",
                       round_id="R1")
        assert led.verify_chain() == []
    with Ledger(p, create=False) as led2:        # 幂等：重开不再迁移
        assert led2.verify_chain() == []


def test_cards_round_candidate_fields_validated(tmp_path):
    """卡的 round/candidate 字段（附录 A + T63/D32 接线）：成对出现、
    格式合法；只写一个 → CardError。"""
    d = tmp_path / "cards"
    d.mkdir()
    good = d / "H100.toml"
    good.write_text(CARD_IN_ROUND, encoding="utf-8")
    card = cards_mod.load_card(good)
    assert card.round == "R1" and card.candidate == "C001"
    for mutate, frag in [
            ("round only", 'round = "R1"\n'),
            ("candidate only", 'candidate = "C001"\n'),
            ("bad round", 'round = "round-one"\ncandidate = "C001"\n'),
            ("bad candidate", 'round = "R1"\ncandidate = "H100"\n')]:
        p = d / "H101.toml"
        p.write_text(CARD_BODY.replace('id = "H100"', 'id = "H101"') + frag,
                     encoding="utf-8")
        with pytest.raises(cards_mod.CardError):
            cards_mod.load_card(p)


# ------------------------------------------------- screened_pass 口径（2 辅）

def test_candidate_screened_pass_latest_decision_wins(tmp_path):
    """screened_pass = 台账 candidate_checks 最新 decision == pass：
    旧 fail 新 pass → pass；旧 pass 新 fail → fail；无记录 → False。"""
    with Ledger(tmp_path / "l.sqlite") as led:
        record_check(led, "C001", "fail")
        record_check(led, "C001", "pass")
        assert rounds_mod.candidate_screened_pass(led, "C001") is True
        record_check(led, "C002", "pass")
        record_check(led, "C002", "fail")
        assert rounds_mod.candidate_screened_pass(led, "C002") is False
        assert rounds_mod.candidate_screened_pass(led, "C003") is False


def test_round_lockbox_uses_max_capped_at_one_per_round(tmp_path):
    """D16 机器强制（用户 2026-10-28 授权"按推荐做"）：每个研究轮次最多
    1 次 G5——此前靠 approved_by 把关（T63 审核观察 1），现装载层直接拒绝
    lockbox_uses_max > 1 的轮次文件。"""
    from mt5quant.research.rounds import RoundError, load_round

    def write_round(lockbox: int):
        p = tmp_path / "R9.toml"
        p.write_text(
            'id = "R9"\nopened = "2026-10-28"\ncandidates = ["C001"]\n'
            f"trial_budget = 10\nlockbox_uses_max = {lockbox}\n"
            'dsr_trial_scope = "project"\nadvance_rule = "x"\nclose_rule = "y"\n'
            'approved_by = "user"\n'
            '[evaluation]\nC001 = { mode = "event", primary = "p" }\n',
            encoding="utf-8")
        return p

    import pytest
    with pytest.raises(RoundError, match="每个研究轮次最多 1 次"):
        load_round(write_round(2))
    r = load_round(write_round(1))
    assert r.lockbox_uses_max == 1


# ------------------------------------- H-08：轮次/候选以冻结快照为准

import hashlib


def test_h08_round_file_modified_after_open_rejects_screen(tmp_path,
                                                           monkeypatch,
                                                           capsys):
    """H-08 失败测试 1：开轮后改轮次文件（预算/分界/假设表）→ 后续
    screen 在结果计算之前拒绝（当前文件 hash 与开启轮次入账的
    toml_sha256 不一致），不产生任何 runs。"""
    mod = load_bt()
    root = make_data_root(tmp_path / "data")
    cards_dir = write_card(tmp_path)
    ledger_path = tmp_path / "ledger.sqlite"
    assert open_round(mod, tmp_path, ledger_path, monkeypatch) == 0
    # 开轮后改预注册内容（trial_budget 40 → 41）
    rpath = tmp_path / "rounds" / "R1.toml"
    rpath.write_text(rpath.read_text(encoding="utf-8").replace(
        "trial_budget = 40", "trial_budget = 41"), encoding="utf-8")
    patch_strategy_fp(monkeypatch, mod)
    rc = run_screen(mod, cards_dir, root, ledger_path)
    assert rc == 1
    out = capsys.readouterr().out
    assert "R1.toml" in out and "不一致" in out
    with Ledger(ledger_path) as led:
        assert led.conn.execute(
            "SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_h08_candidate_modified_after_pass_requires_recheck(tmp_path,
                                                            monkeypatch,
                                                            capsys):
    """H-08 失败测试 2：候选 pass 记录绑定候选内容——预筛通过后改候选
    登记文件 → screen 拒（须重新 bt candidate check 绑定新内容）；重检
    后才放行。"""
    mod = load_bt()
    root = make_data_root(tmp_path / "data")
    cards_dir = write_card(tmp_path)
    cdir = tmp_path / "candidates"
    cdir.mkdir()
    cpath = cdir / "C001.toml"
    body = 'id = "C001"\ntitle = "月末效应"\n'
    cpath.write_text(body, encoding="utf-8")
    ledger_path = tmp_path / "ledger.sqlite"
    rdir = write_round(tmp_path)
    with Ledger(ledger_path) as led:
        led.record_candidate_check("C001", decision="pass", result={
            "candidate_id": "C001", "decision": "pass",
            "candidate_sha256": hashlib.sha256(
                cpath.read_bytes()).hexdigest()})
    patch_git(monkeypatch, mod)
    assert mod.main(["round", "open", "R1", "--rounds-dir", str(rdir),
                     "--ledger", str(ledger_path)]) == 0
    # 预筛通过后改候选登记
    cpath.write_text(body + '# tuned after pass\n', encoding="utf-8")
    patch_strategy_fp(monkeypatch, mod)
    rc = run_screen(mod, cards_dir, root, ledger_path)
    assert rc == 1
    out = capsys.readouterr().out
    assert "C001" in out and ("重新" in out or "candidate check" in out)
    with Ledger(ledger_path) as led:
        assert led.conn.execute(
            "SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    # 重新预筛（绑定新内容）后才放行
    with Ledger(ledger_path) as led:
        led.record_candidate_check("C001", decision="pass", result={
            "candidate_id": "C001", "decision": "pass",
            "candidate_sha256": hashlib.sha256(
                cpath.read_bytes()).hexdigest()})
    rc = run_screen(mod, cards_dir, root, ledger_path)
    assert rc == 0, capsys.readouterr().out


def test_h08_round_eval_mode_broken_file_fails_closed(tmp_path):
    """H-08 失败测试 3：_round_eval_mode 的模式文件坏了即失败——不再
    回退普通（bar 级）模式静默放行。"""
    mod = load_bt()
    cards_dir = write_card(tmp_path)
    rdir = write_round(tmp_path)
    (rdir / "R1.toml").write_text("id = [broken\n", encoding="utf-8")
    card = mod.cards.load_card(cards_dir / "H100.toml")
    with pytest.raises(RoundError):
        mod._round_eval_mode(card)
