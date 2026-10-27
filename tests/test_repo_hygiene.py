"""CI 敏感信息扫描（T70 / 审核第十一轮 F2 的 CI 侧防线，铁律 9）。

对 `git ls-files` 的文本文件扫描四类敏感信息：
1. ``\\d{7,}@``——账号@服务器（MT5 账号 7 位以上数字紧接 @；
   6 位 magic 不会命中）；
2. ``Exness-MT5(Trial|Real)\\d+``——真实服务器名；
3. ``C:[/\\\\]Users[/\\\\](?!<)``——本机绝对路径（Windows 用户目录）；
   ``<`` 开头的占位符（如 ``C:/Users/<用户名>/``、``<demo-login>``）豁免；
4. 非私有网段 IPv4（10./127./192.168./172.16–31. 豁免；localhost 非地址，
   天然不命中）。

已知例外写 config/hygiene-allowlist.txt：每行 ``路径`` 或 ``路径:模式名``，
后跟 ``# 一行理由``（缺理由即失败）；allowlist 里没有对应残留的条目
（失效条目）同样失败，防止"修好了但豁免永久留下去"。

扫描器自身的夹具字符串在源码里拆分拼接（如 ``"9"*8 + "@"``），使本文件
自身对这四个模式干净，无需自豁免。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_PATH = REPO_ROOT / "config" / "hygiene-allowlist.txt"

HYGIENE_PATTERNS: dict[str, re.Pattern[str]] = {
    "account_at_server": re.compile(r"\d{7,}@"),
    "exness_server": re.compile(r"Exness-MT5(Trial|Real)\d+"),
    "local_path": re.compile(r"C:[/\\]Users[/\\](?!<)"),
    "ipv4": re.compile(
        r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
        r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"),
}

# 私有/回环网段（豁免）；localhost 是主机名，不会被 IPv4 模式命中
_PRIVATE_IPV4 = re.compile(
    r"^(?:10\.|127\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)")

# git ls-files 不会列出 .venv，但防御性排除仍然保留（如未来误提交）
_EXCLUDED_PARTS = ("/.venv/", "/.git/")
_BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".xlsx", ".xls",
    ".zip", ".gz", ".7z", ".tar", ".parquet", ".db", ".sqlite",
    ".pickle", ".pkl", ".exe", ".dll", ".woff", ".woff2", ".ttf",
})


def is_exempt_ipv4(ip: str) -> bool:
    """私有/回环网段的 IPv4 不算敏感（localhost 非地址，不会走到这里）。"""
    return bool(_PRIVATE_IPV4.match(ip))


def scan_text(text: str) -> list[tuple[str, int, str]]:
    """返回文本内的违规 [(模式名, 行号, 命中文本)]，不做 allowlist 处理。"""
    violations: list[tuple[str, int, str]] = []
    for name, rx in HYGIENE_PATTERNS.items():
        for m in rx.finditer(text):
            hit = m.group(0)
            if name == "ipv4" and is_exempt_ipv4(hit):
                continue
            line = text.count("\n", 0, m.start()) + 1
            violations.append((name, line, hit))
    return violations


def read_text_if_scannable(path: Path) -> str | None:
    """文本文件内容；二进制（扩展名或前 8 KB 含 NUL）或不可解码 → None。"""
    if path.suffix.lower() in _BINARY_SUFFIXES:
        return None
    try:
        head = path.open("rb").read(8192)
    except OSError:
        return None
    if b"\0" in head:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, ValueError):
        return None


def scan_files(root: Path, files: list[str],
               allowlist_path: Path | None = None) -> list[str]:
    """扫描 root 下的给定相对路径，返回豁免 allowlist 之后的违规行列表。

    allowlist_path 缺省时只在文件存在时加载（测试里允许缺席）。
    """
    entries: set[tuple[str, str | None]] = set()
    if allowlist_path is None:
        allowlist_path = ALLOWLIST_PATH
    if allowlist_path.exists():
        entries = load_allowlist(allowlist_path)
    violations: list[str] = []
    for rel in files:
        if any(part in f"/{rel}" for part in _EXCLUDED_PARTS):
            continue
        text = read_text_if_scannable(root / rel)
        if text is None:
            continue
        for name, line, hit in scan_text(text):
            if (rel, name) in entries or (rel, None) in entries:
                continue
            violations.append(f"{rel}:{line}: {name}: {hit}")
    return violations


def load_allowlist(path: Path) -> set[tuple[str, str | None]]:
    """解析 allowlist：``路径`` 或 ``路径:模式名`` + ``# 理由``（必填）。

    返回 {(路径, 模式名|None)}；条目缺理由或模式名非法 → ValueError
    （fail-closed）。
    """
    entries: set[tuple[str, str | None]] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line:
            entry, reason = line.split("#", 1)
            entry, reason = entry.strip(), reason.strip()
        else:
            entry, reason = line, ""
        if not reason:
            raise ValueError(f"allowlist 条目缺一行理由：{raw!r}")
        if ":" in entry:
            path_part, pattern = entry.split(":", 1)
            if not path_part or pattern not in HYGIENE_PATTERNS:
                raise ValueError(f"allowlist 条目非法（路径:模式名）：{raw!r}")
            entries.add((path_part, pattern))
        elif entry:
            entries.add((entry, None))
        else:
            raise ValueError(f"allowlist 条目为空：{raw!r}")
    return entries


def repo_tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT, check=True,
                         capture_output=True, text=True)
    return out.stdout.splitlines()


# ---------------------------------------------------------------- 契约测试

def test_scan_flags_account_and_server(tmp_path):
    """含账号@服务器与真实服务器名的文件必须被报出（失败测试先行项）。"""
    f = tmp_path / "note.md"
    f.write_text(f"login {'9' * 8}{'@'}{'Exness-MT5'}{'Trial8'} ok",
                 encoding="utf-8")
    violations = scan_files(tmp_path, ["note.md"],
                            allowlist_path=tmp_path / "absent.txt")
    kinds = {v.split(": ")[1] for v in violations}
    assert "account_at_server" in kinds
    assert "exness_server" in kinds


def test_scan_placeholder_login_not_flagged(tmp_path):
    """占位符 <demo-login>@<server> 不报（失败测试先行项）。"""
    f = tmp_path / "note.md"
    f.write_text("账号改为 <demo-login>@<server> 占位符", encoding="utf-8")
    assert scan_files(tmp_path, ["note.md"],
                      allowlist_path=tmp_path / "absent.txt") == []


def test_scan_local_path_real_vs_placeholder(tmp_path):
    """真实本机路径报出；<用户名> 占位形态不报。"""
    f = tmp_path / "note.md"
    f.write_text("real: " + "C:" + "/Users/" + "alice/x.py\n"
                 "placeholder: C:/Users/<用户名>/x.py\n",
                 encoding="utf-8")
    violations = scan_files(tmp_path, ["note.md"],
                            allowlist_path=tmp_path / "absent.txt")
    assert len(violations) == 1 and "local_path" in violations[0]


def test_scan_ipv4_public_flagged_private_exempt(tmp_path):
    """公网 IPv4 报出；私有/回环网段与 localhost 不报。"""
    f = tmp_path / "note.md"
    f.write_text(
        "public: 8" + ".8.8.8\n"          # 公网 DNS
        "loopback: 127.0.0.1\n"
        "private: 10.1.2.3 / 192.168.0.5 / 172.20.0.9\n"
        "hostname: localhost\n"
        "outside-172-range: 172." + "32.0.1\n",   # 172.32 不在豁免段 → 报
        encoding="utf-8")
    violations = scan_files(tmp_path, ["note.md"],
                            allowlist_path=tmp_path / "absent.txt")
    hits = [v for v in violations if "ipv4" in v]
    assert len(hits) == 2      # 8.8.8.8 与 172.32.0.1


def test_scan_skips_binary_and_venv(tmp_path):
    """二进制（NUL / 后缀）与 .venv 路径跳过。"""
    (tmp_path / "blob.bin").write_bytes(b"12345678" + b"\x00" + b"9" * 20)
    (tmp_path / "img.png").write_bytes(b"\x89PNG not text " + b"9" * 40)
    v = tmp_path / ".venv"
    v.mkdir()
    (v / "leak.txt").write_text("9" * 8 + "@x", encoding="utf-8")
    assert scan_files(tmp_path, ["blob.bin", "img.png", ".venv/leak.txt"],
                      allowlist_path=tmp_path / "absent.txt") == []


def test_allowlist_entry_with_reason_suppresses(tmp_path):
    """allowlist 命中的条目（文件:模式）被豁免；缺理由的条目本身报错。"""
    f = tmp_path / "note.md"
    f.write_text("login 1234567" + "8@srv", encoding="utf-8")
    al = tmp_path / "al.txt"
    al.write_text("note.md:account_at_server  # 演练用的示例豁免\n",
                  encoding="utf-8")
    assert scan_files(tmp_path, ["note.md"], allowlist_path=al) == []
    bad = tmp_path / "bad.txt"
    bad.write_text("note.md:account_at_server\n", encoding="utf-8")
    try:
        load_allowlist(bad)
        raised = False
    except ValueError:
        raised = True
    assert raised, "allowlist 条目缺理由必须 ValueError"


# ---------------------------------------------------------------- 仓库级测试

def _raw_hits(root: Path, files: list[str]) -> dict[str, set[str]]:
    """{文件: {命中的模式名}}（allowlist 之前的原始命中）。"""
    hits: dict[str, set[str]] = {}
    for rel in files:
        text = read_text_if_scannable(root / rel)
        if text is not None:
            found = {name for name, _, _ in scan_text(text)}
            if found:
                hits[rel] = found
    return hits


def test_repo_tracked_files_are_clean():
    """全仓库（git ls-files 文本文件）经 allowlist 豁免后零敏感信息。"""
    files = repo_tracked_files()
    violations = scan_files(REPO_ROOT, files)
    assert violations == [], f"敏感信息残留 {len(violations)} 处:\n" + "\n".join(
        violations[:20])


def test_allowlist_has_no_stale_entries():
    """allowlist 每条都能对应现存残留；修好后不清理豁免条目 → 失败。"""
    files = repo_tracked_files()
    hits = _raw_hits(REPO_ROOT, files)
    entries = load_allowlist(ALLOWLIST_PATH)
    stale = []
    for path, pattern in sorted(entries):
        if pattern is not None:
            if pattern not in hits.get(path, set()):
                stale.append(f"{path}:{pattern}")
        elif not hits.get(path):
            stale.append(path)
    assert stale == [], f"allowlist 失效条目（对应残留已修，应删除）: {stale}"
