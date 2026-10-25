"""mt5live.fileio（T33/M1、M2、M8、L3）：跨进程文件契约的唯一写入口。

write_json_atomic / atomic_replace 直接复用 mt5gw.snapshot
（``_atomic_replace`` 的 PermissionError 重试——Windows 下读者进程占着目标
文件句柄时 os.replace 会失败，M1 的根因）；append_line / append_jsonl 用
O_APPEND 单次 write（追加写对单行原子）+ 共享冲突重试（M8：多运行器并发
追加不再交错出坏行）。
"""
import json
import os
import time

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
