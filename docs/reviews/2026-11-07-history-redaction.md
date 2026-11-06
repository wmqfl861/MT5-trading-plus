# 历史脱敏说明（2026-11-07）

## 为什么部分文档出现 `<demo-login> @ <demo-server>` 与 `<workspace>/` 占位符

项目约束：公开仓库不得含账号（含模拟盘账号）、服务器名、IP、令牌、本机绝对路径。2026-11-07 全仓回归的卫生测试（`tests/test_repo_hygiene.py::test_repo_tracked_files_are_clean`）发现本地未推送提交中有两处违规：

1. `docs/research/pline-feasibility-prereg-v2-2026-11-06.md`（§1 与 §1-bis）：账户范围行内联了 Demo 账号与服务器名——替换为 `<demo-login> @ <demo-server>` 占位符；实际值保存在 SHA 绑定的本地证据工件（`account-fee-terms.json`，1aa60bd6…）中，语义不变。
2. `research/candidates/C005.precheck.json`（provenance 两行）：本机绝对路径前缀——替换为 `<workspace>/`，其余路径段保留。

该批提交**从未推送**（当时远端 main 停在 CI#307），敏感信息零外泄；处置为推送前对本地未推送区间做逐提交语义中性重放（rebase），替换仅涉及上述字面值，全部代码/规则/数字内容逐字节不变。

## 对既有绑定的影响

- prereg v2 文档字节变化 → 其 blob SHA 变化（原 32aaa92c… 为登记链所钉，脱敏后为 9084d7c9…）→ t121-release-v2 登记按重裁流程以新 blob 重绑（登记审核代理 scoped 重裁，2026-11-07）。
- A1 绑定工件（C005 d00adad5…/H102/R3/FOMC 日历 a53577d9…/各数据工件 SHA）全部为内容哈希，不受重放影响。
- 本文件本身不含任何敏感值。
