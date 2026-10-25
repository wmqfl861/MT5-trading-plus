"""六张基线假设卡（H002–H007）与策略实现的一致性测试。

- 卡 schema 校验（cards.load_card：字段/参数/网格/文件名）；
- 卡与策略逐一对应：symbols/timeframe/参数名与网格完全一致，
  每个网格组合都能通过策略 validate_params（bt screen 的前提）；
- reentry 用缺省 immediate（任务约定）。
"""
from pathlib import Path

import pytest

from mt5quant.research import cards
from mt5quant.strategies import (
    DonchianBreakout,
    IntradayMomentumNy,
    LondonAsianBreakout,
    NyOpeningRangeBreakout,
    TrendFilteredMeanReversion,
    VolTargetLong,
)

CARDS_DIR = Path(__file__).resolve().parents[1] / "research" / "cards"

CARD_TO_STRATEGY = {
    "H002": DonchianBreakout,
    "H003": NyOpeningRangeBreakout,
    "H004": LondonAsianBreakout,
    "H005": TrendFilteredMeanReversion,
    "H006": IntradayMomentumNy,
    "H007": VolTargetLong,
}


def _load(cid):
    return cards.load_card(CARDS_DIR / f"{cid}.toml")


@pytest.mark.parametrize("cid", sorted(CARD_TO_STRATEGY))
def test_card_schema_valid(cid):
    card = _load(cid)
    assert card.id == cid and card.version >= 1
    assert card.rules and set(card.rules) == {"entry", "exit", "stop"}
    assert set(card.expected) == {"trades_per_year", "win_rate",
                                  "payoff_ratio"}
    assert card.phenomenon and card.mechanism and card.should_fail_when \
        and card.falsified_if


@pytest.mark.parametrize("cid", sorted(CARD_TO_STRATEGY))
def test_card_matches_strategy(cid):
    card = _load(cid)
    cls = CARD_TO_STRATEGY[cid]
    rd = cls.required_data
    assert set(card.symbols) <= set(rd.symbols), \
        f"{cid} 品种超出策略声明：{card.symbols} vs {rd.symbols}"
    assert card.timeframe == rd.timeframe
    spec_names = {s.name for s in cls.params_schema}
    assert set(card.params) == spec_names, \
        f"{cid} 参数名与策略不一致：{set(card.params)} vs {spec_names}"
    grid_by_name = {s.name: set(s.grid) for s in cls.params_schema}
    for name, values in card.params.items():
        assert set(values) <= grid_by_name[name], \
            f"{cid} 参数 {name} 网格越界：{sorted(set(values))} ⊄ " \
            f"{sorted(grid_by_name[name])}"
    combos = card.param_grid()
    assert len(combos) <= 100
    strategy = cls()
    for params in combos:
        strategy.validate_params(dict(params))


@pytest.mark.parametrize("cid", sorted(CARD_TO_STRATEGY))
def test_strategy_grid_size_within_limits(cid):
    cls = CARD_TO_STRATEGY[cid]
    sizes = [len(s.grid) for s in cls.params_schema]
    prod = 1
    for s in sizes:
        prod *= s
    assert prod <= 100 and len(cls.params_schema) <= 4


@pytest.mark.parametrize("cid", sorted(CARD_TO_STRATEGY))
def test_reentry_is_default_immediate(cid):
    assert CARD_TO_STRATEGY[cid].exit_rules.reentry == "immediate"


def test_expected_strategy_grids_documented_here():
    """网格大小备忘（报告口径）：9/6/6/27/4/12 = 64 组，全部 ≤100。"""
    sizes = {cid: len(cards.load_card(CARDS_DIR / f"{cid}.toml").param_grid())
             for cid in sorted(CARD_TO_STRATEGY)}
    assert sizes == {"H002": 9, "H003": 6, "H004": 6, "H005": 27,
                     "H006": 4, "H007": 12}
    assert all(v <= 100 for v in sizes.values())


def test_cards_commit_status_checkable():
    """bt card check 允许未提交（仅警告）；这里只验证 committed_at 可调用
    （子代理禁止 git 写，真跑 screen/gate 由主会话在卡提交后执行）。"""
    for cid in sorted(CARD_TO_STRATEGY):
        # committed_at 返回 None 或 datetime 都合法；不允许抛非 git 异常
        cards.committed_at(CARDS_DIR / f"{cid}.toml")
