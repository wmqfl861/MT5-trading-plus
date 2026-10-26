"""链头锚定（T37/M6/D27；R6/C5 链式锚 + 校验模式；R7/N-I3 历史前缀核验）：
把台账哈希链的链头追加写进 git 管理的 Markdown——docs/ops/ledger-anchors.md
（R6/C5 起从 docs/research/ 挪入 docs/ops/：锚文件是研究角色**白名单外**的
证据，研究账户改不了自己的锚）。

用法（计划任务每日跑，管理员把该文件随每日提交入 git）：
    .venv/Scripts/python.exe scripts/ledger_anchor.py
    .venv/Scripts/python.exe scripts/ledger_anchor.py \
        --ledger data/research/ledger.sqlite \
        --anchor docs/ops/ledger-anchors.md
    .venv/Scripts/python.exe scripts/ledger_anchor.py --verify   # 校验模式

- 只读打开台账（sqlite URI mode=ro：绝不触发迁移、绝不写台账文件）；
- 每次运行把 runs/gates/lockbox_uses 三条链的（行数， 链头 row_hash）连同
  UTC 时刻各追加一行；三链（行数， 链头）与已记录的完全一致 → 不重复写
  （幂等，计划任务重复跑不会堆重复行）；
- 链式锚（R6/C5）：每条锚定行带 prev_anchor（上一条锚的 anchor_hash，
  首条接 GENESIS）和 anchor_hash = sha256(规范化内容 + prev_anchor)——
  锚文件自成一条链，改历史锚行会破坏其后所有锚的哈希；
- 历史前缀核验（R7/N-I3）：行数**增长**不再一律放行——按锚定时刻重放
  锚定位置处的前缀链头，必须与锚记录一致（"重写历史→重封链→再追加"
  由此暴露）；write_anchor 追加新锚前先做同一核验，前缀被改写时拒绝
  收编。重放按锚定时刻取行内容：gates 重录 / lockbox started→done 这类
  **有归档**的合法改写用 ledger_events 里的 old_content，不误报；
- 为什么锚定有用（M6 的威胁模型：研究账户可直接写台账）：链上改一行或删
  一行会被 `bt ledger verify` 报出，但改写者可以整链重算；链头进了 git
  之后，重算出的新链头与锚定记录不符即暴露——删最后一行则行数对不上，
  重写历史再追加则前缀重放对不上（锚定位置的链头是 sha256 原像，造不出）。
  --verify 把两面对起来：锚文件链完整性 + 台账现状与锚历史的比对。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = REPO_ROOT / "data" / "research" / "ledger.sqlite"
# R6/C5：锚文件挪到 docs/ops/（研究角色白名单外；docs/research/** 可被
# 研究账户改写，锚放在那里等于让被审计者保管审计证据）。
DEFAULT_ANCHOR = REPO_ROOT / "docs" / "ops" / "ledger-anchors.md"

CHAIN_TABLES = ("runs", "gates", "lockbox_uses")
GENESIS_ANCHOR = "0" * 64

_HEADER = """\
# 台账链头锚定（T37/M6/D27；R6/C5 链式锚）

由 `scripts/ledger_anchor.py` 生成；管理员每日提交本文件（计划任务）。
链头 = 各链式表按 rowid 最后一行的 `row_hash`；行数为该表当前行数。
每条锚带 prev_anchor（上一条锚的 anchor_hash，首条接全零）与
anchor_hash = sha256(内容 + prev_anchor)——锚文件自成链，改任何历史
锚都会破坏其后所有锚。与本文件记录不符的台账即发生过改写/删行（即便
整链被重算）；比对用 `scripts/ledger_anchor.py --verify`。

| 锚定 UTC | 台账 | 表 | 行数 | 链头 row_hash | prev_anchor | anchor_hash |
| --- | --- | --- | --- | --- | --- | --- |
"""

_ROW_RE = re.compile(
    r"^\|\s*(?P<ts>[^|]+?)\s*\|\s*(?P<ledger>[^|]+?)\s*\|"
    r"\s*(?P<table>[^|]+?)\s*\|\s*(?P<rows>\d+)\s*\|"
    r"\s*(?P<head>[^|]*?)\s*\|\s*(?P<prev>[0-9a-f]{64})\s*\|"
    r"\s*(?P<hash>[0-9a-f]{64})\s*\|$")


def _parse_head(raw: str):
    """锚文件里的链头列 → row_hash 或 None（空表记法）。"""
    s = raw.strip()
    return None if (not s or s.startswith("（")) else s


def _parse_anchor_line(line: str):
    m = _ROW_RE.match(line)
    if m is None:
        return None
    return {"ts": m.group("ts").strip(), "ledger": m.group("ledger").strip(),
            "table": m.group("table").strip(), "rows": int(m.group("rows")),
            "head": _parse_head(m.group("head")),
            "prev": m.group("prev"), "hash": m.group("hash")}


def _read_entries(anchor_path) -> list[dict]:
    p = Path(anchor_path)
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        e = _parse_anchor_line(line)
        if e is not None:
            out.append(e)
    return out


def _anchor_digest(ts: str, ledger: str, table: str, rows: int,
                   head, prev: str) -> str:
    """链式锚哈希：sha256(规范化锚内容 + prev_anchor)——内容含上一条锚
    的哈希，改任何历史锚行都会使其后所有锚的 anchor_hash 对不上。"""
    canon = json.dumps({"ts": ts, "ledger": ledger, "table": table,
                        "rows": int(rows), "head": head,
                        "prev_anchor": prev},
                       sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _ro_connect(ledger_path) -> sqlite3.Connection:
    """只读连接（mode=ro：绝不触发迁移、绝不写台账文件）。"""
    p = Path(ledger_path)
    if not p.is_file():
        raise SystemExit(f"✗ 台账不存在：{p}（先跑 bt screen 生成台账）")
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _chain_heads_on(conn: sqlite3.Connection,
                    ledger_path) -> dict[str, dict]:
    """在既有只读连接上取各链式表的 {行数, 链头 row_hash}（空表 None）。"""
    for t in CHAIN_TABLES:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
        if "row_hash" not in cols:
            raise SystemExit(
                f"✗ 台账 {Path(ledger_path)} 的 {t} 表无 row_hash 列"
                "（旧库未迁移）——先用新代码打开一次台账（Ledger 自动迁移），"
                "再跑锚定（T37）")
    out: dict[str, dict] = {}
    for t in CHAIN_TABLES:
        n = int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        head = conn.execute(
            f"SELECT row_hash FROM {t} ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        out[t] = {"rows": n, "head": head[0] if head else None}
    return out


def chain_heads(ledger_path) -> dict[str, dict]:
    """各链式表的 {行数, 链头 row_hash}（只读连接；空表链头为 None）。"""
    conn = _ro_connect(ledger_path)
    try:
        return _chain_heads_on(conn, ledger_path)
    finally:
        conn.close()


# ------------------------------------------------ R7/N-I3：历史前缀核验

_TABLE_PK = {"runs": ("run_id",), "gates": ("run_id", "gate"),
             "lockbox_uses": ("card_id",)}


def _content_cols(conn: sqlite3.Connection, table: str) -> list[str]:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    return [c for c in cols if c not in ("prev_hash", "row_hash")]


def _canonical(content: dict) -> str:
    """与 mt5quant.research.ledger._canonical_json 同一规范化（键排序、
    紧凑分隔符、非 ASCII 原样）——重放的 row_hash 才能与台账算法对上。"""
    return json.dumps(content, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def _parse_ts(raw):
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def _events_timeline(conn: sqlite3.Connection,
                     table: str) -> dict[tuple, list]:
    """ledger_events 中该表的归档改写事件 → {行身份: [(时刻, old, new), …]}
    （event_id 序）。行身份取事件 new_content 里的主键字段——与
    ledger._update_chained 归档时的完整行内容一致。"""
    out: dict[tuple, list] = {}
    for r in conn.execute(
            "SELECT old_content_json, new_content_json, recorded_utc"
            " FROM ledger_events WHERE table_name=? ORDER BY event_id",
            (table,)).fetchall():
        try:
            old = json.loads(r["old_content_json"])
            new = json.loads(r["new_content_json"])
        except (TypeError, ValueError):
            continue     # 归档不可解析：重放退回当前内容（对不上会如实报警）
        key = tuple(new.get(k) for k in _TABLE_PK[table])
        out.setdefault(key, []).append(
            (_parse_ts(r["recorded_utc"]), old, new))
    return out


def _replay_head_at(conn: sqlite3.Connection, table: str, n_rows: int,
                    as_of_ts) -> str | None:
    """按 rowid 序重放**前 n_rows 行**的哈希链，返回该位置的链头。

    行内容取**锚定时刻**的状态（R7/N-I3）：该行若有晚于锚定时刻的归档
    改写事件（合法状态更新：gates 重录 / lockbox started→done），用首笔
    晚于锚定的事件的 old_content——即锚定当时的行内容；否则用当前内容。
    合法改写不误报；重写历史后重封链（无归档可依，或归档也对不上）则
    前缀链头必与锚记录不符——锚定位置的链头是 sha256 原像，造不出来。
    """
    cols = _content_cols(conn, table)
    sel = ", ".join(f'"{c}"' for c in cols)
    rows = conn.execute(
        f"SELECT rowid AS _rid, {sel} FROM {table} ORDER BY rowid"
        " LIMIT ?", (n_rows,)).fetchall()
    if not rows:
        return None
    timeline = _events_timeline(conn, table)
    pk = _TABLE_PK[table]
    anchor_dt = _parse_ts(as_of_ts) if as_of_ts else None
    prev = GENESIS_ANCHOR
    for r in rows:
        content = {c: r[c] for c in cols}
        later = [e for e in timeline.get(tuple(content.get(k)
                                               for k in pk), [])
                 if anchor_dt is not None and e[0] is not None
                 and e[0] > anchor_dt]
        if later:
            content = later[0][1]          # 锚定当时的行内容（归档 old）
        prev = hashlib.sha256(
            (_canonical(content) + prev).encode("utf-8")).hexdigest()
    return prev


def _prefix_problems(conn: sqlite3.Connection, heads: dict, entries: list,
                     label: str) -> list[str]:
    """台账现状 vs 每表**最近一条**锚：行数减少报删行；行数相同或增长都
    按锚定时刻重放锚定位置的前缀链头，与锚记录不符报改写（R7/N-I3：
    此前增长一律放行，"重写历史→重封链→再追加"检不出）。"""
    problems: list[str] = []
    for t in CHAIN_TABLES:
        mine = [e for e in entries
                if e["table"] == t and e["ledger"] == label]
        if not mine:
            problems.append(
                f"{t}：锚文件没有该台账该表的锚定历史（缺历史锚）")
            continue
        last = mine[-1]
        cur_rows = heads[t]["rows"]
        if cur_rows < last["rows"]:
            problems.append(
                f"{t}：现有 {cur_rows} 行 < 最近锚定 {last['rows']} 行"
                "（锚定后被删行或回滚）")
            continue
        replayed = _replay_head_at(conn, t, last["rows"], last["ts"])
        if replayed != last["head"]:
            if cur_rows == last["rows"]:
                problems.append(
                    f"{t}：行数与锚定相同但链头不符（整链被重算改写）")
            else:
                problems.append(
                    f"{t}：现有 {cur_rows} 行 > 最近锚定 {last['rows']} 行，"
                    "但按锚定时刻重放该位置的前缀链头与锚记录不符"
                    "（重写历史后重封链再追加）")
    return problems


def _ledger_label(ledger_path) -> str:
    p = Path(ledger_path)
    try:
        return p.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def write_anchor(anchor_path, ledger_path, *, now=None) -> bool:
    """追加锚定行（链式锚：prev = 锚文件最后一条 anchor_hash）；三链
    （行数， 链头）与既有记录完全一致 → False（幂等）。

    R7/N-I3：追加新锚**之前**先做历史前缀核验（台账现状 vs 既有锚）——
    前缀被改写（重写历史后重封链、删行回滚）→ RuntimeError 拒绝补写
    （锚文件不得收编被改写的历史），保持锚文件原文不变。
    返回 True = 本次写入了新行。
    """
    conn = _ro_connect(ledger_path)
    try:
        heads = _chain_heads_on(conn, ledger_path)
        p = Path(anchor_path)
        existing = p.read_text(encoding="utf-8") if p.exists() else ""
        entries = _read_entries(p)
        label = _ledger_label(ledger_path)
        if entries:
            prefix = _prefix_problems(conn, heads, entries, label)
            if prefix:
                raise RuntimeError(
                    "锚定前历史前缀核验失败，拒绝追加新锚（R7/N-I3：锚文件"
                    "不得收编被改写的历史——请人工比对锚文件与台账核查）：\n"
                    "  - " + "\n  - ".join(prefix))
        # 幂等：该台账在文件里已记录过完全相同的（表， 行数， 链头）三元组
        recorded = {(e["table"], e["rows"], e["head"]) for e in entries}
        current = {(t, heads[t]["rows"], heads[t]["head"])
                   for t in CHAIN_TABLES}
        if current <= recorded:
            return False
        ts = (now or datetime.now(timezone.utc)).isoformat()
        prev = entries[-1]["hash"] if entries else GENESIS_ANCHOR
        lines = []
        for t in CHAIN_TABLES:
            head = heads[t]["head"]
            ah = _anchor_digest(ts, label, t, heads[t]["rows"], head, prev)
            lines.append(f"| {ts} | {label} | {t} | {heads[t]['rows']} | "
                         f"{head or '（空表）'} | {prev} | {ah} |")
            prev = ah
        with open(p, "a", encoding="utf-8") as f:
            if not existing:
                f.write(_HEADER)
            f.write("\n".join(lines) + "\n")
        return True
    finally:
        conn.close()


def verify_anchor(anchor_path, ledger_path) -> list[str]:
    """R6/C5 校验模式的核心（R7/N-I3 补历史前缀核验）：比对台账链头与
    锚文件历史，返回问题列表（空 = 一致）。

    - 锚文件不存在 / 无锚定记录 / 某表没有锚定条目 → 缺历史锚；
    - 逐条重算 anchor_hash（含 prev_anchor 链）→ 锚文件被改 / 锚链断裂；
    - 台账现状 vs 该表最近一条锚：行数 < 锚定 → 锚后被删行/回滚；
      行数相同或增长 → 按锚定时刻重放锚定位置的前缀链头（合法归档改写
      用 old_content 重放，不误报），与锚记录不符 → 重写历史后重封链
      （含重封后再追加）；重放一致 → 合法增长/合法状态更新（待下次
      锚定收编）。
    """
    problems: list[str] = []
    p = Path(anchor_path)
    if not p.is_file():
        return [f"锚文件不存在：{p}（缺历史锚——先跑 write 模式生成）"]
    entries = _read_entries(p)
    if not entries:
        return [f"锚文件 {p} 无任何锚定记录（缺历史锚）"]
    prev = GENESIS_ANCHOR
    for i, e in enumerate(entries, 1):
        if e["prev"] != prev:
            problems.append(
                f"锚#{i}（{e['table']}）：prev_anchor 与上一条锚不符"
                "（锚链断裂——锚文件被删行或被改）")
        expect = _anchor_digest(e["ts"], e["ledger"], e["table"], e["rows"],
                                e["head"], e["prev"])
        if e["hash"] != expect:
            problems.append(
                f"锚#{i}（{e['table']}）：anchor_hash 与内容不符"
                "（锚文件历史被改）")
        prev = e["hash"]
    conn = _ro_connect(ledger_path)
    try:
        heads = _chain_heads_on(conn, ledger_path)
        label = _ledger_label(ledger_path)
        problems.extend(_prefix_problems(conn, heads, entries, label))
        return problems
    finally:
        conn.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="ledger_anchor",
        description="把台账哈希链的链头锚定进 docs/ops/ledger-anchors.md"
                    "（T37/M6/D27；R6/C5 链式锚；只读台账，幂等追加；"
                    "--verify 为校验模式，不写锚文件）")
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER),
                    help=f"台账路径（缺省 {DEFAULT_LEDGER}）")
    ap.add_argument("--anchor", default=str(DEFAULT_ANCHOR),
                    help=f"锚定文件（缺省 {DEFAULT_ANCHOR}）")
    ap.add_argument("--verify", action="store_true",
                    help="校验模式：比对台账链头/行数与锚文件历史"
                         "（含锚链完整性；缺历史锚、锚后被改写都报出），"
                         "不写锚文件")
    args = ap.parse_args(argv)
    if args.verify:
        problems = verify_anchor(args.anchor, args.ledger)
        if problems:
            print(f"✗ 锚定校验失败（{len(problems)} 处）：")
            for q in problems:
                print(f"  - {q}")
            return 1
        print("✓ 锚定校验通过：锚文件链完整，三链（行数， 链头）与最近"
              "锚定一致或在锚定后合法增长")
        return 0
    heads = chain_heads(args.ledger)
    try:
        written = write_anchor(args.anchor, args.ledger)
    except RuntimeError as e:
        print(f"✗ {e}")
        return 1
    for t in CHAIN_TABLES:
        print(f"  {t}: {heads[t]['rows']} 行，链头 "
              f"{(heads[t]['head'] or '（空表）')[:16]}…")
    print(("✓ 已追加锚定记录：" if written else "✓ 链头未变，未重复写：")
          + str(Path(args.anchor)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
