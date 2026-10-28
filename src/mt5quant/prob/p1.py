"""P1 运行编排（T90，蓝图 P 线 §3.2）：真实触发 vs 随机化触发的 Δp 检验。

`bt prob test`（scripts/bt.py）按卡逐条假设执行：真实触发 → 标签；
随机化触发（validation.prob_mode.randomize_triggers，种子由卡号与假设
确定性生成）→ 标签；delta_p_test（日聚合、月内置换）→ 单侧 p 值。
数据只读 time < split（发现段）；标签的 train_end = split（越过分界的
触发按 bar 下标剔除，D52）。评估代码（D45：研究角色只读）。

**真实局面触发上的运行属 T91，须等 T77 轮次草案获 P 线审核会话批准**
（蓝图 v4 §2）；本模块的测试只用合成数据。
"""
from __future__ import annotations

import hashlib

import pandas as pd

from mt5quant.backtest.engine_a import atr_series
from mt5quant.prob.labels import Bracket, label_triggers
from mt5quant.strategies.library.prob.actions import (
    ActionParams, action_triggers,
)
from mt5quant.prob.counting import level_rows_for_situation
from mt5quant.strategies.library.prob.features import atr_pct_2000, trend_sma200
from mt5quant.strategies.library.prob.situations import Approach, find_level_events
from mt5quant.validation.prob_mode import delta_p_test, randomize_triggers


def hypothesis_seed(card_id: str, hypothesis: str) -> int:
    """确定性随机化种子：sha256(卡|假设) 前 8 字节（记录进 params）。"""
    digest = hashlib.sha256(f"{card_id}|{hypothesis}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def card_triggers(card, bars_by_symbol: dict, *, approach: Approach,
                  action_params: ActionParams) -> pd.DataFrame:
    """卡的全部（局面×动作）触发（不看结果），带特征列。"""
    parts = []
    for sym, bars in bars_by_symbol.items():
        atr = atr_series(bars)
        sma = trend_sma200(bars).to_numpy()
        pct = atr_pct_2000(atr).to_numpy()
        for sit in card.prob["situations"]:
            grid = (card.prob.get("level_grid") or {}).get(sym)                 if sit == "S1" else None
            levels = level_rows_for_situation(bars, sit, grid_step=grid,
                                              approach=approach, atr=atr)
            events = find_level_events(bars, levels, situation=sit,
                                       approach=approach, atr=atr)
            trig = action_triggers(bars, events, params=action_params, atr=atr)
            if len(trig):
                idx = trig["bar"].astype(int).to_numpy()
                trig = trig.assign(
                    symbol=sym,
                    trend_sma200=[sma[i] if sma[i] is not None else "na"
                                  for i in idx],
                    atr_pct_2000=[pct[i] if pct[i] is not None else "na"
                                  for i in idx])
                parts.append(trig)
    if not parts:
        return pd.DataFrame(columns=["time", "situation", "action", "side",
                                     "level", "kind", "anchor", "bar",
                                     "symbol", "trend_sma200", "atr_pct_2000"])
    return pd.concat(parts, ignore_index=True)


def run_p1_card(card, round_obj, bars_by_symbol: dict, costs_by_symbol: dict,
                *, sizes: dict, sims: int = 400) -> list[dict]:
    """一张卡的全部假设一次跑完（不能只跑几条）。

    每条假设返回 dict：{hypothesis, seed, n_triggers, n_trigger_days,
    real_win_rate, null_win_rate, delta_p, p_value, prob_version}。
    只读发现段（调用方保证 bars 的 time < split）；标签 train_end=split。
    """
    bracket = Bracket(sl_atr=card.prob["bracket"]["sl_atr"],
                      tp_atr=card.prob["bracket"]["tp_atr"],
                      horizon_bars=card.prob["bracket"]["horizon_bars"])
    approach = Approach(**card.prob["approach"])
    aparams = ActionParams(breakout_atr=card.prob["breakout_atr"],
                           retest_window_bars=card.prob["retest_window_bars"])
    split = pd.Timestamp((round_obj.segments or {})["split"])
    triggers = card_triggers(card, bars_by_symbol, approach=approach,
                             action_params=aparams)
    results = []
    for hyp in card.prob["hypotheses"]:
        sit, act = hyp.split(":")[0], hyp.split(":")[1]
        sub = triggers[(triggers["situation"] == sit)
                       & (triggers["action"] == act)]
        seed = hypothesis_seed(card.id, hyp)
        real_parts, null_parts = [], []
        for sym, bars in bars_by_symbol.items():
            sub_sym = sub[sub["symbol"] == sym]
            if len(sub_sym) == 0:
                continue
            costs = costs_by_symbol[sym]
            kw = dict(symbol=sym, timeframe=card.timeframe, bracket=bracket,
                      contract_size=(sizes or {}).get(sym, 1.0),
                      volume_lots=1.0, train_end=split)
            real = label_triggers(bars, sub_sym.drop(
                columns=["symbol"]), costs, **kw)
            rnd = randomize_triggers(sub_sym.drop(columns=["symbol"]), bars,
                                     seed=seed)
            null = label_triggers(bars, rnd, costs, **kw)
            real_parts.append(real)
            null_parts.append(null)
        real_all = pd.concat(real_parts, ignore_index=True) if real_parts \
            else pd.DataFrame(columns=["time", "outcome"])
        null_all = pd.concat(null_parts, ignore_index=True) if null_parts \
            else pd.DataFrame(columns=["time", "outcome"])
        test = delta_p_test(real_all, null_all, sims=sims,
                            seed=seed % (2**31))
        days = pd.to_datetime(real_all["time"]).dt.date.nunique() \
            if len(real_all) else 0
        results.append({
            "hypothesis": hyp,
            "seed": seed,
            "n_triggers": int(len(real_all)),
            "n_trigger_days": int(days),
            "real_win_rate": _day_winrate(real_all),
            "null_win_rate": _day_winrate(null_all),
            "delta_p": test["delta_p"],
            "p_value": test["p_value"],
            "prob_version": "1.0.0",
        })
    return results


def _day_winrate(labels: pd.DataFrame) -> float:
    if len(labels) == 0:
        return float("nan")
    t = pd.to_datetime(labels["time"])
    win = (labels["outcome"] == "win").astype(float)
    return float(win.groupby(t.dt.date).mean().mean())
