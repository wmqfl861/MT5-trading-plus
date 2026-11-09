"""T188（第十七轮 D75，2026-11-09）：判决代码指纹改为 git tree 哈希。

覆盖计划 docs/handoff/2026-11-08-round17-plan.md §2 P1 T188 三条（全部
合成 git 仓库 / 合成台账，不读真实行情、不动生产台账）：

1. D75：`verdict_code_sha` = `src/mt5quant`、`src/mt5data`、`scripts/bt.py`
   三处在**干净检出**（HEAD 提交）下的 git tree/blob 哈希 + 日历工件成分
   （D66 保留）——
   - **覆盖补全**（H17-2 正面）：lookahead.py（G0）、robustness.py
     （ScaledCosts ×1.5 否决）、gates.py（evaluate_v3）、costs/* 任一改动
     → 指纹变（旧 D66 指纹只含 events.py，这些改动不涨 N）；
   - **过宽收窄**（H17-2 反面）：三路径之外（mt5gw、其他 scripts）的改动
     → 指纹不变（旧 D66 把整份 bt.py 之外的世界排除但对 bt.py 内无关
     改动也涨 N；D75 的三子树口径逐字）；
   - 同一提交下确定性（tree 哈希是提交内容的纯函数，与检出平台无关
     ——取代 D66 的手工 CRLF→LF 归一）；
   - 提交号随身份返回并在 runs 行记录（tree 哈希不唯一确定提交）。
2. 工作树不干净（三路径有未提交改动/未跟踪文件）→ 拒绝（RuntimeError，
   接到预检 T170 check_worktree_clean 的 git status --porcelain 同口径；
   三路径之外的不干净不影响指纹正确性——全仓干净由预检把关）。
3. 台账迁移按铁律只追加：runs 加 verdict_commit 列（旧行 NULL 不回填，
   与 T151 verdict_code_sha 迁移同一信任边界）。
"""
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "bt.py"
REPO_ROOT = Path(__file__).resolve().parents[1]


def load_bt():
    import importlib.util
    spec = importlib.util.spec_from_file_location("bt_script_t188", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------- 合成仓库（三判决路径齐备）

_SYNTH_FILES = {
    "src/mt5quant/__init__.py": "# pkg\n",
    "src/mt5quant/validation/__init__.py": "# pkg\n",
    "src/mt5quant/validation/events.py": "def event_returns():\n    return 0\n",
    "src/mt5quant/validation/lookahead.py": "def run_g0():\n    return {}\n",
    "src/mt5quant/validation/robustness.py": (
        "class ScaledCosts:\n    def __init__(self, base, scale):\n"
        "        self.base, self.scale = base, scale\n"),
    "src/mt5quant/validation/gates.py": (
        "def evaluate_v3():\n    return {'passed': True}\n"),
    "src/mt5quant/validation/stats.py": "def t_stat():\n    return 0.0\n",
    "src/mt5quant/costs/__init__.py": "# pkg\n",
    "src/mt5quant/costs/model.py": (
        "def spread_p90():\n    return 0.0\n"),
    "src/mt5data/__init__.py": "# pkg\n",
    "src/mt5data/ext.py": "def merged_research_series():\n    return None\n",
    "scripts/bt.py": "ENGINE_VERSION = 'A'\n",
    # 三路径之外（过宽收窄断言用）：改这些不得变指纹
    "src/mt5gw/__init__.py": "# pkg\n",
    "src/mt5gw/connection.py": "class MT5Connection:\n    pass\n",
    "scripts/live_runner.py": "def main():\n    return 0\n",
    "README.md": "synthetic\n",
}


def _git(repo: Path, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace",
                          timeout=60, check=check)


def build_synth_repo(tmp_path: Path) -> Path:
    """合成仓库：三判决路径（src/mt5quant 含 lookahead/robustness/gates/
    costs、src/mt5data、scripts/bt.py）+ 三路径之外的对照文件，全部提交。"""
    repo = tmp_path / "repo"
    for rel, text in _SYNTH_FILES.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t188@test")
    _git(repo, "config", "user.name", "t188")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "synthetic verdict paths")
    return repo


def _commit_change(repo: Path, rel: str, text: str, msg="change"):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)


# ================================================== 组成 / 确定性 / 提交号

def test_identity_composition_tree_hashes_and_commit(tmp_path):
    """T188/D75 组成逐字：三处 tree/blob 哈希（=git rev-parse HEAD:<path>）
    + 提交号 + 日历成分；同一提交两次计算逐位相同（确定性=tree 哈希是
    提交内容的纯函数，与检出平台/文件字节无关）。"""
    mod = load_bt()
    repo = build_synth_repo(tmp_path)
    ident1 = mod._verdict_code_identity(
        calendar_component=f"fomc:{'a' * 64}", repo_root=repo)
    ident2 = mod._verdict_code_identity(
        calendar_component=f"fomc:{'a' * 64}", repo_root=repo)
    assert ident1 == ident2
    assert re.fullmatch(r"[0-9a-f]{64}", ident1["verdict_code_sha"])
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert ident1["commit"] == head                 # 提交号随身份返回
    for rel in ("src/mt5quant", "src/mt5data"):
        want = _git(repo, "rev-parse", f"HEAD:{rel}").stdout.strip()
        assert ident1["trees"][rel] == want         # 子树 tree 哈希（天然涵盖
        # 子树全部文件——lookahead/robustness/gates/events/costs 都在
        # src/mt5quant 里）
    want_bt = _git(repo, "rev-parse", "HEAD:scripts/bt.py").stdout.strip()
    assert ident1["trees"]["scripts/bt.py"] == want_bt   # 文件 = blob 哈希
    # 日历成分敏感（D66 语义保留）：换日历工件 = 新指纹
    assert mod._verdict_code_identity(
        calendar_component=f"fomc:{'b' * 64}", repo_root=repo)[
        "verdict_code_sha"] != ident1["verdict_code_sha"]
    assert mod._verdict_code_identity(
        calendar_component=None, repo_root=repo)[
        "verdict_code_sha"] != ident1["verdict_code_sha"]
    # _verdict_code_sha 恒等包装（既有调用点/探针内省目标不变）
    assert mod._verdict_code_sha(
        calendar_component=f"fomc:{'a' * 64}", repo_root=repo) \
        == ident1["verdict_code_sha"]


def test_fingerprint_covers_all_verdict_modules(tmp_path):
    """H17-2 正面（覆盖补全）：事件模式 V3 判决实际调用的模块——
    validation/lookahead.py（G0）、validation/robustness.py（ScaledCosts
    ×1.5 否决）、validation/gates.py（evaluate_v3）、costs/*（成本计算，
    cost_model_version 只描述配置）——任一改动 → 指纹变（RED 基线：
    旧 D66 指纹只含 events.py，这些改动全部不涨 N）。"""
    mod = load_bt()
    repo = build_synth_repo(tmp_path)
    base = mod._verdict_code_sha(repo_root=repo)
    prev = base
    for rel, text in (
        ("src/mt5quant/validation/lookahead.py",
         "def run_g0():\n    return {'passed': False}\n"),
        ("src/mt5quant/validation/robustness.py",
         "class ScaledCosts:\n    SCALE = 1.5\n"),
        ("src/mt5quant/validation/gates.py",
         "def evaluate_v3():\n    return {'passed': False}\n"),
        ("src/mt5quant/validation/events.py",
         "def event_returns():\n    return 1\n"),
        ("src/mt5quant/costs/model.py",
         "def spread_p90():\n    return 1e-4\n"),
        ("src/mt5data/ext.py",
         "def merged_research_series():\n    return object()\n"),
        ("scripts/bt.py", "ENGINE_VERSION = 'A2'\n"),
    ):
        _commit_change(repo, rel, text, msg=f"touch {rel}")
        now = mod._verdict_code_sha(repo_root=repo)
        assert now != prev, f"改 {rel} 后指纹未变（H17-2：判决路径改动必变指纹）"
        prev = now


def test_fingerprint_narrow_not_overbroad(tmp_path):
    """H17-2 反面（过宽收窄）：三判决路径之外的改动（mt5gw、其他
    scripts、文档）→ 指纹不变（不生成新试验）。"""
    mod = load_bt()
    repo = build_synth_repo(tmp_path)
    base = mod._verdict_code_sha(repo_root=repo)
    for rel, text in (
        ("src/mt5gw/connection.py", "class MT5Connection:\n    v = 2\n"),
        ("scripts/live_runner.py", "def main():\n    return 1\n"),
        ("README.md", "synthetic v2\n"),
    ):
        _commit_change(repo, rel, text, msg=f"touch {rel}")
        assert mod._verdict_code_sha(repo_root=repo) == base, (
            f"改 {rel}（判决路径之外）后指纹变了——D75 三子树口径下不应"
            "生成新试验")


# ================================================== 脏树拒绝（接到预检口径）

def test_dirty_verdict_paths_refused_clean_allowed(tmp_path):
    """计划 T188 第 1 条：工作树不干净时拒绝运行——三判决路径任一有
    未提交改动 / 未跟踪文件 → RuntimeError（fail-closed：tree 哈希描述
    的是干净检出，工作树实际执行的可能不同）；三路径之外不干净不影响
    （全仓干净由预检 T170 把关，同一 git status --porcelain 口径）。"""
    mod = load_bt()
    repo = build_synth_repo(tmp_path)
    assert mod._verdict_code_sha(repo_root=repo)      # 干净 → 正常计算
    # 未提交改动（跟踪文件）
    (repo / "src" / "mt5quant" / "validation" / "lookahead.py").write_text(
        "dirty\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="lookahead"):
        mod._verdict_code_sha(repo_root=repo)
    _git(repo, "checkout", "--", ".")
    # 未跟踪文件落在判决路径内（可能被 import 的影子模块）
    (repo / "src" / "mt5quant" / "validation" / "shadow.py").write_text(
        "x = 1\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="shadow"):
        mod._verdict_code_sha(repo_root=repo)
    (repo / "src" / "mt5quant" / "validation" / "shadow.py").unlink()
    # scripts/bt.py 脏 → 拒绝
    (repo / "scripts" / "bt.py").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="bt.py"):
        mod._verdict_code_sha(repo_root=repo)
    _git(repo, "checkout", "--", ".")
    # 三路径之外的未跟踪/改动不影响指纹（指纹只描述三处）
    (repo / "evidence.txt").write_text("untracked\n", encoding="utf-8")
    (repo / "src" / "mt5gw" / "connection.py").write_text("dirty\n",
                                                          encoding="utf-8")
    assert mod._verdict_code_sha(repo_root=repo)


def test_missing_verdict_path_or_broken_git_fail_closed(tmp_path):
    """三路径任一不在 HEAD（仓库形态不符）→ RuntimeError；非 git 目录 →
    RuntimeError（指纹算不出就拒绝运行，与 strategy_fingerprint 同
    fail-fast 口径）。"""
    mod = load_bt()
    repo = build_synth_repo(tmp_path)
    _git(repo, "rm", "-rq", "src/mt5data")
    _git(repo, "commit", "-qm", "drop mt5data")
    with pytest.raises(RuntimeError, match="src/mt5data"):
        mod._verdict_code_sha(repo_root=repo)
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    (plain / "src").mkdir()
    with pytest.raises(RuntimeError):
        mod._verdict_code_sha(repo_root=plain)


# ================================================== 台账：只追加迁移 + 记录

def test_ledger_verdict_commit_additive_migration(tmp_path):
    """T188 第 2 条（铁律只追加）：旧库（无该列）重开后补列，旧行
    verdict_commit 不回填（NULL）、其余内容值一字不改、链校验通过；
    新行写入提交号。"""
    from mt5quant.research.ledger import Ledger
    path = tmp_path / "led.sqlite"
    kw = dict(card_id="H102", card_version=1, strategy="s", params={},
              symbol="USTEC", timeframe="D1", data_first="", data_last="",
              data_sha256="d" * 64, engine_version="A",
              cost_model_version="c", gates_hash="g",
              metrics={"sharpe": 1.0}, actor="bt", round_id="R3")
    with Ledger(path) as led:
        rid = led.record_run(**kw)
        row = led.conn.execute(
            "SELECT * FROM runs WHERE run_id=?", (rid,)).fetchone()
        before = {k: row[k] for k in row.keys()
                  if k not in ("prev_hash", "row_hash", "verdict_commit")}
    # 模拟 T188 之前的旧库：列不存在
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE runs DROP COLUMN verdict_commit")
    con.commit()
    con.close()
    with Ledger(path) as led:                        # 打开即迁移（只补列）
        assert led.verify_chain() == []
        row = led.conn.execute(
            "SELECT * FROM runs WHERE run_id=?", (rid,)).fetchone()
        assert row["verdict_commit"] is None         # 旧行不回填
        for k, v in before.items():
            assert row[k] == v, k                    # 旧行内容值一字不改
        rid2 = led.record_run(**kw, verdict_commit="ab12cd34" * 5)
        row2 = led.conn.execute(
            "SELECT verdict_commit FROM runs WHERE run_id=?",
            (rid2,)).fetchone()
        assert row2["verdict_commit"] == "ab12cd34" * 5   # 新行写入
        assert led.verify_chain() == []


def test_gate_run_records_verdict_commit_in_runs_row(
        tmp_path, monkeypatch, capsys):
    """T188 第 1 条：runs 行同时记录提交号——CLI 事件模式 gate 后
    runs.verdict_commit == HEAD（真实仓库的当前提交号，40 hex），
    verdict_code_sha 仍与 _verdict_code_sha 同源；bar 级行为不变
    （verdict_commit NULL）。工作树须干净（bt/src 提交后再跑）。"""
    from tests.test_fomc_calendar_engine import (
        _setup_fomc_round, _fomc_gate_args, meeting, write_calendar,
    )
    from mt5quant.research.ledger import Ledger
    mod = load_bt()
    root, cards_dir, ledger_path = _setup_fomc_round(tmp_path, monkeypatch,
                                                     mod)
    cal_path, sha = write_calendar(tmp_path, [meeting("2024-01-31")])
    args = _fomc_gate_args(root, cards_dir, ledger_path, cal_path, sha)
    rc = mod.main(args)
    out = capsys.readouterr().out
    assert rc in (0, 1), out            # G1 关卡可败——判据只看 runs 行
    head = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=60, check=True).stdout.strip()
    with Ledger(ledger_path) as led:
        rows = led.conn.execute(
            "SELECT verdict_code_sha, verdict_commit FROM runs"
            " WHERE card_id='H704'").fetchall()
        assert rows, "事件模式 gate 应有 runs 行"
        for r in rows:
            assert r["verdict_code_sha"] == mod._verdict_code_sha(
                calendar_component=f"fomc:{sha}")
            assert r["verdict_commit"] == head     # 提交号逐位 == HEAD
        v3 = led.conn.execute(
            "SELECT details_json FROM gates WHERE gate='V3'").fetchone()
        import json
        details = json.loads(v3["details_json"])
        assert details["verdict_commit"] == head   # V3 details 同源记录
    assert "verdict_commit" in out                 # 报告层可见
