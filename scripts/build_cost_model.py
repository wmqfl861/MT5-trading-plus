"""构建点差模型缓存（T28/M8）。

示例：
    .venv/Scripts/python.exe scripts/build_cost_model.py --data-root data
    .venv/Scripts/python.exe scripts/build_cost_model.py --symbols XAUUSD,BTCUSD

背景（第四轮审核 M8）：每次回测都从全部 tick（XAUUSD 7,780 万行、全列约
3.7 GB）重建点差模型。本脚本把它变成一次性构建：

- 只读 time_msc/bid/ask 三列（mt5data.ticks.load_ticks(columns=...)），按
  纽约本地小时构建相对点差模型（T20 口径，SpreadModel.from_ticks）；
- 写 {data-root}/costs/spread_model.json：各品种 tick 水位
  （rows/first_msc/last_msc，来自 mt5data.ticks.tick_watermark 只读 parquet
  元数据）+ 模型负载 + 模型 sha256；
- 读取方（run_backtest.build_costs）加载缓存并逐项比对水位：tick 增量后
  未重建 → 报错要求重跑本脚本，绝不静默沿用过期模型；
- --symbols 缺省 = tick 库下全部品种；无 tick 库的品种报错（不静默跳过）；
- 幂等：重跑直接覆盖旧缓存（水位按当前库重新记录）。

内存提示：构建需把全部品种的 3 列 tick 载入内存做分位统计（一次性，
XAUUSD 约 2 GB 级）；产物 JSON 只有几十 KB，回测侧从此零 tick 加载。
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 复用 run_backtest 装配（同 bt.py）

from mt5data.ticks import load_ticks, tick_watermark, ticks_dir  # noqa: E402
from mt5quant.costs import SpreadModel  # noqa: E402
from run_backtest import (  # noqa: E402
    save_spread_model_cache, spread_model_cache_path,
)


def build_cost_models(data_root: str, symbols, *,
                      spread_mode: str = "relative",
                      out_path: str | None = None) -> dict:
    """（重）建点差模型缓存并原子写盘，返回写入的文档（dict）。

    symbols 为 None → 枚举 {data_root}/ticks 下全部品种；某品种无 tick 库 →
    ValueError（先跑 scripts/download_ticks.py）。
    """
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
    models: list[SpreadModel] = []
    watermarks: dict[str, dict] = {}
    total_rows = 0
    for sym in symbols:
        wm = tick_watermark(data_root, sym)
        if wm is None:
            raise ValueError(
                f"{sym} 无 tick 库（{ticks_dir(data_root, sym)}）——"
                "先跑 scripts/download_ticks.py 回补后再构建成本模型")
        print(f"· {sym}: 读 {wm['rows']:,} 条 tick（只取 time_msc/bid/ask）…")
        ticks = load_ticks(data_root, sym,
                           columns=["time_msc", "bid", "ask"])
        model = SpreadModel.from_ticks(sym, ticks, mode=spread_mode)
        del ticks                      # 逐品种释放，峰值只驻留一个品种
        models.append(model)
        watermarks[sym] = wm
        total_rows += int(wm["rows"])
        hours = len(model._per_symbol[sym])  # noqa: SLF001 — 摘要打印
        miss = 24 - hours
        print(f"✓ {sym}: {hours}/24 个纽约小时有统计"
              + (f"（缺 {miss} 小时，回测按 forbid 禁入）" if miss else ""))
    merged = models[0]
    if len(models) > 1:
        merged = merged.merge(*models[1:])
    path = out_path or spread_model_cache_path(data_root)
    doc = save_spread_model_cache(path, merged, watermarks)
    print(f"✓ 点差模型缓存已写 {path}"
          f"（{len(symbols)} 品种 / {total_rows:,} 条 tick / 口径 {spread_mode}"
          f" / sha256 {doc['model_sha256'][:12]}…）")
    return doc


def main() -> int:
    ap = argparse.ArgumentParser(
        description="构建点差模型缓存 data/costs/spread_model.json（T28/M8）")
    ap.add_argument("--data-root", default="data")
    ap.add_argument("--symbols", default=None,
                    help="逗号分隔，如 XAUUSD,BTCUSD；缺省 = tick 库下全部品种")
    ap.add_argument("--spread-mode", choices=("relative", "absolute"),
                    default="relative",
                    help="点差口径（与 run_backtest --spread-mode 一致，默认相对）")
    ap.add_argument("--out", default=None,
                    help="缓存文件路径（缺省 {data-root}/costs/spread_model.json）")
    args = ap.parse_args()
    symbols = ([s.strip() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else None)
    try:
        build_cost_models(args.data_root, symbols,
                          spread_mode=args.spread_mode, out_path=args.out)
    except Exception as e:  # fail-fast：如实报出
        print(f"✗ {type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
