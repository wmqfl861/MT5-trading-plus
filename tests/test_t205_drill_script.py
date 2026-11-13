"""scripts/ops/drill_block_terminal.ps1 静态测试（T205，P0，D91）。

只读源码断言，**绝不执行**该脚本（红线：建真实防火墙规则是周六演练
主会话的事；测试执行它会在本机留下规则/计划任务副作用）。断言逐条对
应 round19 计划 §2.5 的做法要求：

- finally 删除：等待结束后在 finally 块里 Remove-NetFirewallRule 同名
  规则，并注销兜底任务（规格③④）；
- 兜底任务：开始等待**之前**注册 10 分钟后一次性触发的计划任务，动作
  = Remove-NetFirewallRule 同名规则（规格②"先建兜底"）；
- 120 秒上限：``$Seconds -gt 120`` 拒绝（且是唯一的 -gt 上限，配
  throw，不截断执行）（规格参数项）；
- 规则名固定：常量 MT5Quant-drill-block-terminal 只定义一次，建/删/
  兜底动作全用它；
- 只按 -Program 拦终端不拦整机：唯一的 New-NetFirewallRule 带
  -Direction Outbound + -Program + -Action Block；源码（去注释后）不
  出现网卡/IP/路由/DNS/防火墙Profile级操作，不按地址/端口/协议/接口
  限定，没有 Inbound，没有本机用户目录硬编码路径。

审核修复（docs/reviews/2026-11-13-t205-review.md，conditional → 追加
断言）：

- M2：兜底 Principal 必须是 NT AUTHORITY\\SYSTEM + ServiceAccount
  （无头触发，不依赖操作员交互登录）；
- M1：FINALLY-ERROR 陈述按 ``$taskCreated`` 分支（兜底未建成时明说
  "无任何自动回收"，不得谎称自动删除），两分支都在**运行时**输出人工
  清理命令原文（建规则删除 + 兜底任务注销两条）；
- L1：兜底动作命令含自删（Unregister-ScheduledTask 同名任务）；
- L2：日志时刻一律 UTC（G19-1，``[DateTime]::UtcNow``，PS5.1 无
  -AsUTC）。

辅助口径：``_code()`` 去掉 ``<# #>`` 块注释与行内 ``#`` 注释后按纯代码
断言（脚本内字符串字面量保证不含 ``#``）；文件编码断言 UTF-8 BOM +
CRLF（PS5.1 正确读中文注释的仓库约定，与既有 ps1 一致）。
"""
from __future__ import annotations

import re

import pytest

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ops" / "drill_block_terminal.ps1"

RULE_NAME = "MT5Quant-drill-block-terminal"
TASK_NAME = "MT5Quant-drill-block-fallback"


def _source() -> str:
    """脚本原文（剥 BOM）。"""
    assert SCRIPT.is_file(), f"缺失 {SCRIPT}（T205 断线演练脚本）"
    return SCRIPT.read_text(encoding="utf-8-sig")


def _code() -> str:
    """去注释后的纯代码（块注释 <# #> + 行内 #）。

    依赖脚本作者约束：字符串字面量内不写 ``#``（静态测试与脚本同步
    维护，若脚本将来需要字符串内 ``#``，这里需换词法级剥离）。
    """
    code = re.sub(r"<#.*?#>", "", _source(), flags=re.S)
    return "\n".join(re.sub(r"#.*$", "", line) for line in code.splitlines())


# ----------------------------------------------------------- 文件形态

def test_ps1_exists_utf8_bom_crlf():
    """存在；UTF-8 BOM + 全 CRLF（PS5.1 读中文注释，仓库 ps1 约定）。"""
    raw = SCRIPT.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "ps1 必须带 UTF-8 BOM"
    text = raw.decode("utf-8-sig")
    assert text.count("\n") == text.count("\r\n"), "行尾必须统一 CRLF"


# ----------------------------------------------------------- 参数契约

def test_seconds_param_mandatory_int():
    """-Seconds 必填且为 [int]（缺省不允许——演练时长不允许猜测）。"""
    code = _code()
    assert re.search(
        r"\[Parameter\(Mandatory\s*=\s*\$true\)\]\s*\r?\n\s*\[int\]\$Seconds\b",
        code), "-Seconds 必须是 Mandatory [int]"


def test_seconds_hard_cap_120_rejects():
    """120 秒上限：超过必须 throw 拒绝；上限比较只此一处，另有下界 1。"""
    code = _code()
    caps = re.findall(r"\$Seconds\s+-gt\s+(\d+)", code)
    assert caps == ["120"], f"-Seconds 的 -gt 上限必须且只能是 120，实际 {caps}"
    lows = re.findall(r"\$Seconds\s+-lt\s+(\d+)", code)
    assert lows == ["1"], f"-Seconds 的 -lt 下界必须是 1，实际 {lows}"
    lines = code.splitlines()
    for idx, line in enumerate(lines):
        if re.search(r"\$Seconds\s+-gt\s+120", line):
            window = "\n".join(lines[idx:idx + 4])
            assert "throw" in window, "超过 120 必须紧随 throw 拒绝（不截断执行）"
            break
    else:
        pytest.fail("缺少 -Seconds -gt 120 校验")


def test_terminal_path_default_empty_probe_or_mandatory():
    """-TerminalPath 缺省探测常见路径；探不到/歧义 → 必填报错（保守实现）。"""
    code = _code()
    assert re.search(r"\[string\]\$TerminalPath\s*=\s*\"\"", code), \
        "-TerminalPath 缺省必须是空串"
    # 探测目标与常见安装根（环境变量，不硬编码本机路径）
    assert "terminal64.exe" in code
    assert "$env:ProgramFiles" in code
    assert "${env:ProgramFiles(x86)}" in code
    assert "$env:LOCALAPPDATA" in code
    # 保守实现：探测失败/多候选都必须 throw 且指明用 -TerminalPath 显式指定
    throws = re.findall(r'throw\s+"[^\r\n]*', code)
    assert any("-TerminalPath" in t for t in throws), \
        "探测不到（或有歧义）必须报错要求 -TerminalPath 显式指定"
    # 目标文件名必须是 terminal64.exe（只允许拦交易终端程序）
    assert '-ine "terminal64.exe"' in code


# ----------------------------------------------------------- 固定规则名

def test_fixed_rule_and_task_names_defined_once():
    """规则/兜底任务名固定为常量，字面量在代码里只出现一次（无第二写法）。"""
    code = _code()
    assert re.search(r'\$RuleName\s*=\s+"' + re.escape(RULE_NAME) + '"', code)
    assert re.search(
        r'\$FallbackTaskName\s*=\s+"' + re.escape(TASK_NAME) + '"', code)
    assert code.count(RULE_NAME) == 1, "规则名字面量只允许在常量定义处出现"
    assert code.count(TASK_NAME) == 1, "任务名字面量只允许在常量定义处出现"


# --------------------------------------------- 只按 -Program 拦终端

def test_single_program_scoped_outbound_block_rule():
    """唯一一条 New-NetFirewallRule：Outbound + -Program + Block + 固定名。"""
    code = _code()
    assert code.count("New-NetFirewallRule") == 1, \
        "只允许一次建规则调用"
    stmt = re.search(r"New-NetFirewallRule[^\r\n]*", code)
    assert stmt, "New-NetFirewallRule 必须是单行可提取的调用"
    line = stmt.group(0)
    for token in ("-Direction Outbound", "-Program $terminal",
                  "-Action Block", "-Name $RuleName", "-DisplayName $RuleName"):
        assert token in line, f"建规则调用缺少 {token}"
    # 方向只有 Outbound 一种；-Action Block 只此一处
    assert re.findall(r"-Direction\s+(\S+)", code) == ["Outbound"]
    assert code.count("-Action Block") == 1
    assert "Inbound" not in code


def test_no_machine_wide_or_adapter_level_blocking():
    """源码不出现整机级拦法/网卡-IP-路由级操作/地址端口协议接口限定。"""
    code = _code()
    forbidden = [
        # 动网卡
        "Disable-NetAdapter", "Enable-NetAdapter", "Restart-NetAdapter",
        "Rename-NetAdapter", "Set-NetAdapterAdvancedProperty",
        # IP/路由/DNS 级
        "New-NetIPAddress", "Remove-NetIPAddress", "Set-NetIPAddress",
        "Set-DnsClientServerAddress", "New-NetRoute", "Remove-NetRoute",
        # 防火墙 Profile 级 / 老式 netsh 全局写法
        "Set-NetFirewallProfile", "netsh",
        # 除 -Program 之外的规则限定维度（出现即偏离"只按程序拦"）
        "-RemoteAddress", "-LocalAddress", "-RemotePort", "-LocalPort",
        "-Protocol", "-InterfaceType", "-InterfaceAlias", "-Service",
    ]
    for token in forbidden:
        assert token not in code.lower(), f"不允许出现 {token}"
    # 不硬编码本机用户目录路径（hygiene 同口径，防将来回归）
    assert not re.search(r"C:[/\\]Users", code)


# ----------------------------------------------------------- 兜底任务

def test_fallback_task_registered_before_wait_ten_minutes():
    """规格②：等待前先注册 10 分钟一次性兜底任务，动作=删同名规则。"""
    code = _code()
    # 顺序：建规则 → 注册兜底任务 → 等待 → finally（先兜底，后等待）
    i_rule = code.index("New-NetFirewallRule")
    i_task = code.index("Register-ScheduledTask")
    i_wait = code.index("Start-Sleep")
    i_fin = code.index("finally")
    assert i_rule < i_task < i_wait < i_fin, \
        "顺序必须是：建规则 → 注册兜底 → 等待 → finally 清理"
    # 一次性触发，10 分钟后
    assert re.search(
        r"New-ScheduledTaskTrigger\s+-Once\s+-At\s*\(Get-Date\)\.AddMinutes\(10\)",
        code), "兜底任务必须是 -Once + (Get-Date).AddMinutes(10)"
    # 动作：powershell 删同名规则并自删任务（L1：不留空任务残留）
    action_line = [ln for ln in code.splitlines()
                   if "New-ScheduledTaskAction" in ln]
    assert len(action_line) == 1
    for token in ("powershell.exe", "-NoProfile", "Remove-NetFirewallRule",
                  "$RuleName", "Unregister-ScheduledTask", "$FallbackTaskName",
                  "-Confirm:`$false"):
        assert token in action_line[0], f"兜底动作缺少 {token}"
    # 注册与注销都用固定任务名
    assert "Register-ScheduledTask -TaskName $FallbackTaskName" in code
    assert "Unregister-ScheduledTask -TaskName $FallbackTaskName" in code


# --------------------------------------------------- 审核修复（M1/M2/L1/L2）

def test_fallback_principal_system_headless():
    """M2：兜底任务以 SYSTEM/ServiceAccount 无头运行，不依赖交互登录。

    Interactive Principal 在操作员注销后触发被跳过、-StartWhenAvailable
    顺延到下次登录——无人值守 soak 期间终端出站会被静默拦到周一，禁止。
    """
    code = _code()
    principals = re.findall(r"New-ScheduledTaskPrincipal[^\r\n]*", code)
    assert len(principals) == 1, "只允许一个 Principal（兜底任务）"
    line = principals[0]
    assert '-UserId "NT AUTHORITY\\SYSTEM"' in line
    assert "-LogonType ServiceAccount" in line
    assert "-RunLevel Highest" in line
    assert "Interactive" not in code, "不允许再出现依赖交互登录的 Principal"


def test_finally_error_honest_manual_cleanup():
    """M1：FINALLY-ERROR 陈述按 $taskCreated 分支；两分支运行时输出清理原文。

    双失效洞：兜底注册失败 + finally 删规则失败 → 无任何自动回收。此时
    日志必须明说"无任何自动回收"，不得沿用"兜底 10 分钟内自动删除"的
    虚假承诺；且人工清理命令必须出现在运行时输出（不藏在注释帮助里）。
    """
    code = _code()
    i_fin = code.index("finally")
    tail = code[i_fin:]
    # 消息分支：条件判断在前，两条分支陈述在后，再输出人工清理命令
    i_branch = tail.index("if ($taskCreated) {")
    i_auto = tail.index("兜底任务仍在，将在 10 分钟内自动删除它")
    i_none = tail.index("兜底任务未建成，无任何自动回收")
    i_manual = tail.index("MANUAL-CLEANUP")
    assert i_branch < i_auto < i_none < i_manual, \
        "删规则失败的日志必须先按 $taskCreated 分支陈述，再输出人工清理命令"
    # 人工清理命令原文：按固定名删规则 + 按固定名注销兜底任务（含 -Confirm:$false 原文）
    frag_rule = "\"Remove-NetFirewallRule -Name '\" + $RuleName"
    frag_task = "\"Unregister-ScheduledTask -TaskName '\" + $FallbackTaskName"
    assert frag_rule in tail, "缺少规则人工清理命令原文（运行时输出）"
    assert frag_task in tail, "缺少兜底任务人工清理命令原文（运行时输出）"
    assert "'-Confirm:$false'" in tail, "清理命令必须保留 -Confirm:$false 原文"


# ----------------------------------------------------------- finally 删除

def test_finally_removes_rule_and_task():
    """规格③④：finally 块里删规则 + 删兜底任务；等待恰好一次且有界。"""
    code = _code()
    i_fin = code.index("finally")
    tail = code[i_fin:]
    assert "Remove-NetFirewallRule -Name $RuleName" in tail, \
        "finally 里必须按固定名删规则"
    assert "Unregister-ScheduledTask -TaskName $FallbackTaskName" in tail, \
        "finally 里必须注销兜底任务"
    # 删除前的存在性检查 + 启动前残留预检，共两处按名查询
    assert code.count("Get-NetFirewallRule -Name $RuleName") == 2, \
        "需要启动前残留预检与 finally 删除前存在性检查两处按名查询"
    # 等待恰好一次，时长就是 -Seconds 参数（上限已在别处校验）
    assert code.count("Start-Sleep") == 1
    assert "Start-Sleep -Seconds $Seconds" in code


# ----------------------------------------------------------- 日志

def test_step_logging_to_evidence_dir():
    """规格⑤：时间戳+步骤日志；默认目录固定 data\\ops-evidence\\drill-block。"""
    code = _code()
    assert re.search(r"\[string\]\$LogDir\s*=\s*\"\"", code), \
        "日志目录必须是可传参数，缺省空后落到固定目录"
    assert "data\\ops-evidence\\drill-block" in code, \
        "缺省日志目录必须固定在 data\\ops-evidence\\drill-block"
    assert "function Write-DrillLog" in code
    assert "yyyy-MM-dd HH:mm:ss" in code, "日志行必须带时间戳"
    for marker in ("START", "STEP0", "STEP1", "STEP2", "STEP3",
                   "FINALLY", "END"):
        assert marker in code, f"缺少步骤日志标记 {marker}"
    # L2（G19-1）：日志时刻一律 UTC（PS5.1 无 -AsUTC → [DateTime]::UtcNow；
    # 日志行与日志文件名两处都取 UTC，本地时间格式化不得再出现）
    assert code.count("[DateTime]::UtcNow") == 2, \
        "日志行与日志文件名的时间戳都必须取 [DateTime]::UtcNow"
    assert "Get-Date -Format" not in code, "不得再用本地时间 Get-Date -Format"
    # 失败必须以非零退出码暴露（不静默成功）
    assert "exit 1" in code and "exit 0" in code
