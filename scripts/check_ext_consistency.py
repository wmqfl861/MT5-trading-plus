"""外部数据 ↔ MT5 主库一致性核对（T67/铁律 19）——只写实测值，不设阈值。

对照 data/ext/<来源>/<品种>/<周期>/ 与主库 data/bars/<品种>/<周期>/ 的
重叠期，逐项输出：

1. bar 时刻对齐：MT5 标签按 Europe/Athens（EET，随 DST）换算成 UTC 后
   与外部时刻匹配的比例；逐月模式平移（夏令时错误 → 部月份 ±1h）；
   专门覆盖 3 月/10 月底切换周；
2. 收益相关性：H1 与 D1 的 close→close 收益皮尔逊相关（时刻精确匹配）；
3. 价格水平差：相对差 (ext−mt5)/mt5 的 p05/p50/p95/最大绝对值；
4. 缺口与节假日：最大相邻间隔、>3 天的间隔数、逐星期 bar 占比；
5. 周末结构：周六/周日 bar 占比（BTC 7×24 vs 外汇 5×24 的实测差异）。

用法（Git Bash）：
    .venv/Scripts/python.exe scripts/check_ext_consistency.py \
        --pair histdata/XAUUSD/H1:XAUUSD/H1
    .venv/Scripts/python.exe scripts/check_ext_consistency.py --all
    .venv/Scripts/python.exe scripts/check_ext_consistency.py --all \
        --out docs/research/ext-consistency-raw.md
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from mt5data.ext import consistency as cons  # noqa: E402
from mt5data.ext import load_ext_series  # noqa: E402
from mt5data.store import load_bars  # noqa: E402

# 品种名映射：外部源品种 → 主库品种（一致性核对按主库符号取数）
DEFAULT_PAIRS = [
    ("histdata", "XAUUSD", "H1", "XAUUSD", "H1"),
    ("histdata", "XAUUSD", "D1", "XAUUSD", "D1"),
    ("histdata", "EURUSD", "H1", "EURUSD", "H1"),
    ("histdata", "EURUSD", "D1", "EURUSD", "D1"),
    ("cdd", "BTCUSD", "D1", "BTCUSD", "D1"),
    ("fred", "USTEC", "D1", "USTEC", "D1"),
]


def _slice_overlap(ext_df: pd.DataFrame, mt5_df: pd.DataFrame
                   ) -> tuple[pd.DataFrame, pd.DataFrame]:
    lo = max(ext_df["time"].min(), mt5_df["time"].min())
    hi = min(ext_df["time"].max(), mt5_df["time"].max())
    e = ext_df[(ext_df["time"] >= lo) & (ext_df["time"] <= hi)] \
        .reset_index(drop=True)
    m = mt5_df[(mt5_df["time"] >= lo - pd.Timedelta(hours=26))
               & (mt5_df["time"] <= hi + pd.Timedelta(hours=26))] \
        .reset_index(drop=True)
    return e, m


def check_pair(ext_root: str, data_root: str, src: str, sym: str, tf: str,
               msym: str, mtf: str, *,
               server_tz: str = cons.SERVER_TZ_DEFAULT) -> dict | None:
    loaded = load_ext_series(ext_root, src, sym, tf)
    if loaded is None:
        return {"pair": f"{src}/{sym}/{tf}", "error": "外部序列缺失"}
    ext_df, manifest = loaded
    mt5_df = load_bars(data_root, msym, mtf)
    if mt5_df is None or len(mt5_df) == 0:
        return {"pair": f"{src}/{sym}/{tf}↔{msym}/{mtf}",
                "error": "主库序列缺失"}
    ext_df, mt5_df = _slice_overlap(ext_df, mt5_df)

    out: dict = {
        "pair": f"{src}/{sym}/{tf} ↔ MT5 {msym}/{mtf}",
        "overlap": f"{ext_df['time'].min()} → {ext_df['time'].max()}",
        "ext_bars": int(len(ext_df)), "mt5_bars": int(len(mt5_df)),
        "price_side": manifest["price_side"],
        "server_tz": server_tz,
    }
    if tf in ("M1", "H1"):
        det = cons.detect_hour_misalignment(ext_df, mt5_df, server_tz=server_tz)
        out["时刻对齐"] = det
        out["切换周"] = cons.dst_switch_week_alignment(
            ext_df, mt5_df, server_tz=server_tz)
    rc = cons.returns_correlation(ext_df, mt5_df, server_tz=server_tz)
    out["收益相关"] = rc
    out["价格水平差"] = cons.price_level_diff(ext_df, mt5_df,
                                                server_tz=server_tz)
    out["缺口"] = {"ext": cons.gap_stats(ext_df), "mt5": cons.gap_stats(mt5_df)}
    out["周末"] = {"ext": cons.weekend_structure(ext_df),
                   "mt5": cons.weekend_structure(mt5_df)}
    return out


def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)


def render_markdown(reports: list[dict]) -> str:
    lines = ["# 外部数据 ↔ MT5 一致性核对（实测输出，机器生成）", ""]
    for r in reports:
        lines.append(f"## {r['pair']}")
        if "error" in r:
            lines.append(f"- 错误：{r['error']}")
            lines.append("")
            continue
        lines.append(f"- 重叠期：{r['overlap']}（ext {r['ext_bars']:,} 根 / "
                     f"MT5 {r['mt5_bars']:,} 根）")
        lines.append(f"- 外部价格口径：{r['price_side']}")
        if "时刻对齐" in r:
            a = r["时刻对齐"]
            lines.append(f"- 时刻对齐（MT5 标签按 server_tz={r.get('server_tz')}"
                         f" 换算成 UTC 后；本仓库实测服务器=UTC）："
                         f"匹配率 {a['matched_fraction']:.2%}，原始标签差的"
                         f"全年模式 {a['modal_offset_hours']}h，逐月模式平移"
                         f"分布 {sorted(set(a['converted_month_offsets'].values()))}"
                         f"，月间差 {a['offset_by_month_diff']}h，"
                         f"错位判定 {'是' if a['misaligned'] else '否'}")
            lines.append("  - 切换周（±3 天）：")
            for w in r["切换周"]:
                lines.append(f"    - {w['switch']}: ext {w['ext_bars']} 根，"
                             f"匹配 {w['matched']}"
                             f"（{'' if w['match_rate'] is None else format(w['match_rate'], '.2%')}）")
        rc = r["收益相关"]
        corr = "n/a" if rc["corr"] is None else f"{rc['corr']:.4f}"
        lines.append(f"- 收益相关（close→close，时刻精确匹配）："
                     f"n={rc['n']:,}，corr={corr}")
        p = r["价格水平差"]
        if p.get("n"):
            lines.append(f"- 价格水平差（相对，ext−mt5）/mt5：p50="
                         f"{p['rel_p50']:.3e}，p05={p['rel_p05']:.3e}，"
                         f"p95={p['rel_p95']:.3e}，max|·|={p['rel_max_abs']:.3e}"
                         f"（n={p['n']:,}；中位绝对差 {p['abs_p50']:.6g} 价格单位）")
        for side in ("ext", "mt5"):
            g = r["缺口"][side]
            wk = g.get("weekday_share")
            wk_s = "{" + ", ".join(f"{k}:{v:.1%}" for k, v in (wk or {}).items()) + "}"
            lines.append(f"- 缺口[{side}]：bars={g.get('bars')}，"
                         f"最大间隔={g.get('max_gap')}，"
                         f">3 天间隔数={g.get('gaps_gt_3d')}，"
                         f"星期占比={wk_s}")
        for side in ("ext", "mt5"):
            w = r["周末"][side]
            lines.append(f"- 周末[{side}]：周六占比={w['sat_share']}，"
                         f"周日占比={w['sun_share']}")
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="外部数据一致性核对（T67）")
    ap.add_argument("--ext-root", default="data/ext")
    ap.add_argument("--data-root", default="data")
    ap.add_argument("--pair", action="append", default=[],
                    help="格式 SOURCE/SYM/TF:MT5SYM/TF（可多次）")
    ap.add_argument("--all", action="store_true", help="核对全部默认配对")
    ap.add_argument("--server-tz", default=cons.SERVER_TZ_DEFAULT,
                    help="MT5 服务器时区（本仓库实测 UTC；EET 券商用 "
                         "Europe/Athens）")
    ap.add_argument("--out", default=None, help="把 markdown 实测写进文件")
    args = ap.parse_args(argv)

    pairs = []
    for spec in args.pair:
        left, right = spec.split(":")
        src, sym, tf = left.split("/")
        msym, mtf = right.split("/")
        pairs.append((src, sym, tf, msym, mtf))
    if args.all or not pairs:
        pairs = DEFAULT_PAIRS

    reports = [check_pair(args.ext_root, args.data_root, *p,
                          server_tz=args.server_tz) for p in pairs]
    md = render_markdown(reports)
    print(md)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(md)
        print(f"\n已写 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
