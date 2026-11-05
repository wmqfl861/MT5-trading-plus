"""Explicit synthetic T114 precheck; no default input/data root or ledger writes."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from mt5quant.costs import (
    CommissionModel, Costs, SlippageModel, SpreadModel, SpreadStat, SwapModel, SwapRule,
)
from mt5quant.costs.spread import BUCKET_TZ
from mt5quant.h_main_scope import CoverageSegment, TimeInterval
from mt5quant.h_readonly_precheck import (
    PAIRS, RULES, _fields, _number, _validate_cost_model, _validate_metadata,
    run_h_readonly_precheck,
)
from mt5quant.research.candidates import validate_candidate


class _Artifacts:
    """One read per resolved path: hashes and parsers use the identical bytes."""

    def __init__(self):
        self.raw = {}
        self.hashes = {}

    def load(self, path, label):
        path = Path(path).resolve()
        if path.suffix.lower() not in (".json", ".toml"):
            raise ValueError(f"{label}: explicit JSON/TOML artifact required")
        if path not in self.raw:
            self.raw[path] = path.read_bytes()
        raw = self.raw[path]
        self.hashes[label] = hashlib.sha256(raw).hexdigest()
        return _parse_bytes(raw, path.suffix.lower())


def _parse_bytes(raw, suffix):
    def pairs(items):
        out = {}
        for k, v in items:
            if k in out:
                raise ValueError(f"duplicate key: {k}")
            out[k] = v
        return out

    def constant(value):
        raise ValueError(f"nonfinite JSON number: {value}")

    text = raw.decode("utf-8")
    return (tomllib.loads(text) if suffix == ".toml"
            else json.loads(text, object_pairs_hook=pairs, parse_constant=constant))


def _synthetic(doc, required, label):
    _fields(doc, {"mode", *required}, label)
    if doc["mode"] != "synthetic":
        raise PermissionError(f"{label}: not_released; only synthetic fixtures are supported")


def _metadata(doc):
    _synthetic(doc, {"inputs"}, "manifest")
    _fields(doc["inputs"], PAIRS, "manifest inputs")
    records = {}
    for key, pair in PAIRS.items():
        rec = doc["inputs"][key]
        _fields(rec, {"bars_file", "coverage", "missing_kind", "missing_intervals", "source_eligibility"}, key)
        if not isinstance(rec["bars_file"], str) or not rec["bars_file"]:
            raise ValueError(f"{key}: bars_file required")
        if not isinstance(rec["coverage"], list) or not isinstance(rec["missing_intervals"], list):
            raise ValueError(f"{key}: explicit coverage/missing_intervals lists required")
        segments, holes = [], []
        for s in rec["coverage"]:
            _fields(s, {"source", "start", "end"}, f"{key} coverage")
            segments.append(CoverageSegment(*pair, s["source"], s["start"], s["end"]))
        for h in rec["missing_intervals"]:
            _fields(h, {"start", "end"}, f"{key} missing")
            holes.append(TimeInterval(h["start"], h["end"]))
        records[key] = {"coverage": segments, "missing_kind": rec["missing_kind"],
                        "missing_intervals": holes, "source_eligibility": rec["source_eligibility"]}
    _validate_metadata(records)
    return records


def _integer(value, label, lower, upper=None):
    if type(value) is not int or value < lower or (upper is not None and value > upper):
        raise ValueError(f"{label}: invalid integer")
    return value


def _costs(doc):
    """Bytes-decoded assembly, using standard model constructors, no build_costs I/O.

    Spread is the existing to_dict schema. Unlike legacy load(), mode/sampling
    must be explicit and unknown keys/omitted charges are rejected. No fees are
    supplied by constructor defaults. Swap calendar defaults retain SwapRule's
    convention; the precheck uses declared fee-nights, not calendar simulation.
    Relative swap bases are parsed as SwapRule fields but rejected by the shared
    API cost validator; only absolute per-lot rates are supported here.
    """
    _synthetic(doc, {"spread", "slippage", "commission_per_lot", "swap"}, "costs")
    spread = doc["spread"]
    _fields(spread, {"mode", "sampling", "bucket_tz", "per_symbol", "overall_median"}, "spread")
    if spread["bucket_tz"] != BUCKET_TZ:
        raise ValueError("spread bucket_tz must be America/New_York")
    _fields(spread["per_symbol"], {"EURUSD"}, "spread per_symbol")
    _fields(spread["overall_median"], {"EURUSD"}, "spread overall_median")
    hours = spread["per_symbol"]["EURUSD"]
    if not isinstance(hours, dict) or not hours:
        raise ValueError("EURUSD spread statistics missing")
    stats = {}
    for hour, st in hours.items():
        if not isinstance(hour, str) or hour not in {str(i) for i in range(24)}:
            raise ValueError("spread hour must be canonical 0..23")
        _fields(st, {"median", "p90", "p99", "n", "n_days"}, "SpreadStat")
        numbers = [_number(st[f], f"spread {f}") for f in ("median", "p90", "p99")]
        if not numbers[0] <= numbers[1] <= numbers[2]:
            raise ValueError("spread quantiles must be ordered")
        stats[int(hour)] = SpreadStat(*numbers, _integer(st["n"], "spread n", 1),
                                     _integer(st["n_days"], "spread n_days", 0))
    model = SpreadModel({"EURUSD": stats}, {"EURUSD": _number(
        spread["overall_median"]["EURUSD"], "overall_median")},
        mode=spread["mode"], sampling=spread["sampling"])
    slip = doc["slippage"]
    _fields(slip, {"spread_fraction", "fixed_price", "event_hours", "event_multiplier"}, "slippage")
    if not isinstance(slip["event_hours"], list):
        raise ValueError("slippage event_hours must be explicit list")
    slippage = SlippageModel(model,
        spread_fraction=_number(slip["spread_fraction"], "slippage spread_fraction"),
        fixed_price=_number(slip["fixed_price"], "slippage fixed_price"),
        event_hours=frozenset(_integer(h, "event hour", 0, 23) for h in slip["event_hours"]),
        event_multiplier=_number(slip["event_multiplier"], "event_multiplier", positive=True))
    _fields(doc["commission_per_lot"], {"EURUSD"}, "commission_per_lot")
    commission = _number(doc["commission_per_lot"]["EURUSD"], "commission_per_lot EURUSD")
    _fields(doc["swap"], {"EURUSD"}, "swap")
    swap = dict(doc["swap"]["EURUSD"])
    _fields(swap, {"long_per_day", "short_per_day"}, "swap EURUSD",
            {"triple_weekdays", "charge_weekdays", "charge_local", "charge_tz", "ref_price", "contract_size"})
    for f in ("long_per_day", "short_per_day"):
        swap[f] = _number(swap[f], f"swap {f}", signed=True)
    for f in ("ref_price", "contract_size"):
        if f in swap:
            swap[f] = _number(swap[f], f"swap {f}", positive=True)
    for f in ("triple_weekdays", "charge_weekdays"):
        if f in swap:
            if not isinstance(swap[f], list):
                raise ValueError(f"swap {f} must be explicit list")
            swap[f] = frozenset(_integer(w, f, 0, 6) for w in swap[f])
    costs = Costs(model, slippage, CommissionModel(default_per_lot=commission,
                  per_symbol={"EURUSD": commission}), SwapModel({"EURUSD": SwapRule(**swap)}))
    _validate_cost_model(costs)  # Reject unsupported bases before reading bars.
    return costs


def _code_hashes():
    root = Path(__file__).resolve().parents[1]
    # Identity covers the entry, local adapter and frozen computation dependencies.
    files = ["scripts/h_readonly_precheck.py", "src/mt5quant/h_readonly_precheck.py",
             "src/mt5quant/h_main_scope.py", "src/mt5quant/research/candidates.py",
             "src/mt5quant/research/power.py", "src/mt5quant/validation/events.py",
             "src/mt5data/ext/merge.py", "src/mt5quant/costs/__init__.py",
             "src/mt5quant/costs/spread.py", "src/mt5quant/costs/slippage.py",
             "src/mt5quant/costs/commission.py", "src/mt5quant/costs/swap.py"]
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True)
    for name in ("inputs", "candidate", "costs", "specs"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--cumulative-n", type=int)
    parser.add_argument("--output", type=Path, help="new exclusive directory; omitted means stdout only")
    args = parser.parse_args(argv)
    try:
        if args.mode != "synthetic":
            raise PermissionError("not_released: production/self-approved modes are unavailable")
        artifacts = _Artifacts()
        manifest = artifacts.load(args.inputs, "inputs")
        inputs = _metadata(manifest)  # ALL source qualifications checked before prices.
        candidate = validate_candidate(artifacts.load(args.candidate, "candidate"), source="synthetic bytes")
        costs = _costs(artifacts.load(args.costs, "costs"))
        specs = artifacts.load(args.specs, "specs")
        _synthetic(specs, {"contract_sizes"}, "specs")
        for key in PAIRS:
            path = Path(manifest["inputs"][key]["bars_file"])
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("bars_file must be relative to the synthetic manifest directory")
            doc = artifacts.load(args.inputs.parent / path, "bars:" + key)
            _fields(doc, {"bars", "source_tags"}, key + " artifact")
            if not isinstance(doc["bars"], list):
                raise ValueError("bars must be explicit records")
            for bar in doc["bars"]:
                _fields(bar, {"time", "close"}, "bar")
            inputs[key].update(bars=pd.DataFrame(doc["bars"], columns=["time", "close"]),
                               source_tags=doc["source_tags"])
        result = run_h_readonly_precheck(candidate, inputs=inputs, cost_model=costs,
                                        contract_sizes=specs["contract_sizes"], cumulative_n=args.cumulative_n)
        rules = json.dumps(RULES, sort_keys=True, separators=(",", ":")).encode("utf-8")
        payload = {"result": result, "hashes": {"domain": "sha256(raw_artifact_bytes)",
                   "artifacts": artifacts.hashes, "rules_domain": "sha256(canonical_rules_json_utf8)",
                   "rules": hashlib.sha256(rules).hexdigest(), "code": _code_hashes()}}
        text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output is not None:
            args.output.mkdir(exist_ok=False)
            with (args.output / "result.json").open("x", encoding="utf-8") as stream:
                stream.write(text)
        print(text, end="")
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"h_readonly_precheck: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
