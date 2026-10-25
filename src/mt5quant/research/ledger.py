"""试验台账：SQLite（标准库 sqlite3），研究侧一切运行的可追溯登记。

表结构（按 P4 计划钉死；T21a/T21c 修订）：
- cards(card_id, version, path, git_commit, committed_at_utc)——主键
  (card_id, version)：版本迭代是**新行**，旧行不动（历史如实保留）；
- runs(run_id, card_id, card_version, strategy, params_json, symbol,
  timeframe, data_first, data_last, data_sha256, engine_version,
  cost_model_version, gates_hash, metrics_json, created_utc, actor)——
  **失败的运行也记一行**（metrics_json 里 ok=false + error 摘要）；
- gates(run_id, gate, passed, details_json)——关卡结果（T12 消费）；
- lockbox_uses(card_id UNIQUE, used_at_utc, approved_by, result_json,
  status)——锁箱一次性：status ∈ {"started", "done"}（T21a/C3）——
  G5 读锁箱数据**之前**先写 started（record_lockbox_start），评估结束写
  结果（finish_lockbox_use → done）；已有 started 行（上次崩溃残留）同样
  视为已用（lockbox_used），第二次插入直接 sqlite3.IntegrityError。
  旧库无 status 列 → 打开时自动补列（旧行按终态 done 处理）。

metrics_json 结构：{"ok": bool, "error": str|None, "metrics": dict|None}。
时间一律 aware-UTC ISO（created_utc 校验强制带时区——它要和卡的提交时间比）。

N 的口径（T21c/H4/M4）：DSR 的试验次数 = 该**谱系**（cards.family_of）中按
(strategy, params, symbol, timeframe, data_sha256, cost_model_version) 去重
后的成功运行数（stats 的 n_dedup / family_n_dedup），同时给出项目级
project_n_dedup——同参 screen+gate 只计 1 次，v1/v2 新卡累计进同一谱系。
"""
from __future__ import annotations

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
  engine_version     TEXT NOT NULL,
  cost_model_version TEXT NOT NULL,
  gates_hash         TEXT NOT NULL DEFAULT '',
  metrics_json       TEXT NOT NULL,
  created_utc        TEXT NOT NULL,
  actor              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_card ON runs(card_id);
CREATE TABLE IF NOT EXISTS gates(
  run_id       INTEGER NOT NULL REFERENCES runs(run_id),
  gate         TEXT NOT NULL,
  passed       INTEGER NOT NULL,
  details_json TEXT NOT NULL,
  PRIMARY KEY (run_id, gate)
);
CREATE TABLE IF NOT EXISTS lockbox_uses(
  card_id     TEXT NOT NULL UNIQUE,
  used_at_utc TEXT NOT NULL,
  approved_by TEXT NOT NULL,
  result_json TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'done'
);
"""


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
                   created_utc=None) -> int:
        """记一行运行（成功的与抛异常的 alike）；返回 run_id。

        metrics 与 error 二选一（有 error 时 metrics 记 None）；
        created_utc 缺省取当前 UTC（测试可注入固定时间，须 aware）。
        """
        payload = {"ok": error is None, "error": error, "metrics": metrics}
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO runs(card_id, card_version, strategy, params_json,"
                " symbol, timeframe, data_first, data_last, data_sha256,"
                " engine_version, cost_model_version, gates_hash,"
                " metrics_json, created_utc, actor)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (card_id, card_version, strategy,
                 json.dumps(_san(params), ensure_ascii=False),
                 symbol, timeframe, data_first, data_last, data_sha256,
                 engine_version, cost_model_version, gates_hash,
                 json.dumps(_san(payload), ensure_ascii=False),
                 _iso(created_utc), actor))
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

    def record_lockbox_use(self, card_id: str, *, approved_by: str,
                           result: dict) -> None:
        """锁箱使用登记（直接写终态）：同一 card_id 第二次插入 →
        sqlite3.IntegrityError。不捕获、不绕过——"只能用一次"由 UNIQUE
        约束钉死。G5 的常规路径用 record_lockbox_start + finish_lockbox_use
        （T21a：读锁箱数据前先写 started，防中途崩溃后重跑）。
        """
        with self.conn:
            self.conn.execute(
                "INSERT INTO lockbox_uses(card_id, used_at_utc, approved_by,"
                " result_json, status) VALUES (?,?,?,?,'done')",
                (card_id, datetime.now(timezone.utc).isoformat(), approved_by,
                 json.dumps(_san(result), ensure_ascii=False)))

    def record_lockbox_start(self, card_id: str, *, approved_by: str) -> None:
        """写 status='started' 行（T21a/C3：G5 读锁箱数据**之前**调用）。

        同一 card_id 已有任何行（含上次崩溃残留的 started）→
        sqlite3.IntegrityError（UNIQUE 约束）；started 行同样算已用。
        """
        with self.conn:
            self.conn.execute(
                "INSERT INTO lockbox_uses(card_id, used_at_utc, approved_by,"
                " result_json, status) VALUES (?,?,?,?,'started')",
                (card_id, datetime.now(timezone.utc).isoformat(), approved_by,
                 "{}"))

    def finish_lockbox_use(self, card_id: str, *, result: dict,
                           approved_by: str = "bt") -> None:
        """评估结束写终态：started → done（保留 started 行的批准人与时刻）。

        无 started 行（run_g5 被直接调用的路径）→ 插入完整 done 行；
        已有 done 行 → sqlite3.IntegrityError（一次性机会已消耗）。
        """
        payload = json.dumps(_san(result), ensure_ascii=False)
        with self.conn:
            cur = self.conn.execute(
                "UPDATE lockbox_uses SET result_json=?, status='done'"
                " WHERE card_id=? AND status='started'", (payload, card_id))
            if cur.rowcount:
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
                " result_json, status) VALUES (?,?,?,?,'done')",
                (card_id, datetime.now(timezone.utc).isoformat(), approved_by,
                 payload))

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

    def _dedup_n(self, rows: list) -> int:
        """按 (strategy, params, symbol, timeframe, data_sha256,
        cost_model_version) 对成功运行去重计数（params 键序无关）。"""
        keys = set()
        for r in rows:
            if not json.loads(r["metrics_json"]).get("ok"):
                continue
            params = json.loads(r["params_json"])
            keys.add((r["strategy"],
                      json.dumps(params, sort_keys=True, ensure_ascii=False),
                      r["symbol"], r["timeframe"], r["data_sha256"],
                      r["cost_model_version"]))
        return len(keys)

    def stats(self, card_id: str | None = None) -> dict:
        """运行计数（供 DSR 的试验次数与 `bt ledger stats`）。

        card_id 给定时只统计该卡（跨全部版本），并附 first/last_run_utc。
        T21c/M4 新增去重口径：
        - n_dedup：按 (strategy, params, symbol, timeframe, data_sha256,
          cost_model_version) 去重后的**成功**运行数（card_id 给定时按谱系
          family 统计，缺省为项目级）——同一组参数先 screen 后 gate 只计 1；
        - family / family_total_runs / family_n_dedup：谱系口径
          （H012-v1 与 H012-v2 累计进同一谱系 H012）；
        - project_n_dedup：项目级去重 N（两种调用都附带）。
        """
        all_rows = self.conn.execute(
            "SELECT card_id, metrics_json, strategy, params_json, symbol,"
            " timeframe, data_sha256, cost_model_version, created_utc"
            " FROM runs").fetchall()
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
