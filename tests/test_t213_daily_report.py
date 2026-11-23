"""T213: the daily report retries inside the rollover window, backfills recent
days and raises an alert when a report cannot be produced (plan round 23 §2.4).

Review round (docs/reviews/2026-11-23-t213-review.md), 2026-11-23: the window is
judged at the moment each attempt starts, the first attempt of every day included
(K1); an account read that returns nothing is a data failure wherever it happens
(K2); the alert states the attempts of each day (K3); every failed run raises the
alert (K5).

Gateways are stand-ins or FakeMT5, so nothing here touches MetaTrader5. The clock
and the sleep are injected: a retry never waits in real time, and no test depends
on the wall clock (the first attempt of every day is gated by the window).
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from mt5gw.errors import NotConnectedError
from mt5live.paper import PaperBroker
from tests.fakes import FakeMT5
from tests.fakes_live import FakeBroker, FakeGateway, QuoteMarket

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 12, 2)                      # winter: the window is 21:55–23:00 UTC
SUMMER_DAY = date(2026, 7, 1)                # summer: the window is 20:55–22:00 UTC
TASK_TIME = datetime(2026, 12, 2, 21, 57)    # the scheduled moment, 05:57 local
WINDOW_END = datetime(2026, 12, 2, 23, 0)    # winter: the window closes at 23:00 UTC
MAGIC = 20261101


def load():
    spec = importlib.util.spec_from_file_location("t213_daily_report", ROOT / "scripts" / "live_daily_report.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class Clock:
    def __init__(self, start):
        self.start, self.now, self.sleeps = start, start, []

    def tick(self):
        return self.now.replace(tzinfo=timezone.utc)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


class FlakyGateway:
    """account.state() fails `failures` times with the gateway's own disconnect error.

    With a clock, every call records the moment the terminal was touched and
    makes `cost` seconds pass: a failing attempt takes time of its own (review R1)."""

    def __init__(self, failures, broker=None, *, clock=None, cost=0):
        self.inner, self.failures = FakeGateway(broker or FakeBroker()), failures
        self.account = self
        self.shutdowns = 0
        self.clock, self.cost, self.touched = clock, cost, []

    def state(self):
        if self.clock is not None:
            self.touched.append(self.clock.now)
            self.clock.now += timedelta(seconds=self.cost)
        if self.failures:
            self.failures -= 1
            raise NotConnectedError("terminal online, trade server not connected")
        return self.inner.account.state()

    def deals(self, start, end):
        return self.inner.account.deals(start, end)

    def shutdown(self):
        self.shutdowns += 1
        self.inner.shutdown()


def run(tmp_path, mod, clock, gw, *extra, day=DAY):
    data = tmp_path / "data"
    (data / "live").mkdir(parents=True, exist_ok=True)
    reports = tmp_path / "reports"
    rc = mod.main(["--data-dir", str(data), "--reports-dir", str(reports),
                   "--date", day.isoformat(), *extra],
                  gw=gw, clock=clock.tick, sleep=clock.sleep)
    return rc, reports / "live"


def alerts(tmp_path):
    path = Path(tmp_path) / "data" / "live" / "alerts.log"
    return [line.split("\t") for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def failure_alerts(tmp_path):
    return [row for row in alerts(tmp_path) if row[1] == "DAILY_REPORT_FAILED"]


# ================================================================ retry inside the window

def test_connection_failure_is_retried_inside_the_rollover_window(tmp_path):
    mod, clock = load(), Clock(TASK_TIME)
    rc, out = run(tmp_path, mod, clock, FlakyGateway(failures=2))
    assert rc == 0 and (out / f"daily-{DAY}.md").exists()
    assert clock.sleeps == [300, 300]          # five minutes apart, three attempts in all
    assert "DAILY_REPORT_FAILED" not in [row[1] for row in alerts(tmp_path)]


def test_gives_up_after_three_attempts_per_day_and_raises_one_alert(tmp_path):
    """A dead connection fails every day alike: three attempts each, one alert."""
    mod, clock = load(), Clock(TASK_TIME)
    rc, out = run(tmp_path, mod, clock, FlakyGateway(failures=99))
    assert rc == mod.EXIT_INCOMPLETE and not out.exists()
    assert clock.sleeps == [300] * 6
    failed = failure_alerts(tmp_path)
    assert len(failed) == 1
    for day in (DAY, DAY - timedelta(days=1), DAY - timedelta(days=2)):
        assert f"{day} 已尝试 3 次" in failed[0][2], day
    assert failed[0][0].startswith("2026-12-02T22:27:00")   # after the last attempt of the last day


def test_no_retry_once_the_next_attempt_would_leave_the_window(tmp_path):
    """Summer: the task starts at 21:57 UTC and the window ends at 22:00, so a
    five-minute retry would connect to the terminal outside the window (D82)."""
    mod, clock = load(), Clock(datetime(2026, 7, 1, 21, 57))
    rc, out = run(tmp_path, mod, clock, FlakyGateway(failures=1), day=SUMMER_DAY)
    assert rc == mod.EXIT_INCOMPLETE and clock.sleeps == []
    failed = failure_alerts(tmp_path)
    assert len(failed) == 1 and f"{SUMMER_DAY} 已尝试 1 次" in failed[0][2]


def test_rollover_window_boundaries_follow_the_session_policy(tmp_path):
    mod = load()
    winter = datetime(2026, 12, 2, 21, 55), datetime(2026, 12, 2, 23, 0)
    summer = datetime(2026, 7, 1, 20, 55), datetime(2026, 7, 1, 22, 0)
    for start, end in (winter, summer):
        assert mod.in_rollover_window(mod.SessionPolicy(), start)
        assert mod.in_rollover_window(mod.SessionPolicy(), end - timedelta(seconds=1))
        assert not mod.in_rollover_window(mod.SessionPolicy(), end)
        assert not mod.in_rollover_window(mod.SessionPolicy(), start - timedelta(seconds=1))


# ============================================== K1: the window is judged when the attempt starts

def test_the_window_is_judged_when_the_attempt_starts_not_from_the_previous_start(tmp_path):
    """Review R1 (C5-2): the failing attempt takes 30 seconds of its own. Judged
    from the previous start plus the interval, the retry would begin at 23:00:10,
    outside the window. Judged from the moment the retry would start: no sleep,
    no retry."""
    mod, clock = load(), Clock(datetime(2026, 12, 2, 22, 54, 40))
    gateway = FlakyGateway(failures=1, clock=clock, cost=30)
    rc, out = run(tmp_path, mod, clock, gateway)
    assert clock.sleeps == []                        # the retry would start after 23:00
    assert rc == mod.EXIT_INCOMPLETE and not (out / f"daily-{DAY}.md").exists()
    assert max(gateway.touched) < WINDOW_END         # the terminal is never touched outside the window
    failed = failure_alerts(tmp_path)
    assert len(failed) == 1 and f"{DAY} 已尝试 1 次（重试落在换日窗外，没有再试）" in failed[0][2]


def test_a_run_outside_the_window_never_touches_the_terminal(tmp_path, monkeypatch):
    """Review R1 (C5-4): a late start (a manual re-run) must not connect. Every day
    is reported as not attempted, with the reason."""
    mod = load()
    clock = Clock(datetime(2026, 12, 2, 23, 10))
    gateway = FlakyGateway(0, clock=clock)
    rc, out = run(tmp_path, mod, clock, gateway)
    assert rc == mod.EXIT_INCOMPLETE and gateway.touched == [] and not out.exists()
    failed = failure_alerts(tmp_path)
    assert len(failed) == 1 and "退出码 3" in failed[0][2]
    for day in (DAY, DAY - timedelta(days=1), DAY - timedelta(days=2)):
        assert f"{day} 未尝试（换日窗外）" in failed[0][2], day
    # The same through build_gw itself (stand-in terminal): the gateway is never connected.
    work = tmp_path / "through-build_gw"
    terminal = Terminal(failures=0)
    rc, out = run_terminal(work, monkeypatch, mod, Clock(datetime(2026, 12, 2, 23, 10)), terminal)
    assert rc == mod.EXIT_INCOMPLETE and terminal.calls("initialize") == 0 and not out.exists()
    assert len(failure_alerts(work)) == 1


def test_the_first_attempt_of_a_backfill_day_is_gated_too(tmp_path):
    """Review R1 (C5-5): the first attempt fails and takes long enough to end
    after the window closed. The backfill days used to connect regardless."""
    mod, clock = load(), Clock(datetime(2026, 7, 1, 21, 58, 50))
    gateway = FlakyGateway(1, clock=clock, cost=100)
    rc, out = run(tmp_path, mod, clock, gateway, day=SUMMER_DAY)
    assert gateway.touched == [datetime(2026, 7, 1, 21, 58, 50)]   # only the first attempt began
    assert rc == mod.EXIT_INCOMPLETE and not out.exists()
    failed = failure_alerts(tmp_path)
    assert f"{SUMMER_DAY - timedelta(days=1)} 未尝试（换日窗外）" in failed[0][2]
    assert f"{SUMMER_DAY - timedelta(days=2)} 未尝试（换日窗外）" in failed[0][2]


def test_paper_state_is_not_gated_by_the_window(tmp_path):
    """--paper-state reads the shadow state files and never connects, so the
    window does not apply; at the same moment a run that would connect does not start."""
    mod, clock = load(), Clock(datetime(2026, 12, 2, 23, 10))     # winter, outside the window
    gateway = FlakyGateway(0, clock=clock)
    rc, out = run(tmp_path, mod, clock, gateway)
    assert rc == mod.EXIT_INCOMPLETE and gateway.touched == []

    paper_dir = tmp_path / "paper"
    paper_dir.mkdir()
    market = QuoteMarket()
    paper = PaperBroker(market, state_path=str(paper_dir / f"state-{MAGIC}.json"),
                        contract_sizes={"XAUUSD": 100.0}, magic=MAGIC,
                        clock=lambda: datetime(2026, 11, 30, 10, 0))
    opened = paper.market_order("XAUUSD", "buy", 0.1, magic=MAGIC)
    market.set_quote(101.0, 101.2)
    paper.close_position(opened.order_ticket)
    clock = Clock(datetime(2026, 12, 2, 23, 10))
    rc, out = run(tmp_path, mod, clock, None, "--paper-state", str(paper_dir))
    assert rc == 0 and (out / f"daily-{DAY}.md").exists()


# ================================================================ backfill

def test_missing_reports_of_the_previous_days_are_generated(tmp_path):
    mod, clock = load(), Clock(TASK_TIME)
    rc, out = run(tmp_path, mod, clock, FlakyGateway(0))
    assert rc == 0
    for day in (DAY, DAY - timedelta(days=1), DAY - timedelta(days=2)):
        assert (out / f"daily-{day}.md").exists(), day
    assert not (out / f"daily-{DAY - timedelta(days=3)}.md").exists()


def test_existing_earlier_reports_are_left_alone_and_the_requested_day_is_redone(tmp_path):
    mod, clock = load(), Clock(TASK_TIME)
    earlier = tmp_path / "reports" / "live"
    earlier.mkdir(parents=True)
    kept = earlier / f"daily-{DAY - timedelta(days=1)}.md"
    kept.write_text("written the day before", encoding="utf-8")
    stale = earlier / f"daily-{DAY}.md"
    stale.write_text("stale copy", encoding="utf-8")
    rc, out = run(tmp_path, mod, clock, FlakyGateway(0))
    assert rc == 0
    assert kept.read_text(encoding="utf-8") == "written the day before"
    assert "实盘日报" in stale.read_text(encoding="utf-8")
    assert (out / f"daily-{DAY - timedelta(days=2)}.md").exists()      # still missing: generated


def test_a_regenerated_report_states_when_it_was_generated(tmp_path):
    mod, clock = load(), Clock(datetime(2026, 12, 4, 21, 57))
    rc, out = run(tmp_path, mod, clock, FlakyGateway(0), day=DAY)
    assert rc == 0
    text = (out / f"daily-{DAY}.md").read_text(encoding="utf-8")
    assert "生成时间：2026-12-04T21:57:00+00:00（UTC）" in text


def test_one_day_failing_does_not_stop_the_other_days(tmp_path):
    mod, clock = load(), Clock(TASK_TIME)
    broker = FakeBroker()
    gateway = FlakyGateway(0, broker)
    real_deals = gateway.deals
    def deals(start, end):
        if start.date() == DAY - timedelta(days=1):
            raise NotConnectedError("terminal online, trade server not connected")
        return real_deals(start, end)
    gateway.deals = deals
    rc, out = run(tmp_path, mod, clock, gateway)
    assert rc == mod.EXIT_INCOMPLETE
    assert (out / f"daily-{DAY}.md").exists() and (out / f"daily-{DAY - timedelta(days=2)}.md").exists()
    assert not (out / f"daily-{DAY - timedelta(days=1)}.md").exists()
    failed = failure_alerts(tmp_path)
    assert len(failed) == 1 and str(DAY - timedelta(days=1)) in failed[0][2]


# ============================================== K3: the alert states each day's attempts

def test_the_alert_states_the_attempts_of_each_day(tmp_path):
    """Review R4 (C6): started at 22:40 with everything failing, the three days
    get 3, 2 and 1 attempts. The alert used to say "each tried 3 times"."""
    mod, clock = load(), Clock(datetime(2026, 12, 2, 22, 40))
    rc, out = run(tmp_path, mod, clock, FlakyGateway(99))
    assert rc == mod.EXIT_INCOMPLETE
    failed = failure_alerts(tmp_path)
    assert len(failed) == 1
    message = failed[0][2]
    assert f"{DAY} 已尝试 3 次" in message
    assert f"{DAY - timedelta(days=1)} 已尝试 2 次（重试落在换日窗外，没有再试）" in message
    assert f"{DAY - timedelta(days=2)} 已尝试 1 次（重试落在换日窗外，没有再试）" in message
    assert "各已尝试" not in message


def test_the_alert_says_that_nothing_was_attempted_only_when_nothing_was(tmp_path):
    """Second review (N4): started at 22:59:50 with a healthy gateway that takes 20
    seconds a call. Today's report is written; the backfill days come due after the
    window closed and are not attempted. The last sentence of the alert used to say
    "no attempt started" for such a run as well."""
    mod, clock = load(), Clock(datetime(2026, 12, 2, 22, 59, 50))
    gateway = FlakyGateway(0, clock=clock, cost=20)
    rc, out = run(tmp_path, mod, clock, gateway)
    assert rc == mod.EXIT_INCOMPLETE and (out / f"daily-{DAY}.md").exists()
    assert gateway.touched == [datetime(2026, 12, 2, 22, 59, 50)]   # today only: in the window
    failed = failure_alerts(tmp_path)
    assert len(failed) == 1
    message = failed[0][2]
    for day in (DAY - timedelta(days=1), DAY - timedelta(days=2)):
        assert f"{day} 未尝试（换日窗外）" in message, day
    assert str(DAY) not in message                         # today's report exists
    assert "没有一次尝试开始" not in message
    assert "未生成的日子一次都没有尝试：轮到它们时不在换日窗内" in message

    # A run in which really no attempt started keeps the other sentence.
    work = tmp_path / "outside"
    rc, out = run(work, mod, Clock(datetime(2026, 12, 2, 23, 10)), FlakyGateway(0))
    assert rc == mod.EXIT_INCOMPLETE and not out.exists()
    failed = failure_alerts(work)
    assert len(failed) == 1 and "没有一次尝试开始：都不在换日窗内" in failed[0][2]


# ============================================== K2: the first account read is a data read

class Terminal(FakeMT5):
    """FakeMT5 with a Demo account whose first `failures` account reads fail: they
    raise `error`, or return nothing when error is None. The gateway is built by
    the script itself (build_gw); the real package is never imported."""

    def __init__(self, failures, error=None):
        super().__init__()
        self.left, self.error = failures, error
        self.set_behavior("account_info", value=SimpleNamespace(
            login=123456, server="Fake-Server", company="Fake-Broker", currency="USD",
            balance=10000.0, equity=10000.0, margin=0.0, margin_free=10000.0, profit=0.0,
            leverage=100, margin_mode=2, trade_mode=0))
        self.set_behavior("history_deals_get", value=[])

    def account_info(self):
        if self.left:
            self.left -= 1
            self.call_counts["account_info"] = self.call_counts.get("account_info", 0) + 1
            if self.error is not None:
                raise self.error
            return None
        return self._record("account_info")


def run_terminal(tmp_path, monkeypatch, mod, clock, terminal, *extra, day=DAY):
    monkeypatch.setitem(sys.modules, "MetaTrader5", terminal)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "gateway.json").write_text(
        json.dumps({"allow_live_trading": False}), encoding="utf-8")
    return run(tmp_path, mod, clock, None, "--demo", "--gw-config", str(tmp_path / "gateway.json"),
               *extra, day=day)


def test_first_account_read_returning_nothing_is_retried_like_any_other_data_failure(tmp_path, monkeypatch):
    """Review R2 (C7-4): the account read inside the trade_mode check returns
    nothing. It used to be taken for a usage or configuration error: exit code 2,
    no retry, no alert."""
    mod, clock = load(), Clock(TASK_TIME)
    terminal = Terminal(failures=99)
    rc, out = run_terminal(tmp_path, monkeypatch, mod, clock, terminal)
    assert rc == mod.EXIT_INCOMPLETE and not out.exists()
    assert clock.sleeps == [300] * 6
    assert terminal.calls("initialize") == 9 and terminal.calls("shutdown") == 9   # closed once per attempt
    failed = failure_alerts(tmp_path)
    assert len(failed) == 1 and "退出码 3" in failed[0][2]
    for day in (DAY, DAY - timedelta(days=1), DAY - timedelta(days=2)):
        assert f"{day} 已尝试 3 次" in failed[0][2], day


def test_a_failed_first_account_read_is_retried(tmp_path, monkeypatch):
    """Review R2 (C7-5): the same failure one line later, in the data phase, was
    always retried. Nothing returned, a disconnect and a timeout all are."""
    for name, error in (("nothing-returned", None),
                        ("disconnected", NotConnectedError("terminal online, trade server not connected")),
                        ("timeout", TimeoutError("account_info timed out"))):
        work = tmp_path / name
        mod, clock = load(), Clock(TASK_TIME)
        rc, out = run_terminal(work, monkeypatch, mod, clock, Terminal(failures=1, error=error))
        assert rc == 0, name
        assert clock.sleeps == [300], name                    # one retry, then the report
        for day in (DAY, DAY - timedelta(days=1), DAY - timedelta(days=2)):
            assert (out / f"daily-{day}.md").exists(), (name, day)
        assert failure_alerts(work) == [], name


def test_connect_failures_are_retried_and_reported_with_their_own_counts(tmp_path, monkeypatch):
    """Review R15 (C7-3): initialize fails every time. Nothing is ever connected,
    so there is nothing to shut down; each day gets three attempts."""
    mod, clock = load(), Clock(TASK_TIME)
    terminal = Terminal(failures=0)
    terminal.initialize_result = False
    rc, out = run_terminal(tmp_path, monkeypatch, mod, clock, terminal)
    assert rc == mod.EXIT_INCOMPLETE and not out.exists()
    assert clock.sleeps == [300] * 6
    assert terminal.calls("initialize") == 9 and terminal.calls("shutdown") == 0
    failed = failure_alerts(tmp_path)
    assert len(failed) == 1
    assert f"{DAY} 已尝试 3 次" in failed[0][2] and f"{DAY - timedelta(days=1)} 已尝试 3 次" in failed[0][2]


@pytest.mark.parametrize("where, error_type",
                         [("the-call", RuntimeError), ("the-conversion", AttributeError)])
def test_an_unretryable_exception_in_the_first_account_read_closes_the_gateway(
        tmp_path, monkeypatch, where, error_type):
    """Second review (N1): the first account read fails in a way that is not retried —
    the call raises RuntimeError, or the row it returns is not a complete account
    (AttributeError one line later, when the row is converted). The gateway used to
    be left open (initialize 1, shutdown 0). It is closed exactly once before the
    exception goes up; nothing is retried."""
    mod, clock = load(), Clock(TASK_TIME)
    if where == "the-call":
        terminal = Terminal(failures=1, error=RuntimeError("account_info failed"))
    else:
        terminal = Terminal(failures=0)
        terminal.set_behavior("account_info", value=SimpleNamespace(login=123456))
    with pytest.raises(error_type):
        run_terminal(tmp_path, monkeypatch, mod, clock, terminal)
    assert terminal.calls("initialize") == 1 and terminal.calls("shutdown") == 1
    assert clock.sleeps == []
    failed = failure_alerts(tmp_path)
    assert len(failed) == 1 and "未预期异常" in failed[0][2] and "退出码 1" in failed[0][2]


# ============================================== K5: every failed run raises the alert

def test_usage_and_configuration_errors_are_never_retried(tmp_path, monkeypatch):
    mod, clock = load(), Clock(TASK_TIME)
    fake = FakeMT5()
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    data, reports = tmp_path / "data", tmp_path / "reports"
    (data / "live").mkdir(parents=True)
    rc = mod.main(["--data-dir", str(data), "--reports-dir", str(reports),
                   "--date", DAY.isoformat(), "--gw-config", str(tmp_path / "gateway.json")],
                  clock=clock.tick, sleep=clock.sleep)
    assert rc == 2 and clock.sleeps == [] and not reports.exists()
    assert fake.calls("initialize") == 0
    failed = failure_alerts(tmp_path)
    assert len(failed) == 1 and "配置或用法错误" in failed[0][2] and "退出码 2" in failed[0][2]


@pytest.mark.parametrize("kind", ["sessions-config-is-a-directory", "sessions-config-not-toml",
                                  "spread-model-broken", "no-mode"])
def test_configuration_errors_raise_one_alert(tmp_path, monkeypatch, kind):
    """Review R3 and R14: these used to exit 2 (or 1 with a traceback when the
    sessions config was a directory) without a line in alerts.log."""
    mod, clock = load(), Clock(TASK_TIME)
    fake = FakeMT5()
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    extra = []
    if kind != "no-mode":
        (tmp_path / "gateway.json").write_text(
            json.dumps({"allow_live_trading": False}), encoding="utf-8")
        extra += ["--demo", "--gw-config", str(tmp_path / "gateway.json")]
    if kind == "sessions-config-is-a-directory":
        (tmp_path / "sessions-dir").mkdir()
        extra += ["--sessions-config", str(tmp_path / "sessions-dir")]
    elif kind == "sessions-config-not-toml":
        (tmp_path / "sessions.toml").write_text("version = ", encoding="utf-8")
        extra += ["--sessions-config", str(tmp_path / "sessions.toml")]
    elif kind == "spread-model-broken":
        model = {"version": 2, "model": {"per_symbol": {}}, "model_sha256": "0" * 64}
        (tmp_path / "spread_model.json").write_text(json.dumps(model), encoding="utf-8")
        extra += ["--spread-model", str(tmp_path / "spread_model.json")]
    rc, out = run(tmp_path, mod, clock, None, *extra)
    assert rc == 2 and clock.sleeps == [] and not out.exists()
    assert fake.calls("initialize") == 0            # none of these ever reaches the terminal
    failed = failure_alerts(tmp_path)
    assert len(failed) == 1 and "配置或用法错误" in failed[0][2] and "退出码 2" in failed[0][2]


def test_unexpected_exceptions_raise_the_alert_and_still_crash(tmp_path):
    """Review R3 (C13) and R11 (C11): another exception in the data phase still
    ends the run with a traceback (exit code 1), but the alert is written first;
    tabs and newlines in the exception text cannot break the line format."""
    mod, clock = load(), Clock(TASK_TIME)
    gateway = FlakyGateway(0)
    def deals(start, end):
        raise RuntimeError("mapping failed\n\tsecond line")
    gateway.deals = deals
    with pytest.raises(RuntimeError):
        run(tmp_path, mod, clock, gateway)
    rows = alerts(tmp_path)
    assert len(rows) == 1
    assert len(rows[0]) == 3 and rows[0][1] == "DAILY_REPORT_FAILED"
    assert "未预期异常 RuntimeError" in rows[0][2] and "退出码 1" in rows[0][2]
    assert f"{DAY} 已尝试 1 次" in rows[0][2]
    assert f"{DAY - timedelta(days=1)} 未尝试（运行中止）" in rows[0][2]


def test_write_failures_raise_the_alert_and_still_crash(tmp_path):
    """Review R3 (C4): the reports location cannot be written. Not retried, the
    run still crashes (exit code 1), and the alert names the days left without a report."""
    mod, clock = load(), Clock(TASK_TIME)
    gateway = FlakyGateway(0, clock=clock)
    (tmp_path / "reports").write_text("a plain file where the directory should be", encoding="utf-8")
    with pytest.raises(Exception):
        run(tmp_path, mod, clock, gateway)
    assert len(gateway.touched) == 1                # the failing day only: no retry, no other day
    rows = alerts(tmp_path)
    assert len(rows) == 1 and rows[0][1] == "DAILY_REPORT_FAILED"
    assert "写盘失败" in rows[0][2] and "退出码 1" in rows[0][2]
    assert f"{DAY - timedelta(days=1)} 未尝试（运行中止）" in rows[0][2]


DEADLINE_DRIVER = '''
import importlib.util, sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

root, work = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(root))
from tests.fakes import FakeMT5


class SlowTerminal(FakeMT5):
    """The account read takes five seconds; the deadline below is half a second."""

    def __init__(self):
        super().__init__()
        self.set_behavior("account_info", value=SimpleNamespace(
            login=123456, server="Fake-Server", company="Fake-Broker", currency="USD",
            balance=10000.0, equity=10000.0, margin=0.0, margin_free=10000.0, profit=0.0,
            leverage=100, margin_mode=2, trade_mode=0), delay=5.0)


sys.modules["MetaTrader5"] = SlowTerminal()        # the real package is never imported
spec = importlib.util.spec_from_file_location("t213_deadline", root / "scripts" / "live_daily_report.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
inside_window = lambda: datetime(2026, 12, 2, 21, 57, tzinfo=timezone.utc)
sys.exit(mod.main(["--data-dir", str(work / "data"), "--reports-dir", str(work / "reports"),
                   "--date", "2026-12-02", "--demo", "--gw-config", str(work / "gateway.json")],
                  clock=inside_window, sleep=lambda seconds: None))
'''


def test_gateway_deadline_raises_the_alert_before_exit_75(tmp_path):
    """Review R3 (C12): a gateway deadline ends the process from inside the
    gateway with os._exit(75); no exception ever reaches the script. The alert
    is written by the evidence callback the gateway runs before it exits."""
    work = tmp_path / "work"
    (work / "data" / "live").mkdir(parents=True)
    (work / "gateway.json").write_text(
        json.dumps({"allow_live_trading": False, "query_timeout_s": 0.5}), encoding="utf-8")
    driver = tmp_path / "driver.py"
    driver.write_text(DEADLINE_DRIVER, encoding="utf-8")
    done = subprocess.run([sys.executable, str(driver), str(ROOT), str(work)],
                          cwd=work, capture_output=True, timeout=60)
    assert done.returncode == 75, done.stdout + done.stderr
    rows = alerts(work)
    assert len(rows) == 1 and rows[0][1] == "DAILY_REPORT_FAILED"
    assert "网关期限" in rows[0][2] and "退出码 75" in rows[0][2] and str(DAY) in rows[0][2]


# ============================================== the alert is classified in the formal check

def test_daily_report_failed_is_a_recorded_alert_in_the_formal_check(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    spec = importlib.util.spec_from_file_location("t213_demo_check", ROOT / "scripts" / "demo_check.py")
    check = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = check
    spec.loader.exec_module(check)
    assert "DAILY_REPORT_FAILED" in check.FORMAL_RECORDED_ALERTS
    assert "DAILY_REPORT_FAILED" not in check.FORMAL_ANOMALY_ALERTS
    assert "DAILY_REPORT_FAILED" not in check.FORMAL_DRILL_ALERTS


def test_guard_covers_the_daily_report_script():
    from tests.test_formal_alert_classification_guard import runtime_alert_kinds
    assert "live_daily_report.py" in runtime_alert_kinds().get("DAILY_REPORT_FAILED", set())


def test_report_path_matches_where_the_report_is_written(tmp_path):
    mod = load()
    rc, out = run(tmp_path, mod, Clock(TASK_TIME), FlakyGateway(0))
    assert rc == 0
    assert mod.report_path(str(tmp_path / "reports"), DAY) == str(out / f"daily-{DAY}.md")
