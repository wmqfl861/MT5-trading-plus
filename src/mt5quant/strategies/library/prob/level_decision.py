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

T104 批 2（audit2 P2-01/P2-02）：白名单从"动作集合"升级为**（局面×动作）
集合**（S1:A3 不再顺带放行 S2:A3），fit / with_model / generate_signals
三处同一获准集合过滤，白名单写入模型 dict（allowed_hypotheses 键）并在
装载时核对；显式覆写 exit_rules 与标签 bracket 不一致 → 拒绝（不再
"尊重"）。generate_signals 支持**决策起点**（decision_start）：起点前只算
特征/局面识别，不执行入场决策、不进 held_until/forced_zero 状态机——
split 后从空状态处理新触发（bt 的输出掩码适配层由丁接线换用本原语）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from mt5quant.backtest.engine_a import atr_series, run_backtest
from mt5quant.prob.artifact import _canonical_bytes
from mt5quant.prob.decision import ev_lower
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
# 动作全集（mt5quant.strategies.library.prob.actions；P-07 白名单的合法值）
ALL_HYPOTHESES: tuple[str, ...] = ("A1", "A2", "A3", "A4")
# 局面全集（mt5quant.strategies.library.prob.situations；P2-01 白名单
# "S:A" 对的合法局面段）
ALL_SITUATIONS: tuple[str, ...] = ("S1", "S2", "S3")
# 白名单条目里表示"任意局面"的通配段（旧式裸动作条目 "A3" 的解析目标）
ANY_SITUATION = "*"


class ModelMissingError(RuntimeError):
    """没装（冻结）模型就调用 generate_signals（fail-closed）。"""


def parse_hypothesis_pairs(items) -> frozenset[tuple[str, str]]:
    """卡假设串 →（局面, 动作）对集合（P2-01；bt checker 与策略共用）。

    输入形如 "S1:A3:fade>base"（卡 [prob].hypotheses / 轮次 hypotheses 的
    条目）——取前两段为（局面, 动作），其余段（方向假设描述）忽略。段数
    不足 / 未知局面 / 未知动作 → ValueError（fail-closed，不静默跳过）。
    """
    out: list[tuple[str, str]] = []
    for raw in list(items):
        parts = str(raw).split(":")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise ValueError(
                f"假设条目 {raw!r} 须为 'S:A:…' 形式（至少 局面:动作 两段）"
                "——P2-01 解析失败即拒绝")
        sit, act = parts[0], parts[1]
        if sit not in ALL_SITUATIONS:
            raise ValueError(
                f"假设条目 {raw!r} 的局面 {sit!r} 未知——合法局面："
                f"{ALL_SITUATIONS}（P2-01）")
        if act not in ALL_HYPOTHESES:
            raise ValueError(
                f"假设条目 {raw!r} 的动作 {act!r} 未知——合法动作："
                f"{ALL_HYPOTHESES}（P2-01）")
        out.append((sit, act))
    return frozenset(out)


def _parse_allowed_entries(entries) -> frozenset[tuple[str, str]] | None:
    """ALLOWED_HYPOTHESES 条目 →（局面, 动作）对集合；空 → None=全部。

    P2-01 升级：条目两种写法——"S1:A3"（局面×动作对）或旧式 "A3"（裸动作
    → (ANY_SITUATION, 动作)，即任意局面下该动作获准；兼容 P-07 期的
    OnlyA3 语义）。未知局面/动作 → ValueError（类定义时即抛）。
    """
    items = tuple(entries or ())
    if not items:
        return None
    out: list[tuple[str, str]] = []
    for raw in items:
        s = str(raw)
        if ":" in s:
            sit, _, act = s.partition(":")
            if sit not in ALL_SITUATIONS:
                raise ValueError(
                    f"ALLOWED_HYPOTHESES 条目 {raw!r} 的局面 {sit!r} 未知"
                    f"——合法局面：{ALL_SITUATIONS}（P2-01）")
        else:
            sit, act = ANY_SITUATION, s
        if act not in ALL_HYPOTHESES:
            raise ValueError(
                f"ALLOWED_HYPOTHESES 条目 {raw!r} 的动作 {act!r} 未知——"
                f"合法动作：{ALL_HYPOTHESES}（P2-01）")
        out.append((sit, act))
    return frozenset(out)


def _allowed_in(allowed, situation, action) -> bool:
    """（局面×动作）对是否获准：精确对或通配局面（ANY_SITUATION, 动作）。"""
    return (situation, action) in allowed \
        or (ANY_SITUATION, action) in allowed


@dataclass(frozen=True)
class DecisionPolicy:
    """决策阈值参数（D41/D46；类常量由卡的子类提供）。"""

    z: float = 1.0                # 概率下界的标准误倍数（lb_z）
    threshold_mult: float = 2.0   # EV_下界 ≥ threshold_mult × 来回成本(R)


class LevelDecisionV1(BaseStrategy):
    """价位决策 v1 基类：子类以类常量绑定一张卡的检验配置。

    P-07 + P2-01：ALLOWED_HYPOTHESES 白名单限定获准**（局面×动作）集合**
    （条目 "S1:A3" 或旧式 "A3"=任意局面；空=全部兼容）；SYMBOLS 须单品种
    （网格/评估按 SYMBOLS[0] 绑定）；子类的 exit_rules 按其最终 SL_ATR/
    TP_ATR 重建（标签与出场同口径）——显式覆写与自身 bracket 不一致 →
    类定义即拒绝（P2-01：不再"尊重"脱节的出场规则）。"""

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
    # 审核四 E2：决策配置类常量（卡的 [prob] 五字段与之核对；不再在
    # fit 里写死）
    FEATURES: tuple[str, ...] = ("trend_sma200", "atr_pct_2000")
    PRIOR_STRENGTH: int = 100
    MIN_CELL: int = 100
    POLICY: DecisionPolicy = DecisionPolicy()
    SYMBOLS: tuple[str, ...] = ("XAUUSD",)
    TIMEFRAME: str = "H1"
    # P2-01 获准（局面×动作）白名单：条目 "S1:A3"（局面×动作对）或旧式
    # "A3"（裸动作 → 任意局面）；空元组 = 全部兼容（缺省）。fit 的标签
    # 循环、with_model 的模型核对与 generate_signals 的候选收集**三处同
    # 一获准集合过滤**——集合外的（局面,动作）既不进模型也不参与决策。
    ALLOWED_HYPOTHESES: tuple[str, ...] = ()

    exit_rules = ExitRules(stop_loss_atr=SL_ATR, take_profit_atr=TP_ATR,
                           reentry="after_signal_change")

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # P2-01：白名单条目解析为（局面×动作）对——未知局面/动作在类定义
        # 时即抛（P-07 的未知动作检查由此覆盖：裸条目仍按动作校验）
        _parse_allowed_entries(getattr(cls, "ALLOWED_HYPOTHESES", ()))
        # P-07：上面类体里的 exit_rules 按基类常量（1.0/2.0）在类定义时
        # 求值——子类覆写 SL_ATR/TP_ATR 后不再跟随（引擎出场与标签口径
        # 脱节）。这里按最终类常量重建；P2-01：子类链上任一层**显式**
        # 覆写 exit_rules 且与其自身 SL/TP 常量推导值不同 → 拒绝（标签
        # bracket 与实际出场脱节；探针 whitelist 的 WrongExit 即此形态，
        # 旧实现"尊重之"会放行声明 SL1/TP2、实际 SL9/TP10 的子类）。本
        # hook 写进各层 __dict__ 的重建值与其常量推导值恒相等，不会被
        # 误判。一致的显式覆写（= 自身常量推导值）仍接受。
        mro = cls.__mro__
        chain = mro[:mro.index(LevelDecisionV1)]

        def _derived_exit_rules(k) -> ExitRules:
            return ExitRules(stop_loss_atr=k.SL_ATR, take_profit_atr=k.TP_ATR,
                             reentry="after_signal_change")

        for k in chain:
            er = k.__dict__.get("exit_rules")
            if er is not None and er != _derived_exit_rules(k):
                raise ValueError(
                    f"{k.__name__}.exit_rules={er!r} 与其标签 bracket 推导值"
                    f" {_derived_exit_rules(k)!r}（SL_ATR={k.SL_ATR}、"
                    f"TP_ATR={k.TP_ATR}、reentry=after_signal_change）不一致"
                    "——标签口径与引擎出场脱节，拒绝定义（P2-01：显式覆写"
                    "不一致不再'尊重'；time_stop_bars/week_close_exit 也不"
                    "进标签口径，D44）")
        # 不一致覆写已被上面拒绝 → 一律按最终类常量重建（P-07 的重建
        # 逻辑：一致的显式覆写与其推导值恒相等，覆写无损）
        cls.exit_rules = _derived_exit_rules(cls)

    def __init__(self):
        self._table: ProbTable | None = None
        self._cost_r: dict[tuple[str, str], float] = {}   # (situation,action)→成本R
        self._model_meta: dict = {}
        # P2-02 决策起点：None = 全历史决策（缺省，兼容）；generate_signals
        # 的 decision_start 参数优先于实例设置。
        self._decision_start: pd.Timestamp | None = None

    # ------------------------------------------------------------- 拟合

    def fit(self, bars_by_symbol: dict, costs_by_symbol: dict, params: dict,
            *, train_end, purge_bars: int) -> dict:
        self._require_single_symbol()
        self._check_exit_rules_consistency()
        # TODO(T105/T73B)：purge_bars 目前只接收未使用——逐窗拟合的 purge
        # 剔除在 T73B 批 3 真正生效，不在本模块（P2-01/02）改其语义。
        # S5：成本在 fit 内局部累计（不改实例状态——同一实例重复拟合、
        # 多品种卡都不得崩或串窗）；S8：合约大小与禁入窗必给，不默认。
        meta = params.get("__fit__") or {}
        sizes = meta.get("sizes") or {}
        policies = meta.get("policies") or {}
        for sym in sorted(bars_by_symbol):
            if policies.get(sym) is None:
                # E4：缺入场政策不按"不限制"处理——回退到仓库唯一来源
                # config/sessions.toml 的 SessionPolicy（铁律 11；与 bt
                # 同一配置）。加载失败才报错。生产路径（bt fit/screen/
                # gate/prob test）恒经 __fit__.policies 显式传入，不走
                # 此回退。
                from mt5data.sessions import SessionConfigError,                     load_session_policy
                try:
                    policies[sym] = load_session_policy()
                except SessionConfigError as e:
                    raise ValueError(
                        f"{sym} 缺入场政策（__fit__.policies），且仓库"
                        f"sessions.toml 加载失败（{e}）——标签引擎须与 bt "
                        "同一 entry_policy_for（审核四 E4；蓝图 §2.3）")                     from e
        cost_acc: dict[tuple[str, str], list[float]] = {}
        parts = []
        for sym in sorted(bars_by_symbol):
            if sym not in sizes:
                raise ValueError(
                    f"{sym} 的合约大小未知（__fit__.sizes 缺失）——标签"
                    "引擎须与评估同口径，不默认 1.0（审核三 S8）")
            parts.append(self._labels_for_symbol(
                sym, bars_by_symbol[sym], costs_by_symbol.get(sym),
                contract_size=sizes[sym],
                session_policy=policies.get(sym),
                train_end=train_end, purge_bars=purge_bars,
                cost_acc=cost_acc))
        labels = pd.concat([p for p in parts if len(p)],
                           ignore_index=True) if any(len(p) for p in parts) \
            else pd.DataFrame(columns=["situation", "action", "outcome",
                                       "net_r", *FEATURES])
        table = ProbTable.fit(labels, group_by=list(self.FEATURES),
                              prior_strength=self.PRIOR_STRENGTH,
                              min_cell=self.MIN_CELL)
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
            # P2-01：获准（局面×动作）集合进模型身份——with_model 按它核对
            # "模型装错卡"（空白名单卡装 S1:A3 模型 / 反之均拒绝）。空列表
            # = 全部（缺省兼容值）。**注意**：本键随模型 dict 进内容哈希
            # （model_sha256/artifact 冻结），批 1 的 TRIAL_IDENTITY_VERSION
            # ="v1" 不含白名单维度——bt.py 侧接线时应提升为 v2（丁），
            # 旧 v1 冻结模型无该键 → with_model 按"全部"处理不拒绝。
            "allowed_hypotheses": self._allowed_model_entries(),
            "cost_r_by_situation_action": {
                f"{k[0]}|{k[1]}": (sum(v) / len(v) if v else 0.0)
                for k, v in cost_acc.items()},
            "prob_table": table.to_dict(),
        }
        return model

    def _labels_for_symbol(self, sym, bars, costs, *, contract_size,
                           session_policy, train_end, purge_bars,
                           cost_acc: dict) -> pd.DataFrame:
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
        # P2-01 三重过滤第一处（fit 侧）：只给获准（局面×动作）对打标签、
        # 累计成本——集合外的动作既不进模型也不累计成本（S1:A3 获准不放行
        # S2:A3；裸动作条目 = 任意局面）
        allowed = self._allowed_hypotheses()
        if allowed is not None:
            keep = [_allowed_in(allowed, str(s), str(a))
                    for s, a in zip(triggers["situation"], triggers["action"])]
            triggers = triggers[keep].reset_index(drop=True)
            if not len(triggers):
                return pd.DataFrame(columns=["situation", "action",
                                             "outcome", "net_r", *FEATURES])
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
            sizing=None, volume_lots=1.0, train_end=train_end,
            session_policy=session_policy)          # S8：禁入窗与引擎同口径
        # 拟合期成本（R）：决策阈值查表用（D41；EV ≥ threshold×成本）。
        # S5：局部累计（sum/list），由调用方收均值进模型。
        atrs = atr.to_numpy()
        for r in triggers.itertuples(index=False):
            hour, price, bar = int(r.ny_hour), float(r.close), int(r.bar)
            if not math.isfinite(atrs[bar]):
                continue
            spr = costs.spread_p90(sym, hour, price=price)
            slip = costs.slippage(sym, hour, price=price)
            comm = costs.commission_per_lot(sym) / contract_size
            r_price = self.SL_ATR * float(atrs[bar])
            cost_acc.setdefault((str(r.situation), str(r.action)),
                                []).append((spr + 2.0 * slip + comm) / r_price)
        return labeled

    # ------------------------------------------------------------- 使用

    def _require_single_symbol(self) -> None:
        """P-07：单品种实例绑定——generate_signals 只按 SYMBOLS[0] 的网格
        评估，SYMBOLS 声明多品种即拒绝（LEVEL_GRID 只需覆盖实际服务的
        品种）。"""
        syms = tuple(self.SYMBOLS)
        if len(syms) != 1:
            raise ValueError(
                f"{type(self).__name__}.SYMBOLS={syms!r} 须恰一个品种——"
                "单品种实例绑定：generate_signals 只按 SYMBOLS[0] 的网格"
                "评估（P-07）")

    def _allowed_hypotheses(self) -> frozenset[tuple[str, str]] | None:
        """P2-01 白名单：解析为（局面,动作）对集合；None = 全部兼容
        （空白名单，缺省）。裸动作条目 → (ANY_SITUATION, 动作)。"""
        return _parse_allowed_entries(self.ALLOWED_HYPOTHESES)

    def _allowed_model_entries(self) -> list[str]:
        """白名单 → 模型 dict 的 allowed_hypotheses 值（排序串；空=[]）。

        通配局面写作 "*:A3"（旧式裸动作条目的规范形）。"""
        allowed = self._allowed_hypotheses()
        if allowed is None:
            return []
        return sorted(f"{s}:{a}" for s, a in allowed)

    def _check_exit_rules_consistency(self) -> None:
        """P2-01：实例**真实** exit_rules（引擎实际消费的属性——实例覆写
        优先于类）与标签 bracket 推导值一致；不一致 → ValueError（不只比
        类常量：运行期 instance.exit_rules = … 的篡改也要拦住）。"""
        cls = type(self)
        derived = ExitRules(stop_loss_atr=cls.SL_ATR,
                            take_profit_atr=cls.TP_ATR,
                            reentry="after_signal_change")
        actual = getattr(self, "exit_rules", None)
        if actual != derived:
            raise ValueError(
                f"{cls.__name__} 实例 exit_rules={actual!r} 与标签 bracket "
                f"推导值 {derived!r}（SL_ATR={cls.SL_ATR}、TP_ATR="
                f"{cls.TP_ATR}、reentry=after_signal_change）不一致——标签"
                "口径与引擎出场脱节，拒绝运行（P2-01）")

    def _check_model_config(self, model: dict) -> None:
        """P-07 + P2-01：模型 dict 携带的 symbols/timeframe/bracket/
        allowed_hypotheses 若与类常量不一致 → ValueError（模型装错卡/
        策略的防呆；缺省字段跳过——旧模型无 allowed_hypotheses 键按
        "全部"处理，向后兼容批 1 的 v1 冻结件）。"""
        cls = type(self)
        syms = model.get("symbols")
        if syms is not None:
            got = [str(s) for s in syms]
            if got != sorted(str(s) for s in self.SYMBOLS):
                raise ValueError(
                    f"模型 symbols={got!r} 与 {cls.__name__}.SYMBOLS="
                    f"{tuple(self.SYMBOLS)!r} 不一致——模型与策略类错配"
                    "（P-07）")
        tf = model.get("timeframe")
        if tf is not None and str(tf) != self.TIMEFRAME:
            raise ValueError(
                f"模型 timeframe={tf!r} 与 {cls.__name__}.TIMEFRAME="
                f"{self.TIMEFRAME!r} 不一致——模型与策略类错配（P-07）")
        bracket = model.get("bracket")
        if bracket is not None:
            expect = {"sl_atr": self.SL_ATR, "tp_atr": self.TP_ATR,
                      "horizon_bars": self.HORIZON_BARS}
            if not isinstance(bracket, dict):
                raise ValueError(
                    f"模型 bracket 须为 dict：{bracket!r}（P-07）")
            for k in ("sl_atr", "tp_atr", "horizon_bars"):
                if k in bracket and bracket[k] != expect[k]:
                    raise ValueError(
                        f"模型 bracket.{k}={bracket[k]!r} 与 {cls.__name__}"
                        f" 常量 {k}={expect[k]!r} 不一致——标签口径与出场"
                        "规则脱节（P-07）")
        # P2-01：白名单进模型身份——模型记录的获准集合与类常量不一致
        # （空白名单卡装 S1:A3 模型 / S1:A3 卡装全量模型 alike）→ 拒绝；
        # 旧模型（无该键）不核对（identity v1 冻结件兼容）
        allowed_in_model = model.get("allowed_hypotheses")
        if allowed_in_model is not None:
            got = sorted(str(x) for x in allowed_in_model)
            expect = self._allowed_model_entries()
            if got != expect:
                raise ValueError(
                    f"模型 allowed_hypotheses={got!r} 与 {cls.__name__} 的"
                    f"获准（局面×动作）集合 {expect!r} 不一致——模型装错卡"
                    "（P2-01：fit/模型/决策三处须同一获准集合）")

    def with_model(self, model: dict) -> "LevelDecisionV1":
        if not isinstance(model, dict) or "prob_table" not in model:
            raise ValueError("模型缺 prob_table（附录 B 格式）")
        self._check_model_config(model)
        new = type(self)()
        new._table = ProbTable.from_dict(model["prob_table"])
        new._model_meta = dict(model)
        new._cost_r = {tuple(k.split("|")): float(v) for k, v in
                       (model.get("cost_r_by_situation_action") or {}).items()}
        return new

    def model_sha256(self) -> str:
        """模型内容的规范化哈希（与冻结文件一致——同一模型同一哈希）。"""
        return hashlib.sha256(_canonical_bytes(self._model_meta)).hexdigest()

    # ------------------------------------------------- P2-02 决策起点

    def set_decision_start(self, decision_start) -> "LevelDecisionV1":
        """P2-02：设置决策起点（时间戳；None = 全历史决策）。返回 self
        （适配层可链式 `strat.set_decision_start(split).generate_signals…`）。

        起点之前 generate_signals 只算特征/局面识别，不执行入场决策、不进
        held_until/forced_zero 状态机；起点后从**空状态**处理新触发——训练
        段（预热）的虚拟持仓不再压掉验证段的第一根合法触发。
        """
        self._decision_start = (pd.Timestamp(decision_start)
                                if decision_start is not None else None)
        return self

    def reset_decision_state(self) -> "LevelDecisionV1":
        """P2-02：清空决策状态（起点回到 None=全历史）。决策状态机本身是
        每次 generate_signals 调用的局部状态（纯函数、截断一致）——本入口
        只重置实例上配置的起点语义。"""
        self._decision_start = None
        return self

    def with_decision_start(self, decision_start) -> "LevelDecisionV1":
        """P2-02：返回配置了决策起点的新实例（不动本实例；与 with_model
        同构的不可变风格——bt 的适配层可用它替代输出掩码门）。"""
        new = type(self)()
        new._table = self._table
        new._cost_r = dict(self._cost_r)
        new._model_meta = dict(self._model_meta)
        new.set_decision_start(decision_start)
        return new

    def generate_signals(self, bars: pd.DataFrame, params: dict,
                         *, decision_start=None) -> pd.Series:
        """每根 bar 找触发、查表、按期望值下界决策（P2-01 白名单过滤）。

        P2-02 决策起点（audit2 P-08/P2-02）：decision_start（时间戳；显式
        参数优先于实例 set_decision_start 的设置，都缺省 None=全历史决策）
        之前的 bar 只算特征/局面识别——不入场、不进 held_until/forced_zero
        状态机（训练段尾部的持有/强制归零不影响验证段）；起点当根起（时间
        ≥ 起点）从空状态处理新触发。"预热进特征"的口径不变：调用方仍传
        足量预热历史，特征/局面识别照常使用全序列。
        """
        if self._table is None:
            raise ModelMissingError(
                f"{type(self).__name__} 未装载冻结模型——先 fit（或 "
                "with_model）再评估（铁律 21：拟合与使用分离）")
        self._require_single_symbol()
        self._check_exit_rules_consistency()
        n = len(bars)
        sig = [0] * n
        atr = atr_series(bars)
        sma = trend_sma200(bars)
        pct = atr_pct_2000(atr)
        atrs, smas, pcts = (atr.to_numpy(), sma.to_numpy(), pct.to_numpy())
        aparams = ActionParams(breakout_atr=self.BREAKOUT_ATR,
                               retest_window_bars=self.RETEST_WINDOW_BARS)
        sym = self.SYMBOLS[0]
        allowed = self._allowed_hypotheses()
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
                # P2-01 三重过滤第三处（决策侧）：获准（局面×动作）集合外
                # 的候选不进决策（S1:A3 获准不放行 S2:A3）
                if allowed is not None and not _allowed_in(
                        allowed, str(t.situation), str(t.action)):
                    continue
                trig_at.setdefault(int(t.bar), []).append(
                    (str(t.situation), str(t.action), int(t.side)))
        # P2-02 决策起点：起点前（time < 起点）的 bar 只算特征（触发已收
        # 集进 trig_at 供特征/局面口径，但状态机不消费它们）
        start_idx = 0
        eff_start = decision_start if decision_start is not None \
            else self._decision_start
        if eff_start is not None:
            times = pd.to_datetime(bars["time"])
            start_idx = int(times.searchsorted(pd.Timestamp(eff_start),
                                               side="left"))
            if start_idx >= n:
                return pd.Series(sig, index=bars.index, dtype="int64")
        held_until = -1          # 持仓期间新触发忽略（信号层状态机）
        forced_zero = -1         # S4：决策段结束后那一根固定 0（不链式续持）
        for i in range(n):
            if i < start_idx:
                continue                   # P2-02：起点前不建任何交易状态
            if i <= held_until:
                sig[i] = sig[i - 1] if i > 0 else 0
                continue
            if i == forced_zero:
                sig[i] = 0        # 第 i+K 根：本段决策的终止 bar，新触发忽略
                continue
            # D2：把（局面,动作,方向,估计,成本）全部交给选择——在**满足
            # 门槛**的候选里取期望值最高（旧实现先取最高再看门槛，会把
            # 其他过门槛的动作放弃）；同一动作两个方向都过门槛 → 不做
            # （方向不可确定）；并列按（局面,动作,方向）字典序取先。
            cands = []
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
                    continue      # 该动作无任何样本——不参与决策（缺省不做）
                key_cost = (sit, action)
                if key_cost not in self._cost_r:
                    continue      # E4：无成本估计的（局面,动作）不参与
                    # 决策（按 0 计会让门槛形同虚设）
                cost_r = self._cost_r[key_cost]
                ev = ev_lower(est, z=self.POLICY.z)
                if ev >= self.POLICY.threshold_mult * cost_r:
                    cands.append((ev, sit, action, side))
            if not cands:
                continue
            cands.sort(key=lambda c: (-c[0], c[1], c[2], c[3]))
            top = cands[0]
            sides = {c[3] for c in cands}
            if -1 in sides and 1 in sides:
                continue          # E3（蓝图 §2.5 第 4 条）：同一根上方向
                # 相反的动作都过门槛（跨局面/跨动作 alike）——两个方向的
                # 估计各自没有考虑对方，信号冲突按缺省不做
            for j in range(i, min(i + self.HORIZON_BARS, n)):
                sig[j] = top[3]
            held_until = i + self.HORIZON_BARS - 1
            forced_zero = i + self.HORIZON_BARS   # S4：K 根后强制归零一根
        return pd.Series(sig, index=bars.index, dtype="int64")


def _ev_of(cand, z) -> float:
    return ev_lower(cand[1], z=z)


# ------------------------------------------------------------- 示例子类（卡绑定）
# 每张 P 卡一个子类：常量即卡配置（同 H003-v2 做法）。T77/T91 用到时
# 按 P###-v# 逐张定义；此处给最小示例供测试与冒烟。


class LevelDecisionS1Demo(LevelDecisionV1):
    """S1 整数关口演示子类（XAUUSD H1）——**只供测试与审核探针**，
    真实卡各自定义子类（常量随卡写死；审核四 E4）。常量与审核探针卡
    一致（K=8、网格 2.0、approach 12/1.0/0.25、retest 4；探针 R1/R7
    的卡-策略常量核对按此对齐）。"""

    SL_ATR = 1.0
    TP_ATR = 2.0
    HORIZON_BARS = 8
    SITUATIONS = ("S1",)
    LEVEL_GRID = {"XAUUSD": 2.0}
    APPROACH = Approach(lookback_bars=12, min_distance_atr=1.0,
                        touch_atr=0.25)
    BREAKOUT_ATR = 0.25
    RETEST_WINDOW_BARS = 4
    SYMBOLS = ("XAUUSD",)
    TIMEFRAME = "H1"
    required_data = RequiredData(symbols=("XAUUSD",), timeframe="H1",
                                 lookback_bars=2100)

