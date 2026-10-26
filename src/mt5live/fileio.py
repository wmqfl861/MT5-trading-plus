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
- file_lock：跨进程文件锁（R7/I1 残留 N-I2 起：**内核字节锁**——Windows
  msvcrt.locking / POSIX flock）——供"先读后写"的复合操作（如全平指令的
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
import uuid as _uuid
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
_LOCK_STALE_AFTER_S = 600.0    # 兼容保留（R7/I1 起内核锁不按 mtime 接管）

# 内核字节锁（R7/I1 残留 N-I2）：Windows 用 msvcrt.locking（LockFile），
# POSIX 用 flock——互斥由内核强制、同进程不同 fd 也冲突（线程间同样互斥）、
# 持锁进程崩溃/退出时内核自动释放（残锁不复存在，无需也不允许超龄接管）
try:
    import msvcrt as _msvcrt
except ImportError:            # pragma: no cover - 非 Windows 回退
    _msvcrt = None
    import fcntl as _fcntl


def _kernel_lock(fd: int) -> None:
    if _msvcrt is not None:
        _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
    else:                      # pragma: no cover - 非 Windows 回退
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)


def _kernel_unlock(fd: int) -> None:
    if _msvcrt is not None:
        _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
    else:                      # pragma: no cover - 非 Windows 回退
        _fcntl.flock(fd, _fcntl.LOCK_UN)


@contextmanager
def file_lock(lock_path: str, *, stale_after: float = _LOCK_STALE_AFTER_S,
              attempts: int = _LOCK_ATTEMPTS, retry_s: float = _LOCK_RETRY_S):
    """跨进程文件锁（R6/I1 引入；R7/I1 残留 N-I2 重设计）。

    旧实现的"读 mtime → 判超龄 → 直接删路径接管"存在双所有者竞态：接管
    者 A 读到旧 mtime 后暂停，B 删旧锁取新锁（活锁），A 恢复后删掉的是
    B 的新锁，两人同时进入临界区；释放侧同样裸删路径，会删掉接管者的
    新锁。**凭路径和时间不能安全接管**（N-I2 结论）。

    R7 起改为内核字节锁：锁住锁文件的 1 个字节（Windows LockFile /
    POSIX flock）——互斥由内核仲裁，活锁持有期间任何人拿不到；持锁进程
    崩溃内核自动释放（残锁不复存在，接管路径整体删除）；释放只解自己
    fd 上的锁，不存在删别人锁的可能。锁文件常驻磁盘（不再删除——
    "释放即删"本身就是竞态源之一），内容为 pid+token+时间戳供排查。

    attempts 次仍拿不到 → TimeoutError（不静默放行——fail-closed）。
    stale_after 仅为兼容保留、被忽略：内核锁没有"超龄"语义，也不允许
    对活锁持有者的接管。
    """
    d = os.path.dirname(lock_path)
    if d:
        os.makedirs(d, exist_ok=True)
    fd: int | None = None
    for _ in range(attempts):
        fd = os.open(lock_path,
                     os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0))
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            _kernel_lock(fd)          # 内核仲裁：他人持有 → OSError → 重试
            break
        except OSError:
            os.close(fd)
            fd = None
            _time.sleep(retry_s)
    if fd is None:
        raise TimeoutError(
            f"等待文件锁超时：{lock_path}（另一持有者 {attempts}×{retry_s}s "
            "内未释放——内核锁不做超龄接管，fail-closed）")
    try:
        # 持锁后写入持有者信息（排查用；下一持有者覆盖）
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()} {_uuid.uuid4().hex} "
                     f"{_time.time():.3f}".encode())
        yield
    finally:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            _kernel_unlock(fd)
        finally:
            os.close(fd)


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
