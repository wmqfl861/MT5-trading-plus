"""A 层回测 CLI。

示例：
    .venv/Scripts/python.exe scripts/run_backtest.py \
        --strategy mt5quant.strategies.library.buy_hold:BuyHold \
        --symbol XAUUSD --timeframe H1 --params '{}' \
        --data-root data --end 2026-09-01 --out data/reports/backtests

约定：
- --end 经 mt5data.lockbox.research_bars（锁箱语义：显式越过 cutoff 的请求
  直接报错，绝不静默截断）；无 lockbox.json 时返回全量，行为与直读一致。
- 金额口径：contract_size 取 --contract-size，否则读
  {data-root}/specs/latest.json（scripts/snapshot_specs.py 产出）；两者都没有
  → 报错退出（绝不静默按 1 折算——那会让盈亏差几百到十万倍）。
- 成本：--costs auto（默认）＝tick 实测点差+滑点，点差默认**相对口径**
  （--spread-mode relative，R1：p90 of (ask−bid)/mid 按小时，回测时按成交
  基准价缩放；--spread-mode absolute 为旧口径）；佣金与隔夜费优先
  {data-root}/costs/calibrated.json（scripts/calibrate_costs.py 实测），其次
  品种快照推算（SwapModel.from_snapshot）；两者都没有 → 报错，除非显式
  --allow-zero-swap（零隔夜费会写进 manifest）。--costs zero 用零成本占位。
- 点差模型缓存（T28/M8）：--costs auto 优先加载 {data-root}/costs/
  spread_model.json（scripts/build_cost_model.py 产出，按 tick 水位失效），
  不再每次回测重读全部原始 tick（XAUUSD 7,780 万行全列约 3.7 GB）。缓存
  缺失 → 明确报错指路（加 --build-cost-model 可本次运行内从 tick 现建——
  选择"缺缓存报错 + 显式 flag"而非静默直建：批量/计划任务里的隐性 4 GB
  加载正是 M8 要消除的）；水位变化而模型未重建 → 同样报错，绝不静默用
  旧模型。函数级 build_costs 保留无缓存直建回退（bt.py / 对账脚本 / 测试
  的兼容路径，行为与旧版一致）。
- 时钟校验（R5）：读 {data-root}/server_clock.json 最新有效样本，与配置的
  server_utc_offset_hours 比对，超限拒绝运行；无有效样本仅警告。
- 禁入窗（T20）：默认加载 config/sessions.toml（SessionPolicy，与实盘风控
  共用的唯一定义源），禁入时段的开仓信号推迟到下一个允许的 bar 开盘；
  其 sha256 写进 manifest 与 cost_model_version。点差模型按纽约本地小时
  分桶，缺统计小时按 missing_hour_policy="forbid" 禁入并把 miss 标注写进
  cost_model_version。
- 定手数（T23/M2）：**默认按风险定手数**——--risk-per-trade 缺省 0.005
  （单笔风险 0.5% 资金；引擎按止损距离折算手数，见 mt5quant.sizing）。
  语义（resolve_lots_mode）：
    * --volume-lots 显式给出 → 固定手数（旧口径；与默认风险值不冲突）；
    * --risk-per-trade 0 → 固定手数（手数取 --volume-lots，未给则 1.0）；
    * --risk-per-trade >0 且 --volume-lots 同时显式给出 → 拒绝（口径二义）；
    * 两者都未给 → 风险定手数 0.005。
  风险定手数需要品种 vol_min/step/max：自动读 {data-root}/specs/latest.json
  （mt5quant.sizing.load_volume_limits，缺数据报错指路 snapshot_specs），
  定手数方式写进 manifest（extra.sizing）与 summary（skipped_zero_lots
  为跳过的零手数入场计数）。
- 每次运行写入独占目录 {out}/{SYMBOL}_{TF}_{UTC戳}_{随机}，不覆盖历史运行。
"""
import argparse
import hashlib
import importlib
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5data.clock import (  # noqa: E402
    ClockMismatchError, check_server_offset, latest_valid_sample,
)
from mt5data.lockbox import research_bars  # noqa: E402
from mt5data.sessions import SessionPolicy, load_session_policy  # noqa: E402
from mt5data.store import TIMEFRAME_SECONDS  # noqa: E402
from mt5data.ticks import load_ticks, tick_watermark  # noqa: E402
from mt5quant.backtest.engine_a import run_backtest  # noqa: E402
from mt5quant.backtest.engine_b import run_backtest_ticks  # noqa: E402
from mt5quant.backtest.report import make_run_dir, summarize, write_report  # noqa: E402
from mt5quant.sizing import RiskSizing, load_volume_limits  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_PATH = REPO_ROOT / "config" / "sessions.toml"


def sessions_sha256(path: str) -> str:
    """config/sessions.toml 的 sha256（写进 manifest 与 cost_model_version）。"""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class ZeroCosts:
    """零成本占位：实现 engine_a.Costs Protocol。"""

    def spread_p90(self, symbol, hour_ny, *, price=None):
        return 0.0

    def slippage(self, symbol, hour_ny, *, price=None):
        return 0.0

    def commission_per_lot(self, symbol):
        return 0.0

    def overnight_fee(self, symbol, side, from_ts, to_ts):
        return 0.0


def resolve_contract_size(data_root: str, symbol: str, override: float | None) -> float:
    """合约大小：CLI 覆盖 > {data_root}/specs/latest.json；都没有 → ValueError。"""
    if override is not None:
        if not override > 0:
            raise ValueError(f"--contract-size 必须为正，收到 {override}")
        return float(override)
    snap_path = os.path.join(data_root, "specs", "latest.json")
    if os.path.exists(snap_path):
        with open(snap_path, encoding="utf-8") as f:
            snap = json.load(f)
        spec = snap.get(symbol) if isinstance(snap, dict) else None
        cs = spec.get("contract_size") if isinstance(spec, dict) else None
        if isinstance(cs, (int, float)) and cs > 0:
            return float(cs)
    raise ValueError(
        f"{symbol} 合约大小未知：先跑 scripts/snapshot_specs.py 生成 "
        f"{snap_path}，或显式传 --contract-size（XAUUSD 常见 100、EURUSD 100000）"
    )


# ------------------------------------------------ T23（M2）：定手数口径解析
#
# CLI 语义（--risk-per-trade 与 --volume-lots，两者 argparse 默认均为 None）：
#   * 都未给               → 风险定手数，risk_fraction=0.005（缺省即新口径）
#   * 仅 --volume-lots     → 固定手数（旧口径；显式手数优先于缺省风险值）
#   * 仅 --risk-per-trade>0→ 风险定手数（该比例）
#   * 仅 --risk-per-trade 0→ 固定手数（手数取 --volume-lots，未给则 1.0）
#   * 两者同时显式给出且风险>0 → 拒绝（口径二义：不知道该听谁的）
# run_backtest 与 bt.py（screen/gate）共用本函数，保证两条路径同一语义。

DEFAULT_RISK_PER_TRADE = 0.005


def resolve_lots_mode(risk_per_trade: float | None,
                      volume_lots: float | None) -> tuple[float | None,
                                                           RiskSizing | None,
                                                           dict]:
    """把 CLI 手数参数解析成引擎入参（volume_lots, sizing, 说明 dict）。

    说明 dict 写进 manifest（run_backtest）/台账 metrics（bt）：
    {"mode": "risk", ...} 或 {"mode": "fixed", ...}。
    """
    if volume_lots is not None:
        if not (isinstance(volume_lots, (int, float))
                and math.isfinite(volume_lots) and volume_lots > 0):
            raise ValueError(f"--volume-lots 必须是有限正数，收到 {volume_lots!r}")
    if risk_per_trade is not None:
        if not (isinstance(risk_per_trade, (int, float))
                and math.isfinite(risk_per_trade) and risk_per_trade >= 0):
            raise ValueError(
                f"--risk-per-trade 必须是非负有限数（0 = 改用固定手数），"
                f"收到 {risk_per_trade!r}")
    explicit_risk = risk_per_trade is not None and risk_per_trade > 0
    if explicit_risk and volume_lots is not None:
        raise ValueError(
            f"--risk-per-trade {risk_per_trade} 与 --volume-lots "
            f"{volume_lots} 互斥：按风险定手数（--risk-per-trade，缺省 "
            f"{DEFAULT_RISK_PER_TRADE}）或固定手数（--volume-lots）二选一"
            "（--risk-per-trade 0 = 显式选固定手数）")
    if volume_lots is not None:
        return volume_lots, None, {
            "mode": "fixed", "volume_lots": float(volume_lots)}
    if explicit_risk:
        return None, RiskSizing(risk_fraction=float(risk_per_trade)), {
            "mode": "risk", "risk_fraction": float(risk_per_trade),
            "basis": "initial", "fallback_stop_atr": 1.0}
    if risk_per_trade is not None and risk_per_trade == 0:
        return 1.0, None, {
            "mode": "fixed", "volume_lots": 1.0,
            "note": "--risk-per-trade 0 = 固定手数（未显式给 --volume-lots，取 1.0）"}
    return None, RiskSizing(risk_fraction=DEFAULT_RISK_PER_TRADE), {
        "mode": "risk", "risk_fraction": DEFAULT_RISK_PER_TRADE,
        "basis": "initial", "fallback_stop_atr": 1.0}


def check_clock(data_root: str, *, tol_s: float = 120.0) -> None:
    """R5：最新有效时钟样本 vs 配置的 server_utc_offset_hours；无样本仅警告。"""
    sample = latest_valid_sample(os.path.join(data_root, "server_clock.json"))
    expected = 0.0
    for cfg_name in ("config.json", "gateway.json"):
        p = os.path.join(os.getcwd(), cfg_name)
        if os.path.exists(p):
            from mt5gw import load
            expected = load(p).server_utc_offset_hours
            break
    if sample is None:
        print(f"⚠ {data_root}/server_clock.json 无有效时钟样本（休市或未测），"
              f"跳过时区校验；开市后跑 scripts/measure_server_clock.py")
        return
    check_server_offset(sample["offset_s"], expected, tol_s=tol_s)
    print(f"✓ 时钟校验：实测偏移 {sample['offset_s']:+.0f}s 与配置 "
          f"{expected:g}h 一致（样本 {sample['wall_utc']}）")


def _load_calibrated(data_root: str) -> dict | None:
    p = os.path.join(data_root, "costs", "calibrated.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------ T28（M8）：点差模型缓存
#
# 缓存文件 {data-root}/costs/spread_model.json 由 scripts/build_cost_model.py
# 产出（本模块的 save/load 是同一读写的另一入口，供 --build-cost-model 与
# 测试使用）。IO/校验逻辑放这里而不是 build_cost_model.py，是因为
# build_costs 在函数级就要消费它，而 bt.py / 测试经 importlib 装载本模块时
# scripts 目录不在 sys.path 上（不能顶层 import 兄弟脚本）。

REBUILD_HINT = ("重跑 scripts/build_cost_model.py（或回测时加 --build-cost-model"
                " 本次现建）")


def spread_model_cache_path(data_root: str) -> str:
    return os.path.join(data_root, "costs", "spread_model.json")


def canonical_json_sha256(payload: dict) -> str:
    """模型负载的规范 sha256（sort_keys + 紧致分隔符，任何等价 dict 同哈希）。"""
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()


def save_spread_model_cache(path: str, model, watermarks: dict) -> dict:
    """原子写缓存：{version, mode, watermarks, model, model_sha256, built_at_utc}。

    watermarks：{symbol: {rows, first_msc, last_msc}}（mt5data.ticks.
    tick_watermark 的产出）——读取方逐项比对当前水位，不一致即要求重建。
    """
    payload = model.to_dict()
    doc = {
        "version": 1,
        "mode": model.mode,
        "watermarks": {s: dict(w) for s, w in watermarks.items()},
        "model": payload,
        "model_sha256": canonical_json_sha256(payload),
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return doc


def load_spread_model_cache(path: str, symbol: str, *,
                            spread_mode: str,
                            watermark: dict | None):
    """读缓存并做四重校验，任何一处不过 → ValueError（提示重建，绝不静默）。

    1) 文件可读、结构完整；2) model_sha256 与模型负载一致（防手改/半截写）；
    3) 口径（mode）与请求一致；4) 品种在模型中且 tick 水位（rows/first/last）
    与当前库逐项一致。watermark=None（tick 库没了）而缓存还在属环境异常，同报错。
    """
    from mt5quant.costs import SpreadModel
    from mt5quant.costs.spread import BUCKET_TZ, SpreadStat

    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        payload = doc["model"]
        sha = doc["model_sha256"]
        wms = doc["watermarks"]
        cached_mode = doc["mode"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ValueError(f"点差缓存 {path} 不可读或结构不完整（{e}）——{REBUILD_HINT}") from e
    if canonical_json_sha256(payload) != sha:
        raise ValueError(
            f"点差缓存 {path} 的 model_sha256 与模型内容不一致"
            f"（文件被改动或写坏）——{REBUILD_HINT}")
    if cached_mode != spread_mode:
        raise ValueError(
            f"点差缓存口径为 {cached_mode}，本次请求 {spread_mode}——"
            f"{REBUILD_HINT} 并加 --spread-mode {spread_mode}")
    # 与 SpreadModel.load 相同的口径防线（此处直接从负载重建，不经文件）
    if payload.get("bucket_tz") != BUCKET_TZ:
        raise ValueError(
            f"点差缓存 bucket_tz={payload.get('bucket_tz')!r}"
            f"（缺标注=UTC 分桶的旧文件），与纽约本地小时口径不兼容——{REBUILD_HINT}")
    try:
        per = {s: {int(h): SpreadStat(**st) for h, st in hours.items()}
               for s, hours in payload["per_symbol"].items()}
        overall = {s: float(v) for s, v in payload["overall_median"].items()}
        model = SpreadModel(per, overall, mode=cached_mode)
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"点差缓存 {path} 模型负载非法（{e}）——{REBUILD_HINT}") from e
    if symbol not in per:
        raise ValueError(
            f"{symbol} 不在点差缓存中（缓存品种：{sorted(per)}）——{REBUILD_HINT}"
            f" --symbols {symbol}")
    wm_cached = wms.get(symbol)
    if wm_cached is None:
        raise ValueError(f"{symbol} 的 tick 水位不在点差缓存中——{REBUILD_HINT}")
    if watermark is None:
        raise ValueError(
            f"{symbol} 当前无 tick 库却存在点差缓存（库被移动/删除？）——"
            f"先确认数据目录，再{REBUILD_HINT}")
    for k in ("rows", "first_msc", "last_msc"):
        if wm_cached.get(k) != watermark.get(k):
            raise ValueError(
                f"{symbol} tick 水位已变化（缓存 {k}={wm_cached.get(k)!r}，"
                f"当前 {watermark.get(k)!r}）——模型可能过期，{REBUILD_HINT}；"
                "绝不静默沿用旧模型")
    return model


def build_costs(data_root: str, symbol: str, spread_fraction: float,
                commission_per_lot: float = 0.0, *,
                allow_zero_swap: bool = False,
                spread_mode: str = "relative",
                sessions_sha: str | None = None,
                cache: bool = True) -> tuple:
    """tick 实测点差 + 佣金/隔夜费（calibrated.json > 品种快照 > 报错）。

    T28/M8：优先加载点差模型缓存 {data-root}/costs/spread_model.json（按
    tick 水位校验，绝不静默沿用过期模型），**不再为每次回测重读全部原始
    tick**（XAUUSD 全库全列约 3.7 GB）。CLI 主路径缺缓存先报错（见 main 的
    --build-cost-model）；本函数保留"无缓存→直建"回退是给 bt.py / 对账
    脚本 / 测试的兼容路径（cache=False 可强制直建）。

    spread_mode（R1/T3）：relative（默认）＝(ask−bid)/mid 按**纽约本地小时**
    分位（T20：换日尖峰不被 DST 摊薄），引擎按成交基准价缩放——近期点差
    结构可外推到价格水平不同的历史；absolute＝旧口径（价格单位，忽略
    price）。cost_version 里写明 rel/abs。

    隔夜费（T20/H1）：calibrated 与快照两条路径的**三倍日一律取快照
    swap_rollover3days**（经 mt5_to_py_weekday），计费时刻来自 SwapRule
    默认（17:00 America/New_York，DST 感知）——校准费率按同一规则复算，
    不再用 frozenset() 摊薄。

    sessions_sha（T20）：config/sessions.toml 的 sha256；给出时写进
    cost_version（sess[前 8 位]）。缺统计小时按 missing_hour_policy=forbid
    标注（missNY[...]），由 entry_policy_for 并入引擎禁入。

    返回 (costs, cost_version)。佣金优先 calibrated 实测，其次 CLI 显式值；
    隔夜费优先 calibrated 实测费率（free_of_swap=true 属合法实测零费），其次
    快照推算；两者都没有且未 --allow-zero-swap → ValueError（禁止静默按 0）。
    """
    from mt5quant.costs import (
        CommissionModel, Costs, SlippageModel, SpreadModel, SwapModel,
        SwapRule,
    )
    from mt5quant.costs.swap import mt5_to_py_weekday

    cache_path = spread_model_cache_path(data_root)
    wm = tick_watermark(data_root, symbol)   # 只读 parquet 元数据，不载数据行
    used_cache = False
    if cache and wm is not None and os.path.exists(cache_path):
        spread = load_spread_model_cache(cache_path, symbol,
                                         spread_mode=spread_mode,
                                         watermark=wm)
        used_cache = True
    else:
        if cache and wm is None and os.path.exists(cache_path):
            raise ValueError(
                f"{symbol} 无 tick 库却存在点差缓存 {cache_path}——环境异常，"
                f"请检查数据目录；确认后可{REBUILD_HINT}")
        ticks = load_ticks(data_root, symbol,
                           columns=["time_msc", "bid", "ask"])
        if ticks is None or len(ticks) == 0:
            print(f"⚠ {symbol} 无 tick 数据，退回零成本；"
                  f"先跑 scripts/download_ticks.py 可启用实测点差/滑点")
            return ZeroCosts(), "zero-v0(no-ticks)"
        spread = SpreadModel.from_ticks(symbol, ticks, mode=spread_mode)
    slip = SlippageModel(spread, spread_fraction=spread_fraction)
    mode_tag = "rel" if spread_mode == "relative" else "abs"

    calibrated = _load_calibrated(data_root)
    cal_sym = None
    if calibrated and symbol in calibrated.get("symbols", {}):
        cal_sym = calibrated["symbols"][symbol]

    # ---- 佣金：calibrated 实测 > CLI 显式值（默认 0，标准账户点差内含属合法） ----
    if cal_sym and cal_sym.get("commission", {}).get("round_trip_per_lot") is not None:
        rt = float(cal_sym["commission"]["round_trip_per_lot"])
        commission = CommissionModel(default_per_lot=rt)
        comm_tag = f"comm{rt:g}-calibrated"
    else:
        commission = CommissionModel(default_per_lot=commission_per_lot)
        comm_tag = f"comm{commission_per_lot:g}-cli"

    # ---- 隔夜费：calibrated 实测费率 > 快照推算 > 报错/显式零 ----
    swap = None
    swap_tag = "swap?"
    cal_sw = cal_sym.get("swap") if cal_sym else None
    cal_rate = None
    if cal_sw:
        lng_r = cal_sw.get("long", {}).get("per_lot_per_night")
        sht_r = cal_sw.get("short", {}).get("per_lot_per_night")
        if lng_r is not None or sht_r is not None:
            cal_rate = (lng_r if lng_r is not None else 0.0,
                        sht_r if sht_r is not None else 0.0)
    if cal_rate is not None:
        # T20：三倍日一律取快照 swap_rollover3days——校准费率是"每夜"口径，
        # 回测按同一规则（含 ×3）复算才与实测对上（探针 P3 的修复）。
        # 快照缺品种/字段 → 如实报错，不静默按无三倍日。
        snap_entry = _snapshot_swap_entry(data_root, symbol)
        triple_py = mt5_to_py_weekday(snap_entry["swap_rollover3days"])
        swap = SwapModel(per_symbol={symbol: SwapRule(
            long_per_day=cal_rate[0], short_per_day=cal_rate[1],
            triple_weekdays=frozenset({triple_py}))})
        swap_tag = f"swap-cal(tri{triple_py})"
    elif cal_sw and cal_sw.get("free_of_swap") is True:
        swap = SwapModel(per_symbol={symbol: SwapRule(
            long_per_day=0.0, short_per_day=0.0)})
        swap_tag = "swap0-calibrated(free)"
    else:
        snap_path = os.path.join(data_root, "specs", "latest.json")
        try:
            with open(snap_path, encoding="utf-8") as f:
                snap = json.load(f)
            swap = SwapModel.from_snapshot(snap, [symbol])
            swap_tag = "swap-snapshot"
        except (OSError, KeyError, NotImplementedError, ValueError) as e:
            if allow_zero_swap:
                swap = SwapModel(per_symbol={symbol: SwapRule(
                    long_per_day=0.0, short_per_day=0.0)})
                swap_tag = "swap0-explicit-allow"
                print(f"⚠ 隔夜费无法推算（{e}），--allow-zero-swap 显式按 0 计"
                      "（已写进 manifest）")
            else:
                raise ValueError(
                    f"{symbol} 隔夜费未知（calibrated.json 无实测、快照推算失败：{e}）。"
                    "先跑 scripts/calibrate_costs.py / scripts/snapshot_specs.py，"
                    "或显式 --allow-zero-swap（会记录在 manifest）") from e

    # ---- T20：缺统计小时（missing_hour_policy=forbid）写进 cost_version ----
    missing = spread.missing_hours(symbol)
    if missing:
        if len(missing) > 8:
            miss_tag = f"-missNY[{len(missing)}h]"
        else:
            miss_tag = "-missNY[" + ",".join(str(h) for h in missing) + "]"
    else:
        miss_tag = ""
    sess_tag = f"-sess[{sessions_sha[:8]}]" if sessions_sha else ""

    costs = Costs(spread=spread, slippage_model=slip, commission=commission,
                  swap=swap)
    src = (f"缓存命中，tick 水位一致（{wm['rows']:,} 条实测）" if used_cache
           else f"tick {len(ticks):,} 条实测点差")
    print(f"✓ 成本：{src}（{mode_tag}@NY p90"
          f"{'×成交价' if mode_tag == 'rel' else ''}）；{comm_tag}；{swap_tag}"
          + (f"；缺统计 {len(missing)} 个纽约小时（forbid 禁入）" if missing else ""))
    return (costs,
            f"tick-spread-p90[{mode_tag}@NY]+slip{spread_fraction}"
            f"-{comm_tag}-{swap_tag}{miss_tag}{sess_tag}")


def _snapshot_swap_entry(data_root: str, symbol: str) -> dict:
    """读快照里 symbol 的条目（calibrated 三倍日来源）；缺文件/品种/字段即抛。"""
    snap_path = os.path.join(data_root, "specs", "latest.json")
    try:
        with open(snap_path, encoding="utf-8") as f:
            snap = json.load(f)
        entry = snap[symbol]
    except (OSError, KeyError) as e:
        raise ValueError(
            f"{symbol} 的 calibrated 隔夜费率需要快照的 swap_rollover3days"
            f"（三倍日，T20 不再按无三倍日摊薄），读 {snap_path} 失败：{e}"
            "——先跑 scripts/snapshot_specs.py") from e
    if "swap_rollover3days" not in entry:
        raise ValueError(
            f"{symbol} 快照条目缺 swap_rollover3days（旧快照），"
            f"重跑 scripts/snapshot_specs.py")
    return entry


def entry_policy_for(costs, symbol: str, base_policy: SessionPolicy):
    """把点差模型的缺统计小时并入禁入策略（T20：missing_hour_policy=forbid）。

    costs 无 spread 属性（ZeroCosts 等）或策略不是 forbid → 原样返回。
    """
    if base_policy is None or base_policy.missing_hour_policy != "forbid":
        return base_policy
    spread = getattr(costs, "spread", None)
    if spread is None:
        return base_policy
    missing = spread.missing_hours(symbol)
    if not missing:
        return base_policy
    return base_policy.with_forbidden_hours({symbol: missing})


def load_strategy(path: str):
    """按 "module:Class" 装载策略类。"""
    try:
        module_name, class_name = path.split(":", 1)
        mod = importlib.import_module(module_name)
        return getattr(mod, class_name)
    except (ValueError, ImportError, AttributeError) as e:
        raise ValueError(f"无法装载策略 {path!r}：{e}") from e


def load_series(data_root, symbol, timeframe, end):
    """读序列：经锁箱加载器（end 越过 cutoff 直接报错，绝不静默截断）。

    end 语义与 research_bars 一致：保留 time <= end 的行（含端点）——
    注意 end 时刻"开盘"的那根 bar 会整根纳入（含其整段 OHLC）；
    做切分严谨的样本外研究时请传 bar 的收盘时刻。
    """
    import pandas as pd

    end_ts = pd.Timestamp(end) if end else None
    if end_ts is not None and end_ts.tzinfo is not None:
        raise ValueError("--end 只接受不带时区的 UTC 时间（如 2026-09-01）")
    df = research_bars(data_root, symbol, timeframe, end=end_ts)
    if df is None or len(df) == 0:
        suffix = (f"（--end {end} 截断后为空或序列不存在）" if end
                  else "（先跑 scripts/download_bars.py）")
        raise ValueError(f"无可用数据：{symbol} {timeframe}{suffix}")
    return df


def main() -> int:
    ap = argparse.ArgumentParser(description="跑 A 层回测并出标准报告")
    ap.add_argument("--strategy", required=True,
                    help="module:Class，如 mt5quant.strategies.library.buy_hold:BuyHold")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--timeframe", required=True,
                    help="M1/H1/D1 等（TIMEFRAME_SECONDS 之一）")
    ap.add_argument("--params", default="{}", help='JSON 字符串，如 \'{"k":1}\'')
    ap.add_argument("--data-root", default="data")
    ap.add_argument("--end", default=None,
                    help="只用该 UTC 时刻及之前的数据（naive UTC；越过锁箱 cutoff 会报错）")
    ap.add_argument("--out", default="data/reports/backtests")
    ap.add_argument("--risk-per-trade", type=float, default=None,
                    help="按风险定手数：单笔风险占资金比例（缺省 0.005=0.5%%；"
                         "传 0 = 改用 --volume-lots 固定手数）。需快照的 "
                         "volume_min/step/max，缺则报错指路 snapshot_specs")
    ap.add_argument("--volume-lots", type=float, default=None,
                    help="固定手数（旧口径）；显式给出即用固定手数，与 "
                         "--risk-per-trade>0 同时给会拒绝")
    ap.add_argument("--initial-capital", type=float, default=10_000.0)
    ap.add_argument("--costs", choices=("auto", "zero"), default="auto",
                    help="auto=本地 tick 实测点差+滑点（佣金/隔夜未配置按 0 并说明）；zero=零成本")
    ap.add_argument("--spread-fraction", type=float, default=0.1,
                    help="--costs auto 时的滑点比例（×p90 点差）")
    ap.add_argument("--spread-mode", choices=("relative", "absolute"),
                    default="relative",
                    help="点差口径：relative=(ask−bid)/mid 按小时 p90，回测按成交基准价"
                         "缩放（R1 默认）；absolute=旧口径（价格单位，忽略 price）")
    ap.add_argument("--commission", type=float, default=0.0,
                    help="每手来回佣金（账户货币），--costs auto 时无 calibrated 实测则用此值")
    ap.add_argument("--engine", choices=("a", "b"), default="a",
                    help="a=向量化；b=tick 级事件驱动（需 tick 库覆盖回测区间）")
    ap.add_argument("--latency-ms", type=int, default=0,
                    help="--engine b：信号 bar 收盘后的执行延迟（毫秒）")
    ap.add_argument("--slippage-price", type=float, default=0.0,
                    help="--engine b：成交价叠加的额外不利滑点（价格单位）")
    ap.add_argument("--allow-zero-swap", action="store_true",
                    help="隔夜费推算不出时显式按 0 计（记入 manifest；默认报错）")
    ap.add_argument("--contract-size", type=float, default=None,
                    help="合约大小覆盖；缺省读 {data-root}/specs/latest.json")
    ap.add_argument("--cost-version", default=None,
                    help="成本模型版本（写进 manifest；缺省按 --costs 自动生成）")
    ap.add_argument("--build-cost-model", action="store_true",
                    help="--costs auto 时本次运行内（重）建点差模型缓存 "
                         "{data-root}/costs/spread_model.json（等价先跑 "
                         "scripts/build_cost_model.py；XAUUSD 全库约数分钟）。"
                         "缺省要求缓存已存在——缺缓存直接报错，不静默重读全部 tick")
    args = ap.parse_args()

    if args.timeframe.upper() not in TIMEFRAME_SECONDS:
        print(f"✗ 未知周期：{args.timeframe}")
        return 1
    try:
        params = json.loads(args.params)
    except ValueError as e:
        print(f"✗ --params 不是合法 JSON：{e}")
        return 1
    try:
        strategy_cls = load_strategy(args.strategy)
        contract_size = resolve_contract_size(args.data_root, args.symbol,
                                              args.contract_size)
        # T23：定手数口径（默认风险定手数 0.005；见 resolve_lots_mode 语义）
        lots, sizing, sizing_info = resolve_lots_mode(args.risk_per_trade,
                                                      args.volume_lots)
        if sizing is not None:
            vol_lo, vol_st, vol_hi = load_volume_limits(args.data_root,
                                                        args.symbol)
            sizing_info.update(vol_min=vol_lo, vol_step=vol_st, vol_max=vol_hi)
            print(f"✓ 定手数：按风险 {sizing.risk_fraction:.4%}"
                  f"（basis={sizing.basis}，vol {vol_lo}/{vol_st}/{vol_hi}）")
        else:
            print(f"✓ 定手数：固定 {lots} 手")
        check_clock(args.data_root)
        bars = load_series(args.data_root, args.symbol, args.timeframe.upper(),
                           args.end)
        # T20：禁入窗唯一来源 config/sessions.toml（与实盘风控共用）
        sess_policy = load_session_policy(str(SESSIONS_PATH))
        sess_sha = sessions_sha256(str(SESSIONS_PATH))
        if args.costs == "auto":
            # T28/M8：CLI 主路径缺缓存先报错（不静默重读全部 tick——那正是
            # M8 要消除的 4 GB 隐性加载）；--build-cost-model 为显式直建通道。
            cache_path = spread_model_cache_path(args.data_root)
            if args.build_cost_model:
                from build_cost_model import build_cost_models  # scripts 目录
                build_cost_models(args.data_root, [args.symbol],
                                  spread_mode=args.spread_mode)
            elif not os.path.exists(cache_path):
                raise ValueError(
                    f"缺 点差模型缓存 {cache_path}：先跑 "
                    f"scripts/build_cost_model.py --data-root {args.data_root} "
                    f"--symbols {args.symbol}"
                    f"（或本次运行加 --build-cost-model 从 tick 现建）")
            costs, cost_version = build_costs(
                args.data_root, args.symbol, args.spread_fraction,
                args.commission, allow_zero_swap=args.allow_zero_swap,
                spread_mode=args.spread_mode, sessions_sha=sess_sha)
        else:
            costs, cost_version = ZeroCosts(), "zero-v0"
        entry_policy = entry_policy_for(costs, args.symbol, sess_policy)
        if args.cost_version:
            cost_version = args.cost_version
        if args.engine == "b":
            result = run_backtest_ticks(
                strategy_cls(), bars,
                lambda start_msc, end_msc: load_ticks(
                    args.data_root, args.symbol,
                    start_msc=start_msc, end_msc=end_msc),
                costs, symbol=args.symbol, params=params,
                contract_size=contract_size,
                volume_lots=lots,
                sizing=sizing,
                vol_min=sizing_info.get("vol_min") if sizing else None,
                vol_step=sizing_info.get("vol_step") if sizing else None,
                vol_max=sizing_info.get("vol_max") if sizing else None,
                initial_capital=args.initial_capital,
                timeframe=args.timeframe.upper(),
                latency_ms=args.latency_ms,
                slippage_price=args.slippage_price,
                session_policy=entry_policy,
            )
        else:
            result = run_backtest(
                strategy_cls(), bars, costs, symbol=args.symbol,
                params=params, contract_size=contract_size,
                volume_lots=lots, sizing=sizing,
                vol_min=sizing_info.get("vol_min") if sizing else None,
                vol_step=sizing_info.get("vol_step") if sizing else None,
                vol_max=sizing_info.get("vol_max") if sizing else None,
                initial_capital=args.initial_capital,
                timeframe=args.timeframe.upper(),
                session_policy=entry_policy,
            )
        summary = summarize(result, cost_version=cost_version)
        run_dir = make_run_dir(args.out, args.symbol, args.timeframe.upper())
        report_path = write_report(
            run_dir, result, cost_version=cost_version, strategy=args.strategy,
            extra={"end": args.end, "costs": args.costs,
                   "spread_fraction": args.spread_fraction,
                   "spread_mode": args.spread_mode,
                   "commission_per_lot": args.commission,
                   "allow_zero_swap": args.allow_zero_swap,
                   "engine": args.engine, "latency_ms": args.latency_ms,
                   "slippage_price": args.slippage_price,
                   "sizing": sizing_info,      # T23：定手数方式进 manifest
                   "sessions_path": str(SESSIONS_PATH),
                   "sessions_sha256": sess_sha})
    except Exception as e:  # fail-fast：任何错误如实报出
        print(f"✗ {type(e).__name__}: {e}")
        return 1

    lots_note = (f"风险{sizing_info['risk_fraction']:.4%}"
                 if sizing_info["mode"] == "risk"
                 else f"固定{sizing_info['volume_lots']:g}手")
    print(f"✓ {args.symbol} {args.timeframe.upper()} "
          f"bars={len(result.bars)} trades={summary['trade_count']} "
          f"合约大小={contract_size:g} 手数={lots_note} "
          f"净利={summary['net_profit']:.2f} "
          f"回撤={summary['max_drawdown']:.2%}")
    if summary.get("skipped_zero_lots"):
        print(f"⚠ {summary['skipped_zero_lots']} 次入场因手数低于 vol_min 跳过"
              f"（skipped_zero_lots，详见 summary.json）")
    print(f"报告：{report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
