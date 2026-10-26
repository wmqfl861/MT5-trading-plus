"""mt5live.fileio（T33/M1、M2、M8、L3）：跨进程文件契约的唯一写入口。

write_json_atomic / atomic_replace 直接复用 mt5gw.snapshot
（``_atomic_replace`` 的 PermissionError 重试——Windows 下读者进程占着目标
文件句柄时 os.replace 会失败，M1 的根因）；append_line / append_jsonl 用
O_APPEND 单次 write（追加写对单行原子）+ 共享冲突重试（M8：多运行器并发
追加不再交错出坏行）。R6（I1）：原子写临时文件带 pid+uuid 唯一后缀
（多写者不再争用同一 tmp）；R7（I1 残留 N-I2）：file_lock 改为内核字节锁
（msvcrt.locking / flock）——互斥由内核仲裁、崩溃自动释放，废除按 mtime
的超龄接管（读旧 mtime 删新锁的双所有者竞态源头）。
"""
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from mt5live import fileio


def _flaky_os_replace(monkeypatch, fail_times: int) -> dict:
    """让 os.replace 前 fail_times 次抛 PermissionError（模拟读者占句柄）。"""
    real = os.replace
    state = {"n": 0}

    def flaky(src, dst):
        if state["n"] < fail_times:
            state["n"] += 1
            raise PermissionError(32, "另一个进程正在使用此文件")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    return state


# ---- write_json_atomic（复用 mt5gw.snapshot._atomic_replace 的重试）----

def test_write_json_atomic_writes_and_replaces(tmp_path):
    path = tmp_path / "state.json"
    fileio.write_json_atomic(str(path), {"a": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
    fileio.write_json_atomic(str(path), {"a": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 2}
    assert not (tmp_path / "state.json.tmp").exists()    # 临时文件已被替换


def test_write_json_atomic_retries_transient_permission_error(tmp_path,
                                                              monkeypatch):
    """M1 根因场景：替换目标正被另一进程读——重试预算内自愈，写入成功。"""
    state = _flaky_os_replace(monkeypatch, 2)
    path = tmp_path / "heartbeat-777.json"
    fileio.write_json_atomic(str(path), {"runner_alive": True})
    assert state["n"] == 2
    assert json.loads(path.read_text(encoding="utf-8")) == {"runner_alive": True}


def test_write_json_atomic_raises_when_retry_exhausted(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)   # 10×0.3s 不真睡
    _flaky_os_replace(monkeypatch, 10_000)
    with pytest.raises(PermissionError):
        fileio.write_json_atomic(str(tmp_path / "x.json"), {"a": 1})
    assert not (tmp_path / "x.json").exists()            # 半截不落目标


# ---- append_line / append_jsonl（O_APPEND 单次 write + 共享冲突重试）----

def test_append_jsonl_appends_valid_lines(tmp_path):
    path = tmp_path / "orders-777.jsonl"
    fileio.append_jsonl(str(path), {"ts": "t1", "magic": 777})
    fileio.append_jsonl(str(path), {"ts": "t2", "magic": 777})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["ts"] == "t1"
    assert json.loads(lines[1])["ts"] == "t2"


def test_append_line_retries_transient_permission_error(tmp_path, monkeypatch):
    real = fileio._append_once
    state = {"n": 0}

    def flaky(path, line):
        if state["n"] < 2:
            state["n"] += 1
            raise PermissionError(32, "共享冲突")
        return real(path, line)

    monkeypatch.setattr(fileio, "_append_once", flaky)
    fileio.append_line(str(tmp_path / "alerts.log"), "hello\n")
    assert (tmp_path / "alerts.log").read_text(encoding="utf-8") == "hello\n"


def test_append_line_raises_when_retry_exhausted(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)

    def always_locked(path, line):
        raise PermissionError(32, "一直被锁")

    monkeypatch.setattr(fileio, "_append_once", always_locked)
    with pytest.raises(PermissionError):
        fileio.append_line(str(tmp_path / "alerts.log"), "x\n")
    assert not (tmp_path / "alerts.log").exists()


def test_append_creates_missing_directories(tmp_path):
    path = tmp_path / "live" / "commands" / "flatten.ack-777"
    fileio.append_jsonl(str(path), {"magic": 777})
    assert json.loads(path.read_text(encoding="utf-8")) == {"magic": 777}


# ================================================================ R6（I1）
# 原子写多写者竞态：唯一临时文件名、跨进程文件锁、读取异常保守处理

def test_r6_concurrent_write_json_atomic_no_exception_valid_result(tmp_path):
    """I1 复现：两个写者并发写同一心跳/状态文件——固定 path+".tmp" 会让
    一个写者 FileNotFoundError；唯一临时文件名后无异常，最终内容总是
    两者之一的完整 JSON，且不残留临时文件。"""
    path = str(tmp_path / "heartbeat-777.json")
    payloads = [{"runner_alive": True, "seq": 0, "magic": 777},
                {"runner_alive": True, "seq": 1, "magic": 777}]
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def writer(payload):
        try:
            barrier.wait()
            for _ in range(20):
                fileio.write_json_atomic(path, payload)
        except Exception as e:                     # pragma: no cover - 收集用
            errors.append(e)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(writer, p) for p in payloads]
        for f in futs:
            f.result()
    assert errors == []
    with open(path, encoding="utf-8") as f:
        assert json.load(f) in payloads            # 完整的两者之一
    assert not list(tmp_path.glob("*.tmp"))        # 无残留临时文件


def test_r6_file_lock_mutual_exclusion_release_and_stale_takeover(tmp_path):
    """file_lock：互斥（第二个获取者短预算内拿不到）、释放后可再拿；
    崩溃语义由内核保证（持锁进程死亡锁自动消失——残锁文件不再阻塞
    后来者，也无须超龄接管；R7/I1 起锁文件常驻磁盘）。"""
    lock = str(tmp_path / "live" / "commands" / ".flatten.lock")
    with fileio.file_lock(lock):
        assert os.path.exists(lock)
        result = {}

        def try_acquire():
            try:
                with fileio.file_lock(lock, attempts=3, retry_s=0.01):
                    result["got"] = True
            except TimeoutError:
                result["got"] = False

        t = threading.Thread(target=try_acquire)
        t.start()
        t.join()
        assert result["got"] is False              # 持锁期间他人拿不到
    # 释放后可再拿（锁文件常驻——内容/存在性不再承载互斥语义）
    with fileio.file_lock(lock, attempts=3, retry_s=0.01):
        assert os.path.exists(lock)

    # 崩溃遗留的"残锁"文件（无任何持有者）：内核锁已随进程消失 →
    # 直接拿到、不阻塞（旧实现的超龄接管竞态源头已整体删除，见 R7）
    with open(lock, "w", encoding="utf-8") as f:
        f.write("stale owner")
    old = time.time() - 3600.0
    os.utime(lock, (old, old))
    with fileio.file_lock(lock, attempts=3, retry_s=0.01):
        assert os.path.exists(lock)


def test_r6_concurrent_write_flatten_yields_single_command(tmp_path):
    """I1 复现：并发 write_flatten——"先读、再创建"不串行化会让两个调用
    返回不同编号、只存活一个；跨进程锁后恰产生一个编号（幂等复用契约）。"""
    from mt5live.commands import read_flatten, write_flatten

    live = str(tmp_path / "live")
    now = datetime(2026, 10, 28, 10, 0)
    errors: list[Exception] = []
    results = []
    barrier = threading.Barrier(2)

    def writer(reason):
        try:
            barrier.wait()
            results.append(write_flatten(live, reason=reason, now=now))
        except Exception as e:                     # pragma: no cover - 收集用
            errors.append(e)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(writer, r) for r in ("r1", "r2")]
        for f in futs:
            f.result()
    assert errors == []
    ids = {c.id for c in results}
    assert len(ids) == 1                           # 两个调用者拿到同一编号
    assert read_flatten(live).id in ids
    # 幂等复用：再次写也不产生新编号
    assert write_flatten(live, reason="r3", now=now).id in ids


def test_r6_read_flatten_oserror_is_command_unreadable(tmp_path):
    """I1：读取时的 OSError/PermissionError（路径被占/是目录）→
    CommandUnreadableError（按有指令处理），不向调用方裸抛。"""
    from mt5live.commands import CommandUnreadableError, read_flatten

    live = str(tmp_path / "live")
    # flatten.json 是目录：open 必然 OSError（Windows 下 PermissionError）
    os.makedirs(os.path.join(live, "commands", "flatten.json"))
    with pytest.raises(CommandUnreadableError):
        read_flatten(live)


# ================================================================ R7（I1 残留/N-I2）
# 锁的超龄接管竞态：读旧 mtime → 他人换新锁 → 恢复者删新锁 → 双所有者

def test_r7_file_lock_stale_mtime_never_breaks_live_holder(tmp_path):
    """N-I2 复现交错（报告序列）：接管者读到"超龄"mtime 后，另一持有者
    仍活着持锁——接管者**不得**闯进临界区（旧实现：读 mtime → 直接删
    路径 → 删掉活锁持有者的新锁 → 两个所有者）。互斥由内核文件锁保证，
    mtime 做旧（崩溃残留的外观）不再构成接管依据。"""
    lock = str(tmp_path / "live" / "commands" / ".flatten.lock")
    with fileio.file_lock(lock):                      # 活锁持有者
        assert os.path.exists(lock)
        # 报告竞态的起点：锁文件被做旧成"崩溃残留"的外观
        old = time.time() - 3600.0
        os.utime(lock, (old, old))
        got = {}

        def taker():
            try:
                with fileio.file_lock(lock, attempts=2, retry_s=0.01):
                    got["in"] = True
            except TimeoutError:
                got["in"] = False

        t = threading.Thread(target=taker)
        t.start()
        t.join()
        assert got["in"] is False        # 互斥不破（旧实现 True=双所有者）
    # 释放后可再拿
    with fileio.file_lock(lock, attempts=2, retry_s=0.01):
        pass


def test_r7_file_lock_release_never_deletes_live_successor(tmp_path):
    """N-I2 释放侧竞态：持有者 A 的锁被做旧成"残锁"外观后，B 依超龄接管
    进入临界区，A 此刻才释放——旧实现 A 的 finally 直接 os.remove 路径，
    删掉的是 B 的锁，第三个竞争者 C 随即拿到锁与 B 并发。断言核心不变量：
    任意时刻至多一个所有者在临界区（B、C 不得同时持有）。"""
    lock = str(tmp_path / "live" / "commands" / ".flatten.lock")
    a_holds = threading.Event()
    a_may_release = threading.Event()
    got = {}

    def holder_a():
        with fileio.file_lock(lock):
            old = time.time() - 3600.0
            os.utime(lock, (old, old))               # A 的锁被做旧
            a_holds.set()
            a_may_release.wait(5.0)                  # B 进（或超时）后再释放

    def taker_b():
        a_holds.wait(5.0)
        try:
            with fileio.file_lock(lock, attempts=3, retry_s=0.01):
                got["b"] = True
                a_may_release.set()                  # B 已在临界区
                time.sleep(0.3)                      # 仍在临界区逗留
        except TimeoutError:
            got["b"] = False
            a_may_release.set()

    def taker_c():
        a_may_release.wait(5.0)
        try:
            with fileio.file_lock(lock, attempts=3, retry_s=0.05):
                got["c"] = True
        except TimeoutError:
            got["c"] = False

    ts = [threading.Thread(target=f) for f in (holder_a, taker_b, taker_c)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    # B 与 C 不得同时持有（旧实现：A 释放删掉 B 的锁 → C 拿到 → 双所有者）
    assert not (got.get("b") and got.get("c"))
