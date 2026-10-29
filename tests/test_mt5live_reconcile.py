"""mt5live.reconcile（T14/P7 + T30/D19/D20）：意图 vs actual 持仓对账。

差分三情形（missing / unexpected / mismatch）+ pending 宽限 + 意图文件
读写契约（新格式与旧格式兼容）+ 按策略暂停（pause-<magic>.json）+
live_reconcile CLI（fake 网关取 actual，不连 MT5）。
"""
from __future__ import annotations

import importlib.util
import json
from collections import namedtuple
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mt5gw.models import Position
from mt5live.reconcile import (
    Intent,
    ExpectedStateError,
    diff_positions,
    load_intents,
    write_intent,
    write_pause,
)
from mt5gw.gateway import MT5Gateway
from tests.fakes import FakeMT5

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "live_reconcile.py"

NOW = datetime(2026, 10, 26, 14, 30, 0)
M1 = 20261101
M2 = 20261102
VOL_TOL = 1e-6
GRACE_S = 60.0


def pos(symbol="XAUUSD", volume=0.2, *, side="buy", magic=M1,
        ticket=9001) -> Position:
    return Position(
        ticket=ticket, symbol=symbol, side=side, volume=volume,
        price_open=100.0, price_current=100.0, sl=0.0, tp=0.0,
        swap=0.0, profit=0.0, time=NOW, comment="", magic=magic,
    )


def intent(symbol="XAUUSD", magic=M1, side="buy", volume=0.2, *,
           status="confirmed", cause="d1", ts=NOW) -> Intent:
    return Intent(symbol=symbol, magic=magic, side=side, volume=volume,
                  status=status, cause=cause, ts=ts)


# ---- 差分三情形 ----

def test_diff_clean_ok():
    res = diff_positions((intent(),), [pos("XAUUSD", 0.2)], now=NOW)
    assert res.ok and res.diffs == ()
    assert not res.pause


def test_diff_missing_expected_not_in_account():
    res = diff_positions((intent(),), [], now=NOW)
    assert not res.ok and res.pause
    assert len(res.diffs) == 1
    d = res.diffs[0]
    assert d.kind == "missing"
    assert (d.symbol, d.magic) == ("XAUUSD", M1)
    assert d.expected_net == pytest.approx(0.2)
    assert d.actual_net == 0.0


def test_diff_unexpected_in_account_only():
    # M1 受管（意图声明了 XAUUSD 空仓），账户却持有 M1 的 EURUSD
    res = diff_positions((intent(volume=0.0),),        # 声明空仓
                         [pos("EURUSD", 0.1, side="sell", magic=M1)], now=NOW)
    assert not res.ok
    assert len(res.diffs) == 1
    d = res.diffs[0]
    assert d.kind == "unexpected"
    assert (d.symbol, d.magic) == ("EURUSD", M1)
    assert d.expected_net == 0.0
    assert d.actual_net == pytest.approx(-0.1)  # sell 记负


def test_diff_mismatch_volume_and_side():
    exp = (intent(),)
    res = diff_positions(exp, [pos("XAUUSD", 0.15)], now=NOW)
    assert not res.ok
    assert res.diffs[0].kind == "mismatch"

    res2 = diff_positions(exp, [pos("XAUUSD", 0.2, side="sell")], now=NOW)
    assert not res2.ok
    assert res2.diffs[0].kind == "mismatch"


# ---- magic 过滤与净额合并 ----

def test_unmanaged_magic_positions_ignored():
    # 非意图中出现的 magic（含 0=手动）不参与对账
    actual = [pos("EURUSD", 5.0, magic=0), pos("USTEC", 1.0, magic=999999)]
    res = diff_positions((), actual, now=NOW)
    assert res.ok and res.diffs == ()


def test_actual_positions_net_by_symbol_magic():
    actual = [pos("XAUUSD", 0.1, ticket=1), pos("XAUUSD", 0.1, ticket=2)]
    res = diff_positions((intent(),), actual, now=NOW)
    assert res.ok  # 0.1 + 0.1 净额 = 0.2


def test_zero_volume_intent_means_flat():
    res = diff_positions((intent(volume=0.0),), [], now=NOW)
    assert res.ok  # 期望 0 手 = 无持仓


def test_volume_tolerance_avoids_float_noise():
    actual = [pos("XAUUSD", 0.1 + 0.2)]  # 0.30000000000000004
    res = diff_positions((intent(volume=0.3),), actual, now=NOW)
    assert res.ok


# ---- T30/D19：pending 宽限（下单在途不判）----

def test_pending_within_grace_not_judged():
    """pending 且 now - ts < grace_s：该 magic 本轮整体不判——即便账户
    还没出现持仓也不算 missing（对账恰在"已决定、订单未回"空档运行）。"""
    pend = intent(status="pending", ts=NOW - timedelta(seconds=30))
    res = diff_positions((pend,), [], now=NOW)
    assert res.ok and res.diffs == ()
    # 该 magic 的多余持仓同样不判（unexpected 也不报）
    res2 = diff_positions((pend,), [pos("XAUUSD", 0.5)], now=NOW)
    assert res2.ok


def test_pending_beyond_grace_is_judged():
    """pending 超过宽限仍未确认 → 按目标比对（下单卡死要被发现）。"""
    stale = intent(status="pending", ts=NOW - timedelta(seconds=GRACE_S + 1))
    res = diff_positions((stale,), [], now=NOW)
    assert not res.ok
    assert res.diffs[0].kind == "missing"


def test_pending_grace_does_not_shield_other_magics():
    """pending 宽限只跳过该 magic，别的 magic 照常判。"""
    pend = intent(status="pending", ts=NOW - timedelta(seconds=10))
    other = intent("EURUSD", M2, "sell", 0.1, cause="d2")
    res = diff_positions((pend, other), [pos("EURUSD", 0.1, side="sell",
                                             magic=M2)], now=NOW)
    assert res.ok                       # M2 一致；M1 在宽限内不判
    res2 = diff_positions((pend, other), [], now=NOW)
    assert not res2.ok                  # M2 missing 照报
    assert res2.diffs[0].magic == M2


def test_confirmed_never_gets_grace():
    confirmed = intent(status="confirmed", ts=NOW)
    res = diff_positions((confirmed,), [], now=NOW)
    assert not res.ok                   # confirmed 立即判，无宽限


# ---- 意图文件读写契约 ----

def test_write_and_load_intent_roundtrip(tmp_path):
    p = tmp_path / f"expected-{M1}.json"
    it = intent(cause="XAUUSD|M15|d1")
    write_intent(str(p), it)
    assert [it] == load_intents(str(tmp_path))
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw == {"symbol": "XAUUSD", "magic": M1, "side": "buy",
                   "volume": 0.2, "status": "confirmed",
                   "cause": "XAUUSD|M15|d1",
                   "ts": "2026-10-26T14:30:00+00:00"}


def test_load_intents_reads_all_magics_and_legacy_single_file(tmp_path):
    write_intent(str(tmp_path / f"expected-{M1}.json"), intent())
    write_intent(str(tmp_path / f"expected-{M2}.json"),
                 intent("EURUSD", M2, "sell", 0.1, cause="d2"))
    intents = load_intents(str(tmp_path))
    assert {i.magic for i in intents} == {M1, M2}


def test_load_intents_legacy_positions_format(tmp_path):
    """T24 及以前的旧格式（{"ts", "positions": [...]}）逐条记成
    confirmed 意图（cause="legacy"）。"""
    (tmp_path / f"expected-{M1}.json").write_text(json.dumps({
        "ts": "2026-10-26T14:30:00+00:00",
        "positions": [{"symbol": "XAUUSD", "magic": M1, "side": "buy",
                       "volume": 0.16}]}), encoding="utf-8")
    # 旧版单文件 expected.json 同样兼容
    (tmp_path / "expected.json").write_text(json.dumps({
        "ts": "2026-10-26T14:30:00+00:00",
        "positions": [{"symbol": "EURUSD", "magic": M2, "side": "sell",
                       "volume": 0.1}]}), encoding="utf-8")
    assert load_intents(str(tmp_path)) == [
        intent(volume=0.16, status="confirmed", cause="legacy"),
        intent("EURUSD", M2, "sell", 0.1, status="confirmed", cause="legacy"),
    ]


def test_load_intents_no_files_raises(tmp_path):
    (tmp_path / "live").mkdir()
    with pytest.raises(ExpectedStateError, match="没有任何意图文件"):
        load_intents(str(tmp_path / "live"))


def test_load_intents_invalid_json(tmp_path):
    (tmp_path / f"expected-{M1}.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ExpectedStateError):
        load_intents(str(tmp_path))


def test_load_intents_missing_key(tmp_path):
    (tmp_path / f"expected-{M1}.json").write_text(json.dumps(
        {"symbol": "XAUUSD", "magic": M1, "side": "buy"}),  # 缺 volume/status…
        encoding="utf-8")
    with pytest.raises(ExpectedStateError, match="缺字段"):
        load_intents(str(tmp_path))


def test_load_intents_bad_side_or_status(tmp_path):
    (tmp_path / f"expected-{M1}.json").write_text(json.dumps(
        {"symbol": "XAUUSD", "magic": M1, "side": "long", "volume": 0.1,
         "status": "confirmed", "cause": "d", "ts": NOW.isoformat()}),
        encoding="utf-8")
    with pytest.raises(ExpectedStateError, match="side"):
        load_intents(str(tmp_path))
    (tmp_path / f"expected-{M1}.json").write_text(json.dumps(
        {"symbol": "XAUUSD", "magic": M1, "side": "buy", "volume": 0.1,
         "status": "maybe", "cause": "d", "ts": NOW.isoformat()}),
        encoding="utf-8")
    with pytest.raises(ExpectedStateError, match="status"):
        load_intents(str(tmp_path))


def test_load_intents_bad_ts(tmp_path):
    (tmp_path / f"expected-{M1}.json").write_text(json.dumps(
        {"symbol": "XAUUSD", "magic": M1, "side": "buy", "volume": 0.1,
         "status": "confirmed", "cause": "d", "ts": "不是时间"}),
        encoding="utf-8")
    with pytest.raises(ExpectedStateError, match="ts"):
        load_intents(str(tmp_path))


def test_intent_rejects_bad_fields():
    with pytest.raises(ValueError, match="side"):
        Intent("XAUUSD", M1, "long", 0.1, "confirmed", "d", NOW)
    with pytest.raises(ValueError, match="status"):
        Intent("XAUUSD", M1, "buy", 0.1, "maybe", "d", NOW)
    with pytest.raises(ValueError, match="volume"):
        Intent("XAUUSD", M1, "buy", -0.1, "confirmed", "d", NOW)
    with pytest.raises(ValueError, match="magic"):
        Intent("XAUUSD", "x", "buy", 0.1, "confirmed", "d", NOW)
    with pytest.raises(ValueError, match="cause"):
        Intent("XAUUSD", M1, "buy", 0.1, "confirmed", "", NOW)


# ---- 暂停按策略隔离（T30/D20）----

def test_write_pause_per_magic_and_manual_global(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    p1 = write_pause(str(live), M1, paused=True, reason="reconcile_mismatch",
                     now=NOW)
    assert p1.endswith(f"pause-{M1}.json")
    assert json.loads((live / f"pause-{M1}.json").read_text(
        encoding="utf-8"))["paused"] is True
    # 全局 pause.json 只留给人工：magic=None 才写它
    assert not (live / "pause.json").exists()
    p2 = write_pause(str(live), None, paused=True, reason="manual",
                     now=NOW)
    assert p2.endswith("pause.json")
    # 解除
    write_pause(str(live), M1, paused=False, reason=None, now=NOW)
    assert json.loads((live / f"pause-{M1}.json").read_text(
        encoding="utf-8"))["paused"] is False


# ---- scripts/live_reconcile.py CLI（fake 网关） ----

PosRow = namedtuple(
    "PosRow",
    "ticket symbol type volume price_open price_current sl tp swap profit "
    "time comment magic",
)


def load_script():
    spec = importlib.util.spec_from_file_location("live_reconcile_script",
                                                  SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fake_gateway(positions=()):
    fake = FakeMT5()
    fake.set_behavior("positions_get", value=tuple(positions))
    gw = MT5Gateway(mt5_module=fake)
    gw.connect()
    return gw


def write_intent_file(live: Path, it: Intent):
    live.mkdir(parents=True, exist_ok=True)
    write_intent(str(live / f"expected-{it.magic}.json"), it)


def test_cli_mismatch_exits_1_and_pauses_that_magic(tmp_path, monkeypatch,
                                                    capsys):
    mod = load_script()
    live = tmp_path / "live"
    write_intent_file(live, intent())            # 0.2 手；账户无持仓
    monkeypatch.setattr(mod, "_open_gateway", lambda cfg: fake_gateway())
    rc = mod.main(["--data-dir", str(tmp_path), "--once"])
    assert rc == 1
    pause = json.loads((live / f"pause-{M1}.json").read_text(encoding="utf-8"))
    assert pause["paused"] is True
    assert pause["reason"] == "reconcile_mismatch"
    assert not (live / "pause.json").exists()    # 全局只留给人工
    out = capsys.readouterr().out
    assert "XAUUSD" in out and "missing" in out
    assert "RECONCILE_MISMATCH" in (live / "alerts.log").read_text(
        encoding="utf-8")


def test_cli_clean_exits_0_unpauses(tmp_path, monkeypatch, capsys):
    mod = load_script()
    live = tmp_path / "live"
    write_intent_file(live, intent())
    gw = fake_gateway(positions=(PosRow(
        9001, "XAUUSD", 0, 0.2, 100.0, 100.0, 0.0, 0.0, 0.0, 0.0,
        1790000000, "", M1,
    ),))
    monkeypatch.setattr(mod, "_open_gateway", lambda cfg: gw)
    rc = mod.main(["--data-dir", str(tmp_path), "--once"])
    assert rc == 0
    pause = json.loads((live / f"pause-{M1}.json").read_text(encoding="utf-8"))
    assert pause["paused"] is False
    assert "对账一致" in capsys.readouterr().out


def test_cli_missing_intent_files_exits_2(tmp_path, monkeypatch):
    mod = load_script()
    (tmp_path / "live").mkdir(parents=True)
    monkeypatch.setattr(mod, "_open_gateway", lambda cfg: fake_gateway())
    rc = mod.main(["--data-dir", str(tmp_path), "--once"])
    assert rc == 2


def test_cli_pending_within_grace_exits_0(tmp_path, monkeypatch):
    """pending 意图在宽限内：不判 → 不暂停（T30/C1 短暂误报反转）。"""
    mod = load_script()
    live = tmp_path / "live"
    write_intent_file(live, intent(status="pending",
                                   ts=NOW - timedelta(seconds=10)))
    monkeypatch.setattr(mod, "_open_gateway", lambda cfg: fake_gateway())
    rc, res = mod.run_once(str(live), fake_gateway(), volume_tolerance=VOL_TOL,
                           now=NOW)
    assert rc == 0 and res.ok
    pause = json.loads((live / f"pause-{M1}.json").read_text(encoding="utf-8"))
    assert pause["paused"] is False


def test_cli_only_diffing_magic_gets_paused(tmp_path, monkeypatch):
    """两个 magic：只有不一致的那个写 paused=true，另一个写回 false。"""
    mod = load_script()
    live = tmp_path / "live"
    write_intent_file(live, intent())                       # M1：missing
    write_intent_file(live, intent("EURUSD", M2, "sell", 0.1, cause="d2"))
    gw = fake_gateway(positions=(PosRow(
        9002, "EURUSD", 1, 0.1, 1.1, 1.1, 0.0, 0.0, 0.0, 0.0,
        1790000000, "", M2,
    ),))
    monkeypatch.setattr(mod, "_open_gateway", lambda cfg: gw)
    rc = mod.main(["--data-dir", str(tmp_path), "--once"])
    assert rc == 1
    assert json.loads((live / f"pause-{M1}.json").read_text(
        encoding="utf-8"))["paused"] is True
    assert json.loads((live / f"pause-{M2}.json").read_text(
        encoding="utf-8"))["paused"] is False


def test_cli_reads_legacy_expected_format(tmp_path, monkeypatch):
    """旧格式文件照样对账（兼容），写出的暂停文件按 magic。"""
    mod = load_script()
    live = tmp_path / "live"
    live.mkdir(parents=True)
    (live / f"expected-{M1}.json").write_text(json.dumps({
        "ts": NOW.isoformat() + "+00:00",
        "positions": [{"symbol": "XAUUSD", "magic": M1, "side": "buy",
                       "volume": 0.2}]}), encoding="utf-8")
    monkeypatch.setattr(mod, "_open_gateway", lambda cfg: fake_gateway())
    rc = mod.main(["--data-dir", str(tmp_path), "--once"])
    assert rc == 1
    assert json.loads((live / f"pause-{M1}.json").read_text(
        encoding="utf-8"))["paused"] is True


# ---- H-04（2026-10-29 审计）：净额为零掩盖对冲残仓——按持仓明细检查 ----

def test_diff_hedged_residual_flagged_even_when_net_matches():
    """复现：多空各 0.08 对锁残仓净额恰 0，与空仓意图净额一致 → 旧实现按
    净额判 clean（净零掩盖风险）；修复后实际总仓 > |净仓| 即报 hedged。"""
    res = diff_positions((intent(volume=0.0),),            # 意图：空仓
                         [pos("XAUUSD", 0.08, ticket=9001),
                          pos("XAUUSD", 0.08, side="sell", ticket=9002)],
                         now=NOW)
    assert not res.ok and res.pause            # 旧：ok（净额口径漏报）
    kinds = {d.kind for d in res.diffs}
    assert "hedged" in kinds
    d = next(d for d in res.diffs if d.kind == "hedged")
    assert (d.symbol, d.magic) == ("XAUUSD", M1)
    assert d.expected_net == 0.0 and d.actual_net == pytest.approx(0.0)


def test_diff_hedged_residual_flagged_when_net_nonzero_matches():
    """意图净 +0.01、实际 buy 0.02 + sell 0.01（净 +0.01）：净额一致但存在
    对锁腿 → 同样报 hedged（总仓 0.03 > |净仓| 0.01）。"""
    res = diff_positions((intent(volume=0.01),),
                         [pos("XAUUSD", 0.02, ticket=9001),
                          pos("XAUUSD", 0.01, side="sell", ticket=9002)],
                         now=NOW)
    assert not res.ok
    assert {d.kind for d in res.diffs} == {"hedged"}


def test_diff_single_side_not_flagged_hedged():
    """单边持仓（总量 == |净额|）不误报 hedged（clean 语义不变）。"""
    res = diff_positions((intent(),), [pos("XAUUSD", 0.2)], now=NOW)
    assert res.ok and res.diffs == ()
