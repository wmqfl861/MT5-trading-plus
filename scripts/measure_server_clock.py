"""服务器时钟差测量 CLI（P0）。

经纪商服务器时间通常贴 GMT+2/+3（随纽约夏令时走），10/25（欧洲）与
11/01（美国）切换前后与墙钟 UTC 的差值会跳变。本脚本只采样留证、
不做推断：每次取 symbol_info_tick 的 time 与墙钟 UTC 求差，追加写
data/server_clock.json，末尾打印 offset 的 min/max/median 与稳定性
判读（判据随输出写明），供切换前后对比。

休市/静默期防护：tick 距墙钟超过 STALE_TICK_S 视为陈旧（度量的是
"距末笔的时间"而非时钟差），样本标 stale=True 且不参与统计。

用法：
    .venv/Scripts/python.exe scripts/measure_server_clock.py --samples 12 --interval 5
"""
import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5gw import GatewayConfig, MT5Gateway, load  # noqa: E402
from mt5gw.snapshot import write_json_atomic  # noqa: E402

STABLE_SPREAD_S = 1.0  # tick 时间粒度为秒：极差 ≤1s 视为稳定
STALE_TICK_S = 120.0   # tick 距墙钟超过该秒数视为陈旧（休市静默期），样本无效


def append_samples(path: str, records: list[dict]) -> int:
    """把采样记录追加进 JSON 数组（原子写，既有记录原样保留）；返回总条数。"""
    data = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError(f"{path} 不是 JSON 数组，拒绝追加（先人工核查历史数据）")
    data.extend(records)
    write_json_atomic(path, data)
    return len(data)


def is_stale(record: dict) -> bool:
    """tick 陈旧判定：server_time 距 wall_utc 超过 STALE_TICK_S（休市/静默期，
    取到的是末笔旧 tick，其差值度量的是"距末笔的时间"而非时钟差——无效）。"""
    wall = datetime.fromisoformat(record["wall_utc"])
    server = datetime.fromisoformat(record["server_time"])
    lag = (wall.replace(tzinfo=None) - server).total_seconds()
    return lag > STALE_TICK_S


def summarize_offsets(offsets: list[float]) -> dict:
    return {
        "n": len(offsets),
        "min_s": min(offsets),
        "max_s": max(offsets),
        "median_s": statistics.median(offsets),
        "spread_s": max(offsets) - min(offsets),
        "stable": max(offsets) - min(offsets) <= STABLE_SPREAD_S,
    }


def _load_config(path: str) -> GatewayConfig:
    if os.path.exists(path):
        return load(path)
    print(f"· 未找到 {path}，使用默认网关配置（本脚本只读行情不下单）")
    return GatewayConfig()


def main() -> int:
    ap = argparse.ArgumentParser(description="测量服务器时钟与墙钟 UTC 的差值")
    ap.add_argument("--samples", type=int, default=12, help="采样次数")
    ap.add_argument("--interval", type=float, default=5.0, help="采样间隔（秒）")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--config", default="config.json",
                    help="网关配置 JSON（不存在则用默认配置）")
    ap.add_argument("--data-dir", default="data")
    args = ap.parse_args()

    records = []
    stale_count = 0
    with MT5Gateway(_load_config(args.config)) as gw:
        for i in range(args.samples):
            wall = datetime.now(timezone.utc)
            q = gw.market.quote(args.symbol)  # 内含 symbol_info_tick，time 即服务器时间
            offset_s = (q.time - wall.replace(tzinfo=None)).total_seconds()
            rec = {
                "wall_utc": wall.isoformat(),
                "server_time": q.time.isoformat(),
                "offset_s": offset_s,
            }
            if is_stale(rec):
                # 休市静默期：末笔 tick 距今太远，offset 度量的是静默时长而非时钟差
                rec["stale"] = True
                stale_count += 1
                records.append(rec)
                print(f"[{i + 1}/{args.samples}] wall={wall.isoformat()} "
                      f"server={q.time.isoformat()} 陈旧 tick（休市？），样本无效",
                      flush=True)
            else:
                records.append(rec)
                print(f"[{i + 1}/{args.samples}] wall={wall.isoformat()} "
                      f"server={q.time.isoformat()} offset={offset_s:+.1f}s",
                      flush=True)
            if i + 1 < args.samples:
                time.sleep(args.interval)

    out = os.path.join(args.data_dir, "server_clock.json")
    total = append_samples(out, records)
    fresh = [r["offset_s"] for r in records if not r.get("stale")]
    print(f"\n已追加 {len(records)} 条到 {out}（累计 {total} 条；"
          f"其中陈旧 {stale_count} 条已标 stale，不参与统计）")
    if not fresh:
        print("全部样本陈旧（休市静默期）——本次无有效测量，"
              "请在开市时段重跑。")
        return 1
    s = summarize_offsets(fresh)
    print(f"offset_s: min={s['min_s']:+.1f} max={s['max_s']:+.1f} "
          f"median={s['median_s']:+.1f} 极差={s['spread_s']:.1f}s")
    verdict = "稳定" if s["stable"] else "存在漂移"
    print(f"是否稳定（判据：极差 ≤ {STABLE_SPREAD_S}s，tick 粒度 1s）：{verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
