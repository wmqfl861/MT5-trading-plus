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


# ---- T82：点数模式相对价格基准（引擎 A 隔夜费 1986 反例修复，2026-10-28） ----
#
# 背景（T65 §5.4 / 计划 2026-10-28-t82）：点数模式费率换算的绝对 USD/手/夜
# 校准于快照时代价格（USTEC≈25097 → −2.5bp/夜）；套到 1986 年 131 价位同一
# 绝对额 = −4.8%/夜（实测 FRED 段 6 笔后权益爆掉）。修复镜像 T66 事件路径
# （event-mode-calibration §1.2）：rel = |per_lot_per_night| ÷ (ref_price ×
# contract)，计费 fee = rel × price × contract（price=出场侧 bar 价）。

# USTEC 快照条目（与 data/specs/latest.json 实测同构）：点数模式，
# -633.8 点 × point 0.01 × contract 1 = -6.338 USD/手/夜
USTEC_SNAP = {
    "name": "USTEC", "point": 0.01, "contract_size": 1.0,
    "swap_long": -633.8, "swap_short": 0.0,
    "swap_mode": SWAP_MODE_POINTS, "swap_rollover3days": 5,   # MT5 周五 → py 周五
    "currency_profit": "USD", "currency_margin": "USD",
}
USTEC_REF = 25097.0   # 快照时代参考价（费率校准价位；rel 分母）
EARLY_1986 = 131.0    # 1986 年 USTEC（fred 纳指 100）价位
PER_LOT_NIGHT = 633.8 * 0.01 * 1.0   # |per_lot_per_night| = 6.338 USD/手/夜


def _ustec_model(ref=USTEC_REF):
    return SwapModel.from_snapshot(
        {"USTEC": dict(USTEC_SNAP)}, ["USTEC"],
        ref_prices={"USTEC": ref} if ref is not None else None)


def test_t82_1986_counterexample_relative_fee():
    """1986 反例：131 价位 5 夜（周三 20:00→周一 20:00，跨周五三倍 = 5 夜），
    新口径每夜 ≈ −2.5bp 的名义额；旧口径同窗口是 −4.8%/夜（金额差约
    ref/price ≈ 190 倍——T65 实测爆仓的量级）。"""
    m = _ustec_model()
    fee = m.overnight_fee("USTEC", "long",
                          D(2026, 10, 21, 20, 0), D(2026, 10, 26, 20, 0),
                          price=EARLY_1986)
    assert fee == pytest.approx(
        -PER_LOT_NIGHT * (EARLY_1986 / USTEC_REF) * 5.0, rel=1e-9)
    per_night = fee / 5.0
    # 相对口径：每夜占名义价值 6.338/25097 ≈ 2.53bp（≈−2.5bp/夜）
    assert abs(per_night) / (EARLY_1986 * 1.0) == pytest.approx(
        PER_LOT_NIGHT / USTEC_REF, rel=1e-9)
    # 金额差实证：旧绝对口径每夜 6.338 → 差约 190 倍（25097/131 ≈ 191.6）
    ratio = PER_LOT_NIGHT / abs(per_night)
    assert 185.0 < ratio < 200.0


def test_t82_recent_price_matches_old_absolute_within_5pct():
    """近期价位回归：price=25,000 与 ref=25,097 只差费率缩放 25000/25097
    ≈ 0.996——新旧口径每夜金额比值 ≈1（容差 5%，快照时代自洽）。"""
    m = _ustec_model()
    fee_new = m.overnight_fee("USTEC", "long",
                              D(2026, 10, 21, 20, 0), D(2026, 10, 26, 20, 0),
                              price=25_000.0)
    fee_old = PER_LOT_NIGHT * 5.0            # 旧绝对口径 5 夜
    assert abs(fee_new) / fee_old == pytest.approx(25_000.0 / USTEC_REF, rel=1e-9)
    assert abs(abs(fee_new) / fee_old - 1.0) < 0.05


def test_t82_fee_scales_linearly_with_price():
    """同一持仓窗口，fee ∝ price（相对口径的定义性质）。"""
    m = _ustec_model()
    f1 = m.overnight_fee("USTEC", "long",
                         D(2026, 10, 21, 20, 0), D(2026, 10, 26, 20, 0),
                         price=100.0)
    f2 = m.overnight_fee("USTEC", "long",
                         D(2026, 10, 21, 20, 0), D(2026, 10, 26, 20, 0),
                         price=200.0)
    assert f2 == pytest.approx(2.0 * f1, rel=1e-12)


def test_t82_currency_mode_ignores_price():
    """货币模式（2/3/4）费率本身是绝对金额语义——带 ref 构造也不相对化，
    price 传入被忽略（换算假设不变）。"""
    snap = {"XAUUSD": _snap_entry(
        name="XAUUSD", swap_mode=SWAP_MODE_CURRENCY_DEPOSIT,
        swap_long=-4.0, swap_short=-1.0, swap_rollover3days=3)}
    m = SwapModel.from_snapshot(snap, ["XAUUSD"],
                                ref_prices={"XAUUSD": 2400.0})
    r = m.rule("XAUUSD")
    assert r.ref_price is None and r.contract_size is None   # 未相对化
    w = (D(2026, 10, 21, 20, 0), D(2026, 10, 22, 20, 59))    # 周三 ×3
    assert m.overnight_fee("XAUUSD", "long", *w) == pytest.approx(-12.0)
    assert m.overnight_fee("XAUUSD", "long", *w, price=131.0) == pytest.approx(-12.0)


def test_t82_disabled_mode_zero_with_or_without_price():
    snap = {"XAUUSD": _snap_entry(name="XAUUSD", swap_mode=SWAP_MODE_DISABLED,
                                  swap_rollover3days=3)}
    m = SwapModel.from_snapshot(snap, ["XAUUSD"], ref_prices={"XAUUSD": 2400.0})
    w = (D(2026, 10, 21, 20, 0), D(2026, 10, 26, 20, 0))
    assert m.overnight_fee("XAUUSD", "long", *w) == 0.0
    assert m.overnight_fee("XAUUSD", "long", *w, price=131.0) == 0.0


def test_t82_points_mode_missing_price_fails_fast():
    """缺 price 的点数模式（相对口径）→ fail-fast（不静默按绝对额计费——
    防旧口径复活）。"""
    m = _ustec_model()
    with pytest.raises(ValueError, match="price"):
        m.overnight_fee("USTEC", "long",
                        D(2026, 10, 21, 20, 0), D(2026, 10, 26, 20, 0))


def test_t82_points_mode_invalid_price_rejected():
    m = _ustec_model()
    w = (D(2026, 10, 21, 20, 0), D(2026, 10, 26, 20, 0))
    with pytest.raises(ValueError, match="有限正数"):
        m.overnight_fee("USTEC", "long", *w, price=0.0)
    with pytest.raises(ValueError, match="有限正数"):
        m.overnight_fee("USTEC", "long", *w, price=-131.0)
    with pytest.raises(ValueError, match="有限正数"):
        m.overnight_fee("USTEC", "long", *w, price=float("nan"))


def test_t82_swap_rule_ref_validation():
    """ref_price 与 contract_size 成对、有限正数；只给其一 → 拒绝。"""
    with pytest.raises(ValueError, match="成对"):
        SwapRule(-7.0, -2.0, ref_price=25097.0)
    with pytest.raises(ValueError, match="成对"):
        SwapRule(-7.0, -2.0, contract_size=1.0)
    with pytest.raises(ValueError, match="ref_price"):
        SwapRule(-7.0, -2.0, ref_price=0.0, contract_size=1.0)
    with pytest.raises(ValueError, match="contract_size"):
        SwapRule(-7.0, -2.0, ref_price=25097.0, contract_size=-1.0)
    # 成对且正 → 合法（相对口径标记 = ref_price 非 None）
    r = SwapRule(-7.0, -2.0, ref_price=25097.0, contract_size=1.0)
    assert r.ref_price == 25097.0 and r.contract_size == 1.0


def test_t82_from_snapshot_bad_ref_rejected():
    with pytest.raises(ValueError, match="有限正数"):
        _ustec_model(ref=0.0)
    with pytest.raises(ValueError, match="有限正数"):
        _ustec_model(ref=float("inf"))


def test_t82_from_snapshot_without_ref_keeps_absolute_legacy():
    """不给 ref_prices：点数模式保持旧绝对口径（预筛/关卡装配路径的现状，
    引擎 CLI 主路径恒传参考价——见 run_backtest.build_costs 的 T82 注记）。"""
    m = _ustec_model(ref=None)
    r = m.rule("USTEC")
    assert r.ref_price is None and r.contract_size is None
    # 旧口径：131 价位 5 夜仍是绝对额 6.338×5（不带 price 也不抛）
    assert m.overnight_fee("USTEC", "long",
                           D(2026, 10, 21, 20, 0), D(2026, 10, 26, 20, 0)
                           ) == pytest.approx(-PER_LOT_NIGHT * 5.0)


def test_t82_relative_rule_holds_ref_for_identity():
    """规则持有 ref_price/contract_size（版本串 relref 与计费同一来源）。"""
    m = _ustec_model()
    r = m.rule("USTEC")
    assert r.ref_price == USTEC_REF
    assert r.contract_size == 1.0
    assert r.long_per_day == pytest.approx(-PER_LOT_NIGHT)   # 绝对额原值保留


# ---- H-13：有限数/范围校验 + mode=2 基础货币明确拒绝（2026-10-27） ----
#
# 审计 H-13：若干成本参数仅比较 <0，NaN/Inf 可进入模型（NaN 与任何数比较为
# False）；swap_mode=2 按 MQL5 定义是**品种基础货币**计，旧代码用 currency_profit
# 代理——EURUSD（base=EUR, profit=USD）的 EUR 金额会被当 USD 用。修复：
# 构建时统一有限数校验；快照带 currency_base 时按真定义检查，基础货币 ≠ 账户
# 货币一律 NotImplementedError（未实现换汇不代算）。

def test_h13_swap_rule_nonfinite_rates_rejected():
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="long_per_day"):
            SwapRule(long_per_day=bad, short_per_day=-2.0)
        with pytest.raises(ValueError, match="short_per_day"):
            SwapRule(long_per_day=-7.0, short_per_day=bad)


def test_h13_per_lot_per_night_nonfinite_snapshot_rejected():
    with pytest.raises(ValueError, match="swap_long"):
        per_lot_per_night(_snap_entry(swap_long=float("nan")))
    with pytest.raises(ValueError, match="swap_short"):
        per_lot_per_night(_snap_entry(swap_short=float("inf")))
    # 点数模式的换算因子也须有限正数（0/NaN × 点值会静默把隔夜费算成 0/NaN）
    for bad in (0.0, -0.00001, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="point"):
            per_lot_per_night(_snap_entry(point=bad))
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="contract_size"):
            per_lot_per_night(_snap_entry(contract_size=bad))


def test_h13_symbol_mode_uses_currency_base_when_present():
    """mode=2 真定义是品种基础货币计：快照带 currency_base 时按真值检查。

    EURUSD：currency_base=EUR ≠ 账户 USD——即使 currency_profit=USD（旧代理
    恰好放行、EUR 金额被当 USD 用）也必须明确拒绝。"""
    with pytest.raises(NotImplementedError, match="基础货币"):
        per_lot_per_night(_snap_entry(
            swap_mode=SWAP_MODE_CURRENCY_SYMBOL, swap_long=-2.5,
            currency_base="EUR", currency_profit="USD"))
    # 基础货币 = 账户货币（如 USDJPY 的 base=USD）→ 金额确为 USD，可用
    lng, sht = per_lot_per_night(_snap_entry(
        name="USDJPY", swap_mode=SWAP_MODE_CURRENCY_SYMBOL,
        swap_long=-2.5, swap_short=1.0,
        currency_base="USD", currency_profit="JPY"))
    assert lng == pytest.approx(-2.5)
    assert sht == pytest.approx(1.0)


def test_h13_symbol_mode_legacy_proxy_rejects_non_account_profit():
    """旧快照（无 currency_base）沿用计价货币代理：≠ 账户货币仍明确拒绝。

    EURUSD 条目误标 currency_profit=EUR（≠USD）→ NotImplementedError，
    不猜汇率。"""
    with pytest.raises(NotImplementedError, match="品种货币"):
        per_lot_per_night(_snap_entry(
            swap_mode=SWAP_MODE_CURRENCY_SYMBOL, currency_profit="EUR"))
