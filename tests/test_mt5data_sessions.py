"""sessions 时段工具测试：DST 切换周的真实边界时刻（Review Focus #2）。

期望值先手写为字面常量，再与 zoneinfo 独立换算结果互钉——两边必须同时成立。

IANA tzdata 事实基线（2026 年）：
- 欧洲冬令时 2026-10-25 01:00 UTC 起生效（如 Europe/London 02:00 BST→GMT）；
- 美国东部 2026-11-01 02:00 本地回拨（EDT→EST，UTC-4→UTC-5）；
- 因此 10-30 周的周末窗口不对称：周五收盘 21:00 UTC、周日开盘 22:00 UTC（49 小时）。
"""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mt5data.sessions import (
    NY_TZ, gap_is_weekend, local_to_utc_naive, weekend_window,
)

NY = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")


def zoneinfo_ref(day: date, hhmm: str, tz) -> datetime:
    """独立基准：直接用 ZoneInfo 换算，不经被测代码。"""
    hh, mm = map(int, hhmm.split(":"))
    return (datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
            .astimezone(timezone.utc).replace(tzinfo=None))


# ---- a) 2026-10-23 周：周五，美欧夏令时均未切 ----


def test_friday_close_oct23_edt_pinned_both_ways():
    expected = datetime(2026, 10, 23, 21, 0)  # 手写：17:00 EDT = UTC-4
    got = local_to_utc_naive(date(2026, 10, 23), NY_TZ, "17:00")
    assert got == expected
    assert got == zoneinfo_ref(date(2026, 10, 23), "17:00", NY)  # zoneinfo 互钉


def test_sunday_open_oct25_still_edt():
    # 欧洲当天已切冬令时，但美国 11-01 才切：周日 17:00 仍为 EDT
    expected = datetime(2026, 10, 25, 21, 0)
    got = local_to_utc_naive(date(2026, 10, 25), NY_TZ, "17:00")
    assert got == expected
    assert got == zoneinfo_ref(date(2026, 10, 25), "17:00", NY)


def test_weekend_window_oct23_week_48h():
    w = weekend_window(datetime(2026, 10, 24, 12, 0))  # 周六正午 UTC
    assert w == (datetime(2026, 10, 23, 21, 0), datetime(2026, 10, 25, 21, 0))
    assert w == (zoneinfo_ref(date(2026, 10, 23), "17:00", NY),
                 zoneinfo_ref(date(2026, 10, 25), "17:00", NY))
    assert w[1] - w[0] == timedelta(hours=48)


# ---- a/b) 2026-10-30 周：欧洲已切、美国未切 → 49 小时不对称周末 ----


def test_weekend_window_oct30_week_49h_asymmetric():
    w = weekend_window(datetime(2026, 10, 31, 12, 0))
    assert w == (datetime(2026, 10, 30, 21, 0),   # 周五 17:00 EDT = UTC-4
                 datetime(2026, 11, 1, 22, 0))    # 周日 17:00 EST = UTC-5（当晨已回拨）
    assert w == (zoneinfo_ref(date(2026, 10, 30), "17:00", NY),
                 zoneinfo_ref(date(2026, 11, 1), "17:00", NY))
    assert w[1] - w[0] == timedelta(hours=49)  # 不对称：49 小时而非 48


def test_london_switch_visible_same_week():
    # 欧洲视角同周对比：10-23 仍 BST(UTC+1)=16:00 UTC；10-30 已 GMT=17:00 UTC
    assert local_to_utc_naive(date(2026, 10, 23), "Europe/London", "17:00") \
        == datetime(2026, 10, 23, 16, 0)
    assert local_to_utc_naive(date(2026, 10, 30), "Europe/London", "17:00") \
        == datetime(2026, 10, 30, 17, 0)


# ---- b) 2026-11-01 美国切换周之后：全部 EST，窗口回到 48 小时 ----


def test_weekend_window_nov06_week_after_us_switch():
    w = weekend_window(datetime(2026, 11, 7, 12, 0))
    assert w == (datetime(2026, 11, 6, 22, 0), datetime(2026, 11, 8, 22, 0))
    assert w == (zoneinfo_ref(date(2026, 11, 6), "17:00", NY),
                 zoneinfo_ref(date(2026, 11, 8), "17:00", NY))
    assert w[1] - w[0] == timedelta(hours=48)


def test_weekend_window_midweek_returns_upcoming():
    # 周三正午不在任何窗口内 → 返回最近一个即将开始的周末窗口（10-30 周）
    w = weekend_window(datetime(2026, 10, 28, 12, 0))
    assert w == (datetime(2026, 10, 30, 21, 0), datetime(2026, 11, 1, 22, 0))


def test_weekend_window_boundary_inclusive():
    # 恰在收盘/开盘时刻也算"在窗口内"
    assert weekend_window(datetime(2026, 10, 23, 21, 0)) \
        == (datetime(2026, 10, 23, 21, 0), datetime(2026, 10, 25, 21, 0))
    assert weekend_window(datetime(2026, 10, 25, 21, 0)) \
        == (datetime(2026, 10, 23, 21, 0), datetime(2026, 10, 25, 21, 0))


# ---- 不存在/歧义时刻 fail-fast ----


def test_nonexistent_local_time_raises():
    with pytest.raises(ValueError):  # 2026-03-08 02:30 美东被春拨跳过
        local_to_utc_naive(date(2026, 3, 8), NY_TZ, "02:30")


def test_ambiguous_local_time_raises():
    with pytest.raises(ValueError):  # 2026-11-01 01:30 美东出现两次
        local_to_utc_naive(date(2026, 11, 1), NY_TZ, "01:30")


def test_bad_hhmm_raises():
    for bad in ("25:00", "24:00", "7:5", "17", "17:0x", "ab:cd", ""):
        with pytest.raises(ValueError):
            local_to_utc_naive(date(2026, 10, 23), NY_TZ, bad)


def test_unknown_tz_raises():
    with pytest.raises(ValueError):
        local_to_utc_naive(date(2026, 10, 23), "Mars/Olympus", "17:00")


# ---- gap_is_weekend：周五末根→周日首根等真实缺口形态 ----


def test_gap_friday_last_to_sunday_first_is_weekend():
    # 10-23 周：周五末根 20:59 → 周日首根 21:00
    assert gap_is_weekend(datetime(2026, 10, 23, 20, 59),
                          datetime(2026, 10, 25, 21, 0))


def test_gap_us_switch_week_49h_is_weekend():
    # 11-01 切换周：周五末根 20:59 → 周日首根 22:00（49 小时缺口不许误判）
    assert gap_is_weekend(datetime(2026, 10, 30, 20, 59),
                          datetime(2026, 11, 1, 22, 0))


def test_gap_midweek_not_weekend():
    assert not gap_is_weekend(datetime(2026, 10, 28, 9, 59),
                              datetime(2026, 10, 28, 11, 0))


def test_gap_inside_weekend_window_is_weekend():
    # 数据在周六凌晨中断（缺口两端都在窗口内部）
    assert gap_is_weekend(datetime(2026, 10, 24, 2, 0),
                          datetime(2026, 10, 24, 6, 0))


def test_gap_crossing_weekend_from_thursday_is_weekend():
    # 巨大缺口跨过周末（周四→下周一）仍应识别出周末成分
    assert gap_is_weekend(datetime(2026, 10, 22, 12, 0),
                          datetime(2026, 10, 26, 12, 0))


# ---- T20：SessionPolicy（禁入窗共享定义源，决策 D11 / 铁律 v3 第 11 条） ----

from mt5data.sessions import (  # noqa: E402
    SessionConfigError, SessionPolicy, load_session_policy,
)

SESSIONS_TOML = """\
# 测试用 sessions.toml（与 config/sessions.toml 同构）
version = 1

[rollover]
local_time = "17:00"
tz = "America/New_York"

[forbidden]
pre_minutes = 5
post_minutes = 60
exempt_symbols = ["BTCUSD"]

[forbidden.extended.EURUSD]
end_local = "23:00"

[costs]
spread_gate_multiplier = 3.0
missing_hour_policy = "forbid"
"""


def _policy() -> SessionPolicy:
    return SessionPolicy(
        rollover_local="17:00", rollover_tz=NY_TZ,
        pre_minutes=5, post_minutes=60,
        exempt_symbols=frozenset({"BTCUSD"}),
        extended_ends={"EURUSD": "23:00"},
    )


def test_entry_forbidden_summer_2110_utc():
    # 2026-07-15（EDT）：17:00 NY = 21:00 UTC，禁入 20:55–22:00 → 21:10 禁入
    assert not _policy().entry_allowed("XAUUSD", datetime(2026, 7, 15, 21, 10))


def test_entry_allowed_winter_2110_utc():
    # 2026-12-15（EST）：17:00 NY = 22:00 UTC，禁入 21:55–23:00 → 21:10 允许
    assert _policy().entry_allowed("XAUUSD", datetime(2026, 12, 15, 21, 10))


def test_entry_forbidden_winter_2210_utc():
    assert not _policy().entry_allowed("XAUUSD", datetime(2026, 12, 15, 22, 10))


def test_entry_window_boundaries():
    p = _policy()
    # 夏令时边界：20:55 起（含）禁入、22:00 止（不含）恢复
    assert p.entry_allowed("XAUUSD", datetime(2026, 7, 15, 20, 54, 59))
    assert not p.entry_allowed("XAUUSD", datetime(2026, 7, 15, 20, 55))
    assert not p.entry_allowed("XAUUSD", datetime(2026, 7, 15, 21, 59, 59))
    assert p.entry_allowed("XAUUSD", datetime(2026, 7, 15, 22, 0))


def test_exempt_symbol_allowed_in_rollover_window():
    p = _policy()
    assert p.entry_allowed("BTCUSD", datetime(2026, 7, 15, 21, 10))
    assert p.entry_allowed("BTCUSD", datetime(2026, 12, 15, 22, 10))


def test_extended_window_eurusd_until_23_local():
    p = _policy()
    # EURUSD 扩展到 23:00 本地：22:30 UTC = 18:30 NY 仍在 [16:55, 23:00) → 禁入
    assert not p.entry_allowed("EURUSD", datetime(2026, 7, 15, 22, 30))
    # 23:00 NY（=次日 03:00 UTC）边界不含 → 允许
    assert p.entry_allowed("EURUSD", datetime(2026, 7, 16, 3, 0))
    assert not p.entry_allowed("EURUSD", datetime(2026, 7, 16, 2, 59, 59))
    # 基础品种 18:00 NY（夏令时 22:00 UTC）已恢复，EURUSD 仍禁入（扩展窗差异）
    assert p.entry_allowed("XAUUSD", datetime(2026, 7, 15, 22, 0))
    assert not p.entry_allowed("EURUSD", datetime(2026, 7, 15, 22, 0))


def test_rollover_window_naive_utc_summer_and_winter():
    p = _policy()
    assert p.rollover_window(date(2026, 7, 15)) == (
        datetime(2026, 7, 15, 20, 55), datetime(2026, 7, 15, 22, 0))
    assert p.rollover_window(date(2026, 12, 15)) == (
        datetime(2026, 12, 15, 21, 55), datetime(2026, 12, 15, 23, 0))


def test_with_forbidden_hours_extends_policy():
    p = _policy().with_forbidden_hours({"XAUUSD": (3, 17), "BTCUSD": (3,)})
    # 数据驱动缺统计小时按品种生效：本地小时 3/17 禁入
    assert not p.entry_allowed("XAUUSD", datetime(2026, 7, 15, 7, 10))    # 03:10 NY
    assert not p.entry_allowed("XAUUSD", datetime(2026, 7, 15, 21, 10))   # 17:10 NY：缺统计 + 换日窗
    assert not p.entry_allowed("BTCUSD", datetime(2026, 7, 15, 7, 10))    # 豁免换日窗但不豁免自己的缺统计小时
    assert p.entry_allowed("BTCUSD", datetime(2026, 7, 15, 21, 10))       # 豁免品种无缺统计的 17 点照常
    assert p.entry_allowed("XAUUSD", datetime(2026, 7, 15, 10, 10))       # 06:10 NY 正常
    assert p.entry_allowed("XAUUSD", datetime(2026, 7, 15, 20, 10))       # 16:10 NY：窗前且小时不在缺统计集
    # 原策略不变（with_ 返回新对象）
    assert _policy().entry_allowed("XAUUSD", datetime(2026, 7, 15, 7, 10))


def test_policy_validation_rejects_bad_values():
    with pytest.raises(ValueError):
        SessionPolicy(pre_minutes=-1)
    with pytest.raises(ValueError):
        SessionPolicy(rollover_local="25:00")
    with pytest.raises(ValueError):
        SessionPolicy(rollover_tz="Mars/Olympus")
    with pytest.raises(ValueError):
        SessionPolicy(missing_hour_policy="ignore")
    with pytest.raises(ValueError):
        SessionPolicy(spread_gate_multiplier=0.0)
    with pytest.raises(ValueError):
        SessionPolicy(extended_ends={"EURUSD": "09:00"})   # 早于换日时刻的扩展无意义
    with pytest.raises(ValueError):
        SessionPolicy().with_forbidden_hours({"XAUUSD": (24,)})


def test_load_session_policy_from_toml(tmp_path):
    p = tmp_path / "sessions.toml"
    p.write_text(SESSIONS_TOML, encoding="utf-8")
    pol = load_session_policy(str(p))
    assert pol.rollover_local == "17:00" and pol.rollover_tz == NY_TZ
    assert pol.pre_minutes == 5 and pol.post_minutes == 60
    assert pol.exempt_symbols == frozenset({"BTCUSD"})
    assert pol.extended_ends == {"EURUSD": "23:00"}
    assert pol.spread_gate_multiplier == 3.0
    assert pol.missing_hour_policy == "forbid"
    assert not pol.entry_allowed("XAUUSD", datetime(2026, 7, 15, 21, 10))
    assert pol.entry_allowed("XAUUSD", datetime(2026, 12, 15, 21, 10))


def test_load_session_policy_strict_validation(tmp_path):
    p = tmp_path / "sessions.toml"
    base = SESSIONS_TOML
    p.write_text(base.replace("version = 1", "version = 2"), encoding="utf-8")
    with pytest.raises(SessionConfigError, match="version"):
        load_session_policy(str(p))
    p.write_text(base + '\nfoo = 1\n', encoding="utf-8")
    with pytest.raises(SessionConfigError, match="foo"):
        load_session_policy(str(p))
    p.write_text(base.replace('pre_minutes = 5', 'pre_minutes = "x"'), encoding="utf-8")
    with pytest.raises(SessionConfigError):
        load_session_policy(str(p))
    p.write_text(base.replace("local_time = \"17:00\"\n", ""), encoding="utf-8")
    with pytest.raises(SessionConfigError, match="local_time"):
        load_session_policy(str(p))
    with pytest.raises(SessionConfigError, match="不存在"):
        load_session_policy(str(tmp_path / "nope.toml"))


def test_repo_sessions_toml_defaults_match_plan():
    # 仓库实际配置：换日 17:00 NY、前 5 分钟到后 60 分钟、BTCUSD 豁免、
    # EURUSD 扩展到 23:00、倍数 3.0、missing forbid（初值，待 DST 实测定稿）
    repo = Path(__file__).resolve().parents[1] / "config" / "sessions.toml"
    pol = load_session_policy(str(repo))
    assert pol.rollover_local == "17:00" and pol.rollover_tz == NY_TZ
    assert pol.pre_minutes == 5 and pol.post_minutes == 60
    assert pol.exempt_symbols == frozenset({"BTCUSD"})
    assert pol.extended_ends == {"EURUSD": "23:00"}
    assert pol.spread_gate_multiplier == 3.0
    assert pol.missing_hour_policy == "forbid"
