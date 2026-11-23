"""T218 link baseline: read-only statistics over terminal journal logs.

Everything here is synthetic: no MetaTrader5, no real log directory, no event
log. The DNS counter gets a stand-in runner.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 11, 24)                    # Tuesday
ACCOUNT = "1234567890"
SERVER = "Demo-Server-X"


def load():
    spec = importlib.util.spec_from_file_location("t218_link_baseline", ROOT / "scripts" / "ops" / "link_baseline.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def line(clock, message):
    return f"XX\t0\t{clock}\tNetwork\t'{ACCOUNT}': {message}\n"


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
    (log_dir / f"{day.strftime('%Y%m%d')}.log").write_text(text, encoding="utf-16")


def test_parse_events_keeps_the_network_events_that_matter():
    mod = load()
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


def test_segments_pair_each_loss_with_the_next_authorization():
    mod = load()
    t = [datetime(2026, 11, 24, hour, minute) for hour, minute in
         ((1, 0), (1, 2), (3, 0), (3, 5), (3, 7), (6, 0), (8, 0), (9, 0))]
    events = list(zip(t, ["lost", "authorized", "lost", "lost", "authorized", "lost", "disconnected", "authorized"],
                      [None] * 8))
    assert mod.segments(events) == [
        (t[0], t[1]),          # recovered
        (t[2], t[3]),          # lost again before recovering: the second loss bounds it
        (t[3], t[4]),
        (t[5], None),          # the terminal itself exited: unknown end
    ]
    assert mod.segments([(t[0], "lost", None)]) == [(t[0], None)]      # never recovered in the data


def test_read_events_maps_local_log_time_to_utc_across_daily_files(tmp_path):
    mod = load()
    logs = tmp_path / "logs"
    # Local date 11-25 00:30 (UTC+8) is 11-24 16:30 UTC; 11-24 07:59 local is 11-23 23:59 UTC.
    write_log(logs, date(2026, 11, 24), lost("07:59:00.000") + authorized("09:00:00.000", 300))
    write_log(logs, date(2026, 11, 25), lost("00:30:00.000") + authorized("00:31:30.000", 456.5))
    events = mod.read_events(logs, datetime(2026, 11, 24), datetime(2026, 11, 25), 8.0)
    assert [(at.strftime("%m-%dT%H:%M:%S"), kind, value) for at, kind, value in events] == [
        ("11-24T01:00:00", "authorized", 300.0),
        ("11-24T16:30:00", "lost", None),
        ("11-24T16:31:30", "authorized", 456.5)]


def test_trading_overlap_leaves_out_the_weekend_window():
    mod = load()
    at = datetime
    # November 2026: New York is on standard time, the weekend window is Fri 22:00 → Sun 22:00 UTC.
    assert mod.trading_overlap_s(at(2026, 11, 27, 20, 0), at(2026, 11, 27, 20, 10)) == 600
    assert mod.trading_overlap_s(at(2026, 11, 27, 21, 55), at(2026, 11, 27, 22, 5)) == 300
    assert mod.trading_overlap_s(at(2026, 11, 28, 10, 0), at(2026, 11, 28, 11, 0)) == 0
    assert mod.trading_overlap_s(at(2026, 11, 29, 21, 50), at(2026, 11, 29, 22, 20)) == 1200


def test_dns_timeouts_counts_events_and_reports_what_it_cannot_read():
    mod = load()
    seen = []
    def runner(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout=b"<Event a><Event b><Event c>")
    assert mod.dns_timeouts(DAY, runner=runner, system_root=r"C:\Windows") == 3
    assert seen[0][0] == r"C:\Windows\System32\wevtutil.exe"        # absolute, not PATH lookup
    query = seen[0][3]                                                # /q:<XPath>
    assert "@SystemTime>='2026-11-24T00:00:00.000Z'" in query
    assert "@SystemTime<'2026-11-25T00:00:00.000Z'" in query
    assert "EventID=1014" in query

    def failing(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout=b"")
    assert mod.dns_timeouts(DAY, runner=failing) is None
    def raising(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 60)
    assert mod.dns_timeouts(DAY, runner=raising) is None


def test_summary_counts_the_segments_that_start_on_the_day_and_measures_across_midnight():
    mod = load()
    at = datetime
    events = [
        (at(2026, 11, 23, 23, 59), "lost", None), (at(2026, 11, 24, 0, 3), "authorized", 350.0),
        (at(2026, 11, 24, 10, 0), "lost", None), (at(2026, 11, 24, 10, 2, 30), "authorized", 400.0),
        (at(2026, 11, 24, 12, 0), "ap_switch", None), (at(2026, 11, 24, 12, 30), "scan", None),
        (at(2026, 11, 24, 23, 58), "lost", None), (at(2026, 11, 25, 0, 4), "authorized", 320.0),
        (at(2026, 11, 25, 1, 0), "scan", None),
    ]
    summary = mod.summarize(DAY, events, 7)
    assert summary == {
        "date_utc": "2026-11-24", "trading_day": True,
        "disconnect_segments": 2, "unresolved": 0,
        "longest_s": 360.0, "longest_trading_s": 360.0, "total_s": 510.0,
        "authorizations": 2, "ping_min_ms": 350.0, "ping_max_ms": 400.0,
        "ap_switches": 1, "scans": 1, "dns_timeouts": 7}
    # A day without any disconnect must not blow up on the empty sums.
    quiet = mod.summarize(DAY, [(datetime(2026, 11, 24, 1), "authorized", 300.0)], 0)
    assert (quiet["disconnect_segments"], quiet["longest_s"], quiet["total_s"]) == (0, 0.0, 0.0)
    assert mod.summarize(date(2026, 11, 28), [], None)["trading_day"] is False


def test_cli_output_carries_counts_only(tmp_path, capsys):
    mod = load()
    logs = tmp_path / "logs"
    # Log clocks are local (UTC+8): the UTC day 11-24 runs from 08:00 local on 11-24.
    write_log(logs, date(2026, 11, 24), authorized("09:00:00.000", 321.4) + lost("18:00:00.000")
              + authorized("18:09:00.000", 455.0)
              + line("18:09:00.000", "previous successful authorization performed from example-host on 2026.11.24 02:00:00"))
    for secrets in (ACCOUNT, SERVER, "Access Point", "example-host"):
        for format_ in ("json", "table"):
            capsys.readouterr()
            rc = mod.main(["--terminal-log-dir", str(logs), "--date", "2026-11-24",
                           "--no-dns", "--format", format_])
            assert rc == 0 and secrets not in capsys.readouterr().out, (secrets, format_)
    capsys.readouterr()
    assert mod.main(["--terminal-log-dir", str(logs), "--date", "2026-11-24", "--no-dns"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["disconnect_segments"] == 1 and summary["longest_s"] == 540.0
    assert summary["dns_timeouts"] is None and summary["ping_min_ms"] == 321.4


def test_cli_says_so_when_the_day_has_no_events(tmp_path, capsys):
    mod = load()
    assert mod.main(["--terminal-log-dir", str(tmp_path / "none"), "--date", "2026-11-24", "--no-dns"]) == 2
    assert "没有任何网络事件" in capsys.readouterr().err


def test_markdown_row_shows_every_column():
    mod = load()
    row = mod.markdown_row({"date_utc": "2026-11-24", "trading_day": True, "disconnect_segments": 2,
                            "longest_trading_s": 42.0, "total_s": 118.0, "ping_min_ms": 315.2,
                            "ping_max_ms": 460.7, "ap_switches": 1, "scans": 2, "dns_timeouts": None})
    assert row == "| 2026-11-24 | 是 | 2 | 42 | 118 | 315–461 | 1 | 2 | 未查 |"
