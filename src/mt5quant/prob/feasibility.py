"""T121 bounded, in-memory trigger counting and frozen cost scenarios.

No data discovery, approval creation, candidate screening or outcome evaluation.
Production admission stays closed under the A2/A3 execution restrictions.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from datetime import timedelta
import hashlib
import json
import math

import pandas as pd

from mt5data.sessions import SessionPolicy, local_to_utc_naive
from mt5quant.costs.commission import CommissionModel
from mt5quant.costs.overshoot import OvershootStat, StopOvershootModel
from mt5quant.costs.slippage import SlippageModel
from mt5quant.costs.spread import SpreadModel, SpreadStat
from mt5quant.costs.swap import SwapModel, SwapRule
from mt5quant.costs.volatility import atr_series
from mt5quant.prob.counting import count_triggers
from mt5quant.prob.gaps import eligibility_reasons
from mt5quant.strategies.library.prob.actions import ActionParams
from mt5quant.strategies.library.prob.situations import Approach

CUTOFF = pd.Timestamp("2016-05-01")
NY_TZ = "America/New_York"


def _times(values):
    return pd.to_datetime(values, utc=True).dt.tz_localize(None)


def _source(tag):
    if tag == "mt5":
        return "mt5"
    if isinstance(tag, str) and tag.startswith("ext:HistData:"):
        return "external"
    return None


def prepare_bars(h1, source_tags, timeframe):
    """Return closed discovery bars, conservative tags, exclusion counts.

    H1 labels must be unique, ordered UTC hour opens. A supplied ``closed``
    column can explicitly exclude incomplete roots; otherwise the caller's
    closed-H1 input contract applies. Unknown sources stay present.
    """
    if timeframe not in ("H1", "H4"):
        raise ValueError("timeframe must be H1 or H4")
    b = h1.copy().reset_index(drop=True)
    b["time"] = _times(b["time"])
    if b.time.isna().any() or not b.time.is_monotonic_increasing:
        raise ValueError("H1 times must be finite and ordered")
    if b.time.duplicated().any():
        raise ValueError("duplicate H1 roots")
    if not (b.time == b.time.dt.floor("h")).all():
        raise ValueError("H1 times must be UTC hour opens")
    tags = [None] * len(b) if source_tags is None else list(source_tags)
    if len(tags) != len(b):
        raise ValueError("source_tags must match H1 roots")
    b["_source"] = pd.Series([_source(t) for t in tags], dtype=object)
    reasons = Counter()
    inside = b.time + pd.Timedelta(hours=1) <= CUTOFF
    reasons["outside_discovery"] += int((~inside).sum())
    closed = b["closed"].eq(True) if "closed" in b else pd.Series(True, index=b.index)
    reasons["incomplete_h1"] += int((inside & ~closed).sum())
    b = b.loc[inside & closed].reset_index(drop=True)
    cols = ["time", "open", "high", "low", "close"]
    if timeframe == "H1":
        return b[cols], b._source.tolist(), {k: v for k, v in reasons.items() if v}
    rows, sources = [], []
    for root, group in b.groupby(b.time.dt.floor("4h"), sort=True):
        expected = list(pd.date_range(root, periods=4, freq="h"))
        if group.time.tolist() != expected:
            reasons["incomplete_h4"] += 1
            continue
        rows.append(dict(time=root, open=group.open.iloc[0],
                         high=group.high.max(skipna=False), low=group.low.min(skipna=False),
                         close=group.close.iloc[-1]))
        values = group._source.tolist()
        sources.append(None if None in values else
                       "external" if "external" in values else "mt5")
    return (pd.DataFrame(rows, columns=cols), sources,
            {k: v for k, v in reasons.items() if v})


def s2_levels(h1, target_times):
    """H1 session extrema visible at each target open, plus actual H1 start.

    Sessions end at New York 17:00. H4 opens inside a session never see
    that session's eventual extrema, including at a DST transition.
    """
    b = h1.copy().reset_index(drop=True)
    b["time"] = _times(b["time"])
    local = b.time.dt.tz_localize("UTC").dt.tz_convert(NY_TZ)
    labels = [t.date() + timedelta(days=int(t.hour >= 17)) for t in local]
    sessions = []
    for day, group in b.groupby(labels, sort=True):
        end = pd.Timestamp(local_to_utc_naive(day, NY_TZ, "17:00"))
        sessions.append((end, group.time.iloc[0],
                         sorted(set([float(group.low.min()), float(group.high.max())]))))
    ends = [s[0] for s in sessions]
    levels, starts = [], []
    for t in _times(pd.Series(list(target_times))):
        j = bisect_right(ends, t) - 1
        levels.append([] if j < 0 else sessions[j][2])
        starts.append(pd.NaT if j < 0 else sessions[j][1])
    return pd.Series(levels, dtype=object), pd.Series(starts, dtype="datetime64[ns]")


def qualify_triggers(triggers, bars, *, symbol, situation, hold_bars,
                     gap_spans, sessions: SessionPolicy, s3_bars=480,
                     situation_starts=None):
    """Registered qualification only; future bars supply timestamps alone."""
    if sessions.forbidden_hours_ny:
        raise ValueError("data-driven forbidden hours are not T121 calendar policy")
    gap_spans = _normalize_gap_spans(gap_spans)
    times = _times(bars["time"]).reset_index(drop=True)
    temporal = pd.DataFrame({"time": times})
    reasons = eligibility_reasons(
        triggers, temporal, gap_spans, situation, 24, s3_bars=s3_bars,
        feature_warmup=2000, hold_bars=hold_bars, max_gap_days=30,
        situation_starts=situation_starts)
    for k, i in enumerate(triggers["bar"]):
        if reasons[k]:
            continue
        entry, end = int(i) + 1, int(i) + hold_bars + 1
        if times.iloc[entry] >= CUTOFF or times.iloc[end] >= CUTOFF:
            reasons[k] = "discovery_cutoff"
        elif not sessions.entry_allowed(symbol, times.iloc[entry].to_pydatetime()):
            reasons[k] = "session_forbidden"
    kept = triggers.iloc[[i for i, r in enumerate(reasons) if not r]].copy()
    return kept, dict(Counter(r for r in reasons if r))


def payload_sha256(payload):
    """Canonical JSON identity, including malformed numbers for fail reporting."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     allow_nan=True).encode()).hexdigest()


def _number(value, *, positive=False):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and (value > 0 if positive else value >= 0))


def _utc(value):
    t = pd.Timestamp(value)
    return t.tz_convert("UTC").tz_localize(None) if t.tzinfo is not None else t


def _normalize_gap_spans(gap_spans):
    """Normalize admitted gap endpoints to the bars' naive UTC basis."""
    if gap_spans is None:
        return []
    normalized = []
    for span in gap_spans:
        if not isinstance(span, (tuple, list)) or len(span) != 2:
            raise ValueError("gap span must contain exactly two timestamps")
        start, end = (_utc(value) for value in span)
        if pd.isna(start) or pd.isna(end) or not start < end:
            raise ValueError("gap span must be an increasing finite interval")
        normalized.append((start, end))
    return normalized


def _valid_weekday_field(value):
    """Require an explicit iterable of plain weekday integers 0..6."""
    if not isinstance(value, (list, tuple, set, frozenset)):
        return False
    return all(isinstance(day, int) and not isinstance(day, bool) and 0 <= day <= 6
               for day in value)


def _valid_event_hours(value):
    """Require explicit local-hour integers 0..23; booleans are not integers here."""
    if not isinstance(value, (list, tuple, set, frozenset)):
        return False
    return all(isinstance(hour, int) and not isinstance(hour, bool) and 0 <= hour <= 23
               for hour in value)


def trigger_cost(*, symbol, signal_time, entry_time, exit_time, side, source,
                 signal_price, atr, sl_atr, evidence):
    """One frozen cost scenario using signal prices and endpoint TIMES only.

    Evidence schema is intentionally narrow and synthetic-only. Hashes establish
    identity, not permission; production evidence never reaches cost arithmetic.
    Missing and invalid components are accumulated independently.
    """
    waiting, failures = [], []

    def result(value=None):
        if value is not None and value > .02:
            failures.append("cost_above_limit")
        return {"cost_r": value,
                "cost_gate": "fail" if failures else "waiting_data" if waiting else "pass",
                "waiting_reasons": sorted(set(waiting)),
                "fail_reasons": sorted(set(failures))}

    for name, value in (("signal_price", signal_price), ("atr", atr), ("sl_atr", sl_atr)):
        if not _number(value, positive=True):
            failures.append("invalid_" + name)
    if not failures and not _number(atr * sl_atr, positive=True):
        failures.append("invalid_r")
    if source not in ("mt5", "external"):
        waiting.append("source_unknown")
    if side not in (-1, 1):
        failures.append("invalid_side")
    if not isinstance(evidence, dict):
        waiting.append("missing_cost_evidence")
        return result()
    # No caller-supplied approval string can release real cost evidence.
    if evidence.get("kind") != "synthetic":
        waiting.append("production_cost_not_released")
        return result()
    expected = {"purpose": "T121-count-cost", "symbol": symbol, "account_kind": "Demo",
                "account_currency": "USD", "quote_currency": "USD"}
    for key, value in expected.items():
        if evidence.get(key) != value:
            waiting.append("evidence_" + key)
    if not evidence.get("basis_ref"):
        waiting.append("evidence_basis_ref")
    try:
        start, end = (_utc(evidence[k]) for k in ("applies_start", "applies_end"))
        observed_start, observed_end = (_utc(evidence[k]) for k in ("observed_start", "observed_end"))
        signal, entry, exit_ = map(_utc, (signal_time, entry_time, exit_time))
        if (any(pd.isna(t) for t in (start, end, observed_start, observed_end, signal, entry, exit_))
                or not start <= signal < entry <= exit_ < end
                or exit_ >= CUTOFF or not observed_start <= observed_end < CUTOFF):
            waiting.append("evidence_range")
    except (KeyError, ValueError, TypeError):
        waiting.append("evidence_range")
    payload = evidence.get("payload")
    if not isinstance(payload, dict) or evidence.get("payload_sha256") != payload_sha256(payload):
        waiting.append("evidence_hash")
    if waiting and any(r.startswith("evidence_") for r in waiting):
        return result()  # no arithmetic on unbound or rejected evidence
    parts = {}
    for name in ("spread", "slippage", "commission", "contract", "overshoot", "swap"):
        part = payload.get(name)
        if not isinstance(part, dict):
            waiting.append("missing_" + name)
        else:
            parts[name] = part

    def field(part, key, *, positive=False):
        if key not in parts[part]:
            waiting.append("missing_" + part + "_" + key)
        elif not _number(parts[part][key], positive=positive):
            failures.append("invalid_" + part + "_" + key)

    if "contract" in parts:
        field("contract", "size", positive=True)
        if not parts["contract"].get("unit"):
            waiting.append("missing_contract_unit")
    if "commission" in parts:
        field("commission", "round_trip_per_lot")
        if not parts["commission"].get("basis"):
            waiting.append("missing_commission_basis")
    if "slippage" in parts:
        for key in ("spread_fraction", "fixed_price", "event_multiplier"):
            field("slippage", key, positive=key == "event_multiplier")
        if "event_hours" not in parts["slippage"]:
            waiting.append("missing_slippage_event_hours")
        elif not _valid_event_hours(parts["slippage"]["event_hours"]):
            failures.append("invalid_slippage_event_hours")

    qualified = {}
    for name in ("spread", "overshoot"):
        qualified[name] = {}
        if name not in parts:
            continue
        table = parts[name].get("hours")
        if not isinstance(table, dict):
            waiting.append("missing_" + name + "_hours")
            continue
        if name == "spread" and parts[name].get("mode") not in ("absolute", "relative"):
            waiting.append("spread_mode")
        if name == "overshoot" and (parts[name].get("version") != 2 or
                                       parts[name].get("basis") != "stop_level"):
            waiting.append("overshoot_version_basis")
        for hour, stat in table.items():
            try:
                if not isinstance(hour, (str, int)) or isinstance(hour, bool):
                    raise ValueError
                h = int(hour)
                if not 0 <= h <= 23:
                    raise ValueError
                if not isinstance(stat, dict):
                    raise TypeError
                p90_present = "p90" in stat
                p90_valid = p90_present and _number(stat["p90"])
                if p90_present and not p90_valid:
                    failures.append("invalid_" + name + "_p90")
                if "n_days" not in stat:
                    waiting.append("invalid_" + name + "_coverage")
                    continue
                days = stat["n_days"]
                if not isinstance(days, int) or isinstance(days, bool) or days < 0:
                    waiting.append("invalid_" + name + "_coverage")
                    continue
                if not p90_present:
                    waiting.append("invalid_" + name + "_coverage")
                elif p90_valid and days >= 20:
                    qualified[name][h] = stat["p90"]
            except (ValueError, TypeError):
                waiting.append("invalid_" + name + "_coverage")
        if not qualified[name]:
            waiting.append("no_qualified_" + name + "_hours")
    if "spread" in parts:
        hour = entry.tz_localize("UTC").tz_convert(NY_TZ).hour
        if hour not in qualified["spread"]:
            waiting.append("entry_spread_coverage")
    if "swap" in parts:
        swap = parts["swap"]
        if swap.get("scope") != "Demo":
            waiting.append("swap_scope")
        if swap.get("basis") not in ("free", "charged"):
            waiting.append("swap_basis")
        if swap.get("basis") == "charged":
            for key in ("long_per_day", "short_per_day", "triple_weekdays", "charge_weekdays"):
                if key not in swap:
                    waiting.append("missing_swap_" + key)
            if "triple_weekdays" in swap and not _valid_weekday_field(swap["triple_weekdays"]):
                failures.append("invalid_swap_triple_weekdays")
            if "charge_weekdays" in swap and not _valid_weekday_field(swap["charge_weekdays"]):
                failures.append("invalid_swap_charge_weekdays")
            for key in ("long_per_day", "short_per_day"):
                if key in swap and not _number(-swap[key] if isinstance(swap[key], (int, float)) else None):
                    failures.append("invalid_swap_" + key)
            if "ref_price" in swap and not _number(swap["ref_price"], positive=True):
                failures.append("invalid_swap_ref_price")
    if waiting or failures:
        return result()
    try:
        contract = parts["contract"]["size"]
        spread = SpreadModel({symbol: {h: SpreadStat(v, v, v, 1, 20)
                                      for h, v in qualified["spread"].items()}},
                             mode=parts["spread"]["mode"])
        slip = SlippageModel(spread, **parts["slippage"])
        overshoot = StopOvershootModel({symbol: {h: OvershootStat(v, v, 1, 20)
                                                for h, v in qualified["overshoot"].items()}})
        spread_cost = max(spread.spread_p90(symbol, h, price=signal_price)
                          for h in qualified["spread"]) * (1.5 if source == "external" else 1.)
        slip_cost = slip.slippage(symbol, hour, price=signal_price) + max(
            slip.slippage(symbol, h, price=signal_price) for h in qualified["spread"])
        over_cost = max(overshoot.overshoot(symbol, h, price=signal_price, level="p90")
                        for h in qualified["overshoot"])
        comm = CommissionModel(per_symbol={symbol: parts["commission"]["round_trip_per_lot"]})
        fee = 0.
        if parts["swap"]["basis"] == "charged":
            rule_args = {k: parts["swap"][k] for k in
                         ("long_per_day", "short_per_day", "triple_weekdays", "charge_weekdays")}
            if "ref_price" in parts["swap"]:
                rule_args.update(ref_price=parts["swap"]["ref_price"], contract_size=contract)
            model = SwapModel({symbol: SwapRule(**rule_args)})
            fee = model.overnight_fee(symbol, "long" if side == 1 else "short",
                                      entry, exit_, price=signal_price)
        if not isinstance(fee, (int, float)) or not math.isfinite(fee) or fee > 0:
            failures.append("invalid_overnight_fee")
            return result()
        components = [spread_cost, slip_cost, over_cost, comm.commission_per_lot(symbol) / contract,
                      -fee / contract]
        if not all(_number(v) for v in components):
            failures.append("invalid_cost_component")
            return result()
        value = math.fsum(components) / (atr * sl_atr)
        if not _number(value):
            failures.append("invalid_cost_r")
            return result()
        return result(value)
    except (ValueError, TypeError, KeyError, OverflowError):
        failures.append("invalid_cost_model")
        return result()


def summarize_hypothesis(times, costs, *, waiting_reasons=(), fail_reasons=()):
    """Registered UTC day weights, descriptive counts, and independent gates.

    ``times=None`` means not counted. An empty sequence means actually zero.
    Per-trigger exceedances do not replace the gate on the complete day mean.
    """
    waiting, failures = list(waiting_reasons), list(fail_reasons)
    value, n, days, distribution, gaps = None, None, None, None, None
    if times is None:
        waiting.append("not_counted")
    else:
        ts = _times(pd.Series(list(times), dtype="datetime64[ns]"))
        n = len(ts)
        if len(costs) != n:
            raise ValueError("one cost record is required per qualified trigger")
        counts = ts.dt.normalize().value_counts().sort_index()
        days = len(counts)
        distribution = ({"p50": float(counts.quantile(.5)), "p90": float(counts.quantile(.9)),
                         "max": int(counts.max())} if days else None)
        gaps = [(b - a).days for a, b in zip(counts.index, counts.index[1:])]
        for item in costs:
            waiting.extend(item["waiting_reasons"])
            failures.extend(r for r in item["fail_reasons"] if r != "cost_above_limit")
            if item["cost_r"] is None and not item["waiting_reasons"] and not item["fail_reasons"]:
                waiting.append("missing_trigger_cost")
        if n and all(item["cost_r"] is not None for item in costs) and not waiting and not failures:
            values = pd.Series([c["cost_r"] for c in costs])
            if not all(_number(v) for v in values):
                failures.append("invalid_cost_r")
            else:
                value = float(values.groupby(ts.dt.normalize()).mean().mean())
                if value > .02:
                    failures.append("cost_above_limit")
        if n == 0:
            waiting.append("no_qualified_triggers")
        if days < 4521:
            waiting.append("insufficient_days")
    sample_gate = "pass" if days is not None and days >= 4521 else "waiting_data"
    cost_gate = "fail" if failures else "pass" if value is not None else "waiting_data"
    return dict(n_triggers=n, n_days=days, cost_r=value,
                three_cost_r=None if value is None else 3 * value,
                delta_ev_r=.06, design_effect=1.5, n_required=4521,
                triggers_per_day=distribution, adjacent_day_gaps=gaps,
                sample_gate=sample_gate, cost_gate=cost_gate,
                status="waiting_data" if waiting else "fail" if failures else "feasible",
                waiting_reasons=sorted(set(waiting)), fail_reasons=sorted(set(failures)))


RULES = {
    "version": "t121-offline-v1", "cutoff_exclusive": "2016-05-01T00:00:00",
    "families": {"F1": ["H1", 1, 24], "F2": ["H1", 2, 24],
                 "F3": ["H4", 1, 12], "F4": ["H4", 2, 12]},
    "grids": {"XAUUSD": 5., "EURUSD": .005}, "tp_r": 2,
    "feature_warmup": 2000, "approach": [24, 1., .25],
    "actions": [.25, 6, .25], "max_gap_days": 30,
    "s3_bars": {"H1": 480, "H4": 120}, "n_required": 4521,
    "cost_limit_r": .02, "delta_ev_r": .06, "design_effect": 1.5,
}


def input_payload(h1, tags, gaps):
    """Canonical explicit input; no paths, implicit reads or source discovery."""
    b = h1.copy()
    b["time"] = _times(b["time"]).map(lambda t: t.isoformat())
    return {"bars": b.to_dict("records"), "source_tags": list(tags),
            "gap_spans": [[_utc(s).isoformat(), _utc(e).isoformat()] for s, e in gaps]}


def _input_reasons(h1, tags, gaps, evidence):
    if not isinstance(evidence, dict):
        return ["missing_input_evidence"]
    if evidence.get("kind") != "synthetic":
        return ["production_input_not_released"]
    reasons = []
    if evidence.get("purpose") != "T121-count-cost":
        reasons.append("input_purpose")
    if tags is None or len(tags) != len(h1):
        return reasons + ["source_tags_unavailable"]
    if gaps is None:
        return reasons + ["gaps_unavailable"]
    try:
        payload = input_payload(h1, tags, gaps)
        if evidence.get("payload_sha256") != payload_sha256(payload):
            reasons.append("input_hash")
        ts = _times(h1["time"])
        start, end = _utc(evidence["range_start"]), _utc(evidence["range_end"])
        if (pd.isna(start) or pd.isna(end) or start >= end or end > CUTOFF or
                (len(ts) and (start > ts.min() or end < ts.max() + pd.Timedelta(hours=1)))):
            reasons.append("input_range")
        manifest = evidence["source_manifest"]
        if not (set(ts.dt.year) <= set(manifest["used_years"]) and
                isinstance(manifest["excluded_months"], list) and manifest["sources"]):
            reasons.append("input_manifest")
        if set(ts.dt.strftime("%Y-%m")) & set(manifest["excluded_months"]):
            reasons.append("excluded_month_present")
        for tag in tags:
            if tag == "mt5" and "mt5" not in manifest["sources"]:
                reasons.append("input_manifest_source")
            elif isinstance(tag, str) and tag.startswith("ext:HistData:"):
                if "ext:HistData" not in manifest["sources"]:
                    reasons.append("input_manifest_source")
        for s, e in gaps:
            if not start <= _utc(s) < _utc(e) < end:
                reasons.append("input_gap_range")
    except (KeyError, ValueError, TypeError, AttributeError):
        reasons.append("input_identity_incomplete")
    return sorted(set(reasons))


def _input_receipt(h1, tags, gaps, evidence):
    segments = []
    for t, tag in zip(_times(h1["time"]), tags):
        source = _source(tag) or "unknown"
        if segments and segments[-1]["source"] == source:
            segments[-1]["end"] = t.isoformat()
        else:
            segments.append({"source": source, "start": t.isoformat(), "end": t.isoformat()})
    return {"payload_sha256": evidence["payload_sha256"],
            "manifest_sha256": payload_sha256(evidence["source_manifest"]),
            "range_start": evidence["range_start"], "range_end": evidence["range_end"],
            "source_segments": segments,
            "excluded_months": evidence["source_manifest"]["excluded_months"],
            "gap_spans": [[_utc(s).isoformat(), _utc(e).isoformat()] for s, e in gaps]}


def run_feasibility(h1_by_symbol, *, source_tags, gap_spans, sessions,
                    input_evidence, cost_evidence):
    """Produce all 96 hypotheses and 8 family/symbol summaries in memory.

    Only explicit synthetic input is executable in this version. All production
    inputs return uncounted waiting_data regardless of caller approval claims.
    This is an engineering artifact, never a T121 execution authorization.
    """
    if sessions != SessionPolicy(exempt_symbols=frozenset({"BTCUSD"})):
        raise ValueError("T121 requires the frozen sessions calendar")
    rows, receipts = [], {}
    for symbol, grid in RULES["grids"].items():
        h1 = h1_by_symbol.get(symbol)
        tags, gaps = source_tags.get(symbol), gap_spans.get(symbol)
        evidence = input_evidence.get(symbol)
        unavailable = (["missing_input"] if h1 is None else
                       _input_reasons(h1, tags, gaps, evidence))
        prepared = {}
        if not unavailable:
            try:
                clean, _, _ = prepare_bars(h1, tags, "H1")
                normalized_gaps = _normalize_gap_spans(gaps)
                receipts[symbol] = _input_receipt(h1, tags, normalized_gaps, evidence)
                # Unknown source anywhere may affect signals through a lookback.
                # Preserve all trigger counts and mark the entire symbol as waiting.
                source_wait = ["source_unknown"] if any(_source(t) is None for t in tags) else []
                for tf, k, per_day in (("H1", 24, 24), ("H4", 12, 6)):
                    b, src, pre_excluded = prepare_bars(h1, tags, tf)
                    atr = atr_series(b)
                    s2, starts = s2_levels(clean, b.time)
                    for sit in ("S1", "S2", "S3"):
                        raw = count_triggers(
                            b, symbol=symbol, situation=sit, grid_step=grid,
                            approach=Approach(24, 1., .25), action_params=ActionParams(.25, 6),
                            atr=atr, feature_warmup=0, hold_bars=0, gap_spans=[],
                            bars_per_day=per_day, s3_bars=20 * per_day,
                            levels_by_bar=s2 if sit == "S2" else None)
                        for action in ("A1", "A2", "A3", "A4"):
                            selected = raw[raw.action == action]
                            qualified, dropped = qualify_triggers(
                                selected, b[["time"]], symbol=symbol, situation=sit, hold_bars=k,
                                gap_spans=normalized_gaps, sessions=sessions, s3_bars=20 * per_day,
                                situation_starts=starts if sit == "S2" else None)
                            prepared[tf, sit, action] = (b, src, qualified,
                                {**pre_excluded, **dropped}, source_wait)
            except (ValueError, KeyError, TypeError):
                unavailable = ["invalid_input"]
        for family, (tf, sl, k) in RULES["families"].items():
            for sit in ("S1", "S2", "S3"):
                for action in ("A1", "A2", "A3", "A4"):
                    excluded = {}
                    if unavailable:
                        summary = summarize_hypothesis(None, [], waiting_reasons=unavailable)
                    else:
                        b, src, qualified, excluded, source_wait = prepared[tf, sit, action]
                        costs = []
                        for t in qualified.itertuples(index=False):
                            i = int(t.bar)
                            costs.append(trigger_cost(
                                symbol=symbol, signal_time=t.time, entry_time=b.time.iloc[i + 1],
                                exit_time=b.time.iloc[i + k + 1], side=int(t.side), source=src[i],
                                signal_price=float(t.close), atr=float(t.atr), sl_atr=sl,
                                evidence=cost_evidence.get(symbol)))
                        summary = summarize_hypothesis(qualified.time, costs, waiting_reasons=source_wait)
                    rows.append(dict(family=family, symbol=symbol, timeframe=tf,
                        sl_atr=sl, tp_r=2, hold_bars=k, situation=sit, action=action,
                        hypothesis="follow>base" if action in ("A1", "A2") else "fade>base",
                        exclusions=excluded, **summary))
    summaries = []
    for family in RULES["families"]:
        for symbol in RULES["grids"]:
            selected = [r for r in rows if r["family"] == family and r["symbol"] == symbol]
            states = {r["status"] for r in selected}
            summaries.append(dict(family=family, symbol=symbol,
                status="feasible" if "feasible" in states else "waiting_data" if "waiting_data" in states else "fail",
                feasible_rows=sum(r["status"] == "feasible" for r in selected),
                waiting_reasons=sorted({v for r in selected for v in r["waiting_reasons"]}),
                fail_reasons=sorted({v for r in selected for v in r["fail_reasons"]})))
    return {"artifact_kind": "synthetic-engineering-only", "production_execution": "not_released",
            "rules": RULES, "qualification_sha256": payload_sha256(RULES),
            "inputs": receipts, "rows": rows, "summaries": summaries}
