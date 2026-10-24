"""bt：研究命令行（P4 试验台账与假设卡）。

用法：
    .venv/Scripts/python.exe scripts/bt.py card check research/cards/H001.toml
    .venv/Scripts/python.exe scripts/bt.py screen --card H001 \
        --strategy mt5quant.strategies.library.buy_hold:BuyHold
    .venv/Scripts/python.exe scripts/bt.py ledger stats --card H001
    .venv/Scripts/python.exe scripts/bt.py gate --card H001 \
        --strategy mt5quant.strategies.library.buy_hold:BuyHold --gates G0,G1,G2,G3,G4

约定：
- 先卡后测：卡文件必须已提交到 git，且最后提交时间早于该卡在台账中的
  首次运行（cards.ensure_card_precedes_run）；未提交/事后改动 → 拒绝运行。
- screen 在卡的参数网格 × 卡的 symbols 上跑 A 层（engine_a.run_backtest），
  逐组合写入台账；抛异常的组合也记一行（ok=false + 错误摘要），有失败则
  退出码 1。数据经 mt5data.lockbox.research_bars（锁箱语义）。
- gate：在卡的主品种上按 --params（缺省取卡网格第一个组合）跑一次基准
  运行记入 runs，然后逐关卡评估并把结果写 gates 表；任一关卡未过 →
  退出码 1。门槛文件 config/gates.toml 的 sha256 与冻结值不符 → 拒绝运行
  （不产生任何 runs）。G5（锁箱一次性）必须显式 --approve。
- contract_size 只认 {data-root}/specs/latest.json（与 run_backtest 同规：
  缺则报错，绝不静默按 1 折算）。
- gates_hash：config/gates.toml 存在时取其 sha256，否则空串（T12 起强制核对）。
- 台账缺省 {data-root}/research/ledger.sqlite（data/ 不入库，本地持久）。
"""
import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # 复用 run_backtest 的装配

from mt5quant.backtest.engine_a import ENGINE_VERSION, run_backtest  # noqa: E402
from mt5quant.backtest.report import (  # noqa: E402
    bars_per_year, current_git_commit, data_sha256, summarize,
)
from mt5quant.research import cards  # noqa: E402
from mt5quant.research.ledger import Ledger  # noqa: E402
from mt5quant.validation import gates as vgate  # noqa: E402
from mt5quant.validation import lookahead as vlook  # noqa: E402
from mt5quant.validation import robustness as vrob  # noqa: E402
from mt5quant.validation import stats as vstats  # noqa: E402
from mt5quant.validation import walkforward as vwf  # noqa: E402
from mt5quant.validation.robustness import BacktestSpec  # noqa: E402
from run_backtest import (  # noqa: E402
    ZeroCosts, build_costs, load_series, load_strategy,
    resolve_contract_size,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _gates_hash() -> str:
    """config/gates.toml 的 sha256（T12 冻结后与运行前核对）；缺文件 → 空串。"""
    p = REPO_ROOT / "config" / "gates.toml"
    if not p.is_file():
        return ""
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _card_path(value: str, cards_dir: Path) -> Path:
    """--card 接受 id（H012）或卡文件路径（.toml 结尾）。"""
    if value.lower().endswith(".toml"):
        return Path(value)
    return cards_dir / f"{value}.toml"


# ---------------------------------------------------------------- card check

def cmd_card_check(args) -> int:
    try:
        card = cards.load_card(args.path)
    except cards.CardError as e:
        print(f"✗ {e}")
        return 1
    committed = cards.committed_at(args.path)
    committed_s = (f"已提交 {committed.isoformat()}" if committed is not None
                   else "⚠ 尚未提交到 git（先卡后测：运行前必须 git commit）")
    print(f"✓ {args.path}")
    print(f"  id={card.id} version={card.version} symbols={list(card.symbols)} "
          f"timeframe={card.timeframe}")
    print(f"  参数 {len(card.params)} 个，网格 {len(card.param_grid())} 组；{committed_s}")
    return 0


# ---------------------------------------------------------------- screen

def cmd_screen(args) -> int:
    path = _card_path(args.card, Path(args.cards_dir))
    try:
        card = cards.load_card(path)
    except cards.CardError as e:
        print(f"✗ {e}")
        return 1

    ledger_path = Path(args.ledger) if args.ledger else \
        Path(args.data_root) / "research" / "ledger.sqlite"
    with Ledger(ledger_path) as ledger:
        # 先卡后测：未提交 / 提交晚于首次运行 → 拒绝（不产生任何 runs）
        committed = cards.committed_at(path)
        try:
            cards.ensure_card_precedes_run(
                card.id, committed, ledger.first_run_at(card.id))
        except (cards.CardNotCommittedError, cards.CardLateCommitError) as e:
            print(f"✗ {e}")
            return 1
        ledger.register_card(card.id, card.version, str(path),
                             current_git_commit(), committed)

        # 装配（setup 失败 = 尚无任何组合开跑，fail-fast 不记 runs）
        try:
            strategy_cls = load_strategy(args.strategy)
            sizes = {sym: resolve_contract_size(args.data_root, sym, None)
                     for sym in card.symbols}
            bars_by_sym = {sym: load_series(args.data_root, sym,
                                            card.timeframe, args.end)
                           for sym in card.symbols}
            # 成本按品种各自实测（点差/滑点是 per-symbol 的，不能借用别的品种）
            if args.costs == "auto":
                costs_by_sym = {}
                for sym in card.symbols:
                    costs_by_sym[sym], cost_version = build_costs(
                        args.data_root, sym, args.spread_fraction,
                        args.commission)
            else:
                cost_version = "zero-v0"
                costs_by_sym = {sym: ZeroCosts() for sym in card.symbols}
        except Exception as e:
            print(f"✗ 装配失败（未开跑，无 runs 记录）：{type(e).__name__}: {e}")
            return 1

        gates_hash = _gates_hash()
        combos = card.param_grid()
        failed = 0
        for sym in card.symbols:
            bars = bars_by_sym[sym]
            dsha = data_sha256(bars)
            times = bars["time"]
            data_first = pd_iso(times.iloc[0])
            data_last = pd_iso(times.iloc[-1])
            for params in combos:
                label = f"{sym} {card.timeframe} {params}"
                try:
                    result = run_backtest(
                        strategy_cls(), bars, costs_by_sym[sym], symbol=sym,
                        params=dict(params), contract_size=sizes[sym],
                        volume_lots=args.volume_lots,
                        initial_capital=args.initial_capital,
                        timeframe=card.timeframe)
                    summary = summarize(result, cost_version=cost_version)
                    rid = ledger.record_run(
                        card_id=card.id, card_version=card.version,
                        strategy=args.strategy, params=params, symbol=sym,
                        timeframe=card.timeframe, data_first=data_first,
                        data_last=data_last, data_sha256=dsha,
                        engine_version=ENGINE_VERSION,
                        cost_model_version=cost_version, gates_hash=gates_hash,
                        metrics=summary, actor=args.actor)
                    print(f"✓ run#{rid} {label} -> "
                          f"净利 {summary['net_profit']:.2f} "
                          f"交易 {summary['trade_count']} 笔")
                except Exception as e:  # 失败组合也进台账
                    failed += 1
                    rid = ledger.record_run(
                        card_id=card.id, card_version=card.version,
                        strategy=args.strategy, params=params, symbol=sym,
                        timeframe=card.timeframe, data_first=data_first,
                        data_last=data_last, data_sha256=dsha,
                        engine_version=ENGINE_VERSION,
                        cost_model_version=cost_version,
                        gates_hash=gates_hash, metrics=None,
                        error=f"{type(e).__name__}: {e}", actor=args.actor)
                    print(f"✗ run#{rid} {label} -> {type(e).__name__}: {e}")

        st = ledger.stats(card.id)
        print(f"— 卡 {card.id}：{st['total_runs']} 次运行（成功 {st['ok_runs']} / "
              f"失败 {st['failed_runs']}），台账 {ledger.path}")
        return 1 if failed else 0


def pd_iso(v) -> str:
    """pandas Timestamp → ISO 字符串（台账 data_first/last 用）。"""
    import pandas as pd
    return pd.Timestamp(v).isoformat()


# ---------------------------------------------------------------- gate

def cmd_gate(args) -> int:
    """关卡 G0–G5：基准运行入台账，逐关卡评估写 gates 表。"""
    path = _card_path(args.card, Path(args.cards_dir))
    try:
        card = cards.load_card(path)
    except cards.CardError as e:
        print(f"✗ {e}")
        return 1
    # 门槛冻结核对：sha256 与冻结值不符 → 拒绝运行（不产生任何 runs）
    try:
        cfg, gsha = vgate.load_gates()
    except vgate.GatesConfigError as e:
        print(f"✗ {e}")
        return 1
    wanted = [g.strip().upper() for g in args.gates.split(",") if g.strip()]
    unknown = [g for g in wanted if g not in vgate.GATE_ORDER]
    if unknown:
        print(f"✗ 未知关卡 {unknown}；合法：{list(vgate.GATE_ORDER)}")
        return 1

    ledger_path = Path(args.ledger) if args.ledger else \
        Path(args.data_root) / "research" / "ledger.sqlite"
    with Ledger(ledger_path) as ledger:
        committed = cards.committed_at(path)
        try:
            cards.ensure_card_precedes_run(
                card.id, committed, ledger.first_run_at(card.id))
        except (cards.CardNotCommittedError, cards.CardLateCommitError) as e:
            print(f"✗ {e}")
            return 1
        ledger.register_card(card.id, card.version, str(path),
                             current_git_commit(), committed)
        # G5 预检：未批准 / 已用过 → 拒绝（不烧一次性，也不跑任何运行）
        if "G5" in wanted and not args.approve:
            print("✗ 锁箱（G5）是一次性测试，必须显式 --approve"
                  "（并建议 --approved-by 记录批准人）——本次未执行")
            return 1
        if "G5" in wanted and ledger.lockbox_used(card.id):
            print(f"✗ 卡 {card.id} 的锁箱已用过一次（lockbox_uses 有记录）——"
                  "第二次请求直接拒绝")
            return 1

        # 装配（失败 = 未开跑，fail-fast 不记 runs）
        try:
            strategy = load_strategy(args.strategy)()
            sizes = {sym: resolve_contract_size(args.data_root, sym, None)
                     for sym in card.symbols}
            bars_by_sym = {sym: load_series(args.data_root, sym,
                                            card.timeframe, args.end)
                           for sym in card.symbols}
            if args.costs == "auto":
                costs_by_sym = {}
                for sym in card.symbols:
                    costs_by_sym[sym], cost_version = build_costs(
                        args.data_root, sym, args.spread_fraction,
                        args.commission)
            else:
                cost_version = "zero-v0"
                costs_by_sym = {sym: ZeroCosts() for sym in card.symbols}
        except Exception as e:
            print(f"✗ 装配失败（未开跑，无 runs 记录）：{type(e).__name__}: {e}")
            return 1

        symbol = card.symbols[0]
        bars = bars_by_sym[symbol]
        costs = costs_by_sym[symbol]
        combos = card.param_grid()
        try:
            params = json.loads(args.params) if args.params else dict(combos[0])
        except ValueError as e:
            print(f"✗ --params 不是合法 JSON：{e}")
            return 1

        # 基准运行（引擎 A）：先入台账（失败的也记），关卡结果挂在它名下
        dsha = data_sha256(bars)
        data_first, data_last = pd_iso(bars["time"].iloc[0]), pd_iso(bars["time"].iloc[-1])
        common = dict(
            card_id=card.id, card_version=card.version,
            strategy=args.strategy, params=params, symbol=symbol,
            timeframe=card.timeframe, data_first=data_first,
            data_last=data_last, data_sha256=dsha,
            engine_version=ENGINE_VERSION, cost_model_version=cost_version,
            gates_hash=gsha, actor=args.actor)
        try:
            base = run_backtest(strategy, bars, costs, symbol=symbol,
                                params=dict(params),
                                contract_size=sizes[symbol],
                                volume_lots=args.volume_lots,
                                initial_capital=args.initial_capital,
                                timeframe=card.timeframe)
            summary = summarize(base, cost_version=cost_version)
            run_id = ledger.record_run(metrics=summary, **common)
            print(f"— run#{run_id} {symbol} {card.timeframe} {params} -> "
                  f"净利 {summary['net_profit']:.2f} "
                  f"交易 {summary['trade_count']} 笔")
        except Exception as e:
            run_id = ledger.record_run(metrics=None,
                                       error=f"{type(e).__name__}: {e}", **common)
            print(f"✗ run#{run_id} 基准运行失败：{type(e).__name__}: {e}")
            return 1

        spec = BacktestSpec(strategy=strategy, symbol=symbol,
                            timeframe=card.timeframe,
                            contract_size=sizes[symbol],
                            volume_lots=args.volume_lots,
                            initial_capital=args.initial_capital)
        seed = vstats.rng_seed(card.id)
        results: dict[str, dict] = {}

        def _run(name, fn):
            try:
                results[name] = fn()
            except Exception as e:   # 单关卡炸掉不拖垮其余关卡
                results[name] = {"passed": False,
                                 "error": f"{type(e).__name__}: {e}"}

        if "G0" in wanted:
            _run("G0", lambda: vlook.run_g0(strategy, bars, params))
        if "G1" in wanted:
            _run("G1", lambda: vgate.run_g1(base.trades,
                                            timeframe=card.timeframe, cfg=cfg))
        if "G2" in wanted:
            _run("G2", lambda: vrob.run_g2(
                spec, bars, costs, params, base_result=base,
                base_summary=summary, grid=card.params,
                bars_by_symbol=bars_by_sym, costs_by_symbol=costs_by_sym,
                sizes_by_symbol=sizes, cfg=cfg, seed=seed))
        if "G3" in wanted:
            _run("G3", lambda: vwf.run_g3(
                spec, bars, costs, grid=combos,
                in_sample_sharpe=summary.get("sharpe"), cfg=cfg))
        if "G4" in wanted:
            def _g4():
                returns = base.equity.astype("float64").pct_change().dropna()
                rows = ledger.conn.execute(
                    "SELECT metrics_json FROM runs WHERE card_id=?"
                    " ORDER BY run_id", (card.id,)).fetchall()
                bpy = summary.get("annualization_bars_per_year") \
                    or bars_per_year(card.timeframe)
                trial_sharpes = []
                for r in rows:
                    payload = json.loads(r["metrics_json"])
                    s = (payload.get("metrics") or {}).get("sharpe")
                    if payload.get("ok") and s is not None:
                        trial_sharpes.append(float(s) / math.sqrt(bpy))
                n_trials = ledger.stats(card.id)["total_runs"]
                return vgate.run_g4(
                    returns, n_trials=n_trials, trial_sharpes=trial_sharpes,
                    actual_max_drawdown=summary.get("max_drawdown", 0.0),
                    cfg=cfg, seed=seed)
            _run("G4", _g4)
        if "G5" in wanted:
            def _g5():
                lb_bars = vgate.load_lockbox_segment(args.data_root, symbol,
                                                     card.timeframe)
                lb = run_backtest(strategy, lb_bars, costs, symbol=symbol,
                                  params=dict(params),
                                  contract_size=sizes[symbol],
                                  volume_lots=args.volume_lots,
                                  initial_capital=args.initial_capital,
                                  timeframe=card.timeframe)
                lb_summary = summarize(lb, cost_version=cost_version)
                mc = vrob.random_entry_check(
                    spec, bars, costs, base.trades, summary.get("sharpe"),
                    sims=cfg.random_entry_sims,
                    percentile=cfg.random_entry_percentile, seed=seed)
                mc_sharpes = mc.get("sharpes") or []
                metrics = {"net_profit": lb_summary["net_profit"],
                           "expectancy": lb_summary.get("expectancy"),
                           "sharpe": lb_summary.get("sharpe"),
                           "trade_count": lb_summary["trade_count"]}
                return vgate.run_g5(ledger, card.id,
                                    approved_by=args.approved_by or args.actor,
                                    metrics=metrics, mc_sharpes=mc_sharpes,
                                    cfg=cfg)
            _run("G5", _g5)

        for gate in vgate.GATE_ORDER:
            if gate in results:
                ledger.record_gate(run_id, gate, bool(results[gate]["passed"]),
                                   results[gate])
        report = vgate.render_gate_report(card.id, params, results)
        print(report)
        if args.out:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(report, encoding="utf-8")
            print(f"报告：{out}")
        return 0 if all(r["passed"] for r in results.values()) else 1


# ---------------------------------------------------------------- ledger stats

def cmd_ledger_stats(args) -> int:
    ledger_path = Path(args.ledger) if args.ledger else \
        Path(args.data_root) / "research" / "ledger.sqlite"
    try:
        with Ledger(ledger_path, create=False) as ledger:
            st = ledger.stats(args.card)
    except FileNotFoundError as e:
        print(f"✗ {e}（先跑 bt screen 生成台账）")
        return 1
    print(json.dumps(st, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- 入口

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="bt", description="研究命令行：假设卡检查、网格筛选进台账、台账统计")

    sub = ap.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("card", help="假设卡操作")
    card_sub = p_check.add_subparsers(dest="card_command", required=True)
    pchk = card_sub.add_parser("check", help="校验卡模式并显示提交状态")
    pchk.add_argument("path", help="卡文件路径（research/cards/H012.toml）")
    pchk.set_defaults(func=cmd_card_check)

    p_screen = sub.add_parser("screen", help="在卡的网格上跑 A 层并记入台账")
    p_screen.add_argument("--card", required=True,
                          help="卡 id（H012）或 .toml 文件路径")
    p_screen.add_argument("--strategy", required=True,
                          help="module:Class，如 mt5quant.strategies.library.buy_hold:BuyHold")
    p_screen.add_argument("--cards-dir", default=str(REPO_ROOT / "research" / "cards"))
    p_screen.add_argument("--data-root", default="data")
    p_screen.add_argument("--ledger", default=None,
                          help="台账路径；缺省 {data-root}/research/ledger.sqlite")
    p_screen.add_argument("--costs", choices=("auto", "zero"), default="auto",
                          help="与 run_backtest 同义；测试/冒烟用 zero")
    p_screen.add_argument("--end", default=None,
                          help="只用该 UTC 时刻及之前的数据（锁箱语义同 run_backtest）")
    p_screen.add_argument("--volume-lots", type=float, default=1.0)
    p_screen.add_argument("--initial-capital", type=float, default=10_000.0)
    p_screen.add_argument("--spread-fraction", type=float, default=0.1)
    p_screen.add_argument("--commission", type=float, default=0.0)
    p_screen.add_argument("--actor", default="bt-screen", help="台账 actor 字段")
    p_screen.set_defaults(func=cmd_screen)

    p_gate = sub.add_parser("gate", help="关卡 G0–G5：跑关卡并写 gates 表")
    p_gate.add_argument("--card", required=True,
                        help="卡 id（H012）或 .toml 文件路径")
    p_gate.add_argument("--strategy", required=True,
                        help="module:Class，如 mt5quant.strategies.library.buy_hold:BuyHold")
    p_gate.add_argument("--gates", default="G0,G1,G2,G3,G4",
                        help="逗号分隔的关卡子集；G5（锁箱一次性）须显式列出且加 --approve")
    p_gate.add_argument("--params", default=None,
                        help="JSON 参数；缺省取卡网格第一个组合")
    p_gate.add_argument("--approve", action="store_true",
                        help="批准 G5 锁箱一次性测试（看过一眼即消耗）")
    p_gate.add_argument("--approved-by", default=None,
                        help="G5 批准人（缺省记 --actor）")
    p_gate.add_argument("--cards-dir", default=str(REPO_ROOT / "research" / "cards"))
    p_gate.add_argument("--data-root", default="data")
    p_gate.add_argument("--ledger", default=None)
    p_gate.add_argument("--costs", choices=("auto", "zero"), default="auto",
                        help="与 run_backtest 同义；测试/冒烟用 zero")
    p_gate.add_argument("--end", default=None,
                        help="只用该 UTC 时刻及之前的数据（锁箱语义同 run_backtest）")
    p_gate.add_argument("--volume-lots", type=float, default=1.0)
    p_gate.add_argument("--initial-capital", type=float, default=10_000.0)
    p_gate.add_argument("--spread-fraction", type=float, default=0.1)
    p_gate.add_argument("--commission", type=float, default=0.0)
    p_gate.add_argument("--actor", default="bt-gate", help="台账 actor 字段")
    p_gate.add_argument("--out", default=None, help="关卡报告 Markdown 落盘路径")
    p_gate.set_defaults(func=cmd_gate)

    p_ledger = sub.add_parser("ledger", help="台账操作")
    ledger_sub = p_ledger.add_subparsers(dest="ledger_command", required=True)
    pstat = ledger_sub.add_parser("stats", help="运行计数（试验次数，供 DSR 用）")
    pstat.add_argument("--card", default=None, help="只统计该卡家族（跨版本）")
    pstat.add_argument("--data-root", default="data")
    pstat.add_argument("--ledger", default=None)
    pstat.set_defaults(func=cmd_ledger_stats)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
