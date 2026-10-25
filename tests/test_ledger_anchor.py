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
