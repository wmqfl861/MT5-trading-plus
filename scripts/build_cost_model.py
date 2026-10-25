"""构建点差模型缓存（T28/M8；T36/H2 补采样口径与止损越幅表）。

示例：
    .venv/Scripts/python.exe scripts/build_cost_model.py --data-root data
    .venv/Scripts/python.exe scripts/build_cost_model.py --symbols XAUUSD,BTCUSD
    .venv/Scripts/python.exe scripts/build_cost_model.py --overshoot          # 含越幅表

背景（第四轮审核 M8）：每次回测都从全部 tick（XAUUSD 7,780 万行、全列约
3.7 GB）重建点差模型。本脚本把它变成一次性构建：

- 只读 time_msc/bid/ask 三列（mt5data.ticks.load_ticks(columns=...)），按
  纽约本地小时构建相对点差模型（T20 口径，SpreadModelBuilder）；
- 写 {data-root}/costs/spread_model.json：各品种 tick 水位
  （rows/first_msc/last_msc，来自 mt5data.ticks.tick_watermark 只读 parquet
  元数据）+ 模型负载 + 模型 sha256；
- 读取方（run_backtest.build_costs）加载缓存并逐项比对水位：tick 增量后
  未重建 → 报错要求重跑本脚本，绝不静默沿用过期模型；
- --symbols 缺省 = tick 库下全部品种；无 tick 库的品种报错（不静默跳过）；
- 幂等：重跑直接覆盖旧缓存（水位按当前库重新记录）。

T36（H2）修订：
- **采样口径** --sampling（缺省 bar_open_m1，每个 M1 边界后第一笔 tick，
  近似时间加权、样本量只随分钟数增长；"tick" 保留旧口径作对比）；
- **逐月流式**：按月分区逐块读取（每块只载当月三列），SpreadModelBuilder /
  StopOvershootBuilder 顺序吸收——构建内存从"全库 tick"降到"单月 tick +
  分钟级样本/蓄水池"，且与一次性构建结果逐位一致（同一事件到达序）；
- 每小时记 n 与 n_days，打印 n_days 最小值与低于 config/sessions.toml
  [costs] min_days_per_hour（缺省 20）的小时（这些小时回测按 forbid 禁入）；
- **止损越幅表** --overshoot：StopOvershootModel（品种 × 纽约小时，越幅
  相对价格 p50/p90，首笔穿价口径，每小时蓄水池上限 200k 控内存）写进同一
  缓存文件（overshoot / overshoot_sha256 顶层键），缓存 version 升 2 并记
  sampling；读取方按需消费（run_backtest 的四重校验只看
  model/model_sha256/mode/watermarks，不受新增键影响）。
"""
import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 复用 run_backtest 装配（同 bt.py）

from mt5data.ticks import load_ticks, tick_watermark, ticks_dir  # noqa: E402
from mt5quant.costs import SpreadModelBuilder  # noqa: E402
from mt5quant.costs.spread import SAMPLINGS  # noqa: E402
from mt5quant.costs.overshoot import StopOvershootBuilder, StopOvershootModel  # noqa: E402
from run_backtest import (  # noqa: E402
    canonical_json_sha256, save_spread_model_cache, spread_model_cache_path,
)


def _month_windows(data_root: str, symbol: str) -> list[tuple[int, int, int, int]]:
    """枚举磁盘上的月份分区 → [(yyyy, mm, start_msc, end_msc)]（闭区间，升序）。"""
    d = ticks_dir(data_root, symbol)
    out: list[tuple[int, int, int, int]] = []
    if not os.path.isdir(d):
        return out
    for year in sorted(os.listdir(d)):
        if len(year) != 4 or not year.isdigit():
            continue
        ydir = os.path.join(d, year)
        if not os.path.isdir(ydir):
            continue
        for fn in sorted(os.listdir(ydir)):
            root, ext = os.path.splitext(fn)
            if ext == ".parquet" and len(root) == 2 and root.isdigit():
                start = _month_start_ms(int(year), int(root))
                out.append((int(year), int(root), start,
                            _month_start_ms(int(year), int(root) + 1) - 1))
    return out


def _month_start_ms(yyyy: int, mm: int) -> int:
    """月首的 epoch 毫秒（time_msc 是贴 UTC 标签的服务器时间，按 UTC 日历月分区）。"""
    if mm > 12:
        yyyy, mm = yyyy + 1, mm - 12
    return int(dt.datetime(yyyy, mm, 1).replace(tzinfo=dt.timezone.utc)
               .timestamp() * 1000)


def _min_days_threshold() -> int:
    """[costs] min_days_per_hour（缺省 20）——覆盖打印用，读不到按缺省。"""
    try:
        from mt5data.sessions import load_session_policy
        return int(load_session_policy().min_days_per_hour)
    except Exception as e:  # 配置缺失/工作目录不同：打印用缺省值，不阻塞构建
        print(f"· 读取 sessions.toml 失败（{type(e).__name__}），覆盖阈值按缺省 20")
        return 20


def _atomic_write_json(path: str, doc: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def build_cost_models(data_root: str, symbols, *,
                      spread_mode: str = "relative",
                      sampling: str = "bar_open_m1",
                      overshoot: bool = False,
                      out_path: str | None = None) -> dict:
    """（重）建点差模型缓存并原子写盘，返回写入的文档（dict）。

    symbols 为 None → 枚举 {data_root}/ticks 下全部品种；某品种无 tick 库 →
    ValueError（先跑 scripts/download_ticks.py）。逐月流式读取（只载当月
    time_msc/bid/ask 三列），overshoot=True 时同循环构建 StopOvershootModel
    并写进缓存（version 2 + sampling 标注 + overshoot/overshoot_sha256）。
    """
    if sampling not in SAMPLINGS:
        raise ValueError(f"sampling 必须是 {SAMPLINGS} 之一，收到 {sampling!r}")
    if symbols is None:
        base = os.path.join(data_root, "ticks")
        symbols = (sorted(os.listdir(base))
                   if os.path.isdir(base) else [])
        if not symbols:
            raise ValueError(
                f"{data_root} 下无任何 tick 库（{base} 不存在或为空）——"
                "先跑 scripts/download_ticks.py")
    if not symbols:
        raise ValueError("symbols 不能为空")
    models = []
    overshoot_models = []
    watermarks: dict[str, dict] = {}
    total_rows = 0
    threshold = _min_days_threshold()
    for sym in symbols:
        wm = tick_watermark(data_root, sym)
        if wm is None:
            raise ValueError(
                f"{sym} 无 tick 库（{ticks_dir(data_root, sym)}）——"
                "先跑 scripts/download_ticks.py 回补后再构建成本模型")
        windows = _month_windows(data_root, sym)
        if not windows:
            raise ValueError(
                f"{sym} 无按月分区 parquet（{ticks_dir(data_root, sym)}）——"
                "先跑 scripts/migrate_ticks_layout.py 或 download_ticks.py")
        print(f"· {sym}: {wm['rows']:,} 条 tick，逐月流式读取"
              f"（{len(windows)} 个月分区，口径 {sampling}"
              f"{' + 越幅' if overshoot else ''}）…")
        sb = SpreadModelBuilder(sym, mode=spread_mode, sampling=sampling)
        ob = StopOvershootBuilder(sym) if overshoot else None
        for yyyy, mm, msc_lo, msc_hi in windows:
            df = load_ticks(data_root, sym, start_msc=msc_lo, end_msc=msc_hi,
                            columns=["time_msc", "bid", "ask"])
            if df is None or len(df) == 0:
                continue
            sb.add_ticks(df)
            if ob is not None:
                ob.add_ticks(df)
            del df                       # 逐月释放：峰值只驻留单月三列
        model = sb.build()
        models.append(model)
        if ob is not None:
            try:
                overshoot_models.append(ob.build())
            except ValueError:
                # 全库无穿价事件（价格纹丝不动的小型库/静默品种）：空表 + 告警，
                # 不让单品种打断整批构建（空表查询回退 0 越幅）
                print(f"⚠ {sym}: 全库无穿价事件，越幅表按空统计（回测不加越幅）")
                overshoot_models.append(StopOvershootModel({sym: {}}))
        watermarks[sym] = wm
        total_rows += int(wm["rows"])
        table = model._per_symbol[sym]   # noqa: SLF001 — 摘要打印
        hours = len(table)
        thin = sorted(h for h, st in table.items() if st.n_days < threshold)
        min_days = min((st.n_days for st in table.values()), default=0)
        print(f"✓ {sym}: {hours}/24 个纽约小时有统计，n_days 最小 {min_days}"
              + (f"（低于 {threshold} 的小时 {thin}，回测按 forbid 禁入）"
                 if thin else ""))
    merged = models[0]
    if len(models) > 1:
        merged = merged.merge(*models[1:])
    path = out_path or spread_model_cache_path(data_root)
    doc = save_spread_model_cache(path, merged, watermarks)
    # T36：顶层键追加采样口径与（可选）越幅表，缓存版本升 2。run_backtest 的
    # 四重校验只读 model/model_sha256/mode/watermarks，不受这些键影响
    doc["version"] = 2
    doc["sampling"] = sampling
    if overshoot_models:
        om = overshoot_models[0]
        if len(overshoot_models) > 1:
            om = om.merge(*overshoot_models[1:])
        doc["overshoot"] = om.to_dict()
        doc["overshoot_sha256"] = canonical_json_sha256(doc["overshoot"])
    _atomic_write_json(path, doc)
    print(f"✓ 点差模型缓存已写 {path}"
          f"（{len(symbols)} 品种 / {total_rows:,} 条 tick / 口径 {spread_mode}"
          f" / 采样 {sampling} / version 2"
          f"{' / 含越幅表' if overshoot_models else ''}"
          f" / sha256 {doc['model_sha256'][:12]}…）")
    return doc


def main() -> int:
    ap = argparse.ArgumentParser(
        description="构建点差模型缓存 data/costs/spread_model.json（T28/M8、T36/H2）")
    ap.add_argument("--data-root", default="data")
    ap.add_argument("--symbols", default=None,
                    help="逗号分隔，如 XAUUSD,BTCUSD；缺省 = tick 库下全部品种")
    ap.add_argument("--spread-mode", choices=("relative", "absolute"),
                    default="relative",
                    help="点差口径（与 run_backtest --spread-mode 一致，默认相对）")
    ap.add_argument("--sampling", choices=SAMPLINGS, default="bar_open_m1",
                    help="采样口径（T36 缺省 bar_open_m1=每 M1 边界后第一笔 tick；"
                         "tick=旧口径，对比用）")
    ap.add_argument("--overshoot", action="store_true",
                    help="同时构建止损越幅表（品种×纽约小时 p50/p90，首笔穿价口径）"
                         "写进同一缓存文件")
    ap.add_argument("--out", default=None,
                    help="缓存文件路径（缺省 {data-root}/costs/spread_model.json）")
    args = ap.parse_args()
    symbols = ([s.strip() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else None)
    try:
        build_cost_models(args.data_root, symbols,
                          spread_mode=args.spread_mode, sampling=args.sampling,
                          overshoot=args.overshoot, out_path=args.out)
    except Exception as e:  # fail-fast：如实报出
        print(f"✗ {type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
