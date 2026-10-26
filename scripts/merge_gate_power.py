"""合并 gate_calibration 分片产物为正式 gate-power 报告。

用法：
    ./.venv/Scripts/python.exe scripts/merge_gate_power.py \
        --shards "data/reports/gates/shard-0,data/reports/gates/shard-1,..." \
        --out data/reports/gates

各分片由 `gate_calibration.py --seeds-from K --seeds N --out <shard-dir>` 产出，
种子段互不相交。本脚本拼接三个实验臂的 runs 记录，用与原脚本同一套聚合函数
（aggregate_rates / collect_flags / render_report / compute_flags——R6/C8 起
flags 也走单进程的 compute_flags，正对照与 G5 检出率双源遍历）重算比率与
门槛建议，产出与单进程版完全同构的 gate-power.{json,md}。

R6 修正（第六轮审核 C8/M2）：
- schema 对齐真实产物：G5 runs 按 **setup** 分组（生成器写 setup，不写 key）；
  正对照 calibration 保持 {setup: {level: …}} **字典**（转列表会让
  render_report 的 .get(key) 直接 AttributeError）；
- G5 误报臂按 **null**（day/slot）分桶分别聚合（绝不混桶平均——100% 与 0%
  混成 50% 违反"任一零假设超限即标记"的取最差规则）；flags 的最差值由
  compute_flags 对各 null 取最大；
- 种子校验除 meta 声明段互不相交外，还核对 runs 里的**实际种子**：每个实验
  条件（负对照 name×null、正对照 setup×level、G5 setup×level×arm×null）
  必须恰好覆盖本分片声明的种子段——重复/缺样/越界都拒绝合并；
- 仓库根定位（M2）：__file__ 同目录有 gate_calibration.py（正常部署）→ 用
  其父级；否则**必须**由环境变量 MT5QUANT_REPO 提供——都没有明确报错，
  不再有个人工作区绝对路径兜底（硬编码路径在他机/CI 不可用）。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

_here = Path(__file__).resolve()


def _resolve_repo() -> Path:
    """仓库根（scripts/gate_calibration.py 所在目录的父级），M2/R6。

    1) __file__ 同目录存在 gate_calibration.py（仓库内正常运行）→ 其父级；
    2) 否则环境变量 MT5QUANT_REPO（测试把本脚本复制到临时目录时注入），
       且其下必须真的有 scripts/gate_calibration.py；
    3) 都没有 → RuntimeError（绝不静默回退到任何硬编码绝对路径）。
    """
    if (_here.parent / "gate_calibration.py").is_file():
        return _here.parents[1]
    env = os.environ.get("MT5QUANT_REPO")
    if env:
        repo = Path(env)
        if (repo / "scripts" / "gate_calibration.py").is_file():
            return repo
        raise RuntimeError(
            f"MT5QUANT_REPO={env} 下没有 scripts/gate_calibration.py"
            "——请指向本仓库根目录")
    raise RuntimeError(
        "无法定位仓库根：merge_gate_power.py 同目录没有 gate_calibration.py，"
        "且环境变量 MT5QUANT_REPO 未设置（应指向仓库根，含 "
        "scripts/gate_calibration.py）")


REPO = _resolve_repo()
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
    """正对照合并（R6/C8）：runs 按 setup（生成器实际写的键）× level 拼接
    重算检出率；calibration 保持 {setup: {level: …}} 字典——校准与分片种子
    无关（calibrate_drift 恒用 seed=0 的同数据），各分片应逐位一致，不一致
    即数据/成本不同源，拒绝合并。"""
    runs = [r for s in shards for r in s["positive"]["runs"]]
    by_key_level: dict[str, dict[str, list]] = {}
    for run in runs:
        by_key_level.setdefault(run["setup"], {}).setdefault(
            str(run["level"]), []).append(run)
    detection = {key: {level: _agg(bucket) for level, bucket in levels.items()}
                 for key, levels in by_key_level.items()}
    calibration: dict[str, dict] = {}
    for s in shards:
        c = s["positive"].get("calibration")
        if not isinstance(c, dict):
            continue
        for key, by_level in c.items():
            if key in calibration and calibration[key] != by_level:
                raise ValueError(
                    f"分片 calibration 不一致（setup {key}）——各分片必须"
                    "来自同一数据/成本/档位配置")
            calibration.setdefault(key, by_level)
    return {"runs": runs, "detection_rates": detection, "calibration": calibration}


def merge_g5(shards: list[dict]) -> dict:
    """G5 判别力合并（R6/C8）：检出臂按 setup（真实产物键，不是 key）×
    level；误报臂按 **null** 分桶（day/slot 两种零假设分别聚合，结构同
    单进程 g5_power 的 {null: {check: rate}}，决策规则的最差值由
    compute_flags 对各 null 取最大——绝不混桶平均）。"""
    out = {}
    for s in shards:
        g5 = s.get("g5")
        if g5:
            out = dict(g5)          # 窗口/设计等元数据各分片一致，取首个
            break
    runs = [r for s in shards for r in (s.get("g5") or {}).get("runs", [])]
    det_buckets: dict[str, dict[str, list]] = {}
    fp_buckets: dict[str, list] = {}
    for run in runs:
        if run.get("arm") == "detection":
            det_buckets.setdefault(run["setup"], {}).setdefault(
                str(run["level"]), []).append(run)
        else:
            fp_buckets.setdefault(str(run.get("null") or "?"), []).append(run)
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
    """R6/C8：与单进程主流程共用 gc.compute_flags（正对照 + G5 检出率双源
    遍历、G5 误报按 null 取最差）。"""
    return gc.compute_flags(results)


def _seed_problems(docs: list[dict]) -> list[str]:
    """分片种子校验（R6/C8）：meta 声明段互不相交之外，再核 runs 里的实际
    种子——每个实验条件必须恰好覆盖本分片声明的种子段（无重复、无缺样、
    无越界）。返回问题清单（空 = 通过）。"""
    problems: list[str] = []
    for idx, s in enumerate(docs):
        meta = s.get("meta") or {}
        start = int(meta.get("seeds_from", 0))
        count = int(meta.get("seeds", 0))
        declared = set(range(start, start + count))
        groups: dict[tuple, list[int]] = {}

        def _add(kind: tuple, run: dict) -> None:
            groups.setdefault(kind, []).append(int(run["seed"]))

        for run in (s.get("negative") or {}).get("runs", []):
            _add(("negative", run.get("name"), run.get("null")), run)
        for run in (s.get("positive") or {}).get("runs", []):
            _add(("positive", run.get("setup"), str(run.get("level"))), run)
        for run in (s.get("g5") or {}).get("runs", []):
            _add(("g5", run.get("setup"), str(run.get("level")),
                  run.get("arm"), run.get("null")), run)
        for kind, seeds in sorted(groups.items(), key=repr):
            got = set(seeds)
            dup = sorted(x for x in got if seeds.count(x) > 1)
            if dup:
                problems.append(f"分片{idx} {kind}: 种子重复 {dup}")
            missing = sorted(declared - got)
            if missing:
                problems.append(f"分片{idx} {kind}: 缺种子 {missing}")
            extra = sorted(got - declared)
            if extra:
                problems.append(f"分片{idx} {kind}: 声明段外种子 {extra}")
    return problems


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
    seed_sets = [range(int(s["meta"].get("seeds_from", 0)),
                       int(s["meta"].get("seeds_from", 0))
                       + int(s["meta"].get("seeds", 0)))
                 for s in docs]
    all_seeds = [x for r in seed_sets for x in r]
    if len(all_seeds) != len(set(all_seeds)):
        print("✗ 分片种子重叠，拒绝合并")
        return 1
    problems = _seed_problems(docs)
    if problems:
        for p in problems:
            print(f"✗ {p}")
        print("✗ 分片实际运行种子与声明不符（重复/缺样/越界），拒绝合并")
        return 1

    try:
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
    except (KeyError, ValueError) as e:
        print(f"✗ 分片产物结构与合并器不兼容：{type(e).__name__}: {e}")
        return 1

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
