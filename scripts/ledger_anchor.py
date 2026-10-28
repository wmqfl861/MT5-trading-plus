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
- 重封准入（T63b，T63 迁移收尾；T88/R13-K7 加保真前置）：迁移整链
  重算（如 runs 补 round_id 列改哈希内容）后，按锚定时刻重放前缀链头
  必然与锚记录不符——这正是反收编保护的目的；合法重封走**有审计记录
  的准入**：`--accept-rechain "<原因>" --old-head-file <json>
  --new-columns <json>`——前置条件：① 台账链干净（各链式表按当前内容
  整链重算 == 存量链头，行数 ≥ 最近锚定）；② 原因非空（写进注记）；
  ③ old-head 文件给出各表旧 {rows, head}；④ 旧头必须**等于锚文件最近
  锚定**（防伪造重封起点：锚定头是 git 内的 sha256 原像）；⑤ **保真
  检查（T88/K7）**——按迁移前的列集合（当前内容列 − --new-columns
  声明的新增列）重放旧前缀，必须等于旧头（迁移只加列、旧内容逐字节
  保留；加列之外改了任何一行旧内容，重放必对不上旧头——拒绝）。
  全部满足才豁免对应表的**改写类**前缀问题（缺历史锚 / 删行不豁免），
  追加的新锚附注记行 `> rechain accepted: <原因> from <旧头8位>；
  rechain_from=<heads>；new_columns=<声明>`（非表格行、不参与锚哈希，
  git 历史即审计链"旧头→新头"）。
  `--dump-heads <ledger>` 只读输出 {table: {rows, head}} JSON——生产
  备份台账头部取证（注意：备份若在最近锚定后追加过行，其头部≠锚文件
  最近锚定，此时 old-head 文件应从锚文件最近条目读取）。
  `--prove-fidelity <ledger> --old-head-file <json> --new-columns <json>`
  （T88）只读补证：按目标列集合重算旧前缀并与旧头比对——已发生的
  历史重封（T63/T80）事后保真证明即此模式，不写台账与锚文件。
- 按事件序号锚定（T88/R13-K7，d96c523 根因消除）：2026-10-28 起新
  锚行带第 8 列 events≤ = 锚定时该表在 ledger_events 的最大 event_id
  （计入 anchor_hash）；前缀重放按序号进行——event_id 大于该值的
  归档改写按其 old_content 回滚到锚定当时。event_id 单调且与写入/
  提交序一致（台账写事务 BEGIN IMMEDIATE 串行化），高水位与链头取自
  同一只读快照，同刻度两事件的先后不再依赖时钟粒度。旧锚行（无
  events≤ 列）兼容读取：按锚定时刻 recorded_utc ≥ 锚 ts 重放
  （d96c523 口径）。
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
# 台账链头锚定（T37/M6/D27；R6/C5 链式锚；T88 按事件序号）

由 `scripts/ledger_anchor.py` 生成；管理员每日提交本文件（计划任务）。
链头 = 各链式表按 rowid 最后一行的 `row_hash`；行数为该表当前行数。
每条锚带 prev_anchor（上一条锚的 anchor_hash，首条接全零）与
anchor_hash = sha256(内容 + prev_anchor)——锚文件自成链，改任何历史
锚都会破坏其后所有锚。与本文件记录不符的台账即发生过改写/删行（即便
整链被重算）；比对用 `scripts/ledger_anchor.py --verify`。
T88（2026-10-28）起新增第 8 列 events≤ = 锚定时该表在 ledger_events 的
最大 event_id——前缀重放按事件序号进行（event_id 大于该值的归档改写
按其 old_content 回滚到锚定当时），消除同刻度竞态（d96c523）；events≤
计入 anchor_hash。旧锚文件无此列 → 兼容按锚定时刻重放。以 `>` 起头的
行是审计注记（如 `> rechain accepted: …` 的重封准入记录，T88 起附
new_columns= 迁移新增列声明），不参与锚哈希；表格行之外的行解析时忽略。

| 锚定 UTC | 台账 | 表 | 行数 | 链头 row_hash | prev_anchor | anchor_hash | events≤ |
| --- | --- | --- | --- | --- | --- | --- | --- |
"""

_ROW_RE = re.compile(
    r"^\|\s*(?P<ts>[^|]+?)\s*\|\s*(?P<ledger>[^|]+?)\s*\|"
    r"\s*(?P<table>[^|]+?)\s*\|\s*(?P<rows>\d+)\s*\|"
    r"\s*(?P<head>[^|]*?)\s*\|\s*(?P<prev>[0-9a-f]{64})\s*\|"
    r"\s*(?P<hash>[0-9a-f]{64})\s*"
    r"(?:\|\s*(?P<events>\d+)\s*)?\|$")


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
            "prev": m.group("prev"), "hash": m.group("hash"),
            # T88：events≤（该表 ledger_events 高水位）；旧锚行无此列 →
            # None（重放退回按锚定时刻的兼容路径）
            "events": (int(m.group("events"))
                       if m.group("events") is not None else None)}


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
                   head, prev: str, events=None) -> str:
    """链式锚哈希：sha256(规范化锚内容 + prev_anchor)——内容含上一条锚
    的哈希，改任何历史锚行都会使其后所有锚的 anchor_hash 对不上。

    T88：events（该表 ledger_events 高水位）给定（新锚行）时计入规范化
    内容；缺省（旧锚行 / 兼容调用）不出现在内容里——旧锚行的
    anchor_hash 复算口径与 T37–T63b 完全一致，锚链不断。"""
    payload = {"ts": ts, "ledger": ledger, "table": table,
               "rows": int(rows), "head": head, "prev_anchor": prev}
    if events is not None:
        payload["events"] = int(events)
    canon = json.dumps(payload, sort_keys=True, ensure_ascii=False,
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
    """ledger_events 中该表的归档改写事件 → {行身份: [(event_id, 时刻,
    old, new), …]}（event_id 升序——T88：同刻度事件的先后按写入序）。
    行身份取事件 new_content 里的主键字段——与 ledger._update_chained
    归档时的完整行内容一致。"""
    out: dict[tuple, list] = {}
    for r in conn.execute(
            "SELECT event_id, old_content_json, new_content_json,"
            " recorded_utc FROM ledger_events WHERE table_name=?"
            " ORDER BY event_id", (table,)).fetchall():
        try:
            old = json.loads(r["old_content_json"])
            new = json.loads(r["new_content_json"])
        except (TypeError, ValueError):
            continue     # 归档不可解析：重放退回当前内容（对不上会如实报警）
        key = tuple(new.get(k) for k in _TABLE_PK[table])
        out.setdefault(key, []).append(
            (int(r["event_id"]), _parse_ts(r["recorded_utc"]), old, new))
    return out


def _events_max_id(conn: sqlite3.Connection, table: str) -> int:
    """该表在 ledger_events 的最大 event_id（T88 锚定高水位；无事件 → 0）。

    与链头取自**同一只读快照**：ledger 写事务 BEGIN IMMEDIATE 串行化、
    event_id AUTOINCREMENT 单调，快照之后提交的事件 id 必大于该值、
    快照之内的事件 id 必不大于该值——高水位是"锚定当时可见事件"的
    精确边界（与锚定 ts 无关，同刻度不产生歧义）。"""
    return int(conn.execute(
        "SELECT COALESCE(MAX(event_id), 0) FROM ledger_events"
        " WHERE table_name=?", (table,)).fetchone()[0])


def _replay_head_at(conn: sqlite3.Connection, table: str, n_rows: int,
                    as_of_ts=None, *, max_event_id=None,
                    cols: list | None = None) -> str | None:
    """按 rowid 序重放**前 n_rows 行**的哈希链，返回该位置的链头。

    行内容取**锚定位置当时**的状态：该行若有锚定之后的归档改写事件
    （合法状态更新：gates 重录 / lockbox started→done），用首笔这样的
    事件的 old_content——即锚定当时的行内容；否则用当前内容。合法改写
    不误报；重写历史后重封链（无归档可依，或归档也对不上）则前缀链头
    必与锚记录不符——锚定位置的链头是 sha256 原像，造不出来。

    T88 按事件序号重放（d96c523 根因消除）：max_event_id 给定（新锚行
    的 events≤ 列）时，"锚定之后"由 event_id > max_event_id 判定——
    event_id 单调且与写入/提交序一致（写事务 BEGIN IMMEDIATE 串行化），
    同刻度两事件的先后不再依赖时钟粒度，顺序确定且与写入序一致。
    未给定（旧锚兼容）退回按时刻：recorded_utc >= 锚定 ts（含同刻度，
    d96c523 口径——时钟粒度可使两者相同，严格 > 会漏掉归档误报）。

    cols（T88 保真检查）：重算用的内容列集合，缺省该表全部内容列；
    给定时按该子集规范化行内容（迁移保真：按迁移前列集合重算旧前缀）。
    """
    if cols is None:
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
        evs = timeline.get(tuple(content.get(k) for k in pk), [])
        if max_event_id is not None:
            later = [e for e in evs if e[0] > max_event_id]
        elif anchor_dt is not None:
            later = [e for e in evs
                     if e[1] is not None and e[1] >= anchor_dt]
        else:
            later = []
        if later:
            old = later[0][2]          # 锚定当时的行内容（归档 old）
            content = {c: old.get(c) for c in cols}
        prev = hashlib.sha256(
            (_canonical(content) + prev).encode("utf-8")).hexdigest()
    return prev


def _prefix_findings(conn: sqlite3.Connection, heads: dict,
                     entries: list, label: str) -> list[tuple[str, str, str]]:
    """台账现状 vs 每表**最近一条**锚 → [(表, 问题类别, 描述)]。

    类别（T63b 重封准入据此豁免）：missing = 该表无锚定历史（缺历史锚，
    不可豁免——旧头无从核验）；deleted = 行数减少（删行/回滚，迁移重算
    不会减行，不可豁免）；rewrite = 行数相同或增长但按锚定时刻重放锚定
    位置的前缀链头与锚记录不符（R7/N-I3：整链被重算改写——**唯一**可由
    --accept-rechain 豁免的类别，且须旧头核验通过）。"""
    findings: list[tuple[str, str, str]] = []
    for t in CHAIN_TABLES:
        mine = [e for e in entries
                if e["table"] == t and e["ledger"] == label]
        if not mine:
            findings.append((t, "missing",
                             f"{t}：锚文件没有该台账该表的锚定历史（缺历史锚）"))
            continue
        last = mine[-1]
        cur_rows = heads[t]["rows"]
        if cur_rows < last["rows"]:
            findings.append((t, "deleted",
                             f"{t}：现有 {cur_rows} 行 < 最近锚定"
                             f" {last['rows']} 行（锚定后被删行或回滚）"))
            continue
        replayed = _replay_head_at(conn, t, last["rows"], last["ts"],
                                   max_event_id=last.get("events"))
        if replayed != last["head"]:
            if cur_rows == last["rows"]:
                findings.append((t, "rewrite",
                                 f"{t}：行数与锚定相同但链头不符"
                                 "（整链被重算改写）"))
            else:
                findings.append((t, "rewrite",
                                 f"{t}：现有 {cur_rows} 行 > 最近锚定"
                                 f" {last['rows']} 行，但按锚定时刻重放"
                                 "该位置的前缀链头与锚记录不符"
                                 "（重写历史后重封链再追加）"))
    return findings


def _prefix_problems(conn: sqlite3.Connection, heads: dict, entries: list,
                     label: str) -> list[str]:
    """同 _prefix_findings，仅返回描述（--verify 沿用；行数**增长**不再
    一律放行——按锚定时刻重放锚定位置的前缀链头，与锚记录不符报改写
    （R7/N-I3）。"""
    return [msg for _t, _kind, msg in _prefix_findings(conn, heads, entries,
                                                       label)]


# ------------------------------------------- T63b：重封准入（--accept-rechain）

def _integrity_problems(conn: sqlite3.Connection, heads: dict) -> list[str]:
    """准入前置条件①（verify 语义的链完整性，复用现有链头计算）：各链式
    表按**当前内容**整链重放（_replay_head_at，as_of=None 即不作归档
    回溯）== 存量链头。不等即链不干净（内容被改而未重封，或链值伪造）——
    重封准入只接受"内容与链值一致、只是历史前缀与锚不符"的迁移重算链。"""
    problems: list[str] = []
    for t in CHAIN_TABLES:
        replayed = _replay_head_at(conn, t, heads[t]["rows"], None)
        if replayed != heads[t]["head"]:
            problems.append(
                f"{t}：存量链头 {(heads[t]['head'] or '（空表）')[:16]}…"
                f" 与按当前内容整链重算的链头 "
                f"{(replayed or '（空表）')[:16]}… 不符"
                "（链不干净——--accept-rechain 前置条件①拒绝：先核查/"
                "修复台账，再谈重封准入）")
    return problems


def _oldhead_problems(old_heads: dict, entries: list,
                      label: str) -> list[str]:
    """准入前置条件③④：old-head 文件里每个表头必须**等于锚文件该台账
    该表的最近锚定**（防伪造旧头——重封起点只能是 git 锚定过的头，锚定头
    是 sha256 原像，造不出；也不得夹带非链式表）。"""
    problems: list[str] = []
    for t, hh in old_heads.items():
        if t not in CHAIN_TABLES:
            problems.append(
                f"{t}：不是链式表（--old-head-file 只认 "
                f"{'/'.join(CHAIN_TABLES)}）")
            continue
        try:
            rows, head = int(hh["rows"]), hh["head"]
        except (KeyError, TypeError, ValueError):
            problems.append(f"{t}：old-head 条目须为 {{rows, head}}")
            continue
        mine = [e for e in entries
                if e["table"] == t and e["ledger"] == label]
        if not mine:
            problems.append(
                f"{t}：锚文件没有该台账该表的锚定历史，旧头无从核验"
                "（不可豁免）")
            continue
        last = mine[-1]
        if rows != last["rows"] or head != last["head"]:
            problems.append(
                f"{t}：old-head（{rows} 行/{head or '（空表）'}）与锚文件"
                f"最近锚定（{last['rows']} 行/"
                f"{last['head'] or '（空表）'}）不符——旧头必须等于锚文件"
                "里最近锚定的头（防伪造重封起点；备份台账在最近锚定后"
                "追加过行时，old-head 应取锚文件最近条目而非备份头部）")
    return problems


def _ledger_label(ledger_path) -> str:
    p = Path(ledger_path)
    try:
        return p.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return p.as_posix()


# -------------------------------------- T88/R13-K7：迁移保真检查（重封准入⑤）

def _fidelity_replay(conn: sqlite3.Connection, table: str, hh: dict,
                     declared: list, last: dict | None):
    """按**目标列集合**（当前内容列 − 声明新增列）重放 hh["rows"] 行的
    旧前缀 → (重算链头, 所用列集合)。last 给定时按该锚行的事件高水位/
    时刻回滚归档改写（旧前缀 = 旧锚定位置当时的状态）。"""
    cols = _content_cols(conn, table)
    old_cols = [c for c in cols if c not in set(declared)]
    replayed = _replay_head_at(
        conn, table, int(hh["rows"]),
        last["ts"] if last else None,
        max_event_id=(last.get("events") if last else None),
        cols=old_cols)
    return replayed, old_cols


def _fidelity_problems(conn: sqlite3.Connection, old_heads: dict,
                       new_columns: dict, entries, label) -> list[str]:
    """准入前置⑤（T88/K7 保真检查）：**迁移保留了旧内容**的证明——按
    迁移前的列集合（当前内容列 − new_columns 声明的新增列）重放旧头
    文件给定的旧前缀，必须等于旧头。迁移若在加列之外改了任何一行旧
    内容再整链重算，重放必对不上旧头（旧头是 git 锚定的 sha256 原像，
    造不出）——拒绝准入。声明的列必须在当前列集合里（防拼写错误绕过）；
    未声明新增列的表按全部当前列重算（从严：加了列不声明同样对不上）。
    """
    problems: list[str] = []
    for t in CHAIN_TABLES:
        if t not in old_heads:
            continue
        hh = old_heads[t]
        declared = list((new_columns or {}).get(t, []))
        cols = _content_cols(conn, t)
        unknown = [c for c in declared if c not in cols]
        if unknown:
            problems.append(
                f"{t}：保真检查无法进行——声明的新增列 {unknown} 不在该表"
                f"当前列集合 {cols} 里（--new-columns 与台账不符：拼写"
                "错误，或不是加列迁移）")
            continue
        mine = ([e for e in (entries or [])
                 if e["table"] == t and e["ledger"] == label]
                if label else [])
        last = mine[-1] if mine else None
        replayed, _old_cols = _fidelity_replay(conn, t, hh, declared, last)
        if replayed != hh["head"]:
            problems.append(
                f"{t}：保真检查失败——按迁移前列集合（除声明新增列 "
                f"{declared or '（无）'} 外全部内容列）重算前 "
                f"{int(hh['rows'])} 行得 "
                f"{(replayed or '（空表）')[:16]}…，不等于旧头 "
                f"{(hh['head'] or '（空表）')[:16]}…（迁移没有保留旧内容"
                "——加列之外还改了旧行内容，拒绝准入；旧头是 git 锚定的"
                " sha256 原像，改了旧行就对不上）")
    return problems


def prove_fidelity(ledger_path, old_heads: dict,
                   new_columns: dict | None = None) -> list[tuple]:
    """T88 事后补证（只读推导，绝不写台账与锚文件）：对 old_heads 各表
    按**目标列集合**（当前内容列 − new_columns 声明的新增列）重放旧
    前缀，返回 [(表, 行数, 重算链头, 旧头, 是否相等)]——全相等 = 该次
    重封的迁移保真成立（迁移只加了列、旧内容逐字节保留）。生产台账的
    两次历史重封（T63/T80）即用此模式补证。"""
    conn = _ro_connect(ledger_path)
    try:
        out: list[tuple] = []
        for t in CHAIN_TABLES:
            if t not in old_heads:
                continue
            hh = old_heads[t]
            declared = list((new_columns or {}).get(t, []))
            cols = _content_cols(conn, t)
            unknown = [c for c in declared if c not in cols]
            if unknown:
                out.append((t, int(hh["rows"]), None, hh["head"], False))
                continue
            replayed, _oc = _fidelity_replay(conn, t, hh, declared, None)
            out.append((t, int(hh["rows"]), replayed, hh["head"],
                        replayed == hh["head"]))
        return out
    finally:
        conn.close()


def write_anchor(anchor_path, ledger_path, *, now=None,
                 accept_rechain: str | None = None,
                 old_heads: dict | None = None,
                 new_columns: dict | None = None) -> bool:
    """追加锚定行（链式锚：prev = 锚文件最后一条 anchor_hash）；三链
    （行数， 链头）与既有记录完全一致 → False（幂等）。

    R7/N-I3：追加新锚**之前**先做历史前缀核验（台账现状 vs 既有锚）——
    前缀被改写（重写历史后重封链、删行回滚）→ RuntimeError 拒绝补写
    （锚文件不得收编被改写的历史），保持锚文件原文不变。

    T63b 重封准入（accept_rechain=非空原因 + old_heads=各表旧
    {rows, head}）：迁移整链重算后的**有审计记录的**重新锚定——前置
    条件（链干净 / 原因非空 / 旧头等于锚文件最近锚定）全过才豁免对应表
    的改写类前缀问题（缺历史锚 / 删行不豁免），新锚附
    "> rechain accepted: …；rechain_from=<heads>" 注记行。
    返回 True = 本次写入了新行。
    """
    conn = _ro_connect(ledger_path)
    try:
        heads = _chain_heads_on(conn, ledger_path)
        p = Path(anchor_path)
        existing = p.read_text(encoding="utf-8") if p.exists() else ""
        entries = _read_entries(p)
        label = _ledger_label(ledger_path)
        admitted: dict[str, dict] = {}      # 准入豁免的表 → 核验过的旧头
        if accept_rechain is not None:
            if not str(accept_rechain).strip():
                raise RuntimeError(
                    "--accept-rechain 需要非空原因（审计注记——重封准入"
                    "必须可追溯）")
            if not isinstance(old_heads, dict) or not old_heads:
                raise RuntimeError(
                    "--accept-rechain 必须同时提供 --old-head-file"
                    "（JSON {table: {rows, head}}；旧头须等于锚文件最近"
                    "锚定——可由 --dump-heads 取证备份台账、或从锚文件"
                    "最近条目读取构造）")
            bad_tables = [t for t in (new_columns or {})
                          if t not in CHAIN_TABLES]
            if bad_tables:
                raise RuntimeError(
                    f"--new-columns 只认 {'/'.join(CHAIN_TABLES)}，收到 "
                    f"{bad_tables}（重封准入声明与链式表不符）")
            guards = (_integrity_problems(conn, heads)
                      + _oldhead_problems(old_heads, entries, label)
                      + _fidelity_problems(conn, old_heads, new_columns or {},
                                           entries, label))
            if guards:
                raise RuntimeError(
                    "--accept-rechain 前置条件不满足，拒绝重封准入：\n"
                    "  - " + "\n  - ".join(guards))
            admitted = old_heads
        findings = (_prefix_findings(conn, heads, entries, label)
                    if entries else [])
        waived = {t for t, kind, _m in findings
                  if kind == "rewrite" and t in admitted}
        blocking = [msg for t, kind, msg in findings
                    if t not in waived]
        if blocking:
            raise RuntimeError(
                "锚定前历史前缀核验失败，拒绝追加新锚（R7/N-I3：锚文件"
                "不得收编被改写的历史——请人工比对锚文件与台账核查"
                + ("；--accept-rechain 只豁免旧头核验通过的改写类问题"
                   if accept_rechain is not None else "") + "）：\n"
                "  - " + "\n  - ".join(blocking))
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
            wm = _events_max_id(conn, t)     # T88：该表事件高水位入锚行
            ah = _anchor_digest(ts, label, t, heads[t]["rows"], head, prev,
                                events=wm)
            lines.append(f"| {ts} | {label} | {t} | {heads[t]['rows']} | "
                         f"{head or '（空表）'} | {prev} | {ah} | {wm} |")
            prev = ah
        if accept_rechain is not None:
            # 审计注记（非表格行，不参与锚哈希；git 历史即"旧头→新头"
            # 审计链）。from 列实际被豁免重封的表的旧头 8 位前缀。
            # T88：迁移声明的新增列一并入注记（保真检查的目标列集合）。
            old_json = json.dumps(admitted, sort_keys=True,
                                  ensure_ascii=False,
                                  separators=(",", ":"))
            nc_json = json.dumps(new_columns or {}, sort_keys=True,
                                 ensure_ascii=False,
                                 separators=(",", ":"))
            short = ", ".join(
                (admitted[t]["head"] or "（空表）")[:8] for t in sorted(waived))
            lines.append(f"> rechain accepted: {accept_rechain}"
                         + (f" from {short}" if short else "")
                         + f"；rechain_from={old_json}"
                         + f"；new_columns={nc_json}")
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
                                e["head"], e["prev"],
                                events=e.get("events"))
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
                    "--verify 为校验模式，不写锚文件；T63b：--accept-rechain"
                    " 迁移重算后的有审计记录重封准入，--dump-heads 只读取证；"
                    "T88/R13-K7：准入加保真前置 --new-columns，"
                    "锚行带 events≤ 事件高水位（前缀重放按序号），"
                    "--prove-fidelity 只读补证")
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER),
                    help=f"台账路径（缺省 {DEFAULT_LEDGER}）")
    ap.add_argument("--anchor", default=str(DEFAULT_ANCHOR),
                    help=f"锚定文件（缺省 {DEFAULT_ANCHOR}）")
    ap.add_argument("--verify", action="store_true",
                    help="校验模式：比对台账链头/行数与锚文件历史"
                         "（含锚链完整性；缺历史锚、锚后被改写都报出），"
                         "不写锚文件")
    ap.add_argument("--dump-heads", metavar="LEDGER",
                    help="只读输出该台账各链式表 {table: {rows, head}} 的"
                         " JSON（生产备份台账头部取证；输出即合法"
                         " --old-head-file 格式——但备份若在最近锚定后"
                         "追加过行，old-head 须改从锚文件最近条目读取）")
    ap.add_argument("--accept-rechain", metavar="原因", default=None,
                    help="T63b 重封准入（T88/R13-K7 加保真前置⑤）：迁移"
                    "整链重算后重新锚定的**有审计记录**通道——前置条件："
                    "链干净（按当前内容整链重算==存量链头，行数≥最近"
                    "锚定）+ 原因非空 + --old-head-file 且旧头等于锚文件"
                    "最近锚定 + 保真检查（--new-columns 声明新增列，按"
                    "其余列重算旧前缀必须等于旧头——迁移没有保留旧内容"
                    "即拒绝）；豁免旧头核验通过表的改写类前缀问题，新锚"
                    "附 rechain 注记（缺历史锚/删行不豁免）")
    ap.add_argument("--old-head-file", metavar="JSON",
                    help="--accept-rechain 的各表旧 {rows, head} JSON"
                    "（--dump-heads 取证或锚文件最近条目构造）")
    ap.add_argument("--new-columns", metavar="JSON", default=None,
                    help="--accept-rechain 的迁移声明新增列"
                    " {table: [列…]} JSON（T88/K7 保真检查：按其余列重算"
                    "旧前缀必须等于旧头；声明写进 rechain 注记）")
    ap.add_argument("--prove-fidelity", metavar="LEDGER", default=None,
                    help="T88 事后补证（只读推导，不写台账与锚文件）：按"
                    " --old-head-file 的旧头与 --new-columns 的目标列集合"
                    "重算旧前缀并比对——两次历史重封的保真证明即此模式")
    args = ap.parse_args(argv)
    if args.dump_heads:
        print(json.dumps(chain_heads(args.dump_heads), sort_keys=True,
                         ensure_ascii=False, indent=2))
        return 0
    if args.prove_fidelity:
        if not args.old_head_file:
            print("✗ --prove-fidelity 必须同时提供 --old-head-file"
                  "（JSON {table: {rows, head}}——重封起点，通常取自锚文件"
                  "历史条目）")
            return 1
        try:
            old_heads = json.loads(
                Path(args.old_head_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"✗ --old-head-file 读取/解析失败：{e}")
            return 1
        new_columns: dict = {}
        if args.new_columns:
            try:
                new_columns = json.loads(args.new_columns)
            except ValueError as e:
                print(f"✗ --new-columns 解析失败（须为 JSON"
                      f" {{table: [列…]}}）：{e}")
                return 1
        label = _ledger_label(args.prove_fidelity)
        declared = {t: list(new_columns.get(t, []))
                    for t in old_heads if t in CHAIN_TABLES}
        print(f"保真补证（只读）：{label}，目标列集合 = 当前列 − 声明新增列"
              f" {json.dumps(declared, ensure_ascii=False)}")
        ok = True
        for t, n_rows, replayed, old, eq in prove_fidelity(
                args.prove_fidelity, old_heads, new_columns):
            if eq:
                print(f"  ✓ {t}：前 {n_rows} 行按目标列集合重算 == 旧头 "
                      f"{(old or '（空表）')[:16]}…（迁移保真成立）")
            else:
                ok = False
                print(f"  ✗ {t}：前 {n_rows} 行重算 "
                      f"{(replayed or '（空表）')[:16]}… != 旧头 "
                      f"{(old or '（空表）')[:16]}…（旧内容未被保留或声明"
                      "不符）")
        return 0 if ok else 1
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
    kwargs = {}
    if args.accept_rechain is not None:
        if not args.accept_rechain.strip():
            print("✗ --accept-rechain 需要非空原因（审计注记——重封准入"
                  "必须可追溯）")
            return 1
        if not args.old_head_file:
            print("✗ --accept-rechain 必须同时提供 --old-head-file"
                  "（JSON {table: {rows, head}}；旧头须等于锚文件最近锚定）")
            return 1
        try:
            old_heads = json.loads(
                Path(args.old_head_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"✗ --old-head-file 读取/解析失败：{e}")
            return 1
        if not isinstance(old_heads, dict) or not old_heads:
            print("✗ --old-head-file 须为非空 JSON 对象"
                  " {table: {rows, head}}")
            return 1
        new_columns = {}
        if args.new_columns:
            try:
                new_columns = json.loads(args.new_columns)
            except ValueError as e:
                print(f"✗ --new-columns 解析失败（须为 JSON"
                      f" {{table: [列…]}}）：{e}")
                return 1
            if (not isinstance(new_columns, dict)
                    or not all(isinstance(v, list)
                               and all(isinstance(c, str) for c in v)
                               for v in new_columns.values())):
                print("✗ --new-columns 须为 JSON {table: [列名…]}——"
                      "各表的新增列清单")
                return 1
        kwargs = {"accept_rechain": args.accept_rechain,
                  "old_heads": old_heads,
                  "new_columns": new_columns}
    heads = chain_heads(args.ledger)
    try:
        written = write_anchor(args.anchor, args.ledger, **kwargs)
    except RuntimeError as e:
        print(f"✗ {e}")
        return 1
    for t in CHAIN_TABLES:
        print(f"  {t}: {heads[t]['rows']} 行，链头 "
              f"{(heads[t]['head'] or '（空表）')[:16]}…")
    if written and kwargs:
        print("✓ 重封准入接受：新锚已附 rechain 注记（旧头→新头审计链）")
    print(("✓ 已追加锚定记录：" if written else "✓ 链头未变，未重复写：")
          + str(Path(args.anchor)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
