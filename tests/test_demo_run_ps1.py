"""scripts/demo_run.ps1 模式边界/生命周期/临界区/运行窗测试。

覆盖（T49/R8-I4/R8-I6 原有 + R10 的 T51/T52/T55/C-1，修 R9-I1/I2/I5）：

- 模式边界（R8-I4）：裸跑/未知 -Mode 拒绝；Demo 配置手数校验
  （risk_fraction / 非 0.01 手 / 两来源皆缺 → 拒绝）；
- T51（R9-I1）：①复刻探针——here-string 经 PS5.1 -File 作为**原生参数**
  传给 ast.parse 会 SyntaxError（证明 -c 老路径反例，防"简化"回退）；
  ②新路径（写临时 .py 文件）源码逐字符等原文；③-SkipGatewayCheck
  不带 -PrecheckOnly 单独使用必须拒绝（防绕过 DEMO 保险丝后门）；
  ④不连终端的 Demo 预检成功路径（-PrecheckOnly + -SkipGatewayCheck，
  到达"等待网关"边界且保险丝源码语法/字节检查通过）；
- T52（R9-I2）：走**真实 ps1→python 链路**（scripts/config_check.py 调
  LiveConfig 真解析器）——顶层 0.02+多行字符串诱饵行拒绝、表作用域
  volume_lots 不影响顶层、TOML 语法错拒绝、合法 0.01 通过；另直测
  config_check.py 单行 JSON ASCII 输出与 error_code；
- T55（R9-I5）：实例登记 <DataDir>/live/instances.json 跨 LogDir 检测；
  同 magic 不同 Mode → mode conflict 拒绝；伪造 PID/命令行特征不匹配
  不视为存活；instances.lock 被独占时第二个调用立即失败；-Stop 只停
  登记表当前核验存活的项；
- C-1 run-manifest：<DataDir>/live/run-manifest.json 读失败/JSON 损坏
  fail-closed 拒绝；启动/恢复重启在锁内追加 runs（kind=start|restart）。

不在测试里跑的路径（会连接终端或启动真实进程）：Demo 模式不带
-SkipGatewayCheck 的网关预检（真终端只读 dry 检查）、任何真实运行器/
风控/对账的启动。**唯一例外**：C-1 追加测试用 where.exe 顶替
-PythonExe 走完整启动分支——where.exe 对任意参数立即退出，不连任何
终端、不产生任何交易路径副作用，但 Start-Process/登记/manifest 追加
全是真实代码路径。
"""
from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "demo_run.ps1"
CONFIG_CHECK = REPO_ROOT / "scripts" / "config_check.py"

MAGIC = 902601

# 完整可装载的 LiveConfig 基底（T52 起判定走真解析器，反例必须能被
# LiveConfig 成功装载、只输在手数口径上——否则拒绝原因不对号）。
_STRATEGY = "mt5quant.backtest.reference:MaCrossATR"

CONFIG_VOLUME_001 = f'''# Demo 模式测试配置
symbol = "XAUUSD"
timeframe = "M15"
magic = {MAGIC}
volume_lots = 0.01
'''

CONFIG_VOLUME_001_FULL = f'''# Demo 模式测试配置（完整可装载）
symbol = "XAUUSD"
timeframe = "M15"
strategy = "{_STRATEGY}"
params = {{ fast = 5, slow = 15 }}
lookback_bars = 30
magic = {MAGIC}
volume_lots = 0.01
'''

CONFIG_RISK_FRACTION = f'''# Demo 模式测试配置（按风险定手数——必须拒绝）
symbol = "XAUUSD"
timeframe = "M15"
strategy = "{_STRATEGY}"
lookback_bars = 30
magic = {MAGIC}
risk_fraction = 0.005
'''

CONFIG_WRONG_LOTS = f'''# Demo 模式测试配置（非 0.01 手——必须拒绝）
symbol = "XAUUSD"
timeframe = "M15"
strategy = "{_STRATEGY}"
lookback_bars = 30
magic = {MAGIC}
volume_lots = 0.02
'''

# R9-I2 审核反例：顶层 0.02 + 合法多行字符串里藏一行 volume_lots = 0.01
# ——旧 Select-String 文本匹配曾放行；LiveConfig 真实装载值是 0.02。
CONFIG_DECOY_MULTILINE = f'''symbol = "XAUUSD"
timeframe = "M15"
strategy = "{_STRATEGY}"
lookback_bars = 30
magic = {MAGIC}
volume_lots = 0.02
created_utc = """
volume_lots = 0.01
"""
'''

# 表作用域反例：[params] 表里的 volume_lots = 0.01 不是顶层值——
# 顶层实际装载 0.02，必须拒绝。
CONFIG_TABLE_SCOPED = f'''symbol = "XAUUSD"
timeframe = "M15"
strategy = "{_STRATEGY}"
lookback_bars = 30
magic = {MAGIC}
volume_lots = 0.02

[params]
volume_lots = 0.01
'''

# TOML 语法错（未闭合数组）：行级文本匹配看不出来，真解析器必须拒绝。
CONFIG_BROKEN_TOML = f'''symbol = "XAUUSD"
timeframe = "M15"
strategy = "{_STRATEGY}"
lookback_bars = 30
magic = {MAGIC}
volume_lots = 0.01
broken = [1, 2
'''


def run_ps1(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    """powershell 跑脚本（拒绝路径/PrecheckOnly，均无副作用）。

    经 -Command 包一层设置 [Console]::OutputEncoding=UTF-8：PS 5.1 管道
    输出缺省取系统 OEM 代码页，会把中文输出转码。**try/catch 包裹**——
    CI 运行器是无控制台会话，[Console] 句柄无效时赋值会抛异常导致整个
    -Command 失败（stdout 全空）；此时回退默认编码，断言用的都是 ASCII
    标记（PaperSoak/Demo/-Mode/runner/<magic>/risk_fraction/mode conflict/
    instances.lock/run-manifest 等），任何代码页下都可读。参数名（-Xxx
    形式）不加引号（-Command 上下文里带引号的 '-Mode' 会被当作位置实参）。
    """
    def _q(s: str) -> str:
        if s.startswith("-") and all(c.isalnum() or c == "-" for c in s):
            return s
        return "'" + s.replace("'", "''") + "'"
    inner = ("try { [Console]::OutputEncoding=[Text.Encoding]::UTF8 } "
             "catch { }; & " + _q(str(SCRIPT)) + " "
             + " ".join(_q(a) for a in args)
             + " 6>&1 2>&1")
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-Command", inner]
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def out_text(proc: subprocess.CompletedProcess) -> str:
    return (proc.stdout or b"").decode("utf-8", errors="replace")


def _diag(proc: subprocess.CompletedProcess) -> str:
    """断言失败时的诊断文本。GitHub 注解只显示首行——首行带上 rc 与
    预检 [X] 失败项（哪个工件/检查没过），正文附完整 stdout，尾部附
    stderr（CI 无控制台会话的报错只在 stderr）。"""
    out = out_text(proc)
    failed_items = [ln.strip() for ln in out.splitlines()
                    if ln.strip().startswith("[X]")]
    first = f"rc={proc.returncode}"
    if failed_items:
        first += " 预检未过：" + "；".join(failed_items)[:200]
    err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
    tail = ("\n[stderr] " + err[-600:]) if err else ""
    return first + "\n" + out + tail


@pytest.fixture(autouse=True)
def _require_powershell():
    if shutil.which("powershell") is None:
        pytest.skip("本机无 powershell（仅 Windows）")


def _dead_pid() -> int:
    """取一个确认已退出的 PID：起一个短命进程等它退出。"""
    p = subprocess.Popen(
        ["powershell", "-NoProfile", "-Command",
         "Start-Sleep -Milliseconds 100; exit 0"])
    pid = p.pid
    deadline = time.time() + 30
    while p.poll() is None and time.time() < deadline:
        time.sleep(0.1)
    p.wait()
    return pid


def _data_dir(tmp_path: Path) -> Path:
    """precheck_artifact_args 使用的 DataDir（live/ 登记在其下）。"""
    return tmp_path / "precheck-data"


def precheck_artifact_args(tmp: Path) -> list[str]:
    """-PrecheckOnly 断言 rc==0 的测试须自备三个数据工件（点差缓存/研究库
    缓存/合约快照）并经参数显式指向——它们只存在于实机 data/（gitignore），
    此前借开发机本地文件通过、干净克隆/CI 上预检 3 项未通过假红。"""
    data = _data_dir(tmp)
    (data / "specs").mkdir(parents=True, exist_ok=True)
    (data / "specs" / "latest.json").write_text("{}", encoding="utf-8")
    spread = tmp / "precheck-spread.json"
    spread.write_text("{}", encoding="utf-8")
    research = tmp / "precheck-research.json"
    research.write_text("{}", encoding="utf-8")
    return ["-DataDir", str(data), "-SpreadModel", str(spread),
            "-ResearchCache", str(research)]


# ------------------------------------------------ 保险丝源码（T51/R9-I1）

def fuse_source() -> str:
    """从 ps1 提取 $demoFusePy here-string 原文（含首尾换行，与探针同法）。"""
    src = SCRIPT.read_text(encoding="utf-8-sig")
    return src.split("$demoFusePy = @'", 1)[1].split("'@", 1)[0]


def _fuse_value_bytes() -> bytes:
    """here-string 的**值**（去掉 @' 后与 '@ 前各一个换行）的 UTF-8 字节
    ——写临时 .py 文件时落盘的正是这些字节。T60：CRLF→LF 归一（幂等保险，
    与 demo_run.ps1 赋值后归一互为镜像——read_text 通用换行本已不含 \r，
    此处钉死口径，防后续改为按字节读取时回退）。"""
    return fuse_source()[1:-1].replace("\r\n", "\n").encode("utf-8")


@contextlib.contextmanager
def fake_live_instance(kind: str):
    """起一个"命令行特征匹配但与演练无关"的活进程，满足 T55 三要素
    存活判定（PID 存活 + 创建时间≈started_utc + 命令行含特征脚本名）：
    `python -c "import time; time.sleep(600)" live_runner.py` 的命令行
    含 live_runner.py，但只是睡觉——不连终端、不写任何状态。"""
    feature = {"runner": "live_runner.py", "risk": "risk_process.py",
               "reconcile": "live_reconcile.py"}[kind]
    p = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)", feature],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        yield p.pid, started
    finally:
        with contextlib.suppress(OSError):
            p.kill()
        with contextlib.suppress(Exception):
            p.wait(timeout=15)


def registry_entry(pid: int, kind: str, mode: str, magic=None,
                   started: str | None = None,
                   log: str = "C:\\synthetic.log") -> dict:
    return {"pid": pid, "kind": kind, "magic": magic, "mode": mode,
            "started_utc": started or datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "log": log,
            "account_hint": "demo" if mode == "Demo" else "paper"}


def write_instances(data_dir: Path, entries: list[dict]) -> None:
    live = data_dir / "live"
    live.mkdir(parents=True, exist_ok=True)
    (live / "instances.json").write_text(
        json.dumps({"schema_version": 1, "instances": entries},
                   ensure_ascii=True),
        encoding="utf-8")


def read_instances(data_dir: Path) -> dict:
    return json.loads(
        (data_dir / "live" / "instances.json").read_text(encoding="utf-8"))


def read_run_manifest(data_dir: Path) -> dict:
    return json.loads(
        (data_dir / "live" / "run-manifest.json").read_text(
            encoding="utf-8-sig"))


# ---------------------------------------------------------------- 模式边界

def test_bare_invocation_refused_with_mode_listing():
    """裸跑（无 -Mode）拒绝：退出码 1 + 两种模式说明（R8-I4）。"""
    proc = run_ps1()
    assert proc.returncode == 1
    out = out_text(proc)
    assert "PaperSoak" in out and "Demo" in out
    assert "-Mode" in out


def test_invalid_mode_refused():
    proc = run_ps1("-Mode", "Bogus")
    assert proc.returncode == 1
    out = out_text(proc)
    assert "PaperSoak" in out and "Demo" in out


def test_demo_mode_rejects_risk_fraction_config(tmp_path):
    """Demo 模式：risk_fraction（按风险定手数）→ 拒绝；且失败发生在网关
    预检之前（输出无 trade_mode 账户核对——绝不连接终端）。"""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "strategy.toml").write_text(CONFIG_RISK_FRACTION, encoding="utf-8")
    proc = run_ps1("-Mode", "Demo", "-ConfigDir", str(cfg_dir),
                   "-PythonExe", sys.executable, "-PrecheckOnly")
    assert proc.returncode == 1
    out = out_text(proc)
    assert "risk_fraction" in out
    assert "trade_mode" not in out          # 网关预检未运行（配置校验先行）


def test_demo_mode_rejects_non_001_volume(tmp_path):
    """Demo 模式：volume_lots ≠ 0.01 → 拒绝（阶段 F 手数上限）。"""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "strategy.toml").write_text(CONFIG_WRONG_LOTS, encoding="utf-8")
    proc = run_ps1("-Mode", "Demo", "-ConfigDir", str(cfg_dir),
                   "-PythonExe", sys.executable, "-PrecheckOnly")
    assert proc.returncode == 1
    out = out_text(proc)
    assert "volume_lots" in out


def test_demo_mode_rejects_missing_volume(tmp_path):
    """Demo 模式：两个手数来源都没写 → 拒绝（固定 0.01 缺失）。"""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "strategy.toml").write_text(
        f'symbol = "XAUUSD"\ntimeframe = "M15"\nstrategy = "{_STRATEGY}"\n'
        f"lookback_bars = 30\nmagic = {MAGIC}\n",
        encoding="utf-8")
    proc = run_ps1("-Mode", "Demo", "-ConfigDir", str(cfg_dir),
                   "-PythonExe", sys.executable, "-PrecheckOnly")
    assert proc.returncode == 1


# ------------------------------------------------ T51（R9-I1）：保险丝源码

def test_t51_native_argument_path_corrupts_fuse_source(tmp_path):
    """①老路径反例（复刻 R9 审核探针）：here-string 经 PS5.1 -File 作为
    原生参数传给 ast.parse → SyntaxError。原文本身合法（ast.parse 通过）
    ——损坏发生在 PS5.1 原生传参剥引号。这是"必须走临时文件"的根据，
    防止后人把调用方式"简化"回内联参数。"""
    body = fuse_source()
    ast.parse(body)                          # 原文可解析（损坏另有来源）
    parse_only = tmp_path / "parse_only.py"
    parse_only.write_text(
        "import ast,sys,json\ns=sys.argv[1]\ntry:\n"
        " ast.parse(s); print(json.dumps({'parsed':True}))\n"
        "except SyntaxError as e:\n"
        " print(json.dumps({'parsed':False,'line':e.lineno,"
        "'text':str(e.text),'msg':e.msg}));sys.exit(1)\n",
        encoding="utf-8")
    probe = tmp_path / "probe_argv.ps1"
    probe.write_text(
        "$demoFusePy = @'" + body + "'@\n"
        "& '" + sys.executable + "' '" + str(parse_only) + "' $demoFusePy\n"
        "exit $LASTEXITCODE\n",
        encoding="utf-8-sig")
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(probe)],
        capture_output=True, timeout=120)
    assert proc.returncode != 0, out_text(proc)   # 收到的文本不可解析
    detail = json.loads(out_text(proc).strip().splitlines()[-1])
    assert detail["parsed"] is False


def test_t51_temp_file_path_char_identical(tmp_path):
    """②新路径：here-string 值写临时 .py（UTF-8 无 BOM，与 ps1 同机制）
    → Python 读回逐字符（逐字节）等原文且可解析；并断言 ps1 源码里
    不再有内联源码参数调用（-c 后接 $ 变量）。"""
    body = fuse_source()
    ast.parse(body)
    expected = hashlib.sha256(_fuse_value_bytes()).hexdigest()
    check = tmp_path / "check_file.py"
    check.write_text(
        "import ast,hashlib,json,sys\nd=open(sys.argv[1],'rb').read()\n"
        "r={'parsed':True,'sha256':hashlib.sha256(d).hexdigest()}\n"
        "try:\n ast.parse(d.decode('utf-8'))\n"
        "except SyntaxError as e:\n r['parsed']=False; r['line']=e.lineno\n"
        "print(json.dumps(r))\n",
        encoding="utf-8")
    probe = tmp_path / "probe_file.ps1"
    probe.write_text(
        "$demoFusePy = @'" + body + "'@\n"
        "$f=Join-Path $env:TEMP ('fuse-'+[guid]::NewGuid().ToString('N')+'.py')\n"
        "[IO.File]::WriteAllText($f,$demoFusePy,"
        "(New-Object System.Text.UTF8Encoding($false)))\n"
        "& '" + sys.executable + "' '" + str(check) + "' $f\n"
        "$rc=$LASTEXITCODE\n"
        "Remove-Item $f -ErrorAction SilentlyContinue\n"
        "exit $rc\n",
        encoding="utf-8-sig", newline="\n")   # LF：与 demo_run.ps1 一致
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(probe)],
        capture_output=True, timeout=120)
    assert proc.returncode == 0, out_text(proc)
    result = json.loads(out_text(proc).strip().splitlines()[-1])
    assert result["parsed"] is True
    assert result["sha256"] == expected       # 逐字符（逐字节）等原文
    src = SCRIPT.read_text(encoding="utf-8-sig")
    assert re.search(r"-c\s+\$", src) is None  # ps1 不再内联传源码


def test_t51_skip_gateway_check_alone_refused(tmp_path):
    """③-SkipGatewayCheck 不带 -PrecheckOnly 单独使用必须拒绝启动
    （防绕过 DEMO 保险丝的后门——跳过网关预检的实际启动不允许）。"""
    proc = run_ps1("-Mode", "Demo", "-SkipGatewayCheck")
    assert proc.returncode == 1
    assert "-SkipGatewayCheck requires -PrecheckOnly" in out_text(proc)


def test_t51_demo_precheck_success_without_terminal(tmp_path):
    """④不连终端的 Demo 预检成功路径：-PrecheckOnly + -SkipGatewayCheck
    → 到达"等待网关"边界（gateway skipped）且保险丝源码经真 ps1→python
    链路做语法/字节完整性检查（sha256 与原文一致）。"""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "strategy.toml").write_text(CONFIG_VOLUME_001_FULL,
                                           encoding="utf-8")
    log_dir = tmp_path / "demo"
    log_dir.mkdir()
    proc = run_ps1("-Mode", "Demo", "-ConfigDir", str(cfg_dir),
                   "-LogDir", str(log_dir), "-PythonExe", sys.executable,
                   *precheck_artifact_args(tmp_path),
                   "-SkipGatewayCheck", "-PrecheckOnly")
    assert proc.returncode == 0, _diag(proc)
    out = out_text(proc)
    assert "gateway skipped" in out            # 等待网关边界（未跨越）
    m = re.search(r"sha256=([0-9a-f]{64})", out)
    assert m, _diag(proc)
    assert m.group(1) == hashlib.sha256(_fuse_value_bytes()).hexdigest()
    assert "trade_mode" not in out             # 未执行真网关检查


# ------------------------------------------------ T52（R9-I2）：真解析器

def _run_config_check(cfg: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CONFIG_CHECK), str(cfg)],
        capture_output=True, timeout=60)


def test_t52_config_check_valid_config_ascii_json(tmp_path):
    """config_check.py 直测：合法配置 → 单行 JSON、ASCII-only（Windows
    管道代码页下无 mojibake）、字段齐全。"""
    cfg = tmp_path / "ok.toml"
    cfg.write_text(CONFIG_VOLUME_001_FULL, encoding="utf-8")
    proc = _run_config_check(cfg)
    assert proc.returncode == 0, proc.stderr
    out = (proc.stdout or b"").decode("utf-8")
    out.encode("ascii")                        # ASCII-only 契约
    data = json.loads(out.strip().splitlines()[-1])
    assert data == {"ok": True, "volume_lots": 0.01,
                    "has_risk_fraction": False, "error_code": None}


def test_t52_config_check_error_codes(tmp_path):
    """config_check.py 直测：缺文件 not_found / TOML 语法错 toml_syntax /
    缺必填项 invalid_config——退出码 2 且输出 ASCII error_code。"""
    gone = tmp_path / "gone.toml"
    proc = _run_config_check(gone)
    assert proc.returncode == 2
    assert json.loads(proc.stdout.decode("utf-8"))["error_code"] == "not_found"

    broken = tmp_path / "broken.toml"
    broken.write_text(CONFIG_BROKEN_TOML, encoding="utf-8")
    proc = _run_config_check(broken)
    assert proc.returncode == 2
    assert json.loads(proc.stdout.decode("utf-8")
                      .strip().splitlines()[-1])["error_code"] == "toml_syntax"

    incomplete = tmp_path / "incomplete.toml"
    incomplete.write_text(f'symbol = "XAUUSD"\ntimeframe = "M15"\n'
                          f"magic = {MAGIC}\n", encoding="utf-8")
    proc = _run_config_check(incomplete)
    assert proc.returncode == 2
    assert json.loads(proc.stdout.decode("utf-8")
                      .strip().splitlines()[-1])["error_code"] == "invalid_config"


def test_t52_decoy_multiline_rejected(tmp_path):
    """①审核反例：顶层 0.02 + created_utc 多行字符串里一行
    volume_lots = 0.01（LiveConfig 装载成功、值为 0.02）→ ps1 必须拒绝。"""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "strategy.toml").write_text(CONFIG_DECOY_MULTILINE,
                                           encoding="utf-8")
    proc = run_ps1("-Mode", "Demo", "-ConfigDir", str(cfg_dir),
                   "-PythonExe", sys.executable, "-PrecheckOnly")
    assert proc.returncode == 1
    out = out_text(proc)
    assert "volume_lots" in out
    assert "0.02" in out                  # 实际加载值必须出现在拒绝信息里
    assert "trade_mode" not in out        # 拒绝发生在网关预检之前（不连终端）


def test_t52_valid_001_passes_real_parser(tmp_path):
    """②合法 0.01 配置经真实 ps1→python 链路通过（-SkipGatewayCheck
    预检成功、退出码 0）。"""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "strategy.toml").write_text(CONFIG_VOLUME_001_FULL,
                                           encoding="utf-8")
    log_dir = tmp_path / "demo"
    log_dir.mkdir()
    proc = run_ps1("-Mode", "Demo", "-ConfigDir", str(cfg_dir),
                   "-LogDir", str(log_dir), "-PythonExe", sys.executable,
                   *precheck_artifact_args(tmp_path),
                   "-SkipGatewayCheck", "-PrecheckOnly")
    assert proc.returncode == 0, _diag(proc)
    out = out_text(proc)
    assert "volume_lots" in out and "[OK]" in out


def test_t52_toml_syntax_error_rejected(tmp_path):
    """③TOML 语法错（行级文本看不出）→ 真解析器拒绝启动。"""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "strategy.toml").write_text(CONFIG_BROKEN_TOML,
                                           encoding="utf-8")
    proc = run_ps1("-Mode", "Demo", "-ConfigDir", str(cfg_dir),
                   "-PythonExe", sys.executable, "-PrecheckOnly")
    assert proc.returncode == 1
    assert "error_code=toml_syntax" in out_text(proc)


def test_t52_table_scoped_volume_not_toplevel(tmp_path):
    """④表作用域反例：[params] 表里的 volume_lots = 0.01 不是顶层值
    （顶层实际装载 0.02）→ 拒绝。"""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "strategy.toml").write_text(CONFIG_TABLE_SCOPED,
                                           encoding="utf-8")
    proc = run_ps1("-Mode", "Demo", "-ConfigDir", str(cfg_dir),
                   "-PythonExe", sys.executable, "-PrecheckOnly")
    assert proc.returncode == 1
    out = out_text(proc)
    assert "volume_lots" in out and "0.02" in out


# ------------------------------------------------ T55（R9-I5）：登记/锁

def test_t55_cross_logdir_mode_conflict_refused(tmp_path):
    """换 LogDir 也不能漏检：instances.json 登记存活运行器（magic 相同、
    Mode=Demo），以 PaperSoak 从另一 LogDir 启动 → mode conflict 拒绝。"""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "strategy.toml").write_text(CONFIG_VOLUME_001, encoding="utf-8")
    args = precheck_artifact_args(tmp_path)
    with fake_live_instance("runner") as (pid, started):
        write_instances(_data_dir(tmp_path),
                        [registry_entry(pid, "runner", "Demo", MAGIC, started)])
        proc = run_ps1("-Mode", "PaperSoak", "-ConfigDir", str(cfg_dir),
                       "-LogDir", str(tmp_path / "logs-b"), *args,
                       "-PrecheckOnly")
        assert proc.returncode == 1
        out = out_text(proc)
        assert "mode conflict" in out and str(MAGIC) in out


def test_t55_cross_logdir_same_mode_runner_detected(tmp_path):
    """同模式跨 LogDir：登记存活运行器（PaperSoak）→ 换 LogDir 预检仍
    检出并列出跳过清单（runner/<magic>——绝不并行第二份）。"""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "strategy.toml").write_text(CONFIG_VOLUME_001, encoding="utf-8")
    args = precheck_artifact_args(tmp_path)
    with fake_live_instance("runner") as (pid, started):
        write_instances(_data_dir(tmp_path),
                        [registry_entry(pid, "runner", "PaperSoak", MAGIC,
                                        started)])
        proc = run_ps1("-Mode", "PaperSoak", "-ConfigDir", str(cfg_dir),
                       "-LogDir", str(tmp_path / "logs-b"), *args,
                       "-PrecheckOnly")
        assert proc.returncode == 0, _diag(proc)
        assert f"runner/{MAGIC}" in out_text(proc)


def test_t55_fake_pid_and_feature_mismatch_not_alive(tmp_path):
    """三要素存活判定：伪造 PID（已退出）与特征不匹配的活进程（命令行
    无 live_runner.py）都不视为存活——不进跳过清单、不挡启动。"""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "strategy.toml").write_text(CONFIG_VOLUME_001, encoding="utf-8")
    args = precheck_artifact_args(tmp_path)
    # 特征不匹配的活进程：同样睡 600 秒，但命令行带的是别的脚本名
    with fake_live_instance("runner") as (live_pid, started):
        wrong = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)",
             "not_a_demo_script.py"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            entries = [
                registry_entry(_dead_pid(), "runner", "PaperSoak", MAGIC),
                registry_entry(wrong.pid, "runner", "PaperSoak", MAGIC,
                               started),
                registry_entry(live_pid, "risk", "PaperSoak", None, started),
            ]
            # risk 特征也不匹配：live_pid 的命令行是 live_runner.py 而非
            # risk_process.py → 同样不视为存活
            write_instances(_data_dir(tmp_path), entries)
            proc = run_ps1("-Mode", "PaperSoak", "-ConfigDir", str(cfg_dir),
                           *args, "-PrecheckOnly")
            assert proc.returncode == 0, _diag(proc)
            out = out_text(proc)
            assert f"runner/{MAGIC}" not in out
            assert "reconcile" not in out   # risk 项特征不匹配 → 未检出
        finally:
            with contextlib.suppress(OSError):
                wrong.kill()
            with contextlib.suppress(Exception):
                wrong.wait(timeout=15)


def test_t55_instances_lock_held_fails_immediately(tmp_path):
    """instances.lock 被独占时第二个调用立即失败（绝不并行第二份）。"""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "strategy.toml").write_text(CONFIG_VOLUME_001, encoding="utf-8")
    args = precheck_artifact_args(tmp_path)
    live = _data_dir(tmp_path) / "live"
    live.mkdir(parents=True, exist_ok=True)
    lock = live / "instances.lock"
    with lock.open("a"):                      # 占住锁（共享读写句柄）
        proc = run_ps1("-Mode", "PaperSoak", "-ConfigDir", str(cfg_dir),
                       *args, "-PrecheckOnly")
        assert proc.returncode == 1
        assert "instances.lock" in out_text(proc)


def test_t55_corrupt_instances_registry_fail_closed(tmp_path):
    """instances.json JSON 损坏 → fail-closed 拒绝启动（无法安全检测）。"""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "strategy.toml").write_text(CONFIG_VOLUME_001, encoding="utf-8")
    args = precheck_artifact_args(tmp_path)
    live = _data_dir(tmp_path) / "live"
    live.mkdir(parents=True, exist_ok=True)
    (live / "instances.json").write_text("{ not json !!", encoding="utf-8")
    proc = run_ps1("-Mode", "PaperSoak", "-ConfigDir", str(cfg_dir),
                   *args, "-PrecheckOnly")
    assert proc.returncode == 1
    assert "instances.json" in out_text(proc)


def _pid_exists(pid: int) -> bool:
    """轮询 PID 是否仍存在（不引第三方依赖；ASCII 输出任何代码页可读）。"""
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) "
             "{{ 'alive-yes' }} else {{ 'alive-no' }}"],
            capture_output=True, timeout=30)
        return b"alive-yes" in (p.stdout or b"")
    except Exception:
        return False


def test_t55_stop_stops_only_verified_current_entries(tmp_path):
    """-Stop：只处理 instances.json 当前项——核验存活的停止（核验失败/
    已退出的不动），随后清空登记；不遍历 pids.txt 历史 PID。"""
    args = precheck_artifact_args(tmp_path)
    data = _data_dir(tmp_path)
    with fake_live_instance("runner") as (pid, started):
        write_instances(data, [
            registry_entry(pid, "runner", "PaperSoak", MAGIC, started),
            registry_entry(_dead_pid(), "risk", "PaperSoak", None),
        ])
        proc = run_ps1("-Stop", "-DataDir", str(data))
        assert proc.returncode == 0, _diag(proc)
        out = out_text(proc)
        assert str(pid) in out
        deadline = time.time() + 30           # fake 进程应已被 ps1 停止
        while time.time() < deadline and _pid_exists(pid):
            time.sleep(0.2)
        assert not _pid_exists(pid), "登记项进程应被 -Stop 停止"
        assert read_instances(data)["instances"] == []


# ------------------------------------------------ C-1 run-manifest

def test_c1_corrupt_run_manifest_refuses(tmp_path):
    """run-manifest.json 读失败/JSON 损坏 → fail-closed 拒绝启动。"""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "strategy.toml").write_text(CONFIG_VOLUME_001, encoding="utf-8")
    args = precheck_artifact_args(tmp_path)
    live = _data_dir(tmp_path) / "live"
    live.mkdir(parents=True, exist_ok=True)
    (live / "run-manifest.json").write_text("{{{ broken", encoding="utf-8")
    proc = run_ps1("-Mode", "PaperSoak", "-ConfigDir", str(cfg_dir),
                   *args, "-PrecheckOnly")
    assert proc.returncode == 1
    assert "run-manifest.json" in out_text(proc)


def test_c1_run_manifest_append_start_and_restart(tmp_path):
    """C-1 契约：启动追加 runs（kind=start；字段 schema_version/mode/
    magics/runs/account_hint）；恢复重启再追加（kind=restart、runs 只增
    不覆盖）。用 where.exe 顶替 -PythonExe 走真实启动分支——where.exe
    对任意参数立即退出，不连终端、无任何交易路径副作用。"""
    where = shutil.which("where.exe")
    if not where:
        pytest.skip("无 where.exe")
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "strategy.toml").write_text(CONFIG_VOLUME_001, encoding="utf-8")
    data = _data_dir(tmp_path)
    args = precheck_artifact_args(tmp_path)
    log1 = tmp_path / "logs-1"
    proc = run_ps1("-Mode", "PaperSoak", "-ConfigDir", str(cfg_dir),
                   "-LogDir", str(log1), "-PythonExe", where, *args,
                   timeout=180)
    assert proc.returncode == 0, _diag(proc)

    mf = read_run_manifest(data)
    assert mf["schema_version"] == 1
    assert mf["mode"] == "PaperSoak"
    assert mf["magics"] == [MAGIC]
    assert mf["account_hint"] == "paper"
    assert len(mf["runs"]) == 1
    assert mf["runs"][0]["kind"] == "start"
    assert mf["runs"][0]["mode"] == "PaperSoak"
    assert mf["runs"][0]["log_dir"] == str(log1)
    assert mf["runs"][0]["started_utc"].endswith("Z")

    kinds = {e["kind"] for e in read_instances(data)["instances"]}
    assert {"runner", "risk", "reconcile"} <= kinds

    # 恢复重启：登记一个存活的同模式运行器 → 跳过该运行器、追加 restart
    with fake_live_instance("runner") as (pid, started):
        write_instances(data, [registry_entry(pid, "runner", "PaperSoak",
                                              MAGIC, started)])
        log2 = tmp_path / "logs-2"
        proc2 = run_ps1("-Mode", "PaperSoak", "-ConfigDir", str(cfg_dir),
                        "-LogDir", str(log2), "-PythonExe", where, *args,
                        timeout=180)
        assert proc2.returncode == 0, _diag(proc2)
        mf2 = read_run_manifest(data)
        assert len(mf2["runs"]) == 2            # 追加，绝不覆盖
        assert mf2["runs"][0] == mf["runs"][0]  # 历史条目原样保留
        assert mf2["runs"][1]["kind"] == "restart"
        assert mf2["runs"][1]["log_dir"] == str(log2)
        assert str(pid) in out_text(proc2)      # 存活运行器被跳过


# ------------------------------------------------ 生命周期（R8-I6 → T55）

def test_precheck_only_lists_alive_runner_skip(tmp_path):
    """instances.json 里目标 magic 有存活运行器（三要素通过）→
    -PrecheckOnly 显示跳过清单 runner/<magic>；且不改写登记文件。"""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "strategy.toml").write_text(CONFIG_VOLUME_001, encoding="utf-8")
    args = precheck_artifact_args(tmp_path)
    data = _data_dir(tmp_path)
    with fake_live_instance("runner") as (pid, started):
        write_instances(data, [registry_entry(pid, "runner", "PaperSoak",
                                              MAGIC, started)])
        before = (data / "live" / "instances.json").read_bytes()
        proc = run_ps1("-Mode", "PaperSoak", "-ConfigDir", str(cfg_dir),
                       *args, "-PrecheckOnly")
        assert proc.returncode == 0, _diag(proc)
        assert f"runner/{MAGIC}" in out_text(proc)   # 将被跳过（绝不并行第二份）
        assert (data / "live" / "instances.json").read_bytes() == before


def test_legacy_pids_txt_not_used_for_detection(tmp_path):
    """pids.txt 只是追加式历史日志（T55）：里面的存活 PID 不再参与
    检测——检测唯一来源是 <DataDir>/live/instances.json。"""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "strategy.toml").write_text(CONFIG_VOLUME_001, encoding="utf-8")
    log_dir = tmp_path / "demo"
    log_dir.mkdir()
    (log_dir / "pids.txt").write_text(
        f"{os.getpid()}\trunner\t{MAGIC}\tC:\\\\old.log\tPaperSoak\t"
        "2026-10-27T00:00:00.0000000Z\n", encoding="utf-8")
    proc = run_ps1("-Mode", "PaperSoak", "-ConfigDir", str(cfg_dir),
                   "-LogDir", str(log_dir), *precheck_artifact_args(tmp_path),
                   "-PrecheckOnly")
    assert proc.returncode == 0, _diag(proc)
    assert f"runner/{MAGIC}" not in out_text(proc)


def test_dead_registry_entry_not_treated_as_alive(tmp_path):
    """登记表里的死 PID 不视为存活：不进跳过清单（允许补启动）。"""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "strategy.toml").write_text(CONFIG_VOLUME_001, encoding="utf-8")
    write_instances(_data_dir(tmp_path),
                    [registry_entry(_dead_pid(), "runner", "PaperSoak", MAGIC)])
    proc = run_ps1("-Mode", "PaperSoak", "-ConfigDir", str(cfg_dir),
                   *precheck_artifact_args(tmp_path), "-PrecheckOnly")
    assert proc.returncode == 0, _diag(proc)
    assert f"runner/{MAGIC}" not in out_text(proc)  # 不挡启动
