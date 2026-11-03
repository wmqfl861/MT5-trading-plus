#!/usr/bin/env python3
"""Explicit-input T121 offline engineering entry; never authorizes production."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd

from mt5data.sessions import load_session_policy
from mt5quant.prob.feasibility import run_feasibility

PREREG = "docs/research/pline-feasibility-prereg-2026-11-02.md"
PREREG_SHA256 = "c78a78d13ed81c611b5f9599c96235d7bbcefceac6fda54566b103f3e8cb3235"
CODE_FILES = (
    "scripts/pline_feasibility.py", "src/mt5quant/prob/feasibility.py",
    "src/mt5quant/prob/counting.py", "src/mt5quant/prob/gaps.py",
    "src/mt5quant/strategies/library/prob/situations.py",
    "src/mt5quant/strategies/library/prob/actions.py", "src/mt5data/sessions.py",
    "src/mt5quant/costs/volatility.py", "src/mt5quant/costs/spread.py",
    "src/mt5quant/costs/slippage.py", "src/mt5quant/costs/commission.py",
    "src/mt5quant/costs/overshoot.py", "src/mt5quant/costs/swap.py",
)


def digest(content):
    return hashlib.sha256(content).hexdigest()


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="explicit JSON H1 inputs and identities")
    parser.add_argument("--cost-evidence", type=Path, required=True, help="explicit JSON symbol cost evidence")
    parser.add_argument("--sessions", type=Path, required=True, help="frozen sessions TOML")
    parser.add_argument("--output", type=Path, required=True, help="new exclusive output directory")
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise ValueError("output directory already exists; refusing overwrite")
        input_bytes, cost_bytes = args.input.read_bytes(), args.cost_evidence.read_bytes()
        input_doc, costs = json.loads(input_bytes), json.loads(cost_bytes)
        symbols = input_doc["symbols"]
        bars, tags, gaps, evidence = {}, {}, {}, {}
        for symbol, item in symbols.items():
            if symbol not in ("XAUUSD", "EURUSD"):
                raise ValueError("input contains a symbol outside the frozen pool")
            bars[symbol] = pd.DataFrame(item["bars"], columns=["time", "open", "high", "low", "close"])
            if "closed" in pd.DataFrame(item["bars"]).columns:
                bars[symbol]["closed"] = pd.DataFrame(item["bars"])["closed"]
            tags[symbol] = item.get("source_tags")
            gaps[symbol] = item.get("gap_spans")
            evidence[symbol] = item.get("evidence")
        prereg_hash = digest(git("show", "HEAD:" + PREREG))
        if prereg_hash != PREREG_SHA256:
            raise ValueError("preregistration differs from the A2-bound version")
        report = run_feasibility(bars, source_tags=tags, gap_spans=gaps,
            sessions=load_session_policy(str(args.sessions)), input_evidence=evidence,
            cost_evidence=costs)
        report_bytes = (json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
        receipt = {
            "artifact_kind": "synthetic-engineering-only",
            "production_execution": "not_released",
            "input_sha256": digest(input_bytes), "cost_evidence_sha256": digest(cost_bytes),
            "sessions_sha256": digest(args.sessions.read_bytes()),
            "prereg_sha256": prereg_hash,
            "qualification_sha256": report["qualification_sha256"],
            "code_sha": git("rev-parse", "HEAD").decode().strip(),
            "code_dirty": bool(git("status", "--porcelain", "--", *CODE_FILES).strip()),
            "code_files_sha256": {name: digest((ROOT / name).read_bytes()) for name in CODE_FILES},
            "report_sha256": digest(report_bytes),
        }
        args.output.mkdir(parents=True, exist_ok=False)
        (args.output / "report.json").write_bytes(report_bytes)
        (args.output / "receipt.json").write_text(
            json.dumps(receipt, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        print("T121 synthetic engineering artifact: 96 rows, 8 summaries; production not released")
        return 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"T121 refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
