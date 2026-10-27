"""交易时段工具：DST 感知的本地↔UTC 换算与周末窗口判定 + 换日禁入策略（T20）。

约定（Global Constraints）：库内时间一律"贴 UTC 标签的 naive datetime"。
涉及本地时段（默认 America/New_York，MT5 常见收盘惯例 17:00）时用
zoneinfo 显式换算，结果仍转回 naive-UTC 表示。

DST 事实（IANA tzdata，测试已钉死）：
- 欧洲冬令时 2026-10-25 01:00 UTC 起生效；
- 美国东部 2026-11-01 02:00 本地回拨（EDT→EST）；
- 因此 10-30 周的周末窗口不对称：周五收盘 21:00 UTC、周日开盘 22:00 UTC
  （49 小时）——周末缺口判断绝不能按"恒 48 小时"做算术推断。

SessionPolicy（T20 / 决策 D11 / 铁律 v3 第 11 条）：换日禁入窗的**唯一定义源**
（config/sessions.toml），回测引擎（engine_a/engine_b 的开仓推迟）与实盘风控
（mt5live.riskproc 的禁入检查）共同读取——两边对"什么时候禁止开仓"永远一致。
窗口锚定 rollover_local 于 rollover_tz 的墙上时刻（Exness 换日 17:00 纽约 =
夏令时 21:00 / 冬令时 22:00 GMT），禁止用固定 UTC 小时表示（铁律第 1 条）。
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

NY_TZ = "America/New_York"

DEFAULT_SESSIONS_PATH = "config/sessions.toml"
_MISSING_HOUR_POLICIES = ("forbid",)


class SessionConfigError(RuntimeError):
    """sessions.toml 缺失 / 键不合法 / 类型错误——拒绝启动。"""


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
                   tz_name: str = NY_TZ, *,
                   bar: timedelta = timedelta(minutes=1)) -> bool:
    """缺口 (t_prev, t_next) 是否**与周末窗口基本重合**（T79/R12-G1 收紧）。

    旧判定"与任一窗口相交即 weekend"会把跨多周末的长缺口整段吞掉（真实
    案例：EURUSD 2021-05/06 的 27 天缺口因此从未报警）。现在为真须同时满足：
    1. 恰覆盖一个周末窗口——覆盖 ≥2 个的是跨多周末长缺口（质检判
       long_gap），一个都不覆盖的与周末无关（其余规则分类）；
    2. 缺口起点不早于周五收盘前一根 bar：t_prev ≥ 收盘 − 2×bar
       （t_prev 为缺口前末根现存 bar，首根缺失 bar = t_prev + bar）；
    3. 缺口终点不晚于周日开盘后一根 bar：t_next ≤ 开盘 + bar。

    容差按 bar=K 线周期换算，吸收"周五提前收盘/周日晚开"的常规偏差；
    DST 由窗口函数逐周换算（切换周窗口可为 49 小时），绝不做"恒 48 小时"
    的算术推断。
    """
    windows = overlapped_weekend_windows(t_prev, t_next, tz_name)
    if len(windows) != 1:
        return False
    close_utc, open_utc = windows[0]
    return t_prev >= close_utc - 2 * bar and t_next <= open_utc + bar


def weekend_windows_between(t_prev: datetime, t_next: datetime,
                            tz_name: str = NY_TZ, close: str = "17:00",
                            open_: str = "17:00"
                            ) -> list[tuple[datetime, datetime]]:
    """[t_prev, t_next] 时段可能触及的全部周末窗口（按周五逐周枚举，naive-UTC）。

    枚举范围从 t_prev 所在周的前一个周五到 t_next 所在周的后一个周五——
    覆盖"缺口一端落在周六凌晨（窗口属上一周五）"与"缺口横跨多周"的情形。
    """
    if t_next <= t_prev:
        return []
    tz = ZoneInfo(tz_name)
    d0 = t_prev.replace(tzinfo=timezone.utc).astimezone(tz).date()
    d1 = t_next.replace(tzinfo=timezone.utc).astimezone(tz).date()
    fri = _friday_of_week(d0) - timedelta(days=7)
    last = _friday_of_week(d1) + timedelta(days=7)
    out: list[tuple[datetime, datetime]] = []
    while fri <= last:
        out.append(_window_for_friday(fri, tz_name, close, open_))
        fri += timedelta(days=7)
    return out


def overlapped_weekend_windows(t_prev: datetime, t_next: datetime,
                               tz_name: str = NY_TZ
                               ) -> list[tuple[datetime, datetime]]:
    """缺口开区间 (t_prev, t_next) 实际覆盖到的周末窗口（相交即算）。

    跨多周末的长缺口覆盖 ≥2 个窗口——质检据此判 long_gap（T79/R12-G1）。
    """
    return [w for w in weekend_windows_between(t_prev, t_next, tz_name)
            if t_prev < w[1] and t_next > w[0]]


# ------------------------------------------------ T20：换日禁入策略（共享定义源）

def _hhmm_to_minutes(hhmm: str) -> int:
    """解析 "HH:MM" → 当日分钟数；非法即抛 ValueError。"""
    hh, mm = _parse_hhmm(hhmm)
    return hh * 60 + mm


@dataclass(frozen=True)
class SessionPolicy:
    """换日禁入窗与点差闸门参数（config/sessions.toml 的内存形态）。

    禁入窗（非豁免品种）= 换日时刻前 pre_minutes 到后 post_minutes（本地墙上
    时间，zoneinfo 逐日换算，DST 感知）；extended_ends 把指定品种的禁入窗
    **结束**时刻延长到该本地时刻。T35 起扩展窗长度（换日时刻 → end_local）
    上限 3 小时：超过须在 sessions.toml 显式写 allow_long_window = true 并
    附理由注释（数据依据见 docs/research/rollover-dst-2026-10-25.md 第 3 节；
    初值 EURUSD 23:00 因口径错误已删——纽约本地小时重测显示工作日 18:00
    已完全恢复，残余尖峰是周日开盘流动性，由点差闸门处理）。
    exempt_symbols 完全豁免换日窗（BTCUSD 7×24），但不豁免 forbidden_hours_ny
    （那是数据保护，不是时段规则）。
    forbidden_hours_ny 是**数据驱动**的补充禁入小时（点差模型缺统计的小时，
    由回测接线层注入，不进 sessions.toml）；spread_gate_multiplier /
    missing_hour_policy 供成本侧与实盘点差闸门（T24）读取；
    min_days_per_hour（T36）供点差模型消费方（SpreadModel.missing_hours /
    spread_p90 的 min_days_per_hour 参数）——某小时覆盖天数不足即缺统计。
    """

    rollover_local: str = "17:00"
    rollover_tz: str = NY_TZ
    pre_minutes: int = 5
    post_minutes: int = 60
    exempt_symbols: frozenset = frozenset()
    extended_ends: Mapping[str, str] = field(default_factory=dict)
    extended_allow_long: frozenset = frozenset()
    forbidden_hours_ny: Mapping[str, frozenset] = field(default_factory=dict)
    spread_gate_multiplier: float = 3.0
    missing_hour_policy: str = "forbid"
    min_days_per_hour: int = 20   # T36：点差模型每小时最少覆盖天数（不足=缺统计）

    # 扩展窗长度上限（分钟）：换日时刻 → end_local 超过 3 小时须显式放行
    EXTENDED_WINDOW_MAX_MINUTES = 180

    def __post_init__(self):
        _parse_hhmm(self.rollover_local)   # 非法即抛
        try:
            ZoneInfo(self.rollover_tz)
        except Exception as e:  # ZoneInfoNotFoundError 等
            raise ValueError(f"未知时区 {self.rollover_tz!r}") from e
        for name in ("pre_minutes", "post_minutes"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ValueError(f"{name} 须为非负整数，收到 {v!r}")
        if self.missing_hour_policy not in _MISSING_HOUR_POLICIES:
            raise ValueError(
                f"missing_hour_policy 须为 {_MISSING_HOUR_POLICIES} 之一，"
                f"收到 {self.missing_hour_policy!r}")
        if not (self.spread_gate_multiplier > 0):
            raise ValueError(
                f"spread_gate_multiplier 须为正，收到 {self.spread_gate_multiplier!r}")
        if (isinstance(self.min_days_per_hour, bool)
                or not isinstance(self.min_days_per_hour, int)
                or self.min_days_per_hour < 1):
            raise ValueError(
                f"min_days_per_hour 须为正整数，收到 {self.min_days_per_hour!r}")
        rollover_min = _hhmm_to_minutes(self.rollover_local)
        for sym, end in self.extended_ends.items():
            if not sym:
                raise ValueError("extended_ends 品种名不能为空")
            end_min = _hhmm_to_minutes(end)
            if end_min <= rollover_min:
                raise ValueError(
                    f"{sym} 扩展窗结束 {end} 须晚于换日时刻 {self.rollover_local}")
            if (end_min - rollover_min > self.EXTENDED_WINDOW_MAX_MINUTES
                    and sym not in self.extended_allow_long):
                raise ValueError(
                    f"{sym} 扩展窗长度 {(end_min - rollover_min) // 60} 小时 "
                    f"{(end_min - rollover_min) % 60} 分（{self.rollover_local}→{end}）"
                    f"超过 {self.EXTENDED_WINDOW_MAX_MINUTES // 60} 小时上限；确有依据"
                    f"请在 sessions.toml 写 allow_long_window = true 并附理由注释")
        for sym, hours in self.forbidden_hours_ny.items():
            for h in hours:
                if not 0 <= int(h) <= 23:
                    raise ValueError(f"{sym} 禁入小时须 0..23，收到 {h}")
        object.__setattr__(self, "exempt_symbols", frozenset(self.exempt_symbols))
        object.__setattr__(
            self, "extended_ends", dict(self.extended_ends))
        object.__setattr__(
            self, "extended_allow_long", frozenset(self.extended_allow_long))
        object.__setattr__(
            self, "forbidden_hours_ny",
            {s: frozenset(h) for s, h in self.forbidden_hours_ny.items()})

    # -- 查询 --

    def rollover_window(self, day: date) -> tuple[datetime, datetime]:
        """该本地日的基础禁入窗（naive UTC）：[换日−pre, 换日+post]。

        品种差异（扩展窗）由 entry_allowed 处理；本方法返回通用锚点窗。
        """
        start = (local_to_utc_naive(day, self.rollover_tz, self.rollover_local)
                 - timedelta(minutes=self.pre_minutes))
        end = (local_to_utc_naive(day, self.rollover_tz, self.rollover_local)
               + timedelta(minutes=self.post_minutes))
        return start, end

    def entry_allowed(self, symbol: str, t_utc: datetime) -> bool:
        """t_utc（naive UTC，或 aware）时刻 symbol 是否允许**开新仓**。

        平仓永远不受限（调用方语义）；本方法只回答开仓。
        """
        t = (t_utc if t_utc.tzinfo is not None
             else t_utc.replace(tzinfo=timezone.utc))
        tz = ZoneInfo(self.rollover_tz)
        local = t.astimezone(tz)
        hours = self.forbidden_hours_ny.get(symbol, frozenset())
        if local.hour in hours:
            return False
        if symbol in self.exempt_symbols:
            return True
        t_naive = t.astimezone(timezone.utc).replace(tzinfo=None)
        for day in (local.date() - timedelta(days=1), local.date()):
            start = (local_to_utc_naive(day, self.rollover_tz, self.rollover_local)
                     - timedelta(minutes=self.pre_minutes))
            end_local = self.extended_ends.get(symbol)
            end = (local_to_utc_naive(day, self.rollover_tz, end_local)
                   if end_local is not None else
                   local_to_utc_naive(day, self.rollover_tz, self.rollover_local)
                   + timedelta(minutes=self.post_minutes))
            if start <= t_naive < end:
                return False
        return True

    def with_forbidden_hours(
            self, hours_by_symbol: Mapping[str, Iterable[int]]) -> "SessionPolicy":
        """合并数据驱动的禁入小时（点差模型缺统计），返回新策略（原对象不变）。"""
        merged = {s: frozenset(self.forbidden_hours_ny.get(s, frozenset()))
                  | frozenset(h) for s, h in hours_by_symbol.items()}
        return replace(self, forbidden_hours_ny=merged)


def load_session_policy(path: str = DEFAULT_SESSIONS_PATH) -> SessionPolicy:
    """加载 sessions.toml（严格校验：缺键/未知键/类型不符 → SessionConfigError）。"""
    if not os.path.exists(path):
        raise SessionConfigError(
            f"会话配置不存在：{path}（T20 起回测与风控共用该文件定义禁入窗）")
    with open(path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise SessionConfigError(f"{path} 不是合法 TOML：{e}") from e
    try:
        return _session_policy_from_dict(data, path)
    except SessionConfigError:
        raise
    except (TypeError, ValueError) as e:
        raise SessionConfigError(f"{path} 会话配置不合法：{e}") from e


def _session_policy_from_dict(data: dict, path: str) -> SessionPolicy:
    def require(key: str):
        if key not in data:
            raise SessionConfigError(f"会话配置缺键：{key}（{path}）")
        return data.pop(key)

    version = require("version")
    if version != 1:
        raise SessionConfigError(f"version 须为 1，收到 {version!r}（{path}）")

    rollover = require("rollover")
    if not isinstance(rollover, dict):
        raise SessionConfigError("rollover 须为表")
    unknown = sorted(set(rollover) - {"local_time", "tz"})
    if unknown:
        raise SessionConfigError(f"rollover 未知键：{unknown}")
    local_time = rollover.get("local_time")
    tz = rollover.get("tz")
    if not isinstance(local_time, str) or not isinstance(tz, str):
        raise SessionConfigError("rollover.local_time/tz 须为字符串（HH:MM / 时区名）")

    forbidden = require("forbidden")
    if not isinstance(forbidden, dict):
        raise SessionConfigError("forbidden 须为表")
    unknown = sorted(set(forbidden) - {"pre_minutes", "post_minutes",
                                       "exempt_symbols", "extended"})
    if unknown:
        raise SessionConfigError(f"forbidden 未知键：{unknown}")
    pre = forbidden.get("pre_minutes", 5)
    post = forbidden.get("post_minutes", 60)
    for name, v in (("pre_minutes", pre), ("post_minutes", post)):
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise SessionConfigError(f"forbidden.{name} 须为非负整数，收到 {v!r}")
    exempt = forbidden.get("exempt_symbols", [])
    if (not isinstance(exempt, list)
            or not all(isinstance(s, str) and s for s in exempt)):
        raise SessionConfigError("forbidden.exempt_symbols 须为字符串数组")
    extended_raw = forbidden.get("extended", {})
    if not isinstance(extended_raw, dict):
        raise SessionConfigError("forbidden.extended 须为表（品种 → {end_local}）")
    extended: dict[str, str] = {}
    extended_allow: set[str] = set()
    for sym, sub in extended_raw.items():
        if (not isinstance(sub, dict)
                or set(sub) - {"end_local", "allow_long_window"}
                or "end_local" not in sub):
            raise SessionConfigError(
                f"forbidden.extended.{sym} 须只含 end_local（HH:MM 本地时刻）"
                f"与可选 allow_long_window（布尔）")
        end = sub["end_local"]
        if not isinstance(end, str):
            raise SessionConfigError(f"forbidden.extended.{sym}.end_local 须为字符串")
        allow = sub.get("allow_long_window", False)
        if not isinstance(allow, bool):
            raise SessionConfigError(
                f"forbidden.extended.{sym}.allow_long_window 须为布尔，收到 {allow!r}")
        extended[sym] = end
        if allow:
            extended_allow.add(sym)

    costs = require("costs")
    if not isinstance(costs, dict):
        raise SessionConfigError("costs 须为表")
    unknown = sorted(set(costs) - {"spread_gate_multiplier", "missing_hour_policy",
                                   "min_days_per_hour"})
    if unknown:
        raise SessionConfigError(f"costs 未知键：{unknown}")
    mult = costs.get("spread_gate_multiplier", 3.0)
    if isinstance(mult, bool) or not isinstance(mult, (int, float)) or not mult > 0:
        raise SessionConfigError(f"costs.spread_gate_multiplier 须为正数，收到 {mult!r}")
    policy = costs.get("missing_hour_policy", "forbid")
    if policy not in _MISSING_HOUR_POLICIES:
        raise SessionConfigError(
            f"costs.missing_hour_policy 须为 {_MISSING_HOUR_POLICIES} 之一，"
            f"收到 {policy!r}")
    min_days = costs.get("min_days_per_hour", 20)
    if (isinstance(min_days, bool) or not isinstance(min_days, int)
            or min_days < 1):
        raise SessionConfigError(
            f"costs.min_days_per_hour 须为正整数，收到 {min_days!r}")

    if data:
        raise SessionConfigError(f"会话配置未知键：{sorted(data)}（{path}）")
    return SessionPolicy(
        rollover_local=local_time, rollover_tz=tz,
        pre_minutes=pre, post_minutes=post,
        exempt_symbols=frozenset(exempt),
        extended_ends=extended,
        extended_allow_long=frozenset(extended_allow),
        spread_gate_multiplier=float(mult),
        missing_hour_policy=policy,
        min_days_per_hour=int(min_days),
    )

