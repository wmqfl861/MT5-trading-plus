"""试验台账：SQLite（标准库 sqlite3），研究侧一切运行的可追溯登记。

表结构（按 P4 计划钉死；T21a/T21c 修订；T37 哈希链）：
- cards(card_id, version, path, git_commit, committed_at_utc)——主键
  (card_id, version)：版本迭代是**新行**，旧行不动（历史如实保留）；
- runs(run_id, card_id, card_version, strategy, params_json, symbol,
  timeframe, data_first, data_last, data_sha256, strategy_sha,
  engine_version, cost_model_version, gates_hash, metrics_json,
  created_utc, actor, run_class, prev_hash, row_hash)——
  **失败的运行也记一行**（metrics_json 里 ok=false + error 摘要）；
  strategy_sha（T37/H3/D23）：策略代码指纹（cards.strategy_fingerprint）
  ——试验身份含代码，改代码重跑即不同的试验（N 不再被低估）；老库迁移
  前的旧行补 "legacy"（指纹未知，不参与卡版本绑定）；
  run_class（T80/G4，D33）：运行类别 research|calibration|ops|migration
  ——由 bt 按候选状态与轮次写入（不看 actor 文本，CLI 保留名拒绝）；
  旧库迁移补列，旧行按 actor 推断并记一行迁移事件；
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
- candidate_checks(candidate_id, decision, checked_at_utc, result_json)
  ——候选预筛结论（T62/D31：bt candidate check 的产出；决策枚举
  pass/fail/waiting_data，result_json 与 research/candidates/C###.precheck.json
  同一负载）。旧库打开时自动建表并入链（空表从链首开始）。
- rounds(round_id PRIMARY KEY, opened_utc, toml_sha256, candidates_json,
  trial_budget, lockbox_uses_max, dsr_trial_scope, advance_rule,
  close_rule, approved_by, evaluation_json, git_commit, status,
  closed_utc)——研究轮次预注册（T63/D32，铁律 16：bt round open 把
  已提交的 research/rounds/R#.toml 内容绑定进台账；toml_sha256 锚预注册
  文件内容，status ∈ {"open", "closed"}，关闭走 _update_chained 归档
  改写——open→closed 的全过程可从 ledger_events 重放）。同一 round_id
  只能开启一次（主键钉死：预注册是一次性动作，关闭后不得重开）。
- runs.round_id（T63/D32）：运行所属轮次；NULL = 轮次制之前（旧库
  迁移补列，旧行保持 NULL——历史如实保留，不回填）。旧库（已有 T37
  哈希链）补列后 runs 链按新内容**整体重算一次**（与 T37 迁移同一信任
  边界；链头变化会与 docs/ops/ledger-anchors.md 的锚定记录不符——
  迁移后须重新锚定，scripts/ledger_anchor.py）。
- runs.run_class（T80/G4，D33）：同上走 _rechain 迁移路径——生产台账
  迁移后须用 scripts/ledger_anchor.py --accept-rechain 重锚（旧头取
  锚文件最近条目，--dump-heads 取证；迁移由主会话执行，代码只提供
  自动迁移路径）。

哈希链（T37/M6/D27；R6/C5 防洗链；T62 加 candidate_checks；T63 加
rounds）：runs / gates / lockbox_uses / candidate_checks / rounds 各一条
按 rowid 串联的链，
row_hash = sha256(规范化行 JSON + prev_hash)
（规范化 = 全部内容列、键排序、紧凑分隔符；prev_hash/row_hash 自身不入
哈希）。首行 prev_hash = GENESIS_HASH。R6/C5 起的写入纪律：

- **写前验证**：所有写方法（record_run / record_gate /
  record_lockbox_use / record_lockbox_start / finish_lockbox_use /
  lockbox_budget / register_card / record_round_open /
  record_round_close）写前先 verify_chain()，不完整 →
  抛 LedgerTamperedError（fail-closed，提示人工核查）——正常写入**绝不
  重算已有行**，因此篡改证据不可能被"正常 record_run 一次"洗掉；
- **增量延伸**：合法追加只计算新行的 row_hash（prev = 当前链尾），
  不重算全表（_rechain 仅保留给 T37 旧库迁移的一次性重建）；
- **状态更新归档**：gates 重录 / lockbox started→done 保留 UPDATE
  语义（表上的 UNIQUE/PRIMARY KEY 钉死一次性），但旧行内容先完整归档
  进 ledger_events（审计附表，追加式），该行 row_hash 用
  (prev_hash, 新内容) 重算、后继行 prev_hash 联动重算——链上任何时刻
  都能重放出"改写前是什么"；
- **删尾检测（chain_meta 快照）**：每张链式表（含 ledger_events）在
  chain_meta 存 last_rowid / row_count / head_hash 快照，每次合法写后
  刷新；verify_chain() 比对快照——行数变少（删行/删尾）、最大 rowid
  回退、链头与快照不符（整链被重算改写）、行数超过快照（绕过写方法的
  伪造追加）都报出。ledger_events 的 head_hash 列存**行内容摘要**
  （R7/N-I3：改写审计行 old/new 内容、行数不变也要报）。攻击者若连
  chain_meta 一起改写，则由 git 里的**链式锚定**（scripts/
  ledger_anchor.py → docs/ops/ledger-anchors.md，研究角色白名单外）
  对出——那是台账之外的证据。

写事务纪律（R7/N-C3）：所有写方法经 _txn()（BEGIN IMMEDIATE …
COMMIT，异常整体回滚）——验链、读链尾、写内容、写审计记录、刷新快照
**同一事务**，最后统一提交；辅助函数不开自己的连接上下文（嵌套即报错，
旧行为里内层 `with conn:` 会把外层事务中途提交）。并发写在 BEGIN
IMMEDIATE 上排队（连接 timeout=30s），两个 record_run 受控交错后链
仍完好。

旧库迁移（T37 + R6/C5）：打开时自动补 strategy_sha/prev_hash/row_hash
列；旧行 strategy_sha='legacy'；三表哈希链自第一行重建（仅此迁移路径
重算旧行）；chain_meta 快照按迁移后的当前状态初始化（不重写旧行）；
迁移本身在 runs 记一行 actor='migration'（card_id='__migration__'，
计入 total_runs 可审计，不计入去重 N——迁移不是试验）。

metrics_json 结构：{"ok": bool, "error": str|None, "metrics": dict|None}。
时间一律 aware-UTC ISO（created_utc 校验强制带时区——它要和卡的提交时间比）。

N 的口径（T21c/H4/M4；T37 加入 strategy_sha）：DSR 的试验次数 = 该**谱系**
（cards.family_of）中按 (strategy, params, symbol, timeframe, data_sha256,
cost_model_version, strategy_sha) 去重后的成功运行数（stats 的 n_dedup /
family_n_dedup），同时给出项目级 project_n_dedup——同参 screen+gate 只计
1 次，v1/v2 新卡累计进同一谱系；改策略代码（指纹变）= 新试验。
T64/D33 新增项目级研究口径 project_n_dedup_research：只计 run_class=
research 的运行（排除 calibration/ops/migration——标定与运维不是研究
试验；H 线与 P 线合并计算）。n_dedup / family_n_dedup / project_n_dedup
的现行语义冻结不变（G4 缺省 family 口径逐位一致），研究口径只作为新增
键与 dsr_trial_scope=project 时的取数来源。
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import contextmanager
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
  round_id           TEXT,
  run_class          TEXT NOT NULL DEFAULT 'research',
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
CREATE TABLE IF NOT EXISTS candidate_checks(
  candidate_id  TEXT NOT NULL,
  decision      TEXT NOT NULL,
  checked_at_utc TEXT NOT NULL,
  result_json   TEXT NOT NULL,
  prev_hash     TEXT NOT NULL DEFAULT '',
  row_hash      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rounds(
  round_id         TEXT NOT NULL PRIMARY KEY,
  opened_utc       TEXT NOT NULL,
  toml_sha256      TEXT NOT NULL,
  candidates_json  TEXT NOT NULL,
  trial_budget     INTEGER NOT NULL,
  lockbox_uses_max INTEGER NOT NULL,
  dsr_trial_scope  TEXT NOT NULL,
  advance_rule     TEXT NOT NULL,
  close_rule       TEXT NOT NULL,
  approved_by      TEXT NOT NULL,
  evaluation_json  TEXT NOT NULL,
  git_commit       TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'open',
  closed_utc       TEXT,
  prev_hash        TEXT NOT NULL DEFAULT '',
  row_hash         TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS lockbox_budget(
  cutoff      TEXT NOT NULL PRIMARY KEY,
  max_uses    INTEGER NOT NULL,
  created_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chain_meta(
  table_name  TEXT NOT NULL PRIMARY KEY,
  last_rowid  INTEGER NOT NULL,
  row_count   INTEGER NOT NULL,
  head_hash   TEXT,
  updated_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_events(
  event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name      TEXT NOT NULL,
  row_key         TEXT NOT NULL,
  action          TEXT NOT NULL,
  old_content_json TEXT,
  new_content_json TEXT,
  recorded_utc    TEXT NOT NULL
);
"""

# T27/M11（D16 未决）：每个锁箱（cutoff）最多允许多少次 G5——
# 多张卡各看一次锁箱等于在锁箱上做选择，需要项目级预算。
# 蓝图 §8 的建议值是 5；用户对 D16 拍板前以此为硬编码默认，
# lockbox_budget(cutoff, max_uses=N) 首次登记时可覆盖。
DEFAULT_LOCKBOX_MAX_USES = 5

# T80/G4（D33）：运行类别与保留 actor 名。run_class 由 bt 按候选状态与
# 轮次写入（标定样品 → calibration；研究运行 → research；运维通道
# actor=ops → ops；迁移行 → migration），N 的研究口径只看这个字段，
# 不看 actor 文本——保留名在 CLI 层拒绝（防研究运行借 actor 逃出研究 N）。
RUN_CLASSES = ("research", "calibration", "ops", "migration")
RESERVED_ACTORS = ("calibration", "ops", "migration")


def run_class_for_actor(actor: str) -> str:
    """actor → run_class 推断（旧库迁移与 record_run 缺省口径）：
    保留名同名映射，其余一律 research（第十二轮 G4：N 的口径不看 actor
    文本，保留名之外 actor 只是自由文本标签）。"""
    return actor if actor in RESERVED_ACTORS else "research"

# T37/M6/D27：带哈希链的表；链首 prev_hash（常量，与"空串"的
# 未迁移状态可区分）。R6/C5：ledger_events 为审计附表（不哈希链，
# 但行数/rowid 进 chain_meta 快照——删审计行要能报出）。
# T62/D31：candidate_checks（候选预筛结论）入链——预筛是开卡前的
# 硬闸门，结论的事后改写必须像 runs 一样留痕。
# T63/D32：rounds（轮次预注册开启/关闭）入链——试验预算与锁箱用量
# 的申报值就锁在这条链上，改预算/抹轮次要能报出。
_CHAIN_TABLES = ("runs", "gates", "lockbox_uses", "candidate_checks",
                 "rounds")
_SNAPSHOT_TABLES = _CHAIN_TABLES + ("ledger_events",)
GENESIS_HASH = "0" * 64


class LedgerTamperedError(RuntimeError):
    """台账哈希链/快照校验失败时的写入拒绝（R6/C5 防洗链，fail-closed）。

    含义：链上已检测到篡改/删行/绕道写入，任何正常写入都可能把证据
    重封掉，因此一律拒绝——请人工比对 docs/ops/ledger-anchors.md 的
    锚定历史后再决定处置（绝不静默修复）。"""


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
    """试验台账句柄；用完 close（或 with）。

    R7/N-C3：每个写方法一个 BEGIN IMMEDIATE 写事务（_txn），验链→读链
    尾→写→审计→快照统一提交；读方法直接执行（autocommit 读）。
    """

    def __init__(self, path, *, create: bool = True):
        p = Path(path)
        if create:
            p.parent.mkdir(parents=True, exist_ok=True)
        elif not p.exists():
            raise FileNotFoundError(f"台账不存在：{p}")
        self.path = str(p)
        # R7/N-C3：autocommit 模式（isolation_level=None）+ 手工 BEGIN
        # IMMEDIATE——写事务由 _txn() 显式管理，连接层不再隐式开启/提交
        # 事务（旧行为里辅助函数的 `with conn:` 会在外层事务中途提交）。
        # timeout=30s：并发写者在 BEGIN IMMEDIATE 上排队等待（busy
        # timeout），而不是立刻报 database is locked。
        self.conn = sqlite3.connect(self.path, timeout=30.0)
        self.conn.isolation_level = None
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._txn_depth = 0
        self.conn.executescript(_SCHEMA)
        # T21a 迁移：老库的 lockbox_uses 无 status 列——补列，旧行按终态 done
        cols = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(lockbox_uses)")}
        if "status" not in cols:
            with self._txn():
                self.conn.execute(
                    "ALTER TABLE lockbox_uses ADD COLUMN status TEXT"
                    " NOT NULL DEFAULT 'done'")
        # T27 迁移：老库的 lockbox_uses 无 cutoff 列（预算制度前）——补列，
        # 旧行 NULL = 既往锁箱，不计入任何 cutoff 的预算计数
        if "cutoff" not in cols:
            with self._txn():
                self.conn.execute(
                    "ALTER TABLE lockbox_uses ADD COLUMN cutoff TEXT")
        # T63 迁移（D32/铁律 16）：runs 加 round_id（NULL = 轮次制之前，
        # 旧行不回填）。列进哈希规范化内容，因此已有 T37 哈希链的旧库补列
        # 后 runs 链按新内容**整体重算一次**（_rechain 仅此迁移路径重算
        # 旧行；链头变化会与 git 锚定记录不符——迁移后须重新锚定）。
        # 在 T37 迁移块**之前**执行：pre-T37 旧库随后的一次性重建直接
        # 覆盖 round_id，无需二次重算（幂等：列已存在即跳过）。
        runs_cols = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(runs)")}
        pre_migration = "strategy_sha" not in runs_cols
        if "round_id" not in runs_cols:
            with self._txn():
                self.conn.execute(
                    "ALTER TABLE runs ADD COLUMN round_id TEXT")
            if not pre_migration:
                n_runs = int(self.conn.execute(
                    "SELECT COUNT(*) n FROM runs").fetchone()["n"])
                if n_runs:
                    with self._txn():
                        self._rechain("runs")
                        self._snapshot_refresh("runs")
        # T80/G4 迁移（D33）：runs 加 run_class（research|calibration|ops|
        # migration，由 bt 按候选状态与轮次写入；旧行按 actor 推断）。
        # 列进哈希规范化内容，已有链的旧库补列后 runs 链按新内容整体重算
        # 一次并记一行迁移事件（t80-run-class-migration）——与 round_id
        # 迁移同一信任边界：链头变化会与 docs/ops/ledger-anchors.md 的
        # 锚定记录不符，**生产台账迁移后须用 scripts/ledger_anchor.py
        # --accept-rechain 重锚**（旧头取锚文件最近条目；--dump-heads 取
        # 证）。迁移本身由主会话执行，这里只提供自动迁移路径。在 T37
        # 迁移块之前执行：pre-T37 旧库旧行先推断好类别，随后的一次性
        # 重建直接覆盖本列（事件行由 T37 迁移行承载，不重复记）。
        if "run_class" not in runs_cols:
            with self._txn():
                self.conn.execute(
                    "ALTER TABLE runs ADD COLUMN run_class TEXT"
                    " NOT NULL DEFAULT 'research'")
                n_runs = int(self.conn.execute(
                    "SELECT COUNT(*) n FROM runs").fetchone()["n"])
                if n_runs:
                    # 旧行按 actor 推断（迁移前无独立字段，G4）
                    for actor_name in RESERVED_ACTORS:
                        self.conn.execute(
                            "UPDATE runs SET run_class=? WHERE actor=?",
                            (actor_name, actor_name))
            if not pre_migration and n_runs:
                with self._txn():
                    payload = {"ok": True, "error": None,
                               "metrics": {
                                   "event": "t80-run-class-migration",
                                   "runs": n_runs,
                                   "note": "旧行 run_class 按 actor "
                                           "推断（calibration/ops/"
                                           "migration 同名映射，其余"
                                           " research）；runs 链按新"
                                           "内容整体重算——生产台账"
                                           "须 --accept-rechain 重锚"}}
                    self.conn.execute(
                        "INSERT INTO runs(card_id, card_version,"
                        " strategy, params_json, symbol, timeframe,"
                        " data_first, data_last, data_sha256,"
                        " strategy_sha, engine_version,"
                        " cost_model_version, gates_hash,"
                        " metrics_json, created_utc, actor,"
                        " run_class)"
                        " VALUES ('__migration__', 0,"
                        " '__migration__', '{}', '-', '-', NULL,"
                        " NULL, '-', 'legacy', 'ledger-v1',"
                        " 'ledger-v1', '', ?, ?, 'migration',"
                        " 'migration')",
                        (json.dumps(payload, ensure_ascii=False),
                         datetime.now(timezone.utc).isoformat()))
                    self._rechain("runs")
                    self._snapshot_refresh("runs")
        # T37 迁移（H3/M6，D23/D27）：runs 加 strategy_sha/prev_hash/
        # row_hash，gates 与 lockbox_uses 加 prev_hash/row_hash；旧行
        # strategy_sha='legacy'，三链自第一行重建，迁移记一行
        # actor=migration（幂等：迁移过再打开不会重复记）。
        if pre_migration:
            with self._txn():
                self.conn.execute(
                    "ALTER TABLE runs ADD COLUMN strategy_sha TEXT"
                    " NOT NULL DEFAULT 'legacy'")
        for tbl in _CHAIN_TABLES:
            tcols = {r["name"] for r in self.conn.execute(
                f"PRAGMA table_info({tbl})")}
            for col in ("prev_hash", "row_hash"):
                if col not in tcols:
                    with self._txn():
                        self.conn.execute(
                            f"ALTER TABLE {tbl} ADD COLUMN {col} TEXT"
                            " NOT NULL DEFAULT ''")
        if pre_migration:
            counts = {t: int(self.conn.execute(
                f"SELECT COUNT(*) n FROM {t}").fetchone()["n"])
                for t in _CHAIN_TABLES}
            with self._txn():
                if any(counts.values()):
                    payload = {"ok": True, "error": None, "metrics": {
                        "event": "t37-hash-chain-migration",
                        **{t: counts[t] for t in _CHAIN_TABLES},
                        "note": "旧行 strategy_sha=legacy；runs/gates/"
                                "lockbox_uses 三链自第一行重建"}}
                    self.conn.execute(
                        "INSERT INTO runs(card_id, card_version, strategy,"
                        " params_json, symbol, timeframe, data_first,"
                        " data_last, data_sha256, strategy_sha,"
                        " engine_version, cost_model_version, gates_hash,"
                        " metrics_json, created_utc, actor, run_class)"
                        " VALUES ('__migration__', 0, '__migration__',"
                        " '{}', '-', '-', NULL, NULL, '-', 'legacy',"
                        " 'ledger-v1', 'ledger-v1', '', ?, ?, 'migration',"
                        " 'migration')",
                        (json.dumps(payload, ensure_ascii=False),
                         datetime.now(timezone.utc).isoformat()))
                for tbl in _CHAIN_TABLES:
                    self._rechain(tbl)
        # R6/C5：chain_meta 快照初始化（迁移——缺快照的表按当前状态登记，
        # 旧行不重写；之后的删行/改写/绕道写入都会与快照对不上）。
        # R7/N-I3：R6/C5 的旧快照对 ledger_events 无内容摘要（head_hash
        # NULL 而行数非零）→ 按当前状态补初始化摘要（与首次登记同一信任
        # 边界：初始化即当前；此后任何 old/new 内容改写都与摘要对不上）。
        with self._txn():
            for tbl in _SNAPSHOT_TABLES:
                has = self.conn.execute(
                    "SELECT row_count, head_hash FROM chain_meta"
                    " WHERE table_name=?", (tbl,)).fetchone()
                stale_digest = (
                    tbl == "ledger_events" and has is not None
                    and has["head_hash"] is None
                    and int(has["row_count"]) > 0)
                if has is None or stale_digest:
                    self._snapshot_refresh(tbl)

    # ------------------------------------------------ 哈希链（T37/M6/D27；R6/C5）

    @contextmanager
    def _txn(self, *, immediate: bool = True):
        """写事务（R7/N-C3）：BEGIN IMMEDIATE → … → COMMIT，异常整体回滚。

        - **先取写锁再做事**：BEGIN IMMEDIATE 立刻持有保留锁，并发写者在
          事务外排队（连接 timeout=30s 即 busy_timeout）；验链、读链尾、
          写内容、写审计、刷新快照全部落在同一事务内、最后统一提交——
          两次并发 record_run 受控交错后链仍完好（旧行为：链尾在事务外
          读取，第三行接旧链尾 → verify 断链）；
        - **中途异常全部回滚**（旧行为：辅助函数的 `with conn:` 把外层
          修改中途提交，出现"关卡内容已变、快照仍旧"的半提交状态）；
        - 辅助函数（_snapshot_refresh / _rechain / _append_chained /
          _update_chained）不再各自开连接上下文——运行在调用方的事务里，
          嵌套直接报错（防止谁又隐式提交调用方的事务）。
        """
        if self._txn_depth:
            raise RuntimeError(
                "嵌套写事务：辅助函数不得自带事务上下文（R7/N-C3——"
                "内层连接上下文的提交会把外层事务中途落地）")
        self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        self._txn_depth += 1
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")
        finally:
            self._txn_depth -= 1

    def _table_cols(self, table: str) -> list[str]:
        """该表的内容列（除 prev_hash/row_hash 外全部，含主键）。"""
        cols = [r["name"] for r in self.conn.execute(
            f"PRAGMA table_info({table})")]
        return [c for c in cols if c not in ("prev_hash", "row_hash")]

    def _row_hash(self, content: dict, prev: str) -> str:
        return hashlib.sha256(
            (_canonical_json(content) + prev).encode("utf-8")).hexdigest()

    def _events_digest(self) -> str | None:
        """ledger_events 全部行内容的 sha256（rowid 序，逐行规范化 JSON
        + 记录分隔符）。空表 → None（与链式表的空表链头记法一致）。

        R7/N-I3：审计附表不做行级哈希链，改以内容摘要进 chain_meta——
        只比对行数/最大 rowid 时，改写 old/new_content_json（如把归档的
        批准人换掉）不会被发现，历史状态被无声改写。
        """
        rows = self.conn.execute(
            "SELECT event_id, table_name, row_key, action, old_content_json,"
            " new_content_json, recorded_utc FROM ledger_events"
            " ORDER BY rowid").fetchall()
        if not rows:
            return None
        h = hashlib.sha256()
        for r in rows:
            h.update(_canonical_json(dict(r)).encode("utf-8"))
            h.update(b"\x1e")                     # 行界（ASCII 记录分隔符）
        return h.hexdigest()

    def _snapshot_refresh(self, table: str) -> None:
        """把 chain_meta 中该表的快照刷新为当前状态（合法写之后调用）。

        R7/N-C3：**不开自己的事务、不提交**——运行在调用方写事务内（或
        迁移流程的事务里）。R7/N-I3：ledger_events 的 head_hash 列存
        _events_digest()（内容摘要），链式表仍是链尾 row_hash。
        """
        n, max_rid = self.conn.execute(
            f"SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM {table}"
        ).fetchone()
        if table == "ledger_events":
            head = self._events_digest()
        else:
            tail = self.conn.execute(
                f"SELECT row_hash FROM {table} ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            head = tail["row_hash"] if tail else None
        self.conn.execute(
            "INSERT OR REPLACE INTO chain_meta(table_name, last_rowid,"
            " row_count, head_hash, updated_utc) VALUES (?,?,?,?,?)",
            (table, int(max_rid), int(n), head,
             datetime.now(timezone.utc).isoformat()))

    def _rechain(self, table: str) -> None:
        """整表重算哈希链（rowid 序，确定性：链值只依赖行内容序列）。

        **仅限迁移**（T37 旧库补列后自第一行重建；R6/C5 起正常写入绝不
        走这里——合法追加经 _append_chained 增量延伸，状态更新经
        _update_chained 归档+定点重算，篡改证据不可能被正常写入洗掉）。
        R7/N-C3：不开自己的事务——运行在迁移流程的写事务内。
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
        self.conn.executemany(
            f"UPDATE {table} SET prev_hash=?, row_hash=? WHERE rowid=?",
            updates)

    # ------------------------------------------- 写前验证与链式写入（R6/C5）

    def _verify_or_raise(self) -> None:
        """写前验证：verify_chain() 有任何问题 → LedgerTamperedError。

        所有写方法的第一步——不完整台账上的写入会把篡改证据重封掉
        （第六轮审核 C5 复现 A），一律 fail-closed 拒绝并提示人工核查。
        """
        problems = self.verify_chain()
        if problems:
            raise LedgerTamperedError(
                "台账哈希链校验失败，拒绝写入（R6/C5 防洗链：不完整台账上"
                "的任何写入都会销毁篡改证据）：\n  - "
                + "\n  - ".join(problems)
                + "\n——请人工比对 docs/ops/ledger-anchors.md 的锚定历史"
                "核查后再决定处置，绝不静默修复")

    def _append_chained(self, table: str, content: dict) -> int:
        """合法追加：只算新行的 row_hash（prev = 当前链尾），旧行不动。

        哈希按**入库后的实际值**计算（主键自增列插入后才有真值），因此
        先插入内容、再读回该行、再补 prev_hash/row_hash，最后刷新快照。
        R7/N-C3：**运行在调用方的 BEGIN IMMEDIATE 事务内**——读链尾到
        提交之间不可能插入其他写者（保留锁在手），并发 record_run 交错
        也不会再出现"第三行接旧链尾"；本方法不开自己的事务（嵌套即报错）。
        """
        cols = self._table_cols(table)
        names = ", ".join(f'"{c}"' for c in cols)
        ph = ", ".join("?" for _ in cols)
        tail = self.conn.execute(
            f"SELECT row_hash FROM {table} ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        prev = tail["row_hash"] if tail else GENESIS_HASH
        cur = self.conn.execute(
            f"INSERT INTO {table}({names}) VALUES ({ph})",
            tuple(content[c] for c in cols))
        rid = int(cur.lastrowid)
        stored = self.conn.execute(
            f"SELECT {names} FROM {table} WHERE rowid=?", (rid,)).fetchone()
        row_hash = self._row_hash({c: stored[c] for c in cols}, prev)
        self.conn.execute(
            f"UPDATE {table} SET prev_hash=?, row_hash=? WHERE rowid=?",
            (prev, row_hash, rid))
        self._snapshot_refresh(table)
        return rid

    def _update_chained(self, table: str, rid: int, new_content: dict, *,
                        row_key: str, action: str) -> None:
        """合法状态更新（R6/C5）：旧行内容先归档进 ledger_events，再改写
        行内容，该行 row_hash 用 (prev_hash, 新内容) 重算、后继行 prev_hash
        联动重算（链不断），最后刷新快照。

        调用方须先 _verify_or_raise()（本方法不重复整链校验）。
        保留 UPDATE 语义而不是追加新行的原因：lockbox_uses 的
        UNIQUE(card_id) 与 gates 的 PRIMARY KEY(run_id, gate) 钉死
        "一次性/幂等重录"约束，拆掉约束反而弱化防重放；改写前的完整内容
        永远可从 ledger_events 重放。
        R7/N-C3：**运行在调用方的写事务内**——归档、改写、重算、快照
        刷新一荣俱荣一损俱损，中途异常全部回滚（不再出现"关卡已变、
        快照仍旧"的半提交）；本方法不开自己的事务（嵌套即报错）。
        """
        cols = self._table_cols(table)
        names = ", ".join(f'"{c}"' for c in cols)
        old = self.conn.execute(
            f"SELECT {names} FROM {table} WHERE rowid=?", (rid,)).fetchone()
        assignments = ", ".join(f'"{c}"=?' for c in cols)
        self.conn.execute(
            "INSERT INTO ledger_events(table_name, row_key, action,"
            " old_content_json, new_content_json, recorded_utc)"
            " VALUES (?,?,?,?,?,?)",
            (table, row_key, action,
             _canonical_json({c: old[c] for c in cols}),
             _canonical_json(new_content),
             datetime.now(timezone.utc).isoformat()))
        self.conn.execute(
            f"UPDATE {table} SET {assignments} WHERE rowid=?",
            tuple(new_content[c] for c in cols) + (rid,))
        # 从被改行起重算到链尾（被改行内容变 → row_hash 变 → 后继联动）
        prev_row = self.conn.execute(
            f"SELECT row_hash FROM {table} WHERE rowid<?"
            " ORDER BY rowid DESC LIMIT 1", (rid,)).fetchone()
        prev = prev_row["row_hash"] if prev_row else GENESIS_HASH
        for r in self.conn.execute(
                f"SELECT rowid AS _rid, {names} FROM {table}"
                " WHERE rowid>=? ORDER BY rowid", (rid,)).fetchall():
            h = self._row_hash({c: r[c] for c in cols}, prev)
            self.conn.execute(
                f"UPDATE {table} SET prev_hash=?, row_hash=?"
                " WHERE rowid=?", (prev, h, r["_rid"]))
            prev = h
        self._snapshot_refresh("ledger_events")
        self._snapshot_refresh(table)

    def verify_chain(self) -> list[str]:
        """校验三表哈希链 + chain_meta 快照；返回问题描述列表（空 = 完好）。

        - 行内容重算的 row_hash 与存量不符 → 该行被改过（篡改）；
        - 行的 prev_hash ≠ 上一行 row_hash（首行 ≠ GENESIS_HASH）→
          断链（典型：删行）；
        - 行数 < 快照 → 删行；最大 rowid < 快照 → 尾部被删；
        - 行数 > 快照 → 绕过写方法的未登记写入；
        - 链头 ≠ 快照 → 整链被重算改写（行级校验全过也瞒不住）；
        - ledger_events（审计附表）行数/rowid 与快照比对，删审计行报出；
          R7/N-I3：另比对**行内容摘要**（chain_meta.head_hash 存
          _events_digest）——改写 old/new_content_json（行数不变）也报；
        - 攻击者连 chain_meta 一起改写 → 由 git 里的链式锚定对出
          （scripts/ledger_anchor.py --verify，docs/ops/ledger-anchors.md）。
        """
        problems: list[str] = []
        for tbl, id_cols in (("runs", ("run_id",)),
                             ("gates", ("run_id", "gate")),
                             ("lockbox_uses", ("card_id",)),
                             ("candidate_checks", ("candidate_id",)),
                             ("rounds", ("round_id",))):
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
                elif tbl == "candidate_checks":
                    label = f"candidate_checks {r['candidate_id']}"
                elif tbl == "rounds":
                    label = f"rounds {r['round_id']}"
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
            # R6/C5：chain_meta 快照比对（删尾/删行/伪造追加/整链重算）
            snap = self.conn.execute(
                "SELECT last_rowid, row_count, head_hash FROM chain_meta"
                " WHERE table_name=?", (tbl,)).fetchone()
            if snap is None:
                continue
            count = len(rows)
            max_rid = rows[-1]["_rid"] if rows else 0
            head = rows[-1]["row_hash"] if rows else None
            if count < int(snap["row_count"]):
                problems.append(
                    f"{tbl}: 行数 {count} < 快照 {snap['row_count']}"
                    "（删行——含删尾）")
            elif count > int(snap["row_count"]):
                problems.append(
                    f"{tbl}: 行数 {count} > 快照 {snap['row_count']}"
                    "（绕过写方法的未登记写入）")
            if max_rid < int(snap["last_rowid"]):
                problems.append(
                    f"{tbl}: 最大 rowid {max_rid} < 快照 "
                    f"{snap['last_rowid']}（尾部被删）")
            if head != snap["head_hash"]:
                problems.append(
                    f"{tbl}: 链头与快照不符（整链被重算改写或快照后"
                    "改写了行）")
        # R6/C5：审计附表快照（ledger_events 不哈希链，删行/绕道写入要报）；
        # R7/N-I3：行内容摘要比对——改写 old/new_content_json（行数不变，
        # 如把归档的批准人换掉）同样要报
        snap = self.conn.execute(
            "SELECT last_rowid, row_count, head_hash FROM chain_meta"
            " WHERE table_name='ledger_events'").fetchone()
        if snap is not None:
            n, max_rid = self.conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(rowid), 0)"
                " FROM ledger_events").fetchone()
            if int(n) < int(snap["row_count"]):
                problems.append(
                    f"ledger_events: 行数 {n} < 快照 {snap['row_count']}"
                    "（审计事件被删）")
            elif int(n) > int(snap["row_count"]):
                problems.append(
                    f"ledger_events: 行数 {n} > 快照 {snap['row_count']}"
                    "（绕过写方法的未登记写入）")
            if int(max_rid) < int(snap["last_rowid"]):
                problems.append(
                    f"ledger_events: 最大 rowid {max_rid} < 快照 "
                    f"{snap['last_rowid']}（尾部被删）")
            digest = self._events_digest()
            if digest != snap["head_hash"]:
                problems.append(
                    "ledger_events: 行内容与快照摘要不符（审计事件的"
                    " old/new 内容被改写）")
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
        R6/C5：写前先验证链（cards 表不在链上，但篡改台账上连卡登记
        也一并冻结，统一 fail-closed）。
        R7/N-C3：验链与写入同一 BEGIN IMMEDIATE 事务。
        """
        with self._txn():
            self._verify_or_raise()
            self.conn.execute(
                "INSERT OR REPLACE INTO cards(card_id, version, path,"
                " git_commit, committed_at_utc, registered_utc)"
                " VALUES (?,?,?,?,?,?)",
                (card_id, version, path, git_commit,
                 committed_at.isoformat() if committed_at is not None
                 else None,
                 datetime.now(timezone.utc).isoformat()))

    def record_run(self, *, card_id: str, card_version: int, strategy: str,
                   params: dict, symbol: str, timeframe: str,
                   data_first, data_last, data_sha256: str,
                   engine_version: str, cost_model_version: str,
                   gates_hash: str, metrics: dict | None,
                   error: str | None = None, actor: str = "bt",
                   created_utc=None, strategy_sha: str = "legacy",
                   round_id: str | None = None,
                   run_class: str | None = None) -> int:
        """记一行运行（成功的与抛异常的 alike）；返回 run_id。

        metrics 与 error 二选一（有 error 时 metrics 记 None）；
        created_utc 缺省取当前 UTC（测试可注入固定时间，须 aware）；
        strategy_sha（T37/D23）：策略代码指纹，缺省 'legacy'（调用方
        未计算的旧路径/迁移前语义——研究路径 bt.py 必须传真实指纹）；
        round_id（T63/D32）：运行所属研究轮次；None = 轮次制之前/之外
        （旧路径与标定样品运行不带轮次——它们不进入轮次统计）；
        run_class（T80/G4，D33）：运行类别 research|calibration|ops|
        migration——bt 按候选状态与轮次写入（样品卡 calibration、研究
        卡 research；运维通道 reconcile_ab 的 actor=ops 经缺省推断为 ops）；
        缺省 None = 按 actor 推断（run_class_for_actor）；枚举外 →
        ValueError。
        R6/C5：写前验证链（不完整 → LedgerTamperedError，不洗链）；
        追加只延伸链（_append_chained，增量计算 row_hash）。
        R7/N-C3：验链、读链尾、写入、刷快照同一 BEGIN IMMEDIATE 事务
        ——并发 record_run 受控交错后链必须完好（旧行为：验链与读链尾
        都在事务外，第三行接旧链尾）。
        """
        if run_class is None:
            run_class = run_class_for_actor(actor)
        if run_class not in RUN_CLASSES:
            raise ValueError(
                f"runs.run_class 必须是 {list(RUN_CLASSES)}，收到 "
                f"{run_class!r}（T80/G4：运行类别由 bt 按候选状态与轮次"
                "写入，N 的研究口径只看这个字段）")
        payload = {"ok": error is None, "error": error, "metrics": metrics}
        with self._txn():
            self._verify_or_raise()
            return self._append_chained("runs", {
                "run_id": None,
                "card_id": card_id, "card_version": card_version,
                "strategy": strategy,
                "params_json": json.dumps(_san(params), ensure_ascii=False),
                "symbol": symbol, "timeframe": timeframe,
                "data_first": data_first, "data_last": data_last,
                "data_sha256": data_sha256, "strategy_sha": strategy_sha,
                "engine_version": engine_version,
                "cost_model_version": cost_model_version,
                "gates_hash": gates_hash,
                "metrics_json": json.dumps(_san(payload), ensure_ascii=False),
                "created_utc": _iso(created_utc), "actor": actor,
                "round_id": round_id,
                "run_class": run_class,
            })

    def record_gate(self, run_id: int, gate: str, passed: bool,
                    details: dict) -> None:
        """记一个关卡结果（run_id 不存在时外键报错，fail-fast）。

        R6/C5：同 (run_id, gate) 重录保留原 INSERT OR REPLACE 语义，但
        旧行内容先归档进 ledger_events（不再悄悄改写），链经
        _update_chained 定点重算保持可验证。
        R7/N-C3：验链/查重/追加（或归档改写）/刷快照同一写事务。
        """
        content = {"run_id": run_id, "gate": gate,
                   "passed": 1 if passed else 0,
                   "details_json": json.dumps(_san(details),
                                              ensure_ascii=False)}
        with self._txn():
            self._verify_or_raise()
            existing = self.conn.execute(
                "SELECT rowid FROM gates WHERE run_id=? AND gate=?",
                (run_id, gate)).fetchone()
            if existing is None:
                self._append_chained("gates", content)
            else:
                self._update_chained(
                    "gates", int(existing["rowid"]), content,
                    row_key=f"run_id={run_id}/{gate}", action="re-record")

    def record_lockbox_use(self, card_id: str, *, approved_by: str,
                           result: dict, cutoff: str | None = None) -> None:
        """锁箱使用登记（直接写终态）：同一 card_id 第二次插入 →
        sqlite3.IntegrityError。不捕获、不绕过——"只能用一次"由 UNIQUE
        约束钉死。G5 的常规路径用 record_lockbox_start + finish_lockbox_use
        （T21a：读锁箱数据前先写 started，防中途崩溃后重跑）。
        cutoff（T27）：该次使用消耗的锁箱（ISO naive UTC），供预算计数。
        R6/C5：写前验证链；追加只延伸链。
        R7/N-C3：验链与追加同一写事务。
        """
        with self._txn():
            self._verify_or_raise()
            self._append_chained("lockbox_uses", {
                "card_id": card_id,
                "used_at_utc": datetime.now(timezone.utc).isoformat(),
                "approved_by": approved_by,
                "result_json": json.dumps(_san(result), ensure_ascii=False),
                "status": "done", "cutoff": _cutoff_iso(cutoff)})

    def record_lockbox_start(self, card_id: str, *, approved_by: str,
                             cutoff: str | None = None) -> None:
        """写 status='started' 行（T21a/C3：G5 读锁箱数据**之前**调用）。

        同一 card_id 已有任何行（含上次崩溃残留的 started）→
        sqlite3.IntegrityError（UNIQUE 约束）；started 行同样算已用。
        cutoff（T27）：预算计数口径，见 lockbox_budget。
        R6/C5：写前验证链（锁箱行被删过 → LedgerTamperedError，一次性
        不可能靠删行重置）。
        R7/N-C3：验链与追加同一写事务。
        """
        with self._txn():
            self._verify_or_raise()
            self._append_chained("lockbox_uses", {
                "card_id": card_id,
                "used_at_utc": datetime.now(timezone.utc).isoformat(),
                "approved_by": approved_by, "result_json": "{}",
                "status": "started", "cutoff": _cutoff_iso(cutoff)})

    def finish_lockbox_use(self, card_id: str, *, result: dict,
                           approved_by: str = "bt",
                           cutoff: str | None = None) -> None:
        """评估结束写终态：started → done（保留 started 行的批准人与时刻）。

        无 started 行（run_g5 被直接调用的路径）→ 插入完整 done 行；
        已有 done 行 → sqlite3.IntegrityError（一次性机会已消耗）。
        cutoff 只在无 started 行需要插入时使用（started 行已带口径）。
        R6/C5：started→done 是**归档式改写**——旧行内容先完整进
        ledger_events（审计可重放"改写前是什么"），行 row_hash 用
        (prev_hash, 新内容) 重算、后继行联动，链保持可验证。
        R7/N-C3：验链/查 started/归档改写（或插入）/刷快照同一写事务
        ——中途异常整体回滚。
        """
        payload = json.dumps(_san(result), ensure_ascii=False)
        with self._txn():
            self._verify_or_raise()
            started = self.conn.execute(
                "SELECT rowid FROM lockbox_uses WHERE card_id=?"
                " AND status='started'", (card_id,)).fetchone()
            if started is not None:
                cols = self._table_cols("lockbox_uses")
                names = ", ".join(f'"{c}"' for c in cols)
                row = self.conn.execute(
                    f"SELECT {names} FROM lockbox_uses WHERE rowid=?",
                    (started["rowid"],)).fetchone()
                content = {c: row[c] for c in cols}
                content["result_json"] = payload
                content["status"] = "done"
                self._update_chained(
                    "lockbox_uses", int(started["rowid"]), content,
                    row_key=f"card_id={card_id}", action="started->done")
                return
            exists = self.conn.execute(
                "SELECT 1 FROM lockbox_uses WHERE card_id=?",
                (card_id,)).fetchone()
            if exists:
                raise sqlite3.IntegrityError(
                    f"lockbox_uses.card_id={card_id} 已有终态记录"
                    "（UNIQUE 约束）——锁箱一次性已消耗")
            self._append_chained("lockbox_uses", {
                "card_id": card_id,
                "used_at_utc": datetime.now(timezone.utc).isoformat(),
                "approved_by": approved_by, "result_json": payload,
                "status": "done", "cutoff": _cutoff_iso(cutoff)})

    # ------------------------------------------- 候选预筛（T62/D31，铁律 17/18）

    def record_candidate_check(self, candidate_id: str, *, decision: str,
                               result: dict) -> int:
        """候选预筛结论入链（bt candidate check 的台账侧；可重复登记——
        每次预筛都是新行，历史结论如实保留，不覆盖）。

        decision 枚举 pass/fail/waiting_data（枚举外 → ValueError）；
        result 与 research/candidates/C###.precheck.json 同一负载。
        R6/C5：写前验证链；追加只延伸链。R7/N-C3：验链与追加同一写事务。
        """
        if decision not in ("pass", "fail", "waiting_data"):
            raise ValueError(
                f"candidate_checks.decision 必须是 pass/fail/waiting_data，"
                f"收到 {decision!r}")
        with self._txn():
            self._verify_or_raise()
            return self._append_chained("candidate_checks", {
                "candidate_id": candidate_id, "decision": decision,
                "checked_at_utc": datetime.now(timezone.utc).isoformat(),
                "result_json": json.dumps(_san(result), ensure_ascii=False)})

    def candidate_checks(self, candidate_id: str | None = None) -> list[dict]:
        """预筛结论（checked_at 升序；candidate_id 给定时只取该候选）。"""
        if candidate_id is None:
            rows = self.conn.execute(
                "SELECT candidate_id, decision, checked_at_utc, result_json"
                " FROM candidate_checks ORDER BY rowid").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT candidate_id, decision, checked_at_utc, result_json"
                " FROM candidate_checks WHERE candidate_id=? ORDER BY rowid",
                (candidate_id,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------- 研究轮次（T63/D32，铁律 16）

    def record_round_open(self, *, round_id: str, candidates, trial_budget,
                          lockbox_uses_max, dsr_trial_scope: str,
                          advance_rule: str, close_rule: str,
                          approved_by: str, evaluation: dict,
                          toml_sha256: str, git_commit: str) -> None:
        """开启轮次：research/rounds/R#.toml 的内容绑定进 rounds 表
        （带哈希链；toml_sha256 锚文件内容，git_commit 记开启时提交）。

        同一 round_id 已存在 → sqlite3.IntegrityError（主键钉死：预注册
        是一次性动作，关闭后同 id 不得重开）。调用方（bt round open）负责
        前置条件：文件已提交且工作区干净、候选全 screened_pass、
        lockbox_uses_max ≤ D16 剩余预算。
        R6/C5：写前验证链；追加只延伸链。R7/N-C3：验链与追加同一写事务。
        """
        with self._txn():
            self._verify_or_raise()
            self._append_chained("rounds", {
                "round_id": round_id,
                "opened_utc": datetime.now(timezone.utc).isoformat(),
                "toml_sha256": toml_sha256,
                "candidates_json": json.dumps(list(candidates),
                                              ensure_ascii=False),
                "trial_budget": int(trial_budget),
                "lockbox_uses_max": int(lockbox_uses_max),
                "dsr_trial_scope": dsr_trial_scope,
                "advance_rule": advance_rule,
                "close_rule": close_rule,
                "approved_by": approved_by,
                "evaluation_json": json.dumps(_san(evaluation),
                                              ensure_ascii=False),
                "git_commit": git_commit,
                "status": "open",
                "closed_utc": None})

    def record_round_close(self, round_id: str) -> None:
        """关闭轮次：open → closed 是**归档式改写**——旧行内容先完整进
        ledger_events（审计可重放"关闭前是什么"），row_hash 用
        (prev_hash, 新内容) 重算、后继行联动，链保持可验证。

        未开启过 → ValueError；已关闭 → ValueError（幂等拒绝，不二改）。
        关闭后该轮不能再运行（bt screen/gate 据状态拒绝），报告骨架由
        bt round close 另行生成。
        R6/C5：写前验证链（rounds 行被删/链不完整 → LedgerTamperedError）。
        """
        with self._txn():
            self._verify_or_raise()
            row = self.conn.execute(
                "SELECT rowid, status FROM rounds WHERE round_id=?",
                (round_id,)).fetchone()
            if row is None:
                raise ValueError(
                    f"轮次 {round_id} 未开启过（rounds 无此行）——无从关闭")
            if row["status"] != "open":
                raise ValueError(
                    f"轮次 {round_id} 已是 {row['status']!r}（{round_id} 的"
                    "关闭是一次性动作，不得二改）")
            cols = self._table_cols("rounds")
            names = ", ".join(f'"{c}"' for c in cols)
            content = dict(self.conn.execute(
                f"SELECT {names} FROM rounds WHERE rowid=?",
                (row["rowid"],)).fetchone())
            content["status"] = "closed"
            content["closed_utc"] = datetime.now(timezone.utc).isoformat()
            self._update_chained(
                "rounds", int(row["rowid"]), content,
                row_key=f"round_id={round_id}", action="open->closed")

    def round(self, round_id: str) -> dict | None:
        """轮次登记（rounds 表一行；不存在 → None）。candidates_json /
        evaluation_json 由调用方解析。"""
        row = self.conn.execute(
            "SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone()
        return None if row is None else dict(row)

    def rounds_any(self) -> bool:
        """台账是否登记过任何轮次（铁律 16：一旦有轮次，研究卡必须属于
        已开启轮次；False = 轮次制之前，旧卡照常运行）。"""
        return self.conn.execute(
            "SELECT 1 FROM rounds LIMIT 1").fetchone() is not None

    def round_n_dedup(self, round_id: str) -> int:
        """本轮累计去重试验数（trial_budget 的计数口径）：runs 中
        round_id 匹配的成功运行按 (strategy, params, symbol, timeframe,
        data_sha256, cost_model_version, strategy_sha) 七元组去重——与
        stats 的 n_dedup 同键同口径（迁移行与失败运行不计）。"""
        rows = self.conn.execute(
            "SELECT strategy, params_json, symbol, timeframe, data_sha256,"
            " cost_model_version, strategy_sha, actor, run_class,"
            " metrics_json FROM runs WHERE round_id=?",
            (round_id,)).fetchall()
        return self._dedup_n(rows)

    def round_lockbox_uses(self, round_id: str) -> int:
        """本轮已消耗的 G5 次数（lockbox_uses_max 的计数口径）：该轮内
        跑过的卡（runs.round_id 匹配）在 lockbox_uses 中的行数——含
        started（读了锁箱就算看过，T21a 同口径）。"""
        return int(self.conn.execute(
            "SELECT COUNT(*) n FROM lockbox_uses WHERE EXISTS ("
            " SELECT 1 FROM runs r WHERE r.card_id = lockbox_uses.card_id"
            " AND r.round_id = ?)", (round_id,)).fetchone()["n"])

    # ---------------------------------------------------------------- 读

    def lockbox_used(self, card_id: str) -> bool:
        """该卡的锁箱是否已用：**任何**行都算——包括上次崩溃残留的
        started（T21a：有 started 也算已用，防中途崩溃后重跑）。

        R6/C5 快照兜底：lockbox_uses 实际行数比 chain_meta 快照少（有行
        被删）→ 一律按**已用**返回（fail-closed：删行不可能重置一次性；
        无法分辨删的是哪张卡时宁可错杀）。"""
        row = self.conn.execute(
            "SELECT 1 FROM lockbox_uses WHERE card_id=?", (card_id,)).fetchone()
        if row is not None:
            return True
        snap = self.conn.execute(
            "SELECT row_count FROM chain_meta WHERE table_name='lockbox_uses'"
        ).fetchone()
        if snap is None:
            return False
        actual = int(self.conn.execute(
            "SELECT COUNT(*) FROM lockbox_uses").fetchone()[0])
        return int(snap["row_count"]) > actual

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
        R6/C5：写前验证链——lockbox_uses 行被删/链不完整时本方法直接
        LedgerTamperedError（fail-closed，删行不可能重置预算；查询类的
        快照兜底见 lockbox_used）。
        R7/N-C3：验链/查预算/登记/计数同一写事务（读到的计数与登记
        状态一致，不被并发写插入）。
        """
        key = _cutoff_iso(cutoff)
        if key is None:
            raise ValueError("lockbox_budget 需要 cutoff（锁箱标识）")
        with self._txn():
            self._verify_or_raise()
            row = self.conn.execute(
                "SELECT max_uses FROM lockbox_budget WHERE cutoff=?",
                (key,)).fetchone()
            if row is None:
                limit = DEFAULT_LOCKBOX_MAX_USES if max_uses is None \
                    else int(max_uses)
                if limit <= 0:
                    raise ValueError(
                        f"max_uses 必须为正整数，收到 {max_uses!r}——预算为"
                        "零或负数应直接不登记 G5，而不是写进表里")
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

    def _dedup_n(self, rows: list, *, only_research: bool = False) -> int:
        """按 (strategy, params, symbol, timeframe, data_sha256,
        cost_model_version, strategy_sha) 对成功运行去重计数（params 键序
        无关；T37：指纹进键——改代码重跑即新试验；run_class=migration 的
        迁移行不是试验，不计入——与旧行为 actor=migration 等价）。
        only_research=True（T64/D33）：只计 run_class=research（排除
        calibration/ops/migration——标定与运维不是研究试验）；行缺
        run_class 键时按 actor 推断（迁移前旧数据）。"""
        keys = set()
        for r in rows:
            rc = r["run_class"] if "run_class" in r.keys() else None
            if rc is None:
                rc = run_class_for_actor(r["actor"])
            if rc == "migration":
                continue
            if only_research and rc != "research":
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
        - project_n_dedup：项目级去重 N（两种调用都附带）；
        - project_n_dedup_research（T64/D33）：项目级**研究**去重 N——
          只计 run_class=research（排除 calibration/ops/migration；
          H 线与 P 线合并计算），dsr_trial_scope=project 时 G4 的取数
          来源。现行 n_dedup / family_n_dedup / project_n_dedup 的语义
          冻结不变（缺省 family 口径逐位一致）。
        迁移行（run_class=migration）计入 total_runs（可审计）、不计入 N。
        """
        all_rows = self.conn.execute(
            "SELECT card_id, metrics_json, strategy, params_json, symbol,"
            " timeframe, data_sha256, strategy_sha, cost_model_version,"
            " created_utc, actor, run_class FROM runs").fetchall()
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
            "project_n_dedup_research": self._dedup_n(
                all_rows, only_research=True),
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
