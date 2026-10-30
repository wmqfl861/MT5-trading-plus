"""G3 walk-forward（T21b/H5）：**日历月**滚动训练/测试窗，参数只在卡的网格内重选。

窗口口径（config/gates.toml [g3]，随门槛冻结）：
- H1 及以上：训练 train_months_hourly（24）个月、测试 test_months_hourly（6）个月；
- 日内（< 1 小时）：12 / 3 个月——旧实现按 bar 数固定（2000/500 根），
  M5 的测试窗只有约 2.4 天，样本外夏普基本是噪声（审核 H5/探针 P5）；
- 滚动步长 = 测试月数；最后一段的测试窗可被数据末尾截短（按实际 bar 计）。

流程（滚动，非锚定），每段：
- 训练窗 [train_start, train_end) 在网格内按**净利**挑最优参数（选择只用
  训练数据；切片带回看预热条，预热条也在样本内，选择不接触测试数据）；
- 测试窗 [test_start, test_end) 用挑出的参数出样本外结果——回看预热条取自
  训练窗末尾（合法：属于样本内数据），引擎从切片起点空仓起步；
- oos_net / oos_trades **只计 entry_time ≥ 测试窗起点的交易**：预热段里
  （测试窗起点之前）发生的交易不计入样本外统计（H5）；
- 样本外 Sharpe 由各段测试窗权益收益率拼接（年化口径与 report.summarize
  一致：实测每年 bar 数，不足退回日历）。口径差（L4）：测试窗收益取自
  权益曲线，**含预热段开仓的持仓在测试窗内的浮动盈亏**（持仓跨越测试窗
  起点时），与 oos_trades（只计测试窗入场）不同——影响小，读报告时留意；
- 通过条件（蓝图附录 B）：样本外夏普 > 0，且 ≥ 样本内夏普 × min_oos_ratio。

T73B（audit2 §4.1 步骤 3-5，2026-10-30 批 3）：fittable 策略（P 线）的
G3 走 run_g3_fittable——**每训练窗独立 fit**（训练输入严格早于窗末、
purge_bars 经 level_decision.fit 真正生效）、每窗模型 hash/数据/配置/
拟合范围冻结进 artifact（save_model：文件内容哈希 = model_sha256）、
OOS 段只用该窗模型、预热不产生交易状态（消费 with_decision_start 决策
起点原语）、OOS 拼接不重叠不含训练收益；并产出关卡 v3 的 P 线专属
Brier 否决数据（brier_skill_veto：校准评价只用 OOS、基准率只用相应
训练窗）。run_g3 的 H 语义逐位不变（H 基准探针/回归另证）。
"""
from __future__ import annotations

import hashlib
import math

import pandas as pd

from mt5data.store import TIMEFRAME_SECONDS
from mt5quant.backtest.report import bars_per_year, data_sha256, observed_bars_per_year
from mt5quant.prob.artifact import _canonical_bytes, save_model
from mt5quant.validation.robustness import BacktestSpec, run_engine


def month_window_spec(timeframe: str, cfg) -> tuple[int, int]:
    """(train_months, test_months)：≥ H1 用小时档，< H1 用日内档（gates.toml）。"""
    tf = timeframe.upper()
    if tf not in TIMEFRAME_SECONDS:
        raise ValueError(f"未知 timeframe：{timeframe!r}")
    if TIMEFRAME_SECONDS[tf] >= 3600:
        return int(cfg.train_months_hourly), int(cfg.test_months_hourly)
    return int(cfg.train_months_intraday), int(cfg.test_months_intraday)


def _month_index(ts) -> int:
    """绝对月序号：year*12 + (month-1)（日历月运算不用日期差）。"""
    return int(ts.year) * 12 + int(ts.month) - 1


def _month_start(m: int) -> pd.Timestamp:
    return pd.Timestamp(year=m // 12, month=m % 12 + 1, day=1)


def plan_windows(times, *, train_months: int, test_months: int) -> list[dict]:
    """日历月滚动窗计划（纯函数，供测试与诊断）。

    返回每段 {"train_start", "train_end", "test_start", "test_end"}（
    Timestamp 边界，左闭右开；train_end == test_start）。段起点从首根 bar
    所在月的月初开始，滚动步长 = test_months；收录条件：测试窗的最后一个
    月不晚于最后一根 bar 所在月（末段测试窗可被数据末尾截短）。
    """
    if train_months < 1 or test_months < 1:
        raise ValueError(
            f"train_months/test_months 必须 ≥1，收到 {train_months}/{test_months}")
    t = pd.Series(times)
    if not pd.api.types.is_datetime64_any_dtype(t):
        t = pd.to_datetime(t)
    if len(t) == 0:
        return []
    m_first = _month_index(t.iloc[0])
    m_last = _month_index(t.iloc[-1])
    out: list[dict] = []
    start = m_first
    while start + train_months + test_months <= m_last + 1:
        out.append({
            "train_start": _month_start(start),
            "train_end": _month_start(start + train_months),
            "test_start": _month_start(start + train_months),
            "test_end": _month_start(start + train_months + test_months),
        })
        start += test_months
    return out


def _span_months(times) -> int:
    return _month_index(times.iloc[-1]) - _month_index(times.iloc[0]) + 1


def run_g3(spec: BacktestSpec, bars, costs, *, grid, in_sample_sharpe, cfg,
           known_gaps: list | None = None) -> dict:
    """grid：参数组合列表（卡网格的笛卡尔积，如 [{"k": 2}, {"k": 3}] 或 [{}]）。

    T79/R12-G1：known_gaps（序列 meta.json 登记、mt5data.quality.
    read_known_gaps 读取的已知缺口）与某段训练∪测试窗相交 → 该段记
    sample_incomplete 并在 known_gaps_note 注明"样本不完整"——只注记，
    不改变段数与通过判定（事件研究 T66 建成后接第三个消费点）。
    H-06：禁入政策经 spec.session_policy 随 BacktestSpec 进入全部复算
    （训练窗选参与测试窗出样本外结果都吃同一禁入口径，与基准一致）。
    """
    strategy = spec.strategy
    lookback = max(int(getattr(strategy.required_data, "lookback_bars", 0) or 0), 0)
    n = len(bars)
    train_m, test_m = month_window_spec(spec.timeframe, cfg)
    windows = plan_windows(bars["time"], train_months=train_m, test_months=test_m)
    bpy = observed_bars_per_year(bars["time"]) or bars_per_year(spec.timeframe)
    times = bars["time"].reset_index(drop=True)

    segments = []
    oos_returns: list[pd.Series] = []
    for w in windows:
        t0 = int(times.searchsorted(w["train_start"], side="left"))
        t1 = int(times.searchsorted(w["test_start"], side="left"))
        t2 = int(times.searchsorted(w["test_end"], side="left"))
        if t1 <= t0 or t2 <= t1:
            continue    # 该窗整段落进数据缺口（整月无 bar）→ 无法评估，跳过
        train = bars.iloc[max(0, t0 - lookback): t1]
        best_params, best_net = None, -math.inf
        for combo in grid:
            trades = run_engine(spec, train, costs, combo).trades
            net = float(trades["net"].sum()) if len(trades) else 0.0
            if net > best_net:
                best_net, best_params = net, dict(combo)
        test = bars.iloc[max(0, t1 - lookback): t2]
        oos = run_engine(spec, test, costs, best_params)
        test_start = times.iloc[t1]
        in_window = (pd.to_datetime(oos.trades["entry_time"]) >= test_start)
        oos_trade_count = int(in_window.sum())
        oos_net = (float(oos.trades.loc[in_window, "net"].sum())
                   if oos_trade_count else 0.0)
        tail = oos.equity.astype("float64").iloc[-(t2 - t1 + 1):]
        oos_returns.append(tail.pct_change().dropna())
        segment = {
            "train_range": [t0, t1],
            "test_range": [t1, t2],
            "train_start": w["train_start"].isoformat(),
            "test_start": w["test_start"].isoformat(),
            "test_end": w["test_end"].isoformat(),
            "test_bars": int(t2 - t1),
            "train_months": train_m,
            "test_months": test_m,
            "params": best_params,
            "train_net": float(best_net),
            "oos_net": oos_net,
            "oos_trades": oos_trade_count,
        }
        if known_gaps:
            hits = [f"{pd.Timestamp(s).isoformat()}→{pd.Timestamp(e).isoformat()}"
                    for s, e in known_gaps
                    if pd.Timestamp(s) < w["test_end"]
                    and pd.Timestamp(e) > w["train_start"]]
            if hits:
                segment["sample_incomplete"] = hits
        segments.append(segment)

    if not segments:
        return {"passed": False, "segments": [], "oos_sharpe": None,
                "in_sample_sharpe": in_sample_sharpe,
                "reason": (f"数据不足：{n} 根、跨 {_span_months(times)} 个日历月"
                           f" < 训练 {train_m} + 测试 {test_m} 个月"
                           f"（回看 {lookback}）")}

    all_rets = pd.concat(oos_returns, ignore_index=True)
    oos_sharpe = None
    if len(all_rets) >= 2:
        std = float(all_rets.std(ddof=1))
        if std > 0:
            oos_sharpe = float(all_rets.mean()) / std * math.sqrt(bpy)

    is_sharpe = float(in_sample_sharpe) if in_sample_sharpe is not None else None
    min_oos = None if is_sharpe is None else is_sharpe * float(cfg.min_oos_ratio)
    passed = bool(oos_sharpe is not None and oos_sharpe > 0
                  and is_sharpe is not None
                  and (is_sharpe <= 0 or oos_sharpe >= min_oos))
    result = {
        "passed": passed,
        "segments": segments,
        "oos_sharpe": oos_sharpe,
        "in_sample_sharpe": is_sharpe,
        "min_oos_ratio": float(cfg.min_oos_ratio),
        "min_required_oos_sharpe": min_oos,
        "n_test_bars_total": int(sum(len(r) for r in oos_returns)),
    }
    if known_gaps:
        flagged = [s for s in segments if s.get("sample_incomplete")]
        if flagged:
            result["known_gaps_note"] = (
                f"样本不完整：{len(flagged)}/{len(segments)} 个训练/测试窗与"
                "已知缺口（meta.json known_gaps）相交，样本外统计按现存"
                "数据计算")
    return result


# ------------------------------------------------ T73B：fittable 逐窗拟合

def brier_skill_veto(rows_per_window: list) -> dict:
    """关卡 v3 的 P 线专属否决项（T73B 条目 5，audit2 §4.1 步骤 4）。

    输入各窗 oos_calibration_rows 的产出（列 p_pred / y / base_rate）。
    池化技能分 SS = 1 − BS_model / BS_base：每行 p_pred = 该窗冻结模型
    的查表预测（只用 OOS 触发），基准预测 = 该行**相应训练窗**的动作
    基准率（模型 base_rates）——不同窗/不同动作的基准率逐行跟随，不混
    用单一全局基准。SS ≤ 0（拟合模型不优于训练窗基准率）→ 否决
    （passed=False）；无任何可评估行 → fail-closed 否决（校准无从评价
    时不得放行）。
    """
    per_window = []
    ps, ys, bs = [], [], []
    for i, rows in enumerate(rows_per_window):
        if rows is None or not len(rows):
            per_window.append({"window": i, "n": 0})
            continue
        p = rows["p_pred"].to_numpy(dtype="float64")
        y = rows["y"].to_numpy(dtype="float64")
        b = rows["base_rate"].to_numpy(dtype="float64")
        ps.append(p)
        ys.append(y)
        bs.append(b)
        per_window.append({"window": i, "n": int(len(rows)),
                           "mean_p_pred": float(p.mean()),
                           "mean_base_rate": float(b.mean())})
    base = {"passed": False, "skill": None, "n_oos": 0,
            "brier_model": None, "brier_base": None,
            "per_window": per_window,
            "note": "P 线专属否决（T73B）：校准评价只用 OOS 触发，基准率"
                    "=相应训练窗动作基准率（模型 base_rates）；Brier skill"
                    "≤0 → 否决"}
    if not ps:
        base["reason"] = "无 OOS 触发可评估（fail-closed：校准无从评价）"
        return base
    import numpy as np

    p = np.concatenate(ps)
    y = np.concatenate(ys)
    b = np.concatenate(bs)
    bs_base = float(np.mean((b - y) ** 2))
    bs_model = float(np.mean((p - y) ** 2))
    skill = 0.0 if bs_base <= 0.0 else 1.0 - bs_model / bs_base
    return {**base, "passed": bool(skill > 0.0), "skill": float(skill),
            "n_oos": int(len(p)), "brier_model": bs_model,
            "brier_base": bs_base}


def run_g3_fittable(spec: BacktestSpec, bars, costs, *, grid, in_sample_sharpe,
                    cfg, fit_meta: dict | None = None,
                    costs_by_symbol: dict | None = None,
                    strategy_cls=None, model_root=None,
                    known_gaps: list | None = None) -> dict:
    """fittable 策略（P 线）的 G3：每训练窗独立 fit + 逐窗冻结 + 决策起点
    OOS（T73B，audit2 §4.1 步骤 3；run_g3 的 H 语义不动，另走本函数）。

    每段（窗口计划/口径与 run_g3 同一 plan_windows/month_window_spec）：
    - 训练切片 [t0−lookback, t1)（预热条也在样本内）→ strategy_cls()
      .fit(train_end=测试窗首根时刻, purge_bars=K+2)——purge 在 fit 内
      真正剔除跨界触发（标签最后信息 t+K+1 不越界）；
    - 每窗模型/数据/配置/拟合范围冻结：model dict 规范化哈希
      （model_sha256；model_root 给出时 save_model 落盘，文件内容哈希
      =model_sha256，可凭 artifact 重放）+ 训练切片 data_sha256 +
      fit_window/purge_bars 随段登记；
    - 训练窗选参规则 = **净利选参**（walkforward 现行语义，result
      ["selection_rule"] 标注，与正式主检验分开）；grid 单组合时平凡；
    - OOS：测试切片 [t1−lookback, t2) 只用该窗模型，经 with_decision_start
      (测试窗首根时刻)——预热段只算特征、不产生任何交易状态
      （warmup_trades 恒 0）；收益拼接与 run_g3 同口径（各窗 OOS 首根
      起、不重叠、不含训练收益）；
    - brier_veto：各窗 OOS 校准行（oos_calibration_rows）→ brier_skill_veto
      （关卡 v3 的 P 线专属否决项，由 bt 侧并入 V3 判决链）。

    fit 失败（如训练窗无可用标签）的窗记入 fit_failures 并跳过（不静默
    计入统计）；全部失败 → 数据不足 fail。训练输入严格早于窗末：切片止于
    t1、purge 再剔末段，改窗末之后的 bar 不影响该窗模型（验收 3）。
    """
    strategy = spec.strategy
    cls = strategy_cls if strategy_cls is not None else type(strategy)
    lookback = max(int(getattr(strategy.required_data, "lookback_bars", 0)
                       or 0), 0)
    n = len(bars)
    train_m, test_m = month_window_spec(spec.timeframe, cfg)
    windows = plan_windows(bars["time"], train_months=train_m,
                           test_months=test_m)
    bpy = observed_bars_per_year(bars["time"]) or bars_per_year(spec.timeframe)
    times = bars["time"].reset_index(drop=True)
    purge = int(getattr(cls, "HORIZON_BARS", 0) or 0) + 2
    costs_by_symbol = costs_by_symbol or {spec.symbol: costs}

    segments: list[dict] = []
    fit_failures: list[dict] = []
    oos_returns: list[pd.Series] = []
    brier_rows: list = []
    window_models: dict[str, dict] = {}
    for w in windows:
        t0 = int(times.searchsorted(w["train_start"], side="left"))
        t1 = int(times.searchsorted(w["test_start"], side="left"))
        t2 = int(times.searchsorted(w["test_end"], side="left"))
        if t1 <= t0 or t2 <= t1:
            continue    # 该窗整段落进数据缺口 → 无法评估，跳过（与 H 同）
        train = bars.iloc[max(0, t0 - lookback): t1]
        test_start = times.iloc[t1]
        # ---- 每训练窗独立 fit（purge 生效；输入严格早于窗末）
        try:
            inst = cls()
            model = inst.fit({spec.symbol: train},
                             {spec.symbol: costs_by_symbol[spec.symbol]},
                             {"__fit__": dict(fit_meta or {})},
                             train_end=test_start, purge_bars=purge)
        except ValueError as e:
            fit_failures.append({"test_start": w["test_start"].isoformat(),
                                 "error": f"{type(e).__name__}: {e}"})
            continue
        if model_root is not None:
            path, sha = save_model(model, model_root)
        else:
            path = None
            sha = hashlib.sha256(_canonical_bytes(model)).hexdigest()
        window_models[sha] = model
        fitted = inst.with_model(model)
        fit_win = dict(model.get("fit_window") or {})
        for k in ("start", "end"):
            if fit_win.get(k) is not None:
                fit_win[k] = pd.Timestamp(fit_win[k]).isoformat()
        # ---- 训练窗选参规则：净利选参（现行语义；grid 单组合时平凡）
        best_params, best_net = None, -math.inf
        for combo in grid:
            trades = run_engine(spec, train, costs, combo,
                                strategy=fitted).trades
            net = float(trades["net"].sum()) if len(trades) else 0.0
            if net > best_net:
                best_net, best_params = net, dict(combo)
        # ---- OOS：只用该窗模型；决策起点 = 测试窗首根（预热零交易状态）
        test = bars.iloc[max(0, t1 - lookback): t2]
        oos_strat = fitted.with_decision_start(test_start)
        oos = run_engine(spec, test, costs, best_params, strategy=oos_strat)
        entry = pd.to_datetime(oos.trades["entry_time"]) \
            if len(oos.trades) else pd.DatetimeIndex([])
        in_window = entry >= test_start
        oos_trade_count = int(in_window.sum()) if len(entry) else 0
        warmup_trades = int(len(entry) - oos_trade_count)
        oos_net = (float(oos.trades.loc[in_window, "net"].sum())
                   if oos_trade_count else 0.0)
        tail = oos.equity.astype("float64").iloc[-(t2 - t1 + 1):]
        oos_returns.append(tail.pct_change().dropna())
        try:      # Brier 否决数据：OOS 校准行（评价只用 OOS/训练窗基准）
            brier_rows.append(oos_strat.oos_calibration_rows(
                test, costs, contract_size=spec.contract_size,
                session_policy=spec.session_policy,
                decision_start=test_start))
        except Exception as e:   # noqa: BLE001 —— 评价行缺失按空窗披露
            fit_failures.append({"test_start": w["test_start"].isoformat(),
                                 "error": f"oos_calibration_rows: "
                                          f"{type(e).__name__}: {e}"})
            brier_rows.append(None)
        segment = {
            "train_range": [t0, t1],
            "test_range": [t1, t2],
            "train_start": w["train_start"].isoformat(),
            "test_start": w["test_start"].isoformat(),
            "test_end": w["test_end"].isoformat(),
            "test_bars": int(t2 - t1),
            "train_months": train_m,
            "test_months": test_m,
            "params": best_params,
            "train_net": float(best_net),
            "oos_net": oos_net,
            "oos_trades": oos_trade_count,
            # ---- T73B 逐窗身份冻结（artifact）
            "model_sha256": sha,
            "model_path": (str(path) if path is not None else None),
            "model_data_sha256": data_sha256(train),
            "fit_window": fit_win,
            "purge_bars": purge,
            "warmup_trades": warmup_trades,
            "selection_rule": "train_net_profit",
        }
        if known_gaps:
            hits = [f"{pd.Timestamp(s).isoformat()}→{pd.Timestamp(e).isoformat()}"
                    for s, e in known_gaps
                    if pd.Timestamp(s) < w["test_end"]
                    and pd.Timestamp(e) > w["train_start"]]
            if hits:
                segment["sample_incomplete"] = hits
        segments.append(segment)

    if not segments:
        reason = (f"数据不足：{n} 根、跨 {_span_months(times)} 个日历月"
                  f" < 训练 {train_m} + 测试 {test_m} 个月"
                  f"（回看 {lookback}）")
        if fit_failures:
            reason += f"；另 {len(fit_failures)} 个窗拟合失败（{fit_failures}"
        return {"passed": False, "segments": [], "oos_sharpe": None,
                "in_sample_sharpe": in_sample_sharpe, "reason": reason,
                "mode": "fittable", "selection_rule": "train_net_profit",
                "purge_bars": purge, "fit_failures": fit_failures,
                "brier_veto": brier_skill_veto(brier_rows),
                "_window_models": {}}

    all_rets = pd.concat(oos_returns, ignore_index=True)
    oos_sharpe = None
    if len(all_rets) >= 2:
        std = float(all_rets.std(ddof=1))
        if std > 0:
            oos_sharpe = float(all_rets.mean()) / std * math.sqrt(bpy)

    is_sharpe = float(in_sample_sharpe) if in_sample_sharpe is not None \
        else None
    min_oos = None if is_sharpe is None else is_sharpe * float(cfg.min_oos_ratio)
    passed = bool(oos_sharpe is not None and oos_sharpe > 0
                  and is_sharpe is not None
                  and (is_sharpe <= 0 or oos_sharpe >= min_oos))
    result = {
        "passed": passed,
        "segments": segments,
        "oos_sharpe": oos_sharpe,
        "in_sample_sharpe": is_sharpe,
        "min_oos_ratio": float(cfg.min_oos_ratio),
        "min_required_oos_sharpe": min_oos,
        "n_test_bars_total": int(sum(len(r) for r in oos_returns)),
        # ---- T73B：与 H 路径分开标注（选参规则 ≠ 正式主检验）
        "mode": "fittable",
        "selection_rule": "train_net_profit",
        "purge_bars": purge,
        "brier_veto": brier_skill_veto(brier_rows),
        "_window_models": window_models,
    }
    if fit_failures:
        result["fit_failures"] = fit_failures
    if known_gaps:
        flagged = [s for s in segments if s.get("sample_incomplete")]
        if flagged:
            result["known_gaps_note"] = (
                f"样本不完整：{len(flagged)}/{len(segments)} 个训练/测试窗与"
                "已知缺口（meta.json known_gaps）相交，样本外统计按现存"
                "数据计算")
    return result
