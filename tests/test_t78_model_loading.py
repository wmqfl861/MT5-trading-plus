"""T78: frozen synthetic models reach the real CLI, signal code and paper broker."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from mt5data.store import save_bars
from mt5live.config import LiveConfig, load
from mt5live.monitor import write_role_heartbeat
from mt5quant.prob.artifact import save_model
from mt5quant.research.gate import experiment_fingerprint
from mt5quant.strategies.library.prob.level_decision import LevelDecisionS1Demo


ROOT = Path(__file__).resolve().parents[1]
STRATEGY = "mt5quant.strategies.library.prob.level_decision:LevelDecisionS1Demo"
H_STRATEGY = "mt5quant.strategies.library.buy_hold:BuyHold"
MAGIC = 780001
EXPERIMENT = {"bars_meta": {"source": "handwritten synthetic fixture"},
              "cost_version": "synthetic-only", "sessions_sha": "synth-session-v1"}
NOW = datetime(2024, 4, 2, 10, 0, 7)
CARD = f'''id = "P978-v1"
version = 1
strategy = "{STRATEGY}"
symbols = ["XAUUSD"]
timeframe = "H1"
segment = "validation"
primary_statistic = "dsr_v0"
session = "synthetic only"
phenomenon = "synthetic fixture"
mechanism = "synthetic fixture"
should_fail_when = "synthetic fixture"
falsified_if = "synthetic fixture"
graveyard_checked = []
[rules]
entry = "synthetic"
exit = "synthetic"
stop = "synthetic"
[expected]
delta_p = "synthetic"
triggers_per_year = "synthetic"
[prob]
situations = ["S1"]
hypotheses = ["S1:A1:break>base", "S1:A2:retest>base", "S1:A3:fade>base", "S1:A4:reclaim>base"]
bracket = {{ sl_atr = 1.0, tp_atr = 2.0, horizon_bars = 8 }}
level_grid = {{ XAUUSD = 2.0 }}
approach = {{ lookback_bars = 12, min_distance_atr = 1.0, touch_atr = 0.25 }}
breakout_atr = 0.25
retest_window_bars = 4
expected_delta_p = 0.04
features = ["trend_sma200", "atr_pct_2000"]
prior_strength = 100
min_cell = 100
lb_z = 1.0
entry_threshold_cost_mult = 2.0
'''


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def frozen_model(*, strong=True):
    # Handwritten probabilities, no fit/label/backtest called even by the fixture.
    estimate = dict(n=1000, p_win=0.9 if strong else 0.1,
                    p_loss=0.1 if strong else 0.9, p_time=0.0,
                    r_win=1.9, r_loss=-1.05, r_time=0.0,
                    se_win=0.01, se_loss=0.01)
    return {
        "schema_version": 1, "prob_version": "1.0.0",
        "symbols": ["XAUUSD"], "timeframe": "H1",
        "fit_window": {"start": "2020-01-01", "end": "2020-02-01"},
        "purge_bars": 10,
        "bracket": {"sl_atr": 1.0, "tp_atr": 2.0, "horizon_bars": 8},
        "situations": ["S1"], "features": ["trend_sma200", "atr_pct_2000"],
        "policy": {"z": 1.0, "threshold_mult": 2.0},
        "allowed_hypotheses": ["*:A1", "*:A2", "*:A3", "*:A4"],
        "cost_r_by_situation_action": {"S1|A3": 0.001},
        "prob_table": {
            "schema_version": 1, "group_by": ["trend_sma200", "atr_pct_2000"],
            "prior_strength": 100, "min_cell": 100,
            "base_rates": {"A3": estimate},
            "parents": [{"situation": "S1", "action": "A3", **estimate}],
            "cells": [],
        },
    }


def fingerprint(model, card_bytes, cfg):
    return experiment_fingerprint(
        strategy_ref=cfg["strategy"], card="P978-v1", model_dict=model,
        params={"strategy_params": cfg.get("params", {}),
                "card_sha256": hashlib.sha256(card_bytes).hexdigest()},
        features=["trend_sma200", "atr_pct_2000"],
        **cfg["experiment"])["sha256"]


def make_config(tmp_path, *, strong=True):
    card = write(tmp_path / "config" / "P978-v1.toml", CARD)
    model = frozen_model(strong=strong)
    model_path, sha = save_model(model, card.parent / "models")
    cfg = dict(symbol="XAUUSD", timeframe="H1", strategy=STRATEGY,
               magic=MAGIC, lookback_bars=2100, volume_lots=0.01,
               data_root=str(tmp_path / "data"),
               model_path="models/" + Path(model_path).name,
               model_sha256=sha, card_path=card.name,
               experiment=EXPERIMENT)
    cfg["experiment_sha256"] = fingerprint(model, card.read_bytes(), cfg)
    path = tmp_path / "config" / "live.toml"
    write_config(path, cfg)
    return path, cfg, model


def toml_value(value):
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {toml_value(v)}" for k, v in value.items()) + " }"
    if isinstance(value, float) and math.isnan(value):
        return "nan"
    return json.dumps(value)


def write_config(path, cfg):
    write(path, "\n".join(f"{k} = {toml_value(v)}" for k, v in cfg.items()) + "\n")


def update_model(path, cfg, model, *, rebind=False):
    model_path, sha = save_model(model, path.parent / "models")
    cfg.update(model_path="models/" + Path(model_path).name, model_sha256=sha)
    if rebind:
        cfg["experiment_sha256"] = fingerprint(model, (path.parent / cfg["card_path"]).read_bytes(), cfg)
    write_config(path, cfg)


@pytest.fixture
def runner(monkeypatch):
    spec = importlib.util.spec_from_file_location("t78_live_runner", ROOT / "scripts/live_runner.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "now_utc", lambda: NOW)

    def forbidden(*a, **kw):
        pytest.fail("T78 offline test must never call fit or connect a gateway")

    monkeypatch.setattr(LevelDecisionS1Demo, "fit", forbidden)
    monkeypatch.setattr("mt5gw.MT5Gateway.from_config_file", forbidden)
    return mod


def paper_inputs(tmp_path, data_root):
    rows = [(98.5, 99.0, 98.0, 98.5)] * 2100 + [(99.5, 99.9, 99.0, 99.0)]
    bars = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    bars.insert(0, "time", pd.date_range(end="2024-04-02 09:00", periods=len(rows), freq="h"))
    bars["tick_volume"], bars["spread"], bars["real_volume"] = 10, 5, 0
    save_bars(str(data_root), "XAUUSD", "H1", bars)
    write(data_root / "specs/latest.json", json.dumps({"XAUUSD": {
        "contract_size": 100.0, "volume_min": 0.01, "volume_step": 0.01, "volume_max": 100.0}}))
    risk = write(tmp_path / "risk.toml", '''version = 1
symbols = ["XAUUSD"]
max_lots_per_order = 0.01
max_positions_per_symbol = 1
max_usd_exposure = 100000.0
max_orders_per_minute = 6
daily_loss_cap_usd = 500.0
''')
    sessions = write(tmp_path / "sessions.toml", '''version = 1
[rollover]
local_time = "17:00"
tz = "America/New_York"
[forbidden]
pre_minutes = 0
post_minutes = 0
exempt_symbols = []
[costs]
spread_gate_multiplier = 3.0
missing_hour_policy = "forbid"
''')
    write_role_heartbeat(str(data_root / "live"), "risk", interval=10.0, now=NOW)
    return ["--risk-config", str(risk), "--sessions-config", str(sessions), "--no-spread-gate"]


def offline_args(path):
    return ["--config", str(path), "--once", "--dry-run", "--offline", "--paper"]


@pytest.mark.parametrize("tier", ["parents", "base_rates", "cells"])
def test_main_consumes_two_frozen_models_in_real_paper_runner(tmp_path, monkeypatch, runner, tier):
    # Ignoring with_model's NEW instance, replacing it in _run, or ignoring the
    # artifact all break the signal and real persisted execution assertions.
    attached = []
    original = LevelDecisionS1Demo.with_model

    def observe(self, model):
        result = original(self, model)
        assert result is not self
        assert self._table is None
        attached.append(result)
        return result

    monkeypatch.setattr(LevelDecisionS1Demo, "with_model", observe)
    original_runner = runner.SignalRunner

    def consume(cfg, strategy, **kw):
        assert strategy is attached[-1]
        return original_runner(cfg, strategy, **kw)

    monkeypatch.setattr(runner, "SignalRunner", consume)
    signals, positions = [], []
    unrelated = tmp_path / "unrelated-cwd"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    for strong in (True, False):
        root = tmp_path / str(strong)
        path, cfg, model = make_config(root, strong=strong)
        if tier == "base_rates":
            model["prob_table"]["parents"] = []
            update_model(path, cfg, model, rebind=True)
        elif tier == "cells":
            estimate = model["prob_table"]["base_rates"]["A3"]
            model["prob_table"]["cells"] = [{
                "situation": "S1", "action": "A3", **estimate,
                "features": {"trend_sma200": "above", "atr_pct_2000": "high"}}]
            # Make the fallback estimate imply the opposite decision so a
            # mistakenly skipped valid cell cannot pass by using its parent.
            fallback = frozen_model(strong=not strong)["prob_table"]
            model["prob_table"]["base_rates"] = fallback["base_rates"]
            model["prob_table"]["parents"] = fallback["parents"]
            update_model(path, cfg, model, rebind=True)
        data = root / "override-data"
        args = paper_inputs(root, data)
        assert runner.main(offline_args(path) + ["--data-dir", str(data)] + args) == 0
        hb = read_json(data / "live" / f"heartbeat-{MAGIC}.json")
        assert hb["last_error"] is None
        signals.append(hb["last_decision"]["signal"])
        book = read_json(data / "live/paper" / f"state-{MAGIC}.json")
        positions.append(book["positions"])
        assert not Path(cfg["data_root"]).exists()
        if strong:
            intent = read_json(data / "live" / f"expected-{MAGIC}.json")
            assert intent["status"] == "confirmed"
            assert intent["volume"] == 0.01
    assert signals == [-1, 0]
    assert len(positions[0]) == 1 and positions[1] == []
    assert positions[0][0]["side"] == "sell"
    assert positions[0][0]["volume"] == 0.01
    assert positions[0][0]["sl"] > positions[0][0]["tp"]
    assert len(attached) == 2


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() if p.is_file() else None
            for p in root.rglob("*")}


def assert_rejected_without_effects(runner, path, cfg, monkeypatch, *, mode=None):
    data = Path(cfg["data_root"])
    for name in (f"heartbeat-{MAGIC}.json", f"expected-{MAGIC}.json",
                 f"paper/state-{MAGIC}.json", f"runner-{MAGIC}.lock", "HALT"):
        write(data / "live" / name, "existing bytes must survive\n")
    before = snapshot(path.parent.parent)
    reached = []

    def forbidden(*args, **kwargs):
        reached.append(True)
        raise AssertionError("preflight reached a side-effect boundary")

    for attr in ("guarded_main", "MagicInstanceLock", "HeartbeatWriter", "PaperBroker", "load_bars"):
        monkeypatch.setattr(runner, attr, forbidden)
    argv = offline_args(path) if mode is None else ["--config", str(path), "--once", *mode]
    assert runner.main(argv) == 2
    assert reached == []
    assert snapshot(path.parent.parent) == before


@pytest.mark.parametrize("case", [
    "no_model", "missing_file", "wrong_hash", "bad_json", "wrong_type",
    "experiment_hash", "symbols", "timeframe", "bracket",
    "allowed_hypotheses", "situations", "features", "policy", "schema_version", "prob_version",
    "missing_symbols", "missing_timeframe", "missing_bracket", "missing_allowed_hypotheses",
    "missing_fit_window", "missing_purge_bars", "missing_features", "missing_policy",
    "missing_situations", "missing_prob_version", "missing_schema_version",
    "fit_start", "fit_end", "fit_order", "fit_future", "purge_short", "purge_type",
    "table_type", "table_features", "table_prior", "table_min_cell",
    "config_symbol", "config_timeframe", "config_params", "lookback",
])
def test_main_model_preflight_rejects_before_all_side_effects(tmp_path, monkeypatch, runner, case):
    path, cfg, model = make_config(tmp_path)
    if case == "no_model":
        for key in ("model_path", "model_sha256", "card_path", "experiment_sha256", "experiment"):
            cfg.pop(key)
    elif case == "missing_file":
        cfg["model_path"] = "missing.json"
    elif case == "wrong_hash":
        cfg["model_sha256"] = "0" * 64
    elif case in {"bad_json", "wrong_type"}:
        payload = b"{ broken" if case == "bad_json" else b"[]"
        (path.parent / cfg["model_path"]).write_bytes(payload)
        cfg["model_sha256"] = hashlib.sha256(payload).hexdigest()
    elif case.startswith("missing_"):
        key = case.removeprefix("missing_")
        model.pop(key)
        update_model(path, cfg, model, rebind=True)
    else:
        mutations = {
            "experiment_hash": (cfg, "experiment_sha256", "a" * 64),
            "symbols": (model, "symbols", ["EURUSD"]),
            "timeframe": (model, "timeframe", "M15"),
            "bracket": (model, "bracket", {"sl_atr": 1.0, "tp_atr": 2.0}),
            "allowed_hypotheses": (model, "allowed_hypotheses", []),
            "situations": (model, "situations", ["S2"]),
            "features": (model, "features", []), "policy": (model, "policy", {}),
            "schema_version": (model, "schema_version", 2),
            "prob_version": (model, "prob_version", "unknown"),
            "fit_start": (model["fit_window"], "start", None),
            "fit_end": (model["fit_window"], "end", "not a date"),
            "fit_order": (model["fit_window"], "end", "2019-01-01"),
            "fit_future": (model["fit_window"], "end", "2099-01-01"),
            "purge_short": (model, "purge_bars", 9), "purge_type": (model, "purge_bars", True),
            "table_type": (model, "prob_table", []),
            "table_features": (model["prob_table"], "group_by", []),
            "table_prior": (model["prob_table"], "prior_strength", 1),
            "table_min_cell": (model["prob_table"], "min_cell", 1),
            "config_symbol": (cfg, "symbol", "EURUSD"),
            "config_timeframe": (cfg, "timeframe", "M15"),
            "config_params": (cfg, "params", {"unused": 1}),
            "lookback": (cfg, "lookback_bars", 40),
        }
        target, key, value = mutations[case]
        target[key] = value
        update_model(path, cfg, model, rebind=case != "experiment_hash")
    write_config(path, cfg)
    assert_rejected_without_effects(runner, path, cfg, monkeypatch)


@pytest.mark.parametrize("old,new", [
    ('timeframe = "H1"', 'timeframe = "M15"'),
    ('symbols = ["XAUUSD"]', 'symbols = ["EURUSD"]'),
    (f'strategy = "{STRATEGY}"', f'strategy = "{H_STRATEGY}"'),
    ('sl_atr = 1.0', 'sl_atr = 1.5'),
    ('horizon_bars = 8', 'horizon_bars = 9'),
    ('XAUUSD = 2.0', 'XAUUSD = 3.0'),
    ('lookback_bars = 12', 'lookback_bars = 13'),
    ('breakout_atr = 0.25', 'breakout_atr = 0.5'),
    ('retest_window_bars = 4', 'retest_window_bars = 5'),
    ('prior_strength = 100', 'prior_strength = 101'),
    ('min_cell = 100', 'min_cell = 101'),
    ('lb_z = 1.0', 'lb_z = 2.0'),
    ('entry_threshold_cost_mult = 2.0', 'entry_threshold_cost_mult = 3.0'),
    ('"S1:A4:reclaim>base"', '"S2:A4:reclaim>base"'),
    ('features = ["trend_sma200", "atr_pct_2000"]', 'features = ["trend_sma200"]'),
    ('segment = "validation"', 'segment = "discovery"'),
])
def test_rehashed_card_must_match_strategy_scope(tmp_path, monkeypatch, runner, old, new):
    path, cfg, model = make_config(tmp_path)
    card = path.parent / cfg["card_path"]
    write(card, CARD.replace(old, new))
    update_model(path, cfg, model, rebind=True)
    assert_rejected_without_effects(runner, path, cfg, monkeypatch)


@pytest.mark.parametrize("mode", [[], ["--dry-run"], ["--paper"], ["--paper", "--dry-run"]])
def test_unreleased_fittable_online_rejected_even_with_self_claimed_approval(tmp_path, monkeypatch, runner, mode):
    path, cfg, model = make_config(tmp_path)
    model.update(approved=True, approved_by="self-claim", synthetic=True, release_id="self-claim")
    update_model(path, cfg, model, rebind=True)
    assert_rejected_without_effects(runner, path, cfg, monkeypatch, mode=mode)


def test_nonfittable_cannot_silently_ignore_model(tmp_path, monkeypatch, runner):
    path, cfg, _ = make_config(tmp_path)
    cfg["strategy"] = H_STRATEGY
    write_config(path, cfg)
    assert_rejected_without_effects(runner, path, cfg, monkeypatch)


@pytest.mark.parametrize("field,value", [
    ("model_path", None), ("model_sha256", None), ("card_path", None),
    ("experiment_sha256", None), ("model_path", ""), ("card_path", 7),
    ("model_sha256", "g" * 64), ("model_sha256", "a" * 63),
    ("experiment_sha256", True), ("experiment_sha256", "a" * 65),
])
def test_model_config_fields_fail_fast_for_direct_and_toml(tmp_path, field, value):
    path, cfg, _ = make_config(tmp_path)
    cfg[field] = value
    with pytest.raises(ValueError):
        LiveConfig(**cfg)
    if value is None:
        cfg.pop(field)
    write_config(path, cfg)
    with pytest.raises(ValueError):
        load(str(path))


def test_config_paths_follow_config_directory_and_old_defaults_unchanged(tmp_path, monkeypatch):
    path, cfg, _ = make_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    actual = load(str(path))
    assert Path(actual.model_path) == path.parent / cfg["model_path"]
    assert Path(actual.card_path) == path.parent / cfg["card_path"]
    old = {k: v for k, v in cfg.items()
           if k not in {"model_path", "model_sha256", "card_path", "experiment_sha256", "experiment"}}
    old["strategy"] = H_STRATEGY
    write_config(path, old)
    loaded = load(str(path))
    assert asdict(replace(loaded, created_utc=None)) == asdict(LiveConfig(**old))
    assert loaded.model_path is loaded.model_sha256 is loaded.card_path is loaded.experiment_sha256 is None


def test_legacy_nonfittable_main_still_produces_signal_and_paper_fill(tmp_path, runner):
    path, cfg, _ = make_config(tmp_path)
    for key in ("model_path", "model_sha256", "card_path", "experiment_sha256", "experiment"):
        cfg.pop(key)
    cfg["strategy"] = H_STRATEGY
    write_config(path, cfg)
    args = paper_inputs(tmp_path, Path(cfg["data_root"]))
    assert runner.main(offline_args(path) + args) == 0
    live = Path(cfg["data_root"]) / "live"
    assert read_json(live / f"heartbeat-{MAGIC}.json")["last_decision"]["signal"] == 1
    assert read_json(live / "paper" / f"state-{MAGIC}.json")["positions"][0]["side"] == "buy"


@pytest.mark.parametrize("case", ["unknown_key", "missing_required", "bad_toml", "bad_context"])
def test_main_malformed_model_config_never_enters_guard(tmp_path, monkeypatch, runner, case):
    path, cfg, _ = make_config(tmp_path)
    if case == "unknown_key":
        cfg["lookbak_bars"] = 20
    elif case == "missing_required":
        cfg.pop("symbol")
    elif case == "bad_context":
        cfg["experiment"] = {**EXPERIMENT, "bars_meta": {"source": float("nan")}}
    write_config(path, cfg)
    if case == "bad_toml":
        with path.open("a", encoding="utf-8") as stream:
            stream.write("[broken\n")
    assert_rejected_without_effects(runner, path, cfg, monkeypatch)


@pytest.mark.parametrize("context", [
    None, {}, [], {**EXPERIMENT, "sessions_sha": False},
    {**EXPERIMENT, "cost_version": ""}, {**EXPERIMENT, "evaluator_sha": 42},
    {**EXPERIMENT, "unknown": "value"}, {**EXPERIMENT, "bars_meta": {}},
    {**EXPERIMENT, "bars_meta": []},
    {**EXPERIMENT, "bars_meta": {"rows": float("nan")}},
])
def test_experiment_context_types_are_strict(tmp_path, context):
    _, cfg, _ = make_config(tmp_path)
    cfg["experiment"] = context
    with pytest.raises(ValueError):
        LiveConfig(**cfg)


@pytest.mark.parametrize("change", ["card", "model", "context"])
def test_changed_assembly_identity_is_observably_rejected(tmp_path, monkeypatch, runner, capsys, change):
    path, cfg, model = make_config(tmp_path)
    if change == "card":
        card = path.parent / cfg["card_path"]
        write(card, CARD.replace("synthetic fixture", "other synthetic fixture"))
    elif change == "model":
        update_model(path, cfg, frozen_model(strong=False))
    else:
        cfg["experiment"] = {**EXPERIMENT, "cost_version": "other-synthetic-cost"}
        write_config(path, cfg)
    assert_rejected_without_effects(runner, path, cfg, monkeypatch)
    assert "experiment_sha256" in capsys.readouterr().out


def test_missing_code_closure_rejected_even_if_rehashed(tmp_path, monkeypatch, runner, capsys):
    path, cfg, model = make_config(tmp_path)
    monkeypatch.setattr("mt5quant.research.cards.strategy_files", lambda ref: [])
    update_model(path, cfg, model, rebind=True)
    assert_rejected_without_effects(runner, path, cfg, monkeypatch)
    assert "闭包" in capsys.readouterr().out


def test_online_refusal_precedes_model_and_card_reads(tmp_path, monkeypatch, runner, capsys):
    path, cfg, _ = make_config(tmp_path)
    original = Path.read_bytes

    def no_artifact_read(self):
        if self.suffix in {".json", ".toml"} and "config" in self.parts:
            pytest.fail("online fittable startup must refuse before artifact reads")
        return original(self)

    monkeypatch.setattr("mt5quant.prob.artifact.load_model", lambda *a, **kw: pytest.fail("model read"))
    # No state files needed: any entry to the legacy guard is a failure here.
    monkeypatch.setattr(runner, "guarded_main", lambda *a, **kw: pytest.fail("guard reached"))
    monkeypatch.setattr(Path, "read_bytes", no_artifact_read)
    assert runner.main(["--config", str(path), "--once", "--paper"]) == 2
    assert "release" in capsys.readouterr().out
    assert not Path(cfg["data_root"]).exists()


@pytest.mark.parametrize("kind", ["time_stop_bars", "week_close_exit"])
def test_legacy_exit_rule_refusal_keeps_monitor_evidence(tmp_path, monkeypatch, runner, kind):
    from mt5quant.strategies import ExitRules
    from mt5quant.strategies.library.buy_hold import BuyHold

    class Unsupported(BuyHold):
        exit_rules = ExitRules(**{kind: 10 if kind == "time_stop_bars" else True})

    path, cfg, _ = make_config(tmp_path)
    for key in ("model_path", "model_sha256", "card_path", "experiment_sha256", "experiment"):
        cfg.pop(key)
    cfg["strategy"] = H_STRATEGY
    write_config(path, cfg)
    monkeypatch.setattr(runner, "load_strategy", lambda ref: Unsupported)
    assert runner.main(offline_args(path)) == 2
    live = Path(cfg["data_root"]) / "live"
    marker = read_json(live / f"exit-runner-{MAGIC}.json")
    assert kind.removesuffix("_bars") in marker["reason"]
    assert not (live / f"runner-{MAGIC}.lock").exists()
    assert not (live / "paper").exists()


def test_legacy_bad_toml_keeps_guarded_startup_evidence(tmp_path, runner):
    path = write(tmp_path / "legacy.toml", 'strategy = "' + H_STRATEGY + '"\n[broken\n')
    data = tmp_path / "legacy-data"
    assert runner.main(["--config", str(path), "--once", "--dry-run", "--offline",
                        "--data-dir", str(data)]) == 2
    marker = read_json(data / "live/exit-runner.json")
    assert "TOML" in marker["reason"]
    assert marker["rc"] == 2


def table_estimate(model, tier):
    table = model["prob_table"]
    if tier == "base_rates":
        return table["base_rates"]["A3"]
    if tier == "cells":
        table["cells"] = [{**table["parents"][0],
                           "features": {"trend_sma200": "above", "atr_pct_2000": "high"}}]
    return table[tier][0]


def write_rehashed_table_model(path, cfg, model):
    # Nonfinite JSON numbers are deliberately malformed input: the frozen
    # loader accepts json.loads' constants, although save_model disallows them.
    # Recompute BOTH identities so no stale hash can mask semantic rejection.
    payload = json.dumps(model, sort_keys=True, separators=(",", ":"), allow_nan=True).encode()
    model_path = path.parent / "models" / "fix1-malformed.json"
    model_path.write_bytes(payload)
    cfg["model_path"] = "models/" + model_path.name
    cfg["model_sha256"] = hashlib.sha256(payload).hexdigest()
    cfg["experiment_sha256"] = fingerprint(model, (path.parent / cfg["card_path"]).read_bytes(), cfg)
    write_config(path, cfg)


@pytest.mark.parametrize("tier", ["base_rates", "parents", "cells"])
@pytest.mark.parametrize("field,value", [
    ("n", 0), ("n", True), ("n", 1.5), ("n", "1000"),
    ("p_win", 2), ("p_loss", -0.1), ("p_time", 0.2),
    ("p_win", False), ("r_win", "1.9"), ("r_loss", float("nan")),
    ("r_time", float("inf")), ("se_win", -10), ("se_loss", -1),
    ("se_win", float("inf")),
])
def test_fix1_rehashed_probability_estimates_reject_before_runtime(
        tmp_path, monkeypatch, runner, capsys, tier, field, value):
    path, cfg, model = make_config(tmp_path, strong=False)
    table_estimate(model, tier)[field] = value
    write_rehashed_table_model(path, cfg, model)
    assert_rejected_without_effects(runner, path, cfg, monkeypatch)
    out = capsys.readouterr().out
    assert f"prob_table.{tier}" in out
    assert "experiment_sha256" not in out and "哈希与预期不符" not in out


@pytest.mark.parametrize("case", [
    "base_type", "parents_type", "cells_type", "base_row_type", "parent_row_type", "cell_row_type",
    "base_missing_estimate", "parent_missing_estimate", "cell_missing_estimate",
    "parent_missing_key", "cell_missing_key", "parent_extra_key", "cell_extra_key",
    "base_action", "parent_action", "cell_action", "parent_situation", "cell_situation",
    "missing_base", "missing_parent", "duplicate_parent", "duplicate_cell",
    "cell_features_type", "cell_feature_missing", "cell_feature_extra", "cell_feature_value",
    "schema_bool", "prior_float", "min_cell_float",
])
def test_fix1_rehashed_table_structure_and_scope_reject_before_runtime(
        tmp_path, monkeypatch, runner, capsys, case):
    path, cfg, model = make_config(tmp_path, strong=False)
    table = model["prob_table"]
    cell = table_estimate(model, "cells")
    base, parent = table["base_rates"]["A3"], table["parents"][0]
    if case == "base_type":
        table["base_rates"] = []
    elif case == "parents_type":
        table["parents"] = {}
    elif case == "cells_type":
        table["cells"] = {}
    elif case == "base_row_type":
        table["base_rates"]["A3"] = []
    elif case == "parent_row_type":
        table["parents"] = [[]]
    elif case == "cell_row_type":
        table["cells"] = [[]]
    elif case.endswith("missing_estimate"):
        {"base": base, "parent": parent, "cell": cell}[case.split("_")[0]].pop("se_loss")
    elif case.endswith("missing_key"):
        (parent if case.startswith("parent") else cell).pop("action")
    elif case.endswith("extra_key"):
        (parent if case.startswith("parent") else cell)["extra"] = 1
    elif case == "base_action":
        table["base_rates"]["A9"] = table["base_rates"].pop("A3")
    elif case.endswith("action"):
        (parent if case.startswith("parent") else cell)["action"] = "A9"
    elif case.endswith("situation"):
        (parent if case.startswith("parent") else cell)["situation"] = "S2"
    elif case == "missing_base":
        table["base_rates"] = {}
    elif case == "missing_parent":
        table["parents"] = []
    elif case == "duplicate_parent":
        table["parents"].append(dict(parent))
    elif case == "duplicate_cell":
        table["cells"].append(dict(cell))
    elif case == "cell_features_type":
        cell["features"] = []
    elif case == "cell_feature_missing":
        cell["features"].pop("trend_sma200")
    elif case == "cell_feature_extra":
        cell["features"]["unexpected"] = "above"
    elif case == "cell_feature_value":
        cell["features"]["trend_sma200"] = "unknown"
    elif case == "schema_bool":
        table["schema_version"] = True
    elif case == "prior_float":
        table["prior_strength"] = 100.0
    elif case == "min_cell_float":
        table["min_cell"] = 100.0
    write_rehashed_table_model(path, cfg, model)
    assert_rejected_without_effects(runner, path, cfg, monkeypatch)
    assert "prob_table" in capsys.readouterr().out


@pytest.mark.parametrize("case", ["lookback_type", "unknown_key", "missing_required"])
def test_fix1_missing_model_with_invalid_legacy_field_never_enters_guard(
        tmp_path, monkeypatch, runner, capsys, case):
    path, cfg, _ = make_config(tmp_path)
    for key in ("model_path", "model_sha256", "card_path", "experiment_sha256", "experiment"):
        cfg.pop(key)
    if case == "lookback_type":
        cfg["lookback_bars"] = "2100"
    elif case == "unknown_key":
        cfg["lookbak_bars"] = 2100
    else:
        cfg.pop("symbol")
    write_config(path, cfg)
    # Deliberately omit --data-dir: an erroneous guard would target cwd/data/live.
    monkeypatch.chdir(tmp_path)
    assert_rejected_without_effects(runner, path, cfg, monkeypatch)
    assert "配置预检失败" in capsys.readouterr().out


def test_fix1_legacy_field_error_keeps_guarded_evidence(tmp_path, monkeypatch, runner):
    path = write(tmp_path / "legacy.toml", f'''symbol = "XAUUSD"
timeframe = "H1"
strategy = "{H_STRATEGY}"
magic = 780001
lookback_bars = "2100"
volume_lots = 0.01
''')
    monkeypatch.chdir(tmp_path)
    assert runner.main(offline_args(path)) == 2
    marker = read_json(tmp_path / "data/live/exit-runner.json")
    assert marker["rc"] == 2 and "lookback_bars" in marker["reason"]


def test_fix2_fittable_volume_overflow_without_model_rejects_before_guard(
        tmp_path, monkeypatch, runner, capsys):
    path = tmp_path / "config" / "live.toml"
    cfg = dict(symbol="XAUUSD", timeframe="H1", strategy=STRATEGY,
               magic=MAGIC, lookback_bars=2100, volume_lots=10 ** 400)
    write_config(path, cfg)
    # Both data_root and --data-dir are absent; a mistaken guard would write
    # cwd/data/live. All sentinel state and filesystem comparisons stay in tmp.
    monkeypatch.chdir(tmp_path)
    assert_rejected_without_effects(
        runner, path, {**cfg, "data_root": str(tmp_path / "data")}, monkeypatch)
    assert "配置预检失败" in capsys.readouterr().out


def test_fix2_nonfittable_volume_overflow_keeps_guarded_evidence(
        tmp_path, monkeypatch, runner, caplog):
    path = tmp_path / "legacy.toml"
    write_config(path, dict(symbol="XAUUSD", timeframe="H1", strategy=H_STRATEGY,
                           magic=MAGIC, lookback_bars=2100, volume_lots=10 ** 400))
    monkeypatch.chdir(tmp_path)
    assert runner.main(offline_args(path)) == 2
    marker = read_json(tmp_path / "data/live/exit-runner.json")
    assert marker["rc"] == 2
    assert "int too large to convert to float" in marker["reason"]
    assert "OverflowError" in caplog.text
