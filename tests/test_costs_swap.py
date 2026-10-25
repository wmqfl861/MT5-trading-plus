"""SwapModel 手算测试：计费夜计数、周三三倍、恒 ≤0、边界时刻、fail-fast。

日期锚点（2026 年）：10-19 周一、10-20 周二、10-21 周三、10-22 周四、10-23 周五、
10-24 周六、10-26 周一。10-25 前美国仍为夏令时（17:00 NY = 21:00 UTC）。
默认 triple_weekdays={2}(周三)、charge 17:00 America/New_York、周末不计。

T20（H1 / 决策 D10）：周末不收、三倍日按品种、计费时刻随 DST（夏 21:00 / 冬 22:00 GMT）。
"""
from datetime import datetime, timedelta, timezone

import pytest

from mt5quant.costs.swap import SwapModel, SwapRule

D = datetime  # 简写


def make_model(**kw):
    return SwapModel(per_symbol={"EURUSD": SwapRule(long_per_day=-7.0, short_per_day=-2.0, **kw)})


def test_wednesday_night_triple():
    m = make_model()
    fee = m.overnight_fee(
        "EURUSD", "long",
        D(2026, 10, 21, 20, 0),    # 周三 20:00
        D(2026, 10, 22, 20, 59),   # 周四 20:59，只跨周三 21:00 计费夜
    )
    assert fee == pytest.approx(-21.0)  # 3 夜 × (−7.0)，负号钉死


def test_friday_night_single_fx_semantics():
    m = make_model()
    fee = m.overnight_fee(
        "EURUSD", "long",
        D(2026, 10, 23, 20, 0),
        D(2026, 10, 24, 12, 0),
    )
    assert fee == pytest.approx(-7.0)  # 周五夜 ×1（周末两夜已并入周三）


def test_short_side_rate():
    m = make_model()
    fee = m.overnight_fee(
        "EURUSD", "short",
        D(2026, 10, 23, 20, 0),
        D(2026, 10, 24, 12, 0),
    )
    assert fee == pytest.approx(-2.0)  # 1 夜 × (−2.0)


def test_positive_rate_treated_as_cost():
    m = SwapModel(per_symbol={"XAUUSD": SwapRule(long_per_day=1.5, short_per_day=0.5)})
    fee = m.overnight_fee(
        "XAUUSD", "long",
        D(2026, 10, 21, 20, 0),
        D(2026, 10, 22, 20, 59),
    )
    assert fee == pytest.approx(-4.5)  # 正费率也按成本方向取负：−|3×1.5|


def test_btc_no_triple():
    m = SwapModel(per_symbol={"BTCUSD": SwapRule(
        long_per_day=-3.0, short_per_day=-3.0, triple_weekdays=frozenset())})
    assert m.overnight_fee(
        "BTCUSD", "long",
        D(2026, 10, 21, 20, 0),
        D(2026, 10, 22, 20, 59),
    ) == pytest.approx(-3.0)  # 周三 ×1
    assert m.overnight_fee(
        "BTCUSD", "short",
        D(2026, 10, 23, 20, 0),
        D(2026, 10, 24, 12, 0),
    ) == pytest.approx(-3.0)  # 周五 ×1


def test_custom_triple_friday():
    m = SwapModel(per_symbol={"XAUUSD": SwapRule(
        long_per_day=-3.0, short_per_day=-3.0, triple_weekdays=frozenset({4}))})
    assert m.overnight_fee(
        "XAUUSD", "long",
        D(2026, 10, 23, 20, 0),
        D(2026, 10, 24, 12, 0),
    ) == pytest.approx(-9.0)  # 周五 ×3
    assert m.overnight_fee(
        "XAUUSD", "long",
        D(2026, 10, 21, 20, 0),
        D(2026, 10, 22, 20, 59),
    ) == pytest.approx(-3.0)  # 周三 ×1


def test_multi_night_sum():
    m = make_model()
    fee = m.overnight_fee(
        "EURUSD", "long",
        D(2026, 10, 21, 20, 0),
        D(2026, 10, 23, 22, 0),
    )
    # 周三×3 + 周四×1 + 周五×1 = 5 夜
    assert fee == pytest.approx(-35.0)


def test_boundary_charge_hour_inclusive_to_exclusive_from():
    m = make_model()
    # to 恰等于计费时刻 → 计入（from < m ≤ to）
    assert m.overnight_fee(
        "EURUSD", "long",
        D(2026, 10, 21, 20, 59, 59),
        D(2026, 10, 21, 21, 0, 0),
    ) == pytest.approx(-21.0)
    # from 恰等于计费时刻 → 不计（且此后未再跨夜）
    assert m.overnight_fee(
        "EURUSD", "long",
        D(2026, 10, 21, 21, 0, 0),
        D(2026, 10, 22, 20, 59),
    ) == pytest.approx(0.0)


def test_no_night_crossed_returns_zero():
    m = make_model()
    assert m.overnight_fee(
        "EURUSD", "long",
        D(2026, 10, 21, 22, 0),
        D(2026, 10, 22, 20, 0),
    ) == 0.0  # 0 ≤ 0，仍满足恒 ≤0


def test_custom_charge_moment_utc_midnight():
    # 自定义计费时刻：UTC 时区本地 00:00 = UTC 午夜
    m = SwapModel(per_symbol={"EURUSD": SwapRule(
        -7.0, -2.0, charge_local="00:00", charge_tz="UTC")})
    fee = m.overnight_fee(
        "EURUSD", "long",
        D(2026, 10, 26, 23, 0),   # 周一 23:00
        D(2026, 10, 27, 1, 0),    # 周二 01:00，跨周二 00:00 计费
    )
    assert fee == pytest.approx(-7.0)  # 周二非三倍日 → ×1


def test_aware_datetimes_converted_to_naive_utc():
    m = make_model()
    fee = m.overnight_fee(
        "EURUSD", "long",
        D(2026, 10, 21, 20, 0, tzinfo=timezone.utc),
        D(2026, 10, 22, 20, 59, tzinfo=timezone.utc),
    )
    assert fee == pytest.approx(-21.0)


def test_always_non_positive_across_windows():
    m = make_model()
    start = D(2026, 10, 19, 0, 0)
    for days in (0, 1, 2, 5, 10):
        for side in ("long", "short"):
            fee = m.overnight_fee("EURUSD", side, start, start + timedelta(days=days, hours=7))
            assert fee <= 0


def test_invalid_side_symbol_and_order():
    m = make_model()
    with pytest.raises(ValueError):
        m.overnight_fee("EURUSD", "buy",
                        D(2026, 10, 21, 20), D(2026, 10, 22, 20))
    with pytest.raises(KeyError):
        m.overnight_fee("XAUUSD", "long",
                        D(2026, 10, 21, 20), D(2026, 10, 22, 20))
    with pytest.raises(ValueError):
        m.overnight_fee("EURUSD", "long",
                        D(2026, 10, 22, 20), D(2026, 10, 21, 20))


def test_swap_rule_validation():
    with pytest.raises(ValueError, match="charge_local"):
        SwapRule(-1.0, -1.0, charge_local="24:00")
    with pytest.raises(ValueError, match="charge_tz"):
        SwapRule(-1.0, -1.0, charge_tz="Mars/Olympus")
    with pytest.raises(ValueError):
        SwapRule(-1.0, -1.0, triple_weekdays={7})  # 星期须 0..6（Monday=0）
    with pytest.raises(ValueError):
        SwapRule(-1.0, -1.0, charge_weekdays={9})


# ---- T20（H1）：周末不计 + DST 计费时刻 + 整周口径 ----

from mt5quant.costs.swap import (  # noqa: E402
    SWAP_MODE_CURRENCY_DEPOSIT, SWAP_MODE_CURRENCY_MARGIN,
    SWAP_MODE_CURRENCY_SYMBOL, SWAP_MODE_DISABLED, SWAP_MODE_INTEREST_CURRENT,
    SWAP_MODE_POINTS, SWAP_MODE_REOPEN_CURRENT, charge_nights,
    mt5_to_py_weekday, per_lot_per_night,
)

RULE = SwapRule(long_per_day=-7.0, short_per_day=-7.0)   # 默认：17:00 NY，周三×3，周末不计


def test_friday_to_monday_is_one_night():
    # 周五 20:00 → 周一 20:00（夏令时周）：只有周五夜，周六周日不计
    assert charge_nights(D(2026, 10, 23, 20, 0), D(2026, 10, 26, 20, 0), RULE) == 1


def test_full_week_is_seven_nights_with_wednesday_split():
    # 周一 20:00 → 次周一 20:00：周一..周五各夜 = 1+1+3+1+1 = 7（周末 0）
    assert charge_nights(D(2026, 10, 19, 20, 0), D(2026, 10, 26, 20, 0), RULE) == 7
    # 拆分断言：每段单独数，周三段 ×3
    assert charge_nights(D(2026, 10, 19, 20, 0), D(2026, 10, 20, 20, 0), RULE) == 1  # 周一夜
    assert charge_nights(D(2026, 10, 20, 20, 0), D(2026, 10, 21, 20, 0), RULE) == 1  # 周二夜
    assert charge_nights(D(2026, 10, 21, 20, 0), D(2026, 10, 22, 20, 0), RULE) == 3  # 周三夜 ×3
    assert charge_nights(D(2026, 10, 22, 20, 0), D(2026, 10, 23, 20, 0), RULE) == 1  # 周四夜
    assert charge_nights(D(2026, 10, 23, 20, 0), D(2026, 10, 24, 12, 0), RULE) == 1  # 周五夜
    assert charge_nights(D(2026, 10, 24, 12, 0), D(2026, 10, 26, 20, 0), RULE) == 0  # 周末 0


def test_weekend_excluded_even_if_weekday_listed_as_triple():
    # 即便三倍日错配到周六，charge_weekdays 默认仍把周末排除——两道闸独立生效
    r = SwapRule(-7.0, -7.0, triple_weekdays=frozenset({5, 6}))
    assert charge_nights(D(2026, 10, 23, 20, 0), D(2026, 10, 26, 20, 0), r) == 1


def test_charge_moment_dst_summer_21_utc():
    # 2026-07-15（EDT，周三）：计费时刻 = 21:00 UTC（含端点）且当日三倍 → ×3
    assert charge_nights(D(2026, 7, 15, 20, 59), D(2026, 7, 15, 21, 0), RULE) == 3
    assert charge_nights(D(2026, 7, 15, 21, 0), D(2026, 7, 15, 21, 30), RULE) == 0
    assert charge_nights(D(2026, 7, 15, 21, 30), D(2026, 7, 15, 22, 0), RULE) == 0
    # 周二（07-14，EDT）：同时刻 ×1——钉死 21:00 UTC 计费而 22:00 不计
    assert charge_nights(D(2026, 7, 14, 20, 59), D(2026, 7, 14, 21, 0), RULE) == 1
    assert charge_nights(D(2026, 7, 14, 21, 30), D(2026, 7, 14, 22, 0), RULE) == 0


def test_charge_moment_dst_winter_22_utc():
    # 2026-12-15（EST）：计费时刻 = 22:00 UTC。21:00–21:59 不再是计费时刻
    assert charge_nights(D(2026, 12, 15, 21, 59), D(2026, 12, 15, 22, 0), RULE) == 1
    assert charge_nights(D(2026, 12, 15, 21, 0), D(2026, 12, 15, 21, 30), RULE) == 0
    assert charge_nights(D(2026, 12, 15, 22, 0), D(2026, 12, 15, 22, 30), RULE) == 0


def test_dst_transition_week_counts_correctly():
    # 10-25（美国仍 EDT）周日不计；10-26 周一 21:00 UTC 计费
    assert charge_nights(D(2026, 10, 25, 12, 0), D(2026, 10, 26, 20, 0), RULE) == 0
    assert charge_nights(D(2026, 10, 25, 12, 0), D(2026, 10, 26, 21, 0), RULE) == 1
    # 11 月（EST）：周五 11-06 的计费时刻 = 22:00 UTC
    assert charge_nights(D(2026, 11, 6, 21, 59), D(2026, 11, 6, 22, 0), RULE) == 1


# ---- T5（R4）：MT5 星期枚举换算 / from_snapshot ----


def test_mt5_to_py_weekday_all_seven_days():
    # MT5：周日=0…周六=6（DAY_OF_WEEK_*）；Python weekday：周一=0…周日=6
    assert [mt5_to_py_weekday(d) for d in range(7)] == [6, 0, 1, 2, 3, 4, 5]


def test_mt5_to_py_weekday_rejects_out_of_range():
    with pytest.raises(ValueError, match="0..6"):
        mt5_to_py_weekday(7)
    with pytest.raises(ValueError, match="0..6"):
        mt5_to_py_weekday(-1)


def test_swap_mode_constants_match_mt5_package():
    # 数值与真包核对（本机 MetaTrader5 包打印，2026-10-24）：写死防漂移
    assert SWAP_MODE_DISABLED == 0
    assert SWAP_MODE_POINTS == 1
    assert SWAP_MODE_CURRENCY_SYMBOL == 2
    assert SWAP_MODE_CURRENCY_MARGIN == 3
    assert SWAP_MODE_CURRENCY_DEPOSIT == 4
    assert SWAP_MODE_INTEREST_CURRENT == 5
    assert SWAP_MODE_REOPEN_CURRENT == 7


def test_charge_nights_public_triple_weighting():
    # (from, to] 内跨周三 21:00 UTC（夏令时）计费夜 ×3；周四 ×1；不给三倍日一律 ×1
    assert charge_nights(
        D(2026, 10, 21, 20, 0), D(2026, 10, 22, 20, 59),
        SwapRule(-7.0, -7.0, triple_weekdays=frozenset({2})),
    ) == 3
    assert charge_nights(
        D(2026, 10, 22, 20, 0), D(2026, 10, 23, 20, 59),
        SwapRule(-7.0, -7.0, triple_weekdays=frozenset({2})),
    ) == 1
    assert charge_nights(
        D(2026, 10, 21, 20, 0), D(2026, 10, 22, 20, 59),
        SwapRule(-7.0, -7.0, triple_weekdays=frozenset()),
    ) == 1


def _snap_entry(**kw):
    """EURUSD 点数模式快照条目（contract_size=100000，contract≠1 防单位混用）。"""
    e = {
        "name": "EURUSD", "point": 0.00001, "contract_size": 100000.0,
        "swap_long": -7.0, "swap_short": 2.0,
        "swap_mode": SWAP_MODE_POINTS, "swap_rollover3days": 3,  # MT5 周三
        "currency_profit": "USD", "currency_margin": "USD",
    }
    e.update(kw)
    return e


def test_per_lot_per_night_points_mode_hand_computed():
    # 点数模式：每手每夜 = swap × point × contract_size（账户货币）
    # -7.0 点 × 0.00001 × 100000 = -7.0 USD/手/夜；空头 2.0 点 → +2.0
    lng, sht = per_lot_per_night(_snap_entry())
    assert lng == pytest.approx(-7.0)
    assert sht == pytest.approx(2.0)


def test_per_lot_per_night_deposit_mode_direct_amount():
    # 存款货币模式：swap 即每手金额（账户货币），与 point/contract 无关
    e = _snap_entry(swap_mode=SWAP_MODE_CURRENCY_DEPOSIT,
                    swap_long=-490.8, swap_short=0.0)
    lng, sht = per_lot_per_night(e)
    assert lng == pytest.approx(-490.8)
    assert sht == pytest.approx(0.0)


def test_per_lot_per_night_margin_currency_mode_requires_account_currency():
    # 保证金货币模式：currency_margin=USD 与账户一致 → 直接金额
    lng, _ = per_lot_per_night(_snap_entry(
        swap_mode=SWAP_MODE_CURRENCY_MARGIN, swap_long=-3.5,
        currency_margin="USD"))
    assert lng == pytest.approx(-3.5)
    # currency_margin≠账户货币（XAUUSD 的 XAU）→ 不猜汇率，fail-fast
    with pytest.raises(NotImplementedError, match="保证金货币"):
        per_lot_per_night(_snap_entry(
            swap_mode=SWAP_MODE_CURRENCY_MARGIN, currency_margin="XAU"))


def test_per_lot_per_night_symbol_currency_mode_requires_profit_usd():
    # 品种货币模式：MQL5 定义为基础货币计，快照无 currency_base，
    # 用计价货币（profit）做一致性检查；≠账户货币 → 不猜
    lng, _ = per_lot_per_night(_snap_entry(
        swap_mode=SWAP_MODE_CURRENCY_SYMBOL, swap_long=-2.5,
        currency_profit="USD"))
    assert lng == pytest.approx(-2.5)
    with pytest.raises(NotImplementedError, match="品种货币"):
        per_lot_per_night(_snap_entry(
            swap_mode=SWAP_MODE_CURRENCY_SYMBOL, currency_profit="EUR"))


def test_per_lot_per_night_disabled_is_zero():
    lng, sht = per_lot_per_night(_snap_entry(
        swap_mode=SWAP_MODE_DISABLED, swap_long=-99.0, swap_short=99.0))
    assert lng == 0.0 and sht == 0.0


def test_per_lot_per_night_interest_and_reopen_raise():
    with pytest.raises(NotImplementedError, match="利率"):
        per_lot_per_night(_snap_entry(swap_mode=SWAP_MODE_INTEREST_CURRENT))
    with pytest.raises(NotImplementedError, match="利率"):
        per_lot_per_night(_snap_entry(swap_mode=6))  # INTEREST_OPEN
    with pytest.raises(NotImplementedError, match="重开仓"):
        per_lot_per_night(_snap_entry(swap_mode=SWAP_MODE_REOPEN_CURRENT))
    with pytest.raises(NotImplementedError, match="重开仓"):
        per_lot_per_night(_snap_entry(swap_mode=8))  # REOPEN_BID
    with pytest.raises(NotImplementedError, match="未知"):
        per_lot_per_night(_snap_entry(swap_mode=99))


def test_per_lot_per_night_missing_swap_mode_raises():
    e = _snap_entry()
    del e["swap_mode"]
    with pytest.raises(KeyError, match="swap_mode"):
        per_lot_per_night(e)
    e = _snap_entry()
    del e["swap_rollover3days"]
    with pytest.raises(KeyError, match="swap_rollover3days"):
        per_lot_per_night(e)


def test_from_snapshot_points_mode_rule_and_end_to_end_fee():
    snap = {"EURUSD": _snap_entry()}
    m = SwapModel.from_snapshot(snap, ["EURUSD"])
    rule_ = m.rule("EURUSD")
    assert rule_.long_per_day == pytest.approx(-7.0)   # -7 点 × 1e-5 × 1e5
    assert rule_.short_per_day == pytest.approx(2.0)
    assert rule_.triple_weekdays == frozenset({2})      # MT5 周三(3) → py 周三(2)
    assert rule_.charge_local == "17:00"                # v2：时刻由规则自带
    assert rule_.charge_tz == "America/New_York"
    assert rule_.charge_weekdays == frozenset({0, 1, 2, 3, 4})
    # 端到端手算：周三夜（三倍）多头 -abs(3 × -7.0) = -21.0
    assert m.overnight_fee(
        "EURUSD", "long",
        D(2026, 10, 21, 20, 0), D(2026, 10, 22, 20, 59),
    ) == pytest.approx(-21.0)


def test_from_snapshot_rollover_friday_maps_to_py_friday():
    # MT5 周五=5（指数/加密货币组的官方三倍日）→ Python 周五=4
    snap = {"USTEC": _snap_entry(name="USTEC", swap_rollover3days=5)}
    m = SwapModel.from_snapshot(snap, ["USTEC"])
    assert m.rule("USTEC").triple_weekdays == frozenset({4})


def test_from_snapshot_rollover_sunday_maps_to_py_sunday():
    snap = {"EURUSD": _snap_entry(swap_rollover3days=0)}  # MT5 周日=0
    m = SwapModel.from_snapshot(snap, ["EURUSD"])
    assert m.rule("EURUSD").triple_weekdays == frozenset({6})  # py 周日=6


def test_from_snapshot_disabled_symbol_zero_fee():
    snap = {"XAUUSD": _snap_entry(
        name="XAUUSD", swap_mode=SWAP_MODE_DISABLED, swap_rollover3days=3)}
    m = SwapModel.from_snapshot(snap, ["XAUUSD"])
    r = m.rule("XAUUSD")
    assert r.long_per_day == 0.0 and r.short_per_day == 0.0
    assert m.overnight_fee(
        "XAUUSD", "long",
        D(2026, 10, 21, 20, 0), D(2026, 10, 22, 20, 59),
    ) == 0.0


def test_from_snapshot_missing_symbol_raises_keyerror():
    snap = {"EURUSD": _snap_entry()}
    with pytest.raises(KeyError, match="XAUUSD"):
        SwapModel.from_snapshot(snap, ["XAUUSD"])


def test_from_snapshot_old_snapshot_without_swap_mode_fails_fast():
    # 旧快照（R4 之前）没有 swap_mode/swap_rollover3days → 报错要求重抓，
    # 绝不静默按 0 计隔夜费
    e = _snap_entry()
    del e["swap_mode"]
    del e["swap_rollover3days"]
    with pytest.raises(KeyError, match="snapshot_specs"):
        SwapModel.from_snapshot({"EURUSD": e}, ["EURUSD"])


def test_from_snapshot_interest_mode_raises_for_whole_model():
    snap = {"EURUSD": _snap_entry(),
            "USTEC": _snap_entry(name="USTEC", swap_mode=SWAP_MODE_INTEREST_CURRENT)}
    with pytest.raises(NotImplementedError, match="USTEC"):
        SwapModel.from_snapshot(snap, ["EURUSD", "USTEC"])
