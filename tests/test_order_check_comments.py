"""scripts/order_check_comments.py 的矩阵逻辑（FakeMT5 mock 验证）。

真实模拟盘的 order_check 实测：2026-10-27 Demo 停机、终端不可连，未能执行
——脚本矩阵（29/30 字符 + 含 '|' 短注释）先以 mock 验证请求构造与逐组记录
正确；**待真实终端实测**（终端可达时运行脚本，结果回填 trading.py 注释）。
"""
import importlib.util
from collections import namedtuple
from pathlib import Path

from mt5gw.connection import MT5Connection

from tests.fakes import FakeMT5

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "order_check_comments.py"

CheckResult = namedtuple(
    "CheckResult", "retcode balance equity margin margin_free comment")


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "order_check_comments_script", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cases_are_29_30_alnum_and_short_pipe():
    mod = _load_script()
    cases = dict(mod.COMMENT_CASES)
    assert len(cases["29_alnum"]) == 29 and cases["29_alnum"].isalnum()
    assert len(cases["30_alnum"]) == 30 and cases["30_alnum"].isalnum()
    # 含 '|' 的短注释：长度 < 29，把"字符嫌疑"与"长度嫌疑"解耦
    assert "|" in cases["short_pipe"]
    assert len(cases["short_pipe"]) < 29


def test_run_cases_checks_each_comment_and_records_verdicts():
    mod = _load_script()
    fake = FakeMT5()
    seen = []

    def check(req):
        seen.append(req["comment"])
        return CheckResult(retcode=0, balance=0.0, equity=0.0, margin=0.0,
                           margin_free=0.0, comment="")

    fake.set_behavior("order_check", value=check)
    conn = MT5Connection(mt5_module=fake)
    conn.connect()
    results = mod.run_cases(conn, fake, symbol="EURUSD", volume=0.01,
                            price=1.1046, magic=20261022, filling_bits=1)
    assert seen == [c for _, c in mod.COMMENT_CASES]   # 逐组真实送达 order_check
    assert len(results) == 3
    for r in results:
        assert r["check_retcode"] == 0                 # 结果如实记录
        assert r["length"] == len(r["comment"])


def test_run_cases_records_none_result_and_last_error():
    """order_check 无返回（客户端拒绝形态）：retcode=None + last_error 原样记录
    ——真实实测时 -2 拒绝组就靠这两个字段判读。"""
    mod = _load_script()
    fake = FakeMT5()
    fake.set_behavior("order_check", value=None)
    fake.last_error_value = (-2, 'Invalid "comment" argument')
    conn = MT5Connection(mt5_module=fake)
    conn.connect()
    results = mod.run_cases(conn, fake, symbol="EURUSD", volume=0.01,
                            price=1.1046, magic=20261022, filling_bits=1)
    assert all(r["check_retcode"] is None for r in results)
    assert all("-2" in r["last_error"] for r in results)
