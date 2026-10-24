"""MCP 服务配置：mcp.json（host/port/token）。

安全口径（S1，docs/blueprint.md §4 第 9 条"MCP 只经隧道访问"）：服务是明文 HTTP +
Bearer 令牌，令牌明文过公网/局域网会被链路窃听重放，因此 host 只允许回环地址；
远程 agent 一律经隧道连接（SSH 本地端口转发 / Tailscale / WireGuard，
见 docs/ops/mcp-remote-access.md）。确要明文对外监听，必须显式写
"allow_plaintext_public": true（自担风险的逃生门，不静默放行）。

**用户操作清单（S1，由用户手动执行，代码与代理不得代办）**：
1) 把现有 mcp.json 的 host 改回 127.0.0.1 并轮换令牌；
2) 删除 Windows 防火墙里 MCP 端口（如 47621）的入站放行规则；
3) 确认 RDP 未暴露公网，或已限制来源 IP 并开启 NLA；
4) 仓库改 Private，或由用户决定是否用 git filter-repo 清理历史（改写历史必须用户自己决定）。
"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

MIN_TOKEN_LEN = 32
# 回环地址白名单（localhost 大小写不敏感）；非回环 host 一律 fail-closed
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
REMOTE_ACCESS_DOC = "docs/ops/mcp-remote-access.md"


@dataclass(frozen=True)
class MCPServerConfig:
    host: str = "127.0.0.1"
    port: int = 8310
    token: str = ""
    # Host 头防 DNS 重绑定校验；真门锁是 Bearer 令牌，默认关闭（SDK 在本机监听且未显式传参会自动开启，本实现总是显式传参）
    dns_rebinding_protection: bool = False
    allowed_hosts: tuple[str, ...] = ()


def load_mcp_config(path: str | Path) -> MCPServerConfig:
    p = Path(path)
    if not p.exists():
        raise ValueError(f"配置不存在：{p}（先运行 run_mcp_server.py --init 生成）")
    data = json.loads(p.read_text(encoding="utf-8"))
    token = data.get("token", "")
    if not isinstance(token, str):
        raise ValueError(f"token 应为字符串，实际 {type(token).__name__}")
    if not token or len(token) < MIN_TOKEN_LEN:
        raise ValueError(
            f"token 缺失或弱于 {MIN_TOKEN_LEN} 字符——对外服务的唯一门锁，不能弱。"
            "用 secrets.token_urlsafe(32) 生成一个。"
        )
    host = data.get("host", "127.0.0.1")
    port = data.get("port", 8310)
    dns_flag = data.get("dns_rebinding_protection", False)
    hosts = data.get("allowed_hosts", [])
    allow_public = data.get("allow_plaintext_public", False)
    if not isinstance(host, str):
        raise ValueError(f"host 应为字符串，实际 {type(host).__name__}")
    if not isinstance(allow_public, bool):
        raise ValueError(f"allow_plaintext_public 应为 true/false，实际 {allow_public!r}")
    if host.strip().lower() not in LOOPBACK_HOSTS and not allow_public:
        raise ValueError(
            f"host={host!r} 不是回环地址（{'/'.join(sorted(LOOPBACK_HOSTS))}）："
            "明文 HTTP 对外监听会让 Bearer 令牌被链路窃听重放。"
            f"远程 agent 请改走隧道（SSH 本地端口转发 / Tailscale / WireGuard），"
            f"见 {REMOTE_ACCESS_DOC}；确需明文对外监听，"
            '请在 mcp.json 显式写 "allow_plaintext_public": true（自担风险）。'
        )
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        raise ValueError(f"port 应为 1-65535 的整数，实际 {port!r}")
    if not isinstance(dns_flag, bool):
        raise ValueError(f"dns_rebinding_protection 应为 true/false，实际 {dns_flag!r}")
    if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
        raise ValueError("allowed_hosts 应为字符串数组")
    return MCPServerConfig(
        host=host, port=port, token=token,
        dns_rebinding_protection=dns_flag,
        allowed_hosts=tuple(hosts),
    )


def create_default(path: str | Path) -> MCPServerConfig:
    p = Path(path)
    cfg = MCPServerConfig(
        host="127.0.0.1",  # --init 只写回环；远程 agent 走隧道（docs/ops/mcp-remote-access.md）
        port=8310,
        token=secrets.token_urlsafe(32),
    )
    if not p.exists():
        p.write_text(json.dumps(cfg.__dict__, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    return load_mcp_config(p)
