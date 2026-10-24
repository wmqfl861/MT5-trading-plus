"""scripts/run_backtest.py 的金额口径装配：合约大小必须有来源，绝不静默按 1。"""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_backtest.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("run_backtest_script", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_contract_size_override_wins(tmp_path):
    mod = _load_script()
    assert mod.resolve_contract_size(str(tmp_path), "XAUUSD", 100.0) == 100.0


def test_contract_size_read_from_specs_snapshot(tmp_path):
    mod = _load_script()
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "latest.json").write_text(
        json.dumps({"EURUSD": {"contract_size": 100000.0}}), encoding="utf-8")
    assert mod.resolve_contract_size(str(tmp_path), "EURUSD", None) == 100000.0


def test_contract_size_missing_is_an_error_not_a_default(tmp_path):
    mod = _load_script()
    with pytest.raises(ValueError, match="snapshot_specs"):
        mod.resolve_contract_size(str(tmp_path), "XAUUSD", None)
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "latest.json").write_text(json.dumps({"BTCUSD": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="合约大小未知"):
        mod.resolve_contract_size(str(tmp_path), "BTCUSD", None)


def test_contract_size_override_must_be_positive(tmp_path):
    mod = _load_script()
    with pytest.raises(ValueError, match="contract-size"):
        mod.resolve_contract_size(str(tmp_path), "XAUUSD", 0.0)
