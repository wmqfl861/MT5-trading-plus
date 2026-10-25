"""watch_swap 纯逻辑测试：FakeBroker 上 swap 字段变化 → 恰记一行（T35/H5）。

计划失败测试清单第 4 条：swap 字段在 21:00 变化 → 记录 1 行，时刻正确。
真实终端轮询由计划任务（scripts/setup_scheduled_tasks.ps1 本周任务）执行，
本测试只验证纯逻辑：快照差分、magic 过滤、CSV 追加、跨进程状态恢复。
"""
import csv
import importlib.util
import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.fakes_live import FakeBroker

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "watch_swap.py"

MAGIC = 20261025
ONE_MIN = timedelta(minutes=1)
T_2059 = datetime(2026, 10, 26, 20, 59, 0)     # 计费时刻前
T_2100 = datetime(2026, 10, 26, 21, 0, 0)      # 计费时刻（纽约口径预期）


def load_module():
    spec = importlib.util.spec_from_file_location("watch_swap", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["watch_swap"] = mod
    spec.loader.exec_module(mod)
    return mod


ws = load_module()


def seeded_broker() -> tuple[FakeBroker, object]:
    b = FakeBroker("EURUSD")
    pos = b.seed_position(side="buy", volume=0.01, magic=MAGIC)
    return b, pos


def read_rows(csv_path: Path) -> list[dict]:
    with open(csv_path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


# ---- 基线与变化：21:00 变化恰记一行，时刻/前后值正确 ----


def test_swap_change_at_2100_records_exactly_one_row(tmp_path):
    b, pos = seeded_broker()
    w = ws.SwapWatcher(b, magic=MAGIC, out_dir=str(tmp_path))

    assert w.poll(now=T_2059) == []            # 首次轮询：建基线，不记行
    assert w.poll(now=T_2059 + timedelta(seconds=30)) == []  # 值未变：不记行

    b._replace_position(replace(pos, swap=-5.9))       # 21:00 计费：swap 变化
    rows = w.poll(now=T_2100)
    assert len(rows) == 1
    r = rows[0]
    assert r["ts_utc"] == T_2100.isoformat()
    assert r["symbol"] == "EURUSD"
    assert float(r["swap_before"]) == 0.0
    assert float(r["swap_after"]) == -5.9

    csv_rows = read_rows(tmp_path / "swap-watch-2026-10-26.csv")
    assert len(csv_rows) == 1
    assert csv_rows[0]["ticket"] == str(pos.ticket)
    assert float(csv_rows[0]["swap_after"]) == -5.9

    assert w.poll(now=T_2100 + ONE_MIN) == []   # 之后值稳定：不再记行


def test_first_poll_writes_baseline_without_rows(tmp_path):
    b, _ = seeded_broker()
    w = ws.SwapWatcher(b, magic=MAGIC, out_dir=str(tmp_path))
    assert w.poll(now=T_2059) == []
    assert not (tmp_path / "swap-watch-2026-10-26.csv").exists()  # 无变化不落 CSV


# ---- 跨进程状态：--once 每分钟一个进程，状态从盘上恢复 ----


def test_state_persists_across_watcher_instances(tmp_path):
    b, pos = seeded_broker()
    ws.SwapWatcher(b, magic=MAGIC, out_dir=str(tmp_path)).poll(now=T_2059)
    b._replace_position(replace(pos, swap=-5.9))

    # 新实例（模拟计划任务每分钟新起进程）
    w2 = ws.SwapWatcher(b, magic=MAGIC, out_dir=str(tmp_path))
    rows = w2.poll(now=T_2100)
    assert len(rows) == 1 and float(rows[0]["swap_after"]) == -5.9


def test_state_file_location_matches_csv(tmp_path):
    b, _ = seeded_broker()
    w = ws.SwapWatcher(b, magic=MAGIC, out_dir=str(tmp_path))
    w.poll(now=T_2059)
    state = json.loads((tmp_path / "swap-watch-2026-10-26.state.json")
                       .read_text(encoding="utf-8"))
    assert "EURUSD" in state and state["EURUSD"]


# ---- magic 过滤与多品种 ----


def test_other_magic_positions_ignored(tmp_path):
    b = FakeBroker("EURUSD")
    mine = b.seed_position(magic=MAGIC)
    other = b.seed_position(magic=999)
    w = ws.SwapWatcher(b, magic=MAGIC, out_dir=str(tmp_path))
    w.poll(now=T_2059)
    b._replace_position(replace(mine, swap=-5.9))
    b._replace_position(replace(other, swap=-99.0))    # 别人的仓：不记
    rows = w.poll(now=T_2100)
    assert len(rows) == 1 and float(rows[0]["swap_after"]) == -5.9


def test_two_symbols_both_recorded(tmp_path):
    b1, p1 = seeded_broker()
    b2 = FakeBroker("XAUUSD")
    p2 = b2.seed_position(magic=MAGIC)

    class _Accounts:
        def positions(self):
            return b1.positions() + b2.positions()

    w = ws.SwapWatcher(_Accounts(), magic=MAGIC, out_dir=str(tmp_path))
    w.poll(now=T_2059)
    b1._replace_position(replace(p1, swap=-5.9))
    b2._replace_position(replace(p2, swap=-49.08))
    rows = w.poll(now=T_2100)
    assert {r["symbol"] for r in rows} == {"EURUSD", "XAUUSD"}


def test_utc_date_in_filename_and_iso_ts(tmp_path):
    b, _ = seeded_broker()
    # 2026-10-26 23:59 UTC 的轮询落 10-26 文件；次晨 00:01 落 10-27 文件
    w = ws.SwapWatcher(b, magic=MAGIC, out_dir=str(tmp_path))
    assert w.poll(now=datetime(2026, 10, 26, 23, 59)) == []
    assert (tmp_path / "swap-watch-2026-10-26.state.json").exists()
    w2 = ws.SwapWatcher(b, magic=MAGIC, out_dir=str(tmp_path))
    assert w2.poll(now=datetime(2026, 10, 27, 0, 1)) == []
    assert (tmp_path / "swap-watch-2026-10-27.state.json").exists()


def test_aware_and_naive_now_accepted(tmp_path):
    b, pos = seeded_broker()
    w = ws.SwapWatcher(b, magic=MAGIC, out_dir=str(tmp_path))
    w.poll(now=T_2059.replace(tzinfo=timezone.utc))    # aware UTC
    b._replace_position(replace(pos, swap=-1.0))
    rows = w.poll(now=T_2100)                           # naive 视为 UTC
    assert len(rows) == 1
