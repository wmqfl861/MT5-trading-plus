"""T15（P6）/ T29：agent 规则文件与权限白名单钩子的轻量守护测试。

守护四件事（文档即契约，被改空/删句即红）：
1. docs/p6/research-agent.md 与 docs/p6/review-agent.md 存在，
   且各自包含关键条款（先卡后测 / 锁箱 / 不改引擎 / 白名单 / 对抗性 /
   策略是假的 / 卡谱系 v2）；
2. docs/p6/permissions.md 含 icacls 命令样例、钩子脚本路径与 MCP 侧说明；
3. scripts/hooks/pre-commit-readonly-paths 为**白名单 + 缺省拒绝**模式
   （T29 / M12）：研究、审查角色只放行——新增卡 research/cards/*.toml
   （仅 git 状态 A）、src/mt5quant/strategies/library/**、
   tests/test_strategies_*.py、docs/research/**；其余路径（含钩子自身、
   scripts/、CI 配置、costs、research 包、data、config）一律拒绝；
4. 规则文件与代码不再矛盾（T29 / L2）：版本迭代=同谱系新建文件
   H###-v2.toml、旧文件不得再改；卡上 strategy 字段绑定策略类，
   bt 拒绝其他策略。
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

# 研究/审查角色参数（钩子接受 --role；env 显式传 {} 保证测试不受宿主环境影响）
RESEARCH = ["--role", "research"]


def load_hook():
    """钩子无 .py 扩展名：用 SourceFileLoader 显式按源码加载（可导入函数）。"""
    loader = SourceFileLoader("pre_commit_readonly_paths_t29", str(HOOK_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- 规则文件


def test_research_rules_core_clauses():
    """研究 agent 规则：六条铁律关键词 + 命令白名单 + 白名单外只读 + 全量报告。"""
    assert RESEARCH_RULES.is_file(), f"缺规则文件：{RESEARCH_RULES}"
    text = RESEARCH_RULES.read_text(encoding="utf-8")
    # 铁律关键词
    for kw in ("先卡后测", "锁箱", "不改引擎", "白名单"):
        assert kw in text, f"研究规则缺关键条款：{kw}"
    # 命令白名单
    for kw in ("bt card check", "bt screen", "run_backtest.py", "--end",
               "git log"):
        assert kw in text, f"研究规则缺白名单命令：{kw}"
    # 白名单外只读（引擎 / 关卡 / 风控 / 门槛 仍在只读示例里）
    for kw in ("backtest", "validation", "mt5live", "gates.toml", "engine_"):
        assert kw in text, f"研究规则缺只读路径示例：{kw}"
    # 报告必须含全部运行结果（不许挑选性汇报）
    assert "全部运行结果" in text


def test_research_rules_six_rules_numbered():
    """六条铁律式规则：文档按 1–6 编号列出（不是散文段落）。"""
    text = RESEARCH_RULES.read_text(encoding="utf-8")
    for i in range(1, 7):
        assert f"\n{i}. " in text or text.startswith(f"{i}. "), \
            f"研究规则缺第 {i} 条编号"


def test_research_rules_card_lineage_v2():
    """T29/L2：版本迭代=同谱系新建文件 H###-v2.toml、旧文件不得再改
    （与蓝图附录 A v2、cards 的先卡后测判定一致，删去"改同一文件"矛盾）；
    卡上 strategy 字段绑定策略类，bt 拒绝其他策略。"""
    text = RESEARCH_RULES.read_text(encoding="utf-8")
    assert "同谱系新建" in text, "研究规则缺谱系规则：同谱系新建 H###-v2.toml"
    assert "不得再改" in text, "研究规则缺约束：旧版本文件不得再改"
    assert "绑定策略类" in text, "研究规则缺卡字段说明：strategy 绑定策略类"
    assert "其他策略" in text, "研究规则缺约束：bt 拒绝用其他策略跑该卡"


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


def test_review_rules_card_lineage_v2():
    """审查规则同样对齐卡谱系 v2 与 strategy 绑定（L2）。"""
    text = REVIEW_RULES.read_text(encoding="utf-8")
    assert "同谱系新建" in text, "审查规则缺谱系规则：同谱系新建 H###-v2.toml"
    assert "不得再改" in text, "审查规则缺约束：旧版本文件不得再改"
    assert "绑定策略类" in text, "审查规则缺卡字段说明：strategy 绑定策略类"
    assert "其他策略" in text, "审查规则缺约束：bt 拒绝用其他策略跑该卡"


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


# ------------------------------------------------- T27（H3）：研究库隔离


def test_permissions_doc_research_store_isolation():
    """T27/H3：研究、审查账户对主库 data\\bars、data\\ticks 拒读（含锁箱段
    的物理隔离），对 data_research 只读；icacls 命令清单齐全；钩子章节
    同步 T29 白名单新签名（check_staged / violation_reason）。"""
    assert PERMISSIONS.is_file()
    text = PERMISSIONS.read_text(encoding="utf-8")
    # 主库 bars/ticks 拒读（deny 行 + 路径同时出现）
    for path in ('data\\bars', 'data\\ticks'):
        assert path in text, f"权限方案缺主库路径 {path} 的隔离说明"
    assert text.count('/deny "MT5Research:') >= 4, \
        "拒读 icacls 样例不足（bars/ticks 至少各一条 deny）"
    # 研究库只读（grant R + deny 写删；export_research_store 产出口径）
    assert 'data_research' in text, "权限方案缺研究库 data_research 条目"
    assert "MT5Research:R" in text, "研究库只读授权（:R）样例缺失"
    assert "export_research_store" in text, "缺研究库导出工具指引"
    assert "tick" in text and "spread_model.json" in text, \
        "缺说明：tick 不进研究库，成本经缓存文件提供"
    # 钩子章节同步 T29 新签名（白名单 + 可导入函数新名）
    for kw in ("check_staged", "violation_reason", "白名单", "MT5QUANT_COMMIT_ROLE"):
        assert kw in text, f"权限方案钩子章节缺（T29 新签名）：{kw}"


def test_readme_research_store_not_main_store():
    """T27/H3/L3：README 不再推荐直读主库 data/bars；研究侧用法改为研究库
    （--research-store / data_research），并注明主库仅限管理员与实盘侧。"""
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert 'read_parquet("data/bars' not in text, \
        "README 仍有直读主库 data/bars 的示例（L3/H3）"
    assert "data_research" in text, "README 缺研究库用法（data_research）"
    assert "--research-store" in text, "README 缺 --research-store 用法"
    assert "管理员与实盘" in text or "仅限管理员" in text, \
        "README 缺主库访问范围说明（仅管理员与实盘侧）"
    assert "export_research_store" in text, "README 缺研究库导出工具指引"


# ---------------------------------------------------------------- pre-commit 钩子（T29 白名单 + 缺省拒绝）


@pytest.fixture()
def hook():
    return load_hook()


def test_hook_blocks_staged_engine_a(hook, capsys):
    """研究角色触碰 engine_a.py 的暂存 → 非零退出并指名路径（核心场景）。"""
    rc = hook.main(RESEARCH + ["src/mt5quant/backtest/engine_a.py"], env={})
    assert rc != 0
    assert "engine_a.py" in capsys.readouterr().out


@pytest.mark.parametrize("staged", [
    "src/mt5quant/backtest/engine_b.py",
    "src/mt5quant/backtest/engine_c.py",       # 未来的引擎同样受保护（glob）
    "src/mt5quant/validation/gates.py",
    "src/mt5quant/validation/walkforward.py",
    "src/mt5live/risk.py",                     # 实盘/风控
    "src\\mt5quant\\backtest\\engine_a.py",    # Windows 反斜杠也要拦
    # ---- M12 漏项：黑名单没保住的路径，白名单模式下全部拒绝 ----
    "src/mt5quant/costs/spread.py",            # 成本模型
    "src/mt5quant/costs/swap.py",
    "src/mt5quant/research/ledger.py",         # 台账/锁箱预算
    "src/mt5data/ticks.py",                    # 数据层与锁箱加载器
    "scripts/bt.py",                           # 先卡后测/台账的执行点
    "scripts/run_backtest.py",
    "scripts/hooks/pre-commit-readonly-paths", # 钩子自身（白名单的门锁）
    ".github/workflows/tests.yml",             # CI 配置
    "config/gates.toml",                       # 门槛配置
    "config/risk.toml",
    "config/sessions.toml",
    "data/lockbox.json",
    "data/research/ledger.sqlite",
    "research/cards/H003.toml",                # 裸路径按 M 算：修改已提交的卡
    "research/cards/notes.md",                 # cards 下只放行 *.toml
    "research/cards/sub/H100.toml",            # 卡目录不嵌套子目录
    "tests/test_p6_docs.py",                   # 本守护测试不在白名单
    "tests/test_validation.py",
    "docs/p6/research-agent.md",               # 研究角色不能改自己的规则
    "docs/p6/review-agent.md",
    "docs/p6/permissions.md",
    "README.md",
    "docs/blueprint.md",
])
def test_hook_blocks_everything_outside_whitelist(hook, staged):
    """白名单之外一律拒绝（研究/审查角色；缺省拒绝）。"""
    assert hook.main(RESEARCH + [staged], env={}) != 0, f"应拦截：{staged}"


@pytest.mark.parametrize("staged", [
    "src/mt5quant/strategies/library/breakout.py",
    "tests/test_strategies_baselines_b1.py",
    "docs/research/note-2026-10-24.md",
])
def test_hook_allows_research_paths(hook, staged):
    """研究产出（策略库、策略测试、研究文档）放行；裸路径按 M 处理。"""
    assert hook.main(RESEARCH + [staged], env={}) == 0, f"不应拦截：{staged}"


# ---- T29 要求的五种情形 ----


def test_hook_whitelist_new_card_allowed(hook):
    """情形 1：新增卡（git 状态 A）放行——研究角色的核心产出。"""
    assert hook.main(RESEARCH + ["A\tresearch/cards/H012-v2.toml"],
                     env={}) == 0


def test_hook_whitelist_modified_card_rejected(hook):
    """情形 2：修改旧卡拒——已提交的卡不得修改，迭代=同谱系新建 v2 文件；
    删除、重命名同样拒。"""
    assert hook.main(RESEARCH + ["M\tresearch/cards/H003.toml"], env={}) != 0
    assert hook.main(RESEARCH + ["D\tresearch/cards/H003.toml"], env={}) != 0
    assert hook.main(RESEARCH + ["R\tresearch/cards/H003.toml"], env={}) != 0


def test_hook_whitelist_cost_model_rejected(hook):
    """情形 3：改成本模型拒（M12：黑名单漏项，白名单补上）。"""
    assert hook.main(RESEARCH + ["M\tsrc/mt5quant/costs/spread.py"],
                     env={}) != 0


def test_hook_whitelist_bt_script_rejected(hook):
    """情形 4：改 bt.py 拒——研究角色不能关掉先卡后测/改台账口径。"""
    assert hook.main(RESEARCH + ["M\tscripts/bt.py"], env={}) != 0


def test_hook_whitelist_hook_itself_rejected(hook):
    """情形 5：改钩子自身拒——否则白名单可被研究角色拆掉。"""
    assert hook.main(RESEARCH + ["M\tscripts/hooks/pre-commit-readonly-paths"],
                     env={}) != 0


# ---- 角色参数化：admin 全放行；缺省/未知角色拒绝 ----


def test_hook_role_admin_allows_everything(hook):
    """admin：引擎、门槛、钩子自身、旧卡修改全部放行。"""
    paths = ["M\tsrc/mt5quant/backtest/engine_a.py",
             "M\tconfig/gates.toml",
             "M\tscripts/hooks/pre-commit-readonly-paths",
             "M\tresearch/cards/H003.toml"]
    assert hook.main(["--role", "admin"] + paths, env={}) == 0


def test_hook_missing_role_denies_by_default(hook):
    """缺省拒绝：未指定角色时连白名单路径也不放行（fail-closed）。"""
    assert hook.main(["M\tsrc/mt5quant/strategies/library/breakout.py"],
                     env={}) != 0
    assert hook.main(["A\tresearch/cards/H012-v2.toml"], env={}) != 0


def test_hook_unknown_role_denies(hook):
    """未知角色一律拒绝。"""
    assert hook.main(["--role", "root", "A\tresearch/cards/H012-v2.toml"],
                     env={}) != 0


def test_hook_role_from_env(hook):
    """角色可经环境变量 MT5QUANT_COMMIT_ROLE 指定（真实钩子的主要入口）。"""
    env = {"MT5QUANT_COMMIT_ROLE": "admin"}
    assert hook.main(["M\tsrc/mt5quant/backtest/engine_a.py"], env=env) == 0
    env = {"MT5QUANT_COMMIT_ROLE": "review"}
    assert hook.main(["M\tconfig/gates.toml"], env=env) != 0
    assert hook.main(["A\tresearch/cards/H012-v2.toml"], env=env) == 0


def test_hook_cli_role_overrides_env(hook):
    """命令行 --role 优先于环境变量。"""
    env = {"MT5QUANT_COMMIT_ROLE": "admin"}
    assert hook.main(["--role", "research",
                      "M\tsrc/mt5quant/backtest/engine_a.py"], env=env) != 0


def test_hook_delete_rejected_on_whitelist_path(hook):
    """白名单路径也只放行 A/M：删除策略库/研究文档需走管理员。"""
    assert hook.main(RESEARCH + ["D\tsrc/mt5quant/strategies/library/b1.py"],
                     env={}) != 0
    assert hook.main(RESEARCH + ["D\tdocs/research/note.md"], env={}) != 0


def test_hook_check_staged_function(hook):
    """可导入函数：check_staged 按 (状态, 路径) 返回逐条原因（不依赖 git）。"""
    msgs = hook.check_staged(
        [("M", "config/gates.toml"), ("A", "research/cards/H001.toml"),
         ("M", "src/mt5quant/backtest/engine_a.py")], role="research")
    assert len(msgs) == 2
    assert any("gates.toml" in m for m in msgs)
    assert any("engine_a.py" in m for m in msgs)
    # admin 角色下同批改动全部放行
    assert hook.check_staged(
        [("M", "config/gates.toml"), ("M", "research/cards/H001.toml")],
        role="admin") == []


# ------------------------------------------------- T38（H4/D25）：CI 白名单


def test_hook_check_paths_function(hook):
    """T38/D25：CI 与本地钩子共用的检查函数 check_paths——修改旧卡、改
    bt.py、改钩子本身三种情形报违规；新增卡与策略库修改放行。"""
    viol = hook.check_paths([("M", "research/cards/H003.toml")],
                            role="research")
    assert viol, "修改旧卡应报违规"
    assert any("research/cards/H003.toml" in v for v in viol)
    assert hook.check_paths([("M", "scripts/bt.py")], role="research"), \
        "改 bt.py 应报违规"
    assert hook.check_paths(
        [("M", "scripts/hooks/pre-commit-readonly-paths")],
        role="research"), "改钩子本身应报违规"
    # 放行：新增卡（状态 A）与策略库修改
    assert hook.check_paths([("A", "research/cards/H012-v2.toml")],
                            role="research") == []
    assert hook.check_paths(
        [("M", "src/mt5quant/strategies/library/b1_donchian_breakout.py")],
        role="research") == []


def test_hook_check_paths_same_rules_as_check_staged(hook):
    """同一份规则：check_paths 与 check_staged 对同批改动结论一致——
    CI 与本地钩子不会各说各话（D25：规则与钩子同一份代码）。"""
    changes = [("A", "research/cards/H001.toml"), ("M", "scripts/bt.py"),
               ("M", "docs/p6/permissions.md"),
               ("M", "src/mt5quant/strategies/library/x.py")]
    assert hook.check_paths(changes, role="research") == \
        hook.check_staged(changes, role="research")
    assert hook.check_paths(changes, role="admin") == []
    assert hook.check_paths(changes, role=None), "未指定角色 → 缺省拒绝"


ALLOWLIST_YML = REPO_ROOT / ".github" / "workflows" / "research-allowlist.yml"


def test_research_allowlist_workflow_wired():
    """T38/D25：research-allowlist.yml 存在且与钩子共用同一份规则——
    python -c 调 check_paths、diff 基线 origin/main...HEAD、PR 触发、
    仅 research/** 头分支强制。"""
    assert ALLOWLIST_YML.is_file(), f"缺 workflow：{ALLOWLIST_YML}"
    text = ALLOWLIST_YML.read_text(encoding="utf-8")
    for kw in ("pull_request", "check_paths", "pre-commit-readonly-paths",
               "origin/main...HEAD", "research/"):
        assert kw in text, f"research-allowlist.yml 缺：{kw}"


def test_permissions_doc_t38_isolation_defaults():
    """T38/D24：permissions.md 写清缺省研究库、--main-store/--ops-ack 语义、
    研究账户运行方式（runas）与 main 分支保护步骤（U0 清单）。"""
    text = PERMISSIONS.read_text(encoding="utf-8")
    for kw in ("--main-store", "--ops-ack", "分支保护", "runas", "check_paths",
               "research-allowlist"):
        assert kw in text, f"permissions.md 缺：{kw}"


def test_readme_t38_default_research_store():
    """README 对齐 T38/D24：缺省即研究库 data_research；读主库须显式
    --main-store（锁箱期另需 --ops-ack）。"""
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "--main-store" in text, "README 缺读主库的显式 flag 说明"
    assert "--ops-ack" in text, "README 缺锁箱期运维豁免说明"
    assert "缺省" in text and "data_research" in text, \
        "README 缺'缺省即研究库'的说明"
