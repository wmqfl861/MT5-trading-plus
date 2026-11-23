"""T213: the daily report retries inside the rollover window, backfills recent
days and raises an alert when a report cannot be produced (plan round 23 §2.4).

The gateway is a stand-in, so nothing here touches MetaTrader5. The clock and
the sleep are injected: a retry never waits in real time.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from mt5gw.errors import NotConnectedError
from tests.fakes import FakeMT5
from tests.fakes_live import FakeBroker, FakeGateway

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 12, 2)                      # winter: the window is 21:55–23:00 UTC
SUMMER_DAY = date(2026, 7, 1)                # summer: the window is 20:55–22:00 UTC
TASK_TIME = datetime(2026, 12, 2, 21, 57)    # the scheduled moment, 05:57 local


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
    """account.state() fails `failures` times with the gateway's own disconnect error."""
    def __init__(self, failures, broker=None):
        self.inner, self.failures = FakeGateway(broker or FakeBroker()), failures
        self.account = self
        self.shutdowns = 0

    def state(self):
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
    path = tmp_path / "data" / "live" / "alerts.log"
    return [line.split("\t") for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_connection_failure_is_retried_inside_the_rollover_window(tmp_path):
    mod, clock = load(), Clock(TASK_TIME)
    rc, out = run(tmp_path, mod, clock, FlakyGateway(failures=2))
    assert rc == 0 and (out / f"daily-{DAY}.md").exists()
    assert clock.sleeps == [300, 300]          # five minutes apart, three attempts in all
    kinds = [row[1] for row in alerts(tmp_path)]
    assert "DAILY_REPORT_FAILED" not in kinds


def test_gives_up_after_three_attempts_per_day_and_raises_one_alert(tmp_path):
    """A dead connection fails every day alike: three attempts each, one alert."""
    mod, clock = load(), Clock(TASK_TIME)
    rc, out = run(tmp_path, mod, clock, FlakyGateway(failures=99))
    assert rc == mod.EXIT_INCOMPLETE and not out.exists()
    assert clock.sleeps == [300] * 6
    failed = [row for row in alerts(tmp_path) if row[1] == "DAILY_REPORT_FAILED"]
    assert len(failed) == 1
    for day in (DAY, DAY - timedelta(days=1), DAY - timedelta(days=2)):
        assert str(day) in failed[0][2], day
    assert "各已尝试 3 次" in failed[0][2]
    assert failed[0][0].startswith("2026-12-02T22:27:00")   # after the last attempt of the last day


def test_no_retry_once_the_next_attempt_would_leave_the_window(tmp_path):
    """Summer: the task starts at 21:57 UTC and the window ends at 22:00, so a
    five-minute retry would connect to the terminal outside the window (D82)."""
    mod, clock = load(), Clock(datetime(2026, 7, 1, 21, 57))
    rc, out = run(tmp_path, mod, clock, FlakyGateway(failures=1), day=SUMMER_DAY)
    assert rc == mod.EXIT_INCOMPLETE and clock.sleeps == []
    failed = [row for row in alerts(tmp_path) if row[1] == "DAILY_REPORT_FAILED"]
    assert len(failed) == 1 and "各已尝试 1 次" in failed[0][2]


def test_rollover_window_boundaries_follow_the_session_policy(tmp_path):
    mod = load()
    winter = datetime(2026, 12, 2, 21, 55), datetime(2026, 12, 2, 23, 0)
    summer = datetime(2026, 7, 1, 20, 55), datetime(2026, 7, 1, 22, 0)
    for start, end in (winter, summer):
        assert mod.in_rollover_window(mod.SessionPolicy(), start)
        assert mod.in_rollover_window(mod.SessionPolicy(), end - timedelta(seconds=1))
        assert not mod.in_rollover_window(mod.SessionPolicy(), end)
        assert not mod.in_rollover_window(mod.SessionPolicy(), start - timedelta(seconds=1))


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
    failed = [row for row in alerts(tmp_path) if row[1] == "DAILY_REPORT_FAILED"]
    assert len(failed) == 1 and str(DAY - timedelta(days=1)) in failed[0][2]


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
    assert fake.calls("initialize") == 0 and alerts(tmp_path) == []


def test_daily_report_failed_is_a_recorded_alert_in_the_formal_check():
    sys.path.insert(0, str(ROOT))
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
