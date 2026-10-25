"""试验台账：SQLite（标准库 sqlite3），研究侧一切运行的可追溯登记。

表结构（按 P4 计划钉死；T21a/T21c 修订；T37 哈希链）：
- cards(card_id, version, path, git_commit, committed_at_utc)——主键
  (card_id, version)：版本迭代是**新行**，旧行不动（历史如实保留）；
- runs(run_id, card_id, card_version, strategy, params_json, symbol,
  timeframe, data_first, data_last, data_sha256, strategy_sha,
  engine_version, cost_model_version, gates_hash, metrics_json,
  created_utc, actor, prev_hash, row_hash)——
  **失败的运行也记一行**（metrics_json 里 ok=false + error 摘要）；
  strategy_sha（T37/H3/D23）：策略代码指纹（cards.strategy_fingerprint）
  ——试验身份含代码，改代码重跑即不同的试验（N 不再被低估）；老库迁移
  前的旧行补 "legacy"（指纹未知，不参与卡版本绑定）；
- gates(run_id, gate, passed, details_json, prev_hash, row_hash)——
  关卡结果（T12 消费）；
- lockbox_uses(card_id UNIQUE, used_at_utc, approved_by, result_json,
  status, cutoff, prev_hash, row_hash)——锁箱一次性：status ∈
  {"started", "done"}（T21a/C3）——G5 读锁箱数据**之前**先写
  started（record_lockbox_start），评估结束写结果（finish_lockbox_use
  → done）；已有 started 行（上次崩溃残留）同样视为已用（lockbox_used），
  第二次插入直接 sqlite3.IntegrityError。
  旧库无 status 列 → 打开时自动补列（旧行按终态 done 处理）。
  cutoff 列（T27/M11）：该次使用消耗的是哪个锁箱（ISO naive UTC）；
  旧库迁移补列，旧行 NULL = 预算制度前的既往锁箱，不计入当前 cutoff
  的预算计数。
- lockbox_budget(cutoff PRIMARY KEY, max_uses, created_utc)——项目级
  锁箱使用预算（T27/M11/D16：几十张卡各看一次锁箱 = 在锁箱上做选择）。
  lockbox_budget() 登记并返回状态；max_uses 首登记生效，缺省
  DEFAULT_LOCKBOX_MAX_USES=5（D16 待用户决定，未决定前硬编码默认 +
  参数可覆盖；调整预算是管理员改库动作，不改代码）。

哈希链（T37/M6/D27）：runs / gates / lockbox_uses 三表各一条按 rowid
串联的链，row_hash = sha256(规范化行 JSON + prev_hash)（规范化 = 全部
内容列、键排序、紧凑分隔符；prev_hash/row_hash 自身不入哈希）。首行
prev_hash = GENESIS_HASH。每次合法写入后整表重算（确定性：链值只依赖
行内容序列）——常规更新（如 lockbox started→done、gates 重录）自然保持
一致；手工 UPDATE/DELETE 不重算链 → verify_chain() 报出。整链被重算
改写瞒不过**每日锚定**（scripts/ledger_anchor.py 把链头写进 git，
D27）。链检不出"删最后一行"（无后继），锚定行的行数能检出。

旧库迁移（T37）：打开时自动补 strategy_sha/prev_hash/row_hash 列；旧行
strategy_sha='legacy'；三表哈希链自第一行重建；迁移本身在 runs 记一行
actor='migration'（card_id='__migration__'，计入 total_runs 可审计，
不计入去重 N——迁移不是试验）。

metrics_json 结构：{"ok": bool, "error": str|None, "metrics": dict|None}。
时间一律 aware-UTC ISO（created_utc 校验强制带时区——它要和卡的提交时间比）。

N 的口径（T21c/H4/M4；T37 加入 strategy_sha）：DSR 的试验次数 = 该**谱系**
（cards.family_of）中按 (strategy, params, symbol, timeframe, data_sha256,
cost_model_version, strategy_sha) 去重后的成功运行数（stats 的 n_dedup /
family_n_dedup），同时给出项目级 project_n_dedup——同参 screen+gate 只计
1 次，v1/v2 新卡累计进同一谱系；改策略代码（指纹变）= 新试验。
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from mt5quant.research.cards import family_of

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards(
  card_id           TEXT NOT NULL,
  version           INTEGER NOT NULL,
  path              TEXT NOT NULL,
  git_commit        TEXT NOT NULL,
  committed_at_utc  TEXT,
  registered_utc    TEXT NOT NULL,
  PRIMARY KEY (card_id, version)
);
CREATE TABLE IF NOT EXISTS runs(
  run_id             INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id            TEXT NOT NULL,
  card_version       INTEGER NOT NULL,
  strategy           TEXT NOT NULL,
  params_json        TEXT NOT NULL,
  symbol             TEXT NOT NULL,
  timeframe          TEXT NOT NULL,
  data_first         TEXT,
  data_last          TEXT,
  data_sha256        TEXT NOT NULL,
  strategy_sha       TEXT NOT NULL DEFAULT 'legacy',
  engine_version     TEXT NOT NULL,
  cost_model_version TEXT NOT NULL,
  gates_hash         TEXT NOT NULL DEFAULT '',
  metrics_json       TEXT NOT NULL,
  created_utc        TEXT NOT NULL,
  actor              TEXT NOT NULL,
  prev_hash          TEXT NOT NULL DEFAULT '',
  row_hash           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_card ON runs(card_id);
CREATE TABLE IF NOT EXISTS gates(
  run_id       INTEGER NOT NULL REFERENCES runs(run_id),
  gate         TEXT NOT NULL,
  passed       INTEGER NOT NULL,
  details_json TEXT NOT NULL,
  prev_hash    TEXT NOT NULL DEFAULT '',
  row_hash     TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, gate)
);
CREATE TABLE IF NOT EXISTS lockbox_uses(
  card_id     TEXT NOT NULL UNIQUE,
  used_at_utc TEXT NOT NULL,
  approved_by TEXT NOT NULL,
  result_json TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'done',
  cutoff      TEXT,
  prev_hash   TEXT NOT NULL DEFAULT '',
  row_hash    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS lockbox_budget(
  cutoff      TEXT NOT NULL PRIMARY KEY,
  max_uses    INTEGER NOT NULL,
  created_utc TEXT NOT NULL
);
"""

# T27/M11（D16 未决）：每个锁箱（cutoff）最多允许多少次 G5——
# 多张卡各看一次锁箱等于在锁箱上做选择，需要项目级预算。
# 蓝图 §8 的建议值是 5；用户对 D16 拍板前以此为硬编码默认，
# lockbox_budget(cutoff, max_uses=N) 首次登记时可覆盖。
DEFAULT_LOCKBOX_MAX_USES = 5

# T37/M6/D27：带哈希链的三张表；链首 prev_hash（常量，与"空串"的
# 未迁移状态可区分）。
_CHAIN_TABLES = ("runs", "gates", "lockbox_uses")
GENESIS_HASH = "0" * 64


def _san(obj):
    """递归把 NaN/inf 换成 None，保证 metrics_json 严格合法（同 report 口径）。"""
    if isinstance(obj, dict):
        return {k: _san(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_san(v) for v in obj]
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return f if math.isfinite(f) else None
    return obj


def _iso(dt) -> str:
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        raise ValueError(
            f"台账时间必须带时区（aware UTC），收到 naive {dt.isoformat()}"
            "——naive/aware 混用会让'先卡后测'的比较静默错位")
    return dt.astimezone(timezone.utc).isoformat()


def _cutoff_iso(cutoff) -> str | None:
    """锁箱 cutoff 的统一键形态：str 直通，datetime/pd.Timestamp 取
    isoformat（naive UTC 语义，与 lockbox.json 的存法一致）。None → None。"""
    if cutoff is None or isinstance(cutoff, str):
        return cutoff
    import pandas as pd

    if isinstance(cutoff, pd.Timestamp):
        return cutoff.isoformat()
    return cutoff.isoformat()


def _canonical_json(content: dict) -> str:
    """哈希链的规范化行 JSON：全部内容列、键排序、紧凑分隔符、非 ASCII
    原样（T37：同内容必得同串，才能跨进程/跨平台复算 row_hash）。"""
    return json.dumps(_san(content), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


class Ledger:
    """试验台账句柄；用完 close（或 with）。所有写入即时提交。"""

    def __init__(self, path, *, create: bool = True):
        p = Path(path)
        if create:
            p.parent.mkdir(parents=True, exist_ok=True)
        elif not p.exists():
            raise FileNotFoundError(f"台账不存在：{p}")
        self.path = str(p)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        with self.conn:
            self.conn.executescript(_SCHEMA)
        # T21a 迁移：老库的 lockbox_uses 无 status 列——补列，旧行按终态 done
        cols = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(lockbox_uses)")}
        if "status" not in cols:
            with self.conn:
                self.conn.execute(
                    "ALTER TABLE lockbox_uses ADD COLUMN status TEXT"
                    " NOT NULL DEFAULT 'done'")
        # T27 迁移：老库的 lockbox_uses 无 cutoff 列（预算制度前）——补列，
        # 旧行 NULL = 既往锁箱，不计入任何 cutoff 的预算计数
        if "cutoff" not in cols:
            with self.conn:
                self.conn.execute(
                    "ALTER TABLE lockbox_uses ADD COLUMN cutoff TEXT")
        # T37 迁移（H3/M6，D23/D27）：runs 加 strategy_sha/prev_hash/
        # row_hash，gates 与 lockbox_uses 加 prev_hash/row_hash；旧行
        # strategy_sha='legacy'，三链自第一行重建，迁移记一行
        # actor=migration（幂等：迁移过再打开不会重复记）。
        runs_cols = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(runs)")}
        pre_migration = "strategy_sha" not in runs_cols
        if pre_migration:
            with self.conn:
                self.conn.execute(
                    "ALTER TABLE runs ADD COLUMN strategy_sha TEXT"
                    " NOT NULL DEFAULT 'legacy'")
        for tbl in _CHAIN_TABLES:
            tcols = {r["name"] for r in self.conn.execute(
                f"PRAGMA table_info({tbl})")}
            for col in ("prev_hash", "row_hash"):
                if col not in tcols:
                    with self.conn:
                        self.conn.execute(
                            f"ALTER TABLE {tbl} ADD COLUMN {col} TEXT"
                            " NOT NULL DEFAULT ''")
        if pre_migration:
            counts = {t: int(self.conn.execute(
                f"SELECT COUNT(*) n FROM {t}").fetchone()["n"])
                for t in _CHAIN_TABLES}
            if any(counts.values()):
                payload = {"ok": True, "error": None, "metrics": {
                    "event": "t37-hash-chain-migration",
                    **{t: counts[t] for t in _CHAIN_TABLES},
                    "note": "旧行 strategy_sha=legacy；runs/gates/"
                            "lockbox_uses 三链自第一行重建"}}
                with self.conn:
                    self.conn.execute(
                        "INSERT INTO runs(card_id, card_version, strategy,"
                        " params_json, symbol, timeframe, data_first,"
                        " data_last, data_sha256, strategy_sha,"
                        " engine_version, cost_model_version, gates_hash,"
                        " metrics_json, created_utc, actor)"
                        " VALUES ('__migration__', 0, '__migration__',"
                        " '{}', '-', '-', NULL, NULL, '-', 'legacy',"
                        " 'ledger-v1', 'ledger-v1', '', ?, ?, 'migration')",
                        (json.dumps(payload, ensure_ascii=False),
                         datetime.now(timezone.utc).isoformat()))
            for tbl in _CHAIN_TABLES:
                self._rechain(tbl)

    # ------------------------------------------------ 哈希链（T37/M6/D27）

    def _rechain(self, table: str) -> None:
        """整表重算哈希链（rowid 序，确定性：链值只依赖行内容序列）。

        每次合法写入后调用——常规更新（lockbox started→done、gates 重录）
        天然保持一致；手工改库不走这里，verify_chain 负责报出。
        """
        cols = [r["name"] for r in self.conn.execute(
            f"PRAGMA table_info({table})")]
        content_cols = [c for c in cols if c not in ("prev_hash", "row_hash")]
        sel = ", ".join(f'"{c}"' for c in content_cols)
        rows = self.conn.execute(
            f"SELECT rowid AS _rid, {sel} FROM {table}"
            " ORDER BY rowid").fetchall()
        updates = []
        prev = GENESIS_HASH
        for r in rows:
            canon = _canonical_json({c: r[c] for c in content_cols})
            row_hash = hashlib.sha256(
                (canon + prev).encode("utf-8")).hexdigest()
            updates.append((prev, row_hash, r["_rid"]))
            prev = row_hash
        with self.conn:
            self.conn.executemany(
                f"UPDATE {table} SET prev_hash=?, row_hash=? WHERE rowid=?",
                updates)

    def verify_chain(self) -> list[str]:
        """校验三表哈希链；返回断链/篡改行的描述列表（空 = 完好）。

        - 行内容重算的 row_hash 与存量不符 → 该行被改过（篡改）；
        - 行的 prev_hash ≠ 上一行 row_hash（首行 ≠ GENESIS_HASH）→
          断链（典型：删行）；
        - 整链被重算改写检测不到（改写者也能重算）——那由每日锚定
          （ledger-anchors.md 里的链头 + 行数）对出，D27。
        """
        problems: list[str] = []
        for tbl, id_cols in (("runs", ("run_id",)),
                             ("gates", ("run_id", "gate")),
                             ("lockbox_uses", ("card_id",))):
            cols = [r["name"] for r in self.conn.execute(
                f"PRAGMA table_info({tbl})")]
            if "row_hash" not in cols:
                problems.append(f"{tbl}: 无 row_hash 列（旧库未迁移）")
                continue
            content_cols = [c for c in cols
                            if c not in ("prev_hash", "row_hash")]
            sel = ", ".join(f'"{c}"' for c in
                            content_cols + list(id_cols)
                            + ["prev_hash", "row_hash"])
            rows = self.conn.execute(
                f"SELECT rowid AS _rid, {sel} FROM {tbl}"
                " ORDER BY rowid").fetchall()
            prev = GENESIS_HASH
            prev_label = "链首"
            for r in rows:
                canon = _canonical_json({c: r[c] for c in content_cols})
                expect = hashlib.sha256(
                    (canon + prev).encode("utf-8")).hexdigest()
                if tbl == "runs":
                    label = f"runs run#{r['run_id']}"
                elif tbl == "gates":
                    label = f"gates run#{r['run_id']}/{r['gate']}"
                else:
                    label = f"lockbox_uses card={r['card_id']}"
                if r["row_hash"] != expect:
                    problems.append(
                        f"{label}: 行内容与 row_hash 不符（疑似篡改）")
                if r["prev_hash"] != prev:
                    problems.append(
                        f"{label}: prev_hash 与上一行（{prev_label}）的"
                        " row_hash 不符（断链或被删行）")
                prev = r["row_hash"]
                prev_label = label
        return problems

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------------------------------------------------------- 写

    def register_card(self, card_id: str, version: int, path: str,
                      git_commit: str, committed_at) -> None:
        """登记卡（幂等：同 (card_id, version) 重登覆盖为最新登记信息）。

        committed_at 可为 None（如实：卡还没提交；调用方应已先过
        ensure_card_precedes_run，正常路径不会出现 None 进来）。
        """
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO cards(card_id, version, path,"
                " git_commit, committed_at_utc, registered_utc)"
                " VALUES (?,?,?,?,?,?)",
                (card_id, version, path, git_commit,
                 committed_at.isoformat() if committed_at is not None else None,
                 datetime.now(timezone.utc).isoformat()))

    def record_run(self, *, card_id: str, card_version: int, strategy: str,
                   params: dict, symbol: str, timeframe: str,
                   data_first, data_last, data_sha256: str,
                   engine_version: str, cost_model_version: str,
                   gates_hash: str, metrics: dict | None,
                   error: str | None = None, actor: str = "bt",
                   created_utc=None, strategy_sha: str = "legacy") -> int:
        """记一行运行（成功的与抛异常的 alike）；返回 run_id。

        metrics 与 error 二选一（有 error 时 metrics 记 None）；
        created_utc 缺省取当前 UTC（测试可注入固定时间，须 aware）；
        strategy_sha（T37/D23）：策略代码指纹，缺省 'legacy'（调用方
        未计算的旧路径/迁移前语义——研究路径 bt.py 必须传真实指纹）。
        """
        payload = {"ok": error is None, "error": error, "metrics": metrics}
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO runs(card_id, card_version, strategy, params_json,"
                " symbol, timeframe, data_first, data_last, data_sha256,"
                " strategy_sha, engine_version, cost_model_version,"
                " gates_hash, metrics_json, created_utc, actor)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (card_id, card_version, strategy,
                 json.dumps(_san(params), ensure_ascii=False),
                 symbol, timeframe, data_first, data_last, data_sha256,
                 strategy_sha, engine_version, cost_model_version, gates_hash,
                 json.dumps(_san(payload), ensure_ascii=False),
                 _iso(created_utc), actor))
        self._rechain("runs")
        return int(cur.lastrowid)

    def record_gate(self, run_id: int, gate: str, passed: bool,
                    details: dict) -> None:
        """记一个关卡结果（run_id 不存在时外键报错，fail-fast）。"""
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO gates(run_id, gate, passed,"
                " details_json) VALUES (?,?,?,?)",
                (run_id, gate, 1 if passed else 0,
                 json.dumps(_san(details), ensure_ascii=False)))
        self._rechain("gates")

    def record_lockbox_use(self, card_id: str, *, approved_by: str,
                           result: dict, cutoff: str | None = None) -> None:
        """锁箱使用登记（直接写终态）：同一 card_id 第二次插入 →
        sqlite3.IntegrityError。不捕获、不绕过——"只能用一次"由 UNIQUE
        约束钉死。G5 的常规路径用 record_lockbox_start + finish_lockbox_use
        （T21a：读锁箱数据前先写 started，防中途崩溃后重跑）。
        cutoff（T27）：该次使用消耗的锁箱（ISO naive UTC），供预算计数。
        """
        with self.conn:
            self.conn.execute(
                "INSERT INTO lockbox_uses(card_id, used_at_utc, approved_by,"
                " result_json, status, cutoff) VALUES (?,?,?,?, 'done', ?)",
                (card_id, datetime.now(timezone.utc).isoformat(), approved_by,
                 json.dumps(_san(result), ensure_ascii=False),
                 _cutoff_iso(cutoff)))
        self._rechain("lockbox_uses")

    def record_lockbox_start(self, card_id: str, *, approved_by: str,
                             cutoff: str | None = None) -> None:
        """写 status='started' 行（T21a/C3：G5 读锁箱数据**之前**调用）。

        同一 card_id 已有任何行（含上次崩溃残留的 started）→
        sqlite3.IntegrityError（UNIQUE 约束）；started 行同样算已用。
        cutoff（T27）：预算计数口径，见 lockbox_budget。
        """
        with self.conn:
            self.conn.execute(
                "INSERT INTO lockbox_uses(card_id, used_at_utc, approved_by,"
                " result_json, status, cutoff)"
                " VALUES (?,?,?,?,'started',?)",
                (card_id, datetime.now(timezone.utc).isoformat(), approved_by,
                 "{}", _cutoff_iso(cutoff)))
        self._rechain("lockbox_uses")

    def finish_lockbox_use(self, card_id: str, *, result: dict,
                           approved_by: str = "bt",
                           cutoff: str | None = None) -> None:
        """评估结束写终态：started → done（保留 started 行的批准人与时刻）。

        无 started 行（run_g5 被直接调用的路径）→ 插入完整 done 行；
        已有 done 行 → sqlite3.IntegrityError（一次性机会已消耗）。
        cutoff 只在无 started 行需要插入时使用（started 行已带口径）。
        """
        payload = json.dumps(_san(result), ensure_ascii=False)
        with self.conn:
            cur = self.conn.execute(
                "UPDATE lockbox_uses SET result_json=?, status='done'"
                " WHERE card_id=? AND status='started'", (payload, card_id))
            if cur.rowcount:
                self._rechain("lockbox_uses")
                return
            exists = self.conn.execute(
                "SELECT 1 FROM lockbox_uses WHERE card_id=?",
                (card_id,)).fetchone()
            if exists:
                raise sqlite3.IntegrityError(
                    f"lockbox_uses.card_id={card_id} 已有终态记录"
                    "（UNIQUE 约束）——锁箱一次性已消耗")
            self.conn.execute(
                "INSERT INTO lockbox_uses(card_id, used_at_utc, approved_by,"
                " result_json, status, cutoff) VALUES (?,?,?,?, 'done', ?)",
                (card_id, datetime.now(timezone.utc).isoformat(), approved_by,
                 payload, _cutoff_iso(cutoff)))
        self._rechain("lockbox_uses")

    # ---------------------------------------------------------------- 读

    def lockbox_used(self, card_id: str) -> bool:
        """该卡的锁箱是否已用：**任何**行都算——包括上次崩溃残留的
        started（T21a：有 started 也算已用，防中途崩溃后重跑）。"""
        row = self.conn.execute(
            "SELECT 1 FROM lockbox_uses WHERE card_id=?", (card_id,)).fetchone()
        return row is not None

    def lockbox_status(self, card_id: str) -> str | None:
        """锁箱使用状态：None（没用过）| "started"（进行中/崩溃残留）| "done"。"""
        row = self.conn.execute(
            "SELECT status FROM lockbox_uses WHERE card_id=?",
            (card_id,)).fetchone()
        return None if row is None else str(row["status"])

    # ------------------------------------------------ 锁箱预算（T27/M11/D16）

    def lockbox_budget(self, cutoff, max_uses: int | None = None) -> dict:
        """登记（幂等）并返回该 cutoff 的锁箱使用预算状态（T27/M11）。

        - 首次见到该 cutoff → 写入 max_uses（缺省
          DEFAULT_LOCKBOX_MAX_USES=5，可参数覆盖；非正数 → ValueError）；
        - 已有行 → 表内值权威，参数不再覆盖（调预算 = 管理员改库动作，
          改代码缺省不动已登记的锁箱）；
        - used = lockbox_uses 中 cutoff 匹配的行数（**含 started**——读了
          就算看过；旧库 NULL 行属预算制度前的既往锁箱，不计入）；
        - exhausted = used >= max_uses：G5 的 begin_lockbox / run_g5 直调
          路径据此拒绝（见 mt5quant.validation.gates）。

        返回 {"cutoff", "max_uses", "used", "remaining", "exhausted"}。
        """
        key = _cutoff_iso(cutoff)
        if key is None:
            raise ValueError("lockbox_budget 需要 cutoff（锁箱标识）")
        row = self.conn.execute(
            "SELECT max_uses FROM lockbox_budget WHERE cutoff=?",
            (key,)).fetchone()
        if row is None:
            limit = DEFAULT_LOCKBOX_MAX_USES if max_uses is None \
                else int(max_uses)
            if limit <= 0:
                raise ValueError(
                    f"max_uses 必须为正整数，收到 {max_uses!r}——预算为零或"
                    "负数应直接不登记 G5，而不是写进表里")
            with self.conn:
                self.conn.execute(
                    "INSERT INTO lockbox_budget(cutoff, max_uses,"
                    " created_utc) VALUES (?,?,?)",
                    (key, limit,
                     datetime.now(timezone.utc).isoformat()))
        else:
            limit = int(row["max_uses"])
        used = int(self.conn.execute(
            "SELECT COUNT(*) n FROM lockbox_uses WHERE cutoff=?",
            (key,)).fetchone()["n"])
        return {"cutoff": key, "max_uses": limit, "used": used,
                "remaining": max(0, limit - used),
                "exhausted": used >= limit}

    def gate_passed(self, card_id: str, gate: str) -> bool:
        """该卡是否有某道关卡的**通过**记录（G5 前置：未过 G3 拒跑）。
        只认 passed=1；未跑过或未通过的关卡都返回 False。"""
        row = self.conn.execute(
            "SELECT 1 FROM gates g JOIN runs r ON r.run_id = g.run_id"
            " WHERE r.card_id=? AND g.gate=? AND g.passed=1 LIMIT 1",
            (card_id, gate)).fetchone()
        return row is not None

    def first_run_at(self, card_id: str, *, version: int | None = None):
        """该卡第一次运行的时刻；没跑过 → None。aware UTC。

        version 给定时只统计该版本（T21d：先卡后测按**卡文件**（id+version）
        判定——新版本文件的首次运行不受旧版本运行影响）。
        """
        if version is None:
            row = self.conn.execute(
                "SELECT MIN(created_utc) m FROM runs WHERE card_id=?",
                (card_id,)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT MIN(created_utc) m FROM runs WHERE card_id=?"
                " AND card_version=?", (card_id, version)).fetchone()
        if row is None or row["m"] is None:
            return None
        return datetime.fromisoformat(row["m"])

    def card_strategy_sha(self, card_id: str, version: int) -> str | None:
        """该卡版本已登记的策略代码指纹（T37/D23 绑定）。

        取首个非 migration、非 legacy 的 strategy_sha；全部为 legacy
        （迁移前的运行，指纹未知）→ None（**不绑定**：旧卡重跑在新代码
        下登记新指纹，不判为版本漂移——"legacy"表示指纹缺失而非已绑定
        某个旧指纹）。bt screen/gate 据此拒绝"同卡版本下改代码重跑"。
        """
        row = self.conn.execute(
            "SELECT strategy_sha FROM runs WHERE card_id=? AND card_version=?"
            " AND actor<>'migration' AND strategy_sha<>'legacy'"
            " ORDER BY run_id LIMIT 1", (card_id, version)).fetchone()
        return None if row is None else str(row["strategy_sha"])

    def _dedup_n(self, rows: list) -> int:
        """按 (strategy, params, symbol, timeframe, data_sha256,
        cost_model_version, strategy_sha) 对成功运行去重计数（params 键序
        无关；T37：指纹进键——改代码重跑即新试验；actor=migration 的
        迁移行不是试验，不计入）。"""
        keys = set()
        for r in rows:
            if r["actor"] == "migration":
                continue
            if not json.loads(r["metrics_json"]).get("ok"):
                continue
            params = json.loads(r["params_json"])
            keys.add((r["strategy"],
                      json.dumps(params, sort_keys=True, ensure_ascii=False),
                      r["symbol"], r["timeframe"], r["data_sha256"],
                      r["cost_model_version"], r["strategy_sha"]))
        return len(keys)

    def stats(self, card_id: str | None = None) -> dict:
        """运行计数（供 DSR 的试验次数与 `bt ledger stats`）。

        card_id 给定时只统计该卡（跨全部版本），并附 first/last_run_utc。
        T21c/M4 去重口径（T37 加入 strategy_sha 七元组）：
        - n_dedup：按 (strategy, params, symbol, timeframe, data_sha256,
          cost_model_version, strategy_sha) 去重后的**成功**运行数
          （card_id 给定时按谱系 family 统计，缺省为项目级）——同一组
          参数先 screen 后 gate 只计 1；改策略代码（指纹变）= 新试验；
        - family / family_total_runs / family_n_dedup：谱系口径
          （H012-v1 与 H012-v2 累计进同一谱系 H012）；
        - project_n_dedup：项目级去重 N（两种调用都附带）。
        迁移行（actor=migration）计入 total_runs（可审计）、不计入 N。
        """
        all_rows = self.conn.execute(
            "SELECT card_id, metrics_json, strategy, params_json, symbol,"
            " timeframe, data_sha256, strategy_sha, cost_model_version,"
            " created_utc, actor FROM runs").fetchall()
        sel = ([r for r in all_rows if r["card_id"] == card_id]
               if card_id else list(all_rows))
        by_card: dict = {}
        for r in sel:
            ok = bool(json.loads(r["metrics_json"]).get("ok"))
            c = by_card.setdefault(r["card_id"],
                                   {"runs": 0, "ok": 0, "failed": 0})
            c["runs"] += 1
            c["ok" if ok else "failed"] += 1
        project_n = self._dedup_n(all_rows)
        st = {
            "ledger_path": self.path,
            "card_id": card_id,
            "total_runs": sum(c["runs"] for c in by_card.values()),
            "ok_runs": sum(c["ok"] for c in by_card.values()),
            "failed_runs": sum(c["failed"] for c in by_card.values()),
            "cards": by_card,
            "lockbox_uses": int(self.conn.execute(
                "SELECT COUNT(*) n FROM lockbox_uses").fetchone()["n"]),
            "project_n_dedup": project_n,
        }
        if sel:
            times = sorted(r["created_utc"] for r in sel)
            st["first_run_utc"] = times[0]
            st["last_run_utc"] = times[-1]
        if card_id:
            fam = family_of(card_id)
            fam_rows = [r for r in all_rows if family_of(r["card_id"]) == fam]
            st["family"] = fam
            st["family_total_runs"] = len(fam_rows)
            st["family_n_dedup"] = self._dedup_n(fam_rows)
            st["n_dedup"] = st["family_n_dedup"]
        else:
            st["n_dedup"] = project_n
        return st
