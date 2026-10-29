"""标准回测报告：指标汇总、Markdown 渲染与可复现 Manifest。

口径说明：
- 金额字段（净利、成本、期望值）均为账户货币（引擎 C1 修复后的口径）。
- Sharpe / Sortino 按 bar 收益率（equity.pct_change()）年化，年化因子 =
  sqrt(每年 bar 数)。每年 bar 数优先取**数据实测**（bars 跨度 ≥ 30 天时：
  bar 根数 ÷ 跨越年数）——外汇/黄金/指数每周只交易约 5 天，按日历
  365.25×86400/周期秒数 会把 Sharpe 高估约 sqrt(7/5)≈18%；跨度不足时退回
  日历口径 bars_per_year(tf)。实际采用的值与依据写进 summary
  （annualization_bars_per_year / annualization_basis）。零方差或样本不足时记 None。
- Sortino 的下行偏差 = sqrt(mean(min(r,0)²))，分母为全部 bar（标准定义）；
  无任何负收益时记 None。
- PF（盈利因子）= 盈利交易净利之和 / |亏损交易净利之和|；无亏损交易时记
  None（避免无穷大进 JSON）。胜率按 net>0 计。
- 最大回撤按 equity 相对历史峰值的回撤（负数比例）。
- 成本占毛利比 = 总成本 / 中价毛利；总成本 = 佣金 + 滑点 + 价差 + |隔夜费|
  （均为 Trade 的拆分字段）；中价毛利 = Σ(gross + slippage_cost +
  spread_cost)，即把成交价里隐含的滑点/价差还原后的毛利。
- 期望值：期望净利（资金）、expectancy_r（平均 R 倍数）、
  expectancy_cost_multiple（期望净利 / 单笔平均来回成本）。
- data_sha256 版本化（H-12，2026-10-29）：
  - v1（旧，data_sha256_v1 保留供对照/迁移核对）：对 bars 的
    time+open+high+low+close 五列做 pandas.util.hash_pandas_object(
    ..., index=False)（逐行 uint64，不看行索引），全部求和得整数 s，
    再对 str(s) 的 UTF-8 字节做 hashlib.sha256，取 hexdigest。缺陷：
    求和满足交换律——**丢行序**；且不含 tick_volume 等策略实际可读列。
  - v2（现行，data_sha256）：bars 的**全部数据列**（列名排序后进头部）
    逐行按 canonical JSON 拼接（每行 payload = [行号, 各列值...]，顺序
    敏感；数值用最短往返表示，时间用 ISO 字符串，非有限数显式记
    "NaN"/"Infinity"/"-Infinity"），整体 UTF-8 再 sha256。同数据（同列
    内容同行序）必得同值；行交换、只改 volume/spread 等任何列、增删列
    均改变身份。
  - 返回值带版本前缀（"v1:" / "v2:"）——台账按完整字符串去重，新旧
    指纹自动区分；旧台账裸哈希记录不迁移（重跑同数据按 v2 记新试验）。
- git_commit：读不到（无 git、非仓库、超时、非零返回码）一律写
  "unknown"，绝不因取不到而中断报告。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from mt5data.store import TIMEFRAME_SECONDS
from mt5quant.backtest.engine_a import ENGINE_VERSION


def bars_per_year(timeframe: str) -> float:
    """年化因子用的一年 bar 数：365.25 天 / 周期秒数（M1→525960，H1→8766，
    D1→365.25）。Sharpe 年化 = mean(bar 收益)/std × sqrt(bars_per_year)。"""
    try:
        tf = TIMEFRAME_SECONDS[timeframe.upper()]
    except KeyError:
        raise ValueError(f"未知 timeframe：{timeframe!r}（须为 TIMEFRAME_SECONDS 之一）")
    return 365.25 * 86400.0 / tf


_MIN_OBSERVED_SPAN_DAYS = 30.0


def observed_bars_per_year(times) -> float | None:
    """数据实测的每年 bar 数（跨度 < 30 天或少于 2 根 → None，调用方退回日历口径）。"""
    t = pd.to_datetime(pd.Series(times))
    if len(t) < 2:
        return None
    span_days = (t.iloc[-1] - t.iloc[0]).total_seconds() / 86400.0
    if span_days < _MIN_OBSERVED_SPAN_DAYS:
        return None
    return (len(t) - 1) / (span_days / 365.25)


def annualization_factor(result) -> tuple[float | None, str]:
    """(每年 bar 数, 依据)；依据 ∈ {"observed", "calendar", "unknown"}。"""
    bars = getattr(result, "bars", None)
    if bars is not None and "time" in getattr(bars, "columns", ()):
        obs = observed_bars_per_year(bars["time"])
        if obs is not None:
            return obs, "observed"
    try:
        if result.timeframe:
            return bars_per_year(result.timeframe), "calendar"
    except ValueError:
        pass
    return None, "unknown"


def summarize(result, *, cost_version: str = "v0") -> dict:
    """把 BacktestResult 汇总为指标 dict（render/write_report 的唯一数据源）。"""
    trades = result.trades
    equity = result.equity.astype("float64")

    net = trades["net"] if len(trades) else pd.Series(dtype="float64")
    n = len(trades)
    wins = float(net[net > 0].sum()) if n else 0.0
    losses = float(net[net < 0].sum()) if n else 0.0
    gross_mid = float(
        (trades["gross"] + trades["slippage_cost"] + trades["spread_cost"]).sum()
    ) if n else 0.0
    total_cost = float(
        trades["commission"].sum() + trades["slippage_cost"].sum()
        + trades["spread_cost"].sum() - trades["swap"].sum()
    ) if n else 0.0

    ruined = bool(getattr(result, "ruined", False))
    ruined_at = getattr(result, "ruined_at", None)
    # T23：按风险定手数为 0 而跳过的入场尝试（含 ATR 不可得）；旧结果对象
    # （手工构造/引擎外合成）无此属性时按 0，与 ruined 的 getattr 口径一致
    skipped_zero_lots = int(getattr(result, "skipped_zero_lots", 0) or 0)

    returns = equity.pct_change().dropna() if len(equity) > 1 else pd.Series(dtype="float64")
    sharpe = sortino = None
    per_year, basis = annualization_factor(result)
    bpy = math.sqrt(per_year) if per_year else None
    if len(returns) >= 2 and bpy is not None:
        std = float(returns.std(ddof=1))
        if std > 0:
            sharpe = float(returns.mean()) / std * bpy
        if (returns < 0).any():
            # 标准下行偏差：分母为全部观测（正收益按 0 计入）
            dd = math.sqrt(float((returns.clip(upper=0.0) ** 2).mean()))
            sortino = float(returns.mean()) / dd * bpy

    if len(equity):
        peak = equity.cummax()
        mdd = float((equity / peak - 1.0).min())
    else:
        mdd = 0.0

    r_mult = trades["r_multiple"].dropna() if n else pd.Series(dtype="float64")
    avg_win = float(net[net > 0].mean()) if (net > 0).any() else None
    avg_loss = float(abs(net[net < 0].mean())) if (net < 0).any() else None

    by_year, by_hour, by_side = {}, {}, {}
    if n:
        years = pd.to_datetime(trades["exit_time"]).dt.year
        for y, g in trades.groupby(years):
            by_year[str(y)] = float(g["net"].sum())
        hours = pd.to_datetime(trades["entry_time"]).dt.hour
        for h, g in trades.groupby(hours):
            by_hour[str(h)] = float(g["net"].sum())
        for side, g in trades.groupby("side"):
            by_side[side] = {
                "count": int(len(g)),
                "net": float(g["net"].sum()),
                "win_rate": float((g["net"] > 0).mean()),
            }

    denom = len(result.bars) - 1
    return {
        "symbol": result.symbol,
        "timeframe": result.timeframe,
        "params": dict(result.params),
        "cost_model_version": cost_version,
        "net_profit": float(net.sum()) if n else 0.0,
        "return_pct": float(equity.iloc[-1] / equity.iloc[0] - 1.0) if len(equity) else 0.0,
        "sharpe": sharpe,
        "sortino": sortino,
        "annualization_bars_per_year": per_year,
        "annualization_basis": basis,
        "max_drawdown": mdd,
        "profit_factor": (wins / abs(losses)) if losses < 0 else None,
        "win_rate": float((net > 0).mean()) if n else 0.0,
        "trade_count": int(n),
        "long_count": int((trades["side"] == "long").sum()) if n else 0,
        "short_count": int((trades["side"] == "short").sum()) if n else 0,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff_ratio": (avg_win / avg_loss) if (avg_win is not None and avg_loss) else None,
        "expectancy": float(net.mean()) if n else None,
        "expectancy_r": float(r_mult.mean()) if len(r_mult) else None,
        "expectancy_cost_multiple": (
            float(net.mean()) / (total_cost / n)) if (n and total_cost > 0) else None,
        "time_in_market": (
            float(trades["bars_held"].sum()) / denom) if denom > 0 else 0.0,
        "cost_to_gross": (total_cost / gross_mid) if gross_mid > 0 else None,
        "total_commission": float(trades["commission"].sum()) if n else 0.0,
        "total_slippage": float(trades["slippage_cost"].sum()) if n else 0.0,
        "total_spread": float(trades["spread_cost"].sum()) if n else 0.0,
        "total_swap": float(trades["swap"].sum()) if n else 0.0,
        "by_year": by_year,
        "by_hour": by_hour,
        "by_side": by_side,
        "ruined": ruined,
        "ruined_at": ruined_at.isoformat() if ruined_at else None,
        "skipped_zero_lots": skipped_zero_lots,
    }


def _fmt(v, spec=".4f"):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return format(v, spec)


def render_markdown(summary: dict, result) -> str:
    """中文 Markdown 报告（write_report 落盘的 report.md 内容）。"""
    lines = [
        f"# 回测报告：{summary['symbol']} {summary['timeframe']}",
        "",
        f"- 参数：`{summary['params']}`　成本模型：{summary['cost_model_version']}",
        f"- 交易数：{summary['trade_count']}"
        f"（多 {summary['long_count']} / 空 {summary['short_count']}）",
    ]
    if summary.get("ruined"):
        lines.append("")
        lines.append("> ⚠ 权益已 ≤0（爆仓，首次 "
                     + str(summary.get("ruined_at"))
                     + "）：已停止开新仓；收益率/回撤等指标失真，仅供诊断。")
    lines += [
        "",
        "## 核心指标",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| 净利 | {_fmt(summary['net_profit'], '.1f')} |",
        f"| 收益率 | {_fmt(summary['return_pct'], '.2%')} |",
        f"| Sharpe（年化，{_fmt(summary.get('annualization_bars_per_year'), '.0f')} 根/年，"
        f"{summary.get('annualization_basis', '—')}） | {_fmt(summary['sharpe'])} |",
        f"| Sortino | {_fmt(summary['sortino'])} |",
        f"| 最大回撤 | {_fmt(summary['max_drawdown'], '.2%')} |",
        f"| PF | {_fmt(summary['profit_factor'], '.2f')} |",
        f"| 胜率 | {_fmt(summary['win_rate'], '.2%')} |",
        f"| 盈亏比 | {_fmt(summary['payoff_ratio'], '.2f')} |",
        f"| 期望值（净利/R/成本倍数） | {_fmt(summary['expectancy'], '.1f')}"
        f" / {_fmt(summary['expectancy_r'])} / {_fmt(summary['expectancy_cost_multiple'])} |",
        f"| 持仓时间占比 | {_fmt(summary['time_in_market'], '.2%')} |",
        f"| 成本占毛利比 | {_fmt(summary['cost_to_gross'], '.2%')} |",
        "",
        "## 成本拆分",
        "",
        f"- 佣金 {_fmt(summary['total_commission'], '.1f')}"
        f"　滑点 {_fmt(summary['total_slippage'], '.1f')}"
        f"　价差 {_fmt(summary['total_spread'], '.1f')}"
        f"　隔夜费 {_fmt(summary['total_swap'], '.1f')}",
    ]
    if summary["by_year"]:
        lines += ["", "## 按年", ""] + [
            f"- {y}: 净利 {_fmt(v, '.1f')}" for y, v in summary["by_year"].items()]
    if summary["by_hour"]:
        lines += ["", "## 按入场小时（UTC）", ""] + [
            f"- {h:->2}: 净利 {_fmt(v, '.1f')}" for h, v in summary["by_hour"].items()]
    if summary["by_side"]:
        lines += ["", "## 多空拆分", ""]
        for side, s in summary["by_side"].items():
            name = "多头" if side == "long" else "空头"
            lines.append(
                f"- {name}: {s['count']} 笔　净利 {_fmt(s['net'], '.1f')}"
                f"　胜率 {_fmt(s['win_rate'], '.2%')}")
    lines.append("")
    return "\n".join(lines)


@dataclass
class Manifest:
    """可复现性清单：同代码（git 提交）+ 同数据（快照哈希）+ 同参数/成本版本。

    2026-10-25 起追加引擎版本、策略、品种周期、数据区间与金额口径——试验台账
    （P4）按这些字段去重与追溯，缺一项就无法判断两次运行是否"同一次试验"。
    strategy_sha（T37/D23，2026-10-26）：策略代码指纹
    （mt5quant.research.cards.strategy_fingerprint）——报告可追溯到产生
    它的确切策略代码；缺省空串（未计算的旧调用方，如实为空）。
    """

    git_commit: str
    data_sha256: str
    params: dict
    cost_model_version: str
    created_utc: str
    engine_version: str = ""
    strategy: str = ""
    strategy_sha: str = ""
    symbol: str = ""
    timeframe: str = ""
    n_bars: int = 0
    data_first_time: str | None = None
    data_last_time: str | None = None
    contract_size: float | None = None
    volume_lots: float | None = None
    initial_capital: float | None = None
    extra: dict = field(default_factory=dict)


def current_git_commit() -> str:
    """当前 git 提交号；工作树/索引脏时附 "+dirty"（可复现性声称的前提）。

    取不到（无 git/非仓库/超时/非零返回码）返回 "unknown"。
    """
    try:
        repo_root = Path(__file__).resolve().parents[3]

        def _run(args):
            return subprocess.run(args, cwd=str(repo_root), capture_output=True,
                                  text=True, timeout=5)

        out = _run(["git", "rev-parse", "HEAD"])
        if out.returncode != 0:
            return "unknown"
        commit = out.stdout.strip()
        if not commit:
            return "unknown"
        dirty = False
        for probe in (["git", "diff", "--quiet"],               # 工作树 vs 索引
                      ["git", "diff", "--cached", "--quiet"]):  # 索引 vs HEAD
            if _run(probe).returncode != 0:
                dirty = True
                break
        return commit + ("+dirty" if dirty else "")
    except Exception:  # 任何失败都不阻塞报告产出
        return "unknown"


_DATA_SHA_V1_PREFIX = "v1:"
_DATA_SHA_V2_PREFIX = "v2:"


def data_sha256_v1(bars: pd.DataFrame) -> str:
    """旧版数据快照哈希（H-12 前的算法逐位复刻 + "v1:" 前缀）：
    time+OHLC 五列逐行哈希求和再 SHA——丢行序、不含 volume 等列。
    仅供迁移核对（旧台账裸 hex == 去掉前缀的本值）与对照测试；
    新代码一律用 data_sha256（v2）。"""
    cols = ["time", "open", "high", "low", "close"]
    row_hashes = pd.util.hash_pandas_object(bars[cols], index=False)
    total = int(row_hashes.sum())
    return _DATA_SHA_V1_PREFIX + hashlib.sha256(
        str(total).encode("utf-8")).hexdigest()


def _ts_token(v) -> str:
    """时间值的 canonical token：UTC 纳秒整数的 ISO 字面量（带 ns 位，
    np.datetime_as_string(unit='ns')；tz-aware 按 UTC 折算后统一表示；
    NaT → "NaT"）。列向量路径与单格路径同值必得同 token。"""
    ts = v if isinstance(v, pd.Timestamp) else pd.Timestamp(v)
    if pd.isna(ts):
        return '"NaT"'
    s = str(np.datetime_as_string(np.datetime64(ts.value, "ns"), unit="ns"))
    return '"' + s + '"'


def _cell_token(v) -> str:
    """单格值 → canonical JSON 字面量（严格 JSON：非有限数显式字符串标记
    "NaN"/"Infinity"/"-Infinity"，保证跨平台确定性；与 json.dumps 的表示
    逐字符一致——向量路径与单格路径同值必得同 token）。"""
    if v is None:
        return "null"
    if isinstance(v, (bool, np.bool_)):
        return "true" if bool(v) else "false"
    if isinstance(v, np.integer):
        return str(int(v))
    if isinstance(v, (np.floating, float)):
        f = float(v)
        if math.isfinite(f):
            return repr(f)                 # json.dumps 对 float 即 float.__repr__
        return '"NaN"' if math.isnan(f) else ('"Infinity"' if f > 0
                                              else '"-Infinity"')
    if isinstance(v, (pd.Timestamp, datetime)):
        return _ts_token(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bytes):
        return json.dumps(v.hex(), ensure_ascii=False)
    if isinstance(v, (list, tuple)):       # 不会出现在 bars 列里，防御性展开
        return "[" + ",".join(_cell_token(x) for x in v) + "]"
    return json.dumps(f"{type(v).__name__}:{v!r}", ensure_ascii=False)


def _column_tokens(s: pd.Series) -> list[str]:
    """整列 → 逐行 canonical JSON token（按 dtype 向量化；与逐格
    _cell_token 完全同值——大序列走快路径，语义不变）。"""
    if len(s) == 0:
        return []
    if pd.api.types.is_bool_dtype(s.dtype):
        return [("true" if x else "false") for x in s.tolist()]
    if pd.api.types.is_integer_dtype(s.dtype):
        return [str(x) for x in s.tolist()]
    if pd.api.types.is_float_dtype(s.dtype):
        out = []
        for f in s.tolist():                # tolist() → Python float
            if math.isfinite(f):
                out.append(repr(f))
            elif math.isnan(f):
                out.append('"NaN"')
            else:
                out.append('"Infinity"' if f > 0 else '"-Infinity"')
        return out
    if pd.api.types.is_datetime64_any_dtype(s.dtype):
        arr = (s.dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()
               if isinstance(s.dtype, pd.DatetimeTZDtype)
               else s.to_numpy())
        return ['"' + t + '"' for t in
                np.datetime_as_string(arr, unit="ns").tolist()]
    return [_cell_token(v) for v in s.tolist()]


def data_sha256(bars: pd.DataFrame) -> str:
    """数据快照哈希 v2（H-12；算法见模块 docstring）：**有序**逐行
    canonical JSON（行号进哈希，行交换即变）× bars **全部数据列**
    （tick_volume/spread/real_volume 等策略实际可读输入都进指纹），
    返回 "v2:" + sha256 hex。旧算法见 data_sha256_v1（"v1:" 前缀）。"""
    cols = sorted(str(c) for c in bars.columns)
    header = json.dumps(cols, ensure_ascii=False, separators=(",", ":"))
    tokens = [_column_tokens(bars[c]) for c in cols]
    parts = [header]
    for i, row in enumerate(zip(*tokens)):
        parts.append(f"[{i},{','.join(row)}]")
    return _DATA_SHA_V2_PREFIX + hashlib.sha256(
        "\n".join(parts).encode("utf-8")).hexdigest()


def _jsonable(obj):
    """递归把 NaN/inf 换成 None，保证落盘 JSON 严格合法。"""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return f if math.isfinite(f) else None
    return obj


def make_run_dir(base_out: str, symbol: str, timeframe: str, *, now=None) -> str:
    """为一次运行生成独占输出目录：{base}/{SYMBOL}_{TF}_{UTC戳}_{6位随机}（并创建）。"""
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(base_out, f"{symbol}_{timeframe.upper()}_{stamp}_{secrets.token_hex(3)}")
    os.makedirs(path, exist_ok=False)   # 撞名即抛：绝不覆盖既有运行
    return path


def build_manifest(result, *, cost_version: str = "v0", strategy: str = "",
                   strategy_sha: str = "",
                   extra: dict | None = None) -> Manifest:
    """从 BacktestResult 组装 Manifest（write_report 与试验台账共用）。

    strategy_sha（T37/D23）：策略代码指纹，由调用方（run_backtest）计算
    传入；缺省空串（不阻塞无指纹的旧调用方，如实为空）。
    """
    bars = result.bars
    times = pd.to_datetime(bars["time"]) if len(bars) else None
    return Manifest(
        git_commit=current_git_commit(),
        data_sha256=data_sha256(bars),
        params=dict(result.params),
        cost_model_version=cost_version,
        created_utc=datetime.now(timezone.utc).isoformat(),
        engine_version=ENGINE_VERSION,
        strategy=strategy,
        strategy_sha=strategy_sha,
        symbol=result.symbol,
        timeframe=result.timeframe or "",
        n_bars=int(len(bars)),
        data_first_time=times.iloc[0].isoformat() if times is not None else None,
        data_last_time=times.iloc[-1].isoformat() if times is not None else None,
        contract_size=getattr(result, "contract_size", None),
        volume_lots=getattr(result, "volume_lots", None),
        initial_capital=getattr(result, "initial_capital", None),
        extra=dict(extra or {}),
    )


def write_report(out_dir: str, result, *, cost_version: str = "v0",
                 strategy: str = "", strategy_sha: str = "",
                 extra: dict | None = None) -> str:
    """写 summary.json + manifest.json + report.md，返回 report.md 路径。

    out_dir 应为**本次运行独占**的目录（CLI 用 make_run_dir 生成）；
    多次运行写同一目录会互相覆盖，试验台账就丢了记录。
    """
    summary = summarize(result, cost_version=cost_version)
    manifest = build_manifest(result, cost_version=cost_version,
                              strategy=strategy, strategy_sha=strategy_sha,
                              extra=extra)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(_jsonable(summary), f, ensure_ascii=False, indent=2,
                  allow_nan=False)
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(_jsonable(asdict(manifest)), f, ensure_ascii=False, indent=2,
                  allow_nan=False)
    md_path = os.path.join(out_dir, "report.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(summary, result))
    return md_path
