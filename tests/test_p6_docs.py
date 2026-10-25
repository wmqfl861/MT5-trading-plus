"""T15（P6）：agent 规则文件与权限隔离的轻量守护测试。

守护三件事（文档即契约，被改空/删句即红）：
1. docs/p6/research-agent.md 与 docs/p6/review-agent.md 存在，
   且各自包含关键条款（先卡后测 / 锁箱 / 不改引擎 / 对抗性 / 策略是假的 等）；
2. docs/p6/permissions.md 含 icacls 命令样例、钩子脚本路径与 MCP 侧说明；
3. scripts/hooks/pre-commit-readonly-paths（可导入函数 + 薄壳）对触碰
   engine_a.py 等禁改路径的暂存返回非零，对研究路径放行。
"""
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RESEARCH_RULES = REPO_ROOT / "docs" / "p6" / "research-agent.md"
REVIEW_RULES = REPO_ROOT / "docs" / "p6" / "review-agent.md"
PERMISSIONS = REPO_ROOT / "docs" / "p6" / "permissions.md"
HOOK_PATH = REPO_ROOT / "scripts" / "hooks" / "pre-commit-readonly-paths"


def load_hook():
    """钩子无 .py 扩展名：用 SourceFileLoader 显式按源码加载（可导入函数）。"""
    loader = SourceFileLoader("pre_commit_readonly_paths_t15", str(HOOK_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- 规则文件


def test_research_rules_core_clauses():
    """研究 agent 规则：六条铁律关键词 + 命令白名单 + 禁改清单 + 全量报告。"""
    assert RESEARCH_RULES.is_file(), f"缺规则文件：{RESEARCH_RULES}"
    text = RESEARCH_RULES.read_text(encoding="utf-8")
    # 铁律关键词
    for kw in ("先卡后测", "锁箱", "不改引擎", "白名单"):
        assert kw in text, f"研究规则缺关键条款：{kw}"
    # 命令白名单
    for kw in ("bt card check", "bt screen", "run_backtest.py", "--end",
               "git log"):
        assert kw in text, f"研究规则缺白名单命令：{kw}"
    # 禁改清单（引擎 / 关卡 / 风控 / 门槛）
    for kw in ("backtest", "validation", "mt5live", "gates.toml", "engine_"):
        assert kw in text, f"研究规则缺禁改路径：{kw}"
    # 报告必须含全部运行结果（不许挑选性汇报）
    assert "全部运行结果" in text


def test_research_rules_six_rules_numbered():
    """六条铁律式规则：文档按 1–6 编号列出（不是散文段落）。"""
    text = RESEARCH_RULES.read_text(encoding="utf-8")
    for i in range(1, 7):
        assert f"\n{i}. " in text or text.startswith(f"{i}. "), \
            f"研究规则缺第 {i} 条编号"


def test_review_rules_core_clauses():
    """审查 agent 规则：全新上下文、G2–G4、对抗性、默认立场、只出结论不改代码。"""
    assert REVIEW_RULES.is_file(), f"缺规则文件：{REVIEW_RULES}"
    text = REVIEW_RULES.read_text(encoding="utf-8")
    for kw in ("全新上下文", "manifest", "G2", "G3", "G4", "对抗性",
               "默认立场", "策略是假的", "不改代码", "通过", "否决"):
        assert kw in text, f"审查规则缺关键条款：{kw}"


def test_review_rules_two_adversarial_examples():
    """对抗性测试至少给出两个具体例子（换品种数据、扰动入场时间）。"""
    text = REVIEW_RULES.read_text(encoding="utf-8")
    assert "换品种" in text, "审查规则缺对抗性测试例子：换品种数据"
    assert "入场时间" in text, "审查规则缺对抗性测试例子：扰动入场时间"


# ---------------------------------------------------------------- 权限隔离文档


def test_permissions_doc_acl_and_hook_and_mcp():
    """permissions.md：icacls 命令样例（锁箱目录/lockbox.json/gates.toml）+
    钩子脚本路径 + MCP 侧由锁箱钳制保证。"""
    assert PERMISSIONS.is_file(), f"缺权限方案：{PERMISSIONS}"
    text = PERMISSIONS.read_text(encoding="utf-8")
    assert "icacls" in text, "缺 icacls 命令样例"
    for kw in ("C:\\mt5-lockbox", "lockbox.json", "gates.toml",
               "scripts/hooks/pre-commit-readonly-paths",
               "MCP", "mt5mcp"):
        assert kw in text, f"权限方案缺：{kw}"
    # 拒读 vs 只读的区分必须写清（对 lockbox.json 拒读会静默禁用锁箱）
    assert "拒读" in text and "只读" in text


# ---------------------------------------------------------------- pre-commit 钩子


@pytest.fixture()
def hook():
    return load_hook()


def test_hook_blocks_staged_engine_a(hook, capsys):
    """触碰 engine_a.py 的暂存 → 非零退出并指名路径（核心场景）。"""
    rc = hook.main(["src/mt5quant/backtest/engine_a.py"])
    assert rc != 0
    assert "engine_a.py" in capsys.readouterr().out


@pytest.mark.parametrize("staged", [
    "src/mt5quant/backtest/engine_b.py",
    "src/mt5quant/backtest/engine_c.py",       # 未来的引擎同样受保护（glob）
    "src/mt5quant/validation/gates.py",
    "src/mt5quant/validation/walkforward.py",
    "src/mt5live/risk.py",                     # 实盘/风控
    "config/gates.toml",                       # 门槛配置
    "src\\mt5quant\\backtest\\engine_a.py",    # Windows 反斜杠也要拦
])
def test_hook_blocks_forbidden_paths(hook, staged):
    assert hook.main([staged]) != 0, f"应拦截：{staged}"


@pytest.mark.parametrize("staged", [
    "research/cards/H099.toml",
    "src/mt5quant/strategies/library/breakout.py",
    "docs/research/note-2026-10-24.md",
    "docs/p6/research-agent.md",
])
def test_hook_allows_research_paths(hook, staged):
    """研究产出（卡、策略库、研究文档）不受限。"""
    assert hook.main([staged]) == 0, f"不应拦截：{staged}"


def test_hook_check_staged_function(hook):
    """可导入函数：check_staged 返回逐条原因（不依赖 git）。"""
    msgs = hook.check_staged(
        ["config/gates.toml", "research/cards/H001.toml",
         "src/mt5quant/backtest/engine_a.py"])
    assert len(msgs) == 2
    assert any("gates.toml" in m for m in msgs)
    assert any("engine_a.py" in m for m in msgs)
