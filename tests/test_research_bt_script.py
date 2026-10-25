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
    df = pd.DataFrame({
        "time": times, "open": 100.0, "high": 100.5, "low": 99.5,
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


def test_screen_ops_ack_unlocks_lockbox_and_records_actor_ops(tmp_path,
                                                              monkeypatch,
                                                              capsys):
    """T38/D24：读锁箱期（--end 越过 cutoff）须显式 --ops-ack——不给 →
    LockboxError（装配失败、无 runs）；给了 → 放行且台账 actor=ops
    （Global Constraints：锁箱期数据只用于运维测量，台账记 actor=ops）。"""
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
    # 1) 不带 --ops-ack → 锁箱边界如实报出，不产生 runs
    rc = mod.main(list(argv))
    assert rc == 1
    assert "锁箱" in capsys.readouterr().out
    # 2) 带 --ops-ack → 放行；运行行 actor=ops（审计口径）
    rc = mod.main(argv + ["--ops-ack"])
    assert rc == 0, capsys.readouterr().out
    with Ledger(tmp_path / "l.sqlite") as led:
        actors = [r["actor"] for r in led.conn.execute(
            "SELECT actor FROM runs").fetchall()]
    assert actors == ["ops"]


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
