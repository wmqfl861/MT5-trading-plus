from mt5gw.errors import (
    RETCode,
    TradeRejectedError,
    LiveTradingBlockedError,
    describe_retcode,
)


def test_known_retcode_has_readable_chinese_message():
    msg = describe_retcode(RETCode.MARKET_CLOSED)
    assert "市场关闭" in msg


def test_every_documented_retcode_maps_to_nonempty_text():
    for code in [
        RETCode.REQUOTE,
        RETCode.REJECT,
        RETCode.CANCEL,
        RETCode.PLACED,
        RETCode.DONE,
        RETCode.INVALID_VOLUME,
        RETCode.INVALID_STOPS,
        RETCode.TRADE_DISABLED,
        RETCode.MARKET_CLOSED,
        RETCode.NO_MONEY,
        RETCode.INVALID_FILL,
    ]:
        assert describe_retcode(code)  # 非空


def test_unknown_retcode_message_contains_the_code():
    msg = describe_retcode(99999)
    assert "99999" in msg


def test_trade_rejected_error_str_carries_retcode_and_comment():
    err = TradeRejectedError(10018, "markets closed for weekend")
    text = str(err)
    assert "10018" in text
    assert "市场关闭" in text  # retcode 可读文案
    assert "markets closed for weekend" in text


def test_error_hierarchy():
    from mt5gw.errors import GatewayError, NotConnectedError, DataUnavailableError

    assert issubclass(TradeRejectedError, GatewayError)
    assert issubclass(NotConnectedError, GatewayError)
    assert issubclass(DataUnavailableError, GatewayError)
    assert issubclass(LiveTradingBlockedError, GatewayError)
    assert "boom" in str(DataUnavailableError(detail="boom"))


def test_retcode_constant_values_match_mt5():
    assert RETCode.REQUOTE == 10004
    assert RETCode.REJECT == 10006
    assert RETCode.CANCEL == 10007
    assert RETCode.PLACED == 10008
    assert RETCode.DONE == 10009
    assert RETCode.INVALID_VOLUME == 10014
    assert RETCode.INVALID_STOPS == 10016
    assert RETCode.TRADE_DISABLED == 10017
    assert RETCode.MARKET_CLOSED == 10018
    assert RETCode.NO_MONEY == 10019
    assert RETCode.INVALID_FILL == 10030
