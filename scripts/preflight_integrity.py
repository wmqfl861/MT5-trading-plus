"""T170/D70（G16-1）：研究运行前机械完整性预检——一条命令，JSON 摘要。

计划 docs/handoff/2026-11-08-round16-plan.md T170 第 1 项的六项机械检查
（钉定文件哈希 / 覆盖与预筛一致 / 台账链校验 / 冻结配置哈希 / 磁盘余量 /
工作树干净），输出直接附在裁定后（裁定 ≤2 页，模板
docs/templates/ruling-template.md）。口径细节见各检查函数的注释与
docs/reviews/2026-11-07-r3-s8b-lineending-ruling.md（S8b，原始字节钉与
LF 归一口径）。退出码：0=全 pass；2=有 warn 无 fail；1=有 fail。
所有路径参数化；缺省台账/数据根指向研究库 mt5-pline。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5quant.research.candidates import (  # noqa: E402
    coverage_segments as _coverage_segments, merged_coverage_years)
from mt5quant.research.ledger import Ledger  # noqa: E402
from mt5quant.validation.gates import gates_sha256  # noqa: E402

REPO_DEFAULT = Path(__file__).resolve().parents[1]
RESEARCH_ROOT_DEFAULT = REPO_DEFAULT.parent / "mt5-pline"
GIGA = 1 << 30
DISK_WARN_GB, DISK_REJECT_GB = 30, 15        # T146：30G 告警 / 15G 拒绝
COVERAGE_TOLERANCE = 0.9                     # 引擎 require_runtime_coverage 同口径


def sha256_raw(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_lf(raw: bytes) -> str:
    """规范化 LF 口径哈希（G16-4：裸字节哈希在 CRLF 检出下会假阳性）。"""
    return hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def _ro_connect(ledger_path: Path) -> sqlite3.Connection:
    """台账只读连接（run6 裁定纪律：mode=ro 直查，绝不触发迁移/写入）。"""
    conn = sqlite3.connect(
        f"file:{Path(ledger_path).resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------ 1) 钉定文件

def check_pinned_files(repo: Path, ledger_path: Path,
                       extra_pins_file: Path | None = None) -> dict:
    """台账钉（rounds.toml_sha256 / candidate_checks 最新行
    candidate_sha256，均为**原始字节**口径，bt.py H-08 同源）vs 磁盘文件。
    每文件同时计算并注明两种口径：sha256_lf（规范化 LF）与 sha256_raw
    （原始字节）。判定按钉的口径：原始字节钉原始==钉即 pass（被钉工件
    本身可含 CRLF，如 fomc_meetings.json——此时 LF 归一哈希天然不同，
    只注明）；LF==钉而原始!=钉 = S8b 形态漂移（warn：内容未变，H-08
    原始字节核对会拒绝，复原按 S8b §2 协议）；口径不符 = fail（真内容
    变更）。--extra-pins 注入日历工件等额外钉，basis 可选 lf。"""
    conn = _ro_connect(ledger_path)
    try:
        rounds = [dict(r) for r in conn.execute(
            "SELECT round_id, toml_sha256 FROM rounds ORDER BY round_id")]
        checks = [dict(r) for r in conn.execute(
            "SELECT candidate_id, result_json FROM candidate_checks"
            " ORDER BY rowid")]
    finally:
        conn.close()
    latest: dict[str, dict] = {}
    for r in checks:                        # rowid 序 → 后行覆盖 = 最新行
        try:
            payload = json.loads(r["result_json"])
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("candidate_sha256"):
            latest[r["candidate_id"]] = payload
    targets = [
        (f"rounds.{r['round_id']}.toml_sha256",
         Path(repo) / "research" / "rounds" / f"{r['round_id']}.toml",
         r["toml_sha256"], "raw_bytes") for r in rounds]
    targets += [
        (f"candidate_checks.{cid}.candidate_sha256",
         Path(repo) / "research" / "candidates" / f"{cid}.toml",
         p["candidate_sha256"], "raw_bytes")
        for cid, p in sorted(latest.items())]
    if extra_pins_file is not None:
        for p in json.loads(Path(extra_pins_file).read_text("utf-8")):
            targets.append((f"extra:{p.get('note', Path(p['path']).name)}",
                            Path(p["path"]), p["sha256"],
                            p.get("basis", "raw_bytes")))
    files, worst = [], "pass"
    order = {"pass": 0, "form_drift": 1, "fail": 2}
    for src, path, pin, basis in targets:
        entry = {"pin_source": src, "path": str(path), "pin": pin,
                 "pin_basis": basis, "exists": path.is_file()}
        if not entry["exists"]:
            entry.update(status="fail", error="钉定文件不存在",
                         sha256_raw=None, sha256_lf=None,
                         match_raw=False, match_lf=False)
        else:
            raw = path.read_bytes()
            h_raw, h_lf = sha256_raw(raw), sha256_lf(raw)
            entry.update(sha256_raw=h_raw, sha256_lf=h_lf,
                         match_raw=h_raw == pin, match_lf=h_lf == pin)
            if basis == "lf":               # LF 钉：LF 归一口径即判定
                entry["status"] = "pass" if entry["match_lf"] else "fail"
                if entry["status"] == "fail":
                    entry["note"] = "LF 归一口径与钉不符"
            elif entry["match_raw"]:
                # 原始字节钉：原始字节口径即判定（被钉文件本身可含 CRLF，
                # 此时 LF 归一哈希天然不同——两种口径都注明，不影响判定）
                entry["status"] = "pass"
            elif entry["match_lf"]:         # 内容一致、原始字节漂移（S8b）
                entry["status"] = "form_drift"
                entry["note"] = ("LF 归一==钉（内容未变）而原始字节!=钉——"
                                 "原始字节钉（bt.py H-08）将拒绝运行；按"
                                 " S8b §2 形态复原协议处理后重跑本检查")
            else:
                entry["status"] = "fail"
                entry["note"] = "内容与台账钉不符（两种口径都不匹配）"
        files.append(entry)
        if order[entry["status"]] > order[worst]:
            worst = entry["status"]
    if not files:
        return {"status": "skipped",
                "reason": "台账无 rounds/candidate_checks 钉", "files": []}
    return {"status": {"pass": "pass", "form_drift": "warn",
                       "fail": "fail"}[worst],
            "worst_file_status": worst,
            "basis_note": ("台账钉为原始字节口径；每文件同时给出 sha256_lf"
                           "（规范化 LF）与 sha256_raw（原始字节）两种口径"
                           "的值与匹配结果"), "files": files}


# ------------------------------------------------------ 2) 覆盖与预筛一致

def check_coverage(ledger_path: Path, data_root: Path) -> dict:
    """candidate_checks 最新行 checks.power.coverage_segments 逐 SYMBOL/TF
    对 data-root 实测（复用 candidates.coverage_segments /
    merged_coverage_years，与 bt candidate check 同一测量）；实测 < 90%×
    台账年数 = fail（容差沿引擎 require_runtime_coverage；预筛后数据只增
    不减是 pass）。无 coverage_segments 的候选（事件模式/旧口径/簿记）跳过
    并注明。"""
    conn = _ro_connect(ledger_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT candidate_id, result_json FROM candidate_checks"
            " ORDER BY rowid")]
    finally:
        conn.close()
    latest: dict[str, dict] = {}
    for r in rows:
        try:
            payload = json.loads(r["result_json"])
        except ValueError:
            continue
        latest[r["candidate_id"]] = payload or {}
    results = []
    for cid, payload in sorted(latest.items()):
        segs = ((payload.get("checks") or {}).get("power") or {}
                ).get("coverage_segments")
        if not segs or not any(segs.values()):
            results.append({"candidate_id": cid, "status": "skipped",
                            "reason": "最新预筛行无 coverage_segments"
                                      "（事件模式/旧口径/簿记候选）"})
            continue
        entry = {"candidate_id": cid, "keys": {}}
        for key, seg_list in sorted(segs.items()):
            ledger_years = merged_coverage_years(seg_list)
            k = {"ledger_years": ledger_years}
            if "/" not in key:
                k.update(ok=False, error=f"非法序列键 {key!r}")
            else:
                sym, tf = key.split("/", 1)
                measured = _coverage_segments(str(data_root), [sym], [tf])
                if key not in measured:
                    k.update(ok=False, measured_years=None,
                             error=f"实测缺序列 {key}（数据根 {data_root}）")
                else:
                    k["measured_years"] = merged_coverage_years(measured[key])
                    k["ok"] = (k["measured_years"] > 0
                               and (k["measured_years"] >=
                                    COVERAGE_TOLERANCE * ledger_years))
                    if not k["ok"]:
                        k["error"] = (f"实测 {k['measured_years']:.4f} 年 < "
                                      f"{COVERAGE_TOLERANCE:.0%}×台账 "
                                      f"{ledger_years:.4f} 年")
            entry["keys"][key] = k
        entry["status"] = ("pass" if all(k.get("ok") for k in
                                         entry["keys"].values()) else "fail")
        results.append(entry)
    if not results:
        return {"status": "skipped", "reason": "candidate_checks 无记录",
                "tolerance": COVERAGE_TOLERANCE, "candidates": []}
    statuses = [e["status"] for e in results]
    return {"status": ("fail" if "fail" in statuses else
                       ("pass" if "pass" in statuses else "skipped")),
            "tolerance": COVERAGE_TOLERANCE,
            "tolerance_note": "实测 ≥ 90%×台账年数（require_runtime_"
                              "coverage 同口径；预筛后数据只增不减为 pass）",
            "candidates": results}


# ------------------------------------------------------------ 3) 台账链

def check_ledger_chain(ledger_path: Path) -> dict:
    """复用 mt5quant.research.ledger.Ledger.verify_chain（import，不重写）。
    生产台账只读：mode=ro 读 chain_meta 概要 + sqlite backup API 快照到
    临时副本，verify_chain 在副本上执行、用后即删——原库零写入零锁升级。"""
    conn = _ro_connect(ledger_path)
    try:
        meta = {r["table_name"]: {"row_count": r["row_count"],
                                  "head_hash": r["head_hash"]}
                for r in conn.execute(
                    "SELECT table_name, row_count, head_hash FROM chain_meta")}
    finally:
        conn.close()
    fd, name = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)                             # Windows：句柄不关则删不掉
    tmp = Path(name)
    try:
        dst = sqlite3.connect(str(tmp))
        try:
            conn = _ro_connect(ledger_path)
            try:
                with dst:
                    conn.backup(dst)
            finally:
                conn.close()
        finally:
            dst.close()
        led = Ledger(tmp, create=False)
        try:
            problems = led.verify_chain()
        finally:
            led.close()
    finally:
        tmp.unlink(missing_ok=True)
    return {"status": "pass" if not problems else "fail",
            "problems": problems, "chain_meta": meta,
            "note": "mode=ro 快照 → 临时副本上 verify_chain（复用"
                    " mt5quant.research.ledger），副本用后即删；生产台账零写入"}


# --------------------------------------------------------- 4) 冻结配置

def check_frozen_config(repo: Path, expected: str | None = None,
                        ledger_path: Path | None = None) -> dict:
    """config/gates.toml 的 LF 归一哈希（vgate.gates_sha256——与 bt gate/
    runs.gates_hash 同口径，CRLF 检出不改变值）vs 参考值；参考值优先级：
    --expected-gates-hash > 台账最新非空 runs.gates_hash。无参考值 → warn。"""
    path = Path(repo) / "config" / "gates.toml"
    out: dict = {"gates_path": str(path)}
    if not path.is_file():
        return dict(out, status="fail", error="缺 config/gates.toml")
    out["sha256_lf"] = gates_sha256(path)
    if expected is None and ledger_path is not None:
        conn = _ro_connect(ledger_path)
        try:
            row = conn.execute(
                "SELECT gates_hash FROM runs WHERE gates_hash!=''"
                " ORDER BY rowid DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        if row is not None:
            expected, out["expected_source"] = row["gates_hash"], \
                "latest runs.gates_hash"
    if expected is None:
        return dict(out, status="warn", error="无参考值（未给 "
                    "--expected-gates-hash 且台账无 runs.gates_hash）")
    out["expected"] = expected
    out["match"] = out["sha256_lf"] == expected
    if not out["match"]:
        out["error"] = ("gates.toml 与冻结参考值不符（LF 归一口径）——"
                        "冻结配置在最近一次运行后被改动")
    out["status"] = "pass" if out["match"] else "fail"
    return out


# ------------------------------------------------------------ 5) 磁盘余量

def check_disk(disk_path: Path, warn_gb: int = DISK_WARN_GB,
               reject_gb: int = DISK_REJECT_GB) -> dict:
    """T146：≥30G pass；<30G warn（日检告警）；<15G fail（禁止研究运行）。"""
    free = shutil.disk_usage(str(disk_path)).free
    free_gb = free / GIGA
    status = "pass" if free_gb >= warn_gb else (
        "warn" if free_gb >= reject_gb else "fail")
    return {"status": status, "path": str(disk_path),
            "free_gb": round(free_gb, 2), "free_bytes": free,
            "warn_gb": warn_gb, "reject_gb": reject_gb,
            "note": "T146：<30G 日检告警；<15G 禁止研究运行（fail）"}


# ---------------------------------------------------------- 6) 工作树干净

def check_worktree_clean(repo: Path) -> dict:
    """git status --porcelain：跟踪文件改动 = fail；仅 untracked = warn
    （裁定/证据文件惯例豁免，run6 裁定 §1.1-1 同例外）；干净 = pass。"""

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    head = git("rev-parse", "HEAD")
    if head.returncode != 0:
        return {"status": "fail", "error": "非 git 仓库或 git 失败："
                f"{head.stderr.strip()}", "porcelain": []}
    st = git("status", "--porcelain")
    entries = [ln for ln in st.stdout.splitlines() if ln.strip()]
    tracked = [e for e in entries if not e.startswith("??")]
    untracked = [e for e in entries if e.startswith("??")]
    status = "fail" if tracked else ("warn" if untracked else "pass")
    return {"status": status, "head": head.stdout.strip(),
            "porcelain": entries, "tracked_changes": tracked,
            "untracked": untracked,
            "note": "跟踪文件改动=fail；仅 untracked=warn（裁定/证据文件"
                    "惯例豁免，run6 裁定 §1.1-1）"}


# ----------------------------------------------------------------- 组装

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="preflight_integrity.py", description=(
            "T170/D70 机械完整性预检：钉定哈希/覆盖一致/台账链/冻结配置/"
            "磁盘余量/工作树干净 → JSON 摘要（裁定附件）"))
    ap.add_argument("--repo", default=str(REPO_DEFAULT),
                    help="被检仓库工作树（缺省=脚本所在仓库）")
    ap.add_argument("--ledger", default=str(
        RESEARCH_ROOT_DEFAULT / "data" / "research" / "ledger.sqlite"),
        help="研究台账（缺省=研究库 mt5-pline 台账）")
    ap.add_argument("--data-root", default=str(
        RESEARCH_ROOT_DEFAULT / "data_research"),
        help="研究数据根（缺省=研究库 data_research）")
    ap.add_argument("--expected-gates-hash", default=None,
                    help="冻结 gates.toml 期望 sha256（LF 归一；缺省取台账"
                         "最新 runs.gates_hash）")
    ap.add_argument("--extra-pins", default=None,
                    help="额外钉 JSON：[{path, sha256, basis: lf|raw_bytes,"
                         " note}]（日历工件等）")
    ap.add_argument("--disk-path", default=None,
                    help="磁盘余量检查路径（缺省=data-root）")
    ap.add_argument("--disk-warn-gb", type=int, default=DISK_WARN_GB)
    ap.add_argument("--disk-reject-gb", type=int, default=DISK_REJECT_GB)
    ap.add_argument("--out", default=None, help="JSON 摘要输出文件")
    return ap


def run_all(args) -> dict:
    repo, ledger = Path(args.repo), Path(args.ledger)
    data_root = Path(args.data_root)
    checks = {
        "pinned_files": check_pinned_files(
            repo, ledger, Path(args.extra_pins) if args.extra_pins else None),
        "coverage_vs_precheck": check_coverage(ledger, data_root),
        "ledger_chain": check_ledger_chain(ledger),
        "frozen_config": check_frozen_config(
            repo, args.expected_gates_hash, ledger),
        "disk_free": check_disk(
            Path(args.disk_path) if args.disk_path else data_root,
            args.disk_warn_gb, args.disk_reject_gb),
        "worktree_clean": check_worktree_clean(repo),
    }
    counts = {s: sum(1 for c in checks.values() if c["status"] == s)
              for s in ("pass", "warn", "fail", "skipped")}
    overall = "fail" if counts["fail"] else ("warn" if counts["warn"]
                                             else "pass")
    return {"schema": "t170-preflight/1", "generated_utc": _utc(),
            "inputs": {"repo": str(repo), "ledger": str(ledger),
                       "data_root": str(data_root),
                       "python": sys.version.split()[0]},
            "checks": checks,
            "summary": dict(counts, overall=overall)}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_all(args)
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return {"pass": 0, "warn": 2, "fail": 1}[summary["summary"]["overall"]]


if __name__ == "__main__":
    sys.exit(main())
