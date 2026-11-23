# O23-1 supervisor 宿主（安全版：路径验证+叶节点检查）
import sys
sys.path.insert(0, r"<deploy>\scripts")
import live_supervisor as s
import os, pathlib

# P1: CREATE_NO_WINDOW → 0
_orig_popen = s.subprocess.Popen.__init__
def _p1(self, *a, **k):
    if k.get("creationflags") == 0x08000000:
        k["creationflags"] = 0
    return _orig_popen(self, *a, **k)
s.subprocess.Popen.__init__ = _p1

# P2+P3: canonical pathlib 归一化
def _fc(path):
    try: return os.path.normcase(str(pathlib.Path(path).resolve()))
    except (OSError, ValueError): return os.path.normcase(os.path.realpath(path))
s.canonical = _fc

# P4: verified_tree 安全跳过系统 conhost 叶节点
_orig_vt = s.verified_tree
def _p4_vt(entry, table, argv):
    _sys = pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "conhost.exe")
    filtered = {}
    for pid, proc in table.items():
        _exe = proc.get("executable") or ""
        try:
            _is_conhost = pathlib.Path(_exe).resolve() == _sys.resolve()
        except (OSError, ValueError):
            _is_conhost = False
        _has_children = any(c.get("parent_pid") == pid for c in table.values())
        if _is_conhost and not _has_children:
            continue  # verified system conhost leaf — skip
        filtered[pid] = proc
    return _orig_vt(entry, filtered, argv)
s.verified_tree = _p4_vt

raise SystemExit(s.main())
