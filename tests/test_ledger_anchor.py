"""scripts/ledger_anchor.py（T37/M6/D27）：链头锚定与幂等。

锚定行 = 各链式表（runs/gates/lockbox_uses）的行数 + 链头 row_hash + UTC；
同链头不重复写；只读打开台账（绝不触发迁移、绝不写台账文件）。
"""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ledger_anchor.py"


def load_anchor():
    spec = importlib.util.spec_from_file_location("ledger_anchor_t37",
                                                  SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ledger_with_runs(tmp_path, n=2):
    from mt5quant.research.ledger import Ledger
    p = tmp_path / "ledger.sqlite"
    with Ledger(p) as led:
        for i in range(n):
            led.record_run(
                card_id="H001", card_version=1, strategy="s:C",
                params={"i": i}, symbol="X", timeframe="H1",
                data_first=None, data_last=None, data_sha256="a" * 64,
                engine_version="e", cost_model_version="v", gates_hash="",
                metrics={"n": 1}, actor="t")
        head = led.conn.execute(
            "SELECT row_hash FROM runs ORDER BY rowid DESC"
            " LIMIT 1").fetchone()[0]
    return p, head


def test_anchor_written_with_head_and_row_count(tmp_path):
    mod = load_anchor()
    p, head = _ledger_with_runs(tmp_path, n=2)
    out = tmp_path / "anchors.md"
    assert mod.write_anchor(out, p) is True
    text = out.read_text(encoding="utf-8")
    assert "| runs | 2 |" in text
    assert head in text                       # 链头 = 最后一行 row_hash
    assert text.count("| runs |") == 1        # 三表各一行：runs 只出现一次


def test_anchor_idempotent_same_head(tmp_path):
    """同链头（台账未变）→ 不重复写（幂等，计划任务可每日跑）。"""
    mod = load_anchor()
    p, _ = _ledger_with_runs(tmp_path)
    out = tmp_path / "anchors.md"
    assert mod.write_anchor(out, p) is True
    first = out.read_text(encoding="utf-8")
    assert mod.write_anchor(out, p) is False
    assert out.read_text(encoding="utf-8") == first


def test_anchor_updates_when_chain_grows(tmp_path):
    mod = load_anchor()
    p, _ = _ledger_with_runs(tmp_path, n=2)
    out = tmp_path / "anchors.md"
    assert mod.write_anchor(out, p) is True
    from mt5quant.research.ledger import Ledger
    with Ledger(p) as led:                    # 再记一行 → 链头变化
        led.record_run(
            card_id="H001", card_version=1, strategy="s:C", params={"i": 9},
            symbol="X", timeframe="H1", data_first=None, data_last=None,
            data_sha256="a" * 64, engine_version="e", cost_model_version="v",
            gates_hash="", metrics={"n": 1}, actor="t")
    assert mod.write_anchor(out, p) is True
    text = out.read_text(encoding="utf-8")
    assert "| runs | 2 |" in text and "| runs | 3 |" in text


def test_anchor_never_touches_ledger_file(tmp_path):
    """锚定只读：台账文件字节不变（绝不触发迁移/写入）。"""
    mod = load_anchor()
    p, _ = _ledger_with_runs(tmp_path)
    before = p.read_bytes()
    assert mod.write_anchor(tmp_path / "anchors.md", p) is True
    assert p.read_bytes() == before


def test_anchor_refuses_unmigrated_ledger(tmp_path):
    """旧库（无 row_hash 列）→ 明确报错指路，不静默锚定。"""
    p = tmp_path / "old.sqlite"
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE runs(run_id INTEGER PRIMARY KEY,"
                 " actor TEXT)")
    conn.commit()
    conn.close()
    mod = load_anchor()
    with pytest.raises(SystemExit, match="row_hash|迁移"):
        mod.chain_heads(p)


# ================================================ R7/N-I3：锚定历史前缀核验

def _rechain_runs(conn):
    """整链重算 runs（模拟攻击者改写历史后重封链）。"""
    import hashlib
    import json

    conn.row_factory = sqlite3.Row
    cols = [r[1] for r in conn.execute("PRAGMA table_info(runs)")]
    content_cols = [c for c in cols if c not in ("prev_hash", "row_hash")]
    sel = ", ".join(f'"{c}"' for c in content_cols)
    with conn:
        conn.execute("UPDATE runs SET actor='hacker' WHERE run_id=1")
    prev = "0" * 64
    head = None
    n = 0
    for r in conn.execute(
            f"SELECT rowid AS rid, {sel} FROM runs ORDER BY rowid").fetchall():
        canon = json.dumps({c: r[c] for c in content_cols},
                           sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
        h = hashlib.sha256((canon + prev).encode("utf-8")).hexdigest()
        conn.execute("UPDATE runs SET prev_hash=?, row_hash=? WHERE rowid=?",
                     (prev, h, r["rid"]))
        prev = h
        head = h
        n += 1
    # 连 chain_meta 一起同步（sqlite 侧证据全覆盖——正是锚定要防的场景）
    with conn:
        conn.execute(
            "UPDATE chain_meta SET last_rowid=(SELECT COALESCE(MAX(rowid),0)"
            " FROM runs), row_count=?, head_hash=?, updated_utc="
            "'2026-10-26T00:00:00+00:00' WHERE table_name='runs'", (n, head))
    conn.commit()


def test_anchor_verify_flags_rewrite_reseal_then_append(tmp_path):
    """N-I3 复现 B：重写历史→整链重封→同步快照→再追加——行数**增长**，
    锚定器必须重放锚定位置处的链头与锚记录比对并报出（旧行为只看行数
    增减，增长一律放行）。"""
    from mt5quant.research.ledger import Ledger

    mod = load_anchor()
    p, _head = _ledger_with_runs(tmp_path, n=2)
    out = tmp_path / "anchors.md"
    mod.write_anchor(out, p)
    conn = sqlite3.connect(str(p))
    _rechain_runs(conn)
    conn.close()
    with Ledger(p) as led:   # 追加一行：链/快照自洽，verify_chain 通过
        led.record_run(
            card_id="H001", card_version=1, strategy="s:C",
            params={"i": 9}, symbol="X", timeframe="H1",
            data_first=None, data_last=None, data_sha256="a" * 64,
            engine_version="e", cost_model_version="v", gates_hash="",
            metrics={"n": 1}, actor="t")
        assert led.verify_chain() == []
    problems = mod.verify_anchor(out, p)
    assert any("runs" in q and ("前缀" in q or "改写" in q or "重放" in q)
               for q in problems)


def test_write_anchor_refuses_when_prefix_broken(tmp_path):
    """N-I3：write_anchor 追加新锚**之前**先做历史前缀核验——前缀被重写
    时拒绝补写（锚文件不得收编被改写的历史），保持原文不变。"""
    from mt5quant.research.ledger import Ledger

    mod = load_anchor()
    p, _head = _ledger_with_runs(tmp_path, n=2)
    out = tmp_path / "anchors.md"
    mod.write_anchor(out, p)
    before = out.read_text(encoding="utf-8")
    conn = sqlite3.connect(str(p))
    _rechain_runs(conn)
    conn.close()
    with Ledger(p) as led:
        led.record_run(
            card_id="H001", card_version=1, strategy="s:C",
            params={"i": 9}, symbol="X", timeframe="H1",
            data_first=None, data_last=None, data_sha256="a" * 64,
            engine_version="e", cost_model_version="v", gates_hash="",
            metrics={"n": 1}, actor="t")
    with pytest.raises(RuntimeError, match="前缀"):
        mod.write_anchor(out, p)
    assert out.read_text(encoding="utf-8") == before   # 未收编被改写的历史


def test_anchor_prefix_check_tolerates_legit_inplace_update(tmp_path):
    """合法状态更新（gates 重录归档改写）跨越锚定：行数不变/增长时按锚定
    时刻的**归档旧内容**重放，不误报；write_anchor 可继续收编新链头。"""
    from mt5quant.research.ledger import Ledger

    mod = load_anchor()
    p = tmp_path / "ledger.sqlite"
    with Ledger(p) as led:
        rid = led.record_run(
            card_id="H001", card_version=1, strategy="s:C", params={},
            symbol="X", timeframe="H1", data_first=None, data_last=None,
            data_sha256="a" * 64, engine_version="e",
            cost_model_version="v", gates_hash="", metrics={"n": 1},
            actor="t")
        led.record_gate(rid, "G0", False, {"attempt": 1})
    out = tmp_path / "anchors.md"
    assert mod.write_anchor(out, p)
    with Ledger(p) as led:
        led.record_gate(rid, "G0", True, {"attempt": 2})   # 原地改写 + 归档
        led.record_run(
            card_id="H001", card_version=1, strategy="s:C", params={"i": 2},
            symbol="X", timeframe="H1", data_first=None, data_last=None,
            data_sha256="a" * 64, engine_version="e",
            cost_model_version="v", gates_hash="", metrics={"n": 1},
            actor="t")
    assert mod.verify_anchor(out, p) == []     # 不误报（按归档旧内容重放）
    assert mod.write_anchor(out, p) is True    # 可继续锚定
    assert mod.verify_anchor(out, p) == []


def test_same_tick_rewrite_does_not_false_alarm(tmp_path, monkeypatch):
    """锚定 ts 与归档事件 recorded_utc 同刻度（CI VM 时钟粒度）不误报。

    run#80/#82 实测：write_anchor 的 now() 与紧随的 record_gate 的 now()
    在 CI 虚拟机上可返回相同值，严格 > 比较漏掉归档、按改写后内容重放，
    误报"gates：行数与锚定相同但链头不符"。冻结时钟确定性复现（本地
    物理机时钟精确、真实时序永不相同，故必须冻结才能测到）。
    """
    from datetime import datetime, timezone

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 27, 12, 0, 0, 0, tzinfo=timezone.utc)

    mod = load_anchor()
    import mt5quant.research.ledger as ledger_mod
    monkeypatch.setattr(mod, "datetime", Frozen)
    monkeypatch.setattr(ledger_mod, "datetime", Frozen)

    from mt5quant.research.ledger import Ledger
    p = tmp_path / "ledger.sqlite"
    with Ledger(p) as led:
        rid = led.record_run(
            card_id="H001", card_version=1, strategy="s:C", params={},
            symbol="X", timeframe="H1", data_first=None, data_last=None,
            data_sha256="a" * 64, engine_version="e",
            cost_model_version="v", gates_hash="", metrics={"n": 1},
            actor="t")
        led.record_gate(rid, "G0", False, {"attempt": 1})
    out = tmp_path / "anchors.md"
    assert mod.write_anchor(out, p) is True
    with Ledger(p) as led:
        led.record_gate(rid, "G0", True, {"attempt": 2})   # 同刻度改写 + 归档
    assert mod.verify_anchor(out, p) == []
