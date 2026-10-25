"""scripts/bt.py（T11）：card check / screen --card / ledger stats。

screen 端到端用临时数据根（parquet K 线 + specs 快照 + 零成本），
"先卡后测"的 git 交互按用例注入或如实走真实 git（tmp 在仓库外 → None）。
"""
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from mt5data.store import _COLUMNS
from mt5quant.research.ledger import Ledger

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "bt.py"
REPO_ROOT = Path(__file__).resolve().parents[1]
PAST = datetime(2026, 1, 1, tzinfo=timezone.utc)


def load_bt():
    spec = importlib.util.spec_from_file_location("bt_script", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def h1_bars(n=6, tf="H1"):
    times = pd.date_range("2026-01-05 10:00", periods=n, freq="h")
    df = pd.DataFrame({
        "time": times, "open": 100.0, "high": 100.5, "low": 99.5,
        "close": [100.0, 100.0, 101.0, 102.0, 103.0, 104.0][:n],
        "tick_volume": 10, "spread": 5, "real_volume": 0,
    })[_COLUMNS]
    return df


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
id = "H099"
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


def write_card(tmp_path, body=CARD_BODY, name="H099.toml"):
    d = tmp_path / "cards"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(body, encoding="utf-8")
    return d


def patch_card_git(monkeypatch, mod, *, committed=PAST, clean=True):
    """注入卡文件的 git 状态（T21d：committed_at + worktree_clean 两问）。"""
    monkeypatch.setattr(mod.cards, "committed_at",
                        lambda path, runner=None: committed)
    monkeypatch.setattr(mod.cards, "worktree_clean",
                        lambda path, runner=None: clean)


# ---------------------------------------------------------------- card check

def test_card_check_valid_repo_card(capsys):
    mod = load_bt()
    rc = mod.main(["card", "check",
                   str(REPO_ROOT / "research" / "cards" / "H001.toml")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "H001" in out
    assert "XAUUSD" in out


def test_card_check_invalid_card(tmp_path, capsys):
    mod = load_bt()
    cards_dir = write_card(tmp_path, CARD_BODY.replace('timeframe = "H1"',
                                                       'timeframe = "H7"'))
    rc = mod.main(["card", "check", str(cards_dir / "H099.toml")])
    assert rc == 1
    assert "timeframe" in capsys.readouterr().out


def test_card_check_missing_file(capsys):
    mod = load_bt()
    rc = mod.main(["card", "check", "Z:/nowhere/H000.toml"])
    assert rc == 1
    assert "✗" in capsys.readouterr().out


# ---------------------------------------------------------------- screen

def test_screen_rejects_uncommitted_card(tmp_path, capsys):
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)
    ledger_path = tmp_path / "ledger.sqlite"
    rc = mod.main(
        ["screen", "--card", "H099", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "先卡后测" in out or "未提交" in out
    with Ledger(ledger_path) as led:   # 拒绝时不产生任何 runs 行
        assert led.stats("H099")["total_runs"] == 0


def test_screen_records_runs_and_card(tmp_path, monkeypatch, capsys):
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)
    ledger_path = tmp_path / "ledger.sqlite"
    patch_card_git(monkeypatch, mod)
    rc = mod.main(
        ["screen", "--card", "H099", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--volume-lots", "0.01"])
    assert rc == 0
    with Ledger(ledger_path) as led:
        rs = led.conn.execute("SELECT * FROM runs").fetchall()
        assert len(rs) == 1
        r = rs[0]
        assert r["card_id"] == "H099"
        assert r["symbol"] == "XAUUSD"
        assert r["timeframe"] == "H1"
        assert json.loads(r["params_json"]) == {}
        assert r["engine_version"]  # 非空（与 engine_a.ENGINE_VERSION 一致）
        payload = json.loads(r["metrics_json"])
        assert payload["ok"] is True
        # 手算：入场 bar1 open=100，末根 close=104，0.01 手 × contract 100
        assert payload["metrics"]["net_profit"] == pytest.approx(4.0)
        assert payload["metrics"]["trade_count"] == 1
        cs = led.conn.execute("SELECT * FROM cards").fetchall()
        assert len(cs) == 1
        assert cs[0]["committed_at_utc"] == PAST.isoformat()


def test_screen_failed_combos_still_recorded(tmp_path, monkeypatch, capsys):
    """卡参数与策略 schema 不符：每个组合抛错但都有一行 runs，退出码 1。"""
    mod = load_bt()
    root = make_data_root(tmp_path)
    body = CARD_BODY.replace("[params]\n",
                             "[params]\nk = [1, 2]\n")
    cards_dir = write_card(tmp_path, body)
    ledger_path = tmp_path / "ledger.sqlite"
    patch_card_git(monkeypatch, mod)
    rc = mod.main(
        ["screen", "--card", "H099", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero"])
    assert rc == 1
    with Ledger(ledger_path) as led:
        st = led.stats("H099")
        assert st["total_runs"] == 2
        assert st["failed_runs"] == 2
        rs = led.conn.execute("SELECT params_json, metrics_json FROM runs"
                              ).fetchall()
        assert [json.loads(r["params_json"])["k"] for r in rs] == [1, 2]
        assert all("未知参数" in json.loads(r["metrics_json"])["error"]
                   for r in rs)


def test_screen_requires_contract_size_source(tmp_path, monkeypatch, capsys):
    mod = load_bt()
    root = make_data_root(tmp_path)
    (root / "specs" / "latest.json").unlink()
    cards_dir = write_card(tmp_path)
    patch_card_git(monkeypatch, mod)
    rc = mod.main(
        ["screen", "--card", "H099", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(tmp_path / "ledger.sqlite"),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero"])
    assert rc == 1
    assert "合约大小" in capsys.readouterr().out


def test_screen_accepts_direct_card_path(tmp_path, monkeypatch):
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)
    patch_card_git(monkeypatch, mod)
    rc = mod.main(
        ["screen", "--card", str(cards_dir / "H099.toml"),
         "--data-root", str(root), "--ledger", str(tmp_path / "l.sqlite"),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero"])
    assert rc == 0


# ---------------------------------------------------------------- ledger stats

def test_ledger_stats_prints_counts(tmp_path, capsys):
    mod = load_bt()
    ledger_path = tmp_path / "ledger.sqlite"
    with Ledger(ledger_path) as led:
        for i in range(2):
            led.record_run(
                card_id="H099", card_version=1, strategy="s:BuyHold",
                params={"i": i}, symbol="XAUUSD", timeframe="H1",
                data_first="2024-01-01", data_last="2026-01-01",
                data_sha256="x" * 64, engine_version="A-1.1",
                cost_model_version="zero-v0", gates_hash="",
                metrics={"net": 1.0}, actor="test")
    rc = mod.main(["ledger", "stats", "--card", "H099",
                   "--ledger", str(ledger_path)])
    assert rc == 0
    st = json.loads(capsys.readouterr().out)
    assert st["card_id"] == "H099"
    assert st["total_runs"] == 2
    assert st["ok_runs"] == 2
    assert st["failed_runs"] == 0


def test_ledger_stats_missing_ledger(tmp_path, capsys):
    mod = load_bt()
    rc = mod.main(["ledger", "stats",
                   "--ledger", str(tmp_path / "missing.sqlite")])
    assert rc == 1
    assert "✗" in capsys.readouterr().out


# ------------------------------------------------- T21e 接线（P6/P7/M4/M5）

def test_screen_rejects_dirty_worktree_card(tmp_path, monkeypatch, capsys):
    """P6 计划失败测试：卡文件改了没提交（worktree_clean=False）→ 拒绝。"""
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)
    ledger_path = tmp_path / "ledger.sqlite"
    patch_card_git(monkeypatch, mod, clean=False)      # committed_at 仍是 PAST
    rc = mod.main(
        ["screen", "--card", "H099", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "工作区" in out or "未提交改动" in out
    with Ledger(ledger_path) as led:   # 拒绝时不产生任何 runs 行
        assert led.stats("H099")["total_runs"] == 0


def test_screen_rejects_strategy_mismatch(tmp_path, monkeypatch, capsys):
    """T21d 接线：卡绑定 strategy 与 --strategy 不一致 → 拒绝运行。"""
    mod = load_bt()
    root = make_data_root(tmp_path)
    body = CARD_BODY.replace(
        'id = "H099"',
        'id = "H099"\nstrategy = "mt5quant.strategies.library.buy_hold:BuyHold"')
    cards_dir = write_card(tmp_path, body)
    ledger_path = tmp_path / "ledger.sqlite"
    patch_card_git(monkeypatch, mod)
    rc = mod.main(
        ["screen", "--card", "H099", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.b1_donchian_breakout"
                       ":DonchianBreakout",
         "--costs", "zero"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "不一致" in out
    with Ledger(ledger_path) as led:
        assert led.stats("H099")["total_runs"] == 0


def test_screen_legacy_card_migration_hint_printed_once(tmp_path, monkeypatch,
                                                        capsys):
    """T21d 接线：旧卡（无 strategy 字段）沿用 --strategy，迁移提示只打一次。"""
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)            # CARD_BODY 无 strategy → 旧卡
    patch_card_git(monkeypatch, mod)
    rc = mod.main(
        ["screen", "--card", "H099", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(tmp_path / "l.sqlite"),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero"])
    assert rc == 0
    assert capsys.readouterr().out.count("迁移提示") == 1


def test_card_check_rejects_unknown_selection_rule(tmp_path, capsys):
    """M5：selection_rule 非法值在定义时（card check）拒绝。"""
    mod = load_bt()
    body = CARD_BODY.replace("graveyard_checked = []",
                             'graveyard_checked = []\n'
                             'selection_rule = "max_moon_phase"')
    cards_dir = write_card(tmp_path, body)
    rc = mod.main(["card", "check", str(cards_dir / "H099.toml")])
    assert rc == 1
    out = capsys.readouterr().out
    assert "selection_rule" in out and "max_moon_phase" in out


def test_gates_hash_lf_normalized_under_crlf(tmp_path):
    """P7 计划失败测试：CRLF 检出下 bt 的 gates_hash 与关卡侧同一口径
    （LF 归一；裸字节哈希会得到另一个值）。"""
    mod = load_bt()
    raw = (REPO_ROOT / "config" / "gates.toml").read_bytes()
    p = tmp_path / "gates.toml"
    p.write_bytes(raw.replace(b"\n", b"\r\n"))
    assert b"\r\n" in p.read_bytes()          # 夹具自检：确为 CRLF
    assert mod._gates_hash(p) == mod.vgate.gates_sha256(p)
    assert mod._gates_hash(p) == mod.vgate.load_gates()[1]   # 与 gate 侧一致
    assert mod._gates_hash(p) == mod.vgate.FROZEN_GATES_SHA256


def test_g4_trials_dedup_same_params_counted_once(tmp_path):
    """T21c/M4 接线：同一组参数先 screen 后 gate，N 与试验夏普都只计 1 次；
    失败运行与 sharpe=None 的成功运行不计入试验夏普（N 按 ok 运行去重）。"""
    import math as _math

    mod = load_bt()
    with Ledger(tmp_path / "l.sqlite") as led:
        common = dict(card_id="H700", card_version=1, strategy="s:SmaK",
                      symbol="XAUUSD", timeframe="H1", data_first="",
                      data_last="", data_sha256="a" * 64,
                      engine_version="A-1.3", cost_model_version="zero-v0",
                      gates_hash="")
        led.record_run(params={"k": 1}, metrics={"sharpe": 1.0}, actor="s",
                       **common)
        led.record_run(params={"k": 1}, metrics={"sharpe": 1.0}, actor="g",
                       **common)                    # 同参 gate 重复 → 只计 1
        led.record_run(params={"k": 2}, metrics={"sharpe": 2.0}, actor="s",
                       **common)
        led.record_run(params={"k": 3}, metrics=None, error="boom", actor="s",
                       **common)                    # 失败运行不计
        led.record_run(params={"k": 4}, metrics={"trade_count": 5}, actor="s",
                       **common)                    # ok 但无夏普 → 计 N 不计夏普
        other = dict(common, card_id="H999")        # 家族外不计
        led.record_run(params={"k": 1}, metrics={"sharpe": 9.0}, actor="s",
                       **other)
        v2 = dict(common, card_id="H700-v2", card_version=2)   # 同谱系新版本
        led.record_run(params={"k": 1}, metrics={"sharpe": 1.0}, actor="s",
                       **v2)
        n, sharpes = mod._dedup_trial_sharpes(led, "H700", 6_000.0)
        assert n == 3          # k=1（screen+gate+v2 合一）、k=2、k=4（无夏普）
        assert sorted(sharpes) == sorted([1.0 / _math.sqrt(6000.0),
                                          2.0 / _math.sqrt(6000.0)])
