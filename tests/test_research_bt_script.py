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
    # n<=6：旧口径递增收盘（既有手算断言用）；n>6：恒定 100（TR=1.0 →
    # ATR14 从 bar13 起恒 1.0，供 T23 定手数用例做确定手数）
    closes = ([100.0, 100.0, 101.0, 102.0, 103.0, 104.0][:n]
              if n <= 6 else [100.0] * n)
    # H-13：high/low 随 close 抬高（旧固定 100.5 在 close>100.5 后非法）；
    # open/close/time 不动——既有手算断言的数值语义不变
    opens = [100.0] * n
    df = pd.DataFrame({
        "time": times, "open": opens,
        "high": [max(o, c) + 0.5 for o, c in zip(opens, closes)],
        "low": [min(o, c) - 0.5 for o, c in zip(opens, closes)],
        "close": closes,
        "tick_volume": 10, "spread": 5, "real_volume": 0,
    })[_COLUMNS]
    return df


def make_data_root(tmp_path, n=6):
    root = tmp_path / "data"
    sdir = root / "bars" / "XAUUSD" / "H1"
    sdir.mkdir(parents=True)
    h1_bars(n).to_parquet(sdir / "data.parquet")
    specs = root / "specs"
    specs.mkdir(parents=True)
    # T23：快照含手数上下限（volume_min/step/max）——默认风险定手数可用
    (specs / "latest.json").write_text(
        json.dumps({"XAUUSD": {"contract_size": 100.0, "volume_min": 0.01,
                               "volume_step": 0.01, "volume_max": 200.0}}),
        encoding="utf-8")
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
    p.write_bytes(raw.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n'))
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


# ------------------------------------------------- T23（M2）：定手数口径


def test_screen_default_risk_sizing_records_metrics(tmp_path, monkeypatch,
                                                    capsys):
    """缺省（不给 --volume-lots/--risk-per-trade）= 风险定手数 0.005：
    交易手数 = 50/(ATR×合约)（bar 够长后入场），skipped_zero_lots 与
    sizing 方式随 metrics 落台账。"""
    mod = load_bt()
    root = make_data_root(tmp_path, n=20)          # 20 根：bar13 起 ATR=1.0
    # 末根收 105（高点覆盖）：入场 0.50 手 × 合约 100 × 涨 5 = 250
    f = root / "bars" / "XAUUSD" / "H1" / "data.parquet"
    df = h1_bars(20)
    df.loc[19, ["close", "high"]] = [105.0, 105.5]
    df.to_parquet(f)
    cards_dir = write_card(tmp_path)
    ledger_path = tmp_path / "ledger.sqlite"
    patch_card_git(monkeypatch, mod)
    rc = mod.main(
        ["screen", "--card", "H099", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero"])
    assert rc == 0
    with Ledger(ledger_path) as led:
        r = led.conn.execute("SELECT metrics_json FROM runs").fetchone()
    payload = json.loads(r["metrics_json"])
    m = payload["metrics"]
    assert m["trade_count"] == 1
    assert m["skipped_zero_lots"] == 12            # bar2..13（sig_bar 无 ATR）
    assert m["net_profit"] == 250.0                # 0.50 手（手数错则对不上）
    assert m["sizing"]["mode"] == "risk"
    assert m["sizing"]["risk_fraction"] == 0.005
    assert m["sizing"]["vol_min"] == 0.01


def test_screen_risk_and_volume_conflict_rejected(tmp_path, monkeypatch,
                                                  capsys):
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)
    patch_card_git(monkeypatch, mod)
    rc = mod.main(
        ["screen", "--card", "H099", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(tmp_path / "l.sqlite"),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--risk-per-trade", "0.006",
         "--volume-lots", "1.0"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "互斥" in out


def test_screen_risk_mode_without_volume_bounds_fails_assembly(tmp_path,
                                                               monkeypatch,
                                                               capsys):
    """快照缺 volume 字段 → 装配期报错（不产生 runs）。"""
    mod = load_bt()
    root = make_data_root(tmp_path)
    (root / "specs" / "latest.json").write_text(
        json.dumps({"XAUUSD": {"contract_size": 100.0}}), encoding="utf-8")
    cards_dir = write_card(tmp_path)
    patch_card_git(monkeypatch, mod)
    rc = mod.main(
        ["screen", "--card", "H099", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(tmp_path / "l.sqlite"),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "volume_min" in out or "snapshot_specs" in out
    # 装配失败不产生任何 runs（台账可能已建，但 runs 为空）
    assert not (tmp_path / "l.sqlite").exists() or Ledger(
        tmp_path / "l.sqlite", create=False).conn.execute(
        "SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_gate_risk_sizing_passes_spec_through_g2(tmp_path, monkeypatch,
                                                 capsys):
    """gate 默认风险定手数：基准运行与关卡（含 G2 的 sizing 透传链——
    成本放大/参数邻域/随机入场全走 run_engine）同一口径；metrics.sizing
    记录方式；G2 各子检查不因 sizing 接线抛错（结果过不过是另一回事）。"""
    mod = load_bt()
    root = make_data_root(tmp_path, n=20)
    cards_dir = write_card(tmp_path)
    ledger_path = tmp_path / "ledger.sqlite"
    patch_card_git(monkeypatch, mod)
    rc = mod.main(
        ["gate", "--card", "H099", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--gates", "G0,G2", "--params", "{}"])
    out = capsys.readouterr().out
    # rc=1：玩具数据上 G2 的若干子检查（去最好 5% 等）合法失败；G0 通过。
    # 本用例只验 sizing 接线：基准与 G2 全链跑完、无 error、口径记录正确。
    assert rc == 1, out
    assert "G0" in out and "G2" in out
    with Ledger(ledger_path) as led:
        r = led.conn.execute("SELECT metrics_json FROM runs").fetchone()
        g2 = led.conn.execute(
            "SELECT details_json FROM gates WHERE gate='G2'").fetchone()
    m = json.loads(r["metrics_json"])["metrics"]
    assert m["sizing"]["mode"] == "risk"
    assert m["skipped_zero_lots"] == 12
    det = json.loads(g2["details_json"])
    assert "error" not in det                   # G2 全部子检查跑完（无接线错误）
    assert set(det["checks"]) >= {"cost_scaling", "param_plateau",
                                  "random_entry_benchmark", "delay_1_bar"}


# ------------------------------------------------- T27（H3）：研究库切换


def make_research_root(tmp_path, n=6):
    """cwd 相对的研究库：bars + specs（研究侧不读主库 data/）。"""
    root = tmp_path / "data_research"
    sdir = root / "bars" / "XAUUSD" / "H1"
    sdir.mkdir(parents=True)
    h1_bars(n).to_parquet(sdir / "data.parquet")
    specs = root / "specs"
    specs.mkdir(parents=True)
    (specs / "latest.json").write_text(
        json.dumps({"XAUUSD": {"contract_size": 100.0, "volume_min": 0.01,
                               "volume_step": 0.01, "volume_max": 200.0}}),
        encoding="utf-8")
    return root


def test_screen_research_store_switch(tmp_path, monkeypatch, capsys):
    """--research-store：--data-root 缺省切到 cwd 的 data_research；台账
    缺省仍落 data/research/ledger.sqlite（permissions.md：研究库对研究
    账户只读，可写的台账目录是 data\research）。"""
    mod = load_bt()
    make_research_root(tmp_path)
    cards_dir = write_card(tmp_path)
    patch_card_git(monkeypatch, mod)
    monkeypatch.chdir(tmp_path)            # cwd 下只有 data_research，无 data/
    rc = mod.main(
        ["screen", "--card", "H099", "--cards-dir", str(cards_dir),
         "--research-store",
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--volume-lots", "0.01"])
    assert rc == 0, capsys.readouterr().out
    ledger_default = tmp_path / "data" / "research" / "ledger.sqlite"
    assert ledger_default.is_file()        # 台账不在只读的研究库里
    assert not (tmp_path / "data_research" / "research").exists()
    with Ledger(ledger_default) as led:
        assert led.stats("H099")["total_runs"] == 1


def test_screen_default_reads_research_store(tmp_path, monkeypatch, capsys):
    """T38/D24 翻转（原"缺省 data/"对照用例反转）：不带任何 flag 时缺省即
    研究库 data_research（cwd 下只有 data_research、无 data/，成功即证明）；
    台账缺省仍固定 data/research/ledger.sqlite（T27 语义不随缺省切换）。"""
    mod = load_bt()
    make_research_root(tmp_path)
    cards_dir = write_card(tmp_path)
    patch_card_git(monkeypatch, mod)
    monkeypatch.chdir(tmp_path)
    rc = mod.main(
        ["screen", "--card", "H099", "--cards-dir", str(cards_dir),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--volume-lots", "0.01"])
    assert rc == 0, capsys.readouterr().out
    ledger_default = tmp_path / "data" / "research" / "ledger.sqlite"
    assert ledger_default.is_file()        # 台账不在只读的研究库里
    with Ledger(ledger_default) as led:
        assert led.stats("H099")["total_runs"] == 1


def test_screen_main_store_requires_explicit_flag(tmp_path, monkeypatch,
                                                  capsys):
    """T38/D24：--data-root 指到主库（字面 data 或仓库内绝对路径）而未给
    --main-store → 拒绝并指路。"""
    mod = load_bt()
    make_research_root(tmp_path)
    cards_dir = write_card(tmp_path)
    patch_card_git(monkeypatch, mod)
    monkeypatch.chdir(tmp_path)
    argv = ["screen", "--card", "H099", "--cards-dir", str(cards_dir),
            "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
            "--costs", "zero", "--volume-lots", "0.01",
            "--ledger", str(tmp_path / "l.sqlite")]
    # 字面 "data"
    rc = mod.main(argv + ["--data-root", "data"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "--main-store" in out and "主库" in out
    # 仓库内绝对路径同样拦
    rc = mod.main(argv + ["--data-root", str(mod.REPO_ROOT / "data")])
    assert rc == 1
    assert "--main-store" in capsys.readouterr().out
    # --main-store → 守卫放行（cwd 无 data/，后续装配失败但不是守卫拒绝）
    rc = mod.main(argv + ["--data-root", "data", "--main-store"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "--main-store" not in out
    assert "装配失败" in out


def test_screen_missing_research_store_points_to_export(tmp_path, monkeypatch,
                                                        capsys):
    """T38/D24：缺省（研究库模式）下 data_research 不存在 → 报错指路
    export_research_store（绝不静默回退主库）。"""
    mod = load_bt()
    cards_dir = write_card(tmp_path)
    patch_card_git(monkeypatch, mod)
    monkeypatch.chdir(tmp_path)            # cwd 空无一物
    rc = mod.main(
        ["screen", "--card", "H099", "--cards-dir", str(cards_dir),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--volume-lots", "0.01",
         "--ledger", str(tmp_path / "l.sqlite")])
    assert rc == 1
    out = capsys.readouterr().out
    assert "data_research" in out and "export_research_store" in out


def test_screen_ops_ack_rejected_as_research_command(tmp_path, monkeypatch,
                                                     capsys):
    """R6/C6 反转（原 T38 放行用例）：--ops-ack 不能用于研究命令 screen——
    运维豁免仅供 reconcile_ab/measure 类运维命令，不能参与研究关卡与试验
    统计；不带 --ops-ack 时锁箱边界仍如实报出（锁箱语义不变）。"""
    from mt5data.lockbox import write_config

    mod = load_bt()
    root = make_data_root(tmp_path)
    write_config(str(root), {"XAUUSD/H1": "2026-01-05T12:00:00"})  # bars 中段
    cards_dir = write_card(tmp_path)
    patch_card_git(monkeypatch, mod)
    patch_strategy_fp(monkeypatch, mod, "a" * 64)
    argv = ["screen", "--card", "H099", "--cards-dir", str(cards_dir),
            "--data-root", str(root), "--ledger", str(tmp_path / "l.sqlite"),
            "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
            "--costs", "zero", "--volume-lots", "0.01",
            "--end", "2026-01-05 20:00"]          # 越过 cutoff 12:00
    # 1) 不带 --ops-ack → 锁箱边界如实报出，不产生 runs（锁箱语义不变）
    rc = mod.main(list(argv))
    assert rc == 1
    assert "锁箱" in capsys.readouterr().out
    # 2) 带 --ops-ack → 直接拒绝（C6）：不产生任何 runs
    rc = mod.main(argv + ["--ops-ack"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "ops-ack" in out and "运维" in out
    assert "reconcile_ab" in out or "关卡" in out
    with Ledger(tmp_path / "l.sqlite") as led:
        assert led.stats("H099")["total_runs"] == 0


def test_gate_ops_ack_rejected_as_research_command(tmp_path, monkeypatch,
                                                   capsys):
    """R6/C6：gate 同样拒绝 --ops-ack（运维豁免不得参与研究关卡）。"""
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)
    ledger_path = tmp_path / "l.sqlite"
    patch_card_git(monkeypatch, mod)
    rc = mod.main(
        ["gate", "--card", "H099", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--volume-lots", "0.01",
         "--gates", "G0", "--params", "{}", "--ops-ack"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "ops-ack" in out and "运维" in out
    with Ledger(ledger_path) as led:
        assert led.stats("H099")["total_runs"] == 0   # 拒绝时不产生 runs


def test_gate_g5_custom_data_root_rejected(tmp_path, monkeypatch, capsys):
    """R6/C6：G5 的主库校验对所有数据根模式生效——自定义 --data-root
    （classify_store=custom）不能绕过 --main-store 授权，一律拒绝且
    不开台账（无 runs / lockbox_uses）。"""
    mod = load_bt()
    root = make_data_root(tmp_path)              # tmp 根 = custom（非主库）
    cards_dir = write_card(tmp_path)
    patch_card_git(monkeypatch, mod)
    rc = mod.main(
        ["gate", "--card", "H099", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(tmp_path / "l.sqlite"),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--volume-lots", "0.01",
         "--gates", "G5", "--approve", "--approved-by", "user",
         "--params", "{}"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "--main-store" in out
    assert "自定义" in out or "custom" in out
    assert not (tmp_path / "l.sqlite").exists()  # 拒绝发生在开台账之前


def test_gate_g5_refused_in_research_mode(tmp_path, monkeypatch, capsys):
    """T38/D24：G5 需读主库锁箱段——研究库模式（缺省即研究库）下一律拒绝
    并指路 --main-store（即使已 --approve）；台账/锁箱不产生任何行。"""
    mod = load_bt()
    make_research_root(tmp_path)
    cards_dir = write_card(tmp_path)
    patch_card_git(monkeypatch, mod)
    patch_strategy_fp(monkeypatch, mod, "a" * 64)
    monkeypatch.chdir(tmp_path)
    rc = mod.main(
        ["gate", "--card", "H099", "--cards-dir", str(cards_dir),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--volume-lots", "0.01",
         "--gates", "G5", "--approve", "--approved-by", "user",
         "--ledger", str(tmp_path / "l.sqlite")])
    assert rc == 1
    out = capsys.readouterr().out
    assert "--main-store" in out and "锁箱" in out
    # 拒绝发生在开台账之前：库文件未创建（无 runs、无 lockbox_uses）
    assert not (tmp_path / "l.sqlite").exists()


# ------------------------------------------------- T37（H3/M6/D23/D27）

def patch_strategy_fp(monkeypatch, mod, sha):
    """注入策略指纹与干净检查（git 交互按用例控制）。"""
    monkeypatch.setattr(mod.cards, "ensure_strategy_clean",
                        lambda ref, root, **kw: None)
    monkeypatch.setattr(mod.cards, "strategy_fingerprint",
                        lambda ref, root, **kw: sha)


def run_screen(mod, cards_dir, root, ledger_path, monkeypatch, sha,
               card="H099"):
    patch_strategy_fp(monkeypatch, mod, sha)
    return mod.main(
        ["screen", "--card", card, "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--volume-lots", "0.01"])


def test_screen_records_strategy_sha(tmp_path, monkeypatch):
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)
    ledger_path = tmp_path / "ledger.sqlite"
    patch_card_git(monkeypatch, mod)
    assert run_screen(mod, cards_dir, root, ledger_path, monkeypatch,
                      "a" * 64) == 0
    with Ledger(ledger_path) as led:
        r = led.conn.execute("SELECT strategy_sha FROM runs").fetchone()
    assert r["strategy_sha"] == "a" * 64


def test_screen_refuses_changed_sha_same_card_version(tmp_path, monkeypatch,
                                                      capsys):
    """计划用例 2：首跑登记指纹后代码改动（指纹不同）→ 同卡版本拒绝运行，
    提示新建版本；同指纹重跑不拒绝。"""
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)
    ledger_path = tmp_path / "ledger.sqlite"
    patch_card_git(monkeypatch, mod)
    assert run_screen(mod, cards_dir, root, ledger_path, monkeypatch,
                      "a" * 64) == 0
    rc = run_screen(mod, cards_dir, root, ledger_path, monkeypatch, "b" * 64)
    assert rc == 1
    out = capsys.readouterr().out
    assert "指纹" in out and "版本" in out
    with Ledger(ledger_path) as led:
        assert led.stats("H099")["total_runs"] == 1   # 拒绝时不产生 runs
    assert run_screen(mod, cards_dir, root, ledger_path, monkeypatch,
                      "a" * 64) == 0                   # 原指纹照常可跑
    with Ledger(ledger_path) as led:
        assert led.stats("H099")["total_runs"] == 2


def test_screen_rejects_uncommitted_strategy(tmp_path, monkeypatch, capsys):
    """计划用例 3：策略文件未提交 → 拒绝运行（不产生 runs）。"""
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)
    ledger_path = tmp_path / "ledger.sqlite"
    patch_card_git(monkeypatch, mod)

    def boom(ref, root_, **kw):
        raise mod.cards.StrategyDirtyError(
            "策略文件 src/mt5quant/strategies/library/buy_hold.py 未提交到"
            " git（D23：策略文件须已提交且干净）")
    monkeypatch.setattr(mod.cards, "ensure_strategy_clean", boom)
    rc = mod.main(
        ["screen", "--card", "H099", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--volume-lots", "0.01"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "未提交" in out and "策略文件" in out
    with Ledger(ledger_path) as led:
        assert led.stats("H099")["total_runs"] == 0


def test_new_card_version_after_code_change_counts_n_plus_one(tmp_path,
                                                              monkeypatch):
    """计划用例 1（端到端）：改代码后同参数重跑 → 须新建卡版本 H099-v2，
    谱系去重 N 由 1 变 2（指纹进七元组去重键）。"""
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)
    v2 = CARD_BODY.replace('id = "H099"', 'id = "H099-v2"').replace(
        "version = 1", 'version = 2\nparent = "H099"')
    (cards_dir / "H099-v2.toml").write_text(v2, encoding="utf-8")
    ledger_path = tmp_path / "ledger.sqlite"
    patch_card_git(monkeypatch, mod)
    assert run_screen(mod, cards_dir, root, ledger_path, monkeypatch,
                      "1" * 64, card="H099") == 0
    assert run_screen(mod, cards_dir, root, ledger_path, monkeypatch,
                      "2" * 64, card="H099-v2") == 0
    with Ledger(ledger_path) as led:
        st = led.stats("H099")
        assert st["family"] == "H099"
        assert st["family_n_dedup"] == 2          # N 加 1（代码版本不同）
        assert st["n_dedup"] == 2


def test_bt_ledger_verify_subcommand(tmp_path, capsys):
    mod = load_bt()
    ledger_path = tmp_path / "ledger.sqlite"
    with Ledger(ledger_path) as led:
        for i in (1, 2):
            led.record_run(
                card_id="H099", card_version=1, strategy="s:BuyHold",
                params={"i": i}, symbol="XAUUSD", timeframe="H1",
                data_first="2024-01-01", data_last="2026-01-01",
                data_sha256="x" * 64, engine_version="A-1.1",
                cost_model_version="zero-v0", gates_hash="",
                metrics={"net": 1.0}, actor="test")
        assert led.verify_chain() == []
    rc = mod.main(["ledger", "verify", "--ledger", str(ledger_path)])
    assert rc == 0
    assert "完好" in capsys.readouterr().out
    with Ledger(ledger_path) as led:              # 手工改一行 → verify 报出
        with led.conn:
            led.conn.execute(
                "UPDATE runs SET metrics_json='{\"ok\":true}'"
                " WHERE run_id=1")
    rc = mod.main(["ledger", "verify", "--ledger", str(ledger_path)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "run#1" in out and "篡改" in out


def test_bt_ledger_verify_missing_ledger(tmp_path, capsys):
    mod = load_bt()
    rc = mod.main(["ledger", "verify",
                   "--ledger", str(tmp_path / "missing.sqlite")])
    assert rc == 1
    assert "✗" in capsys.readouterr().out


# ------------------------------------------------- T37：run_backtest 接线

def load_rb():
    spec = importlib.util.spec_from_file_location(
        "rb_script_t37", REPO_ROOT / "scripts" / "run_backtest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rb_argv(root, out):
    return ["run_backtest.py", "--strategy",
            "mt5quant.strategies.library.buy_hold:BuyHold",
            "--symbol", "XAUUSD", "--timeframe", "H1", "--params", "{}",
            "--data-root", str(root), "--out", str(out),
            "--costs", "zero", "--contract-size", "100",
            "--volume-lots", "0.01", "--allow-zero-swap"]


def test_run_backtest_refuses_dirty_strategy(tmp_path, monkeypatch, capsys):
    """run_backtest 同样先过 ensure_strategy_clean（D23）。"""
    mod = load_rb()
    root = make_data_root(tmp_path)
    monkeypatch.chdir(tmp_path)               # 时钟校验读 cwd 的 config.json

    def boom(ref, root_, **kw):
        raise mod.cards.StrategyDirtyError(
            "策略文件 src/mt5quant/strategies/library/buy_hold.py 未提交到"
            " git（D23）")
    monkeypatch.setattr(mod.cards, "ensure_strategy_clean", boom)
    monkeypatch.setattr("sys.argv", _rb_argv(root, tmp_path / "out"))
    assert mod.main() == 1
    assert "未提交" in capsys.readouterr().out
    assert not (tmp_path / "out").exists() or not list(
        (tmp_path / "out").iterdir())


def test_run_backtest_manifest_carries_strategy_sha(tmp_path, monkeypatch):
    mod = load_rb()
    root = make_data_root(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mod.cards, "ensure_strategy_clean",
                        lambda ref, root_, **kw: None)
    monkeypatch.setattr(mod.cards, "strategy_fingerprint",
                        lambda ref, root_, **kw: "c" * 64)
    monkeypatch.setattr("sys.argv", _rb_argv(root, tmp_path / "out"))
    assert mod.main() == 0
    run_dir = sorted((tmp_path / "out").iterdir())[-1]
    manifest = json.loads((run_dir / "manifest.json").read_text(
        encoding="utf-8"))
    assert manifest["strategy_sha"] == "c" * 64


# ------------------------------------------------- T61（D30）：撤回与标定样品

def write_candidate(tmp_path, cid, status, source_card, extra=""):
    """T61：候选登记 research/candidates/C###.toml（cards 目录的兄弟目录；
    最小字段集——id/status/source_card，T62 迁移到蓝图附录 C 完整装载器）。"""
    d = tmp_path / "candidates"
    d.mkdir(exist_ok=True)
    (d / f"{cid}.toml").write_text(
        f'id = "{cid}"\nstatus = "{status}"\nsource_card = "{source_card}"\n'
        + extra, encoding="utf-8")
    return d


def test_screen_rejects_withdrawn_card(tmp_path, monkeypatch, capsys):
    """T61/D30 失败测试 1：候选登记 status=withdrawn 的卡 → screen 拒绝
    （消息含 withdrawn），不产生任何 runs。"""
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)
    write_candidate(tmp_path, "C907", "withdrawn", "H099")
    ledger_path = tmp_path / "ledger.sqlite"
    patch_card_git(monkeypatch, mod)
    rc = mod.main(
        ["screen", "--card", "H099", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--volume-lots", "0.01"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "withdrawn" in out and "撤回" in out
    with Ledger(ledger_path) as led:
        assert led.stats("H099")["total_runs"] == 0


def test_gate_rejects_withdrawn_card(tmp_path, monkeypatch, capsys):
    """T61/D30 失败测试 2：撤回卡同样拒绝 gate（关卡也是"运行"）。"""
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)
    write_candidate(tmp_path, "C907", "withdrawn", "H099")
    ledger_path = tmp_path / "ledger.sqlite"
    patch_card_git(monkeypatch, mod)
    rc = mod.main(
        ["gate", "--card", "H099", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--volume-lots", "0.01",
         "--gates", "G0", "--params", "{}"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "withdrawn" in out and "撤回" in out
    with Ledger(ledger_path) as led:
        assert led.stats("H099")["total_runs"] == 0


def test_screen_specimen_card_requires_flag(tmp_path, monkeypatch, capsys):
    """T61/D30 失败测试 3：标定样品卡（status=specimen）不带 --specimen →
    拒绝运行（样品只用于校准，不作研究候选），不产生任何 runs。"""
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)
    write_candidate(tmp_path, "C900", "specimen", "H099")
    ledger_path = tmp_path / "ledger.sqlite"
    patch_card_git(monkeypatch, mod)
    rc = mod.main(
        ["screen", "--card", "H099", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--volume-lots", "0.01"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "--specimen" in out and "标定样品" in out
    with Ledger(ledger_path) as led:
        assert led.stats("H099")["total_runs"] == 0


def test_screen_specimen_actor_calibration(tmp_path, monkeypatch, capsys):
    """T61/D30 失败测试 4：标定样品卡带 --specimen → 正常运行入台账，
    actor 强制为 calibration（不进入研究轮次统计）。"""
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)
    write_candidate(tmp_path, "C900", "specimen", "H099")
    ledger_path = tmp_path / "ledger.sqlite"
    patch_card_git(monkeypatch, mod)
    rc = mod.main(
        ["screen", "--card", "H099", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--volume-lots", "0.01",
         "--specimen", "--actor", "bt-screen"])
    assert rc == 0, capsys.readouterr().out
    assert "actor=calibration" in capsys.readouterr().out
    with Ledger(ledger_path) as led:
        r = led.conn.execute("SELECT actor FROM runs").fetchone()
    assert r["actor"] == "calibration"


def test_gate_specimen_actor_calibration(tmp_path, monkeypatch, capsys):
    """T61/D30 失败测试 5：gate 路径同样——样品卡带 --specimen 才跑，
    基准运行 actor=calibration 入账。"""
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)
    write_candidate(tmp_path, "C900", "specimen", "H099")
    ledger_path = tmp_path / "ledger.sqlite"
    patch_card_git(monkeypatch, mod)
    rc = mod.main(
        ["gate", "--card", "H099", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--volume-lots", "0.01",
         "--gates", "G0", "--params", "{}", "--specimen"])
    out = capsys.readouterr().out
    assert "G0" in out                              # 基准运行 + 关卡跑完
    with Ledger(ledger_path) as led:
        r = led.conn.execute("SELECT actor FROM runs").fetchone()
    assert r is not None and r["actor"] == "calibration"


def test_screen_specimen_flag_on_ordinary_card_rejected(tmp_path, monkeypatch,
                                                        capsys):
    """T61/D30 失败测试 6：--specimen 用于非标定样品卡 → 拒绝（标定通道
    不能给研究卡让统计逃逸），不产生任何 runs。"""
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)            # 无候选登记 → 普通研究卡
    ledger_path = tmp_path / "ledger.sqlite"
    patch_card_git(monkeypatch, mod)
    rc = mod.main(
        ["screen", "--card", "H099", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--volume-lots", "0.01", "--specimen"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "标定样品" in out
    with Ledger(ledger_path) as led:
        assert led.stats("H099")["total_runs"] == 0


def test_repo_candidate_registrations_specimen_and_withdrawn():
    """T61/D30：仓库 research/candidates/ 登记守护——C900–C906 把 H001–H007
    登记为 status=specimen（写明"不作研究候选"）；三张 v2 卡登记为
    status=withdrawn，撤回理由逐条写明、运行次数如实（0 次，不计入 N）；
    cards.candidate_statuses_by_card 缺省目录解析到同一结论。"""
    import tomllib

    from mt5quant.research import cards as cards_mod

    cdir = REPO_ROOT / "research" / "candidates"
    assert cdir.is_dir(), f"缺候选登记目录：{cdir}"
    specs = {
        f"C{900 + i}": card for i, card in enumerate(
            ("H001", "H002", "H003", "H004", "H005", "H006", "H007"))}
    for cid, card in specs.items():
        p = cdir / f"{cid}.toml"
        assert p.is_file(), f"缺标定样品登记：{p}"
        data = tomllib.loads(p.read_text(encoding="utf-8"))
        assert data.get("status") == "specimen", (cid, data.get("status"))
        assert data.get("source_card") == card
        assert "不作研究候选" in data.get("notes", "")
    reasons = {
        "C907": ("H002-v2", "样本内"),           # 网格/预期笔数取自 v3 样本内
        "C908": ("H003-v2", "成本"),             # 日内持仓，事前成本不过
        "C909": ("H006-v2", "成本"),
    }
    for cid, (card, kw) in reasons.items():
        p = cdir / f"{cid}.toml"
        assert p.is_file(), f"缺撤回登记：{p}"
        data = tomllib.loads(p.read_text(encoding="utf-8"))
        assert data.get("status") == "withdrawn", (cid, data.get("status"))
        assert data.get("source_card") == card
        assert kw in data.get("notes", ""), (cid, data.get("notes"))
        assert "0 次" in data.get("notes", ""), cid   # 运行次数如实写明
    # 缺省目录（仓库 research/candidates/）→ 卡状态映射与上面逐条一致
    statuses = cards_mod.candidate_statuses_by_card()
    for card in ("H001", "H002", "H003", "H004", "H005", "H006", "H007"):
        assert statuses.get(card) == "specimen", card
    for v2 in ("H002-v2", "H003-v2", "H006-v2"):
        assert statuses.get(v2) == "withdrawn", v2


# ================================================ T64/T80：G4 取数口径与运行类别

def test_g4_trials_default_family_scope_bitwise_unchanged(tmp_path):
    """T64/D33 冻结失败测试：缺省（现行 gates.toml 无 dsr_trial_scope 键）
    family 口径的 N 与试验夏普与现行**逐位一致**——calibration 运行仍计入
    谱系 N（现行语义），migration 行不计；家系外不计。

    D34 冻结语义变更（2026-10-29，如实更新）：gates.toml（v3）显式写
    dsr_trial_scope = "rounds_lineage"（L1）——配置缺省不再是 family；
    family 的取数语义本身不变（本测试主体逐位保留，见
    test_g4_trials_rounds_lineage_scope_matches_primary_test_n）。"""
    import math as _math

    mod = load_bt()
    assert mod.vgate.load_gates()[0].dsr_trial_scope == "rounds_lineage"
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
        led.record_run(params={"k": 7}, metrics={"sharpe": 5.0},
                       actor="calibration", **common)   # 冻结语义：谱系 N 仍计
        other = dict(common, card_id="H999")        # 家族外不计
        led.record_run(params={"k": 1}, metrics={"sharpe": 9.0}, actor="s",
                       **other)
        led.record_run(params={"k": 1}, metrics={"sharpe": 4.0},
                       actor="migration", **common)     # 迁移行不计
        n, sharpes = mod._dedup_trial_sharpes(led, "H700", 6_000.0)
        assert n == 3          # k=1、k=2、k=7（calibration 冻结计入）
        assert sorted(sharpes) == sorted([1.0 / _math.sqrt(6000.0),
                                          2.0 / _math.sqrt(6000.0),
                                          5.0 / _math.sqrt(6000.0)])


def test_g4_trials_project_scope_counts_project_research_only(tmp_path):
    """T64/D33 失败测试 1（G4 取数）：dsr_trial_scope=project——两谱系各
    10 次试验 → 项目 N=20（family 口径 N 含标定为 11）；calibration 运行
    不计（run_class=research 才计）。"""
    mod = load_bt()
    with Ledger(tmp_path / "l.sqlite") as led:
        t = "mt5quant.strategies.library.buy_hold:BuyHold"
        for fam, sym in (("H701", "XAUUSD"), ("H702", "EURUSD")):
            for i in range(1, 11):
                led.record_run(card_id=fam, card_version=1, strategy=t,
                               params={"i": i}, symbol=sym, timeframe="H1",
                               data_first="", data_last="",
                               data_sha256="a" * 64, engine_version="A",
                               cost_model_version="v", gates_hash="",
                               metrics={"sharpe": 1.0}, actor="s")
        led.record_run(card_id="H701", card_version=1, strategy=t,
                       params={"i": 99}, symbol="XAUUSD", timeframe="H1",
                       data_first="", data_last="", data_sha256="a" * 64,
                       engine_version="A", cost_model_version="v",
                       gates_hash="", metrics={"sharpe": 1.0},
                       actor="calibration")
        n_fam, _ = mod._dedup_trial_sharpes(led, "H701", 6_000.0)
        n_proj, sharpes = mod._dedup_trial_sharpes(
            led, "H701", 6_000.0, scope="project")
        assert n_fam == 11                       # family 口径（冻结）含标定
        assert n_proj == 20                      # 项目（research）口径：两谱系
        assert len(sharpes) == 20
        with pytest.raises(ValueError, match="scope"):
            mod._dedup_trial_sharpes(led, "H701", 6_000.0, scope="global")


def test_g4_trials_rounds_lineage_scope_matches_primary_test_n(tmp_path):
    """T85 冻结（R14 §2 条件 1/L1）失败测试：D34 冻结（2026-10-29）后
    [g4].dsr_trial_scope = "rounds_lineage"——bt 的 G4 取数按
    ledger.primary_test_n 的门槛部分计数：全项目已开启轮次内的去重研究
    试验（跨轮、跨谱系）+ 候选所延续谱系的轮次外研究历史；轮次外其余
    谱系、标定与迁移行不计。"""
    import math as _math

    mod = load_bt()
    assert mod.vgate.load_gates()[0].dsr_trial_scope == "rounds_lineage"
    with Ledger(tmp_path / "l.sqlite") as led:
        t = "mt5quant.strategies.library.buy_hold:BuyHold"
        common = dict(card_version=1, strategy=t, symbol="XAUUSD",
                      timeframe="H1", data_first="", data_last="",
                      data_sha256="a" * 64, engine_version="A",
                      cost_model_version="v", gates_hash="")
        # 轮次内：H701 两条 + H702 一条（跨轮跨谱系，全项目计）
        led.record_run(card_id="H701", params={"r": 1},
                       metrics={"sharpe": 1.0}, actor="s", round_id="R1",
                       **common)
        led.record_run(card_id="H701", params={"r": 2},
                       metrics={"sharpe": 2.0}, actor="s", round_id="R1",
                       **common)
        led.record_run(card_id="H702", params={"o": 1},
                       metrics={"sharpe": 3.0}, actor="s", round_id="R2",
                       **common)
        # H701 轮次外历史（被评估卡 H701 所延续的谱系）
        led.record_run(card_id="H701", params={"h": 1},
                       metrics={"sharpe": 4.0}, actor="s", **common)
        # 不计：轮次外其余谱系 / 标定 / 迁移
        led.record_run(card_id="H999", params={"x": 1},
                       metrics={"sharpe": 9.0}, actor="s", **common)
        led.record_run(card_id="H701", params={"c": 1},
                       metrics={"sharpe": 5.0}, actor="calibration",
                       round_id="R1", **common)
        led.record_run(card_id="H701", params={"m": 1},
                       metrics={"sharpe": 6.0}, actor="migration",
                       round_id="R1", **common)
        n, sharpes = mod._dedup_trial_sharpes(led, "H701", 6_000.0,
                                              scope="rounds_lineage")
        assert n == 4          # 轮次内 3 + H701 谱系轮次外 1
        assert sorted(sharpes) == sorted(
            [s / _math.sqrt(6000.0) for s in (1.0, 2.0, 3.0, 4.0)])
        assert n == led.primary_test_n("H701")["n"]   # 与台账口径一致
        # 函数缺省仍是 family（T64 冻结语义：谱系内轮次内外全计、标定
        # 计入、迁移不计）——H701 的 r1/r2/h1/c1 四键去重
        n_def, _ = mod._dedup_trial_sharpes(led, "H701", 6_000.0)
        assert n_def == 4



@pytest.mark.parametrize("reserved", ["calibration", "ops", "migration"])
def test_screen_rejects_reserved_actor_names(tmp_path, monkeypatch, capsys,
                                             reserved):
    """T80/G4 失败测试 3：--actor 保留名（calibration/ops/migration）拒绝
    ——运行类别由 bt 按候选状态与轮次写 run_class 字段，不看 actor 文本。"""
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)
    ledger_path = tmp_path / "ledger.sqlite"
    patch_card_git(monkeypatch, mod)
    rc = mod.main(
        ["screen", "--card", "H099", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--volume-lots", "0.01", "--actor", reserved])
    out = capsys.readouterr().out
    assert rc == 1
    assert "保留" in out and reserved in out
    with Ledger(ledger_path) as led:
        assert led.stats("H099")["total_runs"] == 0


def test_gate_rejects_reserved_actor_names(tmp_path, monkeypatch, capsys):
    """T80/G4：gate 路径同样拒绝保留 actor 名（不产生任何 runs）。"""
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)
    ledger_path = tmp_path / "ledger.sqlite"
    patch_card_git(monkeypatch, mod)
    rc = mod.main(
        ["gate", "--card", "H099", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--volume-lots", "0.01",
         "--gates", "G0", "--params", "{}", "--actor", "ops"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "保留" in out
    with Ledger(ledger_path) as led:
        assert led.stats("H099")["total_runs"] == 0


def test_screen_run_class_research_and_specimen_calibration(tmp_path,
                                                            monkeypatch,
                                                            capsys):
    """T80/G4 失败测试 4：样品卡（--specimen）运行 run_class=calibration；
    普通研究卡 run_class=research（按候选状态写入，不看 actor 文本）。"""
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)
    write_candidate(tmp_path, "C900", "specimen", "H099")
    ledger_path = tmp_path / "ledger.sqlite"
    patch_card_git(monkeypatch, mod)
    rc = mod.main(
        ["screen", "--card", "H099", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--volume-lots", "0.01", "--specimen"])
    assert rc == 0, capsys.readouterr().out
    with Ledger(ledger_path) as led:
        r = led.conn.execute(
            "SELECT actor, run_class FROM runs").fetchone()
    assert r["actor"] == "calibration"
    assert r["run_class"] == "calibration"
    # 普通研究卡（独立目录、无候选登记）：run_class=research
    (tmp_path / "plain").mkdir()
    cards_dir2 = write_card(tmp_path / "plain")
    ledger2 = tmp_path / "l2.sqlite"
    rc2 = mod.main(
        ["screen", "--card", "H099", "--cards-dir", str(cards_dir2),
         "--data-root", str(root), "--ledger", str(ledger2),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--volume-lots", "0.01"])
    assert rc2 == 0, capsys.readouterr().out
    with Ledger(ledger2) as led2:
        r2 = led2.conn.execute(
            "SELECT actor, run_class FROM runs").fetchone()
    assert r2["run_class"] == "research"


def test_gate_g4_reports_three_trial_counts(tmp_path, monkeypatch, capsys):
    """T64/D33 失败测试：G4 关卡结果同时列谱系/轮次/项目三种 N 与
    dsr_trial_scope；报告 Markdown 同列。

    D34 冻结语义变更（2026-10-29，如实更新）：gates.toml（v3）显式写
    dsr_trial_scope = "rounds_lineage"（T85 冻结 L1）——trial_scope 记录
    冻结口径（原"现行 gates.toml 缺省 family"）；谱系/轮次/项目三种 N
    照列（可审计）。"""
    mod = load_bt()
    root = make_data_root(tmp_path)
    cards_dir = write_card(tmp_path)
    ledger_path = tmp_path / "ledger.sqlite"
    patch_card_git(monkeypatch, mod)
    rc = mod.main(
        ["gate", "--card", "H099", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--volume-lots", "0.01",
         "--gates", "G4", "--params", "{}"])
    out = capsys.readouterr().out
    assert "G4" in out
    with Ledger(ledger_path) as led:
        row = led.conn.execute(
            "SELECT details_json FROM gates WHERE gate='G4'").fetchone()
    assert row is not None
    details = json.loads(row["details_json"])
    assert details["trial_scope"] == "rounds_lineage"   # D34 冻结口径
    assert details["n_trials_family"] >= 1
    assert details["n_trials_round"] is None          # 轮次制之前
    assert details["n_trials_project_research"] >= 1
    assert "G4 试验计数" in out                       # 报告同列三种 N


def test_gate_g4_round_n_when_card_in_round(tmp_path, monkeypatch, capsys):
    """T64/D33（审核次要 3）直接测试：卡带轮次时 G4 的 n_trials_round 非
    None（= 本轮去重试验数，基准运行已计入），报告"轮次 N"行同列。"""
    from datetime import datetime, timezone

    mod = load_bt()
    root = make_data_root(tmp_path)
    body = CARD_BODY.replace(
        "[rules]", 'round = "R1"\ncandidate = "C001"\n\n[rules]')
    cards_dir = write_card(tmp_path, body)
    ledger_path = tmp_path / "ledger.sqlite"
    # H-08：轮次以冻结快照为准——铺台账行的同时铺出内容一致的轮次文件
    # （toml_sha256 = 文件真实哈希），否则 _round_context 按快照失联拒绝
    import hashlib
    rnd_dir = tmp_path / "rounds"
    rnd_dir.mkdir(exist_ok=True)
    rnd_body = ('id = "R1"\nopened = "2026-10-27"\ncandidates = ["C001"]\n'
                'trial_budget = 10\nlockbox_uses_max = 1\n'
                'dsr_trial_scope = "family"\nadvance_rule = "按 advance_rule"'
                '\nclose_rule = "按 close_rule"\napproved_by = "user"\n'
                '\n[evaluation]\n'
                'C001 = { mode = "standard", primary = "dsr_v0" }\n')
    (rnd_dir / "R1.toml").write_text(rnd_body, encoding="utf-8")
    with Ledger(ledger_path) as led:          # 直接铺轮次上下文（绕过 CLI
        led.record_candidate_check(           # 前置：候选最新 decision=pass）
            "C001", decision="pass", result={"candidate_id": "C001"})
        led.record_round_open(
            round_id="R1", candidates=("C001",), trial_budget=10,
            lockbox_uses_max=1, dsr_trial_scope="family",
            advance_rule="按 advance_rule", close_rule="按 close_rule",
            approved_by="user", evaluation={},
            toml_sha256=hashlib.sha256(
                (rnd_dir / "R1.toml").read_bytes()).hexdigest(),
            git_commit="0" * 40)
    patch_card_git(monkeypatch, mod,
                   committed=datetime(2026, 10, 27, tzinfo=timezone.utc))
    rc = mod.main(
        ["gate", "--card", "H099", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--volume-lots", "0.01",
         "--gates", "G4", "--params", "{}"])
    out = capsys.readouterr().out
    assert "G4" in out
    with Ledger(ledger_path) as led:
        row = led.conn.execute(
            "SELECT details_json FROM gates WHERE gate='G4'").fetchone()
        rid = led.conn.execute(
            "SELECT round_id FROM runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()["round_id"]
    assert rid == "R1"                        # 基准运行确实落在轮次内
    details = json.loads(row["details_json"])
    assert details["n_trials_round"] == 1     # 轮次 N = 本轮去重试验数
    assert "轮次 N：1" in out                 # 报告行同列（非 None 分支）


# ================================== T86：关卡 v3 的 bt 接线（T85 冻结之后）

_ROUND_R1_EVENT = """\
id = "R1"
opened = "2026-10-27"
candidates = ["C003"]
trial_budget = 40
lockbox_uses_max = 1
dsr_trial_scope = "rounds_lineage"
advance_rule = "事件模式候选按 [v3.event] 随机化参照 p<=alpha/N 且成本x1.5 净均值为正"
close_rule = "预算用尽或全部候选有结论即结束"
approved_by = "用户A"

[evaluation]
C003 = { mode = "event", primary = "event_mean_return_one_sided" }
"""

_ROUND_R1_STANDARD = _ROUND_R1_EVENT.replace(
    'C003 = { mode = "event", primary = "event_mean_return_one_sided" }',
    'C003 = { mode = "standard", primary = "dsr_v0" }')

_CARD_H703 = """\
id = "H703"
version = 1
symbols = ["XAUUSD"]
timeframe = "H1"
session = "全时段"
phenomenon = "月末资金流"
mechanism = "固定日历资金流"
should_fail_when = "月末效应消失"
falsified_if = "样本内净利为负"
graveyard_checked = []
round = "R1"
candidate = "C003"

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


def make_daily_data_root(tmp_path, *, start="2024-01-01", days=800):
    """事件模式数据根：逐日一根 bar（合成日历，月末事件可构造）。"""
    root = tmp_path / "data"
    sdir = root / "bars" / "XAUUSD" / "H1"
    sdir.mkdir(parents=True)
    times = pd.date_range(start, periods=days, freq="D")
    closes = 100.0 + (pd.Series(range(days)) * 0.01)
    df = pd.DataFrame({
        "time": times, "open": closes.tolist(), "high": closes.tolist(),
        "low": closes.tolist(), "close": closes.tolist(),
        "tick_volume": 1, "spread": 0, "real_volume": 0,
    })[_COLUMNS]
    df.to_parquet(sdir / "data.parquet")
    specs = root / "specs"
    specs.mkdir(parents=True)
    (specs / "latest.json").write_text(
        json.dumps({"XAUUSD": {"contract_size": 100.0, "volume_min": 0.01,
                               "volume_step": 0.01, "volume_max": 200.0}}),
        encoding="utf-8")
    return root


def _setup_t86_round(tmp_path, monkeypatch, mod, *, mode="event",
                     continues_family="", pre_runs=0):
    """装 T86 用具：数据根 + 轮次（开启）+ 卡 + 候选登记（declared
    continues_family）+ git 注入；pre_runs 为轮内预置研究运行数。"""
    root = make_daily_data_root(tmp_path)
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir(exist_ok=True)
    (cards_dir / "H703.toml").write_text(_CARD_H703, encoding="utf-8")
    cdir = tmp_path / "candidates"
    cdir.mkdir(exist_ok=True)
    body = 'id = "C003"\n'
    if continues_family:
        body += f'continues_family = "{continues_family}"\n'
    (cdir / "C003.toml").write_text(body, encoding="utf-8")
    rdir = tmp_path / "rounds"
    rdir.mkdir(exist_ok=True)
    (rdir / "R1.toml").write_text(
        _ROUND_R1_EVENT if mode == "event" else _ROUND_R1_STANDARD,
        encoding="utf-8")
    ledger_path = tmp_path / "ledger.sqlite"
    with Ledger(ledger_path) as led:
        led.record_candidate_check("C003", decision="pass",
                                   result={"candidate_id": "C003",
                                           "decision": "pass"})
    patch_card_git(monkeypatch, mod)
    monkeypatch.setattr(mod.cards, "ensure_strategy_clean",
                        lambda ref, root_dir, **kw: None)
    monkeypatch.setattr(mod.cards, "strategy_fingerprint",
                        lambda ref, root_dir, **kw: "f" * 64)
    rc = mod.main(["round", "open", "R1", "--rounds-dir", str(rdir),
                   "--ledger", str(ledger_path)])
    assert rc == 0, "轮次开启前置未过（用具故障，非被测行为）"
    if pre_runs:
        with Ledger(ledger_path) as led:
            for i in range(pre_runs):
                led.record_run(
                    card_id="H703", card_version=1,
                    strategy="mt5quant.strategies.library.buy_hold:BuyHold",
                    params={"seed": i}, symbol="XAUUSD", timeframe="H1",
                    data_first="", data_last="", data_sha256="a" * 64,
                    engine_version="A", cost_model_version="v",
                    gates_hash="", metrics={"sharpe": 1.0}, actor="s",
                    round_id="R1")
    return root, cards_dir, ledger_path


def test_gate_v3_event_card_skips_buyhold_and_delay_vetoes(
        tmp_path, monkeypatch, capsys):
    """T86 失败测试 1：轮次 [evaluation] mode="event" 的卡，关卡 V3 走
    事件分支——随机化参照主检验（p ≤ alpha/N，阈值随 N 收紧）+ 成本 ×1.5
    否决；**买入持有超额与延迟一根两项否决不运行**（D35 修改 2：标记
    不适用，绝不调用对应实现）。"""
    mod = load_bt()
    root, cards_dir, ledger_path = _setup_t86_round(
        tmp_path, monkeypatch, mod, mode="event")

    def _must_not_run(*a, **k):        # 事件分支调用即失败
        raise AssertionError("事件模式不得运行 bar 级否决")

    monkeypatch.setattr(mod.vgate, "v3_veto_buyhold", _must_not_run)
    monkeypatch.setattr(mod.vgate, "v3_veto_delay", _must_not_run)
    rc = mod.main(
        ["gate", "--card", "H703", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--gates", "V3", "--params", "{}",
         "--event-kind", "month_end", "--event-side", "long"])
    out = capsys.readouterr().out
    assert "V3" in out and "事件模式" in out
    with Ledger(ledger_path) as led:
        row = led.conn.execute(
            "SELECT details_json FROM gates WHERE gate='V3'").fetchone()
    assert row is not None, out
    det = json.loads(row["details_json"])
    assert det["mode"] == "event"
    assert det["primary"]["test"] == "randomization"
    assert det["primary"]["threshold"] == pytest.approx(0.05 / 1)  # N=1（本次运行）
    assert det["vetoes"]["buyhold_excess"]["applicable"] is False
    assert det["vetoes"]["delay_1_bar"]["applicable"] is False
    assert "error" not in det and "AssertionError" not in json.dumps(det)
    assert set(det["diagnostics"]) == {
        "year_block_vs_zero", "window_shift_1_bar", "segmented_means",
        "known_gaps"}


def test_gate_v3_event_draws_below_floor_refuses_run(
        tmp_path, monkeypatch, capsys):
    """T86 失败测试 2：事件分支随机化次数下限 max(1999, ceil(20N/alpha))
    ——预置 5 条轮内研究运行（本次运行为第 6 条，N=6 → 下限 2400），
    --event-sims 2000 低于下限 → **拒绝运行**（不产生任何 runs）。"""
    mod = load_bt()
    root, cards_dir, ledger_path = _setup_t86_round(
        tmp_path, monkeypatch, mod, mode="event", pre_runs=5)
    rc = mod.main(
        ["gate", "--card", "H703", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--gates", "V3", "--params", "{}",
         "--event-kind", "month_end", "--event-sims", "2000"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "随机化次数" in out and "2400" in out and "拒绝" in out
    with Ledger(ledger_path) as led:
        n = led.conn.execute(
            "SELECT COUNT(*) FROM runs WHERE card_id='H703'").fetchone()[0]
    assert n == 5      # 只有预置的 5 条——拒绝发生在基准运行之前


def test_gate_v3_bar_card_n_composition_matches_ledger_declared_lineage(
        tmp_path, monkeypatch, capsys):
    """T86 失败测试 3：bar 级 V3 的 N 组成与台账 primary_test_n 一致——
    轮次内（含本次运行）+ 候选**声明**的 continues_family 谱系（H401，≠
    卡自身谱系 H703——审核 §3 语义提示：按候选声明取）+ 其余历史只报告；
    三行进关卡报告与 gates 表 details。"""
    mod = load_bt()
    root, cards_dir, ledger_path = _setup_t86_round(
        tmp_path, monkeypatch, mod, mode="standard",
        continues_family="H401", pre_runs=2)
    common = dict(card_version=1,
                  strategy="mt5quant.strategies.library.buy_hold:BuyHold",
                  symbol="XAUUSD", timeframe="H1", data_first="",
                  data_last="", data_sha256="b" * 64, engine_version="A",
                  cost_model_version="v", gates_hash="",
                  metrics={"sharpe": 1.0}, actor="s")
    with Ledger(ledger_path) as led:
        for i in range(3):                     # H401 谱系轮次外历史（lineage）
            led.record_run(card_id=f"H401-v{i + 1}", params={"h": i},
                           round_id=None, **common)
        for i in range(5):                     # 其余谱系轮次外（只报告）
            led.record_run(card_id=f"H999-v{i + 1}", params={"x": i},
                           round_id=None, **common)
    rc = mod.main(
        ["gate", "--card", "H703", "--cards-dir", str(cards_dir),
         "--data-root", str(root), "--ledger", str(ledger_path),
         "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
         "--costs", "zero", "--gates", "V3", "--params", "{}"])
    out = capsys.readouterr().out
    with Ledger(ledger_path) as led:
        expect = led.primary_test_n("H401")    # 按候选声明的谱系
        wrong = led.primary_test_n("H703")     # 按卡自身谱系（不得采用）
    assert expect["n"] == 6 and wrong["n"] == 3   # 3 轮内(2预置+本次)+3 lineage
    assert "轮次内" in out and "延续谱系" in out and "其余历史" in out
    assert "N 的组成" in out
    assert str(expect["project_n_dedup_rounds"]) in out
    assert str(expect["lineage_n_dedup"]) in out
    assert str(expect["pre_round_other_n_dedup"]) in out
    with Ledger(ledger_path) as led:
        row = led.conn.execute(
            "SELECT details_json FROM gates WHERE gate='V3'").fetchone()
    det = json.loads(row["details_json"])
    comp = det["n_composition"]
    assert comp["n"] == expect["n"]
    assert comp["continues_family"] == "H401"
    assert comp["project_n_dedup_rounds"] == expect["project_n_dedup_rounds"]
    assert comp["lineage_n_dedup"] == expect["lineage_n_dedup"]
    assert comp["pre_round_other_n_dedup"] == expect["pre_round_other_n_dedup"]
    assert det["mode"] == "bar"


def test_gate_v3_keeps_v2_gate_order_untouched():
    """T86 守护：V2 的 G0–G5 关卡顺序不变（V3 追加，不插入/改名）——
    现行 G1–G4 路径与守护测试不受接线影响。"""
    mod = load_bt()
    order = mod.vgate.GATE_ORDER
    assert order[:6] == ("G0", "G1", "G2", "G3", "G4", "G5")
    assert "V3" in order

