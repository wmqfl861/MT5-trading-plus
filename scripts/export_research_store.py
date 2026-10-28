"""导出研究库：把主库 time < cutoff 的 K 线物理复制到 data_research（T27/H3）。

研究/审查账户对主库 data\\bars、data\\ticks、C:\\mt5-lockbox 拒读（ACL 见
docs/p6/permissions.md）；研究侧一切读取改走 data_research（--research-store，
研究侧 CLI 的 --data-root 缺省即它）。本脚本由**管理员**在主库侧运行。

导出内容与约定：
- data_research/bars/{品种}/{周期}/data.parquet——**同布局**，只含
  time < 全局 cutoff 的行；meta.json 追加来源信息：source_root（主库
  绝对路径）、source_cutoff、data_sha256（导出段的快照哈希，与
  bt/run_backtest 的 data_sha256 同算法——研究侧运行的 data_sha256 与
  主库研究窗一致，台账谱系连续）、exported_at_utc；
- data_research/costs/spread_model.json——T28 的点差模型缓存**复制件**；
  **tick 不导出**：tick 库对研究账户拒读，研究侧成本一律经这份缓存文件
  提供（build_costs(research_store=True) 命中缓存，不再对照 tick 水位——
  水位核验移到导出时：缓存与主库 tick 水位不一致 → 拒绝导出）；
- data_research/specs/latest.json——合约大小与手数上下限（必需要有，
  缺则拒绝导出）；costs/calibrated.json、server_clock.json 有则复制；
- **lockbox.json 不复制**：研究库里没有锁箱段，research_bars 在研究库上
  行为与直读一致（锁箱语义自然满足）；
- 导出后自检（verify_research_store）：研究库里**任何**序列的 last_time
  都必须早于 cutoff，一条越界即整体失败退出（退出码 2）；--verify-only
  可对既有研究库单独复检。

外部数据（T67/D36/铁律 19，**默认不启用**）：
- `--include-ext` 才把 data/ext/**（外部源适配后的 K 线）导出为
  data_research/ext/<来源>/<品种>/<周期>/——研究库纳入外部数据须用户
  先确认数据源（docs/research/ext-data-*.md 的"来源清单"），因此缺省关；
- **白名单硬校验（T81/D36 + T87/R13 §5）**：只有批准来源
  （mt5data.ext.approvals：fred/cdd 第十二轮批准；histdata 第十三轮
  §5 有条件批准）可导出；未批准来源（stooq/yahoo 等）逐条拒绝、
  不进研究库——ext 库里只剩未批准序列时整体退出非零；
- 批准来源的 manifest 必须带与批准表一致的使用限定
  （usage_restriction，只研究用/不再分发/原始数据不入公开仓库），
  缺限定或文本不一致 → 拒绝导出（fail-fast 整体失败）；
- cutoff 纪律同样硬约束外部数据：只导出 time < 全局 cutoff 的行，
  导出后 verify_ext_research_store 复检研究库 ext 段（manifest 必须合法、
  last_time < cutoff，一条越界整体失败退出 2）；
- ext 序列的 manifest 在读取与导出两侧都过 validate_manifest（缺字段
  拒绝入库/拒绝导出），并追加 research_cutoff 与 exported_at_utc。

用法（Git Bash，管理员）：
    .venv/Scripts/python.exe scripts/export_research_store.py
    .venv/Scripts/python.exe scripts/export_research_store.py --verify-only
    .venv/Scripts/python.exe scripts/export_research_store.py --include-ext
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from mt5data.ext import ExtDataError  # noqa: E402
from mt5data.lockbox import cutoff_for, load_config  # noqa: E402
from mt5data.schedule import series_inventory  # noqa: E402
from mt5data.store import load_bars, read_watermark, save_bars  # noqa: E402
from mt5data.ticks import tick_watermark  # noqa: E402
from mt5quant.backtest.report import data_sha256  # noqa: E402


def _copy_file(src: str, dst: str) -> None:
    """原子复制（tmp + os.replace；Windows 读者占句柄时稍候重试）。"""
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    tmp = dst + ".tmp"
    shutil.copyfile(src, tmp)
    for attempt in range(10):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            if attempt == 9:
                raise
            import time
            time.sleep(0.3)


def _merge_meta(dest_root, symbol: str, timeframe: str, extra: dict) -> None:
    """save_bars 写完水位后，把来源信息并进该序列的 meta.json（原子写）。"""
    p = os.path.join(dest_root, "bars", symbol, timeframe, "meta.json")
    with open(p, encoding="utf-8") as f:
        meta = json.load(f)
    meta.update(extra)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def verify_cost_cache(data_root: str, cache_path: str,
                      symbols: list[str]) -> None:
    """导出时核验点差缓存新鲜度（研究侧无 tick 库，无法自验）：

    缓存内每个品种的 tick 水位（rows/first_msc/last_msc）必须与主库
    当前水位逐项一致，且覆盖全部被导出的品种；不一致 → ValueError
    （提示先重跑 scripts/build_cost_model.py，绝不导出过期成本模型）。
    """
    with open(cache_path, encoding="utf-8") as f:
        doc = json.load(f)
    wms = doc.get("watermarks") or {}
    missing = [s for s in symbols if s not in wms]
    if missing:
        raise ValueError(
            f"点差缓存缺品种 {missing}（缓存品种：{sorted(wms)}）——先跑 "
            f"scripts/build_cost_model.py --symbols {','.join(missing)}"
            " 再导出研究库（研究侧成本只能来自缓存文件）")
    for sym in symbols:
        cur = tick_watermark(data_root, sym)
        cached = wms[sym]
        if cur is None:
            raise ValueError(
                f"{sym} 主库无 tick 库却有点差缓存——环境异常，先确认"
                f"数据目录 {data_root}")
        for k in ("rows", "first_msc", "last_msc"):
            if cached.get(k) != cur.get(k):
                raise ValueError(
                    f"{sym} 的点差缓存已过期——tick 水位不一致（缓存 "
                    f"{k}={cached.get(k)!r}，主库 {k}={cur.get(k)!r}）——"
                    "研究侧无法自验缓存，必须先重跑 scripts/build_cost_model.py"
                    " 再导出")


def verify_research_store(dest: str, cutoff) -> list[str]:
    """研究库自检：任何序列 last_time ≥ cutoff → 记一条越界（T27）。

    返回越界清单（空 = 通过）；越界序列的读回不经 save/merge 的内存
    副本，而是重新 load_bars（防"写的东西与验的东西不是同一份"）。
    """
    cut = pd.Timestamp(cutoff)
    violations: list[str] = []
    inventory, _warnings = series_inventory(dest)
    for ref in inventory:
        df = load_bars(dest, ref.symbol, ref.timeframe)
        if df is None or len(df) == 0:
            continue
        last = pd.Timestamp(df["time"].max())
        if last >= cut:
            violations.append(
                f"{ref.symbol}/{ref.timeframe}: last_time="
                f"{last.isoformat()} ≥ cutoff={cut.isoformat()}"
                "（研究库混入锁箱段，导出失败）")
    return violations


def export_ext_series(ext_root: str, dest: str, cutoff, *,
                      now_iso: str) -> tuple[int, int, int]:
    """把 data/ext/** 导出进研究库（只取 time < cutoff 的行；T67/T81）。

    - manifest 在读取侧过 validate_manifest（缺字段 → ExtDataError，
      调用方整体失败）：来源与版本不明的数据不进研究库；
    - **白名单（T81/D36）**：未批准来源逐条拒绝（打印原因、计入
      rejected，不进研究库）；ext 库只有未批准序列时整体失败（返回
      exported=0 且 rejected>0，调用方据此退出非零）；
    - 批准来源的 manifest 缺使用限定/与批准表不一致 → ExtDataError
      整体失败（研究数据必须带着批准时的使用限定入库）；
    - cutoff 之后的行**被裁掉**（铁律 19：外部数据同样只允许 cutoff
      之前部分进研究路径），开放段为空的序列跳过；
    - manifest 追加 research_cutoff / exported_at_utc 后随数据落盘。
    返回 (导出序列数, 跳过数, 白名单拒绝数)。
    """
    from mt5data.ext import (ExtDataError, ExtSeries, approved_sources,
                             check_exportable, ext_inventory,
                             load_ext_series, save_ext_series,
                             slice_before_cutoff)

    inv = ext_inventory(ext_root)
    if not inv:
        print(f"· {ext_root} 无外部序列（ext manifest 一个都没有），跳过")
        return 0, 0, 0
    exported = skipped = rejected = 0
    for m in inv:
        key = f"{m['source']}/{m['symbol']}/{m['timeframe']}"
        try:
            check_exportable(m)
        except ExtDataError as e:
            # 未批准来源：数据留在主库侧（一致性核对可用），只拒入研究库；
            # 批准来源缺/错使用限定同样在此抛出——属 manifest 与批准表
            # 不一致的缺陷，fail-fast 整体失败
            if m["source"] not in approved_sources():
                print(f"✗ {key}: 白名单外来源，拒绝导出——{e}")
                rejected += 1
                continue
            print(f"✗ {key}: {e}")
            raise
        try:
            loaded = load_ext_series(ext_root, m["source"], m["symbol"],
                                     m["timeframe"])
        except ExtDataError as e:
            print(f"✗ {key}: manifest 校验失败——{e}")
            raise
        if loaded is None:
            continue
        df, manifest = loaded
        seg = slice_before_cutoff(df, cutoff)
        if len(seg) == 0:
            print(f"· {key} 开放段（time < cutoff）为空，跳过"
                  "（整段都在锁箱期）")
            skipped += 1
            continue
        out = dict(manifest)
        out["research_cutoff"] = pd.Timestamp(cutoff).isoformat()
        out["exported_at_utc"] = now_iso
        out["rows"] = int(len(seg))
        out["first_time"] = pd.Timestamp(seg["time"].iloc[0]).isoformat()
        out["last_time"] = pd.Timestamp(seg["time"].iloc[-1]).isoformat()
        save_ext_series(os.path.join(str(dest), "ext"),
                        ExtSeries(df=seg, manifest=out))
        print(f"✓ ext {key} 开放段 "
              f"{len(seg):,} 根（time < {pd.Timestamp(cutoff).isoformat()}）",
              flush=True)
        exported += 1
    if rejected and not exported:
        raise ExtDataError(
            f"--include-ext 白名单拒绝了全部 {rejected} 个序列（无任何批准"
            f"来源可导出；批准来源：{list(approved_sources())}）")
    return exported, skipped, rejected


def verify_ext_research_store(dest: str, cutoff) -> list[str]:
    """研究库 ext 段自检（T67）：manifest 合法 + last_time < cutoff。

    与 verify_research_store 同语义：一条越界/一个坏 manifest 即记越界，
    整体失败退出；读回不经写路径的内存副本。
    """
    from mt5data.ext import ExtDataError, ext_inventory

    ext_root = os.path.join(str(dest), "ext")
    if not os.path.isdir(ext_root):
        return []
    cut = pd.Timestamp(cutoff)
    violations: list[str] = []
    try:
        inv = ext_inventory(ext_root)
    except ExtDataError as e:
        return [f"ext manifest 校验失败：{e}"]
    for m in inv:
        df = pd.read_parquet(os.path.join(
            ext_root, m["source"], m["symbol"], m["timeframe"],
            "data.parquet"))
        if len(df) and pd.Timestamp(df["time"].max()) >= cut:
            violations.append(
                f"ext {m['source']}/{m['symbol']}/{m['timeframe']}: "
                f"last_time={pd.Timestamp(df['time'].max()).isoformat()}"
                f" ≥ cutoff={cut.isoformat()}（外部数据混入锁箱段）")
    return violations


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="导出研究库（time < cutoff 的 K 线 + 成本缓存；T27/H3）")
    ap.add_argument("--data-root", default="data", help="主库根目录")
    ap.add_argument("--dest", default="data_research",
                    help="研究库根目录（默认 data_research）")
    ap.add_argument("--verify-only", action="store_true",
                    help="不导出，只对既有研究库跑导出自检")
    ap.add_argument("--include-ext", action="store_true",
                    help="纳入外部数据 data/ext/**（T67；默认不启用——"
                         "研究库纳入须用户先确认数据源）")
    ap.add_argument("--ext-root", default="data/ext",
                    help="外部数据根目录（默认 data/ext）")
    args = ap.parse_args(argv)

    lockbox = load_config(args.data_root)
    if not lockbox:
        print(f"✗ {args.data_root} 无锁箱配置（lockbox.json）：研究库的"
              "边界就是 cutoff，先跑 scripts/lockbox_init.py")
        return 1
    global_cutoff = getattr(lockbox, "global_cutoff", None)

    if args.verify_only:
        # v2 用全局 cutoff 核全部序列；v1 逐序列分别核，缺 cutoff 的序列
        # 直接记越界（研究库里的序列必须全部在锁箱约束下导出）
        inventory, _ = series_inventory(args.dest)
        if global_cutoff is not None:
            violations = verify_research_store(args.dest, global_cutoff)
        else:
            violations = []
            for ref in inventory:
                cut = cutoff_for(lockbox, ref.symbol, ref.timeframe)
                if cut is None:
                    violations.append(
                        f"{ref.symbol}/{ref.timeframe}: 无 cutoff（研究库"
                        "序列必须全部在锁箱约束下导出）")
                else:
                    violations.extend(verify_research_store(args.dest, cut))
        if global_cutoff is not None:
            violations += verify_ext_research_store(args.dest, global_cutoff)
        for v in violations:
            print(f"✗ 越界：{v}")
        if violations:
            print(f"研究库自检失败：{len(violations)} 条越界序列")
            return 2
        print("✓ 研究库自检通过：所有序列 last_time < cutoff")
        return 0

    inventory, warnings = series_inventory(args.data_root)
    for w in warnings:
        print(f"⚠ 坏 meta，跳过：{w}", flush=True)
    if not inventory:
        print(f"✗ {args.data_root}/bars 无序列可导出")
        return 1

    # 逐序列导出（先数据后成本；任何序列无 cutoff → 整体失败）
    exported = skipped = 0
    symbols: list[str] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for ref in inventory:
        cut = cutoff_for(lockbox, ref.symbol, ref.timeframe)
        if cut is None:
            print(f"✗ {ref.symbol}/{ref.timeframe} 无 cutoff——研究库不得"
                  "包含未锁序列，拒绝导出（v1 配置请补齐该序列或改用全局锁箱）")
            return 1
        df = load_bars(args.data_root, ref.symbol, ref.timeframe)
        if df is None or len(df) == 0:
            print(f"· {ref.symbol}/{ref.timeframe} 无数据文件，跳过")
            skipped += 1
            continue
        seg = df[df["time"] < pd.Timestamp(cut)].reset_index(drop=True)
        if len(seg) == 0:
            print(f"· {ref.symbol}/{ref.timeframe} 开放段（time < cutoff）"
                  "为空，跳过（整段都在锁箱内）")
            skipped += 1
            continue
        save_bars(args.dest, ref.symbol, ref.timeframe, seg,
                  server=ref.watermark.get("server"),
                  login=ref.watermark.get("login"))
        _merge_meta(args.dest, ref.symbol, ref.timeframe, {
            "source_root": os.path.abspath(args.data_root),
            "source_cutoff": pd.Timestamp(cut).isoformat(),
            "data_sha256": data_sha256(seg),
            "exported_at_utc": now_iso,
        })
        if ref.symbol not in symbols:
            symbols.append(ref.symbol)
        print(f"✓ {ref.symbol}/{ref.timeframe} 开放段 {len(seg):,} 根"
              f"（time < {pd.Timestamp(cut).isoformat()}）", flush=True)
        exported += 1

    if exported == 0:
        print("✗ 没有任何序列导出（主库开放段全空？）")
        return 1

    # 成本缓存：必须存在、必须新鲜、必须覆盖全部导出品种
    cache_src = os.path.join(args.data_root, "costs", "spread_model.json")
    if not os.path.exists(cache_src):
        print(f"✗ 缺 {cache_src}——研究侧无 tick 库（拒读），成本只能来自"
              "缓存文件；先跑 scripts/build_cost_model.py 再导出研究库")
        return 1
    try:
        verify_cost_cache(args.data_root, cache_src, symbols)
    except ValueError as e:
        print(f"✗ {e}")
        return 1
    _copy_file(cache_src, os.path.join(args.dest, "costs",
                                       "spread_model.json"))

    # specs 必需（合约大小/手数上下限）；calibrated 与时钟样本有则复制
    specs_src = os.path.join(args.data_root, "specs", "latest.json")
    if not os.path.exists(specs_src):
        print(f"✗ 缺 {specs_src}——研究侧 resolve_contract_size / "
              "load_volume_limits 无来源；先跑 scripts/snapshot_specs.py")
        return 1
    _copy_file(specs_src, os.path.join(args.dest, "specs", "latest.json"))
    for rel in ("costs/calibrated.json", "server_clock.json"):
        src = os.path.join(args.data_root, rel)
        if os.path.exists(src):
            _copy_file(src, os.path.join(args.dest, rel))

    # 导出自检：研究库里任何序列 last_time 都必须早于 cutoff
    if global_cutoff is not None:
        violations = verify_research_store(args.dest, global_cutoff)
    else:
        violations = []
        for ref in inventory:
            cut = cutoff_for(lockbox, ref.symbol, ref.timeframe)
            if cut is not None:
                violations.extend(verify_research_store(args.dest, cut))
    if violations:
        for v in violations:
            print(f"✗ 越界：{v}")
        print(f"研究库自检失败：{len(violations)} 条越界序列——导出作废，"
              "请重跑或排查写入路径")
        return 2

    # 外部数据（T67/T81）：--include-ext 才纳入；白名单与 cutoff 硬校验
    ext_exported = ext_skipped = ext_rejected = 0
    if args.include_ext:
        try:
            ext_exported, ext_skipped, ext_rejected = export_ext_series(
                args.ext_root, args.dest, global_cutoff, now_iso=now_iso)
        except ExtDataError as e:
            print(f"✗ 外部数据导出失败：{e}")
            return 1
        if ext_rejected:
            print(f"⚠ 白名单拒绝 {ext_rejected} 个未批准序列（不进研究库，"
                  "数据保留主库侧 data/ext/ 作一致性核对）")
        if global_cutoff is not None:
            ext_violations = verify_ext_research_store(args.dest,
                                                       global_cutoff)
            if ext_violations:
                for v in ext_violations:
                    print(f"✗ 越界：{v}")
                print("研究库 ext 段自检失败——外部数据导出作废")
                return 2
    else:
        print("· 外部数据未纳入（默认不启用；确认数据源后用 --include-ext，"
              "只导出批准来源）")

    print(f"摘要：导出 {exported} 个序列（跳过 {skipped}）"
          + (f"，外部数据 {ext_exported} 个序列（跳过 {ext_skipped}，"
             f"白名单拒绝 {ext_rejected}）" if args.include_ext else "")
          + f"，成本缓存与 specs 已复制 → {args.dest}；自检通过"
            "（所有 last_time < cutoff）。tick 不导出：研究侧成本经 "
            "data_research/costs/spread_model.json 缓存提供；"
            "lockbox.json 不复制（研究库无锁箱段）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
