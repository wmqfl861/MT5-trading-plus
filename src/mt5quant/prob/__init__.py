"""Probability-line public API with lazy legacy exports.

The counting/feasibility entry can cold-import this package without importing
labels, probability tables, or the backtest engine. Legacy names retain their
module/object identity once explicitly requested.
"""
from __future__ import annotations

import importlib

_LAZY_EXPORTS = {
    "ACTION_ORDER": ("decision", "ACTION_ORDER"),
    "Bracket": ("labels", "Bracket"),
    "CellEstimate": ("tables", "CellEstimate"),
    "LOOKBACK_BARS": ("labels", "LOOKBACK_BARS"),
    "ModelIntegrityError": ("artifact", "ModelIntegrityError"),
    "ProbTable": ("tables", "ProbTable"),
    "choose": ("decision", "choose"),
    "ev_lower": ("decision", "ev_lower"),
    "label_triggers": ("labels", "label_triggers"),
    "load_model": ("artifact", "load_model"),
    "save_model": ("artifact", "save_model"),
}

__all__ = list(_LAZY_EXPORTS)


def __getattr__(name):
    try:
        module_name, attribute = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(importlib.import_module(f"{__name__}.{module_name}"), attribute)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
