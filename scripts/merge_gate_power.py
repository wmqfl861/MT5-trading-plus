"""合并 gate_calibration 分片产物为正式 gate-power 报告。

用法：
    ./.venv/Scripts/python.exe scripts/merge_gate_power.py \
        --shards "data/reports/gates/shard-0,data/reports/gates/shard-1,..." \
        --out data/reports/gates

各分片由 `gate_calibration.py --seeds-from K --seeds N --out <shard-dir>` 产出，
种子段互不相交。本脚本拼接三个实验臂的 runs 记录，用与原脚本同一套聚合函数
（aggregate_rates / collect_flags / render_report）重算比率与门槛建议，
产出与单进程版完全同构的 gate-power.{json,md}。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

# 从测试的临时副本加载时 __file__ 在 tmp 下：回退到环境变量或固定仓库根
_here = Path(__file__).resolve()
REPO = _here.parents[1] if (_here.parents[1] / "scripts" / "gate_calibration.py").exists() else Path(os.environ.get("MT5QUANT_REPO", 'C:\\Users\\Administrator\\.zcode\\workspace\\664d5945-068c-46aa-ba82-7141f134aa13'))
_spec = importlib.util.spec_from_file_location(
    "gate_calibration", REPO / "scripts" / "gate_calibration.py")
gc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gc)

import numpy as np  # noqa: E402


def _agg(runs: list[dict]) -> dict[str, float]:
    return gc.aggregate_rates(runs)


def merge_negative(shards: list[dict]) -> dict:
    runs = [r for s in shards for r in s["negative"]["runs"]]
    merged = [r for s in shards for r in s["negative"].get("merged", [])]
    buckets: dict[tuple, list] = {}
    for run in runs:
        for check, value in run["checks"].items():
            buckets.setdefault((run.get("null", "day"), check), []).append(bool(value))
    by_null: dict[str, dict[str, float]] = {}
    for (null, check), values in buckets.items():
        by_null.setdefault(null, {})[check] = float(np.mean(values))
    worst = {check: max(rates.get(check, 0.0) for rates in by_null.values())
             for check in {c for r in by_null.values() for c in r}}
    mbuckets: dict[str, list[bool]] = {}
    for row in merged:
        mbuckets.setdefault(f"{row['name']}|{row['null']}", []).append(bool(row["merged_pass"]))
    return {"runs": runs, "by_null": by_null, "false_positive_rates": worst,
            "merged": merged,
            "merged_pass_rates": {k: float(np.mean(v)) for k, v in mbuckets.items()}}


def merge_positive(shards: list[dict]) -> dict:
    runs = [r for s in shards for r in s["positive"]["runs"]]
    by_key_level: dict[str, dict[str, list]] = {}
    for run in runs:
        by_key_level.setdefault(run["setup"], {}).setdefault(
            str(run["level"]), []).append(run)
    detection = {key: {level: _agg(bucket) for level, bucket in levels.items()}
                 for key, levels in by_key_level.items()}
    calib = []
    for s in shards:
        c = s["positive"].get("calibration")
        if isinstance(c, list):
            calib.extend(c)
        elif c:
            calib.append(c)
    return {"runs": runs, "detection_rates": detection, "calibration": calib}


def merge_g5(shards: list[dict]) -> dict:
    out = {}
    for s in shards:
        g5 = s.get("g5")
        if g5:
            out = g5          # 窗口/设计等元数据各分片一致，取首个
            break
    runs = [r for s in shards for r in (s.get("g5") or {}).get("runs", [])]
    det_buckets: dict[str, dict[str, list]] = {}
    fp_buckets: dict[str, list] = {}
    for run in runs:
        if run.get("arm") == "detection":
            det_buckets.setdefault(run["key"], {}).setdefault(
                str(run["level"]), []).append(run)
        else:
            fp_buckets.setdefault(str(run.get("arm", "?/day")), []).append(run)
    detection = {key: {level: _agg(bucket) for level, bucket in levels.items()}
                 for key, levels in det_buckets.items()}
    fp = {null: _agg(bucket) for null, bucket in fp_buckets.items() if bucket}
    out.update({"runs": runs, "detection_rates": detection,
                "false_positive_rates": fp})
    return out


def merge_meta(shards: list[dict]) -> dict:
    meta = dict(shards[0]["meta"])
    total = sum(int(s["meta"].get("seeds", 0)) for s in shards)
    meta["seeds"] = total
    meta["shards"] = sorted(
        ({"from": int(s["meta"].get("seeds_from", 0)),
          "n": int(s["meta"].get("seeds", 0))} for s in shards),
        key=lambda d: d["from"])
    meta.pop("seeds_from", None)
    meta["merged_from_shards"] = True
    meta["generated_utc"] = shards[-1]["meta"].get("generated_utc")
    return meta


def recompute_flags(results: dict) -> list[dict]:
    fp_rates = dict((results.get("negative") or {}).get(
        "false_positive_rates") or {})
    g5_block = results.get("g5") or {}
    for null, rates in (g5_block.get("false_positive_rates") or {}).items():
        for check, rate in rates.items():
            if check == "G5.passed":
                continue
            fp_rates[check] = max(fp_rates.get(check, 0.0), float(rate))
    level_rates: dict[str, float] = {}
    for key, by_level in ((results.get("positive") or {}).get("detection_rates")
                          or g5_block.get("detection_rates") or {}).items():
        for check, rate in (by_level.get(str(gc.DECISION_LEVEL)) or {}).items():
            if check == "G5.passed":
                continue
            prev = level_rates.get(check)
            level_rates[check] = rate if prev is None else min(prev, rate)
    if not (level_rates or fp_rates):
        return []
    return gc.collect_flags(level_rates, fp_rates)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="合并 gate-power 分片产物")
    ap.add_argument("--shards", required=True,
                    help="逗号分隔的分片目录（各含 gate-power.json）")
    ap.add_argument("--out", default=str(REPO / "data" / "reports" / "gates"))
    args = ap.parse_args(argv)

    docs = []
    for d in args.shards.split(","):
        p = Path(d.strip()) / "gate-power.json"
        docs.append(json.loads(p.read_text(encoding="utf-8")))
    if len({tuple(sorted(s["meta"]["baselines"])) for s in docs}) != 1:
        print("✗ 分片的 baselines 不一致，拒绝合并")
        return 1
    seed_sets = [range(s["meta"]["seeds_from"],
                       s["meta"]["seeds_from"] + s["meta"]["seeds"])
                 for s in docs]
    all_seeds = [x for r in seed_sets for x in r]
    if len(all_seeds) != len(set(all_seeds)):
        print("✗ 分片种子重叠，拒绝合并")
        return 1

    results = {"meta": merge_meta(docs)}
    if "negative" in docs[0]:
        results["negative"] = merge_negative(docs)
    if "positive" in docs[0]:
        results["positive"] = merge_positive(docs)
        results["meta"]["detection_min_note"] = docs[0]["meta"].get(
            "detection_min_note")
    if any("g5" in d for d in docs):
        results["g5"] = merge_g5(docs)
    results["flags"] = recompute_flags(results)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "gate-power.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2,
                   default=gc._json_default), encoding="utf-8")
    (out / "gate-power.md").write_text(
        gc.render_report(results), encoding="utf-8")
    n_runs = sum(len((results.get(k) or {}).get("runs", []))
                 for k in ("negative", "positive", "g5"))
    print(f"— 合并 {len(docs)} 分片 / {len(all_seeds)} 种子 / {n_runs} 条运行 "
          f"→ {out / 'gate-power.md'}")
    flags = results["flags"]
    if flags:
        print(f"⚠ 决策规则触发 {len(flags)} 项："
              f"{[f['check'] for f in flags]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
