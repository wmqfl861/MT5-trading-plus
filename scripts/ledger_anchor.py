"""链头锚定（T37/M6/D27）：把台账哈希链的链头追加写进 git 管理的 Markdown。

用法（计划任务每日跑，管理员把该文件随每日提交入 git）：
    .venv/Scripts/python.exe scripts/ledger_anchor.py
    .venv/Scripts/python.exe scripts/ledger_anchor.py \
        --ledger data/research/ledger.sqlite \
        --anchor docs/research/ledger-anchors.md

- 只读打开台账（sqlite URI mode=ro：绝不触发 T37 迁移、绝不写台账文件）；
- 每次运行把 runs/gates/lockbox_uses 三条链的（行数， 链头 row_hash）连同
  UTC 时刻各追加一行；三链（行数， 链头）与已记录的完全一致 → 不重复写
  （幂等，计划任务重复跑不会堆重复行）；
- 为什么锚定有用（M6 的威胁模型：研究账户可直接写台账）：链上改一行或删
  一行会被 `bt ledger verify` 报出，但改写者可以整链重算；链头进了 git
  之后，重算出的新链头与锚定记录不符即暴露——删最后一行则行数对不上。
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = REPO_ROOT / "data" / "research" / "ledger.sqlite"
DEFAULT_ANCHOR = REPO_ROOT / "docs" / "research" / "ledger-anchors.md"

CHAIN_TABLES = ("runs", "gates", "lockbox_uses")

_HEADER = """\
# 台账链头锚定（T37/M6/D27）

由 `scripts/ledger_anchor.py` 生成；管理员每日提交本文件（计划任务）。
链头 = 各链式表按 rowid 最后一行的 `row_hash`；行数为该表当前行数。
与本文件记录不符的台账即发生过改写/删行（即便整链被重算）——
比对用 `bt ledger verify` + 本表。

| 锚定 UTC | 台账 | 表 | 行数 | 链头 row_hash |
| --- | --- | --- | --- | --- |
"""

_ROW_RE = re.compile(
    r"^\|\s*(?P<ts>[^|]+?)\s*\|\s*(?P<ledger>[^|]+?)\s*\|"
    r"\s*(?P<table>[^|]+?)\s*\|\s*(?P<rows>\d+)\s*\|"
    r"\s*(?P<head>[^|]*?)\s*\|$")


def chain_heads(ledger_path) -> dict[str, dict]:
    """各链式表的 {行数, 链头 row_hash}（只读连接；空表链头为 None）。"""
    p = Path(ledger_path)
    if not p.is_file():
        raise SystemExit(f"✗ 台账不存在：{p}（先跑 bt screen 生成台账）")
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        out: dict[str, dict] = {}
        for t in CHAIN_TABLES:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
            if "row_hash" not in cols:
                raise SystemExit(
                    f"✗ 台账 {p} 的 {t} 表无 row_hash 列（旧库未迁移）——"
                    "先用新代码打开一次台账（Ledger 自动迁移），"
                    "再跑锚定（T37）")
            n = int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
            head = conn.execute(
                f"SELECT row_hash FROM {t} ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            out[t] = {"rows": n, "head": head[0] if head else None}
        return out
    finally:
        conn.close()


def _ledger_label(ledger_path) -> str:
    p = Path(ledger_path)
    try:
        return p.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def write_anchor(anchor_path, ledger_path, *, now=None) -> bool:
    """追加锚定行；三链（行数， 链头）与既有记录完全一致 → False（幂等）。

    返回 True = 本次写入了新行。
    """
    heads = chain_heads(ledger_path)
    p = Path(anchor_path)
    existing = p.read_text(encoding="utf-8") if p.exists() else ""
    label = _ledger_label(ledger_path)
    # 幂等：该台账在文件里已记录过完全相同的（表， 行数， 链头）三元组
    recorded: set[tuple[str, int, str | None]] = set()
    for line in existing.splitlines():
        m = _ROW_RE.match(line)
        if not m:
            continue
        head = m.group("head").strip()
        recorded.add((m.group("table").strip(), int(m.group("rows")),
                      None if head.startswith("（") else head))
    current = {(t, heads[t]["rows"], heads[t]["head"])
               for t in CHAIN_TABLES}
    if current <= recorded:
        return False
    ts = (now or datetime.now(timezone.utc)).isoformat()
    lines = [f"| {ts} | {label} | {t} | {heads[t]['rows']} | "
             f"{heads[t]['head'] or '（空表）'} |" for t in CHAIN_TABLES]
    with open(p, "a", encoding="utf-8") as f:
        if not existing:
            f.write(_HEADER)
        f.write("\n".join(lines) + "\n")
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="ledger_anchor",
        description="把台账哈希链的链头锚定进 docs/research/ledger-anchors.md"
                    "（T37/M6/D27；只读台账，幂等追加）")
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER),
                    help=f"台账路径（缺省 {DEFAULT_LEDGER}）")
    ap.add_argument("--anchor", default=str(DEFAULT_ANCHOR),
                    help=f"锚定文件（缺省 {DEFAULT_ANCHOR}）")
    args = ap.parse_args(argv)
    heads = chain_heads(args.ledger)
    written = write_anchor(args.anchor, args.ledger)
    for t in CHAIN_TABLES:
        print(f"  {t}: {heads[t]['rows']} 行，链头 "
              f"{(heads[t]['head'] or '（空表）')[:16]}…")
    print(("✓ 已追加锚定记录：" if written else "✓ 链头未变，未重复写：")
          + str(Path(args.anchor)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
