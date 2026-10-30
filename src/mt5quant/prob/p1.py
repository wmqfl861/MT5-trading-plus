"""P1 运行编排（T90，蓝图 P 线 §3.2）：真实触发 vs 随机化触发的 Δp 检验。

`bt prob test`（scripts/bt.py）按卡逐条假设执行：真实触发 → 标签；
随机化触发（validation.prob_mode.randomize_triggers，种子由卡号与假设
确定性生成）→ 标签；delta_p_test（日聚合、月内置换）→ 单侧 p 值。
数据只读 time < split（发现段）；标签的 train_end = split（越过分界的
触发按 bar 下标剔除，D52）。评估代码（D45：研究角色只读）。

T98 audit P-09：真实触发与随机对照走**同一共同可用集合**（gaps.
eligibility_mask：局面回看窗——A2/A4 从 anchor 根起算、特征预热 2000
根、持有窗不跨洞）；randomize_triggers 显式收该集合（eligible_bars）。
报告口径（audit P-11）：n_total/n_usable/n_skipped 三报（n_triggers 为
n_total 的兼容别名），real/null 赢率与触发日数排除 skipped（与
delta_p_test 的 usable 同口径）。

T104 批 2（audit2 P2-11）：返回行补齐**资格剔除诊断**——n_eligible_dropped
（进入标签前的剔除量）与 eligible_dropped_by_reason（逐原因；现按
gaps.eligibility_mask 同三条件独立重算，TODO：甲的 gaps.EligibilityContext
.exclusion_reasons 就绪后改为消费其逐原因记录）+ null 侧同口径计数
（n_null_total/n_null_usable/n_null_skipped/n_null_trigger_days/
n_null_eligible_dropped）。台账/轮次报告写入由丁接线（bt.py）。

**真实局面触发上的运行属 T91，须等 T77 轮次草案获 P 线审核会话批准**
（蓝图 v4 §2）；本模块的测试只用合成数据。
"""
from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd

from mt5quant.backtest.engine_a import atr_series
from mt5quant.prob.gaps import eligibility_mask
from mt5quant.prob.labels import Bracket, label_triggers
from mt5quant.strategies.library.prob.actions import (
    ActionParams, action_triggers,
)
from mt5quant.prob.counting import level_rows_for_situation
from mt5quant.prob.scenarios import ScenarioCosts
from mt5quant.strategies.library.prob.features import (
    ATR_RANK_WINDOW, atr_pct_2000, trend_sma200,
)
from mt5quant.strategies.library.prob.situations import Approach, find_level_events
from mt5quant.validation.prob_mode import delta_p_test, randomize_triggers

# P-11（S7/T72 同口径）：skipped（未成交/截尾）只计数不进概率/赢率
_USABLE_OUTCOMES = frozenset({"win", "loss", "time"})


def hypothesis_seed(card_id: str, hypothesis: str) -> int:
    """确定性随机化种子：sha256(卡|假设) 前 8 字节（记录进 params）。"""
    digest = hashlib.sha256(f"{card_id}|{hypothesis}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def _filter_eligible(trig, bars, gaps, situation, approach_lookback,
                     feature_warmup, hold_bars, max_gap_days):
    """触发表 → 共同可用集合过滤（P-09；三元组全缺省时不滤）。"""
    if len(trig) and (gaps or feature_warmup is not None or hold_bars):
        keep = eligibility_mask(
            trig, bars, gaps or [], situation, approach_lookback,
            feature_warmup=(0 if feature_warmup is None
                            else int(feature_warmup)),
            hold_bars=int(hold_bars), max_gap_days=max_gap_days)
        return trig.iloc[[k for k, ok in enumerate(keep) if ok]]
    return trig


# T104 批 2（audit2 P2-11）：逐原因剔除键——与 gaps.eligibility_mask 的
# 三条件对应，命名对齐甲的 gaps.EligibilityContext.exclusion_reasons 词表
# （其 "future_window" = 持有/未来窗不满足——跨洞或末端 K+1 不可观察）。
# TODO(甲/丁接线)：EligibilityContext 逐原因记录就绪后改为消费其产出，
# 本重算退役（届时 "lookback_crosses_gap" 并入其局面窗 reason）。
_ELIGIBILITY_REASONS = ("lookback_crosses_gap", "feature_warmup",
                        "future_window")


def _eligibility_drop_reasons(trig, bars, gaps, situation, approach_lookback,
                              feature_warmup, hold_bars, max_gap_days
                              ) -> list[dict | None]:
    """被共同可用集合剔除的行 → 逐原因计数（P2-11；与 _filter_eligible /
    gaps.eligibility_mask 同三条件独立重算，返回与 trig 等长的
    {原因: 1} 字典列表；合格行为 None）。一行可同时违反多个条件（计数
    非互斥——审计口径：逐原因剔除量，不为凑互斥而掩盖复合违例）。"""
    fw = 0 if feature_warmup is None else int(feature_warmup)
    if not len(trig) or not (gaps or fw or hold_bars):
        return [None] * len(trig)
    kw = dict(gap_spans=gaps or [], situation=situation,
              approach_lookback=approach_lookback, max_gap_days=max_gap_days)
    full = eligibility_mask(trig, bars, feature_warmup=fw,
                            hold_bars=int(hold_bars), **kw)
    lb_only = eligibility_mask(trig, bars, feature_warmup=0, hold_bars=0,
                               **kw)
    with_hold = (eligibility_mask(trig, bars, feature_warmup=0,
                                  hold_bars=int(hold_bars), **kw)
                 if hold_bars else lb_only)
    bars_col = trig["bar"].astype(int).to_numpy()
    out: list[dict | None] = []
    for k, ok in enumerate(full):
        if ok:
            out.append(None)
            continue
        reasons: dict[str, int] = {}
        if not lb_only[k]:
            reasons["lookback_crosses_gap"] = 1     # 局面回看窗跨洞（先决 4）
        if fw and int(bars_col[k]) < fw:
            reasons["feature_warmup"] = 1           # 特征预热不足（2000 根）
        if hold_bars and lb_only[k] and not with_hold[k]:
            reasons["future_window"] = 1            # 持有/未来窗不满足（跨洞
            # 或末端 K+1 不可观察——词表对齐甲的 EligibilityContext）
        out.append(reasons or {"unknown": 1})
    return out


def _record_eligibility_drops(drop_stats, trig, sym, sit, bars, gaps,
                              approach_lookback, feature_warmup, hold_bars,
                              max_gap_days) -> None:
    """把 _eligibility_drop_reasons 聚到 drop_stats[(sym, sit, action)]：
    {"n": 剔除行数, "reasons": {原因: 行数}}（P2-11；card_triggers 在过滤
    **前**记录——进入标签前的逐原因剔除量）。"""
    why = _eligibility_drop_reasons(
        trig, bars, gaps, sit, approach_lookback, feature_warmup,
        hold_bars, max_gap_days)
    for k, reasons in enumerate(why):
        if reasons is None:
            continue
        act = str(trig["action"].iloc[k])
        rec = drop_stats.setdefault((sym, sit, act),
                                    {"n": 0, "reasons": {}})
        rec["n"] += 1
        for r in reasons:
            rec["reasons"][r] = rec["reasons"].get(r, 0) + 1


def card_triggers(card, bars_by_symbol: dict, *, approach: Approach,
                  action_params: ActionParams,
                  gap_spans_by_symbol: dict | None = None,
                  feature_warmup: int | None = None, hold_bars: int = 0,
                  max_gap_days: int = 30,
                  drop_stats: dict | None = None) -> pd.DataFrame:
    """卡的全部（局面×动作）触发（不看结果），带特征列。

    gap_spans_by_symbol（T97 波次 2，可选）：品种 → 合并序列洞集合；
    给出的品种按先决 4 抑制回看窗跨洞的触发（缺省 None 行为不变）。

    P-09 共同可用集合（T98，可选三元组，缺省不启用）：feature_warmup /
    hold_bars / max_gap_days 透传 gaps.eligibility_mask（A2/A4 的局面窗
    从 anchor 根起算）。

    drop_stats（T104 批 2 / audit2 P2-11，可选 dict）：给出时按
    (symbol, situation, action) 记录**进入标签前**的资格剔除——
    {"n": 剔除行数, "reasons": {原因: 行数}}；不影响返回值（过滤仍在
    eligibility_mask 同一口径下进行——TODO：甲的 EligibilityContext
    逐原因记录就绪后改为消费其产出）。
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
            if drop_stats is not None and len(trig):
                _record_eligibility_drops(
                    drop_stats, trig, sym, sit, bars, gaps,
                    approach.lookback_bars, feature_warmup, hold_bars,
                    max_gap_days)
            trig = _filter_eligible(trig, bars, gaps, sit,
                                    approach.lookback_bars, feature_warmup,
                                    hold_bars, max_gap_days)
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


def _eligible_pool(bars, gaps, situation, approach_lookback, feature_warmup,
                   hold_bars, max_gap_days) -> np.ndarray:
    """共同可用集合的**抽样池**（P-09）：逐 bar 以自身为 origin 评估
    eligibility_mask（随机时刻没有 anchor——桶键 小时/星期/ATR 档 在
    合格 bar 上才可匹配）。调用方再把合格触发自身的 bar 并入池。"""
    pseudo = pd.DataFrame({"action": "A1",               # 非 A2/A4 → origin=bar
                           "anchor": np.arange(len(bars)),
                           "bar": np.arange(len(bars))})
    keep = eligibility_mask(
        pseudo, bars, gaps or [], situation, approach_lookback,
        feature_warmup=int(feature_warmup), hold_bars=int(hold_bars),
        max_gap_days=max_gap_days)
    return np.flatnonzero(np.asarray(keep, dtype=bool))


def run_p1_card(card, round_obj, bars_by_symbol: dict, costs_by_symbol: dict,
                *, sizes: dict, policy_by_symbol: dict,
                sims: int = 400, gap_spans_by_symbol: dict | None = None,
                mt5_first_by_symbol: dict | None = None,
                feature_warmup: int | None = ATR_RANK_WINDOW,
                hold_bars: int | None = None,
                max_gap_days: int = 30) -> list[dict]:
    """一张卡的全部假设一次跑完（不能只跑几条）。

    每条假设返回 dict：{hypothesis, seed, n_total, n_usable, n_skipped,
    n_triggers（= n_total 的兼容别名）, n_trigger_days, real_win_rate,
    null_win_rate, delta_p, p_value, prob_version}——赢率/日数按可成交
    行（win/loss/time）计，skipped 只计数（P-11，与 delta_p_test 同
    口径）。

    T104 批 2（audit2 P2-11）增补诊断键（台账写入由丁接线）：
    - n_eligible_dropped / eligible_dropped_by_reason：进入标签**前**的
      资格剔除量与逐原因计数（lookback_crosses_gap / feature_warmup /
      future_window——与 eligibility_mask 同三条件独立重算；
      TODO：甲的 gaps.EligibilityContext.exclusion_reasons 就绪后改为
      消费其逐原因记录）；分母口径：n_total 是过滤后的标签输入，
      剔除量单独披露（n_total + n_eligible_dropped = 过滤前触发数）；
    - n_null_total / n_null_usable / n_null_skipped / n_null_trigger_days
      / n_null_eligible_dropped：null 侧同口径计数（随机对照从共同
      可用集合抽样 → 资格剔除结构性为 0，如实记录）。

    只读发现段（调用方保证 bars 的 time < split）；标签
    train_end=split；禁入窗与 bt 同一来源（policy_by_symbol[sym]——
    审核三 S8/E6：蓝图 §2.3 标签与引擎同口径，**必填**，缺品种 →
    ValueError，不按"不限制"处理）；sizes 缺品种 → ValueError（不默认
    1.0）。

    T97 波次 2（可选参数）：
    - gap_spans_by_symbol：品种 → 合并序列洞集合，card_triggers 按
      先决 4 抑制回看窗跨洞的触发；
    - mt5_first_by_symbol：品种 → MT5 段首 bar 时刻（era 判定，可用
      gaps.mt5_first_time(bars=…, source_tags=…) 从 merge 实测）。卡带
      [prob].cost_scenario_mult 时触发按 era 分组标注：ext 组（触发
      时刻 < mt5_first）用 ScenarioCosts(costs, 保守档 mult[1])、MT5 组
      原 costs，结果拼接；**卡带该键而边界不可得（缺品种/None/NaT）→
      ValueError，不静默 1×**（T98 audit P-09）；卡不带该键 → 全组原
      costs（旧卡/探针零变化）。

    T98 audit P-09（共同可用集合）：
    - feature_warmup：真实触发与随机抽样池共用的预热下界（缺省 2000
      = atr_pct_2000 排名窗；显式 None/0 关闭——合成小序列测试用）；
    - hold_bars：持有窗不跨洞的根数（缺省 None = 卡 bracket 的
      horizon_bars——标签持有窗须未来可观察）；
    - max_gap_days：持有窗的洞宽阈值（缺省 30，与 merge 口径一致；
      回看窗按先决 4 用 gap_spans 原样）。
    randomize_triggers 显式收合格 bar 集（eligible_bars）：池 = 逐 bar
    评估的合格集 ∪ 合格触发自身的 bar（后者保证桶匹配不因池限制退化
    拒绝——A2/A4 的局面窗按 anchor 合格时其触发根未必在逐 bar 合格集
    里，而触发自身必在自身桶中）。
    """
    bracket = Bracket(sl_atr=card.prob["bracket"]["sl_atr"],
                      tp_atr=card.prob["bracket"]["tp_atr"],
                      horizon_bars=card.prob["bracket"]["horizon_bars"])
    approach = Approach(**card.prob["approach"])
    aparams = ActionParams(breakout_atr=card.prob["breakout_atr"],
                           retest_window_bars=card.prob["retest_window_bars"])
    split = pd.Timestamp((round_obj.segments or {})["split"])
    eff_warmup = 0 if feature_warmup is None else int(feature_warmup)
    eff_hold = (int(card.prob["bracket"]["horizon_bars"])
                if hold_bars is None else int(hold_bars))
    drop_stats: dict[tuple[str, str, str], dict] = {}   # P2-11：逐原因剔除
    triggers = card_triggers(card, bars_by_symbol, approach=approach,
                             action_params=aparams,
                             gap_spans_by_symbol=gap_spans_by_symbol,
                             feature_warmup=feature_warmup,
                             hold_bars=eff_hold, max_gap_days=max_gap_days,
                             drop_stats=drop_stats)
    # T97：卡声明了情景倍数才分 era 标注（缺省 None → 全组原 costs）
    csm = (card.prob or {}).get("cost_scenario_mult")
    scenario_mult = float(csm[1]) if csm is not None else None
    if csm is not None:
        missing = [sym for sym in bars_by_symbol
                   if (mt5_first_by_symbol or {}).get(sym) is None
                   or pd.isna((mt5_first_by_symbol or {}).get(sym))]
        if missing:
            raise ValueError(
                f"卡声明了 cost_scenario_mult 但 {missing} 的 era 边界不可得"
                "（mt5_first_by_symbol 缺该品种/None/NaT）——ext 段按情景档"
                "计费需要 merge 实测的 MT5 段首 bar（gaps.mt5_first_time 可收"
                " merged_research_series 的 (bars, source_tags)），不静默按"
                " 1× 处理（audit P-09）")
    pool_cache: dict[tuple[str, str], np.ndarray] = {}
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
            # P-09：随机对照从共同可用集合抽样（合格触发自身 bar 并入池）
            key = (sym, sit)
            if key not in pool_cache:
                pool_cache[key] = _eligible_pool(
                    bars, (gap_spans_by_symbol or {}).get(sym), sit,
                    approach.lookback_bars, eff_warmup, eff_hold,
                    max_gap_days)
            pool = np.union1d(pool_cache[key],
                              sub_sym["bar"].astype(int).to_numpy())
            rnd = randomize_triggers(sub_sym.drop(columns=["symbol"]), bars,
                                     seed=seed, eligible_bars=pool)
            null = _label(rnd)
            real_parts.append(real)
            null_parts.append(null)
        real_all = pd.concat(real_parts, ignore_index=True) if real_parts \
            else pd.DataFrame(columns=["time", "outcome"])
        null_all = pd.concat(null_parts, ignore_index=True) if null_parts \
            else pd.DataFrame(columns=["time", "outcome"])
        test = delta_p_test(real_all, null_all, sims=sims,
                            seed=seed % (2**31))
        n_total = int(len(real_all))
        if n_total:
            usable = real_all["outcome"].isin(_USABLE_OUTCOMES)
            n_usable = int(usable.sum())
            n_skipped = int((real_all["outcome"] == "skipped").sum())
            days = int(pd.to_datetime(
                real_all.loc[usable, "time"]).dt.date.nunique())
        else:
            n_usable, n_skipped, days = 0, 0, 0
        # ---- P2-11：资格剔除诊断（real 侧，进入标签前；TODO：甲的
        # gaps.EligibilityContext 逐原因记录就绪后替换 card_triggers 的
        # 独立重算，此处聚合口径不变）----
        n_dropped = 0
        dropped_reasons: dict[str, int] = {}
        for sym in bars_by_symbol:
            rec = drop_stats.get((sym, sit, act))
            if not rec:
                continue
            n_dropped += int(rec["n"])
            for r, m in rec["reasons"].items():
                dropped_reasons[r] = dropped_reasons.get(r, 0) + int(m)
        # ---- P2-11：null 侧同口径计数（随机对照从共同可用集合抽样 →
        # 资格剔除结构性为 0，同口径如实记录）----
        n_null_total = int(len(null_all))
        if n_null_total:
            usable_n = null_all["outcome"].isin(_USABLE_OUTCOMES)
            n_null_usable = int(usable_n.sum())
            n_null_skipped = int((null_all["outcome"] == "skipped").sum())
            null_days = int(pd.to_datetime(
                null_all.loc[usable_n, "time"]).dt.date.nunique())
        else:
            n_null_usable, n_null_skipped, null_days = 0, 0, 0
        results.append({
            "hypothesis": hyp,
            "seed": seed,
            "n_total": n_total,
            "n_usable": n_usable,
            "n_skipped": n_skipped,
            "n_triggers": n_total,     # P-11：兼容别名（bt/探针读它）
            "n_trigger_days": days,
            # P2-11：进入标签前的资格剔除（逐原因；与 n_total 的分母
            # 不同——n_total 是过滤后的标签输入，剔除量单独披露）
            "n_eligible_dropped": n_dropped,
            "eligible_dropped_by_reason": dropped_reasons,
            # P2-11：null 侧同口径（usable/skipped/日数排除 skipped；
            # 剔除恒 0——池已按共同可用集合预滤）
            "n_null_total": n_null_total,
            "n_null_usable": n_null_usable,
            "n_null_skipped": n_null_skipped,
            "n_null_trigger_days": null_days,
            "n_null_eligible_dropped": 0,
            "real_win_rate": _day_winrate(real_all),
            "null_win_rate": _day_winrate(null_all),
            "delta_p": test["delta_p"],
            "p_value": test["p_value"],
            "prob_version": "1.0.0",
        })
    return results


def _day_winrate(labels: pd.DataFrame) -> float:
    """日赢率均值——只按可成交行（win/loss/time）计，skipped 不进概率
    （P-11：与 delta_p_test 的 usable 同口径；全 skipped → NaN）。"""
    if len(labels) == 0:
        return float("nan")
    d = labels[labels["outcome"].isin(_USABLE_OUTCOMES)]
    if len(d) == 0:
        return float("nan")
    t = pd.to_datetime(d["time"])
    win = (d["outcome"] == "win").astype(float)
    return float(win.groupby(t.dt.date).mean().mean())
