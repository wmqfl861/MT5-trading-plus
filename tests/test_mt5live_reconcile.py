"""mt5live.reconcile（T14/P7）：expected vs actual 持仓对账（按 magic 过滤）。

差分三情形（missing / unexpected / mismatch）+ expected.json 读写契约 +
live_reconcile CLI（fake 网关取 actual，不连 MT5）。
"""
from __future__ import annotations

import importlib.util
import json
from collections import namedtuple
from datetime import datetime
from pathlib import Path

import pytest

from mt5gw.models import Position
from mt5live.reconcile import (
    ExpectedPosition,
    ExpectedStateError,
    diff_positions,
    load_expected,
    write_expected,
)
from mt5gw.gateway import MT5Gateway
from tests.fakes import FakeMT5

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "live_reconcile.py"

NOW = datetime(2026, 10, 26, 14, 30, 0)
M1 = 20261101
M2 = 20261102
VOL_TOL = 1e-6


def pos(symbol="XAUUSD", volume=0.2, *, side="buy", magic=M1,
        ticket=9001) -> Position:
    return Position(
        ticket=ticket, symbol=symbol, side=side, volume=volume,
        price_open=100.0, price_current=100.0, sl=0.0, tp=0.0,
        swap=0.0, profit=0.0, time=NOW, comment="", magic=magic,
    )


def expected(*positions: ExpectedPosition, ts=NOW) -> dict:
    return {
        "ts": ts.isoformat(),
        "positions": [
            {"symbol": p.symbol, "magic": p.magic, "side": p.side,
             "volume": p.volume}
            for p in positions
        ],
    }


# ---- 差分三情形 ----

def test_diff_clean_ok():
    exp = (ExpectedPosition("XAUUSD", M1, "buy", 0.2),)
    res = diff_positions(exp, [pos("XAUUSD", 0.2)])
    assert res.ok and res.diffs == ()
    assert not res.pause


def test_diff_missing_expected_not_in_account():
    exp = (ExpectedPosition("XAUUSD", M1, "buy", 0.2),)
    res = diff_positions(exp, [])
    assert not res.ok and res.pause
    assert len(res.diffs) == 1
    d = res.diffs[0]
    assert d.kind == "missing"
    assert (d.symbol, d.magic) == ("XAUUSD", M1)
    assert d.expected_net == pytest.approx(0.2)
    assert d.actual_net == 0.0


def test_diff_unexpected_in_account_only():
    # M1 受管（expected 声明了 XAUUSD 空仓），账户却持有 M1 的 EURUSD
    exp = (ExpectedPosition("XAUUSD", M1, "buy", 0.0),)  # 声明空仓
    res = diff_positions(exp, [pos("EURUSD", 0.1, side="sell", magic=M1)])
    assert not res.ok
    assert len(res.diffs) == 1
    d = res.diffs[0]
    assert d.kind == "unexpected"
    assert (d.symbol, d.magic) == ("EURUSD", M1)
    assert d.expected_net == 0.0
    assert d.actual_net == pytest.approx(-0.1)  # sell 记负


def test_diff_mismatch_volume_and_side():
    exp = (ExpectedPosition("XAUUSD", M1, "buy", 0.2),)
    res = diff_positions(exp, [pos("XAUUSD", 0.15)])
    assert not res.ok
    assert res.diffs[0].kind == "mismatch"

    res2 = diff_positions(exp, [pos("XAUUSD", 0.2, side="sell")])
    assert not res2.ok
    assert res2.diffs[0].kind == "mismatch"


# ---- magic 过滤与净额合并 ----

def test_unmanaged_magic_positions_ignored():
    # 非 expected 中出现的 magic（含 0=手动）不参与对账
    actual = [pos("EURUSD", 5.0, magic=0), pos("USTEC", 1.0, magic=999999)]
    res = diff_positions((), actual)
    assert res.ok and res.diffs == ()


def test_actual_positions_net_by_symbol_magic():
    exp = (ExpectedPosition("XAUUSD", M1, "buy", 0.2),)
    actual = [pos("XAUUSD", 0.1, ticket=1), pos("XAUUSD", 0.1, ticket=2)]
    res = diff_positions(exp, actual)
    assert res.ok  # 0.1 + 0.1 净额 = 0.2


def test_zero_volume_expected_means_flat():
    exp = (ExpectedPosition("XAUUSD", M1, "buy", 0.0),)
    res = diff_positions(exp, [])
    assert res.ok  # 期望 0 手 = 无持仓


def test_volume_tolerance_avoids_float_noise():
    exp = (ExpectedPosition("XAUUSD", M1, "buy", 0.3),)
    actual = [pos("XAUUSD", 0.1 + 0.2)]  # 0.30000000000000004
    res = diff_positions(exp, actual)
    assert res.ok


# ---- expected.json 读写契约 ----

def test_write_and_load_expected_roundtrip(tmp_path):
    p = tmp_path / "expected.json"
    write_expected(str(p), NOW,
                   [ExpectedPosition("XAUUSD", M1, "buy", 0.2),
                    ExpectedPosition("EURUSD", M2, "sell", 0.5)])
    st = load_expected(str(p))
    assert st.ts == NOW
    assert st.positions == (
        ExpectedPosition("XAUUSD", M1, "buy", 0.2),
        ExpectedPosition("EURUSD", M2, "sell", 0.5),
    )
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["positions"][0] == {"symbol": "XAUUSD", "magic": M1,
                                   "side": "buy", "volume": 0.2}


def test_load_expected_missing_file(tmp_path):
    with pytest.raises(ExpectedStateError, match="不存在"):
        load_expected(str(tmp_path / "expected.json"))


def test_load_expected_invalid_json(tmp_path):
    p = tmp_path / "expected.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ExpectedStateError):
        load_expected(str(p))


def test_load_expected_position_missing_key(tmp_path):
    p = tmp_path / "expected.json"
    p.write_text(json.dumps({
        "ts": NOW.isoformat(),
        "positions": [{"symbol": "XAUUSD", "magic": M1}],  # 缺 side/volume
    }), encoding="utf-8")
    with pytest.raises(ExpectedStateError, match="side"):
        load_expected(str(p))


def test_load_expected_bad_side(tmp_path):
    p = tmp_path / "expected.json"
    p.write_text(json.dumps({
        "ts": NOW.isoformat(),
        "positions": [{"symbol": "XAUUSD", "magic": M1, "side": "long",
                       "volume": 0.1}],
    }), encoding="utf-8")
    with pytest.raises(ExpectedStateError, match="side"):
        load_expected(str(p))


def test_load_expected_bad_ts(tmp_path):
    p = tmp_path / "expected.json"
    p.write_text(json.dumps({"ts": "不是时间", "positions": []}),
                 encoding="utf-8")
    with pytest.raises(ExpectedStateError, match="ts"):
        load_expected(str(p))


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


def write_expected_file(live: Path, positions, ts=None):
    live.mkdir(parents=True, exist_ok=True)
    write_expected(str(live / "expected.json"), ts or NOW, list(positions))


def test_cli_mismatch_exits_1_and_pauses(tmp_path, monkeypatch, capsys):
    mod = load_script()
    live = tmp_path / "live"
    write_expected_file(live, [ExpectedPosition("XAUUSD", M1, "buy", 0.2)])
    monkeypatch.setattr(mod, "_open_gateway", lambda cfg: fake_gateway())
    rc = mod.main(["--data-dir", str(tmp_path), "--once"])
    assert rc == 1
    pause = json.loads((live / "pause.json").read_text(encoding="utf-8"))
    assert pause["paused"] is True
    assert pause["reason"] == "reconcile_mismatch"
    out = capsys.readouterr().out
    assert "XAUUSD" in out and "missing" in out
    assert "RECONCILE_MISMATCH" in (live / "alerts.log").read_text(
        encoding="utf-8")


def test_cli_clean_exits_0_unpauses(tmp_path, monkeypatch, capsys):
    mod = load_script()
    live = tmp_path / "live"
    write_expected_file(live, [ExpectedPosition("XAUUSD", M1, "buy", 0.2)])
    gw = fake_gateway(positions=(PosRow(
        9001, "XAUUSD", 0, 0.2, 100.0, 100.0, 0.0, 0.0, 0.0, 0.0,
        1790000000, "", M1,
    ),))
    monkeypatch.setattr(mod, "_open_gateway", lambda cfg: gw)
    rc = mod.main(["--data-dir", str(tmp_path), "--once"])
    assert rc == 0
    pause = json.loads((live / "pause.json").read_text(encoding="utf-8"))
    assert pause["paused"] is False
    assert "对账一致" in capsys.readouterr().out


def test_cli_missing_expected_exits_2(tmp_path, monkeypatch):
    mod = load_script()
    (tmp_path / "live").mkdir(parents=True)
    monkeypatch.setattr(mod, "_open_gateway", lambda cfg: fake_gateway())
    rc = mod.main(["--data-dir", str(tmp_path), "--once"])
    assert rc == 2
