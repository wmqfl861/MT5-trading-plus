"""scripts/calibrate_costs.py 纯逻辑测试：假 gateway 注入 + 手算数字。

锚点（2026-10）：10-19 周一、10-20 周二、10-21 周三、10-22 周四、10-23 周五、
10-24 周六。快照 EURUSD 为点数模式（-7 点 × point 1e-5 × 合约 100000 = -7 USD/手/夜），
三倍日 MT5 周三（=3 → Python weekday 2）。

手算场景（佣金一律 开 30/手、平 20/手）：
- P1 多 1 手 (周一 20:30, 周二 21:30] swap −14 → H=21 时跨周一/二两夜 → −7/夜
- P2 多 1 手 (周二 14:00, 周三 03:00] swap −7  → H=21 时跨周一夜 → −7/夜
- P3 多 1 手 (周三 20:00, 周四 06:00] swap −21 → H=21 时跨周三夜 ×3 → −7/夜
- P4 空 1 手 (周四 18:00, 周五 21:30] swap −14 → H=21 时跨周四/五两夜 → −7/夜
四笔的费率一致性唯一地把计费时刻钉在 21 点（其他时刻必有一笔费率偏离中位数）。
"""
import importlib.util
import itertools
import json
from collections import namedtuple
from datetime import datetime
from pathlib import Path

import pytest

from mt5gw.models import AccountState, Deal
from mt5quant.costs.commission import CommissionModel

T = datetime  # 简写
_ticket = itertools.count(1000)


def mk_deal(time, symbol, side, entry, volume, *, comm=0.0, swap=0.0, pos=0):
    return Deal(
        ticket=next(_ticket), order=next(_ticket), position_id=pos,
        symbol=symbol, side=side, entry=entry, volume=volume, price=1.1,
        profit=0.0, commission=comm, swap=swap, fee=0.0, time=time,
        comment="", magic=0,
    )


def eur_deals():
    """手算场景的全部 EURUSD 成交（含窗口外一笔、未平仓一笔、出入金一笔）。"""
    return [
        # P0：当日来回（佣金非常规 3/2 每手仍为 30/20，0.1 手），无隔夜
        mk_deal(T(2026, 10, 19, 10, 0), "EURUSD", "buy", "in", 0.1, comm=3.0, pos=100),
        mk_deal(T(2026, 10, 19, 11, 0), "EURUSD", "sell", "out", 0.1, comm=2.0, pos=100),
        # P1
        mk_deal(T(2026, 10, 19, 20, 30), "EURUSD", "buy", "in", 1.0, comm=30.0, pos=101),
        mk_deal(T(2026, 10, 20, 21, 30), "EURUSD", "sell", "out", 1.0, comm=20.0, swap=-14.0, pos=101),
        # P2
        mk_deal(T(2026, 10, 20, 14, 0), "EURUSD", "buy", "in", 1.0, comm=30.0, pos=102),
        mk_deal(T(2026, 10, 21, 3, 0), "EURUSD", "sell", "out", 1.0, comm=20.0, swap=-7.0, pos=102),
        # P3（周三夜 ×3）
        mk_deal(T(2026, 10, 21, 20, 0), "EURUSD", "buy", "in", 1.0, comm=30.0, pos=103),
        mk_deal(T(2026, 10, 22, 6, 0), "EURUSD", "sell", "out", 1.0, comm=20.0, swap=-21.0, pos=103),
        # P4（空头）
        mk_deal(T(2026, 10, 22, 18, 0), "EURUSD", "sell", "in", 1.0, comm=30.0, pos=104),
        mk_deal(T(2026, 10, 23, 21, 30), "EURUSD", "buy", "out", 1.0, comm=20.0, swap=-14.0, pos=104),
        # 未平仓（佣金计入聚合，但不进隔夜统计）
        mk_deal(T(2026, 10, 23, 22, 0), "EURUSD", "buy", "in", 1.0, comm=30.0, pos=105),
        # 出入金流水（entry=None，必须被过滤）
        mk_deal(T(2026, 10, 19, 9, 0), "", "balance", None, 0.0, comm=0.0),
        # 窗口外（周六）——绝不能进统计
        mk_deal(T(2026, 10, 24, 10, 0), "EURUSD", "buy", "in", 1.0, comm=30.0, pos=106),
        mk_deal(T(2026, 10, 24, 11, 0), "EURUSD", "sell", "out", 1.0, comm=20.0, swap=-999.0, pos=106),
    ]


EUR_SNAP = {
    "EURUSD": {
        "name": "EURUSD", "point": 0.00001, "contract_size": 100000.0,
        "swap_long": -7.0, "swap_short": -7.0,
        "swap_mode": 1, "swap_rollover3days": 3,  # MT5 周三
        "currency_profit": "USD", "currency_margin": "USD",
    },
}


class FakeAccount:
    def __init__(self, deals, state):
        self._deals = deals
        self._state = state

    def deals(self, since, to):
        return [d for d in self._deals if since <= d.time <= to]

    def state(self):
        return self._state


class FakeGateway:
    def __init__(self, deals):
        self.account = FakeAccount(deals, AccountState(
            login=12345, server="Exness-MT5Trial", company="Exness",
            currency="USD", leverage=200, balance=1000.0, equity=1000.0,
            margin=0.0, margin_free=1000.0, margin_mode=0, trade_mode=0,
        ))


def load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "calibrate_costs.py"
    spec = importlib.util.spec_from_file_location("calibrate_costs", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


WINDOW = (T(2026, 10, 19, 0, 0), T(2026, 10, 23, 23, 59, 59))


def build(deals=None, snap=None):
    mod = load_script()
    gw = FakeGateway(eur_deals() if deals is None else deals)
    return mod, mod.build_result(
        gw, EUR_SNAP if snap is None else snap, *WINDOW,
        now_utc=T(2026, 10, 24, 12, 0))


def test_commission_aggregates_hand_computed():
    _, res = build()
    c = res["symbols"]["EURUSD"]["commission"]
    # 开仓：Σ佣 3+30×5=153，Σ量 0.1+5.0=5.1 → 30/手；平仓：82/4.1 → 20/手
    assert c["per_lot_in"] == pytest.approx(30.0)
    assert c["per_lot_out"] == pytest.approx(20.0)
    assert c["round_trip_per_lot"] == pytest.approx(50.0)
    assert c["deals_in"] == 6 and c["deals_out"] == 5
    assert c["volume_in"] == pytest.approx(5.1)


def test_summary_counts_filter_balance_and_open_and_out_of_window():
    _, res = build()
    s = res["summary"]
    # 窗口内 12 笔（含出入金 1 笔），交易类 11 笔；平仓 5 个、未平仓 1 个
    assert s["deals_total"] == 12
    assert s["deals_trading"] == 11
    assert s["closed_positions"] == 5
    assert s["open_positions"] == 1
    # 窗口外 -999 的 swap 没有污染统计
    assert res["symbols"]["EURUSD"]["swap"]["long"]["total_swap"] == pytest.approx(-42.0)


def test_swap_rates_and_charge_hour_pinned_to_21():
    _, res = build()
    sw = res["symbols"]["EURUSD"]["swap"]
    assert sw["free_of_swap"] is False
    assert sw["charge_hour_candidates_utc"] == [21]
    assert sw["charge_hour_ambiguous"] is False
    assert sw["long"]["positions_with_swap"] == 3       # P1/P2/P3（P0 当日无 swap）
    assert sw["long"]["total_swap"] == pytest.approx(-42.0)
    assert sw["long"]["per_lot_per_night"] == pytest.approx(-7.0)
    assert sw["short"]["positions_with_swap"] == 1      # P4：-14 / 2 夜 = -7
    assert sw["short"]["per_lot_per_night"] == pytest.approx(-7.0)


def test_snapshot_side_by_side_points_conversion():
    _, res = build()
    snap_block = res["symbols"]["EURUSD"]["swap"]["snapshot"]
    assert snap_block["available"] is True
    assert snap_block["swap_mode"] == 1
    assert snap_block["triple_weekday_py"] == 2          # MT5 周三(3) → py(2)
    assert snap_block["long_per_lot_per_night"] == pytest.approx(-7.0)   # -7 点 × 1e-5 × 1e5
    assert snap_block["short_per_lot_per_night"] == pytest.approx(-7.0)
    assert res["warnings"] == []                          # 实测与快照一致 → 无告警


def test_free_of_swap_account_detected():
    # 两个过夜持仓 swap 全 0 → 免隔夜费；费率 0；计费时刻无从判定
    deals = [
        mk_deal(T(2026, 10, 19, 20, 0), "EURUSD", "buy", "in", 1.0, comm=30.0, pos=1),
        mk_deal(T(2026, 10, 20, 22, 0), "EURUSD", "sell", "out", 1.0, comm=20.0, swap=0.0, pos=1),
        mk_deal(T(2026, 10, 20, 20, 0), "EURUSD", "sell", "in", 1.0, comm=30.0, pos=2),
        mk_deal(T(2026, 10, 21, 22, 0), "EURUSD", "buy", "out", 1.0, comm=20.0, swap=0.0, pos=2),
    ]
    _, res = build(deals=deals)
    sw = res["symbols"]["EURUSD"]["swap"]
    assert sw["free_of_swap"] is True
    assert sw["long"]["per_lot_per_night"] == 0.0
    assert sw["short"]["per_lot_per_night"] == 0.0
    assert sw["charge_hour_candidates_utc"] == []
    assert res["account_free_of_swap"] is True


def test_no_overnight_positions_swap_unknown():
    # 只有当日来回 → 无从判断免隔夜费（None，不猜）
    deals = [
        mk_deal(T(2026, 10, 19, 10, 0), "EURUSD", "buy", "in", 1.0, comm=30.0, pos=1),
        mk_deal(T(2026, 10, 19, 11, 0), "EURUSD", "sell", "out", 1.0, comm=20.0, pos=1),
    ]
    _, res = build(deals=deals)
    sw = res["symbols"]["EURUSD"]["swap"]
    assert sw["free_of_swap"] is None
    assert sw["long"]["per_lot_per_night"] is None
    assert res["account_free_of_swap"] is None


def test_symbol_missing_from_snapshot_warns_not_crashes():
    snap = {"XAUUSD": EUR_SNAP["EURUSD"]}  # 快照里没有 EURUSD
    _, res = build(snap=snap)
    block = res["symbols"]["EURUSD"]["swap"]["snapshot"]
    assert block["available"] is False
    assert any("EURUSD" in w for w in res["warnings"])


def test_nights_between_matches_engine_semantics():
    mod = load_script()
    # (from, to] 语义 + 周三 ×3：与 mt5quant.costs.swap.charge_nights 同源
    assert mod.nights_between(
        T(2026, 10, 21, 20, 0), T(2026, 10, 22, 6, 0), 21, triple_weekday=2) == 3
    assert mod.nights_between(
        T(2026, 10, 21, 21, 0), T(2026, 10, 21, 21, 0), 21) == 0  # 空区间
    assert mod.nights_between(
        T(2026, 10, 21, 20, 59, 59), T(2026, 10, 21, 21, 0), 21) == 1  # to 含端点


def test_write_outputs_and_commission_from_calibrated_roundtrip(tmp_path):
    mod, res = build()
    json_path, md_path = mod.write_outputs(res, str(tmp_path))
    assert Path(json_path) == tmp_path / "costs" / "calibrated.json"
    assert Path(md_path) == tmp_path / "reports" / "costs" / "calibration.md"
    with open(json_path, encoding="utf-8") as f:
        loaded = json.load(f)
    assert loaded["symbols"]["EURUSD"]["commission"]["round_trip_per_lot"] == 50.0
    # 佣金模型可直接从落盘文件构造（T5 主接线用）
    m = CommissionModel.from_calibrated(loaded)
    assert m.commission_per_lot("EURUSD") == 50.0
    md = Path(md_path).read_text(encoding="utf-8")
    for token in ("EURUSD", "50", "-7", "21"):
        assert token in md, f"报告缺 {token!r}"
    assert "2026-10-19" in md and "2026-10-23" in md  # 窗口写进表头
