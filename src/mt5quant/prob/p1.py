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
from mt5quant.prob.gaps import suppression_mask
from mt5quant.prob.labels import Bracket, label_triggers
from mt5quant.strategies.library.prob.actions import (
    ActionParams, action_triggers,
)
from mt5quant.prob.counting import level_rows_for_situation
from mt5quant.prob.scenarios import ScenarioCosts
from mt5quant.strategies.library.prob.features import atr_pct_2000, trend_sma200
from mt5quant.strategies.library.prob.situations import Approach, find_level_events
from mt5quant.validation.prob_mode import delta_p_test, randomize_triggers


def hypothesis_seed(card_id: str, hypothesis: str) -> int:
    """确定性随机化种子：sha256(卡|假设) 前 8 字节（记录进 params）。"""
    digest = hashlib.sha256(f"{card_id}|{hypothesis}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def card_triggers(card, bars_by_symbol: dict, *, approach: Approach,
                  action_params: ActionParams,
                  gap_spans_by_symbol: dict | None = None) -> pd.DataFrame:
    """卡的全部（局面×动作）触发（不看结果），带特征列。

    gap_spans_by_symbol（T97 波次 2，可选）：品种 → 合并序列洞集合；
    给出的品种按先决 4 抑制回看窗跨洞的触发（缺省 None 行为不变）。
    """
    parts = []
    for sym, bars in bars_by_symbol.items():
        atr = atr_series(bars)
        sma = trend_sma200(bars).to_numpy()
        pct = atr_pct_2000(atr).to_numpy()
        gaps = (gap_spans_by_symbol or {}).get(sym)
        for sit in card.prob["situations"]:
            grid = (card.prob.get("level_grid") or {}).get(sym)                 if sit == "S1" else None
            levels = level_rows_for_situation(bars, sit, grid_step=grid,
                                              approach=approach, atr=atr)
            events = find_level_events(bars, levels, situation=sit,
                                       approach=approach, atr=atr)
            trig = action_triggers(bars, events, params=action_params, atr=atr)
            if gaps and len(trig):
                keep = suppression_mask(trig["bar"], bars, gaps, sit,
                                        approach.lookback_bars)
                trig = trig.iloc[[k for k, ok in enumerate(keep) if ok]]
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
                *, sizes: dict, policy_by_symbol: dict,
                sims: int = 400, gap_spans_by_symbol: dict | None = None,
                mt5_first_by_symbol: dict | None = None) -> list[dict]:
    """一张卡的全部假设一次跑完（不能只跑几条）。

    每条假设返回 dict：{hypothesis, seed, n_triggers, n_trigger_days,
    real_win_rate, null_win_rate, delta_p, p_value, prob_version}。
    只读发现段（调用方保证 bars 的 time < split）；标签 train_end=split；
    禁入窗与 bt 同一来源（policy_by_symbol[sym]——审核三 S8/E6：蓝图
    §2.3 标签与引擎同口径，**必填**，缺品种 → ValueError，不按"不限制"
    处理）；sizes 缺品种 → ValueError（不默认 1.0）。

    T97 波次 2（可选参数，缺省 None 行为逐位不变）：
    - gap_spans_by_symbol：品种 → 合并序列洞集合，card_triggers 按
      先决 4 抑制回看窗跨洞的触发；
    - mt5_first_by_symbol：品种 → MT5 段首 bar 时刻（era 判定）。卡带
      [prob].cost_scenario_mult 时触发按 era 分组标注：ext 组（触发
      时刻 < mt5_first）用 ScenarioCosts(costs, 保守档 mult[1])、MT5 组
      原 costs，结果拼接；卡不带该键或该品种 mt5_first 缺失 → 全组
      原 costs（旧卡/探针零变化）。
    """
    bracket = Bracket(sl_atr=card.prob["bracket"]["sl_atr"],
                      tp_atr=card.prob["bracket"]["tp_atr"],
                      horizon_bars=card.prob["bracket"]["horizon_bars"])
    approach = Approach(**card.prob["approach"])
    aparams = ActionParams(breakout_atr=card.prob["breakout_atr"],
                           retest_window_bars=card.prob["retest_window_bars"])
    split = pd.Timestamp((round_obj.segments or {})["split"])
    triggers = card_triggers(card, bars_by_symbol, approach=approach,
                             action_params=aparams,
                             gap_spans_by_symbol=gap_spans_by_symbol)
    # T97：卡声明了情景倍数才分 era 标注（缺省 None → 全组原 costs）
    csm = (card.prob or {}).get("cost_scenario_mult")
    scenario_mult = float(csm[1]) if csm is not None else None
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
            if sym not in (sizes or {}):
                raise ValueError(
                    f"{sym} 的合约大小未知（sizes 缺失）——标签引擎须与"
                    "评估同口径，不默认 1.0（审核三 S8）")
            policy = policy_by_symbol.get(sym)
            if policy is None:
                raise ValueError(
                    f"{sym} 缺入场政策（policy_by_symbol）——标签引擎须与"
                    " bt 同一 entry_policy_for（审核四 E6；蓝图 §2.3），"
                    "不按'不限制'处理")
            mt5_first = (mt5_first_by_symbol or {}).get(sym)
            first = pd.Timestamp(mt5_first) if mt5_first is not None else None
            kw = dict(symbol=sym, timeframe=card.timeframe, bracket=bracket,
                      contract_size=sizes[sym],
                      volume_lots=1.0, train_end=split,
                      session_policy=policy)

            def _label(trig_df, *, _costs=costs, _first=first):
                """按 era 分组标注（T97）：ext 组 ScenarioCosts、MT5 组
                原 costs；未启用情景（scenario_mult/first 缺）→ 单组。"""
                if scenario_mult is None or _first is None or len(trig_df) == 0:
                    return label_triggers(bars, trig_df, _costs, **kw)
                t = pd.to_datetime(trig_df["time"])
                ext = trig_df[t < _first]
                mt5 = trig_df[t >= _first]
                pieces = []
                if len(ext):
                    pieces.append(label_triggers(
                        bars, ext, ScenarioCosts(_costs, scenario_mult), **kw))
                if len(mt5):
                    pieces.append(label_triggers(bars, mt5, _costs, **kw))
                return pd.concat(pieces, ignore_index=True)

            real = _label(sub_sym.drop(columns=["symbol"]))
            rnd = randomize_triggers(sub_sym.drop(columns=["symbol"]), bars,
                                     seed=seed)
            null = _label(rnd)
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
