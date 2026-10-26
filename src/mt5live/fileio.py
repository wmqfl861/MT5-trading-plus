"""mt5live.fileio（T33/M1、M2、M8、L3；R6/I1）：跨进程文件契约的唯一写入口。

data/live/ 下所有会被**另一进程**读的文件，写入一律经本模块（评审 M1/M8：
Windows 下目标文件被别的进程占着句柄时，os.replace 会抛 PermissionError、
多进程共用同一文件的追加可能行交错——重试与单次 write 的纪律集中在这里，
各处不得再自带 tmp+replace 或裸 append）：

- write_json_atomic / atomic_replace：**直接 import 复用** mt5gw.snapshot
  （唯一临时文件名 + fsync + ``_atomic_replace`` 的 PermissionError 重试，
  总预算约 3 秒；R6/I1 起临时文件名带 pid+uuid——多写者不再争用同一 tmp）；
- append_line / append_jsonl：O_APPEND 打开 + 整行一次 write（追加写对单行
  原子，多运行器并发不再交错）+ 共享冲突同预算重试；
- file_lock：跨进程文件锁（O_CREAT|O_EXCL + 残锁接管，模式对齐
  mt5data.store._SeriesLock）——供"先读后写"的复合操作（如全平指令的
  创建）串行化，防止两个写者各自读到"没有指令"再各自创建不同编号。

T33 契约清单（写入调用方 → 落点文件）：
- runner 心跳（heartbeat-<magic>.json）、monitor 旧版心跳与 alerts.log 告警；
- executor 状态（state-<magic>.json）与下单日志（orders-<magic>.jsonl，
  M8：每策略各写各的）；ack（commands/flatten.ack-<magic>）与意图
  （expected-<magic>.json，经 mt5live.commands / mt5live.reconcile）；
- reconcile 暂停（pause-<magic>.json / pause.json）；
- 风控指令（halt.json、commands/flatten.json，经 riskproc / commands）。

时间约定不变：内存 naive UTC，文件 ISO 带 +00:00。
"""
from __future__ import annotations

import json
import os
import time as _time
from contextlib import contextmanager

# 直接 import 复用 mt5gw.snapshot 的原子写与 PermissionError 重试（M1 根因的
# 解法——本模块不另写一份重试逻辑）
from mt5gw.snapshot import write_json_atomic
from mt5gw.snapshot import _atomic_replace as _snapshot_replace

__all__ = [
    "append_jsonl", "append_line", "atomic_replace", "file_lock",
    "write_json_atomic",
]

_APPEND_ATTEMPTS = 10          # 与 mt5gw.snapshot._atomic_replace 同预算
_APPEND_RETRY_S = 0.3

# 跨进程文件锁的默认预算（同 mt5data.store._SeriesLock：50×0.2s = 10 秒）
_LOCK_ATTEMPTS = 50
_LOCK_RETRY_S = 0.2
_LOCK_STALE_AFTER_S = 600.0    # 锁文件超过 10 分钟视为进程崩溃遗留 → 接管


@contextmanager
def file_lock(lock_path: str, *, stale_after: float = _LOCK_STALE_AFTER_S,
              attempts: int = _LOCK_ATTEMPTS, retry_s: float = _LOCK_RETRY_S):
    """跨进程文件锁（R6/I1）：O_CREAT|O_EXCL 创建锁文件，独占期间他人
    重试，超龄（mtime > stale_after）残锁接管（进程崩溃遗留）。

    模式对齐 mt5data.store._SeriesLock；锁文件写 pid+时间戳便于排查。
    attempts 次仍拿不到 → TimeoutError（不静默放行——fail-closed）。
    线程间同样互斥（锁在文件系统上，不在线程原语上）。
    """
    d = os.path.dirname(lock_path)
    if d:
        os.makedirs(d, exist_ok=True)
    for _ in range(attempts):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, f"{os.getpid()} {_time.time():.3f}".encode())
            finally:
                os.close(fd)
            break
        except FileExistsError:
            try:
                if _time.time() - os.path.getmtime(lock_path) > stale_after:
                    os.remove(lock_path)       # 残锁接管
                    continue
            except OSError:
                pass
            _time.sleep(retry_s)
    else:
        raise TimeoutError(
            f"等待文件锁超时：{lock_path}（另一进程持有且未超龄？）")
    try:
        yield
    finally:
        try:
            os.remove(lock_path)
        except OSError:
            pass                # 已被接管/清理：锁的使命已尽


def atomic_replace(tmp_path: str, final_path: str) -> None:
    """tmp → final 的替换（PermissionError 重试；语义同 mt5gw.snapshot）。"""
    _snapshot_replace(tmp_path, final_path)


def _append_once(path: str, line: str) -> None:
    """O_APPEND 打开并整行一次 write（小行单次 WriteFile 原子）+ fsync。"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def append_line(path: str, line: str) -> None:
    """追加一行文本（O_APPEND 单次 write；共享冲突按 mt5gw 同款预算重试）。"""
    err: OSError | None = None
    for attempt in range(_APPEND_ATTEMPTS):
        try:
            _append_once(path, line)
            return
        except PermissionError as e:
            err = e
            if attempt == _APPEND_ATTEMPTS - 1:
                raise
            _time.sleep(_APPEND_RETRY_S)   # Windows：句柄被占，稍候重试
    raise err  # pragma: no cover（循环内必 return 或 raise）


def append_jsonl(path: str, obj) -> None:
    """JSONL 追加一行（append_line + json.dumps；行内 JSON 由调用方保证）。"""
    append_line(path, json.dumps(obj, ensure_ascii=False) + "\n")
