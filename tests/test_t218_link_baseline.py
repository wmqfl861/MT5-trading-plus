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


def present(*days):
    """Journal lines every six hours through these UTC days: the terminal was there."""
    marks = []
    for day in days:
        first = datetime(day.year, day.month, day.day) - timedelta(hours=2)
        marks += [first + timedelta(hours=6 * step) for step in range(6)]
    return sorted(marks)


def summary_for(mod, day, evts, last_line="2026-12-02 12:00", marks=None, **options):
    marks = present(*[day + timedelta(days=step) for step in range(-1, 2)]) if marks is None else marks
    reading = mod.Reading(events=evts, marks=marks, last_line=at(last_line))
    return mod.summarize(day, reading, **options)


def run_main(mod, capsys, logs, *options):
    capsys.readouterr()
    code = mod.main(["--terminal-log-dir", str(logs), "--no-dns", *options])
    return code, capsys.readouterr().out


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
    reading = mod.read_logs(logs, datetime(2026, 11, 24), datetime(2026, 11, 25), 8.0)
    assert [(moment.strftime("%m-%dT%H:%M:%S"), kind, value) for moment, kind, value in reading.events] == [
        ("11-24T01:00:00", "authorized", 300.0),
        ("11-24T16:30:00", "lost", None),
        ("11-24T16:31:30", "authorized", 456.5)]
    assert reading.found == {"2026-11-24", "2026-11-25"} and reading.unreadable == []
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
    # a truncated last line is read as far as it was written; the recognizable part is kept
    assert mod.parse_events(mod._decode(full[:-8])) == whole
    assert mod.parse_events(mod._decode(full[:-40])) == whole[:1]
    assert mod.parse_events(mod._decode(text.encode("utf-16-le"))) == whole
    assert mod.parse_events(mod._decode(b"\xfe\xff" + text.encode("utf-16-be"))) == whole
    assert mod.parse_events(mod._decode(b"\xef\xbb\xbf" + text.encode("utf-8"))) == whole
    assert mod._decode(b"") == ""
    with pytest.raises(UnicodeError):
        mod._decode(b"\x00\xd8" * 20)                              # not UTF-16


def test_unresolved_outage_is_a_lower_bound_and_never_zero(mod):
    reading = mod.Reading(events=events(("11-24 10:00", "lost")), marks=[at("11-24 04:00"), at("11-24 10:00")],
                          last_line=at("11-24 10:30"))
    summary = mod.summarize(DAY, reading, 0)
    assert summary["disconnect_segments"] == 1 and summary["unresolved_at_day_end"] == 0   # the day is not over in the data
    assert summary["longest_s"] == 1800.0 and summary["longest_is_lower_bound"] is True
    assert summary["open_segments"] == [{"start_utc": "2026-11-24T10:00:00", "kind": "lost", "at_least_s": 1800.0}]
    assert summary["would_fail_window"] is True and summary["first_breach_utc"] == "2026-11-24T10:15:00"
    assert summary["data_complete"] is False and summary["baseline_day"] is False
    assert "| 1（0） | 0 | ≥1800 | **失败** |" in mod.markdown_row(summary)
    # lost again before recovering: still one outage, still open, 10:00 to the end of the data
    again = mod.summarize(DAY, mod.Reading(events=events(("11-24 10:00", "lost"), ("11-24 12:00", "lost")),
                                           marks=[at("11-24 04:00")], last_line=at("11-24 12:30")), 0)
    assert (again["disconnect_segments"], again["longest_s"], again["longest_is_lower_bound"]) == (1, 9000.0, True)
    assert again["open_segments"] == [{"start_utc": "2026-11-24T10:00:00", "kind": "lost", "at_least_s": 9000.0}]
    # the data ends right after the loss: nothing is claimed about how long it lasted
    bare = mod.summarize(DAY, mod.Reading(events=events(("11-24 10:00", "lost")), marks=[at("11-24 04:00"), at("11-24 10:00")],
                                          last_line=at("11-24 10:00")), 0)
    assert bare["open_segments"][0]["at_least_s"] == 0.0 and bare["would_fail_window"] is False
    assert bare["longest_is_lower_bound"] is True and bare["baseline_day"] is False


def test_recovery_seen_more_than_a_day_later_is_measured(mod):
    evts = events(("11-24 23:50", "lost"), ("11-26 01:00", "authorized", 320.0))
    marks = present(date(2026, 11, 24), date(2026, 11, 25), date(2026, 11, 26))
    first = summary_for(mod, DAY, evts, last_line="11-26 12:00", marks=marks)
    assert (first["longest_s"], first["total_s"], first["longest_is_lower_bound"]) == (90600.0, 90600.0, False)
    assert first["unresolved_at_day_end"] == 1 and first["open_segments"] == []
    assert first["would_fail_window"] is False          # the exit lands on Wednesday, not Tuesday
    second = summary_for(mod, date(2026, 11, 25), evts, last_line="11-26 12:00", marks=marks)
    assert (second["continuing_from_previous_day"], second["whole_day_in_outage"]) == (1, True)
    assert second["longest_s"] == 86400.0 and second["would_fail_window"] is True
    assert second["first_breach_utc"] == "2026-11-25T00:05:00"
    # the day it finally recovered sees the last hour of it, and no failure
    third = summary_for(mod, date(2026, 11, 26), evts, last_line="11-26 12:00", marks=marks)
    assert (third["disconnect_segments"], third["continuing_from_previous_day"], third["longest_s"]) == (1, 1, 3600.0)
    assert third["would_fail_window"] is False


def test_a_whole_trading_day_inside_an_outage_is_reported_on_that_day(mod):
    evts = events(("11-22 23:00", "lost"), ("11-24 00:10", "authorized", 320.0))       # Sunday → Tuesday
    marks = present(date(2026, 11, 22), date(2026, 11, 23), date(2026, 11, 24))
    sunday = summary_for(mod, date(2026, 11, 22), evts, last_line="11-24 12:00", marks=marks)
    assert (sunday["trading_day"], sunday["longest_s"], sunday["would_fail_window"]) == (False, 90600.0, False)
    monday = summary_for(mod, date(2026, 11, 23), evts, last_line="11-24 12:00", marks=marks)
    assert (monday["disconnect_segments"], monday["continuing_from_previous_day"]) == (1, 1)
    assert (monday["whole_day_in_outage"], monday["unresolved_at_day_end"], monday["longest_s"]) == (True, 1, 86400.0)
    assert monday["would_fail_window"] is True and monday["first_breach_utc"] == "2026-11-23T00:15:00"
    assert monday["baseline_day"] is False


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


@pytest.mark.parametrize("span, calendar, day, fails, breach", [
    (("11-24 22:05", "11-24 22:40"), None, DAY, True, "2026-11-24T22:20:00"),
    (("11-27 21:50", "11-28 01:00"), None, date(2026, 11, 27), True, "2026-11-27T22:05:00"),
    (("11-27 16:00", "11-29 23:30"), None, date(2026, 11, 27), True, "2026-11-27T16:15:00"),
    (("11-28 23:00", "11-29 23:00"), None, date(2026, 11, 28), False, None),
    (("11-28 23:00", "11-29 23:00"), None, date(2026, 11, 29), False, None),
    (("11-29 23:50", "11-30 00:20"), None, date(2026, 11, 29), False, None),
    (("11-29 23:50", "11-30 00:20"), None, date(2026, 11, 30), True, "2026-11-30T00:05:00"),
    (("11-27 21:50", "11-28 01:00"), {"2026-11-27": False}, date(2026, 11, 27), False, None),
])
def test_window_rule_follows_the_formal_check(mod, span, calendar, day, fails, breach):
    """The exit with rc=78 fails the window when its moment falls on a calendar trading day."""
    summary = summary_for(mod, day, events((span[0], "lost"), (span[1], "authorized", 320.0)), calendar=calendar)
    assert summary["would_fail_window"] is fails, summary
    assert summary["first_breach_utc"] == breach
    assert summary["trading_day"] is (calendar.get(day.isoformat(), day.weekday() < 5) if calendar else day.weekday() < 5)
    assert summary["calendar"] == ("file" if calendar else "weekday")


def test_outages_near_the_line_are_counted_apart(mod):
    just_under = summary_for(mod, DAY, events(("11-24 10:00", "lost"), ("11-24 10:14:30", "authorized", 300.0)))
    assert just_under["would_fail_window"] is False and just_under["near_breach_segments"] == 1
    exactly = summary_for(mod, DAY, events(("11-24 10:00", "lost"), ("11-24 10:15:00", "authorized", 300.0)))
    assert (exactly["longest_s"], exactly["would_fail_window"], exactly["near_breach_segments"]) == (900.0, True, 0)
    finer = [(at("11-24 10:00"), "lost", None), (at("11-24 10:00") + timedelta(seconds=899.96), "authorized", 300.0)]
    almost = summary_for(mod, DAY, finer)
    assert (almost["longest_s"], almost["would_fail_window"]) == (899.9, False)      # not rounded up to the line
    # started before midnight, back 5 minutes into the trading day: reported, not called a failure
    tail = summary_for(mod, date(2026, 11, 30), events(("11-29 23:40", "lost"), ("11-30 00:05", "authorized", 300.0)))
    assert (tail["would_fail_window"], tail["tails_into_trading_day"], tail["continuing_from_previous_day"]) == (False, 1, 1)
    assert tail["longest_s"] == 300.0


def test_exit_code_separates_no_log_unreadable_and_a_quiet_day(mod, tmp_path, capsys, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    code, out = run_main(mod, capsys, logs, "--date", "2026-11-24")
    assert (code, json.loads(out)) == (3, {"date_utc": "2026-11-24", "error": "no_terminal_log"})
    code, out = run_main(mod, capsys, tmp_path / "nowhere", "--date", "2026-11-24")
    assert (code, json.loads(out)) == (3, {"date_utc": "2026-11-24", "error": "log_dir_unreadable"})
    # the terminal was there the whole day and never re-connected: a normal result, exit 0
    write_log(logs, date(2026, 11, 24), scan("09:00:00.000") + scan("15:00:00.000") + scan("21:00:00.000"))
    write_log(logs, date(2026, 11, 25), scan("03:00:00.000") + scan("09:00:00.000"))
    code, out = run_main(mod, capsys, logs, "--date", "2026-11-24")
    summary = json.loads(out)
    assert code == 0 and (summary["disconnect_segments"], summary["scans"]) == (0, 4)
    assert (summary["terminal_absent"], summary["data_complete"], summary["baseline_day"]) == (False, True, True)
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
    (logs / "20261126.log").write_bytes(b"\x00\xd8" * 40)               # not UTF-16: a lone surrogate, no BOM
    code, out = run_main(mod, capsys, logs, "--date", "2026-11-25")
    assert code == 3 and json.loads(out)["error"] == "log_unreadable"              # the 26th belongs to that UTC day


def test_terminal_absence_and_incomplete_data_are_told_apart(mod, tmp_path, capsys):
    # a) the journal stops at 09:00 local and resumes 35 hours later: the terminal was not there
    logs = tmp_path / "absent"
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
    assert mod.dns_timeouts(DAY, system_root=r"C:\Windows", run=make("2023-05-11T11:49:12.000", "2026-11-24T15:05:24.000", 2)) == 2
    assert [system_root for system_root, _ in seen] == [r"C:\Windows"] * 3
    assert seen[0][1] == ("/q:*[System[TimeCreated[@SystemTime<'2026-11-25T00:00:00.000Z']]]", "/c:1", "/rd:false", "/f:xml")
    assert seen[1][1][0] == "/q:*[System[TimeCreated[@SystemTime>='2026-11-24T00:00:00.000Z']]]"
    query = seen[2][1][0]                               # /q:<XPath>
    assert "@SystemTime>='2026-11-24T00:00:00.000Z'" in query and "@SystemTime<'2026-11-25T00:00:00.000Z'" in query
    assert "EventID=1014" in query and "Microsoft-Windows-DNS-Client" in query
    before = make("2023-05-11T11:49:12.000", "2023-05-12T00:00:00.000", 0)
    assert mod.dns_timeouts(date(2023, 5, 1), run=before) is None        # the log starts after that day
    assert mod.dns_timeouts(date(2027, 1, 1), run=before) is None        # nothing recorded since that day began
    assert mod.dns_timeouts(DAY, run=make("2023-05-11T11:49:12.000", "2026-11-24T01:00:00.000", 0)) == 0   # covered: a real zero
    assert mod.dns_timeouts(DAY, run=make("2023-05-11T11:49:12.000", "2026-11-24T01:00:00.000", 10001)) is None   # at the query cap
    assert mod.dns_timeouts(DAY, run=lambda *args: None) is None         # the query failed
    assert mod.dns_timeouts(DAY, run=lambda *args: "") is None           # the log is empty
    # the real query function with nowhere to find the tool: None, not an exception
    assert mod.dns_timeouts(DAY, system_root=str(tmp_path / "no-system-root")) is None


def test_cli_output_carries_counts_only(mod, tmp_path, capsys):
    logs = tmp_path / "logs"
    # Log clocks are local (UTC+8): the UTC day 11-24 runs from 08:00 local on 11-24.
    write_log(logs, date(2026, 11, 24), note("08:30:00.000") + authorized("09:00:00.000", 321.4) + note("14:30:00.000")
              + lost("18:00:00.000") + authorized("18:09:00.000", 455.0) + note("20:30:00.000")
              + line("18:09:00.000", "previous successful authorization performed from example-host on 2026.11.24 02:00:00"))
    write_log(logs, date(2026, 11, 25), note("02:30:00.000") + note("08:30:00.000"))
    for secrets in ((ACCOUNT, SERVER, "Access Point", "example-host"), (str(logs),)):
        for format_ in ("json", "table"):
            code, out = run_main(mod, capsys, logs, "--date", "2026-11-24", "--format", format_)
            assert code == 0 and all(secret not in out for secret in secrets), (secrets, format_)
    code, out = run_main(mod, capsys, logs, "--date", "2026-11-24")
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
    code, out = run_main(mod, capsys, logs, "--from", "2026-11-24", "--to", "2026-11-26", "--format", "table")
    assert code == 0 and out.splitlines()[0].startswith("| UTC 日 | 交易日 | 断线段")
    assert len(out.splitlines()) == 5 and out.count("判断不了（缺 2026-11-25 的日志）") == 2


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
    code, out = run_main(mod, capsys, logs)
    assert code == 0 and json.loads(out)["date_utc"] == "2026-11-22"       # not the local calendar's yesterday


def test_accounts_are_counted_not_printed(mod, tmp_path, capsys):
    logs = tmp_path / "logs"
    write_log(logs, date(2026, 11, 24), note("08:30:00.000") + lost("09:00:00.000") + line("09:02:00.000", "authorized on x (ping: 1 ms)", account=OTHER))
    code, out = run_main(mod, capsys, logs, "--date", "2026-11-24")
    summary = json.loads(out)
    assert code == 0 and summary["accounts_seen"] == 2                     # more than one: pairing may cross accounts
    assert ACCOUNT not in out and OTHER not in out


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
        evts = events(("11-26 10:00", "lost"), ("11-26 12:00", "authorized", 300.0))
        summary = summary_for(mod, date(2026, 11, 26), evts, calendar=calendar)
        assert (summary["trading_day"], summary["would_fail_window"], summary["longest_s"]) == (False, False, 7200.0)
        assert summary_for(mod, date(2026, 11, 26), evts)["would_fail_window"] is True     # weekdays only: stricter
    empty = tmp_path / "empty.txt"
    empty.write_text("no dated lines here\n", encoding="utf-8")
    with pytest.raises(ValueError):
        mod.load_calendar(empty)


def test_markdown_row_shows_open_outages_presence_and_the_verdict(mod):
    evts = events(("11-23 23:59", "lost"), ("11-24 00:03", "authorized", 350.0), ("11-24 10:00", "lost"),
                  ("11-24 10:02:30", "authorized", 400.0), ("11-24 12:00", "ap_switch"), ("11-24 12:30", "scan"),
                  ("11-24 23:58", "lost"), ("11-25 00:04", "authorized", 320.0), ("11-25 01:00", "scan"))
    summary = summary_for(mod, DAY, evts, dns=7)
    assert (summary["disconnect_segments"], summary["continuing_from_previous_day"], summary["unresolved_at_day_end"]) == (3, 1, 1)
    assert (summary["longest_s"], summary["total_s"], summary["authorizations"], summary["ping_min_ms"]) == (360.0, 690.0, 2, 350.0)
    assert summary["baseline_day"] is True and summary["open_segments"] == []
    assert mod.markdown_row(summary) == ("| 2026-11-24 | 是 | 3（0） | 1 | 360 | 不失败 | 在（最长空档 6.0 小时） | "
                                         "350–400 | 1 | 1 | 7 | 计入 |")
    away = dict(summary, terminal_absent=True, baseline_day=False, dns_timeouts=None)
    assert mod.markdown_row(away).endswith("| **不在** | 350–400 | 1 | 1 | 未查 | **不计入** |")
    unknown = dict(summary, terminal_absent=None, missing_log_days=["2026-11-25"])
    assert "| 判断不了（缺 2026-11-25 的日志） |" in mod.markdown_row(unknown)
