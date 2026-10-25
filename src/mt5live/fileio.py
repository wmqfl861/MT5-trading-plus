"""mt5live.fileio（T33/M1、M2、M8、L3）：跨进程文件契约的唯一写入口。

data/live/ 下所有会被**另一进程**读的文件，写入一律经本模块（评审 M1/M8：
Windows 下目标文件被别的进程占着句柄时，os.replace 会抛 PermissionError、
多进程共用同一文件的追加可能行交错——重试与单次 write 的纪律集中在这里，
各处不得再自带 tmp+replace 或裸 append）：

- write_json_atomic / atomic_replace：**直接 import 复用** mt5gw.snapshot
  （tmp + fsync + ``_atomic_replace`` 的 PermissionError 重试，总预算约 3 秒）；
- append_line / append_jsonl：O_APPEND 打开 + 整行一次 write（追加写对单行
  原子，多运行器并发不再交错）+ 共享冲突同预算重试。

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

# 直接 import 复用 mt5gw.snapshot 的原子写与 PermissionError 重试（M1 根因的
# 解法——本模块不另写一份重试逻辑）
from mt5gw.snapshot import write_json_atomic
from mt5gw.snapshot import _atomic_replace as _snapshot_replace

__all__ = ["append_jsonl", "append_line", "atomic_replace", "write_json_atomic"]

_APPEND_ATTEMPTS = 10          # 与 mt5gw.snapshot._atomic_replace 同预算
_APPEND_RETRY_S = 0.3


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
