"""T218 link baseline: read-only statistics over terminal journal logs.

Everything here is synthetic: no MetaTrader5, no real log directory, no event
log. The DNS counter gets a stand-in runner; the one test that reaches the real
query function points it at a directory where the tool does not exist.

Journal lines carry the local clock (UTC+8 unless a test says otherwise).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 11, 24)                    # Tuesday
ACCOUNT = "1234567890"
OTHER = "9876543210"
SERVER = "Demo-Server-X"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("t218_link_baseline", ROOT / "scripts" / "ops" / "link_baseline.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def line(clock, message, source="Network", account=ACCOUNT):
    return f"QQ\t0\t{clock}\t{source}\t'{account}': {message}\n"


def note(clock):
    """A journal line that is not a network event: it only shows the terminal was running."""
    return f"QQ\t0\t{clock}\tExperts\ttick\n"


def lost(clock):
    return line(clock, f"connection to {SERVER} lost")


def authorized(clock, ping, access="Access Point #5"):
    return line(clock, f"authorized on {SERVER} through {access} (ping: {ping} ms, build 6274)")


def switched(clock, quality, previous):
    return line(clock, f"auto connecting to a better access point with {quality} % quality (previous: {previous} %)")


def scan(clock):
    return line(clock, "scanning network for access points")


def exited(clock):
    return line(clock, f"disconnected from {SERVER}")


def write_log(log_dir, day, text):
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"{day.strftime('%Y%m%d')}.log").write_bytes(text.encode("utf-16"))


def at(text):
    """'11-24 10:05' or '2026-11-24 10:02:30' → that moment, UTC, naive."""
    return datetime.fromisoformat(text if len(text) > 2 and text[4] == "-" else "2026-" + text)


def events(*items):
    """('11-24 10:00', 'lost') or ('11-24 10:05', 'authorized', 320.0) → reading events."""
    return [(at(item[0]), item[1], item[2] if len(item) > 2 else None) for item in items]


def reading(mod, evts=(), gaps=(), last_line="2026-12-02 12:00", first_line=None):
    """A reading with the given events and journal gaps, with data from before the day to December."""
    return mod.Reading(events=evts, gaps=gaps, last_line=at(last_line), first_line=first_line)


def summary_for(mod, day, evts, **options):
    return mod.summarize(day, reading(mod, evts, **{key: options.pop(key) for key in ("gaps", "last_line", "first_line") if key in options}),
                         **options)


def run_main(mod, capsys, logs, *options):
    capsys.readouterr()
    code = mod.main(["--terminal-log-dir", str(logs), "--no-dns", *options])
    return code, capsys.readouterr().out


def weekdays(first=date(2026, 11, 1), days=45):
    """A calendar that knows nothing but weekdays, for the days a test needs to count."""
    return {(first + timedelta(days=step)).isoformat(): (first + timedelta(days=step)).weekday() < 5
            for step in range(days)}


CAL = weekdays()


def calendar_file(tmp_path, calendar=None) -> str:
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps({"schema_version": 1, "timezone": "UTC", "market": "XAUUSD",
                                "days": [{"date": name, "trading": trading, "reason": "weekday" if trading else "holiday"}
                                         for name, trading in sorted((calendar or CAL).items())]}), encoding="utf-8")
    return f"--calendar={path}"


def test_parse_events_keeps_the_network_events_that_matter(mod):
    text = "".join([
        "QQ\t0\t01:02:03.004\tTrades\tdeal #12 buy 0.01 XAUUSD\n",     # not network
        authorized("00:29:41.674", 330.62),
        line("00:29:41.674", "previous successful authorization performed from example-host on 2026.11.20 13:37:35"),
        lost("02:45:13.068"),
        switched("03:10:00.000", 47, 43),
        scan("03:45:21.304"),
        exited("19:37:27.041"),
        authorized("20:36:49.600", 334.70, access="MT5T05AP07"),
        "garbage without tabs\n"])
    assert mod.parse_events(text) == [
        (29 * 60 + 41.674, "authorized", 330.62),
        (2 * 3600 + 45 * 60 + 13.068, "lost", None),
        (3 * 3600 + 10 * 60, "ap_switch", None),
        (3 * 3600 + 45 * 60 + 21.304, "scan", None),
        (19 * 3600 + 37 * 60 + 27.041, "disconnected", None),
        (20 * 3600 + 36 * 60 + 49.6, "authorized", 334.70)]
    assert mod.parse_line_times(text)[:2] == [1 * 3600 + 2 * 60 + 3.004, 29 * 60 + 41.674]
    assert mod.parse_accounts(text) == {ACCOUNT}


@pytest.mark.parametrize("written, expected", [("330.62", 330.62), ("1 234,5", 1234.5), ("1.234,5", 1234.5), ("1,234.5", 1234.5)])
def test_authorized_lines_are_recognized_whatever_the_number_format(mod, written, expected):
    assert mod.parse_events(authorized("00:29:41.674", written)) == [(29 * 60 + 41.674, "authorized", expected)]


def test_an_authorization_without_a_ping_still_ends_the_outage(mod):
    assert mod.parse_events(line("00:29:41.674", "authorized on somewhere")) == [(29 * 60 + 41.674, "authorized", None)]
    bare = summary_for(mod, DAY, [(at("11-24 10:00"), "lost", None), (at("11-24 10:02"), "authorized", None)])
    assert (bare["disconnect_segments"], bare["unresolved_at_day_end"], bare["open_segments"]) == (1, 0, [])
    assert (bare["authorizations"], bare["ping_min_ms"]) == (1, None)


def test_segments_and_outages_follow_the_connection_not_the_log_categories(mod):
    t = [datetime(2026, 11, 24, hour, minute) for hour, minute in
         ((1, 0), (1, 2), (3, 0), (3, 5), (3, 7), (6, 0), (8, 0), (9, 0))]
    events_ = list(zip(t, ["lost", "authorized", "lost", "lost", "authorized", "lost", "disconnected", "authorized"],
                       [None] * 8))
    spans = mod.segments(events_)
    assert spans == [
        (t[0], t[1], "lost"),           # recovered
        (t[2], t[3], "lost"),           # lost again before recovering
        (t[3], t[4], "lost"),
        (t[5], t[6], "lost"),           # the link stayed down until the terminal itself went away …
        (t[6], t[7], "terminal_down"),  # … and stayed down until the terminal came back
    ]
    assert mod.outages(spans) == [[spans[0]], [spans[1], spans[2]], [spans[3], spans[4]]]
    assert mod.segments([(t[0], "lost", None)]) == [(t[0], None, "lost")]          # never recovered in the data
    assert mod.segments([(t[0], "lost", None), (t[1], "lost", None)]) == [(t[0], t[1], "lost"), (t[1], None, "lost")]
    # the terminal went away while connected; a second line saying so does not split the gap
    assert mod.segments([(t[0], "disconnected", None), (t[1], "disconnected", None), (t[2], "authorized", None)]) \
        == [(t[0], t[2], "terminal_down")]


def test_read_logs_maps_local_log_time_to_utc_across_daily_files(mod, tmp_path):
    logs = tmp_path / "logs"
    # Local date 11-25 00:30 (UTC+8) is 11-24 16:30 UTC; 11-24 07:59 local is 11-23 23:59 UTC.
    write_log(logs, date(2026, 11, 24), lost("07:59:00.000") + authorized("09:00:00.000", 300))
    write_log(logs, date(2026, 11, 25), lost("00:30:00.000") + authorized("00:31:30.000", 456.5))
    result = mod.read_logs(logs, datetime(2026, 11, 24), datetime(2026, 11, 25), 8.0)
    assert [(moment.strftime("%m-%dT%H:%M:%S"), kind, value) for moment, kind, value in result.events] == [
        ("11-24T01:00:00", "authorized", 300.0),
        ("11-24T16:30:00", "lost", None),
        ("11-24T16:31:30", "authorized", 456.5)]
    assert result.found == {"2026-11-24", "2026-11-25"} and result.unreadable == []
    # 07:59 → 09:00 local is 61 minutes, and 09:00 local → 00:30 the next day is 15.5 hours
    assert [(a.strftime("%m-%dT%H:%M"), b.strftime("%m-%dT%H:%M")) for a, b in result.gaps] == [
        ("11-23T23:59", "11-24T01:00"), ("11-24T01:00", "11-24T16:30")]
    assert (result.first_line, result.last_line) == (at("11-23T23:59:00"), at("11-24T16:31:30"))
    assert result.accounts_by_day == {"2026-11-24": {ACCOUNT}, "2026-11-25": {ACCOUNT}}
    # the same files with the clock declared as UTC-5: the day boundary moves with the clock
    shifted = mod.read_logs(logs, datetime(2026, 11, 23), datetime(2026, 11, 25), -5.0)
    assert [moment.strftime("%m-%dT%H:%M:%S") for moment, _, _ in shifted.events][:2] == ["11-24T12:59:00", "11-24T14:00:00"]
    assert mod.log_dates(logs) == [date(2026, 11, 24), date(2026, 11, 25)]


def test_decode_follows_the_bom_and_tolerates_a_file_being_written(mod):
    text = lost("10:00:00.000") + authorized("10:05:00.000", 300)
    whole = [(36000.0, "lost", None), (36300.0, "authorized", 300.0)]
    full = text.encode("utf-16")                                   # BOM + little endian
    assert mod.parse_events(mod._decode(full)) == whole
    assert mod.parse_events(mod._decode(full[:-1])) == whole       # half a character at the end
    assert mod.parse_events(mod._decode(full[:-8])) == whole       # the last line is cut short: read as far as written
    assert mod.parse_events(mod._decode(full[:-200])) == whole[:1]    # cut before "authorized on": not a recovery
    assert mod.parse_events(mod._decode(text.encode("utf-16-le"))) == whole     # no BOM: NUL bytes say UTF-16
    assert mod.parse_events(mod._decode(b"\xfe\xff" + text.encode("utf-16-be"))) == whole
    assert mod.parse_events(mod._decode(b"\xef\xbb\xbf" + text.encode("utf-8"))) == whole
    assert mod.parse_events(mod._decode(text.encode("utf-8"))) == whole          # UTF-8 without a BOM is read as UTF-8
    assert mod._decode(b"") == ""
    with pytest.raises(UnicodeError):
        mod._decode("中文".encode("gbk"))                           # neither UTF-16 nor UTF-8


def test_unresolved_outage_is_a_lower_bound_and_never_zero(mod):
    result = mod.summarize(DAY, reading(mod, events(("11-24 10:00", "lost")), last_line="11-24 10:30"), 0)
    assert result["disconnect_segments"] == 1 and result["unresolved_at_day_end"] == 0   # the day is not over in the data
    assert result["longest_s"] == 1800.0 and result["longest_is_lower_bound"] is True
    assert result["open_segments"] == [{"start_utc": "2026-11-24T10:00:00", "kind": "lost", "at_least_s": 1800.0}]
    assert result["would_fail_window"] is True and result["first_breach_utc"] == "2026-11-24T10:15:00"
    assert result["data_complete"] is False and result["baseline_day"] is False
    assert "| 1（0） | 0 | ≥1800 | **失败** |" in mod.markdown_row(result)
    # lost again before recovering: still one outage, still open, 10:00 to the end of the data
    again = mod.summarize(DAY, reading(mod, events(("11-24 10:00", "lost"), ("11-24 12:00", "lost")), last_line="11-24 12:30"), 0)
    assert (again["disconnect_segments"], again["longest_s"], again["longest_is_lower_bound"]) == (1, 9000.0, True)
    assert again["open_segments"] == [{"start_utc": "2026-11-24T10:00:00", "kind": "lost", "at_least_s": 9000.0}]
    # the data ends right after the loss: nothing is claimed about how long it lasted
    bare = mod.summarize(DAY, reading(mod, events(("11-24 10:00", "lost")), last_line="11-24 10:00"), 0)
    assert bare["open_segments"][0]["at_least_s"] == 0.0 and bare["would_fail_window"] is False
    assert bare["longest_is_lower_bound"] is True and bare["baseline_day"] is False


def test_recovery_seen_more_than_a_day_later_is_measured_and_both_days_fail(mod):
    evts = events(("11-24 23:50", "lost"), ("11-26 01:00", "authorized", 320.0))
    first = summary_for(mod, DAY, evts, last_line="11-26 12:00")
    assert (first["longest_s"], first["total_s"], first["longest_is_lower_bound"]) == (90600.0, 90600.0, False)
    assert first["unresolved_at_day_end"] == 1 and first["open_segments"] == []
    assert first["would_fail_window"] is False          # the first exit lands on Wednesday, not Tuesday
    second = summary_for(mod, date(2026, 11, 25), evts, last_line="11-26 12:00")
    assert (second["continuing_from_previous_day"], second["whole_day_in_outage"]) == (1, True)
    assert (second["longest_s"], second["longest_trading_s"]) == (86400.0, 86400.0)
    assert second["would_fail_window"] is True and second["first_breach_utc"] == "2026-11-25T00:05:00"
    # the day it finally recovered: the last hour is still an hour without a connection on a trading day
    third = summary_for(mod, date(2026, 11, 26), evts, last_line="11-26 12:00")
    assert (third["disconnect_segments"], third["continuing_from_previous_day"], third["longest_s"]) == (1, 1, 3600.0)
    assert third["would_fail_window"] is True and third["first_breach_utc"] == "2026-11-26T00:15:00"
    assert third["baseline_day"] is False


def test_a_whole_trading_day_inside_an_outage_is_reported_on_that_day(mod):
    evts = events(("11-22 23:00", "lost"), ("11-24 00:10", "authorized", 320.0))       # Sunday → Tuesday
    sunday = summary_for(mod, date(2026, 11, 22), evts, last_line="11-24 12:00")
    assert (sunday["trading_day"], sunday["longest_s"], sunday["would_fail_window"]) == (False, 90600.0, False)
    assert sunday["longest_trading_s"] == 0.0           # the Sunday row does not borrow Monday's hours
    monday = summary_for(mod, date(2026, 11, 23), evts, last_line="11-24 12:00")
    assert (monday["disconnect_segments"], monday["continuing_from_previous_day"]) == (1, 1)
    assert (monday["whole_day_in_outage"], monday["unresolved_at_day_end"], monday["longest_s"]) == (True, 1, 86400.0)
    assert monday["would_fail_window"] is True and monday["first_breach_utc"] == "2026-11-23T00:15:00"
    assert monday["baseline_day"] is False
    # Tuesday: the last ten minutes of the outage reach into a trading day and stop short of 900 s
    tuesday = summary_for(mod, DAY, evts, last_line="11-24 12:00")
    assert (tuesday["would_fail_window"], tuesday["tails_into_trading_day"]) == (False, 1)
    assert (tuesday["longest_s"], tuesday["baseline_day"]) == (600.0, False)


def test_a_terminal_exit_in_the_middle_of_an_outage_keeps_it_one_outage(mod):
    evts = events(("11-24 10:00", "lost"), ("11-24 10:05", "disconnected"), ("11-24 18:00", "authorized", 320.0))
    summary = summary_for(mod, DAY, evts)
    assert (summary["disconnect_segments"], summary["lost_segments"], summary["terminal_down_segments"]) == (1, 1, 0)
    assert (summary["longest_s"], summary["total_s"], summary["terminal_down_s"]) == (28800.0, 28800.0, 28500.0)
    assert summary["would_fail_window"] is True and summary["first_breach_utc"] == "2026-11-24T10:15:00"
    # the terminal went away while it was connected: an outage of its own
    away = summary_for(mod, DAY, events(("11-24 10:00", "disconnected"), ("11-24 10:20", "authorized", 320.0)))
    assert (away["disconnect_segments"], away["terminal_down_segments"], away["longest_s"]) == (1, 1, 1200.0)
    assert away["would_fail_window"] is True and away["first_breach_utc"] == "2026-11-24T10:15:00"


def outage(start, end):
    return events((start, "lost"), (end, "authorized", 320.0))


@pytest.mark.parametrize("span, calendar, day, fails, breach", [
    (("11-24 22:05", "11-24 22:40"), None, DAY, True, "2026-11-24T22:20:00"),
    (("11-27 21:50", "11-28 01:00"), None, date(2026, 11, 27), True, "2026-11-27T22:05:00"),
    (("11-27 16:00", "11-29 23:30"), None, date(2026, 11, 27), True, "2026-11-27T16:15:00"),
    (("11-27 16:00", "11-29 23:30"), None, date(2026, 11, 28), False, None),     # Saturday: nothing is owed
    (("11-28 23:00", "11-29 23:00"), None, date(2026, 11, 28), False, None),
    (("11-28 23:00", "11-29 23:00"), None, date(2026, 11, 29), False, None),
    (("11-29 23:50", "11-30 00:20"), None, date(2026, 11, 29), False, None),
    (("11-29 23:50", "11-30 00:20"), None, date(2026, 11, 30), True, "2026-11-30T00:05:00"),
    (("11-27 21:50", "11-28 01:00"), {"2026-11-27": False}, date(2026, 11, 27), False, None),
    # the outage runs on into the next trading day: that day fails on its own
    (("11-23 23:30", "11-24 10:00"), None, date(2026, 11, 23), True, "2026-11-23T23:45:00"),
    (("11-23 23:30", "11-24 10:00"), None, DAY, True, "2026-11-24T00:15:00"),
    (("11-23 22:00", "11-24 00:30"), None, date(2026, 11, 23), True, "2026-11-23T22:15:00"),
    (("11-23 22:00", "11-24 00:30"), None, DAY, True, "2026-11-24T00:15:00"),
    (("11-24 10:00", "11-25 10:00"), None, DAY, True, "2026-11-24T10:15:00"),
    (("11-24 10:00", "11-25 10:00"), None, date(2026, 11, 25), True, "2026-11-25T00:15:00"),
    (("11-27 23:30", "11-30 10:00"), {"2026-11-27": False}, date(2026, 11, 30), True, "2026-11-30T00:15:00"),
    (("11-27 23:30", "11-30 10:00"), {"2026-11-27": False}, date(2026, 11, 27), False, None),   # holiday evening
])
def test_window_rule_follows_the_formal_check(mod, span, calendar, day, fails, breach):
    """The exit with rc=78 fails the window when its moment falls on a calendar trading day."""
    summary = summary_for(mod, day, outage(*span), calendar=calendar)
    assert summary["would_fail_window"] is fails, summary
    assert summary["first_breach_utc"] == breach
    assert summary["trading_day"] is (calendar.get(day.isoformat(), day.weekday() < 5) if calendar else day.weekday() < 5)
    assert summary["calendar"] == ("file" if calendar else "weekday")
    assert not fails or summary["baseline_day"] is False
    # a segment that fails the day is neither a tail nor a near miss (N-5)
    assert not fails or (summary["tails_into_trading_day"] == 0 and summary["near_breach_segments"] == 0)


def test_outages_near_the_line_are_counted_apart_and_keep_the_day_out_of_the_baseline(mod):
    just_under = summary_for(mod, DAY, outage("11-24 10:00", "11-24 10:14:30"))
    assert (just_under["would_fail_window"], just_under["near_breach_segments"]) == (False, 1)
    assert just_under["baseline_day"] is False                # the process may have reached 900 s: not ours to call
    exactly = summary_for(mod, DAY, outage("11-24 10:00", "11-24 10:15:00"))
    assert (exactly["longest_s"], exactly["would_fail_window"], exactly["near_breach_segments"]) == (900.0, True, 0)
    finer = [(at("11-24 10:00"), "lost", None), (at("11-24 10:00") + timedelta(seconds=899.96), "authorized", 300.0)]
    almost = summary_for(mod, DAY, finer)
    assert (almost["longest_s"], almost["would_fail_window"]) == (899.9, False)      # not rounded up to the line
    comfortable = summary_for(mod, DAY, outage("11-24 10:00", "11-24 10:10:00"), calendar=CAL)
    assert (comfortable["near_breach_segments"], comfortable["tails_into_trading_day"], comfortable["baseline_day"]) == (0, 0, True)
    # started before midnight, back 5 minutes into the trading day: reported, not called a failure, not in the baseline
    tail = summary_for(mod, date(2026, 11, 30), outage("11-29 23:40", "11-30 00:05"), calendar=CAL)
    assert (tail["would_fail_window"], tail["tails_into_trading_day"], tail["continuing_from_previous_day"]) == (False, 1, 1)
    assert (tail["longest_s"], tail["longest_trading_s"], tail["baseline_day"]) == (300.0, 300.0, False)
    # the near miss belongs to the day the exit would have landed on, not to the day the outage began
    evening = summary_for(mod, date(2026, 11, 23), outage("11-23 23:52", "11-24 00:06:30"), calendar=CAL)
    assert (evening["near_breach_segments"], evening["tails_into_trading_day"], evening["baseline_day"]) == (0, 0, True)
    next_day = summary_for(mod, DAY, outage("11-23 23:52", "11-24 00:06:30"), calendar=CAL)
    assert (next_day["near_breach_segments"], next_day["tails_into_trading_day"]) == (1, 0)    # would have exited at 00:07 on Tuesday
    assert next_day["baseline_day"] is False


def test_exit_code_separates_no_log_unreadable_and_a_quiet_day(mod, tmp_path, capsys, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    code, out = run_main(mod, capsys, logs, "--date", "2026-11-24")
    assert (code, json.loads(out)) == (3, {"date_utc": "2026-11-24", "error": "no_terminal_log"})
    code, out = run_main(mod, capsys, tmp_path / "nowhere", "--date", "2026-11-24")
    assert (code, json.loads(out)) == (3, {"date_utc": "2026-11-24", "error": "log_dir_unreadable"})
    # the terminal was there the whole day and never re-connected: a normal result, exit 0
    write_log(logs, date(2026, 11, 23), scan("21:00:00.000"))                     # a line before the day began
    write_log(logs, date(2026, 11, 24), scan("09:00:00.000") + scan("15:00:00.000") + scan("21:00:00.000"))
    write_log(logs, date(2026, 11, 25), scan("03:00:00.000") + scan("09:00:00.000"))
    code, out = run_main(mod, capsys, logs, "--date", "2026-11-24", calendar_file(tmp_path))
    summary = json.loads(out)
    assert code == 0 and (summary["disconnect_segments"], summary["scans"]) == (0, 4)
    assert (summary["terminal_absent"], summary["data_complete"], summary["baseline_day"]) == (False, True, True)
    # the same healthy day without a calendar: not counted, and the table says why
    code, out = run_main(mod, capsys, logs, "--date", "2026-11-24", "--format", "table")
    assert code == 0 and out.rstrip().endswith("| **不计入**（没有日历） |"), out
    # a file that cannot be read: said so, without the path or the operating system's text
    real = Path.read_bytes
    def denied(self):
        if self.name == "20261124.log":
            raise PermissionError(13, "Permission denied")
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", denied)
    code, out = run_main(mod, capsys, logs, "--date", "2026-11-24")
    assert code == 3 and json.loads(out) == {"date_utc": "2026-11-24", "error": "log_unreadable"}
    assert "Permission" not in out and str(logs) not in out
    monkeypatch.undo()
    (logs / "20261126.log").write_bytes(b"\x00\xd8" * 40)               # not UTF-16: a lone surrogate
    code, out = run_main(mod, capsys, logs, "--date", "2026-11-25")
    assert code == 3 and json.loads(out)["error"] == "log_unreadable"              # the 26th belongs to that UTC day


def test_terminal_absence_and_incomplete_data_are_told_apart(mod, tmp_path, capsys):
    # a) the journal stops at 09:00 local and resumes 35 hours later: the terminal was not there
    logs = tmp_path / "absent"
    write_log(logs, date(2026, 11, 23), note("21:00:00.000"))
    write_log(logs, date(2026, 11, 24), note("08:30:00.000") + scan("09:00:00.000"))
    write_log(logs, date(2026, 11, 25), note("20:00:00.000") + scan("20:30:00.000"))
    code, out = run_main(mod, capsys, logs, "--date", "2026-11-24")
    summary = json.loads(out)
    assert code == 0 and (summary["terminal_absent"], summary["data_complete"]) == (True, True)
    assert summary["max_silent_gap_s"] == 82800.0 and summary["baseline_day"] is False
    assert "**不在**" in mod.markdown_row(summary)
    # b) the terminal was running and logging, but away from the server for ten hours
    logs = tmp_path / "away"
    write_log(logs, date(2026, 11, 24), note("08:30:00.000") + authorized("08:45:00.000", 300) + note("14:30:00.000")
              + exited("18:00:00.000") + note("20:30:00.000"))
    write_log(logs, date(2026, 11, 25), note("02:30:00.000") + authorized("04:00:00.000", 300) + note("08:30:00.000"))
    summary = json.loads(run_main(mod, capsys, logs, "--date", "2026-11-24")[1])
    assert (summary["terminal_absent"], summary["terminal_down_segments"], summary["longest_s"]) == (True, 1, 36000.0)
    assert summary["would_fail_window"] is True and summary["first_breach_utc"] == "2026-11-24T10:15:00"
    # c) the data simply stops in the middle of the day: not "absent", just not finished
    logs = tmp_path / "short"
    write_log(logs, date(2026, 11, 24), note("08:30:00.000") + note("12:00:00.000"))
    write_log(logs, date(2026, 11, 25), "")                                              # today's file, nothing in it yet
    summary = json.loads(run_main(mod, capsys, logs, "--date", "2026-11-24")[1])
    assert (summary["data_complete"], summary["terminal_absent"], summary["baseline_day"]) == (False, False, False)
    assert summary["observed_until"] == "2026-11-24T04:00:00"
    # d) one of the day's two files is missing: cannot tell, and says so instead of guessing
    logs = tmp_path / "half"
    write_log(logs, date(2026, 11, 25), note("08:30:00.000"))
    code, out = run_main(mod, capsys, logs, "--date", "2026-11-24", "--format", "table")
    assert code == 0 and "判断不了（缺 2026-11-24 的日志）" in out and "**不计入**" in out


def test_a_busy_journal_does_not_look_like_an_absent_terminal(mod, tmp_path, capsys):
    """A line every minute all day leaves no recorded gap at all: that must mean "present"."""
    logs = tmp_path / "busy"
    day = datetime(2026, 11, 24, 8, 0)
    write_log(logs, date(2026, 11, 23), note("21:00:00.000"))                      # a line before the day began
    write_log(logs, date(2026, 11, 24), "".join(note((day + timedelta(minutes=step)).strftime("%H:%M:%S.000"))
                                                for step in range(16 * 60)))              # 08:00 … 23:59 local
    write_log(logs, date(2026, 11, 25), "".join(note((day + timedelta(minutes=step)).strftime("%H:%M:%S.000"))
                                                for step in range(16 * 60, 24 * 60))     # 00:00 … 07:59 local
              + note("08:30:00.000"))                                           # and a line after the day is over
    result = mod.read_logs(logs, datetime(2026, 11, 24), datetime(2026, 11, 25), 8.0)
    # a line every minute from 08:00 to 07:59: no gap of five minutes anywhere inside the day
    assert mod.longest_silent_gap(datetime(2026, 11, 24), datetime(2026, 11, 25), result.gaps) is None
    summary = json.loads(run_main(mod, capsys, logs, "--date", "2026-11-24", calendar_file(tmp_path))[1])
    assert (summary["terminal_absent"], summary["max_silent_gap_s"], summary["baseline_day"]) == (False, None, True)
    assert "在（日志行相隔不到 5 分钟）" in mod.markdown_row(summary)
    # eight solid hours of lines every minute, quiet around them: still present
    logs = tmp_path / "partly-busy"
    write_log(logs, date(2026, 11, 24), "".join(
        note((datetime(2026, 11, 24, 8) + timedelta(minutes=step)).strftime("%H:%M:%S.000")) for step in range(8 * 60))
        + note("21:00:00.000"))
    write_log(logs, date(2026, 11, 25), note("01:00:00.000") + note("07:30:00.000") + note("08:30:00.000"))
    summary = json.loads(run_main(mod, capsys, logs, "--date", "2026-11-24", calendar_file(tmp_path))[1])
    assert (summary["terminal_absent"], summary["baseline_day"], summary["max_silent_gap_s"]) == (False, True, 6.5 * 3600.0)


def test_days_whose_own_files_hold_no_lines_are_not_mistaken_for_healthy_ones(mod, tmp_path, capsys):
    # both of the day's files exist but are empty; the journal resumes the day after: absent all day
    logs = tmp_path / "blank"
    write_log(logs, date(2026, 11, 23), note("23:00:00.000"))
    write_log(logs, date(2026, 11, 24), "")
    write_log(logs, date(2026, 11, 25), "")
    write_log(logs, date(2026, 11, 26), note("01:00:00.000"))
    summary = json.loads(run_main(mod, capsys, logs, "--date", "2026-11-24")[1])
    assert (summary["terminal_absent"], summary["data_complete"], summary["max_silent_gap_s"]) == (True, True, 86400.0)
    assert (summary["disconnect_segments"], summary["longest_s"], summary["baseline_day"]) == (0, 0.0, False)
    # lines, but none with a timestamp we can read, and the data ends before the day: nothing is claimed
    logs = tmp_path / "untimed"
    write_log(logs, date(2026, 11, 23), lost("23:50:00.000"))
    write_log(logs, date(2026, 11, 24), "no tabs here\n" * 5)
    write_log(logs, date(2026, 11, 25), "no tabs here\n" * 5)
    summary = json.loads(run_main(mod, capsys, logs, "--date", "2026-11-24")[1])
    assert (summary["disconnect_segments"], summary["longest_s"], summary["data_complete"]) == (1, 0.0, False)
    # the outage opened the evening before is still listed as open, with nothing claimed about its length
    assert summary["open_segments"] == [{"start_utc": "2026-11-23T15:50:00", "kind": "lost", "at_least_s": 0.0}]
    assert summary["observed_until"] == "2026-11-23T15:50:00" and summary["would_fail_window"] is False


def test_dns_timeouts_counts_events_and_reports_what_it_cannot_read(mod, tmp_path):
    def xml(moment):
        return f"<Event><System><TimeCreated SystemTime='{moment}Z'/></System></Event>"
    seen = []
    def make(oldest, newest, count):
        def run(system_root, *args):
            seen.append((system_root, args))
            if "/rd:false" in args:                      # the oldest event in the log
                return xml(oldest)
            if "/c:1" in args:                           # the newest event since the day began
                return xml(newest)
            return "<Event " * count
        return run
    covered = make("2023-05-11T11:49:12.000", "2026-11-26T00:00:00.000", 2)
    assert mod.dns_timeouts(DAY, system_root=r"C:\Windows", run=covered) == 2
    assert [system_root for system_root, _ in seen] == [r"C:\Windows"] * 3
    assert seen[0][1] == ("/q:*[System[TimeCreated[@SystemTime<'2026-11-25T00:00:00.000Z']]]", "/c:1", "/rd:false", "/f:xml")
    assert seen[1][1][0] == "/q:*[System[TimeCreated[@SystemTime>='2026-11-24T00:00:00.000Z']]]"
    query = seen[2][1][0]                               # /q:<XPath>
    assert "@SystemTime>='2026-11-24T00:00:00.000Z'" in query and "@SystemTime<'2026-11-25T00:00:00.000Z'" in query
    assert "EventID=1014" in query and "Microsoft-Windows-DNS-Client" in query
    # the System log only started at 11:49 that day (the real log's oldest event): not covered
    assert mod.dns_timeouts(date(2023, 5, 11), run=make("2023-05-11T11:49:12.000", "2023-05-20T00:00:00.000", 0)) is None
    # it starts at midnight: covered, and a real zero
    assert mod.dns_timeouts(date(2023, 5, 12), run=make("2023-05-11T11:49:12.000", "2023-05-20T00:00:00.000", 0)) == 0
    # cleared at 23:50 that evening and recording resumed later: nothing is claimed about the hours before
    assert mod.dns_timeouts(DAY, run=make("2023-05-11T11:49:12.000", "2026-11-24T23:50:00.000", 0)) is None
    assert mod.dns_timeouts(DAY, run=make("2026-11-24T12:00:00.000", "2026-11-26T00:00:00.000", 2)) is None   # began mid-day
    assert mod.dns_timeouts(date(2027, 1, 1), run=make("2023-05-11T11:49:12.000", "2026-11-26T00:00:00.000", 0)) is None   # not yet
    assert mod.dns_timeouts(DAY, run=make("2023-05-11T11:49:12.000", "2026-11-26T00:00:00.000", 10001)) is None   # at the cap
    assert mod.dns_timeouts(DAY, run=lambda *args: None) is None         # the query failed
    assert mod.dns_timeouts(DAY, run=lambda *args: "") is None           # the log is empty
    # the real query function with nowhere to find the tool: None, not an exception
    assert mod.dns_timeouts(DAY, system_root=str(tmp_path / "no-system-root")) is None


def test_cli_output_carries_counts_only(mod, tmp_path, capsys):
    logs = tmp_path / "logs"
    write_log(logs, date(2026, 11, 23), note("21:00:00.000"))                      # a line before the day began
    # Log clocks are local (UTC+8): the UTC day 11-24 runs from 08:00 local on 11-24.
    write_log(logs, date(2026, 11, 24), note("08:30:00.000") + authorized("09:00:00.000", 321.4) + note("14:30:00.000")
              + lost("18:00:00.000") + authorized("18:09:00.000", 455.0) + note("20:30:00.000")
              + line("18:09:00.000", "previous successful authorization performed from example-host on 2026.11.24 02:00:00"))
    write_log(logs, date(2026, 11, 25), note("02:30:00.000") + note("08:30:00.000"))
    for secrets in ((ACCOUNT, SERVER, "Access Point", "example-host"), (str(logs),)):
        for format_ in ("json", "table"):
            code, out = run_main(mod, capsys, logs, "--date", "2026-11-24", "--format", format_)
            assert code == 0 and all(secret not in out for secret in secrets), (secrets, format_)
    code, out = run_main(mod, capsys, logs, "--date", "2026-11-24", calendar_file(tmp_path))
    summary = json.loads(out)
    assert (summary["disconnect_segments"], summary["longest_s"]) == (1, 540.0)
    assert (summary["dns_timeouts"], summary["ping_min_ms"], summary["baseline_day"]) == (None, 321.4, True)


def test_cli_range_gives_one_line_per_day_and_flags_the_missing_one(mod, tmp_path, capsys):
    logs = tmp_path / "logs"
    write_log(logs, date(2026, 11, 24), note("08:30:00.000") + note("20:30:00.000"))
    write_log(logs, date(2026, 11, 26), note("08:30:00.000") + note("20:30:00.000"))
    code, out = run_main(mod, capsys, logs, "--from", "2026-11-24", "--to", "2026-11-27")
    rows = [json.loads(text) for text in out.splitlines()]
    assert code == 3 and [row["date_utc"] for row in rows] == ["2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27"]
    assert rows[3] == {"date_utc": "2026-11-27", "error": "no_terminal_log"}
    assert rows[0]["missing_log_days"] == ["2026-11-25"] and rows[1]["missing_log_days"] == ["2026-11-25"]
    assert rows[2]["missing_log_days"] == ["2026-11-27"] and rows[2]["terminal_absent"] is None
    code, out = run_main(mod, capsys, logs, "--from", "2026-11-24", "--to", "2026-11-27", "--format", "table")
    lines = out.splitlines()
    assert code == 3 and lines[0].startswith("| UTC 日 | 交易日 | 断线段")
    assert len(lines) == 6 and out.count("判断不了（缺 2026-11-25 的日志）") == 2
    assert all(line.count("|") == 14 for line in lines), lines      # every row has all 13 columns


def test_cli_rejects_impossible_arguments(mod, tmp_path, capsys):
    logs = tmp_path / "logs"
    write_log(logs, date(2026, 11, 24), note("08:30:00.000"))
    bad = (["--date", "2026-11-24", "--log-utc-offset-hours", value] for value in ("nan", "inf", "1e9", "25"))
    for options in [*bad, ["--date", "2026-11-24", "--lookback-days", "-1"], ["--date", "2026-13-45"],
                    ["--from", "2026-11-24"], ["--date", "2026-11-24", "--to", "2026-11-26"],
                    ["--from", "2026-11-26", "--to", "2026-11-24"], ["--date", "2026-11-24", "--absent-gap-s", "0"]]:
        with pytest.raises(SystemExit) as stopped:
            mod.main(["--terminal-log-dir", str(logs), "--no-dns", *options])
        assert stopped.value.code == 2, options


def test_default_day_is_yesterday_in_utc(mod, tmp_path, capsys, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 23, 19, 0, tzinfo=timezone.utc)     # 03:00 local on the 24th
    monkeypatch.setattr(mod, "datetime", Clock)
    logs = tmp_path / "logs"
    write_log(logs, date(2026, 11, 22), note("08:30:00.000") + note("20:30:00.000"))
    write_log(logs, date(2026, 11, 23), note("08:30:00.000") + note("20:30:00.000"))
    code, out = run_main(mod, capsys, logs)
    assert code == 0 and json.loads(out)["date_utc"] == "2026-11-22"       # not the local calendar's yesterday


def test_accounts_are_counted_for_the_day_only_and_never_printed(mod, tmp_path, capsys):
    logs = tmp_path / "logs"
    write_log(logs, date(2026, 11, 23), line("20:00:00.000", "authorized on x (ping: 1 ms)", account=OTHER))
    write_log(logs, date(2026, 11, 24), note("08:30:00.000") + lost("09:00:00.000")
              + line("09:02:00.000", "authorized on x (ping: 1 ms)", account=OTHER) + note("20:30:00.000"))
    write_log(logs, date(2026, 11, 25), note("08:30:00.000"))
    code, out = run_main(mod, capsys, logs, "--date", "2026-11-24")
    summary = json.loads(out)
    assert code == 0 and summary["accounts_seen"] == 2                     # more than one: pairing may cross accounts
    assert ACCOUNT not in out and OTHER not in out
    write_log(logs, date(2026, 11, 24), lost("09:00:00.000") + authorized("09:02:00.000", 300) + note("20:30:00.000"))
    assert json.loads(run_main(mod, capsys, logs, "--date", "2026-11-24")[1])["accounts_seen"] == 1   # the other account is yesterday's


def test_calendar_comes_from_the_formal_check_file_or_its_source(mod, tmp_path):
    formal = tmp_path / "calendar.json"
    formal.write_text(json.dumps({"schema_version": 1, "timezone": "UTC",
                                  "days": [{"date": "2026-11-26", "trading": False, "reason": "holiday"},
                                           {"date": "2026-11-27", "trading": True, "reason": "weekday"}]}), encoding="utf-8")
    source = tmp_path / "calendar.txt"
    source.write_text("XAUUSD calendar\n2026-11-26 holiday Thanksgiving closure\n2026-11-27 weekday\n", encoding="utf-8-sig")
    for path in (formal, source):
        calendar = mod.load_calendar(path)
        assert calendar == {"2026-11-26": False, "2026-11-27": True}
        evts = outage("11-26 10:00", "11-26 12:00")
        summary = summary_for(mod, date(2026, 11, 26), evts, calendar=calendar)
        assert (summary["trading_day"], summary["would_fail_window"], summary["longest_s"]) == (False, False, 7200.0)
        assert (summary["calendar"], summary["calendar_missing_days"]) == ("file", [])
        assert summary_for(mod, date(2026, 11, 26), evts)["would_fail_window"] is True     # weekdays only: stricter
    # the calendar stops before the day asked about: weekdays are assumed and the gap is named
    later = summary_for(mod, DAY, outage("11-24 10:00", "11-24 12:00"), calendar={"2026-11-26": False})
    assert (later["calendar"], later["calendar_missing_days"], later["trading_day"]) == ("file", ["2026-11-24"], True)
    assert "是（日历缺 1 天）" in mod.markdown_row(later)
    empty = tmp_path / "empty.txt"
    empty.write_text("no dated lines here\n", encoding="utf-8")
    with pytest.raises(ValueError):
        mod.load_calendar(empty)


def test_markdown_row_shows_open_outages_presence_and_the_verdict(mod):
    evts = events(("11-24 00:59", "lost"), ("11-24 01:03", "authorized", 350.0), ("11-24 10:00", "lost"),
                  ("11-24 10:02:30", "authorized", 400.0), ("11-24 12:00", "ap_switch"), ("11-24 12:30", "scan"),
                  ("11-24 23:58", "lost"), ("11-25 00:04", "authorized", 320.0), ("11-25 01:00", "scan"))
    summary = summary_for(mod, DAY, evts, dns=7, calendar=CAL)
    assert (summary["disconnect_segments"], summary["continuing_from_previous_day"], summary["unresolved_at_day_end"]) == (3, 0, 1)
    assert (summary["longest_s"], summary["total_s"], summary["authorizations"], summary["ping_min_ms"]) == (360.0, 750.0, 2, 350.0)
    assert (summary["longest_trading_s"], summary["baseline_day"], summary["open_segments"]) == (360.0, True, [])
    assert mod.markdown_row(summary) == ("| 2026-11-24 | 是 | 3（0） | 1 | 360 | 不失败 | — | 在（日志行相隔不到 5 分钟） | "
                                         "350–400 | 1 | 1 | 7 | 计入 |")
    # the same day, but the first outage began the evening before: a three-minute tail into the trading day
    tail = summary_for(mod, DAY, events(("11-23 23:59", "lost"), ("11-24 00:03", "authorized", 350.0),
                                        ("11-24 10:00", "lost"), ("11-24 10:02:30", "authorized", 400.0)))
    assert (tail["continuing_from_previous_day"], tail["tails_into_trading_day"], tail["longest_trading_s"]) == (1, 1, 180.0)
    assert (tail["longest_s"], tail["total_s"], tail["baseline_day"]) == (180.0, 330.0, False)
    assert "| 180 | 不失败 | 0/1 |" in mod.markdown_row(tail)
    away = dict(summary, terminal_absent=True, baseline_day=False, dns_timeouts=None)
    assert mod.markdown_row(away).endswith("| **不在** | 350–400 | 1 | 1 | 未查 | **不计入** |")
    unknown = dict(summary, terminal_absent=None, missing_log_days=["2026-11-25"])
    assert "| 判断不了（缺 2026-11-25 的日志） |" in mod.markdown_row(unknown)
    edges = dict(summary, near_breach_segments=1, tails_into_trading_day=2, baseline_day=False)
    assert "| 不失败 | 1/2 |" in mod.markdown_row(edges)
    quiet = dict(summary, gaps=None, max_silent_gap_s=12600.0)
    assert "在（最长空档 3.5 小时）" in mod.markdown_row(quiet)


# ------------------------------------------------- 第二次复核（M-1、M-3 等）

def six_hourly(day):
    """Four journal lines a day: the terminal was there."""
    return "".join(note(f"{clock}:00.000") for clock in ("02:30", "08:30", "14:30", "20:30"))


def test_an_unreadable_file_does_not_hide_the_silence_that_follows_it(mod, tmp_path, capsys, monkeypatch):
    """Local 11-25 cannot be read and 11-26 has no line until 20:00: the first half of UTC 11-26 has no line anywhere."""
    logs = tmp_path / "logs"
    write_log(logs, date(2026, 11, 23), note("21:00:00.000"))
    write_log(logs, date(2026, 11, 24), six_hourly(date(2026, 11, 24)))
    write_log(logs, date(2026, 11, 25), six_hourly(date(2026, 11, 25)))
    write_log(logs, date(2026, 11, 26), note("20:00:00.000") + note("23:00:00.000"))
    write_log(logs, date(2026, 11, 27), note("08:30:00.000"))
    real = Path.read_bytes
    def denied(self):
        if self.name == "20261125.log":
            raise PermissionError(13, "Permission denied")
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", denied)
    calendar = calendar_file(tmp_path)
    code, out = run_main(mod, capsys, logs, "--from", "2026-11-24", "--to", "2026-11-26", calendar)
    rows = [json.loads(text) for text in out.splitlines()]
    assert code == 3 and [row.get("error") for row in rows] == ["log_unreadable", "log_unreadable", None]
    after = rows[2]                                       # 11-26: readable files of its own, but silent until 12:00 UTC
    assert (after["terminal_absent"], after["data_complete"], after["baseline_day"]) == (True, True, False)
    assert after["max_silent_gap_s"] == 43200.0           # 23:00 local on the 24th → 20:00 local on the 26th, cut at the day
    # the same files with the 25th readable: its last line (20:30 local) is where the silence starts
    monkeypatch.undo()
    after = json.loads(run_main(mod, capsys, logs, "--date", "2026-11-26", calendar)[1])
    assert (after["terminal_absent"], after["max_silent_gap_s"]) == (True, 43200.0)     # silence from 20:30 local on the 25th, cut at the day


def test_a_day_with_no_line_at_all_is_absent_or_unknown_never_present(mod, tmp_path, capsys):
    logs = tmp_path / "blank"
    write_log(logs, date(2026, 11, 24), "")
    write_log(logs, date(2026, 11, 25), "")
    summary = json.loads(run_main(mod, capsys, logs, "--date", "2026-11-24")[1])
    assert (summary["terminal_absent"], summary["data_complete"], summary["baseline_day"]) == (None, False, False)
    write_log(logs, date(2026, 11, 26), note("01:00:00.000"))           # the first line of all comes the day after
    summary = json.loads(run_main(mod, capsys, logs, "--date", "2026-11-24")[1])
    assert (summary["terminal_absent"], summary["data_complete"], summary["max_silent_gap_s"]) == (True, False, 86400.0)


def test_a_day_counts_only_with_a_calendar_that_covers_it_and_one_account(mod, tmp_path, capsys):
    logs = tmp_path / "logs"
    write_log(logs, date(2026, 11, 23), six_hourly(date(2026, 11, 23)))
    write_log(logs, date(2026, 11, 24), six_hourly(date(2026, 11, 24)))
    write_log(logs, date(2026, 11, 25), six_hourly(date(2026, 11, 25)))
    healthy = json.loads(run_main(mod, capsys, logs, "--date", "2026-11-24", calendar_file(tmp_path))[1])
    assert (healthy["trading_day"], healthy["baseline_day"], healthy["calendar_missing_days"]) == (True, True, [])
    holiday = dict(CAL, **{"2026-11-24": False})
    off = json.loads(run_main(mod, capsys, logs, "--date", "2026-11-24", calendar_file(tmp_path, holiday))[1])
    assert (off["trading_day"], off["baseline_day"]) == (False, False)       # a day the market is closed is no baseline day
    # the calendar that does not reach the day: weekdays are assumed, the gap is named, the day does not count
    blind = json.loads(run_main(mod, capsys, logs, "--date", "2026-11-24", calendar_file(tmp_path, {"2026-11-23": True}))[1])
    assert (blind["trading_day"], blind["calendar_missing_days"], blind["baseline_day"]) == (True, ["2026-11-24"], False)
    out = run_main(mod, capsys, logs, "--date", "2026-11-24", calendar_file(tmp_path, {"2026-11-23": True}), "--format", "table")[1]
    assert "| 是（日历缺 1 天） |" in out and out.rstrip().endswith("| **不计入**（日历没覆盖到） |")
    # two accounts in the day's journal: outages may have been paired across accounts
    write_log(logs, date(2026, 11, 24), six_hourly(date(2026, 11, 24)) + scan("09:00:00.000")
              + line("10:00:00.000", "authorized on x (ping: 1 ms)", account=OTHER))
    many = json.loads(run_main(mod, capsys, logs, "--date", "2026-11-24", calendar_file(tmp_path))[1])
    assert (many["accounts_seen"], many["baseline_day"]) == (2, False)
    out = run_main(mod, capsys, logs, "--date", "2026-11-24", calendar_file(tmp_path), "--format", "table")[1]
    assert out.rstrip().endswith("| **不计入**（日志里有多个账号） |")


def test_calendar_gaps_are_listed_only_where_they_can_affect_the_day(mod):
    summary = summary_for(mod, DAY, outage("11-20 10:00", "11-24 12:00"), calendar={"2026-11-24": True})
    assert (summary["would_fail_window"], summary["calendar_missing_days"]) == (True, ["2026-11-23"])


def test_an_error_only_table_still_has_its_header(mod, tmp_path, capsys):
    code, out = run_main(mod, capsys, tmp_path / "nowhere", "--date", "2026-11-24", "--format", "table")
    lines = out.splitlines()
    assert code == 3 and lines[0] == mod.TABLE_HEAD and len(lines) == 3
    assert lines[2].startswith("| 2026-11-24 | — | log_dir_unreadable |")


def test_the_last_baseline_day_needs_the_next_local_date_file(mod, tmp_path, capsys):
    logs = tmp_path / "logs"
    for day in (date(2026, 11, 23), date(2026, 12, 1), date(2026, 12, 2)):
        write_log(logs, day, six_hourly(day))
    summary = json.loads(run_main(mod, capsys, logs, "--date", "2026-12-02", calendar_file(tmp_path))[1])
    assert (summary["missing_log_days"], summary["terminal_absent"], summary["baseline_day"]) == (["2026-12-03"], None, False)
    write_log(logs, date(2026, 12, 3), six_hourly(date(2026, 12, 3)))      # the second half of UTC 12-02 lives in this file
    summary = json.loads(run_main(mod, capsys, logs, "--date", "2026-12-02", calendar_file(tmp_path))[1])
    assert (summary["missing_log_days"], summary["terminal_absent"], summary["baseline_day"]) == ([], False, True)


# ------------------------------------------------- 第三次复核（P-1、P-3）

def test_days_after_an_unreadable_file_do_not_count_until_the_connection_is_seen_again(mod, tmp_path, capsys, monkeypatch):
    """The unreadable file may hold an outage that runs on into the next day: invisible, so no day is
    counted until an authorized line shows the connection is back."""
    logs = tmp_path / "logs"
    for day in (date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 28)):
        write_log(logs, day, six_hourly(day))
    write_log(logs, date(2026, 11, 25), lost("12:00:00.000") + six_hourly(date(2026, 11, 25)))   # 04:00 UTC, never recovered
    write_log(logs, date(2026, 11, 26), six_hourly(date(2026, 11, 26)))
    write_log(logs, date(2026, 11, 27), six_hourly(date(2026, 11, 27)))
    calendar = calendar_file(tmp_path)
    readable = [json.loads(text) for text in run_main(mod, capsys, logs, "--from", "2026-11-25", "--to", "2026-11-27", calendar)[1].splitlines()]
    assert [row["would_fail_window"] for row in readable] == [True, True, True]
    assert [row["uncertain_due_to_unreadable"] for row in readable] == [False, False, False]

    real = Path.read_bytes
    def denied(self):
        if self.name == "20261125.log":
            raise PermissionError(13, "Permission denied")
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", denied)
    code, out = run_main(mod, capsys, logs, "--from", "2026-11-25", "--to", "2026-11-27", calendar)
    rows = [json.loads(text) for text in out.splitlines()]
    assert code == 3 and rows[0] == {"date_utc": "2026-11-25", "error": "log_unreadable"}
    assert [row["disconnect_segments"] for row in rows[1:]] == [0, 0]                 # the outage itself is invisible
    assert [row["would_fail_window"] for row in rows[1:]] == [False, False]
    assert [row["uncertain_due_to_unreadable"] for row in rows[1:]] == [True, True]
    assert [row["baseline_day"] for row in rows[1:]] == [False, False]

    # recovered at 06:00 UTC on the 26th: that day is still in the dark, the day after is an ordinary day
    write_log(logs, date(2026, 11, 26), six_hourly(date(2026, 11, 26)) + authorized("14:00:00.000", 300))
    write_log(logs, date(2026, 11, 27), six_hourly(date(2026, 11, 27)) + scan("10:00:00.000"))
    code, out = run_main(mod, capsys, logs, "--from", "2026-11-25", "--to", "2026-11-27", calendar)
    rows = [json.loads(text) for text in out.splitlines()]
    assert code == 3 and [row["uncertain_due_to_unreadable"] for row in rows[1:]] == [True, False]
    assert [row["baseline_day"] for row in rows[1:]] == [False, True]
    out = run_main(mod, capsys, logs, "--date", "2026-11-26", calendar, "--format", "table")[1]
    assert out.rstrip().endswith("| **不计入**（前面的日志读不了） |")

    # the file can be read again: the same days are judged on what they hold
    monkeypatch.undo()
    rows = [json.loads(text) for text in run_main(mod, capsys, logs, "--from", "2026-11-25", "--to", "2026-11-27", calendar)[1].splitlines()]
    assert [row["would_fail_window"] for row in rows] == [True, True, False]      # down 04:00 on the 25th → 06:00 on the 26th
    assert [row["baseline_day"] for row in rows] == [False, False, True]


def test_the_table_does_not_let_a_calendar_reason_hide_a_real_one(mod, tmp_path, capsys):
    failing = mod.markdown_row(summary_for(mod, DAY, outage("11-24 10:00", "11-24 12:00")))        # no calendar, and it fails
    assert "| **失败** |" in failing and failing.endswith("| **不计入** |")
    closed = mod.markdown_row(summary_for(mod, date(2026, 11, 28), [], calendar=CAL))                 # a Saturday
    assert closed.startswith("| 2026-11-28 | 否 |") and closed.endswith("| **不计入**（不是交易日） |")
    # a healthy trading day without a calendar: the calendar is the only reason, and it is named
    logs = tmp_path / "logs"
    for day in (date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25)):
        write_log(logs, day, six_hourly(day))
    out = run_main(mod, capsys, logs, "--date", "2026-11-24", "--format", "table")[1]
    assert out.rstrip().endswith("| **不计入**（没有日历） |")
