"""决策策略 v1（T74 收尾，蓝图 P 线 §2.5 / 决策 D41/D44/D46）。

`LevelDecisionV1`：fittable 策略——fit 对每个（局面×动作）触发打首达标签
（mt5quant.prob.labels，引擎同口径）并拟合概率表（mt5quant.prob.tables）；
generate_signals 每根 bar 找触发、查表、按期望值下界决策（choose），入场后
信号保持方向 K 根然后归零（信号层持有上限），持仓期间的新触发忽略。
没装模型就调用 → ModelMissingError；模型文件哈希不符在装载层
（artifact.load_model）拒绝。每张卡绑定一个子类，止损止盈/局面/动作/网格
写成类常量（同 H003-v2 的子类做法）。ExitRules：sl/tp×ATR +
after_signal_change；不声明 time_stop_bars/week_close_exit（D44）。
本模块属策略库（D45：研究角色可写，计入代码指纹）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from mt5quant.backtest.engine_a import atr_series, run_backtest
from mt5quant.prob.artifact import _canonical_bytes
from mt5quant.prob.decision import choose, ev_lower
from mt5quant.prob.labels import Bracket, label_triggers
from mt5quant.prob.tables import ProbTable
from mt5quant.strategies import BaseStrategy, ExitRules, RequiredData
from mt5quant.strategies.library.prob.actions import (
    ActionParams, action_triggers,
)
from mt5quant.prob.counting import level_rows_for_situation
from mt5quant.strategies.library.prob.features import atr_pct_2000, trend_sma200
from mt5quant.strategies.library.prob.situations import (
    Approach, find_level_events,
)

import hashlib

PROB_VERSION = "1.0.0"
NY_TZ = "America/New_York"
FEATURES = ("trend_sma200", "atr_pct_2000")


class ModelMissingError(RuntimeError):
    """没装（冻结）模型就调用 generate_signals（fail-closed）。"""


@dataclass(frozen=True)
class DecisionPolicy:
    """决策阈值参数（D41/D46；类常量由卡的子类提供）。"""

    z: float = 1.0                # 概率下界的标准误倍数（lb_z）
    threshold_mult: float = 2.0   # EV_下界 ≥ threshold_mult × 来回成本(R)


class LevelDecisionV1(BaseStrategy):
    """价位决策 v1 基类：子类以类常量绑定一张卡的检验配置。"""

    fittable = True
    # ---- 卡绑定常量（子类覆写） ----
    SL_ATR: float = 1.0
    TP_ATR: float = 2.0
    HORIZON_BARS: int = 24                  # K（信号层持有上限）
    SITUATIONS: tuple[str, ...] = ("S1",)
    LEVEL_GRID: dict[str, float] | None = None   # S1 必填：品种→网格间距
    APPROACH: Approach = Approach(lookback_bars=24, min_distance_atr=1.0,
                                  touch_atr=0.25)
    BREAKOUT_ATR: float = 0.25
    RETEST_WINDOW_BARS: int = 6
    POLICY: DecisionPolicy = DecisionPolicy()
    SYMBOLS: tuple[str, ...] = ("XAUUSD",)
    TIMEFRAME: str = "H1"

    exit_rules = ExitRules(stop_loss_atr=SL_ATR, take_profit_atr=TP_ATR,
                           reentry="after_signal_change")

    def __init__(self):
        self._table: ProbTable | None = None
        self._cost_r: dict[tuple[str, str], float] = {}   # (situation,action)→成本R
        self._model_meta: dict = {}

    # ------------------------------------------------------------- 拟合

    def fit(self, bars_by_symbol: dict, costs_by_symbol: dict, params: dict,
            *, train_end, purge_bars: int) -> dict:
        meta = params.get("__fit__") or {}
        sizes = meta.get("sizes") or {}
        parts = []
        for sym, bars in bars_by_symbol.items():
            parts.append(self._labels_for_symbol(
                sym, bars, costs_by_symbol.get(sym),
                contract_size=sizes.get(sym, 1.0),
                train_end=train_end, purge_bars=purge_bars))
        labels = pd.concat([p for p in parts if len(p)],
                           ignore_index=True) if any(len(p) for p in parts) \
            else pd.DataFrame(columns=["situation", "action", "outcome",
                                       "net_r", *FEATURES])
        table = ProbTable.fit(labels, group_by=list(FEATURES),
                              prior_strength=100, min_cell=100)
        model = {
            "schema_version": 1,
            "prob_version": PROB_VERSION,
            "symbols": sorted(bars_by_symbol),
            "timeframe": self.TIMEFRAME,
            "fit_window": {
                "start": (min(pd.to_datetime(b["time"]).iloc[0]
                              for b in bars_by_symbol.values())
                          .isoformat() if bars_by_symbol else None),
                "end": (str(train_end) if train_end is not None
                        else max(pd.to_datetime(b["time"]).iloc[-1]
                                 for b in bars_by_symbol.values()).isoformat()),
            },
            "purge_bars": int(purge_bars),
            "bracket": {"sl_atr": self.SL_ATR, "tp_atr": self.TP_ATR,
                        "horizon_bars": self.HORIZON_BARS},
            "situations": list(self.SITUATIONS),
            "features": list(FEATURES),
            "policy": {"z": self.POLICY.z,
                       "threshold_mult": self.POLICY.threshold_mult},
            "cost_r_by_situation_action": {f"{s}|{a}": v
                                           for (s, a), v in
                                           self._cost_r.items()},
            "prob_table": table.to_dict(),
        }
        return model

    def _labels_for_symbol(self, sym, bars, costs, *, contract_size,
                           train_end, purge_bars) -> pd.DataFrame:
        atr = atr_series(bars)
        aparams = ActionParams(breakout_atr=self.BREAKOUT_ATR,
                               retest_window_bars=self.RETEST_WINDOW_BARS)
        rows = []
        for sit in self.SITUATIONS:
            levels = level_rows_for_situation(
                bars, sit,
                grid_step=(self.LEVEL_GRID or {}).get(sym)
                if sit == "S1" else None,
                approach=self.APPROACH, atr=atr)
            events = find_level_events(bars, levels, situation=sit,
                                       approach=self.APPROACH, atr=atr)
            trig = action_triggers(bars, events, params=aparams, atr=atr)
            if len(trig):
                rows.append(trig.assign(symbol=sym))
        if not rows:
            return pd.DataFrame(columns=["situation", "action", "outcome",
                                         "net_r", *FEATURES])
        triggers = pd.concat(rows, ignore_index=True)
        triggers["ny_hour"] = (pd.to_datetime(triggers["time"])
                               .dt.tz_localize("UTC")
                               .dt.tz_convert(NY_TZ).dt.hour.astype("int64"))
        closes = bars["close"].to_numpy(dtype="float64")
        triggers["close"] = [float(closes[int(b)])
                             for b in triggers["bar"]]
        # 特征列（触发 bar 处的有界窗口特征；None→"na" 进格键）
        sma = trend_sma200(bars)
        pct = atr_pct_2000(atr)
        bars_idx = triggers["bar"].astype(int).to_numpy()
        triggers["trend_sma200"] = [
            v if v is not None else "na"
            for v in sma.to_numpy()[bars_idx]]
        triggers["atr_pct_2000"] = [
            v if v is not None else "na"
            for v in pct.to_numpy()[bars_idx]]
        bracket = Bracket(sl_atr=self.SL_ATR, tp_atr=self.TP_ATR,
                          horizon_bars=self.HORIZON_BARS)
        labeled = label_triggers(
            bars, triggers, costs, symbol=sym, timeframe=self.TIMEFRAME,
            bracket=bracket, contract_size=contract_size,
            sizing=None, vol_bounds=(0.01, 0.01, 100.0),
            volume_lots=1.0, train_end=train_end)
        # 拟合期成本（R）：决策阈值查表用（D41；EV ≥ threshold×成本）
        self._fit_cost_r(sym, triggers, costs, contract_size, atr)
        return labeled

    def _fit_cost_r(self, sym, triggers, costs, contract_size, atr):
        atrs = atr.to_numpy()
        for r in triggers.itertuples(index=False):
            hour, price, bar = int(r.ny_hour), float(r.close), int(r.bar)
            if not math.isfinite(atrs[bar]):
                continue
            spr = costs.spread_p90(sym, hour, price=price)
            slip = costs.slippage(sym, hour, price=price)
            comm = costs.commission_per_lot(sym) / contract_size
            r_price = self.SL_ATR * float(atrs[bar])
            key = (str(r.situation), str(r.action))
            # 逐触发累计均值（(situation,action) 粒度）
            tot, n = self._cost_r.get(key, (0.0, 0))
            self._cost_r[key] = (tot + (spr + 2.0 * slip + comm) / r_price,
                                 n + 1)
        self._cost_r = {k: (t / n if n else 0.0) for k, (t, n)
                        in self._cost_r.items()}

    # ------------------------------------------------------------- 使用

    def with_model(self, model: dict) -> "LevelDecisionV1":
        if not isinstance(model, dict) or "prob_table" not in model:
            raise ValueError("模型缺 prob_table（附录 B 格式）")
        new = type(self)()
        new._table = ProbTable.from_dict(model["prob_table"])
        new._model_meta = dict(model)
        new._cost_r = {tuple(k.split("|")): float(v) for k, v in
                       (model.get("cost_r_by_situation_action") or {}).items()}
        return new

    def model_sha256(self) -> str:
        """模型内容的规范化哈希（与冻结文件一致——同一模型同一哈希）。"""
        return hashlib.sha256(_canonical_bytes(self._model_meta)).hexdigest()

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        if self._table is None:
            raise ModelMissingError(
                f"{type(self).__name__} 未装载冻结模型——先 fit（或 "
                "with_model）再评估（铁律 21：拟合与使用分离）")
        n = len(bars)
        sig = [0] * n
        atr = atr_series(bars)
        sma = trend_sma200(bars)
        pct = atr_pct_2000(atr)
        atrs, smas, pcts = (atr.to_numpy(), sma.to_numpy(), pct.to_numpy())
        aparams = ActionParams(breakout_atr=self.BREAKOUT_ATR,
                               retest_window_bars=self.RETEST_WINDOW_BARS)
        sym = self.SYMBOLS[0]
        # 触发索引：bar → [(situation, action, side)]
        trig_at: dict[int, list[tuple[str, str, int]]] = {}
        for sit in self.SITUATIONS:
            levels = level_rows_for_situation(
                bars, sit,
                grid_step=(self.LEVEL_GRID or {}).get(sym)
                if sit == "S1" else None,
                approach=self.APPROACH, atr=atr)
            events = find_level_events(bars, levels, situation=sit,
                                       approach=self.APPROACH, atr=atr)
            for t in action_triggers(bars, events, params=aparams,
                                     atr=atr).itertuples(index=False):
                trig_at.setdefault(int(t.bar), []).append(
                    (str(t.situation), str(t.action), int(t.side)))
        held_until = -1          # 持仓期间新触发忽略（信号层状态机）
        for i in range(n):
            if i <= held_until:
                sig[i] = sig[i - 1] if i > 0 else 0
                continue
            best = None
            for sit, action, side in trig_at.get(i, []):
                if not math.isfinite(atrs[i]):
                    continue
                feats = {"trend_sma200": smas[i] if smas[i] is not None
                         else "na",
                         "atr_pct_2000": pcts[i] if pcts[i] is not None
                         else "na"}
                try:
                    est = self._table.lookup(sit, action, feats)
                except KeyError:
                    continue          # 该动作无任何样本——不参与决策（缺省不做）
                cost_r = self._cost_r.get((sit, action), 0.0)
                cand = (action, est, cost_r)
                if best is None or _ev_of(cand, self.POLICY.z) > \
                        _ev_of(best, self.POLICY.z):
                    best = cand
            if best is None:
                continue
            action = choose([(best[0], best[1], best[2])], z=self.POLICY.z,
                            threshold_mult=self.POLICY.threshold_mult)
            if action is not None:
                side = next(s for sit2, a2, s in trig_at[i]
                            if a2 == action)
                for j in range(i, min(i + self.HORIZON_BARS, n)):
                    sig[j] = side
                held_until = i + self.HORIZON_BARS - 1
        return pd.Series(sig, index=bars.index, dtype="int64")


def _ev_of(cand, z) -> float:
    return ev_lower(cand[1], z=z)


# ------------------------------------------------------------- 示例子类（卡绑定）
# 每张 P 卡一个子类：常量即卡配置（同 H003-v2 做法）。T77/T91 用到时
# 按 P###-v# 逐张定义；此处给最小示例供测试与冒烟。


class LevelDecisionS1Demo(LevelDecisionV1):
    """S1 整数关口演示子类（XAUUSD H1，网格 50）。"""

    SL_ATR = 1.0
    TP_ATR = 2.0
    HORIZON_BARS = 24
    SITUATIONS = ("S1",)
    LEVEL_GRID = {"XAUUSD": 50.0}
    APPROACH = Approach(lookback_bars=24, min_distance_atr=1.0,
                        touch_atr=0.25)
    BREAKOUT_ATR = 0.25
    RETEST_WINDOW_BARS = 6
    SYMBOLS = ("XAUUSD",)
    TIMEFRAME = "H1"
    required_data = RequiredData(symbols=("XAUUSD",), timeframe="H1",
                                 lookback_bars=2100)
