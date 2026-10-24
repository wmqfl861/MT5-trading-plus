"""SwapModel 手算测试：计费夜计数、周三三倍、恒 ≤0、边界时刻、fail-fast。

日期锚点（2026 年）：10-21 周三、10-22 周四、10-23 周五、10-24 周六、
10-26 周一、10-27 周二。默认 charge_hour_utc=21、triple_weekdays={2}(周三)。
"""
from datetime import datetime, timedelta, timezone

import pytest

from mt5quant.costs.swap import SwapModel, SwapRule


def make_model(**kw):
    return SwapModel(per_symbol={"EURUSD": SwapRule(long_per_day=-7.0, short_per_day=-2.0, **kw)})


def test_wednesday_night_triple():
    m = make_model()
    fee = m.overnight_fee(
        "EURUSD", "long",
        datetime(2026, 10, 21, 20, 0),    # 周三 20:00
        datetime(2026, 10, 22, 20, 59),   # 周四 20:59，只跨周三 21:00 计费夜
    )
    assert fee == pytest.approx(-21.0)  # 3 夜 × (−7.0)，负号钉死


def test_friday_night_single_fx_semantics():
    m = make_model()
    fee = m.overnight_fee(
        "EURUSD", "long",
        datetime(2026, 10, 23, 20, 0),
        datetime(2026, 10, 24, 12, 0),
    )
    assert fee == pytest.approx(-7.0)  # 周五夜 ×1（周末两夜已并入周三）


def test_short_side_rate():
    m = make_model()
    fee = m.overnight_fee(
        "EURUSD", "short",
        datetime(2026, 10, 23, 20, 0),
        datetime(2026, 10, 24, 12, 0),
    )
    assert fee == pytest.approx(-2.0)  # 1 夜 × (−2.0)


def test_positive_rate_treated_as_cost():
    m = SwapModel(per_symbol={"XAUUSD": SwapRule(long_per_day=1.5, short_per_day=0.5)})
    fee = m.overnight_fee(
        "XAUUSD", "long",
        datetime(2026, 10, 21, 20, 0),
        datetime(2026, 10, 22, 20, 59),
    )
    assert fee == pytest.approx(-4.5)  # 正费率也按成本方向取负：−|3×1.5|


def test_btc_no_triple():
    m = SwapModel(per_symbol={"BTCUSD": SwapRule(
        long_per_day=-3.0, short_per_day=-3.0, triple_weekdays=frozenset())})
    assert m.overnight_fee(
        "BTCUSD", "long",
        datetime(2026, 10, 21, 20, 0),
        datetime(2026, 10, 22, 20, 59),
    ) == pytest.approx(-3.0)  # 周三 ×1
    assert m.overnight_fee(
        "BTCUSD", "short",
        datetime(2026, 10, 23, 20, 0),
        datetime(2026, 10, 24, 12, 0),
    ) == pytest.approx(-3.0)  # 周五 ×1


def test_custom_triple_friday():
    m = SwapModel(per_symbol={"XAUUSD": SwapRule(
        long_per_day=-3.0, short_per_day=-3.0, triple_weekdays=frozenset({4}))})
    assert m.overnight_fee(
        "XAUUSD", "long",
        datetime(2026, 10, 23, 20, 0),
        datetime(2026, 10, 24, 12, 0),
    ) == pytest.approx(-9.0)  # 周五 ×3
    assert m.overnight_fee(
        "XAUUSD", "long",
        datetime(2026, 10, 21, 20, 0),
        datetime(2026, 10, 22, 20, 59),
    ) == pytest.approx(-3.0)  # 周三 ×1


def test_multi_night_sum():
    m = make_model()
    fee = m.overnight_fee(
        "EURUSD", "long",
        datetime(2026, 10, 21, 20, 0),
        datetime(2026, 10, 23, 22, 0),
    )
    # 周三×3 + 周四×1 + 周五×1 = 5 夜
    assert fee == pytest.approx(-35.0)


def test_boundary_charge_hour_inclusive_to_exclusive_from():
    m = make_model()
    # to 恰等于计费时刻 → 计入（from < m ≤ to）
    assert m.overnight_fee(
        "EURUSD", "long",
        datetime(2026, 10, 21, 20, 59, 59),
        datetime(2026, 10, 21, 21, 0, 0),
    ) == pytest.approx(-21.0)
    # from 恰等于计费时刻 → 不计（且此后未再跨夜）
    assert m.overnight_fee(
        "EURUSD", "long",
        datetime(2026, 10, 21, 21, 0, 0),
        datetime(2026, 10, 22, 20, 59),
    ) == pytest.approx(0.0)


def test_no_night_crossed_returns_zero():
    m = make_model()
    assert m.overnight_fee(
        "EURUSD", "long",
        datetime(2026, 10, 21, 22, 0),
        datetime(2026, 10, 22, 20, 0),
    ) == 0.0  # 0 ≤ 0，仍满足恒 ≤0


def test_custom_charge_hour():
    m = SwapModel(per_symbol={"EURUSD": SwapRule(-7.0, -2.0, charge_hour_utc=0)})
    fee = m.overnight_fee(
        "EURUSD", "long",
        datetime(2026, 10, 26, 23, 0),   # 周一 23:00
        datetime(2026, 10, 27, 1, 0),    # 周二 01:00，跨周二 00:00 计费
    )
    assert fee == pytest.approx(-7.0)  # 周二非三倍日 → ×1


def test_aware_datetimes_converted_to_naive_utc():
    m = make_model()
    fee = m.overnight_fee(
        "EURUSD", "long",
        datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 22, 20, 59, tzinfo=timezone.utc),
    )
    assert fee == pytest.approx(-21.0)


def test_always_non_positive_across_windows():
    m = make_model()
    start = datetime(2026, 10, 19, 0, 0)
    for days in (0, 1, 2, 5, 10):
        for side in ("long", "short"):
            fee = m.overnight_fee("EURUSD", side, start, start + timedelta(days=days, hours=7))
            assert fee <= 0


def test_invalid_side_symbol_and_order():
    m = make_model()
    with pytest.raises(ValueError):
        m.overnight_fee("EURUSD", "buy",
                        datetime(2026, 10, 21, 20), datetime(2026, 10, 22, 20))
    with pytest.raises(KeyError):
        m.overnight_fee("XAUUSD", "long",
                        datetime(2026, 10, 21, 20), datetime(2026, 10, 22, 20))
    with pytest.raises(ValueError):
        m.overnight_fee("EURUSD", "long",
                        datetime(2026, 10, 22, 20), datetime(2026, 10, 21, 20))


def test_swap_rule_validation():
    with pytest.raises(ValueError):
        SwapRule(-1.0, -1.0, charge_hour_utc=24)
    with pytest.raises(ValueError):
        SwapRule(-1.0, -1.0, triple_weekdays={7})  # 星期须 0..6（Monday=0）
