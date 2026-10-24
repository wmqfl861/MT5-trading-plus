"""交易时段工具：DST 感知的本地↔UTC 换算与周末窗口判定。

约定（Global Constraints）：库内时间一律"贴 UTC 标签的 naive datetime"。
涉及本地时段（默认 America/New_York，MT5 常见收盘惯例 17:00）时用
zoneinfo 显式换算，结果仍转回 naive-UTC 表示。

DST 事实（IANA tzdata，测试已钉死）：
- 欧洲冬令时 2026-10-25 01:00 UTC 起生效；
- 美国东部 2026-11-01 02:00 本地回拨（EDT→EST）；
- 因此 10-30 周的周末窗口不对称：周五收盘 21:00 UTC、周日开盘 22:00 UTC
  （49 小时）——周末缺口判断绝不能按"恒 48 小时"做算术推断。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

NY_TZ = "America/New_York"


def _parse_hhmm(hhmm: str) -> tuple[int, int]:
    """解析 "HH:MM"（严格两位数字），非法即抛 ValueError。"""
    if (not isinstance(hhmm, str) or len(hhmm) != 5 or hhmm[2] != ":"
            or not hhmm[:2].isdigit() or not hhmm[3:].isdigit()):
        raise ValueError(f"时刻格式应为 HH:MM，收到 {hhmm!r}")
    hh, mm = int(hhmm[:2]), int(hhmm[3:])
    if hh > 23 or mm > 59:
        raise ValueError(f"时刻越界：{hhmm!r}")
    return hh, mm


def local_to_utc_naive(day: date, tz_name: str, hhmm: str) -> datetime:
    """本地墙上时刻 → naive-UTC datetime（DST 感知）。

    不存在（春拨跳变）或歧义（秋拨重叠）的本地时刻一律 ValueError——
    时段边界必须精确，宁可拒绝也不猜 fold。
    """
    hh, mm = _parse_hhmm(hhmm)
    try:
        tz = ZoneInfo(tz_name)
    except Exception as e:  # ZoneInfoNotFoundError 等
        raise ValueError(f"未知时区 {tz_name!r}") from e
    naive = datetime.combine(day, time(hh, mm))
    a0 = naive.replace(tzinfo=tz, fold=0)
    a1 = naive.replace(tzinfo=tz, fold=1)
    if a0.utcoffset() != a1.utcoffset():
        # fold 间 offset 不同：歧义（两次出现）或不存在（被跳过）。
        # 区分法（PEP 495）：不存在的时刻经 UTC 往返后墙上时间会变。
        back = a0.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
        if back == naive:
            raise ValueError(
                f"本地时刻 {naive}（{tz_name}）存在两个 UTC 对应（夏令时回拨歧义），拒绝猜测")
        raise ValueError(f"本地时刻 {naive}（{tz_name}）不存在（夏令时跳变）")
    return a0.astimezone(timezone.utc).replace(tzinfo=None)


def _friday_of_week(d: date) -> date:
    """所在周（周一为始）的周五；周六/日归上一周——周末窗口属于刚过去的周五。"""
    return d + timedelta(days=4 - d.weekday())


def _window_for_friday(friday: date, tz_name: str, close: str, open_: str
                       ) -> tuple[datetime, datetime]:
    return (local_to_utc_naive(friday, tz_name, close),
            local_to_utc_naive(friday + timedelta(days=2), tz_name, open_))


def _nearby_windows(around: datetime, tz_name: str,
                    close: str, open_: str) -> list[tuple[datetime, datetime]]:
    """around 邻近（前一周/当周/后一周）的三个周末窗口（naive-UTC）。"""
    local_day = around.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name)).date()
    fri = _friday_of_week(local_day)
    return [_window_for_friday(fri + timedelta(days=7 * k), tz_name, close, open_)
            for k in (-1, 0, 1)]


def weekend_window(around: datetime, tz_name: str = NY_TZ,
                   close: str = "17:00", open_: str = "17:00"
                   ) -> tuple[datetime, datetime]:
    """包含 around 的那次"周五当地收盘→周日当地开盘"周末窗口（naive-UTC）。

    around 落在窗口内（含两端）→ 该窗口；周中不在任何窗口内 →
    返回最近一个即将开始的窗口（对缺口判断而言即"下一处休市"）。
    """
    windows = _nearby_windows(around, tz_name, close, open_)
    for w in windows:
        if w[0] <= around <= w[1]:
            return w
    upcoming = [w for w in windows if w[0] > around]
    if upcoming:
        return min(upcoming)
    return windows[-1]  # 理论不可达（候选已含下一周五）


def gap_is_weekend(t_prev: datetime, t_next: datetime,
                   tz_name: str = NY_TZ) -> bool:
    """缺口区间 (t_prev, t_next) 与任一邻近周末窗口相交 → True。

    对 t_prev、t_next 各自邻近（前/当/后一周）的窗口都检查，
    覆盖"缺口一端恰在窗口边界之外"与"缺口跨过周末"的情形；
    绝不做"恒 48 小时"的算术推断（DST 切换周窗口可为 49 小时）。
    """
    windows: list[tuple[datetime, datetime]] = []
    for anchor in (t_prev, t_next):
        for w in _nearby_windows(anchor, tz_name, "17:00", "17:00"):
            if w not in windows:
                windows.append(w)
    return any(t_prev < wo and t_next > wc for wc, wo in windows)
