# recovery-20261119A 树外 supervisor 宿主（codex 裁定 B）：
# 唯一改动=重启子进程 creationflags CREATE_NO_WINDOW→0（继承本宿主隐藏控制台，不再产生 conhost 子进程）
import sys
sys.path.insert(0, r"<deploy>\scripts")
import live_supervisor as s
_orig_popen = s.subprocess.Popen.__init__
def _patched(self, *a, **k):
    if k.get("creationflags") == 0x08000000:  # CREATE_NO_WINDOW
        k["creationflags"] = 0
    return _orig_popen(self, *a, **k)
s.subprocess.Popen.__init__ = _patched
sys.argv = [sys.argv[0]] + sys.argv[1:]
raise SystemExit(s.main())
