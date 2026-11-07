"""scripts/preflight_integrity.py（T170/D70，G16-1）：一条命令的机械完整性预检。

每个检查项的通过/失败两态都用合成夹具验证（TDD）：
- 钉定文件哈希（LF 归一口径为主、原始字节钉注明两种口径）；
- 覆盖与预筛一致（candidate_checks 最新行 coverage_segments vs 实测）；
- 台账链校验（复用 mt5quant.research.ledger.Ledger.verify_chain，临时副本）；
- 冻结配置哈希（gates.toml LF 归一）；
- 磁盘余量（30G 告警 / 15G 拒绝）；
- 工作树干净（git status --porcelain）。
"""
import hashlib
import importlib.util
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

SCRIPT_PATH = (Path(__file__).resolve().parents[1] / "scripts"
               / "preflight_integrity.py")


def load_pf():
    spec = importlib.util.spec_from_file_location("preflight_integrity_t170",
                                                  SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pf():
    return load_pf()


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha_lf(raw: bytes) -> str:
    return hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


R3_TEXT = 'id = "R3"\nopened = "2026-11-06"\ncandidates = ["C005"]\n'
C005_TEXT = 'id = "C005"\ntitle = "合成候选"\nsymbols = ["USTEC"]\n'
GATES_TEXT = 'version = 1\n[g1]\nmin_trades = 10\n'


def build_repo(tmp_path: Path) -> Path:
    """合成主仓：rounds/candidates/gates.toml + git 干净工作树。"""
    repo = tmp_path / "repo"
    (repo / "research" / "rounds").mkdir(parents=True)
    (repo / "research" / "candidates").mkdir(parents=True)
    (repo / "config").mkdir(parents=True)
    (repo / "research" / "rounds" / "R3.toml").write_bytes(R3_TEXT.encode())
    (repo / "research" / "candidates" / "C005.toml").write_bytes(
        C005_TEXT.encode())
    (repo / "config" / "gates.toml").write_bytes(GATES_TEXT.encode())
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo,
                   check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "synthetic"], cwd=repo,
                   check=True)
    return repo


def build_ledger(path: Path, repo: Path, *, r3_bytes=None, c005_bytes=None,
                 gates_sha=None, coverage=None) -> Path:
    """合成台账：R3 开轮钉 + C005 预筛绑定 + 带 gates_hash 的 runs 行。"""
    from mt5quant.research.ledger import Ledger
    r3 = r3_bytes if r3_bytes is not None else R3_TEXT.encode()
    c005 = c005_bytes if c005_bytes is not None else C005_TEXT.encode()
    cov = coverage if coverage is not None else {
        "USTEC/D1": [{"source": "fred", "first": "2015-01-01T00:00:00",
                      "last": "2020-01-01T00:00:00", "years": 5.0,
                      "counted": True}]}
    with Ledger(path) as led:
        led.record_round_open(
            round_id="R3", candidates=["C005"], trial_budget=2,
            lockbox_uses_max=1, dsr_trial_scope="rounds_lineage",
            advance_rule="synthetic", close_rule="synthetic",
            approved_by="test", evaluation={},
            toml_sha256=_sha(r3), git_commit="synthetic")
        led.record_candidate_check("C005", decision="pass", result={
            "candidate_sha256": _sha(c005),
            "checks": {"power": {"coverage_segments": cov}}})
        led.record_run(
            card_id="H102", card_version=1, strategy="s", params={},
            symbol="USTEC", timeframe="D1", data_first=None, data_last=None,
            data_sha256="0" * 64, engine_version="e",
            cost_model_version="c", gates_hash=gates_sha or _sha_lf(
                GATES_TEXT.encode()), metrics={"ok": True}, actor="bt")
    return path


def build_data_root(tmp_path: Path, first="2015-01-01T00:00:00",
                    last="2020-01-01T00:00:00") -> Path:
    """合成研究库：只有 meta.json 水位（无 parquet → coverage 用水位口径）。"""
    root = tmp_path / "data_research"
    d = root / "bars" / "USTEC" / "D1"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({
        "symbol": "USTEC", "timeframe": "D1", "first_time": first,
        "last_closed_time": last}), encoding="utf-8")
    return root


def ro_rows(ledger_path, sql, args=()):
    conn = sqlite3.connect(f"file:{Path(ledger_path).as_posix()}?mode=ro",
                           uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


# ------------------------------------------------ 1) 钉定文件哈希

def test_pinned_files_pass(pf, tmp_path):
    repo = build_repo(tmp_path)
    led = build_ledger(tmp_path / "ledger.sqlite", repo)
    out = pf.check_pinned_files(repo, led)
    assert out["status"] == "pass"
    by_pin = {f["pin_source"]: f for f in out["files"]}
    r3 = by_pin["rounds.R3.toml_sha256"]
    assert r3["pin"] == _sha(R3_TEXT.encode())
    assert r3["match_raw"] and r3["match_lf"]
    assert r3["pin_basis"] == "raw_bytes"
    assert "sha256_raw" in r3 and "sha256_lf" in r3   # 两种口径都注明
    assert by_pin["candidate_checks.C005.candidate_sha256"]["match_raw"]


def test_pinned_files_content_change_fails(pf, tmp_path):
    repo = build_repo(tmp_path)
    led = build_ledger(tmp_path / "ledger.sqlite", repo)
    (repo / "research" / "rounds" / "R3.toml").write_text(
        'id = "R3"\ncandidates = ["C005", "C009"]\n', encoding="utf-8")
    out = pf.check_pinned_files(repo, led)
    assert out["status"] == "fail"
    r3 = [f for f in out["files"] if f["pin_source"]
          == "rounds.R3.toml_sha256"][0]
    assert r3["status"] == "fail" and not r3["match_lf"]


def test_pinned_files_crlf_form_drift_warns(pf, tmp_path):
    """S8b 场景：内容相同、CRLF 形态——LF 归一==钉、原始字节!=钉。"""
    repo = build_repo(tmp_path)
    led = build_ledger(tmp_path / "ledger.sqlite", repo)
    p = repo / "research" / "candidates" / "C005.toml"
    p.write_bytes(C005_TEXT.encode().replace(b"\n", b"\r\n"))
    out = pf.check_pinned_files(repo, led)
    assert out["status"] == "warn"
    c5 = [f for f in out["files"] if f["pin_source"]
          == "candidate_checks.C005.candidate_sha256"][0]
    assert c5["status"] == "form_drift"
    assert c5["match_lf"] and not c5["match_raw"]


def test_raw_pin_on_crlf_file_passes(pf, tmp_path):
    """回归（真实证据 run2 发现）：原始字节钉对**本身含 CRLF** 的被钉文件
    （fomc_meetings.json 等 Windows 导出工件）——原始字节==钉即 pass，
    LF 归一口径天然不同、只注明不参与判定。"""
    repo = build_repo(tmp_path)
    led = build_ledger(tmp_path / "ledger.sqlite", repo)
    art = tmp_path / "artifact.json"
    raw = b'{"a": 1}\r\n{"b": 2}\r\n'
    art.write_bytes(raw)
    pins = tmp_path / "pins.json"
    pins.write_text(json.dumps([{
        "path": str(art), "sha256": _sha(raw),   # 钉=CRLF 原始字节
        "basis": "raw_bytes", "note": "win artifact"}]), encoding="utf-8")
    out = pf.check_pinned_files(repo, led, extra_pins_file=pins)
    assert out["status"] == "pass"
    f = [x for x in out["files"] if x["pin_source"].startswith("extra:")][0]
    assert f["status"] == "pass"
    assert f["match_raw"] and not f["match_lf"]   # 两口径都如实注明


def test_pinned_files_missing_fails(pf, tmp_path):
    repo = build_repo(tmp_path)
    led = build_ledger(tmp_path / "ledger.sqlite", repo)
    (repo / "research" / "rounds" / "R3.toml").unlink()
    out = pf.check_pinned_files(repo, led)
    assert out["status"] == "fail"


def test_extra_pins_lf_basis(pf, tmp_path):
    repo = build_repo(tmp_path)
    led = build_ledger(tmp_path / "ledger.sqlite", repo)
    cal = tmp_path / "calendar.json"
    raw = b'{"events": []}\r\n'
    cal.write_bytes(raw)
    pins_file = tmp_path / "extra.json"
    pins_file.write_text(json.dumps([{
        "path": str(cal), "sha256": _sha_lf(raw), "basis": "lf",
        "note": "calendar artifact"}]), encoding="utf-8")
    out = pf.check_pinned_files(repo, led, extra_pins_file=pins_file)
    assert out["status"] == "pass"
    cal_pin = [f for f in out["files"]
               if f["pin_source"].startswith("extra:")][0]
    assert cal_pin["pin_basis"] == "lf" and cal_pin["match_lf"]
    # 钉不匹配 → fail
    pins_file.write_text(json.dumps([{
        "path": str(cal), "sha256": "0" * 64, "basis": "lf"}]),
        encoding="utf-8")
    out = pf.check_pinned_files(repo, led, extra_pins_file=pins_file)
    assert out["status"] == "fail"


# ------------------------------------------------ 2) 覆盖与预筛一致

def test_coverage_pass(pf, tmp_path):
    repo = build_repo(tmp_path)
    led = build_ledger(tmp_path / "ledger.sqlite", repo)
    data = build_data_root(tmp_path)          # 与台账同跨度
    out = pf.check_coverage(led, data)
    assert out["status"] == "pass"
    entry = out["candidates"][0]
    assert entry["candidate_id"] == "C005"
    key = entry["keys"]["USTEC/D1"]
    # merged_coverage_years 按日历跨度（365.25 天/年）复算，非段内 years 字段
    assert key["ledger_years"] == pytest.approx(key["measured_years"],
                                                abs=1e-9)
    assert 4.9 < key["ledger_years"] < 5.1


def test_coverage_shrunk_fails(pf, tmp_path):
    repo = build_repo(tmp_path)
    led = build_ledger(tmp_path / "ledger.sqlite", repo)   # 台账 5 年
    data = build_data_root(tmp_path, "2019-01-01T00:00:00",
                           "2020-01-01T00:00:00")          # 实测 1 年
    out = pf.check_coverage(led, data)
    assert out["status"] == "fail"
    assert out["candidates"][0]["keys"]["USTEC/D1"]["ok"] is False


def test_coverage_data_grown_passes(pf, tmp_path):
    """数据只增不减：实测 >= 台账（90% 容差沿引擎 require_runtime_coverage）。"""
    repo = build_repo(tmp_path)
    led = build_ledger(tmp_path / "ledger.sqlite", repo)
    data = build_data_root(tmp_path, "2015-01-01T00:00:00",
                           "2020-07-01T00:00:00")          # 5.5 年
    out = pf.check_coverage(led, data)
    assert out["status"] == "pass"


def test_coverage_missing_series_fails(pf, tmp_path):
    repo = build_repo(tmp_path)
    led = build_ledger(tmp_path / "ledger.sqlite", repo)
    empty = tmp_path / "empty_root"
    empty.mkdir()
    out = pf.check_coverage(led, empty)
    assert out["status"] == "fail"
    assert "missing" in json.dumps(out)


def test_coverage_no_segments_skipped(pf, tmp_path):
    repo = build_repo(tmp_path)
    led = build_ledger(tmp_path / "ledger.sqlite", repo,
                       coverage={"USTEC/D1": []})           # 空段列表
    out = pf.check_coverage(led, tmp_path / "unused")
    assert out["status"] == "skipped"


# ------------------------------------------------ 3) 台账链校验

def test_ledger_chain_pass(pf, tmp_path):
    repo = build_repo(tmp_path)
    led = build_ledger(tmp_path / "ledger.sqlite", repo)
    before = set(Path(tmp_path).glob("*.sqlite"))
    out = pf.check_ledger_chain(led)
    assert out["status"] == "pass" and out["problems"] == []
    assert out["chain_meta"]["rounds"]["row_count"] == 1
    # 只读纪律：原库字节不变，临时副本用后即删（不在源目录留痕）
    assert set(Path(tmp_path).glob("*.sqlite")) == before
    assert out["note"]


def test_ledger_chain_tamper_fails(pf, tmp_path):
    repo = build_repo(tmp_path)
    led = build_ledger(tmp_path / "ledger.sqlite", repo)
    conn = sqlite3.connect(str(led))                       # 绕链直改
    conn.execute("UPDATE runs SET metrics_json='{\"ok\":false}'")
    conn.commit()
    conn.close()
    out = pf.check_ledger_chain(led)
    assert out["status"] == "fail" and out["problems"]


# ------------------------------------------------ 4) 冻结配置哈希

def test_frozen_config_pass_with_expected(pf, tmp_path):
    repo = build_repo(tmp_path)
    out = pf.check_frozen_config(repo, expected=_sha_lf(GATES_TEXT.encode()))
    assert out["status"] == "pass"
    assert out["sha256_lf"] == _sha_lf(GATES_TEXT.encode())


def test_frozen_config_reference_from_ledger_run(pf, tmp_path):
    repo = build_repo(tmp_path)
    led = build_ledger(tmp_path / "ledger.sqlite", repo)   # runs.gates_hash=钉
    out = pf.check_frozen_config(repo, ledger_path=led)
    assert out["status"] == "pass"
    assert out["expected_source"] == "latest runs.gates_hash"


def test_frozen_config_change_fails(pf, tmp_path):
    repo = build_repo(tmp_path)
    led = build_ledger(tmp_path / "ledger.sqlite", repo)
    (repo / "config" / "gates.toml").write_text(
        'version = 1\n[g1]\nmin_trades = 999\n', encoding="utf-8")
    out = pf.check_frozen_config(repo, ledger_path=led)
    assert out["status"] == "fail"


def test_frozen_config_crlf_keeps_lf_basis(pf, tmp_path):
    """CRLF 检出不改变 LF 归一哈希（gates_sha256 同口径）。"""
    repo = build_repo(tmp_path)
    led = build_ledger(tmp_path / "ledger.sqlite", repo)
    (repo / "config" / "gates.toml").write_bytes(
        GATES_TEXT.encode().replace(b"\n", b"\r\n"))
    out = pf.check_frozen_config(repo, ledger_path=led)
    assert out["status"] == "pass"


def test_frozen_config_no_reference_warns(pf, tmp_path):
    repo = build_repo(tmp_path)
    out = pf.check_frozen_config(repo)                     # 无台账无参数
    assert out["status"] == "warn"


# ------------------------------------------------ 5) 磁盘余量

def _fake_disk(pf, monkeypatch, free_bytes):
    import collections

    usage = collections.namedtuple("usage", "total used free")

    def fake(path):
        return usage(total=100 << 30, used=0, free=free_bytes)
    monkeypatch.setattr(pf.shutil, "disk_usage", fake)


def test_disk_pass(pf, tmp_path, monkeypatch):
    _fake_disk(pf, monkeypatch, 68 << 30)
    out = pf.check_disk(tmp_path, warn_gb=30, reject_gb=15)
    assert out["status"] == "pass" and out["free_gb"] == 68.0


def test_disk_warn(pf, tmp_path, monkeypatch):
    _fake_disk(pf, monkeypatch, 25 << 30)
    out = pf.check_disk(tmp_path, warn_gb=30, reject_gb=15)
    assert out["status"] == "warn"


def test_disk_reject(pf, tmp_path, monkeypatch):
    _fake_disk(pf, monkeypatch, 12 << 30)
    out = pf.check_disk(tmp_path, warn_gb=30, reject_gb=15)
    assert out["status"] == "fail"


# ------------------------------------------------ 6) 工作树干净

def test_worktree_clean_pass(pf, tmp_path):
    repo = build_repo(tmp_path)
    out = pf.check_worktree_clean(repo)
    assert out["status"] == "pass" and out["porcelain"] == []


def test_worktree_untracked_warns(pf, tmp_path):
    repo = build_repo(tmp_path)
    (repo / "evidence.txt").write_text("x", encoding="utf-8")
    out = pf.check_worktree_clean(repo)
    assert out["status"] == "warn" and out["tracked_changes"] == []


def test_worktree_tracked_change_fails(pf, tmp_path):
    repo = build_repo(tmp_path)
    (repo / "config" / "gates.toml").write_text("version = 2\n",
                                                encoding="utf-8")
    out = pf.check_worktree_clean(repo)
    assert out["status"] == "fail"
    assert any("gates.toml" in e for e in out["tracked_changes"])


# ------------------------------------------------ 端到端 CLI

def test_cli_end_to_end_pass(pf, tmp_path, capsys, monkeypatch):
    repo = build_repo(tmp_path)
    led = build_ledger(tmp_path / "ledger.sqlite", repo)
    data = build_data_root(tmp_path)
    _fake_disk(pf, monkeypatch, 68 << 30)
    rc = pf.main([
        "--repo", str(repo), "--ledger", str(led),
        "--data-root", str(data),
        "--expected-gates-hash", _sha_lf(GATES_TEXT.encode()),
        "--out", str(tmp_path / "summary.json")])
    assert rc == 0
    summary = json.loads((tmp_path / "summary.json").read_text("utf-8"))
    assert summary["summary"]["overall"] == "pass"
    assert set(summary["checks"]) == {
        "pinned_files", "coverage_vs_precheck", "ledger_chain",
        "frozen_config", "disk_free", "worktree_clean"}
    assert json.loads(capsys.readouterr().out)["summary"] == summary["summary"]


def test_cli_failure_exit_code(pf, tmp_path, monkeypatch):
    repo = build_repo(tmp_path)
    led = build_ledger(tmp_path / "ledger.sqlite", repo)
    (repo / "research" / "rounds" / "R3.toml").write_text(
        "id = 'R3' tampered\n", encoding="utf-8")
    _fake_disk(pf, monkeypatch, 12 << 30)
    rc = pf.main(["--repo", str(repo), "--ledger", str(led),
                  "--data-root", str(tmp_path), "--disk-path", str(tmp_path)])
    assert rc == 1
