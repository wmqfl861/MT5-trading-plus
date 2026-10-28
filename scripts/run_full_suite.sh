#!/usr/bin/env bash
# 全量回归封装（T96/R14-L7：推送前验证失效的事故纠正）。
#
# 背景：第十四轮 L7——本地回归的退出码被转发管道吞掉，语法错误
# （f46fd9e）被推送，CI 红约一小时。本脚本是此后所有代理/会话跑全量
# 回归的标准入口：
#   ./scripts/run_full_suite.sh              # 全量（缺省）
#   ./scripts/run_full_suite.sh tests/test_validation_gates.py -k v3
# 用法要点（守护测试 test_full_suite_script_checks_exit_code_explicitly
# 逐条钉住，不得回退）：
#   - set -o pipefail；
#   - 输出重定向到 full-suite.log（不经 tee 管道——退出码不可能被吞）；
#   - RC=$? 显式保存、打印，脚本按 RC 退出；
#   - RC≠0 时末行给醒目警告：不得推送。
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT" || exit 1

# 解释器：仓库 .venv 优先（Windows: .venv/Scripts/python.exe；
# POSIX: .venv/bin/python）；无 venv 时回退 py -3 / python3 / python
# （Windows 商店别名对 python/python3 报"未安装"——py 启动器放最前）
if [ -x ".venv/Scripts/python.exe" ]; then
    PY=(".venv/Scripts/python.exe")
elif [ -x ".venv/bin/python" ]; then
    PY=(".venv/bin/python")
elif command -v py >/dev/null 2>&1; then
    PY=(py -3)
elif command -v python3 >/dev/null 2>&1; then
    PY=(python3)
else
    PY=(python)
fi

LOG="full-suite.log"
echo "== run_full_suite（T96/R14-L7：RC 显式检查）=="
echo "== 解释器: ${PY[*]}；参数: $*；日志: $LOG =="
"${PY[@]}" -m pytest -q -p no:cacheprovider "$@" > "$LOG" 2>&1
RC=$?
tail -n 20 "$LOG"
echo "RC=$RC"
if [ "$RC" -ne 0 ]; then
    echo "✗ 全量回归未通过（RC=$RC）——不得推送；完整日志：$LOG"
else
    echo "✓ 全量回归通过（RC=0）；完整日志：$LOG"
fi
exit "$RC"
