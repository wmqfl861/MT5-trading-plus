"""假设卡模式校验与"先卡后测"（T11）。

模板字段见 docs/blueprint.md 附录 A：参数 ≤4、网格组合 ≤100、必填字段齐全。
committed_at 的 git 交互通过注入 runner 测；真实 git 只做只读探测。
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

from mt5quant.research import cards


def _repo_root() -> str:
    """仓库根（tests/ 的上一级）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def write_card(tmp_path, body: str, name: str = "H097.toml"):
    """写一张临时卡（文件名须与 id 一致，否则 load_card 拒绝）。"""
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


VALID = """\
id = "H097"
version = 1
symbols = ["XAUUSD"]
timeframe = "H1"
session = "Europe/London 08:00-10:00"
phenomenon = "伦敦开盘动量"
mechanism = "亚盘区间积累的止损单被开盘流吃掉"
should_fail_when = "亚盘无区间的趋势日"
falsified_if = "样本内净利为负"
graveyard_checked = []

[rules]
entry = "突破亚盘区间上沿"
exit = "收盘反向或时间出场"
stop = "区间对侧"

[params]
fast = [2, 3, 4]
slow = [10, 20]

[expected]
trades_per_year = "50-200"
win_rate = "45-60%"
payoff_ratio = "1.2-2.0"
"""


def valid_card(tmp_path):
    return write_card(tmp_path, VALID)


# ---------------------------------------------------------------- load/validate

def test_load_card_valid(tmp_path):
    card = cards.load_card(valid_card(tmp_path))
    assert card.id == "H097"
    assert card.version == 1
    assert card.symbols == ("XAUUSD",)
    assert card.timeframe == "H1"
    assert card.rules["entry"] == "突破亚盘区间上沿"
    assert card.params == {"fast": (2, 3, 4), "slow": (10, 20)}
    assert card.expected["win_rate"] == "45-60%"
    assert card.path.endswith("H097.toml")


def test_load_card_missing_file(tmp_path):
    with pytest.raises(cards.CardError, match="不存在"):
        cards.load_card(tmp_path / "H000.toml")


def test_load_card_bad_toml(tmp_path):
    p = write_card(tmp_path, "id = \"H097\"\nversion = [broken\n")
    with pytest.raises(cards.CardError, match="TOML"):
        cards.load_card(p)


def test_missing_required_field(tmp_path):
    body = VALID.replace('mechanism = "亚盘区间积累的止损单被开盘流吃掉"\n', "")
    with pytest.raises(cards.CardError, match="mechanism"):
        cards.load_card(write_card(tmp_path, body))


def test_missing_expected_key(tmp_path):
    body = VALID.replace('win_rate = "45-60%"\n', "")
    with pytest.raises(cards.CardError, match="win_rate"):
        cards.load_card(write_card(tmp_path, body))


def test_missing_rules_key(tmp_path):
    body = VALID.replace('stop = "区间对侧"\n', "")
    with pytest.raises(cards.CardError, match="stop"):
        cards.load_card(write_card(tmp_path, body))


def test_unknown_top_level_key_rejected(tmp_path):
    body = VALID.replace('graveyard_checked = []\n',
                         'graveyard_checked = []\nmechnism = "typo"\n')
    with pytest.raises(cards.CardError, match="mechnism"):
        cards.load_card(write_card(tmp_path, body))


def test_too_many_params(tmp_path):
    body = VALID.replace(
        "[params]\nfast = [2, 3, 4]\nslow = [10, 20]\n",
        "[params]\np1 = [1]\np2 = [1]\np3 = [1]\np4 = [1]\np5 = [1]\n")
    with pytest.raises(cards.CardError, match="参数.*5.*4"):
        cards.load_card(write_card(tmp_path, body))


def test_grid_product_over_100(tmp_path):
    body = VALID.replace(
        "[params]\nfast = [2, 3, 4]\nslow = [10, 20]\n",
        "[params]\nfast = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]\n"
        "slow = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]\n")  # 11×11=121
    with pytest.raises(cards.CardError, match="121.*100"):
        cards.load_card(write_card(tmp_path, body))


def test_empty_grid_rejected(tmp_path):
    body = VALID.replace("[params]\nfast = [2, 3, 4]\nslow = [10, 20]\n",
                         "[params]\nfast = []\n")
    with pytest.raises(cards.CardError, match="fast"):
        cards.load_card(write_card(tmp_path, body))


def test_string_grid_value_rejected(tmp_path):
    body = VALID.replace("[params]\nfast = [2, 3, 4]\nslow = [10, 20]\n",
                         "[params]\nfast = [2, 3, \"4\"]\n")
    with pytest.raises(cards.CardError, match="fast"):
        cards.load_card(write_card(tmp_path, body))


def test_bad_timeframe_rejected(tmp_path):
    body = VALID.replace('timeframe = "H1"', 'timeframe = "H7"')
    with pytest.raises(cards.CardError, match="timeframe"):
        cards.load_card(write_card(tmp_path, body))


def test_bad_version_rejected(tmp_path):
    body = VALID.replace("version = 1", "version = 0")
    with pytest.raises(cards.CardError, match="version"):
        cards.load_card(write_card(tmp_path, body))
    body = VALID.replace("version = 1", 'version = "1"')
    with pytest.raises(cards.CardError, match="version"):
        cards.load_card(write_card(tmp_path, body))


def test_empty_symbols_rejected(tmp_path):
    body = VALID.replace('symbols = ["XAUUSD"]', "symbols = []")
    with pytest.raises(cards.CardError, match="symbols"):
        cards.load_card(write_card(tmp_path, body))


def test_filename_must_match_id(tmp_path):
    p = write_card(tmp_path, VALID, name="H098.toml")
    with pytest.raises(cards.CardError, match="H098"):
        cards.load_card(p)


# ------------------------------------------------------- 谱系字段（T21d/H4）

V2 = """\
id = "H012-v2"
family = "H012"
version = 2
parent = "H012-v1"
strategy = "mt5quant.strategies.library.b1_donchian_breakout:DonchianBreakout"
selection_rule = "max_net_profit"
symbols = ["XAUUSD"]
timeframe = "H1"
session = "全时段"
phenomenon = "趋势延续"
mechanism = "时间序列动量"
should_fail_when = "长期震荡"
falsified_if = "样本外夏普为负"
graveyard_checked = []

[rules]
entry = "突破 N 日高点"
exit = "跌破短通道"
stop = "ATR 止损"

[params]
entry_days = [10, 20]

[expected]
trades_per_year = "10-40"
win_rate = "35-50%"
payoff_ratio = "1.5-3.0"
"""


def v2_card(tmp_path):
    return write_card(tmp_path, V2, name="H012-v2.toml")


def test_v2_card_loads_lineage_fields(tmp_path):
    card = cards.load_card(v2_card(tmp_path))
    assert card.family == "H012"
    assert card.parent == "H012-v1"
    assert (card.strategy
            == "mt5quant.strategies.library.b1_donchian_breakout:DonchianBreakout")
    assert card.selection_rule == "max_net_profit"
    assert card.version == 2
    assert card.is_legacy is False
    assert card.migration_hint == ""


def test_legacy_card_defaults_family_to_id(tmp_path):
    """旧卡缺字段：family=id、strategy=None（须由 --strategy 给出）+ 迁移提示。"""
    card = cards.load_card(valid_card(tmp_path))
    assert card.family == "H097"           # 无 -v 后缀 → family = id
    assert card.parent == ""
    assert card.strategy is None
    assert card.selection_rule == "max_net_profit"
    assert card.is_legacy is True
    assert "--strategy" in card.migration_hint


def test_missing_family_on_versioned_id_derives_family(tmp_path):
    body = V2.replace('family = "H012"\n', "")
    card = cards.load_card(write_card(tmp_path, body, name="H012-v2.toml"))
    assert card.family == "H012"


def test_family_mismatched_with_id_rejected(tmp_path):
    body = V2.replace('family = "H012"', 'family = "H13"')
    with pytest.raises(cards.CardError, match="family"):
        cards.load_card(write_card(tmp_path, body, name="H012-v2.toml"))


def test_parent_required_for_v2(tmp_path):
    body = V2.replace('parent = "H012-v1"\n', "")
    with pytest.raises(cards.CardError, match="parent"):
        cards.load_card(write_card(tmp_path, body, name="H012-v2.toml"))


def test_parent_must_be_empty_for_v1(tmp_path):
    body = VALID.replace("version = 1",
                         'version = 1\nparent = "H096"')
    with pytest.raises(cards.CardError, match="parent"):
        cards.load_card(write_card(tmp_path, body))


def test_id_version_suffix_must_match(tmp_path):
    body = V2.replace("version = 2", "version = 3")
    with pytest.raises(cards.CardError, match="-v2"):
        cards.load_card(write_card(tmp_path, body, name="H012-v2.toml"))


def test_strategy_bad_format_rejected(tmp_path):
    for bad in ("no_colon_here", " :Cls", "mod:"):
        body = V2.replace(
            'strategy = "mt5quant.strategies.library.b1_donchian_breakout:DonchianBreakout"',
            f'strategy = "{bad}"')
        with pytest.raises(cards.CardError, match="strategy"):
            cards.load_card(write_card(tmp_path, body, name="H012-v2.toml"))


def test_selection_rule_blank_allowed_default(tmp_path):
    """selection_rule 缺省 → max_net_profit（T21e 的 --params auto 规则）。"""
    body = V2.replace('selection_rule = "max_net_profit"\n', "")
    card = cards.load_card(write_card(tmp_path, body, name="H012-v2.toml"))
    assert card.selection_rule == "max_net_profit"


def test_repo_legacy_cards_load_with_defaults():
    """research/cards/ 旧卡 H001-H007 不改（迁移由主会话决定）——仍可装载。"""
    repo = os.path.join(_repo_root(), "research", "cards")
    for name in ("H001", "H004", "H007"):
        card = cards.load_card(os.path.join(repo, f"{name}.toml"))
        assert card.family == name
        assert card.strategy is None
        assert card.is_legacy is True


# ---------------------------------------------------------------- param_grid

def test_param_grid_cartesian(tmp_path):
    card = cards.load_card(valid_card(tmp_path))
    grid = card.param_grid()
    assert len(grid) == 6
    assert {"fast": 3, "slow": 20} in grid


def test_param_grid_empty_params_is_single_empty_combo(tmp_path):
    body = VALID.replace("[params]\nfast = [2, 3, 4]\nslow = [10, 20]\n",
                         "[params]\n")
    card = cards.load_card(write_card(tmp_path, body))
    assert card.param_grid() == [{}]


# ---------------------------------------------------------------- committed_at

class _Proc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def test_committed_at_untracked_returns_none(tmp_path):
    p = valid_card(tmp_path)
    out = cards.committed_at(p, runner=lambda args, cwd: _Proc(0, ""))
    assert out is None


def test_committed_at_git_error_returns_none(tmp_path):
    p = valid_card(tmp_path)
    out = cards.committed_at(p, runner=lambda args, cwd: _Proc(128, ""))
    assert out is None


def test_committed_at_parses_iso_with_timezone(tmp_path):
    p = valid_card(tmp_path)
    out = cards.committed_at(
        p, runner=lambda args, cwd: _Proc(0, "2026-10-20T09:30:00+08:00\n"))
    assert out == datetime(2026, 10, 20, 9, 30,
                           tzinfo=timezone(timedelta(hours=8)))


def test_committed_at_calls_git_log_with_path(tmp_path):
    seen = {}

    def runner(args, cwd):
        seen["args"] = args
        seen["cwd"] = cwd
        return _Proc(0, "2026-10-20T09:30:00+08:00\n")

    p = valid_card(tmp_path)
    cards.committed_at(p, runner=runner)
    assert seen["args"][:3] == ["git", "log", "-1"]
    assert "--" in seen["args"]
    assert str(p) in seen["args"]


def test_committed_at_real_git_tracked_file():
    """只读探测：仓库内已提交文件（README.md）能取到带时区的提交时间。"""
    out = cards.committed_at(os.path.join(_repo_root(), "README.md"))
    assert isinstance(out, datetime)
    assert out.tzinfo is not None


def test_committed_at_real_git_ignored_file_returns_none():
    """仓库内但从未提交的文件（data/ 被 gitignore）→ None（如实=未提交）。

    CI 的干净克隆里没有 data/ 目录——先建目录再落探针（目录本身也被忽略）。
    """
    probe = os.path.join(_repo_root(), "data", "_card_commit_probe.toml")
    os.makedirs(os.path.dirname(probe), exist_ok=True)
    with open(probe, "w", encoding="utf-8") as f:
        f.write('id = "H999"\n')
    try:
        assert cards.committed_at(probe) is None
    finally:
        os.remove(probe)


# ------------------------------------------------------- 先卡后测（纯函数）

def test_uncommitted_card_rejected():
    with pytest.raises(cards.CardNotCommittedError, match="先卡后测|提交"):
        cards.ensure_card_precedes_run("H097", None, None)


def test_committed_after_first_run_rejected():
    committed = datetime(2026, 10, 22, tzinfo=timezone.utc)
    first_run = datetime(2026, 10, 21, tzinfo=timezone.utc)
    with pytest.raises(cards.CardLateCommitError, match="首次运行"):
        cards.ensure_card_precedes_run("H097", committed, first_run)


def test_committed_before_first_run_ok():
    committed = datetime(2026, 10, 20, tzinfo=timezone.utc)
    first_run = datetime(2026, 10, 21, tzinfo=timezone.utc)
    cards.ensure_card_precedes_run("H097", committed, first_run)


def test_committed_without_prior_runs_ok():
    cards.ensure_card_precedes_run(
        "H097", datetime(2026, 10, 20, tzinfo=timezone.utc), None)


def test_ensure_mixed_awareness_is_valueerror():
    with pytest.raises(ValueError, match="aware|naive"):
        cards.ensure_card_precedes_run(
            "H097", datetime(2026, 10, 20, tzinfo=timezone.utc),
            datetime(2026, 10, 21))


# --------------------------------------------- 工作区干净（T21d/P6）与策略绑定

def test_worktree_clean_runner_injection(tmp_path):
    p = valid_card(tmp_path)
    assert cards.worktree_clean(
        p, runner=lambda args, cwd: _Proc(0, "")) is True
    assert cards.worktree_clean(
        p, runner=lambda args, cwd: _Proc(0, " M H097.toml\n")) is False
    assert cards.worktree_clean(
        p, runner=lambda args, cwd: _Proc(1, "")) is False   # fail-closed


def test_worktree_clean_calls_git_status_with_path(tmp_path):
    seen = {}

    def runner(args, cwd):
        seen["args"] = args
        return _Proc(0, "")

    p = valid_card(tmp_path)
    cards.worktree_clean(p, runner=runner)
    assert seen["args"][:4] == ["git", "status", "--porcelain", "--"]
    assert str(p) in seen["args"]


def test_worktree_clean_real_git_lifecycle(tmp_path):
    """真实 git：提交后干净 → 改动即脏 → 再提交又干净（P6 场景）。"""
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    p = write_card(repo, VALID)

    def git(*args):
        subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                       check=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    git("add", "H097.toml")
    git("commit", "-qm", "card")
    assert cards.worktree_clean(p) is True
    p.write_text(VALID + "# local edit\n", encoding="utf-8")
    assert cards.worktree_clean(p) is False
    git("add", "H097.toml")
    git("commit", "-qm", "card edit")
    assert cards.worktree_clean(p) is True


def test_p6_dirty_worktree_rejected():
    """探针 P6：卡文件改了没提交 → 先卡后测必须拒绝。"""
    committed = datetime(2026, 10, 20, tzinfo=timezone.utc)
    with pytest.raises(cards.CardDirtyWorktreeError, match="未提交改动"):
        cards.ensure_card_precedes_run(
            "H097", committed, None, worktree_clean=False)
    # 已跑过的卡同理
    with pytest.raises(cards.CardDirtyWorktreeError):
        cards.ensure_card_precedes_run(
            "H097", committed,
            datetime(2026, 10, 21, tzinfo=timezone.utc),
            worktree_clean=False)


def test_clean_worktree_passes():
    committed = datetime(2026, 10, 20, tzinfo=timezone.utc)
    first_run = datetime(2026, 10, 21, tzinfo=timezone.utc)
    cards.ensure_card_precedes_run("H097", committed, first_run,
                                   worktree_clean=True)
    # None = 未检查（向后兼容的旧调用）
    cards.ensure_card_precedes_run("H097", committed, first_run)


def test_ensure_strategy_matches(tmp_path):
    card = cards.load_card(v2_card(tmp_path))
    ref = "mt5quant.strategies.library.b1_donchian_breakout:DonchianBreakout"
    cards.ensure_strategy_matches(card, ref)          # 一致 → 放行
    with pytest.raises(cards.CardStrategyMismatchError, match="不一致"):
        cards.ensure_strategy_matches(card, "mt5quant.strategies.library.buy_hold:BuyHold")
    legacy = cards.load_card(valid_card(tmp_path))
    cards.ensure_strategy_matches(legacy, "any:Thing")  # 旧卡不限制


# ------------------------------------------------- T37（H3/D23）：代码指纹

import hashlib as _hashlib  # noqa: E402

_LIB = "mt5quant.strategies.library"


def test_strategy_fingerprint_deterministic_and_strategy_specific():
    """真仓只读探测：同策略两次指纹一致（sha256 hex）；不同策略指纹不同。"""
    fp1 = cards.strategy_fingerprint(f"{_LIB}.buy_hold:BuyHold", _repo_root())
    fp2 = cards.strategy_fingerprint(f"{_LIB}.buy_hold:BuyHold", _repo_root())
    assert fp1 == fp2
    assert len(fp1) == 64 and int(fp1, 16) >= 0          # sha256 hexdigest
    other = cards.strategy_fingerprint(
        f"{_LIB}.b1_donchian_breakout:DonchianBreakout", _repo_root())
    assert other != fp1


def test_strategy_fingerprint_canonical_combination(tmp_path):
    """注入 files+runner：逐文件 git hash-object，按路径排序拼
    "relpath:hash\\n"，整体 sha256（手算对照）。"""
    (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y=2\n", encoding="utf-8")

    def runner(args, cwd):
        assert args[:2] == ["git", "hash-object"]        # 逐文件 hash-object
        return _Proc(0, "HA1\nHB2\n")                    # 与文件同序输出

    fp = cards.strategy_fingerprint(
        "mod:Cls", tmp_path, runner=runner,
        files=[tmp_path / "b.py", tmp_path / "a.py"])    # 乱序给入 → 排序
    expect = _hashlib.sha256("a.py:HA1\nb.py:HB2\n".encode()).hexdigest()
    assert fp == expect


def test_strategy_files_exclude_outside_library():
    """指纹覆盖范围：策略类所在模块必进；strategies/library **外**的依赖
    （如 strategies.base）不进指纹。"""
    main = cards._module_file(f"{_LIB}.buy_hold")
    files = cards.strategy_files(f"{_LIB}.buy_hold:BuyHold")
    assert [f.resolve() for f in files] == [main.resolve()]


def test_strategy_files_include_intra_library_imports(tmp_path):
    """import 的 strategies/library 内模块进指纹（绝对 import 与
    from-import 两种形式都认）。"""
    main = cards._module_file(f"{_LIB}.buy_hold")
    probe = tmp_path / "zz_probe.py"
    probe.write_text(
        "from mt5quant.strategies.library import buy_hold\n"
        f"import {_LIB}.b1_donchian_breakout\n",
        encoding="utf-8")
    deps = cards._imports_within(probe, main.parent)
    assert {f.name for f in deps} == {"buy_hold.py",
                                      "b1_donchian_breakout.py"}


def test_strategy_files_transitive_within_library(tmp_path, monkeypatch):
    """传递闭包：alpha imports beta（库内）→ 两个文件都进指纹；
    库外的 numpy 不进。"""
    libdir = tmp_path / "library"
    libdir.mkdir()
    (libdir / "__init__.py").write_text("", encoding="utf-8")
    alpha = libdir / "alpha.py"
    beta = libdir / "beta.py"
    alpha.write_text(f"import {_LIB}.beta\n", encoding="utf-8")
    beta.write_text("import numpy\n", encoding="utf-8")
    fake = {f"{_LIB}": libdir / "__init__.py",
            f"{_LIB}.alpha": alpha, f"{_LIB}.beta": beta}
    monkeypatch.setattr(cards, "_module_file", lambda name: fake.get(name))
    files = cards.strategy_files(f"{_LIB}.alpha:Alpha")
    assert {f.name for f in files} == {"alpha.py", "beta.py"}


def test_strategy_fingerprint_bad_ref_rejected():
    for bad in ("no_colon", ":Cls", "mod:"):
        with pytest.raises(ValueError, match="module:Class"):
            cards.strategy_files(bad)


def _git_repo_with_strategy(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    f = repo / "strat.py"
    f.write_text("class S: pass\n", encoding="utf-8")

    def git(*args):
        subprocess.run(["git", *args], cwd=str(repo),
                       capture_output=True, check=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    return repo, f, git


def test_ensure_strategy_clean_uncommitted_rejected(tmp_path):
    """计划用例 3（函数级）：策略文件未提交 → StrategyDirtyError。"""
    repo, f, _ = _git_repo_with_strategy(tmp_path)      # 未 commit
    with pytest.raises(cards.StrategyDirtyError, match="未提交"):
        cards.ensure_strategy_clean("m:S", repo, files=[f])


def test_ensure_strategy_clean_dirty_rejected(tmp_path):
    repo, f, git = _git_repo_with_strategy(tmp_path)
    git("add", "strat.py")
    git("commit", "-qm", "s")
    cards.ensure_strategy_clean("m:S", repo, files=[f])  # 已提交且干净 → 通过
    f.write_text("class S: pass  # local edit\n", encoding="utf-8")
    with pytest.raises(cards.StrategyDirtyError, match="工作区|未提交改动"):
        cards.ensure_strategy_clean("m:S", repo, files=[f])


def test_ensure_strategy_clean_fail_closed_on_git_error(tmp_path):
    """git 返回非零（无法证明干净）→ 从严拒绝（与 worktree_clean 同口径）。"""
    repo, f, _ = _git_repo_with_strategy(tmp_path)
    with pytest.raises(cards.StrategyDirtyError):
        cards.ensure_strategy_clean(
            "m:S", repo, files=[f],
            runner=lambda a, cwd=None, **kw: _Proc(128, ""))


def test_ensure_strategy_clean_real_repo_library_strategy():
    """只读探测：本仓策略库文件已提交且干净 → 通过（bt/回测运行的前提）。"""
    cards.ensure_strategy_clean(f"{_LIB}.buy_hold:BuyHold", _repo_root())
