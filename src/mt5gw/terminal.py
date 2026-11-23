"""Is the trading terminal process running? (T209, plan round 23 §2.2)

The real MetaTrader5 package starts the terminal when ``initialize()`` finds no
process for the configured path. A worker that does that becomes the terminal's
parent; the supervisor then refuses the worker's tree for as long as the terminal
lives (11-21), and the terminal dies with the worker. So the gateway asks here
first and never calls ``initialize()`` while the terminal is absent.

The lookup stays inside the calling process (ctypes only). A worker must not
start any child process: the supervisor refuses unknown descendants.
"""
from __future__ import annotations

import ctypes
import ntpath
import os

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TH32CS_SNAPPROCESS = 0x00000002


def _same_image(left: str, right: str) -> bool:
    """Same file, allowing for case, separators, short (8.3) names and links.

    Both paths are resolved on disk first (read-only, in-process); a path that
    cannot be resolved is compared as spelled.
    """
    def spell(path: str) -> str:
        try:
            path = os.path.realpath(path)
        except (OSError, ValueError):
            pass
        return ntpath.normpath(path).lower()
    return bool(left) and bool(right) and spell(left) == spell(right)


def _kernel():
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    return kernel, wintypes


def _image_path(kernel, wintypes, pid: int) -> str | None:
    """Full image path of a process; None when it cannot be opened or queried."""
    handle = kernel.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return buffer.value
    finally:
        kernel.CloseHandle(handle)


def _pids_named(image_name: str) -> list[int]:
    """PIDs of the processes whose image file name matches (case-insensitive)."""
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        # PROCESSENTRY32W; th32DefaultHeapID is ULONG_PTR (pointer-sized).
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    snapshot = kernel.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        return []
    wanted = image_name.lower()
    found = []
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.szExeFile.lower() == wanted:
                found.append(entry.th32ProcessID)
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return found


def terminal_running(terminal_path: str) -> bool:
    """True only when a process whose image is exactly terminal_path is running.

    False whenever it cannot be told (not Windows, no path, snapshot failed):
    the cost of a wrong False is one refused initialize call, the cost of a
    wrong True is a terminal launched under a worker.
    """
    if os.name != "nt" or not terminal_path:
        return False
    try:
        candidates = _pids_named(ntpath.basename(str(terminal_path)))
        if not candidates:
            return False
        kernel, wintypes = _kernel()
        for pid in candidates:
            image = _image_path(kernel, wintypes, pid)
            if image and _same_image(image, terminal_path):
                return True
    except (OSError, AttributeError, ValueError):
        return False
    return False
