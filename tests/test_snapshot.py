"""品种参数快照测试（P0）。

覆盖：快照字段完整性（含新增合约参数与 captured_at_utc）、diff 语义
（同对象为空 / 单字段变更恰好一条 / 品种增减）、旧式 row 缺字段兜底、
落盘往返（latest + history），以及两个脚本的纯辅助函数。
"""
import importlib.util
import json
from collections import namedtuple
from pathlib import Path

from mt5gw.snapshot import (
    SpecChange, diff_snapshots, load_snapshot, save_snapshot,
    spec_to_json, take_snapshot,
)
from mt5gw.symbols import _to_spec

# 全字段 row：字段名与真包 symbol_info 一致（trade_tick_size / trade_tick_value 等）
FullRow = namedtuple(
    "FullRow",
    "name description digits point spread trade_mode currency_profit currency_margin "
    "trade_contract_size volume_min volume_max volume_step filling_mode visible "
    "trade_stops_level swap_long swap_short trade_freeze_level "
    "trade_tick_size trade_tick_value trade_calc_mode margin_initial margin_maintenance "
    "swap_mode swap_rollover3days",
)
# 旧式 row：无新字段（与 fakes.py 时代的 SymRow 同构，证明 getattr 兜底）
OldRow = namedtuple(
    "OldRow",
    "name description digits point spread trade_mode currency_profit currency_margin "
    "trade_contract_size volume_min volume_max volume_step filling_mode visible",
)


def full(sym="XAUUSD"):
    return FullRow(
        name=sym, description="Gold vs USD", digits=2, point=0.01, spread=20,
        trade_mode=4, currency_profit="USD", currency_margin="USD",
        trade_contract_size=100.0, volume_min=0.01, volume_max=100.0,
        volume_step=0.01, filling_mode=1, visible=True,
        trade_stops_level=30, swap_long=-18.4, swap_short=9.2,
        trade_freeze_level=15, trade_tick_size=0.01, trade_tick_value=1.0,
        trade_calc_mode=1, margin_initial=0.005, margin_maintenance=0.0025,
        swap_mode=1, swap_rollover3days=3,
    )


class FakeCatalog:
    """最小 SymbolCatalog 替身：get 走真实 _to_spec（把映射路径一并覆盖）。"""

    def __init__(self, rows):
        self._rows = {r.name: r for r in rows}

    def get(self, name):
        return _to_spec(self._rows[name])

    def all(self):
        return [_to_spec(r) for r in self._rows.values()]


def _load_script(name: str):
    path = Path(__file__).resolve().parents[1] / "scripts" / name
    spec = importlib.util.spec_from_file_location(name[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ALL_PARAM_FIELDS = [
    "contract_size", "volume_min", "volume_step", "volume_max",
    "trade_stops_level", "trade_freeze_level",
    "swap_long", "swap_short",
    "tick_size", "tick_value", "trade_calc_mode",
    "margin_initial", "margin_maintenance",
    "swap_mode", "swap_rollover3days",
]


# ---- 快照内容 ----

def test_snapshot_contains_all_contract_param_fields():
    snap = take_snapshot(FakeCatalog([full()]), ["XAUUSD"])
    entry = snap["XAUUSD"]
    for f in ALL_PARAM_FIELDS:
        assert f in entry, f"快照缺字段 {f}"
    assert entry["contract_size"] == 100.0
    assert entry["swap_long"] == -18.4 and entry["swap_short"] == 9.2
    assert entry["trade_freeze_level"] == 15
    assert entry["tick_size"] == 0.01 and entry["tick_value"] == 1.0
    assert entry["trade_calc_mode"] == 1
    assert entry["margin_initial"] == 0.005 and entry["margin_maintenance"] == 0.0025
    assert entry["swap_mode"] == 1          # T5：计费口径（1=点数）
    assert entry["swap_rollover3days"] == 3  # T5：三倍日（MT5 星期枚举，周三=3）
    assert entry["captured_at_utc"]  # 抓取时刻必填
    assert "raw" not in entry  # 原始对象不进 JSON


def test_snapshot_old_row_missing_new_fields_falls_back_to_defaults():
    row = OldRow(
        name="EURUSD", description="", digits=5, point=0.00001, spread=12,
        trade_mode=4, currency_profit="USD", currency_margin="USD",
        trade_contract_size=100000.0, volume_min=0.01, volume_max=100.0,
        volume_step=0.01, filling_mode=1, visible=True,
    )
    snap = take_snapshot(FakeCatalog([row]), ["EURUSD"])
    e = snap["EURUSD"]
    assert e["swap_long"] == 0.0 and e["swap_short"] == 0.0
    assert e["trade_freeze_level"] == 0
    assert e["tick_size"] == 0.0 and e["tick_value"] == 0.0
    assert e["trade_calc_mode"] == 0
    assert e["margin_initial"] == 0.0 and e["margin_maintenance"] == 0.0
    # T5：旧式 row 缺 swap 字段 → getattr 兜底（0=禁用 / 3=MT5 周三）
    assert e["swap_mode"] == 0 and e["swap_rollover3days"] == 3
    assert e["contract_size"] == 100000.0  # 旧字段照常映射


# ---- diff 语义 ----

def test_diff_same_object_is_empty():
    cat = FakeCatalog([full()])
    # captured_at_utc 不参与比对：两次抓取时间不同也必须零差异
    assert diff_snapshots(take_snapshot(cat, ["XAUUSD"]),
                          take_snapshot(cat, ["XAUUSD"])) == []


def test_diff_single_changed_field_reported_exactly():
    old_snap = take_snapshot(FakeCatalog([full()]), ["XAUUSD"])
    new_snap = take_snapshot(
        FakeCatalog([full()._replace(swap_long=-25.0)]), ["XAUUSD"])
    assert diff_snapshots(old_snap, new_snap) == [
        SpecChange("swap_long", "XAUUSD", -18.4, -25.0)
    ]


def test_diff_reports_added_and_removed_symbols():
    old = take_snapshot(FakeCatalog([full()]), ["XAUUSD"])
    new = take_snapshot(FakeCatalog([full(), full("BTCUSD")]),
                        ["XAUUSD", "BTCUSD"])
    assert diff_snapshots(old, new) == [SpecChange("symbol", "BTCUSD", None, "<新增>")]
    assert diff_snapshots(new, old) == [SpecChange("symbol", "BTCUSD", "<移除>", None)]


# ---- 落盘往返 ----

def test_save_snapshot_writes_latest_and_history_roundtrip(tmp_path):
    snap = take_snapshot(FakeCatalog([full()]), ["XAUUSD"])
    latest = save_snapshot(str(tmp_path), snap)
    assert latest == str(tmp_path / "specs" / "latest.json")
    assert load_snapshot(latest) == snap
    history = sorted((tmp_path / "specs" / "history").glob("*.json"))
    assert len(history) == 1
    with open(history[0], encoding="utf-8") as f:
        assert json.load(f) == snap


def test_spec_to_json_excludes_raw_only_whitelist_scalars():
    spec = _to_spec(full())
    entry = spec_to_json(spec)
    assert set(entry) == {
        "name", "description", "digits", "point", "spread", "trade_mode",
        "currency_profit", "currency_margin", "contract_size",
        "volume_min", "volume_max", "volume_step", "filling_mode",
        "trade_stops_level", "swap_long", "swap_short", "trade_freeze_level",
        "tick_size", "tick_value", "trade_calc_mode",
        "margin_initial", "margin_maintenance", "captured_at_utc",
        "swap_mode", "swap_rollover3days",
    }
    json.dumps(entry)  # 全部可 JSON 化（raw 对象进 JSON 会当场炸）


def test_diff_reports_swap_mode_change():
    # T5：swap_mode/swap_rollover3days 变更必须被 diff 抓到（券商改口径第一时间可见）
    old_snap = take_snapshot(FakeCatalog([full()]), ["XAUUSD"])
    new_snap = take_snapshot(
        FakeCatalog([full()._replace(swap_mode=4)]), ["XAUUSD"])
    assert diff_snapshots(old_snap, new_snap) == [
        SpecChange("swap_mode", "XAUUSD", 1, 4)
    ]
    old_snap = take_snapshot(FakeCatalog([full()]), ["XAUUSD"])
    new_snap = take_snapshot(
        FakeCatalog([full()._replace(swap_rollover3days=5)]), ["XAUUSD"])
    assert diff_snapshots(old_snap, new_snap) == [
        SpecChange("swap_rollover3days", "XAUUSD", 3, 5)
    ]


# ---- 脚本辅助函数 ----

def test_script_render_changes_reports_no_change():
    mod = _load_script("snapshot_specs.py")
    assert mod.render_changes([]) == "无变化"
    text = mod.render_changes([SpecChange("swap_long", "XAUUSD", -18.4, -25.0)])
    assert "XAUUSD" in text and "-18.4" in text and "-25.0" in text


def test_script_append_samples_and_summarize(tmp_path):
    mod = _load_script("measure_server_clock.py")
    p = str(tmp_path / "server_clock.json")
    total = mod.append_samples(
        p, [{"wall_utc": "w1", "server_time": "s1", "offset_s": 7200.0}])
    assert total == 1
    total = mod.append_samples(p, [
        {"wall_utc": "w2", "server_time": "s2", "offset_s": 7201.0},
        {"wall_utc": "w3", "server_time": "s3", "offset_s": 7200.5},
    ])
    assert total == 3
    with open(p, encoding="utf-8") as f:
        records = json.load(f)
    assert [r["wall_utc"] for r in records] == ["w1", "w2", "w3"]  # 旧记录保留
    s = mod.summarize_offsets([r["offset_s"] for r in records])
    assert s["min_s"] == 7200.0 and s["max_s"] == 7201.0
    assert s["median_s"] == 7200.5 and s["spread_s"] == 1.0
    assert s["stable"] is True   # 极差 1.0s ≤ 判据（tick 粒度 1s）
    assert mod.summarize_offsets([0.0, 5.0])["stable"] is False


def test_measure_server_clock_stale_detection():
    """I5 回归：休市静默期的陈旧 tick（server_time 远落后 wall）必须被判 stale。"""
    mod = _load_script("measure_server_clock.py")
    from datetime import datetime, timedelta
    wall = datetime(2026, 10, 24, 14, 0, tzinfo=datetime.utcoffset(None) or None)         if False else datetime.fromisoformat("2026-10-24T14:00:00+00:00")
    stale = {"wall_utc": wall.isoformat(),
             "server_time": "2026-10-23T20:57:59",  # 周五收盘后 17 小时
             "offset_s": -61741.0}
    fresh = {"wall_utc": wall.isoformat(),
             "server_time": "2026-10-24T13:59:58",
             "offset_s": -2.0}
    assert mod.is_stale(stale) is True
    assert mod.is_stale(fresh) is False
