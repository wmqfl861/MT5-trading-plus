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
    """仓库内但从未提交的文件（data/ 被 gitignore）→ None（如实=未提交）。"""
    probe = os.path.join(_repo_root(), "data", "_card_commit_probe.toml")
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
