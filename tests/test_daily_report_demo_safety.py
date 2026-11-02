"""日报终端入口必须显式选择 Demo；离线假件不触及 MT5。"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from mt5gw.errors import DataUnavailableError
from tests.fakes import FakeMT5

ROOT = Path(__file__).resolve().parents[1]


def report_module():
    spec = importlib.util.spec_from_file_location(
        "daily_demo_safety", ROOT / "scripts" / "live_daily_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fake_terminal(monkeypatch, mode=0):
    fake = FakeMT5()
    fake.set_behavior("account_info", SimpleNamespace(
        login=123456, server="Fake-Server", company="Fake-Broker", currency="USD", balance=10000.0,
        equity=10000.0, margin=0.0, margin_free=10000.0, profit=0.0,
        leverage=100, margin_mode=2, trade_mode=mode))
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    return fake


def demo_config(tmp_path, **extra):
    path = tmp_path / "gateway.json"
    path.write_text(json.dumps({"allow_live_trading": False, **extra}), encoding="utf-8")
    return str(path)


def test_report_without_explicit_mode_never_connects(tmp_path, monkeypatch):
    fake = fake_terminal(monkeypatch)
    with pytest.raises(DataUnavailableError):
        report_module().build_gw(None, demo_config(tmp_path), str(tmp_path))
    assert fake.calls("initialize") == 0


def test_demo_report_missing_config_never_connects(tmp_path, monkeypatch):
    fake = fake_terminal(monkeypatch)
    with pytest.raises(DataUnavailableError):
        report_module().build_gw(None, str(tmp_path / "missing.json"),
                                 str(tmp_path), demo=True)
    assert fake.calls("initialize") == 0


def test_demo_report_live_unlock_never_connects(tmp_path, monkeypatch):
    fake = fake_terminal(monkeypatch)
    path = demo_config(tmp_path, allow_live_trading=True, expected_login=123456)
    with pytest.raises(DataUnavailableError):
        report_module().build_gw(None, path, str(tmp_path), demo=True)
    assert fake.calls("initialize") == 0


@pytest.mark.parametrize("mode", [1, 2, 99])
def test_demo_report_rejects_non_demo_before_history(tmp_path, monkeypatch, mode):
    fake = fake_terminal(monkeypatch, mode)
    with pytest.raises(DataUnavailableError):
        report_module().build_gw(None, demo_config(tmp_path), str(tmp_path), demo=True)
    assert fake.calls("history_deals_get") == 0
    assert fake.calls("shutdown") == 1


def test_demo_report_accepts_demo_gateway(tmp_path, monkeypatch):
    fake = fake_terminal(monkeypatch)
    gw, owned = report_module().build_gw(
        None, demo_config(tmp_path), str(tmp_path), demo=True)
    try:
        assert owned
        assert gw.account.state().trade_mode == 0
    finally:
        gw.shutdown()
    assert fake.calls("order_send") == 0


def test_report_cli_requires_mode_before_connecting(tmp_path, monkeypatch):
    fake = fake_terminal(monkeypatch)
    out = tmp_path / "reports"
    (tmp_path / "live").mkdir()
    rc = report_module().main([
        "--gw-config", demo_config(tmp_path), "--data-dir", str(tmp_path),
        "--reports-dir", str(out), "--date", "2026-11-02"])
    assert rc == 2
    assert not out.exists()
    assert fake.calls("initialize") == 0
