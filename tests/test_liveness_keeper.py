"""存活检查的看护和终端发现（定版前跟进 D）：liveness_logic.py / liveness_check.py。

- --expect-keeper：给了才看看护脚本的状态文件，得 keeper_absent、keeper_stale、keeper_refused；不带给
  输出的 keeper 是 not_checked。装没装看护任务，状态文件自己说不清（装了但一次没跑成和没装，文件
  系统上一样），所以由小时任务的命令行明说。
- terminal_absent:<运行器>：不受那个开关管。运行器的进程在、心跳新鲜，心跳的 last_error 说它没有终端——
  启动等待（waiting_for_terminal），或连上之后终端进程没了（网关的 TerminalNotConnectedError 进了
  last_error）。

纯逻辑跑在合成的现场文件上：没有 MetaTrader5、没有终端、没有网络；进程探针和时钟是注入的。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 24, 23, 30, tzinfo=timezone.utc)        # 22:30 UTC 之后：要求昨天的日报在
ROLES = (("runner", 902610, 11), ("risk", None, 13), ("reconcile", None, 14))
RUNNER, RISK, RECONCILE = "902610", "risk", "reconcile"
# 运行器在运行中丢了终端时写进心跳的 last_error（live_runner.py 的断线分支）
GONE = ("broker_unavailable: NotConnectedError: MT5 终端进程不在：未调用 initialize"
        "（终端只由看护任务或维护窗内的人工操作启动，T209）")
# 虚构的敏感内容（拼出来写，卫生扫描才不会把它当成真的本机路径）
FAKE_PATH = "C:" + "/Users/" + "nobody/live/x.py"
RUNNING = {"outcome": "running", "reason": "", "running": 1, "others": 0, "pid": 0}
ABSENT = f"terminal_absent:{RUNNER}"
MESSAGE = "MT5 Demo 值守："


def load():
    """入口模块（liveness_check.py）；纯逻辑在 sys.modules 里，名字是它自己导入的。"""
    spec = importlib.util.spec_from_file_location("t214_keeper_liveness", ROOT / "scripts" / "ops" / "liveness_check.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def ago(seconds):
    return (NOW - timedelta(seconds=seconds)).isoformat()


def key_of(kind, magic):
    return str(magic) if kind == "runner" else kind


def site(root, *, errors=None, started=None, heartbeat_age=None, alive=None):
    """一个合成的现场：登记表里三个角色，心跳 5 秒前，日报都在。errors 是 {角色的键: 心跳的 last_error}；
    started、heartbeat_age 是 {角色的键: 距今多少秒}（缺省三天、5 秒）；alive 是进程探针（缺省都在）。
    → (live, reports, 探针)."""
    errors, started, heartbeat_age = errors or {}, started or {}, heartbeat_age or {}
    data = Path(root) / "data"
    live, reports = data / "live", data / "reports"
    live.mkdir(parents=True), (reports / "live").mkdir(parents=True)
    entries = [{"pid": pid, "started_utc": ago(started.get(key_of(kind, magic), 3 * 86400)),
                "kind": kind, "magic": magic} for kind, magic, pid in ROLES]
    (live / "instances.json").write_text(json.dumps({"schema_version": 1, "instances": entries}), encoding="utf-8")
    for kind, magic, pid in ROLES:
        key = key_of(kind, magic)
        (live / f"heartbeat-{key}.json").write_text(json.dumps(
            {"runner_alive": True, "ts": ago(heartbeat_age.get(key, 5)), "pid": pid + 100,
             "last_error": errors.get(key)}), encoding="utf-8")
    (live / "supervisor-state.json").write_text(json.dumps({"last_checked": ago(30)}), encoding="utf-8")
    day = (NOW - timedelta(days=1)).date().isoformat()
    (reports / "live" / f"daily-{day}.md").write_text("# report", encoding="utf-8")
    return live, reports, (alive or (lambda pid, started_utc: True))


def keeper_state(root, record=None, *, text=None, age=30):
    """看护脚本的状态文件：<数据目录>/ops-evidence/terminal_keeper/state.json（看护脚本的 -LogPath 所在
    目录）。record 是 None 且没有 text：文件不在。age 是 ts 距今多少秒（同时也是文件的修改时间）。"""
    path = Path(root) / "data" / "ops-evidence" / "terminal_keeper" / "state.json"
    if record is None and text is None:
        path.unlink(missing_ok=True)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    body = text if text is not None else json.dumps({"ts": ago(age), **record})
    path.write_text(body, encoding="utf-8")
    stamp = (NOW - timedelta(seconds=age)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def look(mod, scene, *, expect_keeper=False, state=None, now=NOW):
    """看一次：observe + assess。→ (现场, {代码: 发现}, (要通知的, 已恢复的, 要写回的状态)).
    不带开关时按原来的参数个数调用 observe：这样的用例在开关还不存在的版本上是行为失败，不是一上来
    就报参数不对。"""
    live, reports, alive = scene
    obs = (mod.observe(live, reports, live, now, alive, expect_keeper) if expect_keeper
           else mod.observe(live, reports, live, now, alive))
    findings, booked = mod.assess(obs, now, state or mod.blank_state())
    return obs, {item["code"]: item for item in findings}, booked


def seen_700s_ago(code, *, production=False):
    """检查器的状态：这个代码 700 秒前第一次看到，还没通知过；当时是不是生产级由 production 说。"""
    return {"open": {code: {"since": ago(700), "notified": None, "production": production}}, "recovered": {}}


def test_without_the_switch_the_keeper_is_not_looked_at(tmp_path):
    mod = load()
    scene = site(tmp_path)
    keeper_state(tmp_path)                                         # 没有状态文件，也不问
    obs, found, (announce, recovered, new_state) = look(mod, scene)
    assert obs["keeper"] is None and found == {} and (announce, recovered, new_state) == ([], [], mod.blank_state())
    keeper_state(tmp_path, text="{not json", age=700)
    obs, found, _ = look(mod, scene)
    assert obs["keeper"] is None and found == {}


def test_the_switch_decides_whether_the_keeper_is_looked_at(tmp_path, capsys):
    mod = load()
    live, reports, alive = site(tmp_path)
    keeper_state(tmp_path)
    argv = ["--live-dir", str(live), "--reports-dir", str(reports), "--disk-path", str(live), "--no-notify",
            "--state-file", str(tmp_path / "liveness.json")]
    rc = mod.main(argv, now=NOW, alive=alive)
    line = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert rc == 0 and line["keeper"] == "not_checked" and line["findings"] == []
    rc = mod.main([*argv, "--expect-keeper"], now=NOW, alive=alive)
    line = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert line["keeper"] == "checked" and [item["code"] for item in line["findings"]] == ["keeper_absent"]
    assert line["findings"][0]["production"] is False and rc == 0
    rc = mod.main([*argv, "--expect-keeper", "--keeper-state", str(tmp_path / "elsewhere.json")], now=NOW, alive=alive)
    line = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert [item["code"] for item in line["findings"]] == ["keeper_absent"]   # 指定了路径就只看那一个


def test_an_absent_keeper_is_production_ten_minutes_after_it_was_first_missed(tmp_path):
    mod = load()
    keeper_state(tmp_path)
    scene = site(tmp_path)
    _, found, (announce, recovered, new_state) = look(mod, scene, expect_keeper=True)
    assert list(found) == ["keeper_absent"] and found["keeper_absent"]["production"] is False
    assert (announce, recovered) == ([], [])
    assert new_state["open"] == {"keeper_absent": {"since": NOW.isoformat(), "notified": None, "production": False}}
    _, found, (announce, recovered, new_state) = look(mod, scene, expect_keeper=True, state=seen_700s_ago("keeper_absent"))
    assert list(found) == ["keeper_absent"]
    assert found["keeper_absent"]["production"] is True and found["keeper_absent"]["lasted_s"] == 700
    assert [item["code"] for item in announce] == ["keeper_absent"] and recovered == []
    assert new_state["open"]["keeper_absent"]["production"] is True
    assert mod.format_message(announce, recovered) == MESSAGE + "keeper_absent(11分)"


def test_a_keeper_that_stopped_reporting_is_production_on_the_first_look(tmp_path):
    """状态文件自己的年龄说了多久没人管：满了 10 分钟，第一次看到就是生产级，不用再等一个小时。"""
    mod = load()
    scene = site(tmp_path)
    keeper_state(tmp_path, RUNNING, age=599)
    _, found, (announce, _, _) = look(mod, scene, expect_keeper=True)
    assert found == {} and announce == []
    keeper_state(tmp_path, RUNNING, age=601)
    _, found, (announce, _, _) = look(mod, scene, expect_keeper=True)
    assert list(found) == ["keeper_stale"]
    assert found["keeper_stale"]["age_s"] == 601 and found["keeper_stale"]["production"] is True
    assert mod.format_message(announce) == MESSAGE + "keeper_stale(10分)"


def test_a_refusing_or_failing_keeper_is_production_after_ten_minutes(tmp_path):
    mod = load()
    for outcome, reason in (("refused", "cannot_tell"), ("start_failed", "InvalidOperationException")):
        root = tmp_path / outcome
        scene = site(root)
        keeper_state(root, {"outcome": outcome, "reason": reason})
        _, found, (announce, _, _) = look(mod, scene, expect_keeper=True)
        assert list(found) == ["keeper_refused"] and announce == []
        assert (found["keeper_refused"]["outcome"], found["keeper_refused"]["reason"]) == (outcome, reason)
        _, found, (announce, _, _) = look(mod, scene, expect_keeper=True, state=seen_700s_ago("keeper_refused"))
        assert list(found) == ["keeper_refused"]
        assert found["keeper_refused"]["production"] is True and found["keeper_refused"]["lasted_s"] == 700
        assert mod.format_message(announce) == MESSAGE + "keeper_refused(11分)"
    root = tmp_path / "fine"
    scene = site(root)
    keeper_state(root, RUNNING)
    _, found, _ = look(mod, scene, expect_keeper=True)
    assert found == {}
    keeper_state(root, {"outcome": "refused", "reason": "cannot_tell"}, age=700)
    _, found, (announce, _, _) = look(mod, scene, expect_keeper=True)        # 既陈旧又在拒绝：两条都报
    assert set(found) == {"keeper_stale", "keeper_refused"}
    # 陈旧按文件自己的年龄，第一次看到就是生产级；拒绝要检查器自己看满 10 分钟
    assert (found["keeper_stale"]["production"], found["keeper_refused"]["production"]) == (True, False)
    assert [item["code"] for item in announce] == ["keeper_stale"]


def test_what_the_state_file_may_not_put_into_the_output(tmp_path):
    """outcome、reason 只有长得像标识符的才进输出（拒绝的代码、.NET 异常的类名）：不像的既不算拒绝，也不
    带着原文出来。读不出的文件按修改时间算年龄；ts 来自未来记 clock_ahead。"""
    mod = load()
    scene = site(tmp_path)
    keeper_state(tmp_path, {"outcome": "refused", "reason": FAKE_PATH})
    _, found, _ = look(mod, scene, expect_keeper=True)
    assert list(found) == ["keeper_refused"] and found["keeper_refused"]["reason"] is None
    assert FAKE_PATH not in json.dumps(found)
    keeper_state(tmp_path, {"outcome": "Restarted", "reason": FAKE_PATH})
    _, found, _ = look(mod, scene, expect_keeper=True)
    assert found == {}
    for body in ("{not json", "[1, 2]", json.dumps(RUNNING)):              # 读不出、不是对象、没有 ts
        keeper_state(tmp_path, text=body, age=700)
        _, found, _ = look(mod, scene, expect_keeper=True)
        assert list(found) == ["keeper_stale"] and found["keeper_stale"]["production"] is True, body
    keeper_state(tmp_path, text=json.dumps({"ts": (NOW + timedelta(minutes=5)).isoformat(), "outcome": "running"}))
    _, found, _ = look(mod, scene, expect_keeper=True)
    assert set(found) == {"clock_ahead:keeper", "keeper_stale"}
    assert found["clock_ahead:keeper"]["production"] is True


def test_a_runner_waiting_for_the_terminal_since_it_started_is_production_on_the_first_look(tmp_path):
    """启动等待：启动了 30 秒的，刚发现；启动等待的角色从启动起就在等，所以"现在减去 started_utc"是
    时长的下界——已经等了 700 秒的，第一次看到就是生产级。下界和检查器自己的时钟取大的那个。"""
    mod = load()
    scene = site(tmp_path, errors={RUNNER: "waiting_for_terminal"}, started={RUNNER: 30})
    _, found, (announce, _, new_state) = look(mod, scene)           # 不需要 --expect-keeper
    assert list(found) == [ABSENT] and announce == []
    assert {key: found[ABSENT][key] for key in ("role", "kind", "age_s", "lasted_s", "production")} == {
        "role": RUNNER, "kind": "startup", "age_s": 30, "lasted_s": 0, "production": False}
    assert list(new_state["open"]) == [ABSENT]
    late = site(tmp_path / "late", errors={RUNNER: "waiting_for_terminal"}, started={RUNNER: 700})
    _, found, (announce, _, _) = look(mod, late)
    assert found[ABSENT]["production"] is True and found[ABSENT]["lasted_s"] == 0 and found[ABSENT]["age_s"] == 700
    assert mod.format_message(announce) == MESSAGE + f"terminal_absent:{RUNNER}(11分)"
    _, found, _ = look(mod, scene, state=seen_700s_ago(ABSENT))     # 才启动 30 秒，但检查器 700 秒前就看到了
    assert found[ABSENT]["production"] is True and found[ABSENT]["lasted_s"] == 700


def test_a_runner_that_lost_the_terminal_needs_the_checker_s_clock(tmp_path):
    """连上之后终端进程没了：登记里的 started_utc 是三天前，不能拿来当时长（连上过再断会说大）；
    只靠检查器第一次看到至今。终端回来了报"已恢复"。"""
    mod = load()
    scene = site(tmp_path, errors={RUNNER: GONE})
    _, found, (announce, _, _) = look(mod, scene)
    assert list(found) == [ABSENT] and announce == []
    assert {key: found[ABSENT][key] for key in ("kind", "age_s", "production")} == {
        "kind": "lost", "age_s": None, "production": False}
    assert mod.format_message([found[ABSENT]]) == MESSAGE + f"terminal_absent:{RUNNER}(刚发现)"
    state = seen_700s_ago(ABSENT)
    _, found, (announce, recovered, new_state) = look(mod, scene, state=state)
    assert found[ABSENT]["production"] is True and found[ABSENT]["lasted_s"] == 700
    assert [item["code"] for item in announce] == [ABSENT] and recovered == []
    announce, recovered, after = look(mod, site(tmp_path / "back"), state=new_state)[2]   # 终端回来了
    assert announce == [] and recovered == [ABSENT]
    assert after["open"] == {} and list(after["recovered"]) == [ABSENT]
    assert mod.format_message(announce, recovered) == MESSAGE + "已恢复 " + ABSENT


def test_what_does_not_count_as_a_missing_terminal(tmp_path):
    """IPC 不通而终端进程还在是另一句报错；进程不在是 role_missing，心跳停了是 heartbeat_stale——都不是
    terminal_absent。对账、风控的心跳不带这类标记（它们写心跳时不带 last_error），这一类只覆盖运行器。"""
    mod = load()
    _, found, _ = look(mod, site(tmp_path / "ipc", errors={RISK: "NotConnectedError: initialize failed",
                                                            RECONCILE: GONE.replace("MT5 终端进程不在", "IPC 不通")}))
    assert found == {}
    _, found, _ = look(mod, site(tmp_path / "other", errors={RUNNER: "risk_gate_error: x", RECONCILE: ""}))
    assert found == {}
    _, found, _ = look(mod, site(tmp_path / "gone", errors={RUNNER: "waiting_for_terminal"},
                                 alive=lambda pid, started_utc: pid != 11))
    assert list(found) == [f"role_missing:{RUNNER}"]
    _, found, _ = look(mod, site(tmp_path / "stale", errors={RUNNER: GONE}, heartbeat_age={RUNNER: 700}))
    assert list(found) == [f"heartbeat_stale:{RUNNER}"]


def test_the_markers_the_checker_looks_for_are_the_ones_the_runner_writes():
    """检查器认的两句话得是运行器真的写进心跳的：哪一边改了字，这条先失败。"""
    load()
    logic = sys.modules["liveness_logic"]
    runner = (ROOT / "scripts" / "live_runner.py").read_text(encoding="utf-8")
    connection = (ROOT / "src" / "mt5gw" / "connection.py").read_text(encoding="utf-8")
    assert f'write(error="{logic.WAITING_FOR_TERMINAL}")' in runner
    assert '"MT5 终端进程不在：未调用 initialize"' in connection       # TerminalNotConnectedError 的报错原文
    assert f'"{logic.TERMINAL_GONE}' in connection and logic.TERMINAL_GONE in GONE
    assert 'broker_unavailable: NotConnectedError: {exc}"' in runner   # 运行器把类名和原文一起写进 last_error
    assert (logic._terminal_wait("waiting_for_terminal"), logic._terminal_wait(GONE),
            logic._terminal_wait("NotConnectedError: initialize failed")) == ("startup", "lost", None)


def test_a_missing_terminal_is_reported_without_the_switch(tmp_path, capsys):
    mod = load()
    live, reports, alive = site(tmp_path, errors={RUNNER: GONE})
    rc = mod.main(["--live-dir", str(live), "--reports-dir", str(reports), "--disk-path", str(live), "--no-notify",
                   "--state-file", str(tmp_path / "liveness.json")], now=NOW, alive=alive)
    line = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert [item["code"] for item in line["findings"]] == [ABSENT]
    assert line["findings"][0]["kind"] == "lost"
    assert rc == 0 and line["ok"] is False and line["keeper"] == "not_checked"
