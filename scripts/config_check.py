"""scripts/config_check.py（T52/R9-I2）：Demo 演练配置手数校验入口。

demo_run.ps1 -Mode Demo 的第 9 项预检不再用 Select-String 文本行匹配
（注释、合法多行字符串、表作用域里的同名行都能骗过它——R9-I2 反例：
顶层 0.02 + created_utc 多行字符串里藏一行 volume_lots = 0.01 曾被
放行），改为对每个配置调用与运行器**完全相同**的 mt5live.config.load
（LiveConfig 真解析器，src 布局自插 sys.path，与全仓 scripts 惯例
一致），把实际加载的 TOML 值以单行 JSON 交给 ps1 判定。

输出契约（单行 JSON、**ASCII-only**——Windows 管道在系统代码页下转
中文必 mojibake，错误一律用 error_code 表达，绝不透传异常消息文本）：

    {"ok": true, "volume_lots": 0.01, "has_risk_fraction": false, "error_code": null}
    {"ok": false, "volume_lots": null, "has_risk_fraction": null, "error_code": "toml_syntax"}

error_code 取值：
    usage           命令行参数不是恰好一个配置路径
    not_found       配置文件不存在
    read_error      文件不可读（权限等 OSError）
    toml_syntax     不是合法 TOML（含非 UTF-8 字节）
    invalid_config  TOML 合法但 LiveConfig 校验拒绝（缺必填/未知键/类型/取值）
    internal        解析器自身故障（依赖缺失等）——调用方 fail-closed

退出码：0 = ok=true；2 = ok=false（ps1 侧同时看 JSON 与退出码，双保险）。

用法：
    python scripts/config_check.py data/demo-configs/DEMO-MA-XAUUSD-M15.toml
"""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _result(ok: bool, volume_lots=None, has_risk_fraction=None,
            error_code: str | None = None) -> dict:
    return {"ok": ok, "volume_lots": volume_lots,
            "has_risk_fraction": has_risk_fraction, "error_code": error_code}


def check(path: str) -> dict:
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return _result(False, error_code="not_found")
    except OSError:
        return _result(False, error_code="read_error")
    try:
        tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return _result(False, error_code="toml_syntax")
    try:
        from mt5live.config import load
        cfg = load(path)
    except ValueError:                        # LiveConfig fail-fast 校验
        return _result(False, error_code="invalid_config")
    except Exception:                         # 依赖/解析器自身故障
        return _result(False, error_code="internal")
    return _result(True, volume_lots=cfg.volume_lots,
                   has_risk_fraction=cfg.risk_fraction is not None)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(json.dumps(_result(False, error_code="usage"),
                         ensure_ascii=True))
        return 2
    result = check(argv[1])
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
