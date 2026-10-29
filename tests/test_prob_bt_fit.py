"""T73A bt 接线：拟合上下文分段约束、_prob_run_fields、G3 拒绝、bt fit。"""
import importlib.util
import io
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "src")]


def load_bt():
    spec = importlib.util.spec_from_file_location(
        "bt_t73a", REPO / "scripts" / "bt.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BT = load_bt()

from mt5quant.research import cards as rcards  # noqa: E402
from mt5quant.research import rounds as rounds_mod  # noqa: E402


def write_repo_tree(tmp_path, *, segment="discovery", split="2019-12-01",
                    last_bar="2019-06-01"):
    """research/cards/P001-v1.toml + research/rounds/R2.toml 的最小树。"""
    (tmp_path / "cards").mkdir(parents=True)
    (tmp_path / "rounds").mkdir(parents=True)
    card_body = f'''id = "P001-v1"
family = "P001"
version = 1
parent = ""
candidate = "C101"
round = "R2"
strategy = "tests_fixture:Fix"
symbols = ["XAUUSD"]
timeframe = "H1"
evaluation_mode = "probability"
primary_statistic = "delta_p_one_sided"
segment = "{segment}"
session = "America/New_York"
phenomenon = "p"
mechanism = "m"
should_fail_when = "s"
falsified_if = "f"
graveyard_checked = []
[rules]
entry = "prob"
exit = "tp/sl"
stop = "sl"
[expected]
delta_p = "0.04"
triggers_per_year = "只数"
[prob]
situations = ["S1"]
hypotheses = ["S1:A3:fade>base"]
bracket = {{ sl_atr = 1.0, tp_atr = 2.0, horizon_bars = 24 }}
level_grid = {{ XAUUSD = 50.0 }}
approach = {{ lookback_bars = 24, min_distance_atr = 1.0, touch_atr = 0.25 }}
breakout_atr = 0.25
retest_window_bars = 6
expected_delta_p = 0.04
'''
    card_path = tmp_path / "cards" / "P001-v1.toml"
    card_path.write_text(card_body, encoding="utf-8")
    round_body = f'''id = "R2"
opened = "2026-11-02"
candidates = ["C101"]
trial_budget = 12
lockbox_uses_max = 0
dsr_trial_scope = "project"
advance_rule = "a"
close_rule = "c"
approved_by = "P线审核会话"
[segments]
split = "{split}"
[evaluation.C101]
mode = "probability"
primary = "delta_p_one_sided"
[hypotheses]
C101 = ["S1:A3:fade>base"]
'''
    (tmp_path / "rounds" / "R2.toml").write_text(round_body, encoding="utf-8")
    return card_path


def load_card_at(path):
    card = rcards.load_card(path)
    return card


def bars_upto(end, n=500, start="2016-01-01"):
    """到 end 为止的 H1 bar，超过 n 根取**尾部**（保证末根贴着 end）。"""
    times = pd.date_range(start=start, end=end, freq="1h")
    if len(times) == 0:
        times = pd.date_range(start=start, periods=n, freq="1h")
    times = times[-n:]
    return pd.DataFrame({"time": times, "open": 100.0, "high": 101.0,
                         "low": 99.0, "close": 100.0})


class ZeroC:
    def spread_p90(self, s, h, *, price=None):
        return 0.0

    def slippage(self, s, h, *, price=None):
        return 0.0

    def commission_per_lot(self, s):
        return 0.0


def test_fit_context_discovery_rejects_data_beyond_split(tmp_path):
    card_path = write_repo_tree(tmp_path, segment="discovery")
    card = load_card_at(card_path)
    bars = bars_upto("2020-06-01")             # 越过 2019-12-01
    with pytest.raises(ValueError, match="不得读取"):
        BT._prob_fit_context(card, None, {"XAUUSD": bars}, {"XAUUSD": ZeroC()},
                             tmp_path)


def test_fit_context_discovery_ok_and_train_end_is_split(tmp_path):
    card_path = write_repo_tree(tmp_path, segment="discovery")
    card = load_card_at(card_path)
    bars = bars_upto("2019-06-01")
    ctx = BT._prob_fit_context(card, None, {"XAUUSD": bars},
                               {"XAUUSD": ZeroC()}, tmp_path)
    assert ctx["split"] == "2019-12-01"
    assert ctx["train_end"] == pd.Timestamp("2019-12-01")
    assert ctx["purge_bars"] == 26              # K=24 + 2
    assert ctx["fit_bars"]["XAUUSD"] is bars    # discovery 全段即训练段


def test_fit_context_validation_trains_before_split(tmp_path):
    card_path = write_repo_tree(tmp_path, segment="validation")
    card = load_card_at(card_path)
    bars = bars_upto("2021-06-01", n=30_000)    # 尾部 3 万根跨过分界
    assert pd.to_datetime(bars["time"]).min() < pd.Timestamp("2019-12-01")
    ctx = BT._prob_fit_context(card, None, {"XAUUSD": bars},
                               {"XAUUSD": ZeroC()}, tmp_path)
    assert pd.to_datetime(ctx["fit_bars"]["XAUUSD"]["time"]).max() \
        < pd.Timestamp("2019-12-01")
    # 分界前无数据（全部分布在 split 之后）→ 拒
    bars_after = bars_upto("2021-06-01", n=50)     # 尾部 50 根全在 2021-05 末
    assert pd.to_datetime(bars_after["time"]).min() > pd.Timestamp("2019-12-01")
    with pytest.raises(ValueError, match="没有数据"):
        BT._prob_fit_context(card, None, {"XAUUSD": bars_after},
                             {"XAUUSD": ZeroC()}, tmp_path)


def test_fit_context_g0_uses_first_70pct(tmp_path):
    card_path = write_repo_tree(tmp_path, segment="discovery")
    card = load_card_at(card_path)
    bars = bars_upto("2019-06-01", n=1000)
    ctx = BT._prob_fit_context(card, None, {"XAUUSD": bars},
                               {"XAUUSD": ZeroC()}, tmp_path, for_g0=True)
    assert len(ctx["fit_bars"]["XAUUSD"]) == 700
    assert ctx["train_end"] is None             # 已物理截断


def test_prob_run_fields_roundtrip():
    fit_ctx = {"params_prob": {"segment": "discovery",
                               "split": "2019-12-01",
                               "bracket": {"sl_atr": 1.0}},
               "metrics_prob": {"model_sha256": "ab" * 32,
                                "prob_version": "1.0.0",
                                "purge_bars": 26}}
    params, summary = BT._prob_run_fields(fit_ctx, {"x": 1}, {"net": 2.0})
    assert params == {"x": 1, "prob": fit_ctx["params_prob"]}
    assert summary["prob"]["model_sha256"] == "ab" * 32
    # 非概率卡 → 原样
    p2, s2 = BT._prob_run_fields(None, {"x": 1}, {"net": 2.0})
    assert p2 == {"x": 1} and s2 == {"net": 2.0}
    # 失败运行（summary=None）只带 params 字段
    p3, s3 = BT._prob_run_fields(fit_ctx, {"x": 1}, None)
    assert "prob" in p3 and s3 is None


def test_fitted_factory_returns_frozen_instances():
    class C:
        fittable = True

        def __init__(self):
            self.model = None

        def with_model(self, model):
            c = C()
            c.model = model
            return c

    fac = BT._FittedFactory(C, {"m": 1})
    a, b = fac(), fac()
    assert a.model == {"m": 1} and b.model == {"m": 1} and a is not b
    assert BT._FittedFactory(C, None).fittable is True   # 类属性透传


def test_gate_rejects_g3_for_fittable():
    """T73A：拟合策略的 walk-forward（G3）在 T73B 前拒绝（守卫函数级；
    cmd_gate 装配段的接线调用同一函数）。"""
    msg = BT._reject_fittable_walkforward(_FixProbe, ["G0", "G3"])
    assert msg and "T73B" in msg
    assert BT._reject_fittable_walkforward(_FixProbe, ["G0"]) is None
    assert BT._reject_fittable_walkforward(type("P", (), {"fittable": False}),
                                           ["G3"]) is None


class _FixProbe:
    fittable = True

    def fit(self, bars, costs, params, *, train_end, purge_bars):
        return {"prob_version": "1.0.0"}

    def with_model(self, model):
        n = _FixProbe()
        n.model = model
        return n


def test_prob_fields_into_ledger_chain(tmp_path):
    """T73A：P 线字段（params_json.prob / metrics_json.prob）随运行进台账，
    runs 表结构不变，verify（哈希链）通过。"""
    from mt5quant.research.ledger import Ledger
    fit_ctx = {"params_prob": {"segment": "discovery",
                               "split": "2019-12-01",
                               "bracket": {"sl_atr": 1.0, "tp_atr": 2.0,
                                           "horizon_bars": 24}},
               "metrics_prob": {"model_sha256": "cd" * 32,
                                "prob_version": "1.0.0",
                                "fit_start": "2013-06-01",
                                "fit_end": "2019-11-30",
                                "purge_bars": 26}}
    params, summary = BT._prob_run_fields(fit_ctx, {}, {"n": 1})
    db = tmp_path / "ledger.sqlite"
    with Ledger(db) as led:
        rid = led.record_run(
            card_id="P001-v1", card_version=1, strategy="s:P", params=params,
            symbol="XAUUSD", timeframe="H1", data_first=None, data_last=None,
            data_sha256="a" * 64, engine_version="e", cost_model_version="v",
            gates_hash="", metrics=summary, actor="bt", round_id="R2")
        assert rid == 1
        probs = led.verify_chain()
    assert probs == []          # 哈希链校验通过（新字段进链可验）


# ============================================ T98 波次 2（P-05 cmd fit）

STRATEGY_REF = ("mt5quant.strategies.library.prob.level_decision:"
                "LevelDecisionS1Demo")

SESSIONS_TOML = """version = 1
[rollover]
local_time = "17:00"
tz = "America/New_York"
[forbidden]
pre_minutes = 5
post_minutes = 60
exempt_symbols = ["BTCUSD"]
[costs]
spread_gate_multiplier = 3.0
missing_hour_policy = "forbid"
min_days_per_hour = 20
"""

P2_CARD = '''id = "P005-v1"
family = "P005"
version = 1
parent = ""
candidate = "C101"
round = "R2"
strategy = "{ref}"
symbols = ["XAUUSD"]
timeframe = "H1"
evaluation_mode = "standard"
segment = "{segment}"
session = "America/New_York"
phenomenon = "合成探针"
mechanism = "合成探针"
should_fail_when = "合成探针"
falsified_if = "合成探针"
graveyard_checked = []
[rules]
entry = "由 [prob] 决定"
exit = "tp/sl/K 根归零"
stop = "sl_atr×ATR"
[expected]
delta_p = "探针"
triggers_per_year = "探针"
[prob]
situations = ["S1"]
hypotheses = ["S1:A3:fade>base"]
bracket = {{ sl_atr = 1.0, tp_atr = 2.0, horizon_bars = 8 }}
level_grid = {{ XAUUSD = 2.0 }}
approach = {{ lookback_bars = 12, min_distance_atr = 1.0, touch_atr = 0.25 }}
breakout_atr = 0.25
retest_window_bars = 4
features = ["trend_sma200", "atr_pct_2000"]
prior_strength = 100
min_cell = 100
lb_z = 1.0
entry_threshold_cost_mult = 2.0
expected_delta_p = 0.04
'''


class FlatCosts:
    def spread_p90(self, s, h, *, price=None):
        return 0.02

    def slippage(self, s, h, *, price=None):
        return 0.01

    def commission_per_lot(self, s):
        return 0.0

    def overnight_fee(self, s, side, f, t, *, price=None):
        return 0.0


def _git(repo, *args):
    import subprocess
    subprocess.run(["git", "-c", "user.name=probe",
                    "-c", "user.email=probe@example.invalid", *args],
                   cwd=repo, check=True, capture_output=True)


def _open_r2(ledger_path, round_path):
    import hashlib
    from mt5quant.research.ledger import Ledger
    with Ledger(ledger_path) as led:
        led.record_candidate_check("C101", decision="pass",
                                   result={"probe": True})
        led.record_round_open(
            round_id="R2", candidates=["C101"], trial_budget=4,
            lockbox_uses_max=0, dsr_trial_scope="project",
            advance_rule="a", close_rule="c", approved_by="P线审核会话",
            evaluation={"C101": {"mode": "standard", "primary": "gates_v3"}},
            toml_sha256=hashlib.sha256(
                Path(round_path).read_bytes()).hexdigest(),
            git_commit="0" * 40)


def test_p05_card_path_absolutizes_relative(tmp_path, monkeypatch):
    """P-05/H-09：_card_path 返回绝对路径（相对 cards-dir 在 git 检查下会
    被当成仓库外文件——无提交、恒"干净"，先卡后测被绕过）。"""
    monkeypatch.chdir(tmp_path)
    p = BT._card_path("P001-v1", Path("research/cards"))
    assert p.is_absolute()
    abs_card = tmp_path / "cards" / "P001-v1.toml"
    p2 = BT._card_path(str(abs_card), Path("x"))
    assert p2 == abs_card.resolve()


def test_p05_fit_rejects_unopened_round(fit_env, monkeypatch, capsys):
    """P-05：cmd_fit 轮次未开启 → 拒绝（不落模型；旧实现无轮次检查会
    照常拟合落盘）。"""
    import contextlib
    empty = fit_env["tmp"] / "empty-ledger.sqlite"
    argv = ["fit", "P005-v1", "--strategy", STRATEGY_REF,
            "--cards-dir", str(fit_env["repo"] / "research" / "cards"),
            "--data-root", str(fit_env["store"]), "--ledger", str(empty)]
    out = io.StringIO()
    code = 0
    monkeypatch.chdir(fit_env["tmp"])
    try:
        with contextlib.redirect_stdout(out):
            code = BT.main(argv)
    except SystemExit as e:
        code = e.code
    assert code == 1, out.getvalue()
    assert "未开启" in out.getvalue()
    assert not (fit_env["store"] / "models").exists()


def test_p05_fit_rejects_uncommitted_card(fit_env, monkeypatch, capsys):
    """P-05：卡未提交 → 拒绝（准入先于拟合；旧实现无提交检查会照常
    拟合落盘）。"""
    import contextlib
    monkeypatch.setattr(BT.cards, "committed_at",
                        lambda path, runner=None: None)
    argv = ["fit", "P005-v1", "--strategy", STRATEGY_REF,
            "--cards-dir", str(fit_env["repo"] / "research" / "cards"),
            "--data-root", str(fit_env["store"]),
            "--ledger", str(fit_env["ledger"])]
    out = io.StringIO()
    code = 0
    monkeypatch.chdir(fit_env["tmp"])
    try:
        with contextlib.redirect_stdout(out):
            code = BT.main(argv)
    except SystemExit as e:
        code = e.code
    assert code == 1
    assert "提交" in out.getvalue()
    assert not (fit_env["store"] / "models").exists()


def test_p05_fit_rejects_standard_discovery(fit_env, monkeypatch, capsys):
    """P-05：standard 卡只有 validation 段可拟合（P2 决策卡=T92；
    discovery P1 属 probability）。"""
    import contextlib
    p = fit_env["repo"] / "research" / "cards" / "P005-v1.toml"
    p.write_text(P2_CARD.format(ref=STRATEGY_REF, segment="discovery"),
                 encoding="utf-8")
    _git(fit_env["repo"], "add", ".")
    _git(fit_env["repo"], "commit", "-q", "-m", "discovery-card")
    argv = ["fit", "P005-v1", "--strategy", STRATEGY_REF,
            "--cards-dir", str(fit_env["repo"] / "research" / "cards"),
            "--data-root", str(fit_env["store"]),
            "--ledger", str(fit_env["ledger"])]
    out = io.StringIO()
    code = 0
    monkeypatch.chdir(fit_env["tmp"])
    try:
        with contextlib.redirect_stdout(out):
            code = BT.main(argv)
    except SystemExit as e:
        code = e.code
    assert code == 1
    assert "validation" in out.getvalue()
    assert not (fit_env["store"] / "models").exists()


@pytest.fixture()
def fit_env(tmp_path, monkeypatch):
    """真实内置策略（LevelDecisionS1Demo）bt fit 合成数据完整环境：
    临时 git 仓库（卡+轮次已提交）+ 研究数据根 + 已开启轮次台账。"""
    import numpy as np
    import sys as _sys
    _sys.path[:0] = [str(REPO / "src")]
    from mt5data.store import save_bars
    repo = tmp_path / "repo"
    (repo / "research" / "cards").mkdir(parents=True)
    (repo / "research" / "rounds").mkdir(parents=True)
    card_p = repo / "research" / "cards" / "P005-v1.toml"
    card_p.write_text(P2_CARD.format(ref=STRATEGY_REF, segment="validation"),
                      encoding="utf-8")
    round_p = repo / "research" / "rounds" / "R2.toml"
    round_p.write_text('''id = "R2"
opened = "2026-11-20"
candidates = ["C101"]
trial_budget = 4
lockbox_uses_max = 1
dsr_trial_scope = "project"
advance_rule = "关卡 v3"
close_rule = "全部决策卡有结论"
approved_by = "P线审核会话"
[segments]
split = "2019-12-01"
[evaluation.C101]
mode = "standard"
primary = "gates_v3"
''', encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "probe")
    store = tmp_path / "store"
    rng = np.random.default_rng(21)
    n = 17000
    close = 100 + np.cumsum(rng.normal(0, 0.3, n))
    op = close + rng.normal(0, 0.05, n)
    bars = pd.DataFrame({
        "time": pd.date_range("2018-06-01", periods=n, freq="1h"),
        "open": op, "high": np.maximum(op, close) + np.abs(rng.normal(0, 0.12, n)),
        "low": np.minimum(op, close) - np.abs(rng.normal(0, 0.12, n)),
        "close": close, "tick_volume": 10, "spread": 5, "real_volume": 0})
    save_bars(str(store), "XAUUSD", "H1", bars)
    (store / "specs").mkdir(parents=True, exist_ok=True)
    (store / "specs" / "latest.json").write_text(
        json.dumps({"XAUUSD": {"contract_size": 100.0}}), encoding="utf-8")
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "sessions.toml").write_text(SESSIONS_TOML, encoding="utf-8")
    monkeypatch.setattr(BT, "SESSIONS_PATH", cfg / "sessions.toml")
    ledger_path = tmp_path / "ledger.sqlite"
    _open_r2(ledger_path, round_p)
    monkeypatch.setattr(BT, "_bind_strategy_sha",
                        lambda led, card, ref: "f" * 40)
    monkeypatch.setattr(BT, "build_costs",
                        lambda *a, **k: (FlatCosts(), "flat-probe"))
    return {"tmp": tmp_path, "repo": repo, "store": store,
            "ledger": ledger_path, "card": card_p, "round": round_p}


def test_p05_fit_full_run_with_builtin_strategy(fit_env, monkeypatch,
                                                capsys):
    """P-05：真实内置策略（LevelDecisionS1Demo）fit 合成数据完整跑通
    （临时 store+git；standard+validation P2 决策卡）；训练窗止于分界。"""
    import contextlib
    argv = ["fit", "P005-v1", "--strategy", STRATEGY_REF,
            "--cards-dir", str(fit_env["repo"] / "research" / "cards"),
            "--data-root", str(fit_env["store"]),
            "--ledger", str(fit_env["ledger"])]
    out = io.StringIO()
    code = 0
    monkeypatch.chdir(fit_env["tmp"])
    try:
        with contextlib.redirect_stdout(out):
            code = BT.main(argv)
    except SystemExit as e:
        code = e.code
    assert code == 0, out.getvalue()
    text = out.getvalue()
    assert "模型冻结" in text
    models_dir = fit_env["store"] / "models" / "P005-v1"
    files = list(models_dir.glob("*.json"))
    assert len(files) == 1
    import json as _json
    model = _json.loads(files[0].read_text(encoding="utf-8"))
    assert model["prob_version"]
    assert str(model["fit_window"]["end"])[:10] <= "2019-12-01"  # 训练窗 < split
    # __fit__.sizes/policies 已传入（缺则 LevelDecisionV1.fit 会 ValueError）
    assert model["symbols"] == ["XAUUSD"]


def test_p05_fit_rejects_round_toml_modified(fit_env, monkeypatch, capsys):
    """P-05：开轮后改轮次文件（分界）→ sha 不一致拒绝（fit 与运行同一
    铁律 16 口径）。"""
    import contextlib
    p = fit_env["round"]
    p.write_text(p.read_text(encoding="utf-8").replace(
        'split = "2019-12-01"', 'split = "2020-06-01"'), encoding="utf-8")
    argv = ["fit", "P005-v1", "--strategy", STRATEGY_REF,
            "--cards-dir", str(fit_env["repo"] / "research" / "cards"),
            "--data-root", str(fit_env["store"]),
            "--ledger", str(fit_env["ledger"])]
    out = io.StringIO()
    code = 0
    monkeypatch.chdir(fit_env["tmp"])
    try:
        with contextlib.redirect_stdout(out):
            code = BT.main(argv)
    except SystemExit as e:
        code = e.code
    assert code == 1
    assert "不一致" in out.getvalue()
    assert not (fit_env["store"] / "models").exists()
